#!/usr/bin/env python3

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

The bus also carries the arbitrated named-phase hook semantics
(:meth:`EventSourcedBus.on` / :meth:`EventSourcedBus.emit`).  Handlers run in
``(priority, registration order)`` order under a per-handler timeout; builtin
handlers (``source="builtin"``) are only logged on failure, while plugin
handlers trip a circuit breaker and are disabled after repeated consecutive
failures.  Dict results are merged in run order (later keys win, conflicting
values are logged and published); a handler returning ``{"block": True}``
short-circuits the phase and its dict becomes the emit result.

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
import contextlib
import functools
import inspect
import json
import secrets
import sys
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from encre.contracts.version import EVENT_SCHEMA_VERSION
from encre.logging_config import get_logger
from encre.secure_io import append_jsonl, read_jsonl

logger = get_logger(__name__)

__all__ = [
    "KIND_CONFIG_RELOADED",
    "PHASE_PRE_MODEL_REQUEST",
    "PHASE_SESSION_END",
    "PHASE_SESSION_START",
    "PHASE_TURN_END",
    "PHASE_TURN_START",
    "EventSourcedBus",
    "HandlerCircuitBreaker",
    "LedgerEvent",
]

# Well-known phase names (open set; collaborators may register more).  They are
# owned by the append-only backbone, which is the single source of phase
# lifecycle semantics.
PHASE_PRE_MODEL_REQUEST = "pre_model_request"
PHASE_TURN_START = "turn_start"
PHASE_TURN_END = "turn_end"
PHASE_SESSION_START = "session_start"
PHASE_SESSION_END = "session_end"

# Well-known non-phase event kind.  A configuration reload is an
# administrative action taken *outside* any turn, so it is deliberately not
# spelled as a phase: phases drive the pipeline, and a reload must never be
# mistaken for one (nor counted as a turn by :meth:`EventSourcedBus.project`).
KIND_CONFIG_RELOADED = "config_reloaded"

#: Event kinds counted as a "turn" by :meth:`EventSourcedBus.project`.
_TURN_KINDS = frozenset({"turn_started", "turn_start", "phase:turn_start"})

#: Event kinds whose ``refs`` are collected as tool-call references.
_TOOL_CALL_KINDS = frozenset({"tool_call", "tool_call_start", "tool_calls"})

#: In-memory event window when the ledger is persisted.  Old events are
#: evicted from RAM (they remain on disk for replay); keeps long sessions
#: from growing linearly in memory.
_MEMORY_WINDOW = 4096

#: In-memory retention budget, in approximate bytes, when the ledger is
#: persisted.  An event-COUNT cap cannot bound memory on its own: the load of
#: one event is arbitrary, and the snapshots the state manager mirrors here
#: (``state_changed`` carries a bounded tail of the accumulator lists) run to
#: tens of kilobytes each, so 4096 events could pin well over a hundred
#: megabytes.  Events are evicted oldest-first as soon as either cap is
#: exceeded -- an evicted event is still on disk, exactly like one evicted by
#: the count cap.
_MEMORY_WINDOW_BYTES = 8 * 1024 * 1024

#: Depth and node guard for :func:`_payload_weight`.  Both exist so that
#: weighing a payload can never itself become the expensive operation the
#: bound is meant to prevent.
_WEIGHT_MAX_DEPTH = 8
_WEIGHT_NODE_GUARD = 20000


def _payload_weight(value: Any) -> int:
    """Approximate the resident size of an event payload, in bytes.

    Cheap alternative to serialising the payload: walking it with
    ``sys.getsizeof`` costs a fraction of the ``json.dumps`` the persistence
    path already performs, and is only ever used to decide eviction.  The
    walk is depth- and node-bounded, so a pathological payload degrades to an
    under-estimate rather than a hang; an under-estimate only delays eviction.

    Args:
        value: The payload to weigh.

    Returns:
        An approximate byte weight (``>= 0``).
    """
    total = 0
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, _WEIGHT_MAX_DEPTH)]
    while stack:
        item, depth = stack.pop()
        total += sys.getsizeof(item)
        nodes += 1
        if nodes >= _WEIGHT_NODE_GUARD or depth <= 0:
            if nodes >= _WEIGHT_NODE_GUARD:
                break
            continue
        if type(item) is dict:
            for k, v in item.items():
                total += sys.getsizeof(k)
                stack.append((v, depth - 1))
        elif type(item) in (list, tuple, set, frozenset):
            for v in item:
                stack.append((v, depth - 1))
    return total


