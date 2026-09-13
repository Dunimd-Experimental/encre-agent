#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

from __future__ import annotations

"""Agent-run domain handlers: run / retry / resume.

The streaming execution paths that drive the agent loop.  Extracted
verbatim from ``encre.transport.ws`` (architecture refactor Stage 3 /
Task 5.2); behaviour is unchanged, only the layout moved.  Outer-loop
``continue`` statements of the original dispatch chain became ``return``
inside the extracted methods.
"""

import asyncio
import contextlib
import logging
import os
import re
import traceback
from dataclasses import replace
from typing import Any

from encre.protocol.handlers.workspace_store import _apply_workspace_config
from encre.protocol.handlers.base import _inject_context_windows
from encre.server.protocol import ClientResume, ClientRetry, ClientRun
from encre.server.session_manager import SessionState
from encre.utils.tokens import count_message_tokens
from encre.utils.types import AssistantBoundary, ToolResult
from encre.config import ModelConfig

logger = logging.getLogger("encre.transport.ws")


def _sync_model_from_fallback(handler, session, ws):
    """Sync model changes from fallback to _default_config and notify frontend."""
    if session is None:
        return
    try:
        cfg = session.agent.config
        # Check if active_model_index changed (fallback occurred)
        if hasattr(handler, '_default_config') and handler._default_config is not None:
            if cfg.active_model_index != handler._default_config.active_model_index:
                handler._default_config.active_model_index = cfg.active_model_index
                handler._default_config.apply_active_model()
                models_dict = _inject_context_windows([
                    m.to_dict(encrypt_api_keys=False) if isinstance(m, ModelConfig) else m
                    for m in cfg.models
                ])
                import asyncio
                asyncio.ensure_future(handler._send(
                    ws, "models_updated",
                    models=models_dict,
                    active_model_index=cfg.active_model_index,
                ))
    except Exception:
        pass


def _format_attachments(attachments: list[dict]) -> str:
    """Format attachment list into a structured markdown block for the agent.

    Each file is listed with its path so the agent can use tools (file_read,
    pdf, document, etc.) to read the file from disk.  Binary files include
    their path; text files also include inline content.
    """
    if not attachments:
        return ""

    parts: list[str] = []
    for att in attachments:
        name = att.get("name", "unnamed")
        size = att.get("size", 0)
        is_binary = att.get("is_binary", True)
        content = att.get("content", "")
        file_path = att.get("path", "")

        size_str = _fmt_size(size)
        path_str = f" `{file_path}`" if file_path else ""

        if is_binary or not content.strip():
            parts.append(f"- **{name}** ({size_str},{path_str})")
        else:
            ext = os.path.splitext(name)[1].lower()
            lang = _ext_to_lang(ext)
            parts.append(f"- **{name}** ({size_str},{path_str}):\n```{lang}\n{content.rstrip()}\n```")

    if not parts:
        return ""

    return "--- Attached Files ---\n" + "\n".join(parts) + "\n---"


def _fmt_size(bytes: int) -> str:
    if bytes < 1024:
        return f"{bytes} B"
    elif bytes < 1024 * 1024:
        return f"{bytes / 1024:.1f} KB"
    return f"{bytes / (1024 * 1024):.1f} MB"


_LANG_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
    ".jsx": "jsx", ".html": "html", ".css": "css", ".scss": "scss",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".md": "markdown", ".rs": "rust", ".go": "go", ".java": "java",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
    ".sh": "bash", ".bash": "bash", ".ps1": "powershell",
    ".sql": "sql", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".kt": "kotlin", ".dart": "dart", ".vue": "vue", ".svelte": "svelte",
    ".xml": "xml", ".svg": "xml", ".tex": "latex",
    ".gradle": "groovy", ".cmake": "cmake",
    ".dockerfile": "dockerfile", ".makefile": "makefile",
}


def _ext_to_lang(ext: str) -> str:
    return _LANG_MAP.get(ext, "")


