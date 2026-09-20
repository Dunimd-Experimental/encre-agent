#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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

"""Append-only event ledger and event-sourced bus.

This module provides the single, system-wide *event backbone* for Encre.  Every
notable thing that happens inside the agent -- a lifecycle phase firing, a turn
starting, a compaction state change, a tool call -- can be written as a
:class:`LedgerEvent` onto an :class:`EventSourcedBus` and be:

* **Persisted.**  When a ``ledger_path`` is supplied, the bus appends each
  event (in ``seq`` order) to an encrypted JSONL file via
  :mod:`encre.secure_io`, so the full history survives a crash.
* **Replayed.**  :meth:`EventSourcedBus.replay` reads the persisted records
  back off disk in ascending ``seq`` order, which lets long-lived sessions be
  reconstructed after a restart.
* **Projected.**  :meth:`EventSourcedBus.project` folds the event stream into a
  small read-model snapshot queryable without guesswork.
* **Observed.**  :meth:`EventSourcedBus.subscribe` fans each published event
  out to sync or async observers without the publisher ever being blocked.

The bus also carries the historical named-phase hook semantics
(:meth:`EventSourcedBus.on` / :meth:`EventSourcedBus.emit`) so the
``pre_model_request`` hook behaviour is carried forward unchanged: handlers run
in registration order, a raising handler is isolated, and ``emit`` returns the
last non-``None`` handler result.

Design invariants
=================
* **Append-only.**  Events are never removed from the in-memory list or the
  on-disk ledger; ``seq`` is monotonic across the entire bus.
* **Write ordering.**  Publishing and flushing are guarded by a threading lock,
  so multi-threaded publishers cannot reorder records on disk.
* **Observer failures are isolated.**  A raising observer never takes down the
  publisher or any other observer; the error is logged and the fan-out
  continues.
* **Replay is side-effect free.**  ``replay`` only reads disk and builds
  objects; it never runs observers and never publishes a new event.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import secrets
from collections import deque
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger
from encre.secure_io import append_jsonl, read_jsonl

logger = get_logger(__name__)

__all__ = [
    "EventSourcedBus",
    "LedgerEvent",
    "PHASE_PRE_MODEL_REQUEST",
    "PHASE_SESSION_END",
    "PHASE_SESSION_START",
    "PHASE_TURN_END",
    "PHASE_TURN_START",
]

# Well-known phase names (open set; collaborators may register more).  They are
# owned by the append-only backbone, which is the single source of phase
# lifecycle semantics.
PHASE_PRE_MODEL_REQUEST = "pre_model_request"
PHASE_TURN_START = "turn_start"
PHASE_TURN_END = "turn_end"
PHASE_SESSION_START = "session_start"
PHASE_SESSION_END = "session_end"

#: Event kinds counted as a "turn" by :meth:`EventSourcedBus.project`.
_TURN_KINDS = frozenset({"turn_started", "turn_start", "phase:turn_start"})

#: Event kinds whose ``refs`` are collected as tool-call references.
_TOOL_CALL_KINDS = frozenset({"tool_call", "tool_call_start", "tool_calls"})

#: In-memory event window when the ledger is persisted.  Old events are
#: evicted from RAM (they remain on disk for replay); keeps long sessions
#: from growing linearly in memory.
_MEMORY_WINDOW = 4096


def _serialise(value: Any) -> Any:
    """Return a JSON-serialisable version of ``value``.

    Containers are walked recursively; any leaf that does not survive
    ``json.dumps`` (objects, ``set``, ...) is degraded to its ``repr`` so the
    ledger can always be persisted without raising.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _serialise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialise(v) for v in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError, RecursionError):
        return repr(value)
    return value


