#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
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

"""Agent-run domain handlers: :class:`ClientRun`, :class:`ClientRetry`,
:class:`ClientResume`.

This module owns the three streaming execution paths that drive the
agent loop and is one of the mixin families composed into
:class:`encre.transport.ws.EncreWSHandler` (wired into its ``_DISPATCH``
table under the ``ClientRun`` / ``ClientRetry`` / ``ClientResume``
message types).

All three handlers resolve their session through
:class:`encre.server.session_manager.SessionManager` (slot acquisition,
state transitions, ``load_or_create_session`` / ``create_session``) and
then stream agent events from the Rust-backed loop back over the socket
through :func:`encre.protocol.handlers.events` dispatch.

Data flow: the client posts a ``run`` message -> workspace config is
layered onto ``_default_config`` (:func:`_apply_workspace_config`) ->
session state flips to ``SessionState.RUNNING`` under an acquired slot ->
``session.agent.run()`` (backed by :mod:`encre.loop`, the PyO3-bound
Encre Agent Core) yields tool-call / assistant / boundary events -> each event
is re-emitted as a WebSocket frame via ``_dispatch_event`` -> teardown
releases the slot and returns the session to ``SessionState.IDLE``.

Public API:
  - :meth:`RunHandlers._h_run` -- primary streaming execution path.
  - :meth:`RunHandlers._h_resume` -- replay a session's history.
  - :meth:`RunHandlers._h_retry` -- branch at a user message and re-run.

Control-flow note: in the original monolithic dispatch chain these
methods sat inside a ``while True`` loop and used bare ``continue`` to
restart the loop.  Outside the loop, every such ``continue`` became an
explicit ``return``.
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
from encre.capabilities.process import get_session_files_dir
from encre.model_selection import model_identity
from encre.protocol.handlers.base import _inject_context_windows
from encre.protocol.handlers.events import EventDispatchMixin
from encre.server.protocol import (
    ClientQueueList,
    ClientQueueMove,
    ClientQueuePop,
    ClientQueueRemove,
    ClientQueueSteer,
    ClientResume,
    ClientRetry,
    ClientRun,
)
from encre.server.session_manager import SessionState
from encre.utils.tokens import count_message_tokens
from encre.utils.types import AssistantBoundary, TextDelta, ThinkingDelta, ToolResult
from encre.config import ModelConfig

logger = logging.getLogger("encre.transport.ws")

# Streamed deltas are merged before they hit the wire: one encrypted frame per
# token couples the model stream to JSON encoding, AES-GCM, base64 and a socket
# write, and makes the client decrypt+parse each token separately. One frame
# per 50 ms window is visually indistinguishable from per-token frames but cuts
# that transport work by several times.
_STREAM_DELTA_WINDOW = 0.05
_STREAM_DELTA_MAX_CHARS = 2048


async def _coalesce_stream_deltas(events: Any, *, window: float = _STREAM_DELTA_WINDOW):
    """Merge consecutive text/thinking deltas into batched wire events.

    Deltas of the same type arriving inside one ``window`` are concatenated;
    any structural event (tool call, boundary, finish, ...) flushes the pending
    batch first so ordering and turn boundaries are preserved. The first batch
    is emitted immediately, so time-to-first-token is unchanged.

    Args:
        events: Async iterator of agent events.
        window: Maximum age of a pending batch, in seconds.

    Yields:
        The original non-delta events untouched, plus one merged delta event
        per flush window.
    """
    loop = asyncio.get_running_loop()
    pending: TextDelta | ThinkingDelta | None = None
    last_flush = loop.time() - window
    async for event in events:
        if isinstance(event, (TextDelta, ThinkingDelta)):
            if pending is not None and type(pending) is type(event):
                pending = replace(pending, text=pending.text + event.text)
            else:
                if pending is not None:
                    yield pending
                pending = event
            now = loop.time()
            if now - last_flush >= window or len(pending.text) >= _STREAM_DELTA_MAX_CHARS:
                yield pending
                pending = None
                last_flush = now
        else:
            if pending is not None:
                yield pending
                pending = None
            last_flush = loop.time()
            yield event
    if pending is not None:
        yield pending


# How many trailing messages the tail-first session open ships in the
# ``session_ready`` frame. Enough to fill several screens so the session
# opens feeling complete; the older history follows in a backfill frame.
_HISTORY_TAIL_SIZE = 60


def _sync_model_from_fallback(handler, session, ws):
    """Propagate a fallback-induced model change back to the handler config.

    When the active backend cannot serve a request the Rust loop falls
    back to another model in the pool and records the new index on the
    session's live config.  :class:`encre.server.session_manager.SessionManager`
    configs are per-session, so the switch would be invisible to every
    other session and to the model selector unless it is written back.

    In general mode the write-back lands on ``handler._default_config`` --
    the config that :func:`encre.config.EncreConfig.get_active_model` reads
    for new sessions and gateway adapters.  In workspace mode the default
    is intentionally untouched (one workspace's fallback must not leak into
    another); the workspace config remains the source of truth.

    Either way the conversation's own metadata records the model it actually
    ran on, and the UI is notified asynchronously (fire-and-forget) with a
    ``models_updated`` frame carrying the full model list, the new index and
    the owning session id.

    Args:
        handler: The composed :class:`encre.transport.ws.EncreWSHandler`
            instance (duck-typed: needs ``_default_config`` and ``_send``).
        session: The owning session, or ``None`` when there is nothing
            to reconcile.
        ws: The WebSocket the notification is sent to.

    Note:
        Every step is guarded; a failed sync must never disturb the run
        that just finished, so failures are swallowed silently.
    """
    if session is None:
        return
    try:
        cfg = session.agent.config
        # Remember what this conversation actually ran on.  In general mode
        # the identity is restored on the next open; in workspace mode the
        # workspace config stays authoritative, so this is only a fallback.
        session_obj = getattr(session.agent, "session", None)
        if (
            session_obj is not None
            and 0 <= getattr(cfg, "active_model_index", -1) < len(cfg.models)
        ):
            identity = model_identity(cfg.models[cfg.active_model_index])
            if identity is not None:
                session_obj.metadata["active_model"] = identity
        try:
            handler._manager._schedule_save(session)
        except Exception:
            pass
        # Workspace sessions share one model per project: a per-run fallback
        # must not rewrite the connection default (that would leak the
        # fallback into other workspaces and general mode).  The selector
        # still has to hear about it for THIS conversation.
        in_workspace = bool((getattr(handler, "_workspace_path", "") or "").strip())
        default_cfg = getattr(handler, "_default_config", None)
        changed = not in_workspace and default_cfg is not None and (
            cfg.active_model_index != default_cfg.active_model_index
        )
        if changed:
            default_cfg.active_model_index = cfg.active_model_index
            default_cfg.apply_active_model()
        if changed or in_workspace:
            models_dict = _inject_context_windows([
                m.to_dict(encrypt_api_keys=False) if isinstance(m, ModelConfig) else m
                for m in cfg.models
            ])
            import asyncio
            asyncio.ensure_future(handler._send(
                ws, "models_updated",
                models=models_dict,
                active_model_index=cfg.active_model_index,
                session_id=session.session_id,
            ))
    except Exception:
        pass


def _format_attachments(attachments: list[dict]) -> str:
    """Render the ``run`` attachment list as a markdown preamble.

    The preamble is prepended to the user prompt for non-multimodal
    models, where attachments cannot travel as ``image_url`` content
    parts.  Each entry carries the absolute on-disk path inside code
    spans so the agent's filesystem tools (``file_read``, ``pdf``,
    ``document``) can load the payload on demand instead of receiving a
    base64 blob inline.

    Args:
        attachments: The raw attachment dicts delivered by the desktop
            client; keys are ``name``, ``size``, ``is_binary``,
            ``content`` and ``path``.

    Returns:
        A fenced markdown block, or an empty string when there is
        nothing to report.

    Note:
        Binary payloads (``is_binary`` true or blank content) are listed
        by path only; the bytes are never inlined.  Text payloads are
        inlined inside a language-tagged fence using
        :func:`_ext_to_lang`, which bounds prompt size to what the model
        can actually consume.
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
    """Format a byte count as a compact human-readable size string.

    Args:
        bytes: The size of the attachment in bytes.

    Returns:
        A string such as ``"512 B"``, ``"24.5 KB"``, or ``"1.3 MB"``.
        Sizes at or above one megabyte collapse into a single ``MB``
        bucket -- attachment lists are a prompt preamble, not a file
        manager, so finer granularity buys nothing.
    """
    if bytes < 1024:
        return f"{bytes} B"
    elif bytes < 1024 * 1024:
        return f"{bytes / 1024:.1f} KB"
    return f"{bytes / (1024 * 1024):.1f} MB"


