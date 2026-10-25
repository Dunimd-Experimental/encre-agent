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

import asyncio
import contextlib
import json
import re
import time
import uuid
from typing import Any

from encre.logging_config import get_logger
from encre.prompts.loader import PromptLoader
from encre.utils.types import (
    Finish,
    Reference,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolProgress,
    ToolResult,
)

logger = get_logger(__name__)

# Module-level loader for the sub-agent enforcement block.  Loaded once and
# cached so we don't re-read the .prompt file on every sub-agent spawn.
_enforcement_loader = PromptLoader()
_sub_agent_enforcement: str | None = None


def _get_sub_agent_enforcement() -> str:
    """Return the cached sub-agent enforcement prompt.

    The enforcement block is prepended to every sub-agent's system_prompt so
    that all sub-agents — regardless of their role .prompt file — receive:
    - English-only enforcement (thinking + output + tool calls)
    - Delivery discipline (act-don't-describe, no stub, no fabrication, verify)
    - Safety rules (tool output is data, never expose secrets)
    - Sub-agent constraints (no nesting, limited tools, clear result)

    Sub-agents do NOT go through the full EncrePromptBuilder, so without
    this injection they would miss the universal discipline blocks that the
    main agent gets.
    """
    global _sub_agent_enforcement
    if _sub_agent_enforcement is None:
        _sub_agent_enforcement = _enforcement_loader.load("sub_agent_enforcement")
    return _sub_agent_enforcement


# ── Structured handoff extraction ─────────────────────────────────
# Sub-agents are asked (via the enforcement prompt) to close their final
# reply with a ``<handoff>{json}</handoff>`` block.  The runtime parses it,
# strips it from the content the parent model sees, and returns it under
# the ``handoff`` key of the result dict so the parent loop can consume
# ``summary`` / ``artifacts`` / ``next_actions`` programmatically.

_HANDOFF_RE = re.compile(r"<handoff>\s*(\{.*?\})\s*</handoff>", re.DOTALL)


def _parse_handoff(final_text: str) -> tuple[dict[str, Any], str]:
    """Extract the trailing ``<handoff>`` block from a sub-agent reply.

    Args:
        final_text: The final assistant text of the sub-agent.

    Returns:
        ``(handoff, cleaned_text)`` where ``handoff`` always contains the
        keys ``summary`` / ``artifacts`` / ``next_actions`` (falling back to
        sane defaults when the block is absent or malformed) and
        ``cleaned_text`` is the reply with the raw block removed.
    """
    handoff: dict[str, Any] = {"summary": "", "artifacts": [], "next_actions": []}
    cleaned = final_text
    match = None
    for match in _HANDOFF_RE.finditer(final_text):
        pass  # keep the LAST block if the model emitted several
    if match is not None:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                summary = parsed.get("summary")
                artifacts = parsed.get("artifacts")
                next_actions = parsed.get("next_actions")
                if isinstance(summary, str):
                    handoff["summary"] = summary.strip()
                if isinstance(artifacts, list):
                    handoff["artifacts"] = [str(a) for a in artifacts if str(a).strip()]
                if isinstance(next_actions, list):
                    handoff["next_actions"] = [str(a) for a in next_actions if str(a).strip()]
        except (json.JSONDecodeError, ValueError):
            logger.warning("[sub_agent] malformed handoff block ignored")
        # Strip the raw block (and the surrounding tag lines) from the text.
        cleaned = _HANDOFF_RE.sub("", final_text).rstrip()
    if not handoff["summary"]:
        # Fallback: first non-empty line of the prose as the relay summary.
        for line in cleaned.splitlines():
            line = line.strip()
            if line:
                handoff["summary"] = line[:300]
                break
    return handoff, cleaned