@dataclass
class LedgerEvent:
    """A single append-only event in the ledger.

    Args:
        seq: Monotonic sequence number, unique across the whole bus and
            starting at ``1``.
        ts: Wall-clock timestamp of the event (``time.time()``).
        kind: Event type, e.g. ``"phase:pre_model_request"``,
            ``"turn_started"`` or ``"state_changed"``.
        session_id: The session the event belongs to (may be ``""``).
        payload: Arbitrary event load.  Non-JSON-serialisable values are
            downgraded to their ``repr`` on :meth:`to_dict` so persistence
            never raises.
        refs: Related references (tool call ids, memory ids, prior event ids).
        event_id: Unique id (``secrets.token_hex(8)``).
    """

    seq: int
    ts: float
    kind: str
    session_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    refs: list[str] = field(default_factory=list)
    event_id: str = field(default_factory=lambda: secrets.token_hex(8))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation of this event."""
        return {
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "session_id": self.session_id,
            "payload": _serialise(self.payload),
            "refs": list(self.refs),
            "event_id": self.event_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LedgerEvent":
        """Build an event from a dict produced by :meth:`to_dict`."""
        return cls(
            seq=int(data["seq"]),
            ts=float(data.get("ts", 0.0)),
            kind=str(data.get("kind", "")),
            session_id=str(data.get("session_id", "")),
            payload=dict(data.get("payload", {}) or {}),
            refs=[str(r) for r in (data.get("refs") or [])],
            event_id=str(data.get("event_id", "")),
        )


PhaseHandler = Callable[..., Awaitable[Any]]
Observer = Callable[["LedgerEvent"], Any]


def _observer_task_done(
    task: "asyncio.Future[Any]", *, observer: Observer, event: LedgerEvent
) -> None:
    """Log the outcome of a fire-and-forget async observer."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "[ledger] async observer raised kind=%s observer=%s event_id=%s",
            event.kind, getattr(observer, "__qualname__", observer), event.event_id,
            exc_info=exc,
        )


