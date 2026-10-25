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

"""Protocol layer: multi-session event routing to the agent runtime.

Hosts :class:`EventRouter`, the bridge between channels (IM adapters via
:mod:`encre.gateway.run`, the desktop client over WebSocket, the headless
JSONL transport) and the agent sessions owned by
:class:`encre.server.session_manager.SessionManager`.

The router exposes :meth:`EventRouter.submit` and
:meth:`EventRouter.submit_stream`, which channels call to run prompts. Each
session gets an independent conversation history, and the SessionManager's
semaphore is what caps how many can run at once.

Everything runs in iClaw mode: the router always injects the ``iclaw_mode``
identity prompt and the connected-adapters context, whether the caller is the
desktop client (``channel="iclaw"``) or a platform adapter (Telegram, QQ,
...). Sessions are saved to the main sessions directory alongside desktop
client sessions, so the frontend sees every conversation regardless of source.

Data flow for one prompt: caller wraps the call in :meth:`iclaw_context` ->
:meth:`submit_stream` loads or creates the session -> the session is tagged
with its channel and moved to ``RUNNING`` -> a concurrency slot is acquired ->
the prompt is appended to history -> the agent runs, each event is yielded and
observed for hook emission -> in ``finally`` the session returns to ``IDLE``,
the slot is released, and the session is persisted.

Public API:

- :class:`EventRouter`
- :meth:`EventRouter.submit` / :meth:`EventRouter.submit_stream`
- :meth:`EventRouter.cancel_session` / :meth:`EventRouter.shutdown`
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from encre.model_selection import apply_target_models
from encre.prompts.loader import PromptLoader
from encre.server.session_manager import SessionState
from encre.utils.types import (
    AgentEvent,
    Finish,
    TextDelta,
    ToolResult,
)

if TYPE_CHECKING:
    from encre.config import EncreConfig
    from encre.server.session_manager import SessionManager

logger = logging.getLogger("encre.protocol.session_router")

# Adapter name used for the router's own rows in the durable pending-message
# queue.  Distinct from real adapters so a startup replay can tell protocol
# backlogs (desktop/automation submits) apart from IM adapter backlogs.
_ROUTER_QUEUE_ADAPTER = "router"

#: Module-level prompt loader, shared by every router. Prompt files are read
#: only, so a single instance is safe to reuse across sessions.
_prompt_loader = PromptLoader()


class SubmitBacklog:
    """Shared FIFO busy-backlog for one submit surface.

    One instance per submit channel: the protocol router's adapter space and
    the desktop WS run path each own one, distinguished by *adapter_name* in
    the durable pending-message table.  Entries are JSON-serialisable dicts
    (plus one private ``_owner`` reference that never persists); the owner
    supplies a ``runner`` callback that executes one drained entry
    server-side, so the backlog itself stays free of runtime details.

    Every mutation is mirrored into the durable store (when wired via
    :meth:`set_store`) as a whole-row rewrite, and announced to the listener
    (:meth:`set_listener`) so owners can repaint their queue UIs.

    Args:
        adapter_name: Registry name used for this backlog's rows in the
            pending-message table (e.g. ``"router"`` or ``"desktop"``).
        runner: Async callable ``(session_id, entry)`` that executes one
            drained entry.  Optional; :meth:`schedule_drain` is a no-op
            until one is installed.
    """

    def __init__(
        self,
        adapter_name: str,
        runner: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        """Store the surface name and wire the drain callback.

        Args:
            adapter_name: Durable-table name for this backlog's rows.
            runner: Async ``(session_id, entry)`` executor for drained
                entries, or ``None`` to defer wiring to :meth:`set_runner`.
        """
        self._adapter = adapter_name
        self._runner = runner
        # session_id -> FIFO backlog of submit requests that arrived while
        # that conversation was running.  Drained FIFO right after the
        # current run finishes, so a message is queued rather than dropped.
        self._entries: dict[str, list[dict[str, Any]]] = {}
        # Optional durable store (SessionStore) mirroring the backlog.
        self._store: Any = None
        # Optional async listener ``(backlog, session_id)`` fired after every
        # mutation so owners can broadcast queue state to their UIs.
        self._listener: Callable[[SubmitBacklog, str], Awaitable[None]] | None = None
        # Strong references to drain tasks so they are not garbage-collected
        # mid-run, plus the per-session pending timer so repeated
        # schedule_drain calls cannot stack duplicate drains.
        self._tasks: set[asyncio.Task[None]] = set()
        self._drain_waiters: dict[str, asyncio.Task[None]] = {}

    def set_runner(self, runner: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        """Install (or replace) the drained-entry executor.

        Args:
            runner: Async callable ``(session_id, entry)``.
        """
        self._runner = runner

    def set_store(self, store: Any) -> None:
        """Wire an optional durable store for the backlog.

        When set, every mutation rewrites this backlog's rows via the
        store's ``replace_pending`` API, so a backlog that outlives the
        process is replayed by :meth:`replay` on the next startup.

        Args:
            store: An object exposing ``replace_pending(adapter,
                session_key, payloads)`` / ``pop_all_pending(adapter)``, or
                ``None`` to disable persistence.
        """
        self._store = store

    def set_listener(
        self, listener: Callable[[SubmitBacklog, str], Awaitable[None]] | None
    ) -> None:
        """Install (or clear) the post-mutation notify callback.

        Args:
            listener: Async callable ``(backlog, session_id)`` invoked after
                every queue mutation, or ``None`` to clear.
        """
        self._listener = listener

    @property
    def adapter_name(self) -> str:
        """Return the durable-table name this backlog owns.

        Returns:
            The adapter name passed to the constructor.
        """
        return self._adapter

    def entries(self, session_id: str) -> list[dict[str, Any]]:
        """Return a shallow copy of one conversation's ordered backlog.

        Args:
            session_id: The conversation to read.

        Returns:
            The queued entries, oldest first; empty when none.  The list is
            a copy, so callers can iterate it while the backlog mutates.
        """
        return list(self._entries.get(session_id, ()))

    def count(self, session_id: str) -> int:
        """Return how many entries wait behind the busy turn.

        Args:
            session_id: The conversation to count.

        Returns:
            The backlog length for that conversation.
        """
        return len(self._entries.get(session_id, ()))

    def queue(self, session_id: str, entry: dict[str, Any], owner: Any = None) -> None:
        """Append one submit request to the conversation's FIFO backlog.

        The entry is persisted (without private keys) and the listener is
        notified so UIs repaint immediately.

        Args:
            session_id: The busy conversation the request waits behind.
            entry: JSON-serialisable payload describing the submit.
            owner: Optional private reference (e.g. the requesting socket)
                kept for drain-time routing; never persisted.
        """
        stored = dict(entry)
        if owner is not None:
            stored["_owner"] = owner
        self._entries.setdefault(session_id, []).append(stored)
        logger.info("[backlog:%s] queued submit for %s (backlog=%d)",
                    self._adapter, session_id, len(self._entries[session_id]))
        self._sync_store(session_id)
        self._notify(session_id)

    def pop(self, session_id: str) -> dict[str, Any] | None:
        """Take the head entry of one conversation's backlog.

        Args:
            session_id: The conversation to drain.

        Returns:
            The oldest entry, or ``None`` when the backlog is empty.  The
            durable mirror and listener are updated on removal.
        """
        entries = self._entries.get(session_id)
        if not entries:
            self._entries.pop(session_id, None)
            return None
        entry = entries.pop(0)
        if not entries:
            self._entries.pop(session_id, None)
        self._sync_store(session_id)
        self._notify(session_id)
        return entry

    def remove_at(self, session_id: str, index: int) -> dict[str, Any] | None:
        """Delete one backlog entry by position.

        Args:
            session_id: The conversation whose backlog to edit.
            index: Zero-based position of the entry to drop.

        Returns:
            The removed entry, or ``None`` when the index is out of range.
        """
        entries = self._entries.get(session_id)
        if not entries or not (0 <= index < len(entries)):
            return None
        entry = entries.pop(index)
        if not entries:
            self._entries.pop(session_id, None)
        self._sync_store(session_id)
        self._notify(session_id)
        return entry

    def move(self, session_id: str, from_index: int, to_index: int) -> bool:
        """Reorder one backlog entry (drag-and-drop support).

        Args:
            session_id: The conversation whose backlog to edit.
            from_index: Zero-based position of the entry to relocate.
            to_index: Target position; clamped into range.

        Returns:
            ``True`` when the entry moved, ``False`` when the indices were
            out of range or equal.
        """
        entries = self._entries.get(session_id)
        if not entries or not (0 <= from_index < len(entries)):
            return False
        to_index = max(0, min(to_index, len(entries) - 1))
        if from_index == to_index:
            return False
        entries.insert(to_index, entries.pop(from_index))
        self._sync_store(session_id)
        self._notify(session_id)
        return True

    def schedule_drain(self, session_id: str, grace: float = 0.0) -> None:
        """Pop the first backlog entry of *session_id* and run it.

        Called when a run finishes (its ``finally`` block) once the session
        is IDLE again.  *grace* delays the pop so UIs get a window to edit
        the queue (remove, reorder, or take the head over) before execution
        starts.  A repeated call while a drain is already pending for the
        same conversation is ignored, so chained run completions cannot
        stack duplicate timers.

        Args:
            session_id: The conversation whose backlog to advance.
            grace: Seconds to wait before taking the head entry.
        """
        if self._runner is None:
            return
        pending = self._drain_waiters.get(session_id)
        if pending is not None and not pending.done():
            return

        async def _drain() -> None:
            """Sleep out the grace window, then execute the head entry.

            Runs inside a tracked task; the drained run re-enters the
            owner's submit path, so its own completion chains the next
            entry -- a plain loop here would not work because the previous
            run still holds its submit lock until the generator closes.
            """
            if grace > 0:
                await asyncio.sleep(grace)
            entry = self.pop(session_id)
            if entry is None:
                return
            logger.info("[backlog:%s] draining queued submit for %s (channel=%s)",
                        self._adapter, session_id, entry.get("channel") or "desktop")
            try:
                await self._runner(session_id, entry)
            except Exception as e:
                logger.error("[backlog:%s] drained submit error [%s] session=%s: %s",
                             self._adapter, entry.get("channel"), session_id, e)

        try:
            task = asyncio.create_task(_drain())
        except RuntimeError:
            # No running loop (shutdown edge): drop instead of crash.
            logger.warning("[backlog:%s] cannot drain queued submit for %s: no event loop",
                           self._adapter, session_id)
            return
        self._drain_waiters[session_id] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def run_now(self, session_id: str, entry: dict[str, Any]) -> asyncio.Task[None]:
        """Execute one already-popped entry immediately through the runner.

        Unlike :meth:`schedule_drain` this takes no grace window and touches
        no queue state: the caller popped the entry (``queue_pop`` handler)
        and just wants it running.  The coroutine is spawned as a tracked
        task, so the synchronous caller (the WS dispatch loop) stays
        responsive.

        Args:
            session_id: The conversation the entry belongs to.
            entry: The popped payload to execute.

        Returns:
            The spawned task (already scheduled; callers may track it).
        """
        async def _run() -> None:
            try:
                await self._runner(session_id, entry)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[backlog:%s] immediate submit error [%s] session=%s: %s",
                             self._adapter, entry.get("channel"), session_id, e)

        task = asyncio.create_task(_run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def replay(self) -> None:
        """Re-run submits that were still queued when the process ended.

        Rows are popped from the store up front (pop semantics, so a crash
        mid-replay loses them instead of duplicating), then each entry is
        executed through the runner.  A no-op without a wired store.
        """
        if self._store is None or self._runner is None:
            return
        try:
            rows = self._store.pop_all_pending(self._adapter)
        except Exception as e:
            logger.warning("[backlog:%s] failed to load persisted queued submits: %s",
                           self._adapter, e)
            return
        for session_id, payload in rows:
            if not isinstance(payload, dict) or "prompt" not in payload:
                logger.warning("[backlog:%s] dropped a malformed queued submit row for %s",
                               self._adapter, session_id)
                continue
            logger.info("[backlog:%s] replaying persisted queued submit for %s",
                        self._adapter, session_id)
            try:
                task = asyncio.create_task(self._replay_one(session_id, payload))
            except RuntimeError:
                logger.warning("[backlog:%s] cannot replay queued submit for %s: no event loop",
                               self._adapter, session_id)
                continue
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _replay_one(self, session_id: str, entry: dict[str, Any]) -> None:
        """Execute one replayed entry through the owner's runner.

        Args:
            session_id: The conversation to run the entry against.
            entry: The persisted payload recorded by :meth:`queue`.
        """
        try:
            await self._runner(session_id, entry)
        except Exception as e:
            logger.error("[backlog:%s] replayed submit error [%s] session=%s: %s",
                         self._adapter, entry.get("channel"), session_id, e)

    def _durable_copy(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """Return a JSON-safe copy of one entry, or ``None`` if unserializable.

        Private keys (leading underscore, e.g. the ``_owner`` socket
        reference) are stripped; the remainder must round-trip through JSON
        because the durable table stores text blobs.

        Args:
            entry: The in-memory entry.

        Returns:
            A clean copy safe to persist, or ``None`` when the entry holds
            non-serialisable data beyond the private keys.
        """
        clean = {k: v for k, v in entry.items() if not k.startswith("_")}
        try:
            json.dumps(clean)
        except (TypeError, ValueError):
            return None
        return clean

    def _sync_store(self, session_id: str) -> None:
        """Mirror the in-memory backlog into the durable store.

        The whole ordered backlog for one conversation is rewritten, so the
        table always matches memory exactly after any mutation.

        Args:
            session_id: The conversation whose rows to rewrite.
        """
        if self._store is None:
            return
        payloads: list[dict[str, Any]] = []
        for entry in self._entries.get(session_id, ()):
            clean = self._durable_copy(entry)
            if clean is not None:
                payloads.append(clean)
        try:
            self._store.replace_pending(self._adapter, session_id, payloads)
        except Exception as e:
            logger.warning("[backlog:%s] failed to persist queued submits for %s: %s",
                           self._adapter, session_id, e)

    def _notify(self, session_id: str) -> None:
        """Fire the listener after a mutation (fire-and-forget, isolated).

        Args:
            session_id: The conversation whose queue changed.
        """
        if self._listener is None:
            return
        try:
            task = asyncio.create_task(self._listener(self, session_id))
        except RuntimeError:
            return
        except Exception as e:
            logger.debug("[backlog:%s] listener dispatch failed: %s", self._adapter, e)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class EventRouter:
    """Multi-session event router backed by the :class:`SessionManager`.

    Each channel connection gets its own isolated agent session with
    independent conversation history. Concurrency is controlled by the
    SessionManager's semaphore, not by this class.

    Callers **must** wrap :meth:`submit_stream` (and any session lookups) in an
    :meth:`iclaw_context` block to serialize concurrent iClaw operations::

        async with router.iclaw_context():
            info = router.session_manager.try_resume_most_recent(...)
            async for event in router.submit_stream(...):
                ...

    The router can optionally hold a reference to connected adapter names,
    set via :meth:`set_connected_adapters`, so the AI knows which IM
    platforms are active when operating in iClaw mode.

    Invariants:

    - A session that is not ``IDLE`` is answered with a terminal
      :class:`Finish` rather than being queued; two concurrent runs of one
      conversation would interleave their histories.
    - The concurrency slot is acquired *after* the busy check and is released
      in ``finally``, so every path -- success, cancellation, and exception --
      returns the slot and resets the session state.
    - Every submitted session is tagged with its originating channel on both
      ``agent.session.metadata`` and the wrapper ``metadata``, so the session
      list can badge the source.

    Attributes:
        session_manager: The underlying session manager; never ``None``.
        _default_config: Config template copied into each new session.
        _running: Cleared by :meth:`shutdown`.
        _active_streams: ``"<channel>:<session_id>"`` -> task, used by
            :meth:`shutdown` to find and cancel in-flight streams.
        _iclaw_lock: Serializes iClaw operations across callers.
        _connected_adapters: Adapter names advertised into the iClaw prompt.

    Args:
        session_manager: The session manager that owns sessions and the
            concurrency semaphore.
        default_config: Default config for new sessions; a fresh
            :class:`encre.config.EncreConfig` is used when omitted.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        default_config: EncreConfig | None = None,
    ) -> None:
        """Attach the session manager and initialise the router's own state.

        Everything the router needs to arbitrate submissions is set up here:
        the concurrency semaphore and session ownership come from the injected
        :class:`SessionManager`, while the router keeps its own liveness flag,
        in-flight stream table, iClaw lock, and adapter list.

        Args:
            session_manager: The :class:`encre.server.session_manager.SessionManager`
                that owns sessions and the concurrency semaphore; the router
                never creates or deletes sessions itself.
            default_config: Template :class:`encre.config.EncreConfig` copied
                into each new session. When omitted, a fresh default config is
                built here rather than by the caller.

        Invariant:
            ``self._manager`` is never ``None``, ``self._running`` starts
            ``True``, and ``self._active_streams`` starts empty; :meth:`shutdown`
            is the only thing that flips ``_running``.
        """
        if default_config is None:
            from encre.config import EncreConfig
            default_config = EncreConfig()
        self._manager = session_manager
        self._default_config = default_config
        self._running = True
        self._active_streams: dict[str, asyncio.Task[None]] = {}
        self._iclaw_lock = asyncio.Lock()
        # Busy-submit backlog (FIFO per conversation), shared with the
        # desktop run path via the SubmitBacklog class.  Entries that arrive
        # while a session runs are queued here, mirrored durably, and
        # drained when the run finishes.
        self._backlog = SubmitBacklog(_ROUTER_QUEUE_ADAPTER, runner=self._run_drained_entry)
        self._connected_adapters: list[str] = []

    @property
    def session_manager(self) -> SessionManager:
        """Return the underlying session manager.

        Exposed so callers can resume sessions or inspect active counts
        directly, for example the iClaw profile's health reporting.

        Returns:
            The :class:`SessionManager`.
        """
        return self._manager

    def set_connected_adapters(self, names: list[str]) -> None:
        """Update the list of connected adapter names.

        Called by the WS bridge and by the gateway whenever the adapter set
        changes. The list is copied wholesale, so a caller can pass a fresh
        list without the router holding a reference to its mutable state.

        Args:
            names: Registry names of adapters currently connected.
        """
        self._connected_adapters = names

    @asynccontextmanager
    async def iclaw_context(self, timeout: float | None = None) -> AsyncIterator[None]:
        """Context manager: acquire the iClaw lock to serialize operations.

        Sessions are saved to the main sessions directory -- the same place
        desktop client sessions live -- so the frontend sees all
        conversations regardless of source (adapter, iClaw desktop, or normal
        desktop).

        If *timeout* is provided, only the **wait for the lock** is bounded:
        once the lock is held it stays held for the whole body, because a
        platform turn routinely runs for minutes and cancelling it mid-run
        would abort the agent (and, from the ``except`` branch, make this
        generator yield a second time -- surfacing to the caller as
        ``RuntimeError: async generator didn't stop after athrow()``).  A
        caller that cannot get the lock within *timeout* proceeds without it,
        so a platform user's message must not be dropped just because a long
        desktop run holds the lock.

        Args:
            timeout: Maximum seconds to wait for the lock. ``None`` waits
                indefinitely.

        Yields:
            ``None`` while the lock is held, or after the timeout has been
                exceeded.

        Note:
            Proceeding without the lock is a deliberate degradation, not a
            failure: a platform user's message must not be dropped just
            because a long desktop run holds the lock.
        """
        if timeout is None:
            async with self._iclaw_lock:
                yield
            return
        try:
            async with asyncio.timeout(timeout):
                await self._iclaw_lock.acquire()
        except asyncio.TimeoutError:
            logger.warning("[router] iclaw_context timeout after %.1fs, proceeding without lock", timeout)
            yield
            return
        try:
            yield
        finally:
            self._iclaw_lock.release()

    async def submit(
        self,
        channel_name: str,
        prompt: str,
        *,
        session_id: str | None = None,
        system_prompt: str | None = None,
        target_model_ids: list[str] | None = None,
    ) -> str:
        """Submit a prompt and collect the full text response (non-streaming).

        **Must** be called inside an :meth:`iclaw_context` block.

        Args:
            channel_name: Originating channel, tagged onto the session.
            prompt: The user prompt.
            session_id: Session to resume, or ``None`` to create a new one.
            system_prompt: Optional system-prompt override.
            target_model_ids: Optional restriction on which models may serve
                this run.

        Returns:
            The concatenated assistant text. Non-text events and a ``Finish``
            that carries no text are ignored, so a run that produced only tool
            activity returns an empty string rather than noise.
        """
        parts: list[str] = []
        async for event in self.submit_stream(
            channel_name, prompt,
            session_id=session_id,
            system_prompt=system_prompt,
            target_model_ids=target_model_ids,
        ):
            if isinstance(event, TextDelta) and event.text:
                parts.append(event.text)
        return "".join(parts)

    async def submit_stream(
        self,
        channel_name: str,
        prompt: str,
        *,
        session_id: str | None = None,
        system_prompt: str | None = None,
        target_model_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Submit a prompt and stream AgentEvent results.

        **Must** be called inside an :meth:`iclaw_context` block so the
        SessionManager is switched to the iClaw sessions directory.

        If *target_model_ids* is provided, only those models are eligible for
        this run (per-adapter gateway model restriction); the agent falls back
        to the indicator model, then a random enabled model, on failure.

        Args:
            channel_name: Originating channel, e.g. ``"iclaw"``,
                ``"telegram"``, or a bot platform name.
            prompt: The user prompt.
            session_id: Session to resume, or ``None`` to create a new one.
            system_prompt: Optional system-prompt override.
            target_model_ids: Optional model restriction for this run.
            metadata: Optional free-form payload stored on the backlog entry
                when the run is queued (busy), so drained replays can carry
                UI hints such as the active mode badge.

        Yields:
            Agent events as the run progresses, always terminated by exactly
            one :class:`Finish` -- on success, on ``busy``, on
            ``capacity``, on cancellation, and on error alike. Callers can
            therefore rely on seeing a terminal event.

        Raises:
            Exception: Nothing escapes. All failures become a ``Finish``
                event so a broken run never tears down the caller.
        """
        logger.info("[router] submit_stream channel=%s session_id=%s prompt=%.60s system_prompt=%s",
                     channel_name, session_id or "(new)", prompt,
                     "yes" if system_prompt else "no")

        # Load the existing session when an id was supplied, otherwise create a
        # fresh one. The config is copied so per-run model restrictions cannot
        # leak into the router's template.
        cfg = replace(self._default_config)
        apply_target_models(cfg, target_model_ids)
        if session_id:
            info = self._manager.load_or_create_session(
                session_id, config=cfg
            )
        else:
            info = self._manager.create_session(config=cfg)
            session_id = info.session_id

        # Tag session with the originating channel so the session list
        # can display a mode badge (normal / iClaw / qqbot / telegram ...).
        info.agent.session.metadata["channel"] = channel_name
        info.metadata["channel"] = channel_name

        self._last_session_id = session_id
        self._manager.touch(session_id)
        logger.info("[router] session %s ready (state=%s)", session_id, info.state.value)

        # Reject concurrent runs of the same conversation with a terminal
        # Finish instead of raising, so a caller can react uniformly.  The
        # request is still remembered: the backlog drains FIFO right after
        # this session's current run finishes, so a message is queued rather
        # than lost.
        if info.state != SessionState.IDLE:
            self._backlog.queue(session_id, {
                "channel": channel_name,
                "prompt": prompt,
                "system_prompt": system_prompt,
                "target_model_ids": list(target_model_ids) if target_model_ids else None,
                "metadata": dict(metadata) if metadata else None,
            })
            yield Finish(reason="busy", error="Session already running")
            return

        # The semaphore caps total concurrent runs across all sessions.
        acquired = await self._manager.acquire_slot()
        if not acquired:
            logger.warning("[router] slot acquire failed for %s", session_id)
            yield Finish(reason="capacity", error="Server at capacity, try later")
            return

        self._manager.set_session_state(session_id, SessionState.RUNNING)
        stream_key = f"{channel_name}:{session_id}"

        # Add the user message to history before running so a resumed session
        # reads its own prior turns plus the new prompt.
        info.agent.add_message("user", prompt)

        # Build channel context as custom_instructions (not system_prompt) so that
        # the prompt builder still runs and produces the full system prompt with
        # identity, safety, permissions, tools, etc.
        # Always load the iClaw identity prompt -- EventRouter operates exclusively
        # in iClaw mode (wrapped in iclaw_context()), whether called from the
        # desktop client (channel="iclaw") or from an adapter (QQ, Telegram, etc.).
        custom_instructions = _prompt_loader.load("iclaw_mode")

        # Append connected-adapters info so the agent knows what IM platforms are active
        _connected = self._connected_adapters
        if _connected:
            adapter_list = "\n".join(f"- {name}" for name in _connected)
            custom_instructions += "\n\n" + _prompt_loader.load_with_context(
                "connected_adapters", adapter_list=adapter_list,
            )

        # Append channel-specific platform hints from prompt files
        if channel_name != "iclaw":
            # A per-platform prompt wins; otherwise fall back to the generic
            # template rendered with the channel name.
            try:
                hint = _prompt_loader.load(f"platform_{channel_name}")
                logger.info("[router] set rich platform context for %s", channel_name)
            except FileNotFoundError:
                hint = _prompt_loader.load_with_context(
                    "platform_default", channel=channel_name,
                )
                logger.info("[router] set generic channel context for %s", channel_name)
            custom_instructions += "\n\n" + hint

        logger.info("[router] starting agent.run for %s", session_id)

        # Fire lifecycle hooks (aligns with Hermes' gateway hook points).
        # session:start fires once per new conversation; agent:start/agent:end
        # bracket the agent run; agent:step fires on each tool result.  Hook
        # failures are logged and swallowed inside emit(), so a misbehaving hook
        # never aborts the run.
        hooks = None
        try:
            from encre.gateway.hooks import get_hook_registry, SESSION_START, AGENT_START, AGENT_STEP, AGENT_END
            hooks = get_hook_registry()
        except Exception:
            hooks = None
        is_new_session = not bool(getattr(info, "_hook_session_started", False))
        if hooks is not None and is_new_session:
            try:
                await hooks.emit(SESSION_START, {"session_id": session_id, "channel": channel_name})
            except Exception:
                pass
            # Marked on the wrapper so a resumed session does not re-fire
            # session:start for every subsequent turn.
            info._hook_session_started = True  # type: ignore[attr-defined]

        if hooks is not None:
            try:
                await hooks.emit(AGENT_START, {
                    "session_id": session_id,
                    "channel": channel_name,
                    "message": prompt[:500],
                })
            except Exception:
                pass

        _last_response_text = ""
        try:
            async for event in info.agent.run(
                prompt=prompt,
                system_prompt=system_prompt,
                custom_instructions=custom_instructions,
            ):
                # Accumulate the assistant response for the agent:end hook.
                if isinstance(event, TextDelta) and event.text:
                    _last_response_text += event.text
                # agent:step -- approximate Hermes' per-tool-iteration point by
                # firing on each ToolResult (a tool call completing).
                if hooks is not None and isinstance(event, ToolResult) and event.id:
                    try:
                        await hooks.emit(AGENT_STEP, {
                            "session_id": session_id,
                            "tool_id": event.id,
                        })
                    except Exception:
                        pass
                yield event
        except asyncio.CancelledError:
            # Cancellation is not a failure; still report a terminal event so
            # the caller's stream ends cleanly.
            logger.info("[router] session %s cancelled", session_id)
            yield Finish(reason="cancelled")
        except Exception as e:
            logger.error("[router] run error [%s] session=%s: %s %s",
                         channel_name, session_id, type(e).__name__, e)
            yield Finish(reason="error", error=str(e))
        finally:
            # Runs on every path, including cancellation and exception, so a
            # failed run never leaves the session RUNNING or the slot held.
            self._manager.set_session_state(session_id, SessionState.IDLE)
            self._manager.release_slot()
            self._manager._save_session(info)
            self._manager.notify_session_completed()
            self._active_streams.pop(stream_key, None)
            # A run finished: give the session's queued submits their turn.
            # Runs after the first are chained through each drained run's own
            # finally block.
            self._backlog.schedule_drain(session_id)
            if hooks is not None:
                try:
                    await hooks.emit(AGENT_END, {
                        "session_id": session_id,
                        "channel": channel_name,
                        "response": _last_response_text,
                    })
                except Exception:
                    pass
            logger.info("[router] session %s done and saved", session_id)

    def set_pending_store(self, store: Any) -> None:
        """Wire an optional durable store for the submit backlog.

        When set, every queue mutation rewrites the backlog's rows in the
        store's pending-message table, so a backlog that outlives the
        process is replayed by :meth:`replay_queued_submits` on the next
        startup.

        Args:
            store: An object exposing the SessionStore pending-message API
                (``replace_pending``/``pop_all_pending``), or ``None`` to
                disable persistence.
        """
        self._backlog.set_store(store)

    @property
    def backlog(self) -> SubmitBacklog:
        """Return the busy-submit backlog owned by this router.

        Exposed so the desktop WS layer can share the durable pending table
        and register a UI listener on the adapter/iclaw space's queue.

        Returns:
            The :class:`SubmitBacklog`.
        """
        return self._backlog

    async def _run_drained_entry(self, session_id: str, entry: dict[str, Any]) -> None:
        """Execute one drained submit without a streaming consumer.

        The session transcript (user message, response, state) is persisted
        by :meth:`submit_stream` itself; UIs refresh from the session-completed
        notification.  If the session went busy again in the meantime the
        entry simply re-queues and drains after that run.

        Args:
            session_id: The conversation to run the backlog entry against.
            entry: The payload recorded by the busy branch.
        """
        try:
            async with self.iclaw_context():
                async for _event in self.submit_stream(
                    entry.get("channel", "iclaw"),
                    entry.get("prompt", ""),
                    session_id=session_id,
                    system_prompt=entry.get("system_prompt"),
                    target_model_ids=entry.get("target_model_ids"),
                    metadata=entry.get("metadata"),
                ):
                    pass  # drained runs have no live consumer to stream to
        except Exception as e:
            logger.error("[router] queued submit drain error [%s] session=%s: %s",
                         entry.get("channel"), session_id, e)

    async def replay_queued_submits(self) -> None:
        """Re-run submits that were still queued when the process ended.

        Rows are popped from the store up front (pop semantics, so a crash
        mid-replay loses them instead of duplicating), then each entry is
        scheduled like a normal drain.  A no-op without a wired store.
        """
        self._backlog.replay()

    def cancel_session(self, session_id: str) -> bool:
        """Cancel a running session by id.

        Cancels both the agent loop and the wrapper task so no half-written
        event is delivered after the cancel, and resets the session to
        ``IDLE`` immediately so a follow-up submit is not rejected as busy.

        Args:
            session_id: The session to cancel.

        Returns:
            ``True`` when a non-idle session was found, ``False`` when the id
            is unknown or the session was already idle.
        """
        info = self._manager.get_session(session_id)
        if info is None or info.state == SessionState.IDLE:
            return False
        if info.agent_task and not info.agent_task.done():
            info.agent.loop.cancel()
            info.agent_task.cancel()
        self._manager.set_session_state(session_id, SessionState.IDLE)
        self._manager.release_slot()
        return True

    def get_session_ids(self) -> list[str]:
        """Return all active session IDs.

        Returns:
            The ids currently known to the session manager.
        """
        return [
            s["session_id"]
            for s in self._manager.list_sessions()
        ]

    async def shutdown(self) -> None:
        """Cancel every in-flight stream and clear the tracking set.

        Streams are keyed ``"<channel>:<session_id>"``, so the id is split on
        the first colon to recover the session id before cancelling.

        Raises:
            Exception: Nothing; a failing cancel is absorbed by the manager.
        """
        self._running = False
        for sid in list(self._active_streams.keys()):
            self.cancel_session(sid.split(":", 1)[1] if ":" in sid else sid)
        self._active_streams.clear()
