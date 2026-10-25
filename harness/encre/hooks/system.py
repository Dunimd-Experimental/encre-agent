#!/usr/bin/env python3

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

"""Encre hook system: runtime registry & emit API.

Implements :class:`EncreHookSystem`, which holds the registered
:data:`~encre.hooks.types.HookHandler` callables (keyed by
:data:`~encre.hooks.types.HookEventType`) and provides ``emit_*`` methods
the agent runtime calls at each lifecycle point.  Handlers may block an
operation or modify its input; the system merges their results in priority
order.  File-backed hooks are loaded into it via
:mod:`encre.hooks.file_loader`.
"""

import asyncio
import contextlib
import logging
import uuid
from typing import Any

from encre.events.ledger import HandlerCircuitBreaker
from encre.hooks.types import (
    HookEventType,
    HookHandler,
    HookResponseEvent,
    HookResult,
    HookStartedEvent,
)

logger = logging.getLogger(__name__)

#: Default per-handler wall-clock budget in seconds.  A handler exceeding it
#: is treated as failed (and fails closed on veto-capable events) but can
#: never stall the guarded operation indefinitely.
_HANDLER_TIMEOUT_SECONDS = 10.0

#: Events whose hook results can veto an operation (routed through
#: ``_emit_with_modify``).  A crashing handler on one of these must fail
#: CLOSED: the guarded operation is blocked instead of silently allowed.
_BLOCKING_EVENTS: frozenset[HookEventType] = frozenset(
    {
        "pre_tool_exec",
        "pre_bash",
        "pre_model_request",
        "on_permission_request",
        "pre_compact",
        "pre_sub_agent",
    }
)