def _serialise(value: Any) -> Any:
    """Return a JSON-serialisable version of ``value``.

    Containers are walked recursively; any leaf that does not survive
    ``json.dumps`` (objects, ``set``, ...) is degraded to its ``repr`` so the
    ledger can always be persisted without raising.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(k): _serialise(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
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
        trace_id: Correlation id shared by every event of one causal chain.
            Empty for events created before the field existed.
        turn: Turn number within the session; ``0`` when not turn-scoped.
        schema_version: Envelope schema version, so an older reader can
            migrate a newer record instead of guessing.
    """

    seq: int
    ts: float
    kind: str
    session_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    refs: list[str] = field(default_factory=list)
    event_id: str = field(default_factory=lambda: secrets.token_hex(8))
    trace_id: str = ""
    turn: int = 0
    schema_version: int = EVENT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation of this event."""
        return {
            "schema_version": self.schema_version,
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "session_id": self.session_id,
            "payload": _serialise(self.payload),
            "refs": list(self.refs),
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "turn": self.turn,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEvent:
        """Build an event from a dict produced by :meth:`to_dict`.

        Records written by older harness versions have no ``trace_id``,
        ``turn``, or ``schema_version``; they are filled with defaults.
        """
        return cls(
            seq=int(data["seq"]),
            ts=float(data.get("ts", 0.0)),
            kind=str(data.get("kind", "")),
            session_id=str(data.get("session_id", "")),
            payload=dict(data.get("payload", {}) or {}),
            refs=[str(r) for r in (data.get("refs") or [])],
            event_id=str(data.get("event_id", "")),
            trace_id=str(data.get("trace_id", "") or ""),
            turn=int(data.get("turn", 0) or 0),
            schema_version=int(data.get("schema_version", 1) or 1),
        )

    @classmethod
    def from_envelope(cls, envelope: Any) -> LedgerEvent:
        """Build an event from a :class:`encre.contracts.events.EventEnvelope`.

        The conversion lives here, not in ``contracts``, so the wire contract
        stays free of runtime imports.
        """
        return cls.from_dict(envelope.to_dict())


PhaseHandler = Callable[..., Awaitable[Any]]
Observer = Callable[["LedgerEvent"], Any]

#: Consecutive failures after which a plugin phase handler is disabled.
#: Builtin handlers are never disabled by this threshold -- they are only
#: logged at ERROR level, so the built-in arbitration rights always stay with
#: the built-in code path.
_HANDLER_FAILURE_THRESHOLD = 3


@dataclass
class _HandlerEntry:
    """A registered phase handler plus its arbitration metadata.

    Args:
        handler: The async callable invoked as ``handler(**payload)``.
        priority: Lower runs earlier (default ``100``); ties broken by
            :attr:`order` (registration order).
        timeout: Per-handler wall-clock budget in seconds; a handler exceeding
            it counts as a failure but cannot stall the phase.
        name: Optional stable label for logs and audit views.
        source: ``"builtin"`` (never disabled) or ``"plugin:<name>"``.
        order: Registration sequence number, assigned by the bus.
        failures: Consecutive failure count (reset on success).
        disabled: When ``True`` the handler is skipped at dispatch time.
        disabled_reason: Why the handler was disabled (audit trail).
    """

    handler: PhaseHandler
    priority: int = 100
    timeout: float = 5.0
    name: str = ""
    source: str = "builtin"
    order: int = 0
    failures: int = 0
    disabled: bool = False
    disabled_reason: str = ""

    @property
    def key(self) -> str:
        """Stable per-registration key used for breaker bookkeeping."""
        label = self.name or getattr(self.handler, "__qualname__", None) or repr(self.handler)
        return f"{self.source}:{label}:{self.order}"

    @property
    def label(self) -> str:
        """Human-readable handler label for logs and audit views."""
        return self.name or getattr(self.handler, "__qualname__", None) or repr(self.handler)


class HandlerCircuitBreaker:
    """Per-key circuit breaker with exponential cooldown backoff.

    Purely in-memory and driven by wall-clock time, so it stays deterministic
    in the mathematical sense: a key is allowed while closed; after
    ``failure_threshold`` consecutive failures it opens for a cooldown that
    doubles on every re-open (capped at ``max_cooldown``) and resets to the
    base on the first success.  The breaker only *gates* attempts -- it never
    records what the handler returned, and it never decides by vocabulary or
    keyword, only by counted outcomes.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        max_cooldown: float = 300.0,
    ) -> None:
        self._threshold = max(1, int(failure_threshold))
        self._base_cooldown = max(0.0, float(cooldown_seconds))
        self._max_cooldown = max(self._base_cooldown, float(max_cooldown))
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._cooldowns: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        """Whether an attempt on ``key`` may proceed right now."""
        until = self._open_until.get(key, 0.0)
        return time.time() >= until if until > 0.0 else True

    def record_success(self, key: str) -> None:
        """Reset the failure counters for ``key`` (breaker fully closed)."""
        self._failures.pop(key, None)
        self._open_until.pop(key, None)
        self._cooldowns.pop(key, None)

    def record_failure(self, key: str) -> bool:
        """Record a failure for ``key``.

        Returns:
            ``True`` when this failure (re-)opened the breaker, ``False``
            while the breaker is still closed for ``key``.
        """
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        if failures < self._threshold:
            return False
        cooldown = self._cooldowns.get(key, self._base_cooldown)
        self._open_until[key] = time.time() + cooldown
        self._cooldowns[key] = min(cooldown * 2.0, self._max_cooldown)
        return True

    def status(self, key: str) -> dict[str, Any]:
        """Return a serialisable breaker snapshot for audit views."""
        return {
            "failures": self._failures.get(key, 0),
            "open_until": self._open_until.get(key, 0.0),
            "cooldown": self._cooldowns.get(key, self._base_cooldown),
        }