class RunHandlers:
    """run / retry / resume streaming execution handlers."""

    async def _h_run(self, ws: Any, msg: ClientRun) -> None:
        #   iClaw mode: route through EventRouter in a task (same session space as
        # adapters)
        if msg.channel == "iclaw" and self._adapter_manager and self._adapter_manager.router:
            router = self._adapter_manager.router
            logger.info("[iclaw] received run: prompt=%.60s session_id=%s adapter_router=%s",
                        msg.prompt, msg.session_id, bool(router))
            # Extract all values needed by the closure outside it to avoid
            # closure variable capture issues across loop iterations.
            iclaw_requested_sid = msg.session_id  # raw frontend value, resolved inside the task
            iclaw_prompt = msg.prompt
            iclaw_system_prompt = msg.system_prompt
            iclaw_default_config = replace(self._default_config, workspace="")

            async def _run_iclaw(*, router=router, iclaw_requested_sid=iclaw_requested_sid, iclaw_default_config=iclaw_default_config, iclaw_prompt=iclaw_prompt, iclaw_system_prompt=iclaw_system_prompt):
                logger.info("[iclaw] task started, acquiring iclaw context")
                async with router.iclaw_context():
                    sid = iclaw_requested_sid
                    if not sid:
                        existing = router.session_manager.try_resume_most_recent(
                            config=iclaw_default_config)
                        if existing is not None:
                            sid = existing.session_id
                            logger.info("[iclaw] resumed most recent session: %s", sid)
                        else:
                            logger.info("[iclaw] no existing session, will create new one")
                    logger.info("[iclaw] calling router.submit_stream sid=%s", sid)

                    try:
                        stream = router.submit_stream(
                            channel_name="iclaw",
                            prompt=iclaw_prompt,
                            session_id=sid,
                            system_prompt=iclaw_system_prompt,
                        )
                        # Resolve the session info once so every event
                        # dispatched downstream carries a concrete
                        # session_id, allowing the desktop UI to
                        # filter by session and prevent one session's
                        # tokens from leaking into another session.
                        iclaw_info = (
                            router.session_manager.get_session(sid)
                            if sid else None
                        )
                        try:
                            async for event in stream:
                                await self._dispatch_event(ws, iclaw_info, event)
                        except asyncio.CancelledError:
                            logger.info("[iclaw] task cancelled")
                            with contextlib.suppress(Exception):
                                await stream.aclose()
                            with contextlib.suppress(Exception):
                                await self._send(ws, "finish", reason="cancelled", session_id=sid)
                        except Exception as e:
                            logger.error("[iclaw] run error: %s", e, exc_info=True)
                            from encre.backends.base import format_backend_error
                            with contextlib.suppress(Exception):
                                await self._send(ws, "finish", reason="error", error=format_backend_error(e), session_id=sid)
                        else:
                            _iclaw_session = router.session_manager.get_session(sid) if sid else None
                            if _iclaw_session and _iclaw_session.agent.telemetry.enabled:
                                with contextlib.suppress(Exception):
                                    await self._send(ws, "telemetry",
                                        data=_iclaw_session.agent.telemetry.get_summary(),
                                        session_id=sid)
                            # Sync model changes from fallback to _default_config and notify frontend
                            _sync_model_from_fallback(self, _iclaw_session, ws)
                    except Exception as e:
                        logger.error("[iclaw] setup error: %s", e, exc_info=True)
                        from encre.backends.base import format_backend_error
                        with contextlib.suppress(Exception):
                            await self._send(ws, "finish", reason="error", error=format_backend_error(e), session_id=sid)

            self._iclaw_task = asyncio.create_task(_run_iclaw())
            return

        if self._workspace_path and os.path.isdir(self._workspace_path):
            run_config = replace(self._default_config, workspace=self._workspace_path)
            _apply_workspace_config(run_config, self._workspace_path)
        else:
            run_config = replace(self._default_config, workspace="")
        if msg.session_id:
            session = self._manager.load_or_create_session(
                msg.session_id, config=run_config)
            self._info = session
            self._current_session_id = session.session_id
        else:
            session = self._get_or_create_session()
            # Temp chat: mark ephemeral sessions immediately so
            # nothing is persisted even if a save is triggered
            # before the run loop starts.
            if msg.temp_chat:
                session.agent.session.metadata["temp_chat"] = True

        self._manager.touch(session.session_id)

        # Same channel-preservation rule as _h_resume: never re-tag an
        # external-channel (iclaw, telegram, qq, ...) session as normal.
        existing_channel = session.agent.session.metadata.get("channel", "")
        if existing_channel in ("", "normal", "iwork"):
            session.agent.session.metadata["channel"] = (
                "iwork" if self._workspace_path else "normal"
            )

        # Temp chat: never persist, never list in sidebar
        if msg.temp_chat:
            session.agent.session.metadata["temp_chat"] = True
            # Don't save this session -- it's ephemeral

        # Log backend identity for debugging
        _bk = session.agent.loop.backend
        if _bk:
            logger.info("[run] backend type=%s model=%s api_key=%s...",
                        type(_bk).__name__, getattr(_bk, "model", "?"),
                        (getattr(_bk, "api_key", "") or "")[:8])

        if session.state != SessionState.IDLE:
            await self._send(ws, "error", message="Session already running", code="busy",
                             session_id=session.session_id)
            return

        acquired = await self._manager.acquire_slot()
        if not acquired:
            await self._send(ws, "error",
                message="Server at capacity, try later", code="capacity",
                session_id=session.session_id)
            return

        self._manager.set_session_state(session.session_id, SessionState.RUNNING)
        # If the backend was force-closed during a previous cancel
        # (to abort an in-flight API request), rebuild it now.
        with contextlib.suppress(Exception):
            session.agent.rebuild_backend()
        prompt = msg.prompt
        system_prompt = msg.system_prompt
        mode_prompt = msg.mode_prompt or ""

        # Strip raw '<mode>...</mode>' markers from the prompt so
        # they never reach the model as literal user text.  Legacy
        # clients fabricate them when the user sends an empty
        # message inside a mode; the real mode lives in msg.mode /
        # msg.mode_prompt and is injected into the system prompt.
        _mode_marker = re.match(
            r"^\s*<mode>\s*([A-Za-z0-9_]+)\s*</mode>\s*$", str(prompt)
        )
        if _mode_marker and not msg.mode:
            msg.mode = _mode_marker.group(1)
        _stripped_prompt = re.sub(
            r"<mode>\s*[A-Za-z0-9_]+\s*</mode>", "", str(prompt)
        ).strip()
        if _stripped_prompt != prompt:
            prompt = _stripped_prompt

        if msg.attachments:
            # Check if the active model supports multimodal.
            active_model = session.agent.config.get_active_model()
            is_multimodal = active_model and (
                active_model.multimodal or False
            ) and getattr(
                active_model, "multimodal_support", "unknown"
            ) != "unsupported"

            if is_multimodal and any(
                a.get("mime_type", "").startswith("image/") for a in msg.attachments
            ):
                # Build a multimodal content block: text + image_url items.
                content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
                for att in msg.attachments:
                    mime = att.get("mime_type", "")
                    if mime.startswith("image/"):
                        data = att.get("content", "")
                        if data:
                            content.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{data}"},
                            })
                        else:
                            file_path = att.get("path", "")
                            if file_path:
                                content.append({"type": "text", "text": f"[Attached image: {att.get('name', 'file')} at `{file_path}`]"})
                    else:
                        name = att.get("name", "file")
                        file_path = att.get("path", "")
                        path_info = f" at `{file_path}`" if file_path else ""
                        content.append({"type": "text", "text": f"[Attached: {name}{path_info}]"})
                session.session.add_message("user", content)
                # Set prompt empty since we already built the combined content
                prompt = ""
            else:
                attachment_block = _format_attachments(msg.attachments)
                if attachment_block:
                    prompt = attachment_block + "\n\n" + prompt

        if msg.specialty and msg.specialty != "general":
            session.agent.loop.prompt_builder._specialty = msg.specialty
        # Wire the session's OWN spec engine into the loop so spec mode can
        # parse specs and enforce the approval gate.  Each session owns its
        # engine (see _spec_engine_for) -- a connection-wide singleton let
        # one session's spec state bleed into the next conversation.
        session.agent.loop.spec_engine = self._spec_engine_for(session)

        active_agent = session.agent.config.get_active_agent()
        if active_agent is not None:
            if not msg.system_prompt:
                system_prompt = active_agent.system_prompt
            if not msg.specialty:
                session.agent.loop.prompt_builder._specialty = "general"
            if active_agent.max_turns > 0:
                session.agent.config.max_turns = active_agent.max_turns
            if active_agent.permission_mode:
                session.agent.config.permission_mode = active_agent.permission_mode

        # Mode transitions.  The desktop frontend echoes the
        # active mode on every ``run`` message (inline chip or the
        # persisted mode).  Treat an explicit ``mode`` that differs
        # from the session's persisted mode as a real transition
        # (the user typed /plan or /spec, or switched modes) and
        # drive it through the single entry point so the string,
        # metadata mirror and derived ``plan_mode_active`` flag
        # stay consistent, then broadcast ``mode_changed`` so the
        # toolbar chip / exit button appear.  When there is no
        # explicit mode, just echo the persisted mode into config
        # for this run WITHOUT touching the persistent slot --
        # this avoids the old "sticky restore" bug where a one-off
        # /plan kept replaying across every later normal message.
        _es_meta = session.agent.session.metadata
        _persisted = _es_meta.get("slash_command_mode", "") or ""
        if msg.mode and msg.mode != _persisted:
            await self._apply_mode(ws, session, msg.mode)
        else:
            session.agent.config.slash_command_mode = msg.mode or _persisted
        logger.info("[run] slash_command_mode resolved to: '%s' (msg.mode='%s', persisted='%s')",
                    session.agent.config.slash_command_mode, msg.mode, _persisted)

        # Echo the persisted slash *command* into config for this
        # run so its ``command_instructions`` block is re-injected.
        # Activation/clearing is handled by the dedicated
        # ``set_command`` message; here we only keep the in-memory
        # mirror in sync with the persisted slot (e.g. after a
        # session switch where config was not yet restored).
        if not getattr(session.agent.config, "active_command", None):
            session.agent.config.active_command = (
                _es_meta.get("active_command") or None
            )

        session.agent.add_message("user", prompt, mode=session.agent.config.slash_command_mode)
        # Immediately update the in-memory index and broadcast so
        # the sidebar shows the new entry before the model responds.
        if not session.agent.session.metadata.get("temp_chat"):
            self._manager._index_add(session)
            self._broadcast_sessions()
            # Persist to disk in the background (non-blocking).
            self._manager._schedule_save(session)
        logger.info("[run] session=%s workspace=%s", session.session_id[:8], self._workspace_path or "(none)")

        # Auto-name: fire-and-forget so conversation is not delayed.
        sess = session.agent.session
        current_name = session.metadata.get("name", "") or sess.metadata.get("name", "")
        # Temp chats must never be auto-named: rename would leak them
        # into the on-disk index and thus the sidebar.
        if (
            current_name.startswith("New Session")
            and sess.turn_count <= 1
            and not sess.metadata.get("temp_chat")
        ):
            _sid = session.session_id
            _p = prompt
            _t = asyncio.ensure_future(self._auto_name_and_rename(session, _p))
            self._tasks.add(_t)

        # Don't block on background code index -- let the agent run
        # immediately. The code index becomes available asynchronously.
        if self._index_manager and self._current_ws_id:
            task = self._index_manager.get_task(self._current_ws_id)
            if task and not task.done():
                logger.info("[run] index still building, running agent without full index")

        # Wire the engine-install requester's immediate emit
        # hook so the desktop dialog pops up the moment a
        # browser / desktop action needs the engine, without
        # waiting for the agent's event loop to tick.
        async def _emit_engine(evt: Any, session=session) -> None:
            try:
                await self._dispatch_event(ws, session, evt)
            except Exception as exc:
                logger.warning("engine emit failed: %s", exc)
        try:
            session.agent.set_engine_emit(_emit_engine)
        except Exception:
            logger.debug("agent has no set_engine_emit", exc_info=True)

        # Snapshot the active_command before the task is created
        # so the _run_agent closure captures it by value, not by
        # reference, avoiding the race with the frontend's
        # set_command clear message.
        _active_command = getattr(session.agent.config, "active_command", None)

        async def _run_agent(*, session=session, prompt=prompt, system_prompt=system_prompt, mode_prompt=mode_prompt, _saved_command=_active_command):
            try:
                # Restore active_command that may have been cleared
                # by the frontend's set_command clear message (sent
                # right after the run message, creating a race).
                if _saved_command and not getattr(session.agent.config, "active_command", None):
                    session.agent.config.active_command = _saved_command
                async for event in session.agent.run(
                    prompt=prompt, system_prompt=system_prompt,
                    custom_instructions=mode_prompt):
                    await self._dispatch_event(ws, session, event)
                    # Mid-turn checkpoint & real-time canvas update
                    if isinstance(event, ToolResult | AssistantBoundary):
                        # Push context usage to canvas panel so the
                        # progress bar updates in real time
                        ctx_msgs = session.agent.session.get_context_messages()
                        ctx_tokens = count_message_tokens(ctx_msgs)
                        window = session.agent.loop.backend.context_window_size() if session.agent.loop.backend else 0
                        await self._send(ws, "context_usage",
                            context_tokens=ctx_tokens,
                            context_window=window,
                            session_id=session.session_id)
                        # Stream telemetry summary so the canvas panel
                        # (Compactions / Tool Calls) updates in real
                        # time, not only on agent-finish.
                        if session.agent.telemetry.enabled:
                            with contextlib.suppress(Exception):
                                await self._send(ws, "telemetry",
                                    data=session.agent.telemetry.get_summary(),
                                    session_id=session.session_id)
                        if not session.agent.session.metadata.get("temp_chat"):
                            with contextlib.suppress(Exception):
                                await self._manager._save_session_async(session)
            except asyncio.CancelledError:
                await self._send(ws, "finish", reason="cancelled", session_id=session.session_id)
            except Exception as e:
                logger.error(f"Agent run failed: {e}\n{traceback.format_exc()}")
                from encre.backends.base import format_backend_error
                from encre.errors import classify_error_code
                err_msg = format_backend_error(e)
                err_code = classify_error_code(err_msg).value
                with contextlib.suppress(Exception):
                    await self._send(ws, "error", message=err_msg, code=err_code, category="unknown", retryable=False, details={}, session_id=session.session_id)
                with contextlib.suppress(Exception):
                    await self._send(ws, "finish", reason="error", error_code=err_code, session_id=session.session_id)
            finally:
                # Sticky commands (activated via ``set_command``)
                # persist across runs by design and are only
                # cleared by an explicit ``set_command`` with an
                # empty name.  One-shot ``mode_prompt`` runs never
                # touch ``config.active_command``, so there is
                # nothing to clear here -- the previous
                # unconditional clear() was wiping persistent
                # commands after every single run, making custom
                # commands "enter and immediately exit".
                pass
                if session.agent.telemetry.enabled:
                    with contextlib.suppress(Exception):
                        summary = session.agent.telemetry.get_summary()
                        await self._send(ws, "telemetry", data=summary, session_id=session.session_id)
                # Sync model changes from fallback to _default_config and notify frontend
                _sync_model_from_fallback(self, session, ws)
                # Only release state when this task is still the
                # current owner -- a new run may have already taken
                # over, and we must NOT clear its state
                # or release its semaphore slot.
                if session.agent_task is asyncio.current_task():
                    self._manager.set_session_state(session.session_id, SessionState.IDLE)
                    self._manager.release_slot()
                    if not session.agent.session.metadata.get("temp_chat"):
                        await self._manager._save_session_async(session)
                    self._manager.notify_session_completed()
                    session.agent_task = None

        # Ensure any previous agent task is fully finished before
        # starting a new one -- otherwise two _run_impl generators
        # run on the same EncreLoop instance, corrupting shared
        # state (session.messages, _cancel_event, caches, etc.).
        if session.agent_task and not session.agent_task.done():
            session.agent.loop.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(session.agent_task, timeout=0.5)
        session.agent_task = asyncio.create_task(_run_agent())

    async def _h_resume(self, ws: Any, msg: ClientResume) -> None:
        # Use workspace config if currently in workspace mode
        if self._workspace_path and os.path.isdir(self._workspace_path):
            resume_config = replace(self._default_config, workspace=self._workspace_path)
            _apply_workspace_config(resume_config, self._workspace_path)
        else:
            resume_config = replace(self._default_config, workspace="")
        if msg.session_id:
            session = self._manager.load_or_create_session(
                msg.session_id, config=resume_config)
            self._info = session
        else:
            session = self._get_or_create_session()
        self._current_session_id = session.session_id
        # Tag the resumed session with the correct channel for the current
        # mode -- but never overwrite an external channel (iclaw, telegram,
        # qq, ...).  Those sessions belong to their platform adapter and
        # must not leak into the normal-mode sidebar through a channel
        # re-tag; resuming them from global search is read-only.
        existing_channel = session.agent.session.metadata.get("channel", "")
        if existing_channel in ("", "normal", "iwork"):
            session.agent.session.metadata["channel"] = (
                "iwork" if self._workspace_path else "normal"
            )
        # Reconcile state from the actual task state -- if the
        # task is still alive, the session is definitely running even
        # if the finally block has not fired yet.  A session parked on
        # a permission prompt is alive but must keep its
        # AWAITING_APPROVAL state, otherwise switching back to it would
        # report (and render) "running" and drop the consent indicator.
        if (
            session.agent_task is not None
            and not session.agent_task.done()
            and session.state != SessionState.AWAITING_APPROVAL
        ):
            self._manager.set_session_state(session.session_id, SessionState.RUNNING)
        sess = session.agent.session
        sess.mark_messages_dirty()
        sess.ensure_artifacts_from_messages()
        msgs = self._renderer_session_messages(sess)
        branches_list = [b.__dict__ for b in sess.branches.values()]
        state_value = self._manager.get_session_state(session.session_id)
        await self._send(ws, "session_ready", session_id=session.session_id, messages=msgs,
                         plan_items=sess.plan_items, artifacts=sess.artifacts, references=sess.references,
                         branches=branches_list, active_branch_id=sess.active_branch_id,
                         state=state_value, request_id=msg.request_id,
                         channel=session.agent.session.metadata.get("channel", "normal"))
        await self._send_session_mode(ws, session)
        await self._send_session_command(ws, session)

    async def _h_retry(self, ws: Any, msg: ClientRetry) -> None:
        sid = msg.session_id or self._current_session_id
        if not sid:
            await self._send(ws, "error", message="No active session", code="no_session")
            return
        info = self._manager.get_session(sid)
        if info is None:
            info = self._manager.load_or_create_session(sid, config=self._default_config)
        self._manager.touch(sid)
        sess = info.agent.session
        mode = getattr(msg, "mode", "normal")
        # Save the old assistant content BEFORE branching, so we can
        # pass it as context for detailed/concise retry.
        old_assistant_content = None
        if mode in ("detailed", "concise"):
            branch_msgs = sess.get_branch_messages(sess.active_branch_id)
            user_count = 0
            for i, m in enumerate(branch_msgs):
                if m.get("role") == "user":
                    if user_count == msg.user_message_index:
                        for j in range(i + 1, len(branch_msgs)):
                            if branch_msgs[j].get("role") == "assistant":
                                old_raw = branch_msgs[j].get("content", "")
                                if isinstance(old_raw, list):
                                    texts = [
                                        b.get("text", "")
                                        for b in old_raw
                                        if isinstance(b, dict) and b.get("type") == "text"
                                    ]
                                    old_assistant_content = " ".join(texts)
                                else:
                                    old_assistant_content = str(old_raw)
                                break
                        break
                    user_count += 1
        try:
            user_msg, _new_branch = sess.retry_at_user_index(msg.user_message_index)
        except ValueError as e:
            await self._send(ws, "error", message=str(e), code="retry_error",
                             session_id=sid)
            return
        # Retry invalidates the compact summary: the conversation
        # diverges at this point, so the old summary (which
        # summarised the previous branch's "future") is now
        # misleading.  Clear it so the next compact regenerates
        # from the new branch's state.
        sess.metadata.pop("user_requirements_summary", None)
        # Notify frontend about the new branch so the branch switcher updates.
        branches_list = [b.__dict__ for b in sess.branches.values()]
        # Send the correct messages for the new branch so the frontend
        # can render only the active branch's messages.
        msgs = self._renderer_session_messages(sess)
        await self._send(ws, "branch_updated",
            session_id=sid,
            active_branch_id=sess.active_branch_id,
            branches=branches_list,
            messages=msgs)
        if user_msg:
            if mode == "detailed" and old_assistant_content:
                user_msg += f"\n\nBelow is my previous response 鈥?please rewrite it to be more detailed and thorough, expanding on all points with deeper explanations:\n\n{old_assistant_content}"
            elif mode == "concise" and old_assistant_content:
                user_msg += f"\n\nBelow is my previous response 鈥?please rewrite it to be more concise, keeping only the essential information:\n\n{old_assistant_content}"
            elif mode == "detailed":
                user_msg += "\n\n(Please provide a more detailed response with thorough explanations and comprehensive coverage.)"
            elif mode == "concise":
                user_msg += "\n\n(Please provide a concise response, keeping it brief and to the point.)"
            self._current_session_id = sid
            self._info = info
            self._manager.set_session_state(sid, SessionState.RUNNING)
            acquired = await self._manager.acquire_slot()
            if not acquired:
                await self._send(ws, "error",
                    message="Server at capacity, try later", code="capacity",
                    session_id=sid)
                self._manager.set_session_state(sid, SessionState.IDLE)
                return

            async def _run_retry(session_id: str, prompt: str, info=info):
                try:
                    async for event in info.agent.run(prompt=prompt):
                        await self._dispatch_event(ws, info, event)
                        if isinstance(event, ToolResult | AssistantBoundary):
                            with contextlib.suppress(Exception):
                                await self._manager._save_session_async(info)
                except asyncio.CancelledError:
                    await self._send(ws, "finish", reason="cancelled", session_id=session_id)
                except Exception as e:
                    logger.error(f"Retry run failed: {e}\n{traceback.format_exc()}")
                    with contextlib.suppress(Exception):
                        await self._send(ws, "error", message=str(e), code="execution_error", session_id=session_id)
                    with contextlib.suppress(Exception):
                        await self._send(ws, "finish", reason="error", session_id=session_id)
                finally:
                    if info.agent_task is asyncio.current_task():
                        self._manager.set_session_state(session_id, SessionState.IDLE)
                        self._manager.release_slot()
                        await self._manager._save_session_async(info)
                        # Sync model changes from fallback to _default_config and notify frontend
                        _sync_model_from_fallback(self, info, ws)
                        info.agent_task = None

            # Ensure previous task is done before starting retry
            if info.agent_task and not info.agent_task.done():
                info.agent.loop.cancel()
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(info.agent_task, timeout=0.5)
            info.agent_task = asyncio.create_task(_run_retry(sid, user_msg))
        else:
            await self._send(ws, "error", message="Original user message not found", code="retry_error",
                             session_id=sid)

    def _load_sub_agent_messages(self, session_id: str) -> list[dict[str, Any]] | None:
        """Load messages from a sub-agent session directory.

        Sub-agent sessions created by :meth:`EncreLoop._run_sub_agent`
        are persisted under ``<data_dir>/sub_agents/<sid>/`` as a
        directory of turn files. This method reads them back in order
        and returns the flat message list for the automation history
        view.
        """
        if not session_id:
            return None
        try:
            from encre.config import EncreConfig, get_data_dir
            from encre.session import EncreSession
            sess_dir = get_data_dir() / "sub_agents" / session_id
            if not sess_dir.is_dir():
                return None
            cfg = EncreConfig()
            session = EncreSession.load_from_dir(str(sess_dir), config=cfg)
            return [dict(m) for m in session.messages]
        except Exception:
            logger.warning("[automation] failed to load sub-agent session %s", session_id, exc_info=True)
            return None