class EncreHookSystem:
    """Hook system with 20+ event types covering the full agent lifecycle.

    Events:
      Tool: pre_tool_exec, post_tool_exec, on_tool_progress, pre_bash
      Session: on_session_start, on_session_end, on_checkpoint
      Turn: on_turn_start, on_turn_end
      Model: pre_model_request, post_model_response
      Safety: on_permission_request, on_permission_response
      Error: on_error, on_backend_error, on_rate_limit
      Compact: pre_compact, post_compact
      Agent: pre_sub_agent, post_sub_agent
      Goal: on_goal_progress
      IO: on_file_change
      Meta: on_telemetry

    Handlers are async callables returning HookResult dicts with optional:
      - block: bool -- block the operation
      - block_reason: str
      - modified_input: dict -- PARTIAL override; only the keys the handler
        returns are applied on top of the original input (consumers merge)
      - extra_context: str -- inject into output
    """

    _ALL_EVENTS: tuple[HookEventType, ...] = (
        "pre_tool_exec",
        "post_tool_exec",
        "on_tool_progress",
        "pre_bash",
        "on_session_start",
        "on_session_end",
        "on_checkpoint",
        "on_turn_start",
        "on_turn_end",
        "pre_model_request",
        "post_model_response",
        "on_permission_request",
        "on_permission_response",
        "on_error",
        "on_backend_error",
        "on_rate_limit",
        "pre_compact",
        "post_compact",
        "pre_sub_agent",
        "post_sub_agent",
        "on_goal_progress",
        "on_file_change",
        "on_user_message_persisted",
        "on_telemetry",
    )

    def __init__(self) -> None:
        self._handlers: dict[HookEventType, list[tuple[str, HookHandler]]] = {e: [] for e in self._ALL_EVENTS}
        # Arbitration order cache: sorting per dispatch paid O(n log n) on
        # every event even though the handler list only changes on
        # register/unregister.  Invalidated by both mutation paths.
        self._ordered_handlers: dict[HookEventType, list[tuple[int, tuple[str, HookHandler]]]] = {}
        self._handler_metadata: dict[str, dict[str, Any]] = {}
        self._event_handlers: list[callable] = []
        self._enabled = True
        # Per-handler circuit breaker: repeated failures open a cooldown that
        # doubles on every re-open; successes reset it.  It only gates
        # attempts -- arbitration authority stays with the built-in code.
        self._breaker = HandlerCircuitBreaker()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def register_handler(
        self,
        event_type: HookEventType,
        handler: HookHandler,
        handler_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Register a hook ``handler`` for a single ``event_type``.

        Assigns (or accepts) a stable handler id, appends the handler to the
        event's list, and records any supplied metadata for later inspection via
        :meth:`list_handlers`.

        Args:
            event_type: The lifecycle event the handler observes.
            handler: The async callable invoked when the event fires.
            handler_id: Optional explicit id; a random UUID is used if omitted.
            metadata: Optional metadata attached to the handler (e.g. source
                path, command, matcher).

        Returns:
            The handler id used (given or freshly generated).

        Raises:
            ValueError: If ``event_type`` is not a known event.
        """
        hid = handler_id or str(uuid.uuid4())
        if event_type not in self._handlers:
            raise ValueError(f"Unknown hook event type: {event_type}")
        self._handlers[event_type].append((hid, handler))
        self._ordered_handlers.pop(event_type, None)
        if metadata:
            self._handler_metadata[hid] = metadata
        return hid

    def unregister_handler(self, handler_id: str) -> bool:
        """Remove a previously registered handler by its id.

        Args:
            handler_id: The id returned by :meth:`register_handler`.

        Returns:
            True if a handler with that id was found and removed, else False.
        """
        for event_type, handlers in self._handlers.items():
            for i, (hid, _) in enumerate(handlers):
                if hid == handler_id:
                    handlers.pop(i)
                    self._handler_metadata.pop(handler_id, None)
                    self._ordered_handlers.pop(event_type, None)
                    return True
        return False

    def list_handlers(self, event_type: HookEventType | None = None) -> list[dict[str, Any]]:
        """Return metadata about registered handlers, optionally filtered by event.

        Each entry contains ``handler_id``, ``event_type`` and the
        metadata dict that was supplied at registration time (typically
        ``source_path``, ``matcher`` and ``command`` for file-loaded
        hooks), plus the live breaker state (``disabled`` / ``failures``).
        Use this to surface the live hook set in the UI
        without exposing the callables themselves.
        """
        out: list[dict[str, Any]] = []
        if event_type is not None:
            for hid, _ in self._handlers.get(event_type, []):
                meta = dict(self._handler_metadata.get(hid, {}))
                meta.setdefault("handler_id", hid)
                meta["event_type"] = event_type
                meta["disabled"] = not self._breaker.allow(hid)
                meta["failures"] = self._breaker.status(hid)["failures"]
                out.append(meta)
            return out
        for evt, handlers in self._handlers.items():
            for hid, _ in handlers:
                meta = dict(self._handler_metadata.get(hid, {}))
                meta.setdefault("handler_id", hid)
                meta["event_type"] = evt
                meta["disabled"] = not self._breaker.allow(hid)
                meta["failures"] = self._breaker.status(hid)["failures"]
                out.append(meta)
        return out

    def on_event(self, callback: callable) -> None:
        """Register a global event observer (receives all HookStartedEvent/HookResponseEvent)."""
        self._event_handlers.append(callback)

    # --- Tool hooks ---

    async def emit_pre_tool(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any] | None:
        """Raised before a tool executes. Handlers may block or modify input."""
        return await self._emit_with_modify("pre_tool_exec", tool_name, tool_input)

    async def emit_post_tool(self, tool_name: str, tool_input: dict[str, Any], tool_output: str) -> str | None:
        """Raised after a tool executes. Handlers may inject extra context."""
        return await self._emit_with_extra("post_tool_exec", tool_name, {"input": tool_input, "output": tool_output})

    async def emit_tool_progress(self, tool_name: str, status: str, detail: str = "") -> None:
        """Raised during long-running tool execution."""
        await self._run_handlers("on_tool_progress", tool_name, {"status": status, "detail": detail})

    async def emit_pre_bash(self, command: str) -> dict[str, Any] | None:
        """Raised specifically before bash commands. May block dangerous commands."""
        return await self._emit_with_modify("pre_bash", "bash", {"command": command})

    # --- Session hooks ---

    async def emit_session_start(self) -> None:
        await self._run_handlers("on_session_start", "_session", {})

    async def emit_session_end(self) -> None:
        await self._run_handlers("on_session_end", "_session", {})

    async def emit_checkpoint(self, label: str) -> None:
        await self._run_handlers("on_checkpoint", label, {})

    # --- Turn hooks ---

    async def emit_turn_start(self, turn: int) -> None:
        await self._run_handlers("on_turn_start", "_turn", {"turn": turn})

    async def emit_turn_end(self, turn: int, event_count: int = 0) -> None:
        await self._run_handlers("on_turn_end", "_turn", {"turn": turn, "event_count": event_count})

    # --- Model hooks ---

    async def emit_pre_model_request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        """Raised before each model API call. Handlers may modify messages or tools."""
        return await self._emit_with_modify("pre_model_request", "_model", {"messages": messages, "tools": tools})

    async def emit_post_model_response(self, response_text: str, tool_calls_count: int) -> str | None:
        """Raised after model responds. Handlers may inject feedback."""
        return await self._emit_with_extra("post_model_response", "_model", {"text": response_text, "tool_calls_count": tool_calls_count})

    # --- Permission hooks ---

    async def emit_permission_request(self, tool_name: str, reason: str) -> dict[str, Any] | None:
        """Raised when a tool requires user permission."""
        return await self._emit_with_modify("on_permission_request", tool_name, {"reason": reason})

    async def emit_permission_response(self, tool_name: str, approved: bool) -> None:
        """Raised when user responds to a permission request."""
        await self._run_handlers("on_permission_response", tool_name, {"approved": approved})

    # --- Error hooks ---

    async def emit_error(self, error: Exception, context: str) -> dict[str, Any] | None:
        """Raised on errors.  Handlers may steer recovery by returning
        ``{"modified_input": {"recovery_action": "<RecoveryAction member>"}}``
        (e.g. ``RELEASE`` to abort retrying, ``COMPACT_CONTINUE`` /
        ``FALLBACK_CONTINUE`` to force that strategy)."""
        return await self._emit_with_modify(
            "on_error",
            context,
            {"error": str(error), "error_type": type(error).__name__},
        )

    async def emit_backend_error(self, error_message: str, provider: str) -> None:
        await self._run_handlers("on_backend_error", provider, {"message": error_message})

    async def emit_rate_limit(self, provider: str, retry_after: float = 0) -> None:
        await self._run_handlers("on_rate_limit", provider, {"retry_after": retry_after})

    # --- Compact hooks ---

    async def emit_pre_compact(self, message_count: int, estimated_tokens: int) -> dict[str, Any] | None:
        """Raised before context compaction. May block compaction."""
        return await self._emit_with_modify(
            "pre_compact", "_compact", {"message_count": message_count, "estimated_tokens": estimated_tokens}
        )

    async def emit_post_compact(self, old_count: int, new_count: int) -> None:
        await self._run_handlers("post_compact", "_compact", {"old_message_count": old_count, "new_message_count": new_count})

    # --- Sub-agent hooks ---

    async def emit_pre_sub_agent(self, prompt: str, tool_names: list[str]) -> dict[str, Any] | None:
        """Raised before spawning a sub-agent. May block or modify prompt."""
        return await self._emit_with_modify("pre_sub_agent", "_sub_agent", {"prompt": prompt, "tools": tool_names})

    async def emit_post_sub_agent(self, result: str, latency_ms: float) -> str | None:
        """Raised after a sub-agent finishes, exposing its result and latency.

        Args:
            result: The sub-agent's output text.
            latency_ms: Time the sub-agent took to complete, in milliseconds.

        Returns:
            Optional extra context string injected by handlers.
        """
        return await self._emit_with_extra("post_sub_agent", "_sub_agent", {"result": result, "latency_ms": latency_ms})

    # --- Goal progress ---

    async def emit_goal_progress(self, attempt: int, status: str, message: str) -> None:
        await self._run_handlers("on_goal_progress", "_goal", {"attempt": attempt, "status": status, "message": message})

    # --- File change ---

    async def emit_file_change(self, path: str, operation: str) -> None:
        """Raised when the agent writes or modifies a file."""
        await self._run_handlers("on_file_change", path, {"operation": operation})

    async def emit_user_message_persisted(self, session_id: str) -> None:
        """Raised when a user message has been added to the session.

        Subscribers (e.g. the WS layer's session manager) use this to flush
        the session to disk immediately, so a process kill between this
        point and the model's response still leaves a resumable transcript.
        Mirrors Claude Code's await-on-user-message-before-query-loop
        (QueryEngine.ts:450-463).
        """
        await self._run_handlers("on_user_message_persisted", session_id, {})

    # --- Telemetry ---

    async def emit_telemetry(self, data: dict[str, Any]) -> None:
        await self._run_handlers("on_telemetry", "_telemetry", data)

    # --- Internal helpers ---

    async def _emit_with_modify(self, event_type: HookEventType, name: str, context: dict[str, Any]) -> dict[str, Any] | None:
        """Run handlers that may block or modify input."""
        if not self._enabled:
            return None
        results = await self._run_handlers(event_type, name, context)
        merged: dict[str, Any] = {}
        for r in results:
            if r.get("block"):
                return {
                    "block": True,
                    "block_reason": r.get("block_reason", f"Blocked by hook: {name}"),
                }
            mi = r.get("modified_input")
            if mi:
                for k, v in mi.items():
                    if k in merged and merged[k] != v:
                        logger.warning(
                            "Hook handlers disagree on modified key %r for %s; the later handler wins",
                            k,
                            event_type,
                        )
                    merged[k] = v
        # Partial-override semantics: ``modified_input`` carries ONLY the
        # keys handlers explicitly returned.  The emitted ``context`` (e.g.
        # the original message list for ``pre_model_request``) is never
        # echoed back -- otherwise any handler that tweaks one parameter
        # would silently re-install the pre-engineering input and wipe
        # everything earlier phases (context providers, request
        # engineering) contributed.
        return {"modified_input": merged} if merged else None

    async def _emit_with_extra(self, event_type: HookEventType, name: str, context: dict[str, Any]) -> str | None:
        """Run handlers that may inject extra context."""
        if not self._enabled:
            return None
        results = await self._run_handlers(event_type, name, context)
        parts: list[str] = []
        for r in results:
            ec = r.get("extra_context")
            if ec:
                parts.append(str(ec))
        return "\n".join(parts) if parts else None

    def _handler_timeout(self, hid: str) -> float:
        """Resolve the wall-clock budget for one handler invocation.

        Handlers may declare ``timeout`` in their registration metadata; a
        missing, invalid or non-positive value falls back to
        :data:`_HANDLER_TIMEOUT_SECONDS`.
        """
        meta = self._handler_metadata.get(hid) or {}
        try:
            value = float(meta.get("timeout", _HANDLER_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            return _HANDLER_TIMEOUT_SECONDS
        return value if value > 0 else _HANDLER_TIMEOUT_SECONDS

    def _handler_priority(self, hid: str) -> float:
        """Resolve the arbitration priority for one handler (lower first).

        Handlers registered through the plugin host carry ``priority`` in
        their metadata; file-loaded hooks and other legacy registrars do
        not and default to ``100``.
        """
        meta = self._handler_metadata.get(hid) or {}
        try:
            return float(meta.get("priority", 100))
        except (TypeError, ValueError):
            return 100.0

    async def _run_handlers(
        self,
        event_type: HookEventType,
        name: str,
        context: dict[str, Any],
    ) -> list[HookResult]:
        results: list[HookResult] = []
        # Arbitration order: lower metadata ``priority`` runs first; ties
        # keep registration order (stable sort on the original index).
        # Cached per event type — the handler list only mutates through
        # register/unregister, which invalidate the cache.
        ordered = self._ordered_handlers.get(event_type)
        if ordered is None:
            entries = self._handlers.get(event_type, [])
            ordered = sorted(
                enumerate(entries),
                key=lambda pair: (self._handler_priority(pair[1][0]), pair[0]),
            )
            self._ordered_handlers[event_type] = ordered
        for _idx, (hid, handler) in ordered:
            if not self._breaker.allow(hid):
                # Breaker open: skip the handler entirely until its cooldown
                # elapses instead of paying the timeout on every dispatch.
                logger.warning(
                    "Hook handler %s skipped on event %s -- breaker open",
                    hid,
                    event_type,
                )
                continue
            self._dispatch_event(HookStartedEvent(hook_id=hid, hook_name=name, event_type=event_type))
            timeout = self._handler_timeout(hid)
            try:
                r = await asyncio.wait_for(handler(name, context, None), timeout=timeout)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self._breaker.record_failure(hid)
                self._dispatch_event(
                    HookResponseEvent(
                        hook_id=hid,
                        hook_name=name,
                        event_type=event_type,
                        output=f"timeout after {timeout:.1f}s",
                        exit_code=1,
                        outcome="error",
                    )
                )
                if event_type in _BLOCKING_EVENTS:
                    # Fail closed: a stalled handler on a veto-capable event
                    # blocks the guarded operation instead of allowing it.
                    logger.error(
                        "Hook handler %s timed out after %.1fs on blocking event %s",
                        hid,
                        timeout,
                        event_type,
                    )
                    results.append(
                        {
                            "block": True,
                            "block_reason": (f"Hook handler {hid} timed out on {event_type}"),
                        }
                    )
                else:
                    logger.error(
                        "Hook handler %s timed out after %.1fs on event %s",
                        hid,
                        timeout,
                        event_type,
                    )
            except Exception as e:
                self._breaker.record_failure(hid)
                self._dispatch_event(
                    HookResponseEvent(
                        hook_id=hid,
                        hook_name=name,
                        event_type=event_type,
                        output=str(e),
                        exit_code=1,
                        outcome="error",
                    )
                )
                if event_type in _BLOCKING_EVENTS:
                    # Fail closed: a crashing handler on a veto-capable event
                    # blocks the guarded operation instead of allowing it.
                    logger.error(
                        "Hook handler %s crashed on blocking event %s: %s",
                        hid,
                        event_type,
                        e,
                        exc_info=True,
                    )
                    results.append(
                        {
                            "block": True,
                            "block_reason": (f"Hook handler {hid} failed on {event_type}: {e}"),
                        }
                    )
                else:
                    # Observability event: keep swallow-and-log behavior.
                    logger.error(
                        "Hook handler %s failed on event %s: %s",
                        hid,
                        event_type,
                        e,
                        exc_info=True,
                    )
            else:
                self._breaker.record_success(hid)
                if r:
                    results.append(r)
                self._dispatch_event(
                    HookResponseEvent(
                        hook_id=hid,
                        hook_name=name,
                        event_type=event_type,
                        output=str(r) if r else "",
                    )
                )
        return results

    def _dispatch_event(self, event: HookStartedEvent | HookResponseEvent) -> None:
        for cb in self._event_handlers:
            with contextlib.suppress(Exception):
                cb(event)