# Maps a lowercased file extension to the fenced-code language tag used
# when an inlined attachment is rendered into the prompt.  Keeping the
# tag lets the model's tokenizer and any downstream syntax highlighter
# treat the block correctly, which measurably improves extraction from
# code and config files.  Extensions absent from the table fall back to
# an empty tag (plain fenced block) rather than failing.
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
    """Look up the fenced-code language tag for a file extension.

    Args:
        ext: Lowercased extension including the leading dot, e.g.
            ``".py"`` (see :func:`os.path.splitext`).

    Returns:
        The language tag, or ``""`` when the extension is unknown.  An
        empty tag still produces a valid fenced block, so callers never
        need a separate branch for the miss case.
    """
    return _LANG_MAP.get(ext, "")


class RunHandlers:
    """Streaming execution mixin: ``run`` / ``retry`` / ``resume``.

    Composed into :class:`encre.transport.ws.EncreWSHandler` together
    with the other domain mixins; every method here assumes the host
    exposes the shared handler surface (``_manager``, ``_default_config``,
    ``_workspace_path``, ``_adapter_manager``, ``_info``, ``_send``,
    ``_dispatch_event``, ``_tasks``).  The class itself holds no state.

    Invariants maintained across the three handlers:

    - A session transitions to ``SessionState.RUNNING`` only after
      :func:`encre.server.session_manager.SessionManager.acquire_slot`
      succeeds, and back to ``IDLE`` only from the task that owns it,
      so a superseded run never releases a slot a newer run holds.
    - ``session.agent_task`` always points at the task currently owning
      the loop, and is cleared to ``None`` on teardown.
    - Temp chats are marked in session metadata before any save can run,
      so they are never indexed, never broadcast, and never auto-named.

    Async-safety: each handler spawns its agent run as a tracked task
    rather than awaiting it, so the WebSocket dispatch loop stays
    responsive and cancellation can be driven from the socket side.
    """

    async def _h_run(self, ws: Any, msg: ClientRun) -> None:
        """Execute one prompt turn against the resolved session.

        The main agent entry point.  Resolution order:

        1. ``iclaw`` channel traffic bypasses this handler's session
           manager entirely and is routed through the gateway
           :class:`encre.gateway` EventRouter, because iClaw shares the
           adapter session space rather than owning its own.
        2. Otherwise the slot is acquired, the session is loaded or
           created, and the run is spawned as a tracked task so the WS
           dispatch loop stays responsive and cancellation can be driven
           from the socket side.

        Every closure captures its inputs through keyword defaults, which
        pins each call's arguments to the current request rather than the
        loop variable -- the classic late-binding hazard in a per-message
        dispatch loop.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientRun` request carrying the prompt,
                optional session id, channel, temp-chat flag and
                subagent context.
        """
        # iClaw traffic shares the adapter session space: it is routed
        # through the gateway EventRouter instead of this handler's own
        # SessionManager, and runs in a tracked background task.
        if msg.channel == "iclaw" and self._adapter_manager and self._adapter_manager.router:
            router = self._adapter_manager.router
            logger.info("[iclaw] received run: prompt=%.60s session_id=%s adapter_router=%s",
                        msg.prompt, msg.session_id, bool(router))
            # Snapshot every value the closure needs before defining it.
            # Passing them as keyword defaults also pins each call's
            # arguments to the *current* request rather than the loop
            # variable, which is the classic late-binding hazard in a
            # per-message dispatch loop.
            iclaw_requested_sid = msg.session_id  # raw frontend value, resolved inside the task
            iclaw_prompt = msg.prompt
            iclaw_system_prompt = msg.system_prompt
            iclaw_default_config = replace(self._default_config, workspace="")

            async def _run_iclaw(*, router=router, iclaw_requested_sid=iclaw_requested_sid, iclaw_default_config=iclaw_default_config, iclaw_prompt=iclaw_prompt, iclaw_system_prompt=iclaw_system_prompt):
                """Execute one iClaw prompt against the EventRouter session space.

                Runs inside :func:`EventRouter.iclaw_context`, which
                establishes the routing scope used by IM adapters, so
                the resulting session lands in the same manager that
                serves Telegram/QQ/... traffic rather than in this
                handler's desktop-local manager.

                Resumes the most recent session when the client did not
                name one, then pumps every stream event through
                ``_dispatch_event`` until the stream closes, the task is
                cancelled, or the backend raises.
                """
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
                        # Resolve the session once and reuse that
                        # reference for every dispatch.  The session
                        # id is stamped onto each outgoing frame so the
                        # renderer can filter events by conversation;
                        # without a concrete id, tokens from two
                        # concurrently running sessions would interleave
                        # in the same chat pane.
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
                            # A mid-run fallback may have changed the active
                            # model; reconcile _default_config and the UI.
                            _sync_model_from_fallback(self, _iclaw_session, ws)
                    except Exception as e:
                        logger.error("[iclaw] setup error: %s", e, exc_info=True)
                        from encre.backends.base import format_backend_error
                        with contextlib.suppress(Exception):
                            await self._send(ws, "finish", reason="error", error=format_backend_error(e), session_id=sid)

            self._iclaw_task = asyncio.create_task(_run_iclaw())
            return

        # Workspace mode: derive a per-run config from the connection
        # default and overlay the project's own ``.encre/config.json``.
        # ``dataclasses.replace`` keeps _default_config itself pristine
        # so a workspace override can never leak into global sessions.
        # A non-existent directory falls back to the plain global config,
        # which is how a removed folder degrades gracefully.
        if self._workspace_path and os.path.isdir(self._workspace_path):
            run_config = replace(self._default_config, workspace=self._workspace_path)
            _apply_workspace_config(run_config, self._workspace_path)
            # The workspace's model pool may be enabled with zero members:
            # nothing is addressable, so refusing here avoids a guaranteed
            # backend error and gives the UI an actionable message.
            if getattr(run_config, "workspace_pool_state", "off") == "empty":
                await self._send(
                    ws, "error",
                    message="该模型池中没有可用的模型，禁止发送",
                    code="empty_model_pool",
                    session_id=msg.session_id or "",
                )
                await self._send(
                    ws, "finish", reason="error",
                    error="empty_model_pool",
                    session_id=msg.session_id or "",
                )
                return
        else:
            run_config = replace(self._default_config, workspace="")
        # An explicit id resumes an existing conversation (loading it
        # from disk if it is not already in memory); an absent id keeps
        # or creates the handler's default session for this socket.
        if msg.session_id:
            session = self._manager.load_or_create_session(
                msg.session_id, config=run_config)
            self._info = session
            self._current_session_id = session.session_id
        else:
            session = self._get_or_create_session()
            # Temp chats are flagged before the run loop starts so any
            # early save or broadcast triggered by the first message is
            # already suppressed by the ephemeral marker.
            if msg.temp_chat:
                session.agent.session.metadata["temp_chat"] = True

        self._manager.touch(session.session_id)

        # Channel tagging is one-way for external traffic: a session
        # that already belongs to an IM adapter (iclaw, telegram, qq,
        # ...) must never be relabelled "normal"/"iwork", or it would
        # leak into the desktop sidebar and be resumable from the wrong
        # surface.  Only untagged or desktop-channel sessions are tagged.
        existing_channel = session.agent.session.metadata.get("channel", "")
        if existing_channel in ("", "normal", "iwork"):
            session.agent.session.metadata["channel"] = (
                "iwork" if self._workspace_path else "normal"
            )

        # Temp chat: never persist, never list in sidebar.  The marker
        # is what every downstream save/list path consults, so setting
        # it here (rather than relying on callers) is the single point
        # that keeps ephemeral conversations out of ``data/sessions/``.
        if msg.temp_chat:
            session.agent.session.metadata["temp_chat"] = True

        # Log backend identity for debugging.  The key is never printed
        # directly -- only an opaque last-4 fragment, so the secret (and
        # even its prefix) stays out of logs.
        _bk = session.agent.loop.backend
        if _bk:
            _ak = getattr(_bk, "api_key", "") or ""
            _ak_tail = ("***" + _ak[-4:]) if len(_ak) > 4 else ("(set)" if _ak else "(unset)")
            logger.info("[run] backend type=%s model=%s api_key=%s",
                        type(_bk).__name__, getattr(_bk, "model", "?"), _ak_tail)

        if session.state != SessionState.IDLE:
            # A force-cancel can leave the flag set after the agent task is
            # already dead (set_session_state used to no-op for parked
            # sessions). Reconcile with the real task before refusing the
            # run, otherwise the session cannot be spoken to until restart.
            task_alive = session.agent_task is not None and not session.agent_task.done()
            if task_alive:
                # Queue behind the busy turn instead of dropping the prompt:
                # the desktop backlog persists it, auto-drains FIFO once the
                # current run finishes, and mirrors every mutation to the UI
                # through the ``queued_submits`` broadcast.  The busy error
                # still goes out so older frontends keep their branch.
                self._ensure_queue_listener()
                entry = {
                    "prompt": msg.prompt,
                    "system_prompt": msg.system_prompt,
                    "mode": msg.mode,
                    "mode_prompt": msg.mode_prompt,
                    "attachments": msg.attachments,
                    "specialty": msg.specialty,
                    "temp_chat": msg.temp_chat,
                    "metadata": {"mode": msg.mode} if msg.mode else {},
                }
                self._submit_backlog.queue(session.session_id, entry, owner=ws)
                await self._send(ws, "error", message="Session already running", code="busy",
                                 session_id=session.session_id)
                return
            self._manager.set_session_state(session.session_id, SessionState.IDLE)

        acquired = await self._manager.acquire_slot()
        if not acquired:
            await self._send(ws, "error",
                message="Server at capacity, try later", code="capacity",
                session_id=session.session_id)
            return

        self._manager.set_session_state(session.session_id, SessionState.RUNNING)
        # Cancellation aborts in-flight API requests by force-closing
        # the backend, which leaves the agent with a dead client.  The
        # rebuild is best-effort: if it fails, the first real request in
        # this run will surface the error instead of failing silently
        # here.
        with contextlib.suppress(Exception):
            session.agent.rebuild_backend()
        prompt = msg.prompt
        system_prompt = msg.system_prompt
        mode_prompt = msg.mode_prompt or ""

        # Strip raw '<mode>...</mode>' markers so they never reach the
        # model as literal user text.  Legacy clients fabricate such a
        # marker when the user submits an empty prompt while a mode is
        # active; the authoritative mode travels in msg.mode /
        # msg.mode_prompt and is injected into the system prompt.  The
        # marker is also mined here to recover the mode name when the
        # structured field is absent.
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

        # Attachments arrive as base64 blobs; how they are surfaced
        # depends on whether the active model accepts image inputs.
        # Exactly ONE user message is staged below (via ``user_content``):
        # the multimodal branch used to add the content array here AND let
        # the single ``add_message("user", prompt)`` further down append an
        # empty second user message -- every image message then shifted the
        # stored user-message ordinals, which made message-level rollbacks
        # and deletes land one turn above the clicked one.
        user_content: str | list[dict[str, Any]] = prompt
        if msg.attachments:
            # The multimodal flag is a two-part check: the model must
            # declare multimodal support AND must not have been probed
            # as explicitly unsupported (see
            # :func:`encre.backends.multimodal.probe_backend_capabilities`).
            active_model = session.agent.config.get_active_model()
            is_multimodal = active_model and (
                active_model.multimodal or False
            ) and getattr(
                active_model, "multimodal_support", "unknown"
            ) != "unsupported"

            if is_multimodal and any(
                a.get("mime_type", "").startswith("image/") for a in msg.attachments
            ):
                # Build an OpenAI-style multimodal content array: one
                # text item followed by one image_url item per image,
                # each embedding the payload as a data: URI so no side
                # channel or network fetch is required by the backend.
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
                user_content = content
                # prompt is cleared because the combined block is what
                # carries the user's words; leaving it would duplicate
                # the text in the transcript.
                prompt = ""
            else:
                attachment_block = _format_attachments(msg.attachments)
                if attachment_block:
                    prompt = attachment_block + "\n\n" + prompt
                    user_content = prompt

        if msg.specialty and msg.specialty != "general":
            session.agent.loop.prompt_builder._specialty = msg.specialty
        # Wire the session's OWN spec engine into the loop so spec mode
        # can parse specifications and enforce its approval gate.  The
        # engine is resolved per session via _spec_engine_for: a
        # connection-wide singleton used to let one conversation's spec
        # state bleed into the next, which corrupted the approval queue.
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

        # Mode transitions.  The frontend echoes the active mode on
        # every ``run`` message, so echo alone cannot distinguish a
        # real switch from a no-op repeat.  The comparison below uses
        # the session's persisted slash_command_mode as the reference:
        #
        # - Explicit mode that differs from the persisted value is a
        #   genuine transition (a /plan or /spec command, or a toolbar
        #   switch) and is routed through _apply_mode.  That single
        #   entry point keeps the config string, the session-metadata
        #   mirror and the derived plan_mode_active flag consistent,
        #   and broadcasts mode_changed so the toolbar chip and exit
        #   button appear.
        # - No explicit mode, or one matching the persisted value,
        #   only writes the value into config for this run.  The
        #   persistent metadata slot is left alone, which prevents a
        #   one-off /plan from re-applying to every later normal
        #   message.
        _es_meta = session.agent.session.metadata
        _persisted = _es_meta.get("slash_command_mode", "") or ""
        if msg.mode and msg.mode != _persisted:
            await self._apply_mode(ws, session, msg.mode)
        else:
            session.agent.config.slash_command_mode = msg.mode or _persisted
        logger.info("[run] slash_command_mode resolved to: '%s' (msg.mode='%s', persisted='%s')",
                    session.agent.config.slash_command_mode, msg.mode, _persisted)

        # Mirror the persisted slash *command* into config so its
        # command_instructions block is re-injected into the prompt.
        # Activation and clearing belong to the dedicated set_command
        # message; the only gap this fills is an in-memory config that
        # has not yet been restored after a session switch, where the
        # metadata holds the truth but config is stale.
        if not getattr(session.agent.config, "active_command", None):
            session.agent.config.active_command = (
                _es_meta.get("active_command") or None
            )

        session.agent.add_message("user", user_content, mode=session.agent.config.slash_command_mode)
        # Echo the stored message id back so the renderer can stamp its
        # optimistic user bubble: without it every message sent this session
        # carries no server id, and the rollback/delete fallback has to trust
        # a frontend-computed ordinal that misresolves across branches.
        _staged_user_id = ""
        if session.agent.session.messages:
            _staged_user_id = session.agent.session.messages[-1].get("id", "") or ""
        await self._send(ws, "run_started",
                         session_id=session.session_id,
                         user_message_id=_staged_user_id)
        # Index and broadcast immediately so the sidebar shows the new
        # entry while the model is still working; the disk write is
        # deferred to a background task so the first token is not
        # delayed by JSON serialization.
        if not session.agent.session.metadata.get("temp_chat"):
            self._manager._index_add(session)
            self._broadcast_sessions()
            # Persist to disk in the background (non-blocking).
            self._manager._schedule_save(session)
        logger.info("[run] session=%s workspace=%s", session.session_id[:8], self._workspace_path or "(none)")

        # Auto-name: fire-and-forget so conversation is not delayed.
        # Only freshly named, first-turn, non-ephemeral sessions qualify
        # -- auto-naming a temp chat would rename its on-disk row and
        # leak it into the sidebar, so the marker is checked here too.
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

        # Wire the engine-install requester's immediate emit hook so
        # the desktop engine dialog appears the moment a browser or
        # desktop action needs one, without waiting for the agent loop
        # to return to its event pump.  Errors inside the hook are
        # logged and swallowed: a failed dialog prompt must not abort
        # the surrounding agent turn.
        async def _emit_engine(evt: Any, session=session) -> None:
            """Relay one engine-originated event to the client.

            Failures are logged and swallowed: a failed dialog prompt must
            not abort the surrounding agent turn.

            Args:
                evt: The engine event to dispatch.
                session: The session the event belongs to, pinned by
                    keyword default so the closure cannot pick up a stale
                    reference.
            """
            try:
                await self._dispatch_event(ws, session, evt)
            except Exception as exc:
                logger.warning("engine emit failed: %s", exc)
        try:
            session.agent.set_engine_emit(_emit_engine)
        except Exception:
            logger.debug("agent has no set_engine_emit", exc_info=True)

        # Snapshot the active_command before the task is created so
        # the _run_agent closure captures the value by copy rather than
        # by reference.  The frontend's set_command clear message can
        # land in this same event loop right after the run message, and
        # without the snapshot the run would start with a cleared
        # command and drop the user's custom instructions.
        _active_command = getattr(session.agent.config, "active_command", None)

        async def _run_agent(*, session=session, prompt=prompt, system_prompt=system_prompt, mode_prompt=mode_prompt, _saved_command=_active_command):
            """Consume one agent run and relay its events to the client.

            Spawns as a tracked task from :meth:`_h_run`.  Cancellation
            is cooperative: the loop's cancel flag is set from the
            socket-side cancel handler, and ``asyncio.CancelledError``
            here simply reports the stop.  State release is guarded on
            ``session.agent_task is asyncio.current_task()`` because a
            newer run may already own the session.
            """
            _run_completed = False
            try:
                # The write gate is derived from the standing goal, not from
                # the run: while an approved plan/spec still has items
                # outstanding, every run keeps writing without a per-write
                # proposal.  Only a goal-less run re-arms the gate, so a run
                # the user stopped mid-flight cannot leave writes ungated for
                # the next one.
                _loop0 = getattr(session.agent, "loop", None)
                if _loop0 is not None:
                    with contextlib.suppress(Exception):
                        _pm0 = getattr(_loop0, "_plan_mode", None)
                        _loop0.plan_gate_released = bool(
                            _pm0 is not None and _pm0.goal_implementing
                        )
                # Restore active_command if a concurrent set_command clear
                # message wiped it between handler entry and task start.
                if _saved_command and not getattr(session.agent.config, "active_command", None):
                    session.agent.config.active_command = _saved_command
                async for event in _coalesce_stream_deltas(session.agent.run(
                    prompt=prompt, system_prompt=system_prompt,
                    custom_instructions=mode_prompt)):
                    await self._dispatch_event(ws, session, event)
                    # Boundary events mark a completed assistant turn.  They
                    # are the natural point for a checkpoint save and for
                    # refreshing the canvas panels without adding latency to
                    # token streaming itself.
                    if isinstance(event, ToolResult | AssistantBoundary):
                        # Recompute the live context window so the canvas
                        # progress bar reflects compactions and pruning as
                        # they happen, not just the initial estimate.
                        ctx_msgs = session.agent.session.get_context_messages()
                        ctx_tokens = count_message_tokens(ctx_msgs)
                        window = session.agent.loop.backend.context_window_size() if session.agent.loop.backend else 0
                        await self._send(ws, "context_usage",
                            context_tokens=ctx_tokens,
                            context_window=window,
                            session_id=session.session_id)
                        # Stream the telemetry summary at each boundary so
                        # the canvas panels for compactions and tool calls
                        # update while the turn is still going.
                        if session.agent.telemetry.enabled:
                            with contextlib.suppress(Exception):
                                await self._send(ws, "telemetry",
                                    data=session.agent.telemetry.get_summary(),
                                    session_id=session.session_id)
                        if not session.agent.session.metadata.get("temp_chat"):
                            # A checkpoint rewrite costs O(history) and used to
                            # block the token stream at every boundary. The
                            # debounced save coalesces boundaries and the run's
                            # final save still runs synchronously below.
                            with contextlib.suppress(Exception):
                                self._manager._schedule_save(session)
                # Reached only when the stream ended on its own -- the task the
                # user approved is done. Cancel and error paths skip this and
                # leave plan mode in place.
                _run_completed = True
            except asyncio.CancelledError:
                await self._send(ws, "finish", reason="cancelled", session_id=session.session_id)
            except Exception as e:
                # A rollback (or a stop) cancels the loop and closes the
                # backend mid-stream, so the in-flight reader raises a
                # connection error that is not a CancelledError. The loop
                # records the cancel in its own flag; when it is set this run
                # was stopped on purpose, so report it as a cancel. Sending
                # an error frame here made the Notifications panel light up
                # with a message about the turn the user just rolled back.
                _loopc = getattr(session.agent, "loop", None)
                _was_cancelled = _loopc is not None and bool(
                    getattr(_loopc, "_cancelled", lambda: False)()
                )
                if _was_cancelled:
                    logger.debug("run ended during a cancel, suppressing error frame", exc_info=True)
                    with contextlib.suppress(Exception):
                        await self._send(ws, "finish", reason="cancelled", session_id=session.session_id)
                else:
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
                # No command is cleared here.  Sticky commands installed
                # through set_command are meant to survive across runs and
                # are only removed by an explicit empty set_command; a
                # one-shot mode_prompt never touches active_command either,
                # so there is nothing for teardown to release.
                pass
                # Plan mode outlives the approval and the run: the mode is
                # dropped when the standing goal closes (every checklist item
                # done) or when the user exits it.  A run that ended with the
                # goal still outstanding keeps the mode on -- the point of the
                # goal is that the next run continues it.  An approved review
                # whose artifacts declared no checklist has no goal to drive,
                # so it falls back to dropping the mode with its run.
                if _run_completed:
                    _loop = getattr(session.agent, "loop", None)
                    _pm = getattr(_loop, "_plan_mode", None) if _loop is not None else None
                    if _pm is not None and getattr(_pm, "goal_completed", False):
                        # The goal closed during this run: the loop already
                        # left the mode, so the client has to be told to drop
                        # its indicator (and so a reopen does not restore it).
                        _pm.goal_completed = False
                        with contextlib.suppress(Exception):
                            await self._send(ws, "mode_changed", mode="",
                                             session_id=session.session_id)
                        with contextlib.suppress(Exception):
                            await self._send(ws, "plan_mode_changed", active=False,
                                             session_id=session.session_id)
                    elif _pm is not None and _pm.goal_implementing:
                        # A standing goal outlives the run: keep the mode and
                        # the released write gate so the next message carries
                        # on with the items that are still outstanding.
                        pass
                    elif _loop is not None and getattr(_loop, "plan_gate_released", False):
                        # No goal to drive (an approved review whose artifacts
                        # declared no checklist): the historical one-pass
                        # behaviour, the mode is dropped with its run.
                        if getattr(_loop, "plan_mode_active", False):
                            _loop.set_mode("")
                            with contextlib.suppress(Exception):
                                await self._send(ws, "mode_changed", mode="",
                                                 session_id=session.session_id)
                        with contextlib.suppress(Exception):
                            _loop.plan_gate_released = False
                # Send a final telemetry snapshot so the panels show the
                # settled totals even when the loop exited early.
                if session.agent.telemetry.enabled:
                    with contextlib.suppress(Exception):
                        summary = session.agent.telemetry.get_summary()
                        await self._send(ws, "telemetry", data=summary, session_id=session.session_id)
                # Propagate any fallback that happened during this run so
                # the model selector and subsequent sessions agree.
                _sync_model_from_fallback(self, session, ws)
                # Push the file-backed planes that have no incremental
                # broadcast of their own -- they are only sent on request
                # or inside the full snapshot.  Sending them here means
                # the memory and rules panels reflect anything the agent
                # wrote during this turn without a manual refresh.  Each
                # send is guarded individually so one failure cannot break
                # the others or the teardown path.  The memory graph and
                # profile are pushed alongside the list here: the list
                # alone leaves the relationship view and the identity
                # card frozen at whatever they looked like when the
                # connection opened, since neither has its own frame.
                with contextlib.suppress(Exception):
                    await self._send(ws, "memory_list", entries=self._build_memory_list())
                with contextlib.suppress(Exception):
                    await self._send(ws, "memory_graph", graph=self._build_memory_graph())
                with contextlib.suppress(Exception):
                    await self._send(ws, "profile_data", profile=self._build_profile_data())
                with contextlib.suppress(Exception):
                    await self._send(ws, "global_rules_list", rules=self._build_global_rules_list())
                # Release ownership only when this task is still the
                # current owner.  A newer run may have already taken over
                # the same session, and an unconditional release would
                # clear the new run's RUNNING state and free a semaphore
                # slot that it still holds.
                if session.agent_task is asyncio.current_task():
                    self._manager.set_session_state(session.session_id, SessionState.IDLE)
                    self._manager.release_slot()
                    if not session.agent.session.metadata.get("temp_chat"):
                        await self._manager._cancel_pending_save(session.session_id)
                        await self._manager._save_session_async(session)
                    self._manager.notify_session_completed()
                    session.agent_task = None
                    # The conversation is idle again: FIFO-drain the next
                    # queued prompt after a short grace window, so the UI
                    # gets a chance to edit the queue (remove, reorder, or
                    # pop the head over) before the next turn streams.
                    with contextlib.suppress(Exception):
                        self._submit_backlog.schedule_drain(session.session_id, grace=1.5)

        # Ensure any previous agent task has fully terminated before
        # starting this one.  Two ``_run_impl`` generators on the same
        # EncreLoop instance would corrupt shared state (messages, the
        # cancel event, caches).  The 0.5 s grace window bounds the wait
        # so a wedged task cannot block the handler indefinitely.
        if session.agent_task and not session.agent_task.done():
            session.agent.loop.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(session.agent_task, timeout=0.5)
        session.agent_task = asyncio.create_task(_run_agent())

    async def _h_queue_list(self, ws: Any, msg: ClientQueueList) -> None:
        """Return one conversation's queued prompts to the requesting socket.

        Args:
            ws: The requesting WebSocket.
            msg: The list request carrying the session id.
        """
        self._ensure_queue_listener()
        backlog = self._queue_backlog_for(msg.session_id)
        items: list[dict[str, Any]] = []
        if backlog is not None:
            items = [
                {k: v for k, v in e.items() if not k.startswith("_")}
                for e in backlog.entries(msg.session_id)
            ]
        await self._send(ws, "queued_submits", session_id=msg.session_id, items=items)

    async def _h_queue_pop(self, ws: Any, msg: ClientQueuePop) -> None:
        """Take the head of one conversation's queue and run it immediately.

        Used by the queue panel's "send now" action: the head entry is
        popped and executed through the backlog's own runner (which handles
        the broadcast stand-in and the iclaw channel distinction), so the
        panel does not have to fake a run locally.

        Args:
            ws: The requesting WebSocket.
            msg: The pop request carrying the session id.
        """
        self._ensure_queue_listener()
        backlog = self._queue_backlog_for(msg.session_id)
        entry = backlog.pop(msg.session_id) if backlog is not None else None
        if entry is None:
            await self._send(ws, "queue_pop_result", session_id=msg.session_id, item=None)
            return
        task = backlog.run_now(msg.session_id, entry)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await self._send(ws, "queue_pop_result", session_id=msg.session_id,
                         item={k: v for k, v in entry.items() if not k.startswith("_")})

    async def _h_queue_remove(self, ws: Any, msg: ClientQueueRemove) -> None:
        """Delete one queued prompt by index and rebroadcast the queue.

        Args:
            ws: The requesting WebSocket.
            msg: The remove request carrying session id and index.
        """
        self._ensure_queue_listener()
        backlog = self._queue_backlog_for(msg.session_id)
        if backlog is not None:
            backlog.remove_at(msg.session_id, msg.index)
        items: list[dict[str, Any]] = []
        if backlog is not None:
            items = [
                {k: v for k, v in e.items() if not k.startswith("_")}
                for e in backlog.entries(msg.session_id)
            ]
        await self._send(ws, "queued_submits", session_id=msg.session_id, items=items)

    async def _h_queue_move(self, ws: Any, msg: ClientQueueMove) -> None:
        """Reorder one queued prompt (drag-and-drop) and rebroadcast.

        Args:
            ws: The requesting WebSocket.
            msg: The move request carrying session id and both indices.
        """
        self._ensure_queue_listener()
        backlog = self._queue_backlog_for(msg.session_id)
        if backlog is not None:
            backlog.move(msg.session_id, msg.from_index, msg.to_index)
        items: list[dict[str, Any]] = []
        if backlog is not None:
            items = [
                {k: v for k, v in e.items() if not k.startswith("_")}
                for e in backlog.entries(msg.session_id)
            ]
        await self._send(ws, "queued_submits", session_id=msg.session_id, items=items)

    async def _h_queue_steer(self, ws: Any, msg: ClientQueueSteer) -> None:
        """Pop one queued prompt and inject it as a mid-run steer.

        Unlike :meth:`_h_queue_pop` this does NOT start a new turn; the
        prompt is pushed into the running loop's steer queue so the next
        model call picks it up as additional context.  The entry is
        consumed either way, so the panel reflects the pop immediately.

        Args:
            ws: The requesting WebSocket.
            msg: The steer request carrying session id and index.
        """
        self._ensure_queue_listener()
        backlog = self._queue_backlog_for(msg.session_id)
        entry: dict[str, Any] | None = None
        if backlog is not None:
            entries = backlog.entries(msg.session_id)
            if 0 <= msg.index < len(entries):
                entry = dict(entries[msg.index])
                backlog.remove_at(msg.session_id, msg.index)
        if entry is not None:
            prompt = (entry.get("prompt") or "").strip()
            if prompt:
                session = self._manager.get_session(msg.session_id)
                loop = session.agent.loop if (session and session.agent) else None
                if loop is not None and getattr(loop, "_steer_queue", None) is not None:
                    loop._steer_queue.push(prompt)
                    logger.info(
                        "[queue:steer] injected queued prompt into session=%s (%d chars)",
                        msg.session_id, len(prompt),
                    )
                    await self._send(ws, "steer_queued", session_id=msg.session_id)
                else:
                    # No live loop to absorb the prompt: report it so the
                    # client can surface the fact, but keep the queue in
                    # sync with what the panel just saw (the entry is gone).
                    await self._send(ws, "steer_queued",
                                     session_id=msg.session_id,
                                     error="no_active_session")
        items = [
            {k: v for k, v in e.items() if not k.startswith("_")}
            for e in backlog.entries(msg.session_id)
        ] if backlog is not None else []
        await self._send(ws, "queued_submits", session_id=msg.session_id, items=items)

    async def _h_resume(self, ws: Any, msg: ClientResume) -> None:
        """Replay a session's transcript back to the client on reconnect.

        Does not run any agent code: it only rebuilds the view model
        (messages, branches, plan items, artifacts, references) and
        re-emits the persisted mode and command so the toolbar matches
        the loaded conversation.

        Args:
            ws: The WebSocket that requested the resume.
            msg: The :class:`ClientResume` request; ``session_id`` may
                be empty, in which case the handler's default session
                is used.

        Raises:
            Nothing -- config failures fall back to the global config
            rather than surfacing an error, so resume is best-effort.
        """
        # Workspace config if currently in workspace mode; an empty
        # model pool refuses resume just like it refuses a fresh run.
        if self._workspace_path and os.path.isdir(self._workspace_path):
            resume_config = replace(self._default_config, workspace=self._workspace_path)
            _apply_workspace_config(resume_config, self._workspace_path)
            if getattr(resume_config, "workspace_pool_state", "off") == "empty":
                await self._send(
                    ws, "error",
                    message="该模型池中没有可用的模型，禁止继续",
                    code="empty_model_pool",
                    session_id=msg.session_id or "",
                )
                await self._send(
                    ws, "finish", reason="error",
                    error="empty_model_pool",
                    session_id=msg.session_id or "",
                )
                return
        else:
            resume_config = replace(self._default_config, workspace="")
        if msg.session_id:
            # Loading a session from disk (read + AES-decrypt the transcript)
            # is pure blocking I/O -- a long session can take hundreds of
            # milliseconds and, run inline, would stall every other
            # connection's frames for that whole time.
            session = await asyncio.to_thread(
                self._manager.load_or_create_session,
                msg.session_id, config=resume_config)
        else:
            session = self._get_or_create_session()
        # Handler state lives in per-connection ContextVar-backed properties:
        # assignments MUST stay on the event loop (a worker thread mutates a
        # copy of the context, so writes there would be silently lost).
        self._info = session
        self._current_session_id = session.session_id
        # Tag the resumed session with the channel of the current mode,
        # but never overwrite an external channel (iclaw, telegram,
        # qq, ...).  Those sessions belong to their platform adapter and
        # must not leak into the normal-mode sidebar through a channel
        # re-tag; resuming them from global search is read-only.
        sess = session.agent.session
        existing_channel = sess.metadata.get("channel", "")
        if existing_channel in ("", "normal", "iwork"):
            sess.metadata["channel"] = (
                "iwork" if self._workspace_path else "normal"
            )
        # Reconcile state from the actual task state -- a live task
        # means the session is running even if its ``finally`` block has
        # not run yet, and the state column would otherwise read STALE
        # "idle" for a session that is mid-turn.  A session parked on a
        # permission prompt is also alive but must keep its
        # AWAITING_APPROVAL state, otherwise switching back to it would
        # render "running" and drop the consent indicator.
        if (
            session.agent_task is not None
            and not session.agent_task.done()
            and session.state != SessionState.AWAITING_APPROVAL
        ):
            self._manager.set_session_state(session.session_id, SessionState.RUNNING)
        sess.mark_messages_dirty()

        def _project_transcript() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            """Project the persisted transcript for the renderer.

            Artifact reconciliation walks every message and the renderer
            projection re-shapes all of them -- CPU-bound work over
            potentially thousands of entries that must not occupy the
            event loop while a session is being opened.

            Returns:
                ``(msgs, branches_list)`` for the ``session_ready`` frame.
            """
            sess.ensure_artifacts_from_messages()
            msgs = self._renderer_session_messages(sess)
            branches_list = [b.__dict__ for b in sess.branches.values()]
            return msgs, branches_list

        msgs, branches_list = await asyncio.to_thread(_project_transcript)
        # Tail-first open: the ``session_ready`` frame carries only the most
        # recent turns, so transfer + JSON parse + the renderer transform all
        # scale with the tail instead of the whole transcript and a long
        # session paints immediately. The older range follows right behind in
        # a background ``session_history_backfill`` frame. The cut point walks
        # back off any leading tool result so the tail never orphans a tool
        # call that lives in the backfill chunk (the renderer pairs results
        # with calls inside one list scan only).
        cut = max(0, len(msgs) - _HISTORY_TAIL_SIZE)
        while cut > 0 and msgs[cut].get("role") == "tool":
            cut -= 1
        tail_msgs = msgs[cut:]
        earlier_msgs = msgs[:cut]
        # The renderer derives per-user-message storage keys from the absolute
        # user-message ordinal; the tail must resume counting where the
        # backfill chunk leaves off.
        earlier_user_count = sum(1 for m in earlier_msgs if m.get("role") == "user")
        state_value = self._manager.get_session_state(session.session_id)
        # Replay a review card ONLY while the live loop is actually blocked on
        # it. A record still sitting at "review" after its run was cancelled,
        # killed, or timed out is history -- re-sending it made a card appear
        # at the top of a brand-new turn whose model had not written (or
        # submitted) anything, and clicking it woke nobody. Retire it here so
        # it cannot resurface on the next open either.
        _reviews = sess.metadata.get("reviews") or []
        _last_review = _reviews[-1] if _reviews else None
        _loop = getattr(getattr(session, "agent", None), "loop", None)
        _live = _loop.live_pending_review() if _loop is not None else None
        if (
            _live is not None and _last_review is not None
            and _live.get("review_id") == _last_review.get("review_id")
        ):
            _last_review = _live
        else:
            if _last_review is not None and _last_review.get("status") in ("review", "draft"):
                try:
                    _loop._cancel_pending_review()
                except Exception:
                    logger.debug("[review] stale pending retire failed", exc_info=True)
            _last_review = None
        await self._send(ws, "session_ready", session_id=session.session_id, messages=tail_msgs,
                         plan_items=sess.plan_items, artifacts=sess.artifacts, references=sess.references,
                         branches=branches_list, active_branch_id=sess.active_branch_id,
                         state=state_value, request_id=msg.request_id,
                         plan_review=_last_review,
                         active_model_index=getattr(session.agent.config, "active_model_index", 0),
                         channel=session.agent.session.metadata.get("channel", "normal"),
                         has_earlier_history=bool(earlier_msgs),
                         earlier_user_count=earlier_user_count,
                         # The per-session files dir lets the renderer key the
                         # editor tree root by session without re-pulling the
                         # whole config snapshot after every session switch.
                         # str(): get_session_files_dir returns a WindowsPath,
                         # which json.dumps cannot serialize.
                         session_files_dir=str(get_session_files_dir(session.session_id)))
        # Older history follows immediately after the tail frame, through THE
        # generic sharded-topic channel (data_begin/data_shard/data_end) that
        # every bulk backend payload shares. Fire-and-forget: the session is
        # already usable, and a failed backfill never breaks the open.
        if earlier_msgs:
            asyncio.create_task(self._send_topic_shards(
                ws, f"session:{session.session_id}:history-backfill",
                {"request_id": msg.request_id or "", "total": len(earlier_msgs)},
                earlier_msgs))
        await self._send_session_mode(ws, session)
        await self._send_session_command(ws, session)
        # Open with a fresh context-usage push (falls back to the loop-cached
        # breakdown from the last prompt phase) so the sidebar gauge and the
        # context-usage detail page are populated the moment a session opens,
        # not only after the next turn finishes. The composed WebSocket
        # handler always mixes in EventDispatchMixin.
        await self.push_context_usage(ws, session, None)

    async def _h_retry(self, ws: Any, msg: ClientRetry) -> None:
        """Branch at a previous user message and re-run from there.

        Resolution order for the target session is ``msg.session_id``
        then the handler's current session.  The session is loaded from
        disk if it is not resident, and the branch operation happens on
        the underlying :class:`EncreSession`, which owns the branching
        structure.  A non-empty ``user_msg`` result means the branch
        was created and a retry run is spawned; an empty result means
        the index pointed at a message that cannot be replayed.

        Args:
            ws: The WebSocket that requested the retry.
            msg: The :class:`ClientRetry` request, carrying
                ``user_message_index`` and an optional ``mode``
                (``detailed`` / ``concise``) that rewrites the prompt.
        """
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
        # pass it as context for detailed/concise retry.  The lookup
        # counts user messages in the active branch and takes the first
        # assistant message after the selected index; multimodal
        # assistant content is flattened to its text parts so the
        # prompt stays single-string.
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
        # Retry invalidates the compact summary: the conversation now
        # diverges at this point, so the old summary -- which described
        # the previous branch's "future" -- would be misleading context.
        # Clearing it forces the next compact to regenerate from the
        # new branch's actual state.
        sess.metadata.pop("user_requirements_summary", None)
        # Notify the frontend about the new branch so the branch
        # switcher and the message list both reflect the fresh head.
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
                """Stream one retry prompt to the client and persist on boundaries.

                Saves the session after each ``ToolResult`` and
                ``AssistantBoundary`` so a crash mid-turn loses at most one
                assistant message.  Cancellation is translated into a
                ``finish`` frame with ``reason="cancelled"`` rather than
                propagating, and any other failure yields both an ``error``
                frame and a terminal ``finish`` so the client never hangs
                waiting for a run that ended.

                Args:
                    session_id: Session to report on in the frames.
                    prompt: The prompt text to run.
                    info: The resolved session info, pinned by keyword
                        default to avoid late binding.
                """
                try:
                    async for event in _coalesce_stream_deltas(info.agent.run(prompt=prompt)):
                        await self._dispatch_event(ws, info, event)
                        if isinstance(event, ToolResult | AssistantBoundary):
                            with contextlib.suppress(Exception):
                                self._manager._schedule_save(info)
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
                        await self._manager._cancel_pending_save(session_id)
                        await self._manager._save_session_async(info)
                        # Sync model changes from fallback to _default_config and notify frontend
                        _sync_model_from_fallback(self, info, ws)
                        # Mirror _h_run: refresh the file-backed memory and
                        # rules planes so the settings panels reflect
                        # anything the agent wrote during this retry.
                        # memory_graph and profile_data are pushed too:
                        # neither has an incremental broadcast, so a retry
                        # that only pushes the list would leave the graph
                        # and the identity card on their pre-retry state.
                        with contextlib.suppress(Exception):
                            await self._send(ws, "memory_list", entries=self._build_memory_list())
                        with contextlib.suppress(Exception):
                            await self._send(ws, "memory_graph", graph=self._build_memory_graph())
                        with contextlib.suppress(Exception):
                            await self._send(ws, "profile_data", profile=self._build_profile_data())
                        with contextlib.suppress(Exception):
                            await self._send(ws, "global_rules_list", rules=self._build_global_rules_list())
                        info.agent_task = None

            # Ensure the previous run has actually stopped before
            # starting the retry; two runs on one EncreLoop would share
            # mutable state and corrupt each other.
            if info.agent_task and not info.agent_task.done():
                info.agent.loop.cancel()
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(info.agent_task, timeout=0.5)
            info.agent_task = asyncio.create_task(_run_retry(sid, user_msg))
        else:
            await self._send(ws, "error", message="Original user message not found", code="retry_error",
                             session_id=sid)

    def _load_sub_agent_messages(self, session_id: str) -> list[dict[str, Any]] | None:
        """Load the flat message list of a persisted sub-agent session.

        Sub-agent sessions created by
        :meth:`encre.loop.EncreLoop._run_sub_agent` are written as a
        directory of turn files under ``<data_dir>/sub_agents/<sid>/``,
        separate from the workspace and global session trees.  This
        reader reconstructs that transcript for the automation history
        view.

        Args:
            session_id: The sub-agent session id, usually the parent
                session id reused by the child task.

        Returns:
            A list of message dicts in file order, or ``None`` when the
            id is empty, the directory does not exist, or the load fails.
            Callers treat ``None`` as "no history" rather than an error.
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