class EventSourcedBus:
    """The system-wide append-only event backbone.

    Args:
        name: A human-readable bus name, used in logs.
        ledger_path: When given, events are appended to this encrypted JSONL
            ledger as they are flushed.  When ``None`` the bus runs purely in
            memory (test-friendly) and nothing touches disk.
        persist: When ``False`` no persistence is performed even if a
            ``ledger_path`` is supplied.  Defaults to ``True``.
        flush_batch: Number of events that must accumulate before they are
            written to disk in one batch.  ``1`` (the default) is write
            through; any positive value batches and :meth:`flush` always writes
            out the remaining events in ``seq`` order.

    ``close()`` semantics: once closed, publishing is refused.  A call to
    :meth:`publish` (or :meth:`emit`) on a closed bus logs a warning and
    returns ``None``; the event is neither appended nor fanned out.  Replay and
    projection keep working against the persisted ledger.
    """

    def __init__(
        self,
        name: str = "events",
        *,
        ledger_path: str | Path | None = None,
        persist: bool = True,
        flush_batch: int = 1,
    ) -> None:
        self.name = name
        self._ledger_path = Path(ledger_path) if ledger_path is not None else None
        self._persist = bool(persist) and self._ledger_path is not None
        self._flush_batch = max(1, int(flush_batch))
        if self._persist:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        # In-memory retention.  When the ledger is persisted, replay reads
        # from disk, so keeping every event in RAM would make a 12h session's
        # memory grow linearly with event count -- retention is capped to a
        # bounded window instead.  Without persistence the in-memory list is
        # the only source for replay, so it stays unbounded by design.
        self._events: deque[LedgerEvent] = deque(
            maxlen=_MEMORY_WINDOW if self._persist else None
        )
        self._total = 0
        self._pending: list[LedgerEvent] = []
        self._closed = False
        self._seq = 0

        # Phase-hook handler registry (legacy phase-bus semantics).
        self._handlers: dict[str, list[PhaseHandler]] = {}
        # Observers keyed by kind; "*" means subscribe to everything.
        self._observers: dict[str, list[Observer]] = {}

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has been called."""
        return self._closed

    @property
    def count(self) -> int:
        """Number of events accepted so far (persisted or pending)."""
        return self._total

    def has_listeners(self, phase: str) -> bool:
        """Whether any phase-style handler is registered for ``phase``."""
        return bool(self._handlers.get(phase))

    # ------------------------------------------------------------------
    # Publishing core
    # ------------------------------------------------------------------

    def publish(
        self,
        kind: str,
        *,
        session_id: str = "",
        payload: dict[str, Any] | None = None,
        refs: list[str] | None = None,
    ) -> LedgerEvent | None:
        """Record, persist and fan out a new event.

        Args:
            kind: The event type (e.g. ``"turn_started"``).
            session_id: The owning session (may be ``""``).
            payload: Event load; non-serialisable values are downgraded to
                their ``repr`` so persistence never raises.
            refs: Related references.

        Returns:
            The created :class:`LedgerEvent`, or ``None`` if the bus is closed.
        """
        with self._lock:
            if self._closed:
                logger.warning(
                    "[ledger:%s] publish ignored -- bus is closed kind=%s",
                    self.name, kind,
                )
                return None
            self._seq += 1
            event = LedgerEvent(
                seq=self._seq,
                ts=time.time(),
                kind=kind,
                session_id=session_id,
                # Payload is kept as published for in-memory observers (a typed
                # object may be carried under ``"event"``); :meth:`to_dict`
                # degrades non-serialisable leaves to ``repr`` for persistence.
                payload=dict(payload or {}),
                refs=[str(r) for r in (refs or [])],
            )
            self._events.append(event)
            self._total += 1
            self._pending.append(event)

            # Batch persistence happens under the lock so the on-disk order
            # always matches seq.
            if self._persist and len(self._pending) >= self._flush_batch:
                self._flush_pending_locked()

        # Fan-out outside the lock: observers may be slow or raise, and must
        # never block other publishers.
        self._fanout(event)
        return event

    def _fanout(self, event: LedgerEvent) -> None:
        """Deliver ``event`` to every matching observer, isolating failures."""
        observers: list[Observer] = list(self._observers.get("*", ()))
        observers += list(self._observers.get(event.kind, ()))
        for observer in observers:
            try:
                result = observer(event)
            except Exception:
                logger.warning(
                    "[ledger:%s] observer failed kind=%s observer=%s",
                    self.name, event.kind, getattr(observer, "__qualname__", observer),
                    exc_info=True,
                )
                continue
            if inspect.isawaitable(result):
                self._schedule_awaitable(result, observer, event)

    @staticmethod
    def _schedule_awaitable(
        awaitable: Awaitable[Any], observer: Observer, event: LedgerEvent
    ) -> None:
        """Schedule an async observer on the running loop (fire-and-forget)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None  # type: ignore[assignment]
        if loop is None:
            logger.warning(
                "[ledger] async observer dropped -- no running loop kind=%s "
                "observer=%s event_id=%s",
                event.kind, getattr(observer, "__qualname__", observer),
                event.event_id,
            )
            return
        task = asyncio.ensure_future(awaitable)
        task.add_done_callback(
            functools.partial(_observer_task_done, observer=observer, event=event)
        )

    # ------------------------------------------------------------------
    # Observe (fire-and-forget subscriptions)
    # ------------------------------------------------------------------

    def subscribe(self, kind: str, handler: Observer) -> None:
        """Register ``handler`` as an observer of ``kind``.

        ``handler`` may be sync or async and takes a single
        :class:`LedgerEvent`.  Use ``"*"`` to observe every event.  Async
        observers are scheduled on the running loop and never awaited by the
        publisher, so a slow observer cannot block publishing.

        Args:
            kind: Event kind to observe, or ``"*"`` for all.
            handler: The observer callable.
        """
        self._observers.setdefault(kind, []).append(handler)

    def unsubscribe(self, kind: str, handler: Observer) -> None:
        """Remove a previously registered observer (no-op when absent)."""
        handlers = self._observers.get(kind)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            pass
        if not handlers:
            self._observers.pop(kind, None)

    # ------------------------------------------------------------------
    # Phase lifecycle hooks (legacy phase-bus semantics)
    # ------------------------------------------------------------------

    def on(self, phase: str, handler: PhaseHandler) -> None:
        """Register an async handler for ``phase`` (phase-hook semantics)."""
        self._handlers.setdefault(phase, []).append(handler)

    def off(self, phase: str, handler: PhaseHandler) -> None:
        """Remove a previously registered phase handler (no-op when absent)."""
        handlers = self._handlers.get(phase)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            pass
        if not handlers:
            self._handlers.pop(phase, None)

    async def emit(self, phase: str, **payload: Any) -> Any:
        """Dispatch ``payload`` to every handler registered for ``phase``.

        The phase is also recorded on the ledger as a ``"phase:PHASE"`` event.
        Handlers run in registration order; raising handlers are isolated.
        Returns the last non-``None`` handler result (phase-hook semantics).

        Args:
            phase: The phase name.
            **payload: Keyword payload forwarded to every handler.

        Returns:
            The last non-``None`` handler result, or ``None`` (also ``None``
            when the bus is closed).
        """
        if self._closed:
            logger.warning(
                "[ledger:%s] emit ignored -- bus is closed phase=%s", self.name, phase
            )
            return None
        self.publish(f"phase:{phase}", payload=payload)
        result: Any = None
        for handler in list(self._handlers.get(phase, ())):
            try:
                handler_result = await handler(**payload)
            except Exception:
                logger.warning(
                    "[ledger:%s] phase handler failed phase=%s handler=%s",
                    self.name, phase, getattr(handler, "__qualname__", handler),
                    exc_info=True,
                )
                continue
            if handler_result is not None:
                result = handler_result
        return result

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _flush_pending_locked(self) -> None:
        """Append pending events to disk one by one. Caller holds ``_lock``."""
        if not self._persist or not self._pending:
            return
        while self._pending:
            event = self._pending[0]
            try:
                append_jsonl(self._ledger_path, event.to_dict())
            except Exception:
                logger.warning(
                    "[ledger:%s] failed to persist seq=%s kind=%s",
                    self.name, event.seq, event.kind,
                    exc_info=True,
                )
                return
            self._pending.pop(0)

    def flush(self) -> None:
        """Write every not-yet-persisted event to disk in ``seq`` order.

        Safe to call on a non-persistent bus (no-op).  An event is removed from
        the pending queue only after it has been written, so a failed write is
        retried on the next flush without duplicating earlier records.
        """
        with self._lock:
            self._flush_pending_locked()

    async def aflush(self) -> None:
        """Flush the ledger without blocking the event loop."""
        await asyncio.to_thread(self.flush)

    def replay(self, session_id: str) -> list[LedgerEvent]:
        """Replay the persisted ledger for ``session_id`` from disk.

        Events are returned in ascending ``seq`` order, filtered to the given
        session.  Replay is side-effect free: it only reads disk and constructs
        objects -- it never runs observers and never publishes.

        Args:
            session_id: The session to replay.  An empty ``session_id``
                matches only events with an empty session (global events).

        Returns:
            A list of :class:`LedgerEvent` in ``seq`` order.  Empty when no
            ledger is persisted (``ledger_path`` was ``None``) or when the
            session has no records.  Events that were published but not yet
            flushed are not visible -- call :meth:`flush` first.
        """
        if self._ledger_path is None:
            return []
        events: list[LedgerEvent] = []
        seen: set[str] = set()
        for record in read_jsonl(self._ledger_path):
            if not isinstance(record, dict):
                continue
            try:
                event = LedgerEvent.from_dict(record)
            except (KeyError, TypeError, ValueError):
                logger.warning("[ledger:%s] skipping malformed record", self.name)
                continue
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            if event.session_id == session_id:
                events.append(event)
        events.sort(key=lambda e: e.seq)
        return events

    def project(self, session_id: str) -> dict[str, Any]:
        """Fold the persisted event history into a queryable read-model.

        The projection is derived purely from the replayed events: turns are
        the events whose kind is in ``{"turn_started", "turn_start",
        "phase:turn_start"}`` and ``tool_calls`` collects the ``refs`` of every
        ``{"tool_call", "tool_call_start", "tool_calls"}`` event.

        Args:
            session_id: The session to project (see :meth:`replay`).

        Returns:
            A dict with keys ``session_id``, ``events`` (count), ``last_seq``
            (highest seq, ``0`` when empty), ``kinds`` (``{kind: count}``),
            ``turns`` and ``tool_calls``.
        """
        events = self.replay(session_id)
        kinds: dict[str, int] = {}
        tool_calls: list[str] = []
        turns = 0
        last_seq = 0
        for event in events:
            kinds[event.kind] = kinds.get(event.kind, 0) + 1
            if event.seq > last_seq:
                last_seq = event.seq
            if event.kind in _TURN_KINDS:
                turns += 1
            if event.kind in _TOOL_CALL_KINDS:
                tool_calls.extend(ref for ref in event.refs if ref)
        return {
            "session_id": session_id,
            "events": len(events),
            "last_seq": last_seq,
            "kinds": kinds,
            "turns": turns,
            "tool_calls": tool_calls,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Stop accepting new events and flush any pending ones.

        After ``close()``, :meth:`publish` and :meth:`emit` log a warning and
        return ``None`` -- the event is neither recorded nor fanned out.
        :meth:`replay` and :meth:`project` keep working against the persisted
        ledger.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._flush_pending_locked()
        logger.info("[ledger:%s] closed", self.name)