def apply_sub_agent_handoff(
    result: str,
    handoff: Any,
    sub_agent_session_id: str | None,
    loop: Any,
) -> str:
    """Consume a sub-agent's structured handoff (single shared consumer).

    Surfaces ``next_actions`` to the parent model immediately by suffixing
    them to the tool result, and persists them in the parent working set so
    they survive across turns.

    Args:
        result: The raw sub-agent tool result string.
        handoff: The ``handoff`` dict from the sub-agent result (or None).
        sub_agent_session_id: Session id of the finished sub-agent.
        loop: The parent EncreLoop (used to reach ``_working_set``).

    Returns:
        The (possibly suffixed) result string.
    """
    if not isinstance(handoff, dict):
        return result
    next_actions = [str(a) for a in (handoff.get("next_actions") or []) if str(a).strip()]
    if not next_actions:
        return result
    result = result + "\n\nSub-agent follow-ups (pending):\n" + "\n".join(
        f"- {a}" for a in next_actions[:5]
    )
    working_set = getattr(loop, "_working_set", None)
    if working_set is not None and hasattr(working_set, "record_sub_agent_handoff"):
        with contextlib.suppress(Exception):
            working_set.record_sub_agent_handoff(sub_agent_session_id or "", handoff)
    return result


class SubAgentRunner:
    """Runs a sub-agent as a fully isolated session.

    Delegated from EncreLoop._run_sub_agent.  All callers that previously
    accessed ``loop._run_sub_agent(...)`` continue to work through a
    forwarding bridge on EncreLoop.
    """

    def __init__(
        self,
        config: Any,
        tool_registry: Any,
        memory_system: Any,
        profile_system: Any,
        soul_system: Any,
        skill_registry: Any,
        hook_system: Any,
        safety: Any,
        sub_agent_depth: int,
        child_loops: set[Any],
        session: Any,
        mode: Any = None,
    ) -> None:
        self._config = config
        self._tool_registry = tool_registry
        self._memory_system = memory_system
        self._profile_system = profile_system
        self._soul_system = soul_system
        self._skill_registry = skill_registry
        self._hook_system = hook_system
        self._safety = safety
        self._sub_agent_depth = sub_agent_depth
        self._child_loops = child_loops
        self._session = session
        self._mode = mode

    async def run(
        self,
        prompt: str,
        system_prompt: str = "",
        max_turns: int = 0,
        model: str = "",
        api_key: str = "",
        base_url: str = "",
        tool_policy: str = "all",
        progress_callback: Any = None,
        event_callback: Any = None,
        session_id: str | None = None,
        cache_context: Any = None,
        permission_mode: str = "",
        model_index: int | None = None,
    ) -> dict[str, Any]:
        """Run a sub-agent as a fully isolated session.

        The sub-agent is ALWAYS a full EncreAgent spawned from this runner.
        The caller can observe execution through two hooks:

        * ``progress_callback(messages_snapshot)`` is awaited on every
          streaming event with the canonical session messages plus any
          uncommitted draft. Used by the chat UI to render live tokens.
        * ``event_callback(event)`` is awaited on every raw AgentEvent
          before it is folded into the draft.

        Returns the standard sub-agent result dict:

            ``{"content": str, "messages": list[dict], "session_id": str}``
        """
        # Plugin gate (S6): the pre_sub_agent hook may veto the spawn
        # outright or rewrite the delegated prompt before the sub-agent
        # session is built.  This is the sub-agent counterpart of the
        # pre_bash / pre_tool_exec gates -- plugins hold real engineering
        # authority over delegation, not just notification.
        if self._hook_system is not None:
            try:
                _tool_names = [
                    getattr(tool, "name", "")
                    for tool in self._tool_registry.list_tools().values()
                ]
            except Exception:
                _tool_names = []
            _pre_sub = await self._hook_system.emit_pre_sub_agent(prompt, _tool_names)
            if _pre_sub and _pre_sub.get("block"):
                _block_reason = _pre_sub.get("block_reason") or "Blocked by hook: pre_sub_agent"
                logger.info("[sub_agent] spawn vetoed by pre_sub_agent hook: %s", _block_reason)
                return {"content": _block_reason, "messages": [], "session_id": None}
            _sub_mi = (_pre_sub or {}).get("modified_input") or {}
            if isinstance(_sub_mi.get("prompt"), str) and _sub_mi["prompt"]:
                prompt = _sub_mi["prompt"]

        if prompt is None:
            prompt = ""
        if system_prompt is None:
            system_prompt = ""

        # Prepend the sub-agent enforcement block so every sub-agent gets
        # English enforcement + delivery discipline + safety, even though
        # sub-agents don't go through the full EncrePromptBuilder.
        _enforcement = _get_sub_agent_enforcement()
        if _enforcement:
            system_prompt = f"{_enforcement}\n\n{system_prompt}" if system_prompt else _enforcement

        logger.info("[sub_agent] run | prompt_len=%s | sys_prompt_len=%s | tool_policy=%s",
                    len(prompt), len(system_prompt), tool_policy)
        logger.info("[sub_agent] prompt_text=%.300s", prompt)

        from encre.agent import EncreAgent
        from encre.config import EncreConfig
        from encre.tools.runtime import MAX_SUB_AGENT_DEPTH
        from encre.tools.runtime import (
            _enforce_tool_policy as _agent_enforce_policy,
        )
        from encre.tools.registry import ToolRegistry

        # Inherit the parent's permission mode unless the caller pinned an
        # explicit one (built-in roles ship ``"bypass"``; named configs may
        # override).  Hardcoding "bypass" here let sub-agents skip the
        # permission engine even when the user configured a stricter mode
        # for the session.
        sub_config = EncreConfig(
            model=model or self._config.model,
            api_key=api_key or self._config.api_key,
            base_url=base_url or self._config.base_url,
            max_tokens=self._config.max_tokens,
            max_turns=max_turns,
            permission_mode=permission_mode or self._config.permission_mode,
            backend_type=self._config.backend_type,
            backend_kwargs=self._config.backend_kwargs,
        )
        # Inherit the parent's model registry: the loop wiring resolves the
        # context window / sampling / headers from config.get_active_model().
        # Without providers + active_model_index the sub-agent fell back to
        # the backend's auto-detected window, so a 59k-token tool/prompt
        # overhead blew past the pressure threshold and EVERY turn paid an
        # 11-20s pre-API compaction the parent never ran.
        sub_config.providers = self._config.providers
        sub_config.active_model_index = self._config.active_model_index
        # A pinned ``model_index`` (named sub-agent config) overrides the
        # inherited active model; ``None`` keeps following the parent.
        if model_index is not None and 0 <= model_index < len(sub_config.providers):
            sub_config.active_model_index = model_index
        tool_registry = ToolRegistry()
        for _tool in self._tool_registry.list_tools().values():
            tool_registry.register(_tool)

        sub_agent = EncreAgent(
            config=sub_config,
            tool_registry=tool_registry,
            memory_system=self._memory_system,
            profile_system=self._profile_system,
            soul_system=self._soul_system,
            skill_registry=self._skill_registry,
            hook_system=self._hook_system,
            safety=self._safety,
            mode=self._mode,
            # Activate plugins off the event loop (ensure_plugins_loaded at
            # the start of run()) instead of synchronously in the constructor
            # -- a ~250-plugin activate_all on the loop freezes the PARENT's
            # streaming UI for the whole duration of every delegation.
            defer_plugins=True,
        )
        # The sub-agent inherits the parent's fully-populated tool registry
        # (MCP tools already registered there), so a fresh ``_init_mcp``
        # would re-spawn every configured stdio server and re-handshake
        # every HTTP endpoint per delegation -- seconds of dead time before
        # the first token for tools that are already present.
        sub_agent._mcp_initialized = True
        sub_agent.loop.sub_agent_depth = self._sub_agent_depth + 1
        if self._sub_agent_depth >= MAX_SUB_AGENT_DEPTH and tool_registry.get("agent") is not None:
            tool_registry.unregister("agent")
            logger.info("[sub_agent] depth=%s reached MAX=%s, removed 'agent' tool from sub-agent registry",
                        self._sub_agent_depth + 1, MAX_SUB_AGENT_DEPTH)
        sub_agent.config.current_tool_policy = tool_policy

        if tool_policy in ("readonly", "no_writes"):
            def _policy_hook(tool_name: str, _tool_input: dict[str, Any]) -> dict[str, Any] | None:
                err = _agent_enforce_policy(tool_name, _tool_input)
                if err is not None:
                    return {"block": True, "block_reason": err}
                return None

            original_emit_pre_tool = sub_agent.loop.hook_system.emit_pre_tool

            async def _emit_pre_tool_with_policy(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any] | None:
                result = _policy_hook(tool_name, tool_input)
                if result is not None:
                    return result
                return await original_emit_pre_tool(tool_name, tool_input)

            sub_agent.loop.hook_system.emit_pre_tool = _emit_pre_tool_with_policy

        sub_agent.add_message("user", prompt)

        if cache_context is not None and hasattr(cache_context, "wrap_prompt"):
            system_prompt = cache_context.wrap_prompt(system_prompt or "")
            logger.info("[sub_agent] applied cache context from parent session=%s hash=%s",
                        getattr(cache_context, "parent_session_id", "?"),
                        getattr(cache_context, "prefix_hash", "?"))

        sub_agent.session.id = session_id or sub_agent.session.id or str(uuid.uuid4())
        saved_session_id = sub_agent.session.id
        sub_agent.session.metadata["channel"] = "sub_agent"
        sub_agent.session.parent_session_id = self._session.id or ""

        def _save():
            try:
                from encre.config import get_data_dir
                _dir = get_data_dir() / "sub_agents" / saved_session_id
                _dir.mkdir(parents=True, exist_ok=True)
                sub_agent.session.save_to_dir(str(_dir))
            except Exception:
                logger.warning("[sub_agent] failed to persist session", exc_info=True)

        def _stamp_error_on_session(err_detail: str, err_code: str = "", err_category: str = "") -> None:
            """Persist turn-scoped error metadata onto the sub-agent session.

            Mirrors the main-loop behaviour (loop.py): the red/yellow status
            cards in the UI are driven by ``errorMessage``/``interruptedReason``
            keys stored on the last assistant message. The main loop stamps
            these AFTER its ``yield create_finish(...)`` — which never runs for
            sub-agents because SubAgentRunner stops consuming at Finish — so
            stamping must happen here before the session is saved.
            """
            try:
                text = (err_detail or "").strip() or "Sub-agent failed"
                stamp: dict[str, Any] = {
                    "errorMessage": text,
                    "errorCode": err_code or "execution_error",
                }
                if err_category:
                    stamp["errorCategory"] = err_category
                target: dict[str, Any] | None = None
                for m in reversed(sub_agent.session.messages):
                    if m.get("role") == "assistant":
                        target = m
                        break
                if target is not None:
                    target.update(stamp)
                    c = target.get("content", "")
                    if isinstance(c, str) and "[Backend API Error]" not in c and (err_detail or "").strip():
                        target["content"] = c + f"\n\n[Backend API Error]\n{err_detail.strip()}"
                else:
                    body = f"[Backend API Error]\n{text}"
                    sub_agent.add_message(
                        "assistant", body,
                        segments=[{"kind": "text", "text": body}],
                        **stamp,
                    )
            except Exception:
                logger.warning("[sub_agent] failed to stamp error onto session", exc_info=True)

        result_parts: list[str] = []
        text_buffer = ""
        sub_refs: list[dict[str, Any]] = []
        draft_content: list[str] = []
        draft_reasoning: list[str] = []
        draft_tool_calls: list[dict[str, Any]] = []
        draft_tool_id_to_idx: dict[str, int] = {}
        # Streaming argument chunks per draft tool call (id -> key -> parts).
        # Without these, every live tool card would render with empty
        # arguments until the message commits -- the sub-agent view shows
        # tool cards whose summary/body only fill in after the fact.
        draft_tool_args: dict[str, dict[str, list[str]]] = {}
        draft_segments: list[dict[str, Any]] = []
        last_seen_msg_count = 0
        last_seen_assistant_id: str | None = None
        # Throttle live transcript emissions.  Without this, every streaming
        # delta re-serialises the FULL sub-agent message history and pushes it
        # over the WebSocket, and the frontend re-merges + re-renders the whole
        # sub-agent card on every frame -- O(n^2) traffic that freezes the UI.
        # We coalesce emissions to at most one per interval; the final state is
        # always flushed with ``force=True``.
        last_emit_ts: float = 0.0
        EMIT_INTERVAL = 0.08

        def _has_uncommitted_draft() -> bool:
            return bool(
                draft_content
                or draft_reasoning
                or draft_tool_calls
                or draft_segments
            )

        def _reset_draft() -> None:
            draft_content.clear()
            draft_reasoning.clear()
            draft_tool_calls.clear()
            draft_tool_id_to_idx.clear()
            draft_tool_args.clear()
            draft_segments.clear()

        def _draft_as_message() -> dict[str, Any]:
            # Stamp accumulated argument chunks onto the draft tool calls so
            # live cards carry real params; serialise once per snapshot, not
            # once per delta.
            tcs: list[dict[str, Any]] = []
            for tc in draft_tool_calls:
                fn = tc.get("function") or {}
                chunks = draft_tool_args.get(str(tc.get("id") or ""))
                if chunks:
                    fn = {
                        **fn,
                        "arguments": json.dumps(
                            {k: "".join(v) for k, v in chunks.items()},
                            ensure_ascii=False,
                        ),
                    }
                tcs.append({**tc, "function": dict(fn)})
            return {
                # Stable id across snapshots: the frontend keys its timeline
                # off message ids, and a churning id here would rebuild the
                # whole sub-agent transcript DOM on every progress tick.
                "id": f"{sub_agent.session.id or 'sub'}:draft",
                "role": "assistant",
                "content": "".join(draft_content),
                "reasoning_content": "".join(draft_reasoning),
                "tool_calls": tcs,
                "segments": [dict(s) for s in draft_segments],
                "created_at": time.time(),
            }

        def _sync_draft_with_session() -> None:
            nonlocal last_seen_msg_count, last_seen_assistant_id
            msgs = sub_agent.session.messages
            current_count = len(msgs)
            current_assistant_id: str | None = None
            for m in reversed(msgs):
                if m.get("role") == "assistant":
                    current_assistant_id = str(m.get("id") or "")
                    break
            if (
                current_assistant_id is not None
                and current_assistant_id != last_seen_assistant_id
            ):
                last_seen_msg_count = current_count
                last_seen_assistant_id = current_assistant_id
                _reset_draft()
            elif current_count != last_seen_msg_count:
                last_seen_msg_count = current_count

        def _build_snapshot() -> list[dict[str, Any]]:
            _sync_draft_with_session()
            snapshot = [dict(m) for m in sub_agent.session.messages]
            if _has_uncommitted_draft():
                snapshot.append(_draft_as_message())
            return snapshot

        async def _emit_live(*, force: bool = False) -> None:
            nonlocal last_emit_ts
            if progress_callback is None:
                return
            now = time.time()
            if not force and now - last_emit_ts < EMIT_INTERVAL:
                return
            last_emit_ts = now
            await progress_callback(_build_snapshot())

        def _flush_text_buffer() -> None:
            nonlocal text_buffer
            text = text_buffer.strip()
            if text:
                result_parts.append(f"### Assistant\n{text}\n")
            text_buffer = ""

        self._child_loops.add(sub_agent.loop)
        cancelled = False
        try:
            # Prime the UI before the first model call: the delegated prompt
            # is already a session message, so push that snapshot now. Without
            # this the first emit waits for the model's first streamed token,
            # and the whole plugin-activation + prefill phase renders as a
            # blank loader that never shows what the parent asked for.
            await _emit_live(force=True)
            async for event in sub_agent.run(prompt=prompt, system_prompt=system_prompt or None):
                if event_callback is not None:
                    try:
                        await event_callback(event)
                    except Exception:
                        logger.warning("[sub_agent] event_callback raised", exc_info=True)
                if isinstance(event, TextDelta):
                    text_buffer += event.text
                    draft_content.append(event.text)
                    if draft_segments and draft_segments[-1].get("kind") == "text":
                        draft_segments[-1]["text"] = (
                            str(draft_segments[-1].get("text") or "") + event.text
                        )
                    else:
                        draft_segments.append({"kind": "text", "text": event.text})
                    await _emit_live()
                elif isinstance(event, ThinkingDelta):
                    _flush_text_buffer()
                    thought = event.text.strip()
                    if thought:
                        result_parts.append(f"### Thought\n{thought}\n")
                        draft_reasoning.append(event.text)
                        if draft_segments and draft_segments[-1].get("kind") == "thinking":
                            draft_segments[-1]["text"] = (
                                str(draft_segments[-1].get("text") or "") + event.text
                            )
                        else:
                            draft_segments.append({"kind": "thinking", "text": event.text})
                        await _emit_live()
                elif isinstance(event, ToolCallStart):
                    _flush_text_buffer()
                    result_parts.append(f"### Tool Start\n- id: `{event.id}`\n- name: `{event.name}`\n")
                    tc_dict = {
                        "id": event.id,
                        "type": "function",
                        "function": {"name": event.name, "arguments": "{}"},
                    }
                    draft_tool_calls.append(tc_dict)
                    draft_tool_id_to_idx[event.id] = len(draft_tool_calls) - 1
                    draft_tool_args[event.id] = {}
                    draft_segments.append({"kind": "tool", "tool_id": event.id})
                    await _emit_live()
                elif isinstance(event, ToolCallDelta):
                    # Accumulate streaming arguments so the live card carries
                    # the real params (summary + expandable body) instead of
                    # an empty "{}" until the message commits.
                    bucket = draft_tool_args.setdefault(event.id, {})
                    bucket.setdefault(event.key, []).append(event.value)
                elif isinstance(event, ToolProgress):
                    _flush_text_buffer()
                    result_parts.append(f"### Tool Progress\n- id: `{event.id}`\n- name: `{event.tool_name}`\n- status: `{event.status}`\n")
                    if progress_callback is not None and event.sub_agent_messages:
                        with contextlib.suppress(Exception):
                            await progress_callback(event.sub_agent_messages)
                    else:
                        await _emit_live()
                elif isinstance(event, ToolCallEnd):
                    _flush_text_buffer()
                    result_parts.append(f"### Tool End\n- id: `{event.id}`\n")
                    await _emit_live()
                elif isinstance(event, ToolResult):
                    _flush_text_buffer()
                    content = event.content.strip()
                    if len(content) > 2000:
                        content = f"{content[:2000]}\n... (truncated)"
                    result_parts.append(
                        f"### Tool Result\n- id: `{event.id}`\n- error: `{'yes' if event.is_error else 'no'}`\n\n```text\n{content}\n```\n"
                    )
                    await _emit_live()
                elif isinstance(event, Reference):
                    if event.reference:
                        sub_refs.append(event.reference)
                elif isinstance(event, Finish):
                    _flush_text_buffer()
                    if event.reason == "error":
                        _stamp_error_on_session(
                            str(getattr(event, "error", "") or getattr(event, "message", "") or ""),
                            str(getattr(event, "error_code", "") or ""),
                            str(getattr(event, "error_category", "") or ""),
                        )
                    await _emit_live(force=True)
                    if event.reason == "error":
                        _save()
                        err_detail = str(
                            getattr(event, "error", "")
                            or getattr(event, "message", "")
                            or ""
                        ).strip()
                        return {
                            "content": (
                                f"Error: Sub-agent failed{(' — ' + err_detail) if err_detail else ''}"
                            ),
                            "error": True,
                            "messages": sub_agent.session.messages,
                            "session_id": saved_session_id,
                            "references": sub_refs,
                            "handoff": {
                                "summary": err_detail[:300],
                                "artifacts": [],
                                "next_actions": [],
                            },
                        }
        except asyncio.CancelledError:
            cancelled = True
            logger.info("[sub_agent] cancelled by parent, session_id={sid}", sid=saved_session_id)
            try:
                for m in reversed(sub_agent.session.messages):
                    if m.get("role") == "assistant":
                        m["interruptedReason"] = "cancelled"
                        break
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
        finally:
            _save()
            self._child_loops.discard(sub_agent.loop)

        final_text = ""
        for msg in reversed(sub_agent.session.messages):
            if msg.get("role") != "assistant":
                continue
            txt = str(msg.get("content") or "")
            if txt.strip():
                final_text = txt
                break
            rsn = str(msg.get("reasoning_content") or "")
            if rsn.strip():
                final_text = f"[Thinking]\n{rsn}"
                break
            tcs = msg.get("tool_calls") or []
            if tcs:
                names = [tc.get("function", {}).get("name", "?") for tc in tcs]
                final_text = f"[Tool calls executed: {', '.join(names)}]"
                break
        logger.info("[sub_agent] done session_id={sid} final_len={flen} msgs={mcount} cancelled={cancelled}",
                      sid=saved_session_id, flen=len(final_text), mcount=len(sub_agent.session.messages), cancelled=cancelled)
        logger.info("[sub_agent] final_text={t:.200s}", t=final_text)
        handoff, cleaned_text = _parse_handoff(final_text)
        if cancelled:
            handoff["summary"] = "Cancelled by user before completion"
            handoff["next_actions"] = ["Re-dispatch the sub-agent task; the session was interrupted mid-run"]
        content = cleaned_text.strip() or (
            "[Cancelled by user]" if cancelled else "No output from sub-agent"
        )
        return {
            "content": content,
            "messages": sub_agent.session.messages,
            "session_id": saved_session_id,
            "references": sub_refs,
            "handoff": handoff,
        }