def _observer_task_done(task: asyncio.Future[Any], *, observer: Observer, event: LedgerEvent) -> None:
    """Log the outcome of a fire-and-forget async observer."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "[ledger] async observer raised kind=%s observer=%s event_id=%s",
            event.kind,
            getattr(observer, "__qualname__", observer),
            event.event_id,
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
        # bounded window instead (by count AND by weight: see
        # :data:`_MEMORY_WINDOW_BYTES`).  Without persistence the in-memory
        # list is the only source for replay, so it stays unbounded by design.
        # Both caps are enforced by :meth:`_evict_locked` in one place so the
        # event list and its running weight can never drift apart.
        self._events: deque[LedgerEvent] = deque()
        self._total = 0
        self._pending: list[LedgerEvent] = []
        self._closed = False
        self._seq = 0
        # Resident weight of ``_events`` (see :data:`_MEMORY_WINDOW_BYTES`);
        # only maintained in the windowed (persisted) mode.
        self._events_bytes = 0
        self._event_weights: deque[int] = deque()

        # Arbitrated phase-handler registry (plugin admission layer).  Each
        # entry carries priority/timeout/source metadata; dispatch order and
        # failure isolation are decided by :meth:`emit`, never by the caller.
        self._handler_entries: dict[str, list[_HandlerEntry]] = {}
        self._handler_order = 0
        self._breaker = HandlerCircuitBreaker()
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
        return bool(self._handler_entries.get(phase))

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
                    self.name,
                    kind,
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
            if self._persist:
                w = _payload_weight(event.payload)
                self._events_bytes += w
                self._event_weights.append(w)
                self._evict_locked()

            # Batch persistence happens under the lock so the on-disk order
            # always matches seq.
            if self._persist and len(self._pending) >= self._flush_batch:
                self._flush_pending_locked()

        # Fan-out outside the lock: observers may be slow or raise, and must
        # never block other publishers.
        self._fanout(event)
        return event

    def _evict_locked(self) -> None:
        """Drop the oldest in-memory events until both caps are satisfied.

        Caller holds ``_lock``.  Enforces the :data:`_MEMORY_WINDOW` count cap
        and the :data:`_MEMORY_WINDOW_BYTES` weight cap; the newest event is
        always kept, so one payload larger than the byte budget cannot empty
        the window.  Eviction is RAM-only -- evicted events stay on disk, and
        :meth:`replay` / :meth:`project` read from disk, so nothing observable
        is lost.  The weight of an event never changes after it is recorded,
        and is tracked alongside it in :attr:`_event_weights`, so the running
        total cannot drift.
        """
        while self._events and (
            len(self._events) > _MEMORY_WINDOW or (self._events_bytes > _MEMORY_WINDOW_BYTES and len(self._events) > 1)
        ):
            self._events.popleft()
            self._events_bytes -= self._event_weights.popleft()

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
                    self.name,
                    event.kind,
                    getattr(observer, "__qualname__", observer),
                    exc_info=True,
                )
                continue
            if inspect.isawaitable(result):
                self._schedule_awaitable(result, observer, event)

    @staticmethod
    def _schedule_awaitable(awaitable: Awaitable[Any], observer: Observer, event: LedgerEvent) -> None:
        """Schedule an async observer on the running loop (fire-and-forget)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None  # type: ignore[assignment]
        if loop is None:
            logger.warning(
                "[ledger] async observer dropped -- no running loop kind=%s observer=%s event_id=%s",
                event.kind,
                getattr(observer, "__qualname__", observer),
                event.event_id,
            )
            return
        task = asyncio.ensure_future(awaitable)
        task.add_done_callback(functools.partial(_observer_task_done, observer=observer, event=event))

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
        with contextlib.suppress(ValueError):
            handlers.remove(handler)
        if not handlers:
            self._observers.pop(kind, None)

    # ------------------------------------------------------------------
    # Phase lifecycle hooks (arbitrated phase-bus semantics)
    # ------------------------------------------------------------------

    def on(
        self,
        phase: str,
        handler: PhaseHandler,
        *,
        priority: int = 100,
        timeout: float = 5.0,
        name: str = "",
        source: str = "builtin",
    ) -> None:
        """Register an async handler for ``phase`` with arbitration metadata.

        Args:
            phase: The phase name.
            handler: Async callable invoked as ``handler(**payload)``.
            priority: Lower runs earlier (default ``100``); ties are broken by
                registration order, so existing positional callers keep the
                historical registration-order semantics.
            timeout: Per-handler wall-clock budget in seconds; a handler that
                exceeds it counts as a failure and is isolated.
            name: Optional stable label for logs and audit views.
            source: ``"builtin"`` (default, never disabled) or
                ``"plugin:<name>"`` for plugin-registered handlers.
        """
        self._handler_order += 1
        entry = _HandlerEntry(
            handler=handler,
            priority=int(priority),
            timeout=max(0.1, float(timeout)),
            name=str(name),
            source=str(source) or "builtin",
            order=self._handler_order,
        )
        self._handler_entries.setdefault(phase, []).append(entry)

    def off(self, phase: str, handler: PhaseHandler) -> None:
        """Remove a previously registered phase handler (no-op when absent)."""
        entries = self._handler_entries.get(phase)
        if not entries:
            return
        kept = [entry for entry in entries if entry.handler is not handler]
        if len(kept) == len(entries):
            return
        if kept:
            self._handler_entries[phase] = kept
        else:
            self._handler_entries.pop(phase, None)

    def list_phase_handlers(self, phase: str | None = None) -> list[dict[str, Any]]:
        """Return an audit view of registered phase handlers.

        Args:
            phase: Restrict the view to one phase; ``None`` lists every phase.

        Returns:
            One dict per registration with its arbitration metadata, failure
            counters and breaker state.
        """
        phases = [phase] if phase is not None else list(self._handler_entries)
        out: list[dict[str, Any]] = []
        for p in phases:
            for entry in self._handler_entries.get(p, ()):
                item: dict[str, Any] = {
                    "phase": p,
                    "handler": entry.label,
                    "source": entry.source,
                    "priority": entry.priority,
                    "timeout": entry.timeout,
                    "failures": entry.failures,
                    "disabled": entry.disabled,
                    "disabled_reason": entry.disabled_reason,
                }
                if entry.source != "builtin":
                    item["breaker"] = self._breaker.status(entry.key)
                out.append(item)
        return out

    def _note_handler_success(self, entry: _HandlerEntry, key: str) -> None:
        """Clear failure state after a handler completed successfully."""
        entry.failures = 0
        if entry.source != "builtin":
            self._breaker.record_success(key)

    def _note_handler_failure(self, entry: _HandlerEntry, phase: str, key: str, error: Any) -> None:
        """Record a handler failure and enforce the arbitration policy.

        Builtin handlers are only logged at ERROR level -- they are never
        disabled and never gated by the breaker.  Plugin handlers accumulate
        consecutive failures; once they cross
        :data:`_HANDLER_FAILURE_THRESHOLD` they are disabled for the life of
        the bus and the disablement is published onto the ledger so the
        append-only audit trail records the arbitration decision.
        """
        entry.failures += 1
        if entry.source == "builtin":
            logger.error(
                "[ledger:%s] builtin phase handler failed phase=%s handler=%s failures=%d error=%s",
                self.name,
                phase,
                entry.label,
                entry.failures,
                error,
            )
            return
        breaker_opened = self._breaker.record_failure(key)
        if entry.failures >= _HANDLER_FAILURE_THRESHOLD:
            entry.disabled = True
            entry.disabled_reason = f"{entry.failures} consecutive failures: {error!s:.200}"
            logger.error(
                "[ledger:%s] plugin phase handler disabled phase=%s handler=%s source=%s failures=%d error=%s",
                self.name,
                phase,
                entry.label,
                entry.source,
                entry.failures,
                error,
            )
            self.publish(
                "handler_disabled",
                payload={
                    "phase": phase,
                    "handler": entry.label,
                    "source": entry.source,
                    "failures": entry.failures,
                    "reason": str(error)[:200],
                    "breaker_opened": breaker_opened,
                },
            )
        else:
            logger.warning(
                "[ledger:%s] plugin phase handler failed phase=%s handler=%s source=%s failures=%d error=%s",
                self.name,
                phase,
                entry.label,
                entry.source,
                entry.failures,
                error,
            )

    def _merge_handler_result(
        self,
        merged: dict[str, Any],
        result: dict[str, Any],
        phase: str,
        entry: _HandlerEntry,
    ) -> dict[str, Any]:
        """Merge one dict handler result into the running merge.

        Later handlers override earlier keys; when the same key is written
        twice with different values the conflict is logged and published so
        the arbitration outcome stays auditable.
        """
        for k, v in result.items():
            if k in merged and merged[k] != v:
                logger.warning(
                    "[ledger:%s] phase handler conflict phase=%s key=%s winner=%s",
                    self.name,
                    phase,
                    k,
                    entry.label,
                )
                self.publish(
                    "handler_conflict",
                    payload={
                        "phase": phase,
                        "key": str(k),
                        "winner": entry.label,
                        "source": entry.source,
                    },
                )
            merged[k] = v
        return merged

    async def emit(
        self,
        phase: str,
        *,
        ledger_payload: dict[str, Any] | None = None,
        **payload: Any,
    ) -> Any:
        """Dispatch ``payload`` to every handler registered for ``phase``.

        The phase is also recorded on the ledger as a ``"phase:PHASE"`` event.
        Handlers run in ``(priority, registration order)`` order, each under
        its own timeout.  A raising or timing-out handler is isolated and
        counted; plugin handlers that fail repeatedly are disabled (builtin
        handlers are only logged).  Dict results are merged in run order
        (later keys win, conflicts logged); any handler returning
        ``{"block": True}`` short-circuits the phase and its dict becomes the
        emit result.  Non-dict results keep the historical "last non-None
        wins" semantics.

        Args:
            phase: The phase name.
            ledger_payload: When given, recorded on the ledger **instead of**
                the dispatched payload.  The ledger is append-only, retained
                in an in-memory window and persisted to disk, so anything it
                references is pinned for the life of the window and rewritten
                on every flush.  Hook payloads that carry live, per-turn loop
                state -- the conversation message list above all -- must
                therefore be replaced here by a bounded summary; recording the
                live object pins a full transcript snapshot per turn (O(n^2)
                memory for an n-turn run) and re-serialises the whole
                conversation to the ledger file on every turn.
            **payload: Keyword payload forwarded to every handler.

        Returns:
            The merged dict result, the block dict when the phase was
            short-circuited, the last non-``None`` non-dict handler result,
            or ``None`` (also ``None`` when the bus is closed).
        """
        if self._closed:
            logger.warning("[ledger:%s] emit ignored -- bus is closed phase=%s", self.name, phase)
            return None
        self.publish(
            f"phase:{phase}",
            payload=payload if ledger_payload is None else ledger_payload,
        )
        entries = sorted(
            (e for e in self._handler_entries.get(phase, ()) if not e.disabled),
            key=lambda e: (e.priority, e.order),
        )
        merged: dict[str, Any] | None = None
        last_result: Any = None
        for entry in entries:
            key = entry.key
            if entry.source != "builtin" and not self._breaker.allow(key):
                continue
            try:
                handler_result = await asyncio.wait_for(entry.handler(**payload), timeout=entry.timeout)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self._note_handler_failure(entry, phase, key, "timeout")
                continue
            except Exception as exc:
                self._note_handler_failure(entry, phase, key, exc)
                continue
            self._note_handler_success(entry, key)
            if isinstance(handler_result, dict):
                if handler_result.get("block") is True:
                    self.publish(
                        "handler_blocked",
                        payload={
                            "phase": phase,
                            "handler": entry.label,
                            "source": entry.source,
                        },
                    )
                    return handler_result
                merged = self._merge_handler_result(merged if merged is not None else {}, handler_result, phase, entry)
            elif handler_result is not None:
                last_result = handler_result
        return merged if merged is not None else last_result

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
                    self.name,
                    event.seq,
                    event.kind,
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
