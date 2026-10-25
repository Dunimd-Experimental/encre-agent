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

"""Loop runtime state machine with a persistent, crash-recoverable event log.

This module answers a single question: *what is the loop currently waiting
on?*  It is deliberately orthogonal to the per-run ephemeral
:class:`encre.compact.loop_state.state.LoopTurnState` dataclass (turn counters,
retry budgets, ...) and to the PlanModeManager's *mode configuration*
(plan/spec are user settings, not runtime states).  The runtime states here
are:

* ``idle``                 -- no run in flight.
* ``running``              -- a turn is driving the model/tools pipeline.
* ``waiting_permission``   -- a tool is blocked on a user permission decision.
* ``waiting_question``     -- the question tool is blocked on user answers.
* ``waiting_plan_review``  -- a plan proposal is awaiting user review.
* ``compacting``           -- a context compaction is running synchronously.
* ``stopped``              -- the session was terminated (terminal).

Every transition is appended as one JSONL line to
``<session_dir>/events.jsonl`` (the same file the trajectory recorder uses;
event types are namespaced so replay can filter).  On restart the last state
event is replayed to detect a crash mid-wait (e.g. stuck in
``waiting_permission`` when the process died) so the loop can log the
recovery and resume cleanly.

Concurrency / atomicity design note
-----------------------------------

The chosen append strategy is the *minimal* one: ``os.open(..., O_APPEND)``
plus a single ``os.write`` of one bounded JSON line, with corrupt-tail
tolerance on read.  Reasons:

* On POSIX, ``O_APPEND`` makes small writes atomic; on Windows, Python maps
  ``O_APPEND`` to ``FILE_APPEND_DATA`` writes that the OS serialises per
  handle.  Records are kept small (payload previews are truncated), well
  below any pipe-buffer threshold, so a torn line is practically impossible
  in-process and vanishingly rare cross-process.
* ``msvcrt.locking`` was rejected: byte-range locks on append handles are
  fragile across CPython releases, deadlock-prone with nested writers, and
  non-portable to POSIX without a second implementation.
* A separate lock *file* was rejected for the same portability reason plus
  stale-lock cleanup complexity.
* Reads always tolerate a damaged tail (partial last line from a hard
  crash): corrupt lines are skipped, so recovery only ever replays healthy
  prefixes.

Sequence numbers are re-synced from the file tail before each append (under
a process-local lock), so multiple writers to the same file -- the
trajectory recorder shares it -- never permanently desynchronise the
counter, and a restart continues the existing numbering instead of
resetting it.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger
from encre.policy_version import policy_versions_stamped

logger = get_logger(__name__)

#: Policy version stamp written as a top-level field next to ``seq``/``ts``
#: on every event line (Task 8.1).  Computed once per process.
_POLICY_VERSIONS_STAMP = policy_versions_stamped()

__all__ = [
    "LoopState",
    "LoopStateMachine",
    "LoopTransitionError",
    "StateEventLog",
]


class LoopState(str, Enum):
    """Runtime states of the agent loop ("what is the loop waiting on?")."""

    IDLE = "idle"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    WAITING_QUESTION = "waiting_question"
    WAITING_PLAN_REVIEW = "waiting_plan_review"
    COMPACTING = "compacting"
    STOPPED = "stopped"

    def __str__(self) -> str:  # serialise as the plain value everywhere
        return self.value


# Allowed transitions.  ``* -> STOPPED`` is handled separately (always
# forced).  The table mirrors the real control flow of the loop:
#
# * idle -> running                 a run()/turn starts.
# * running -> waiting_*            a tool hits a permission/question/plan gate.
# * running -> compacting           a synchronous compaction starts.
# * waiting_* / compacting -> running
#                                   the gate resolved / compact finished.
# * waiting_* / compacting / running -> idle
#                                   the run ended (normally, cancelled, or
#                                   abnormally torn down); abnormal exits may
#                                   land here from a waiting state.
# * stopped -> running              a terminated session loop object being
#                                   explicitly revived by a brand-new run.
_ALLOWED_TRANSITIONS: dict[LoopState, frozenset[LoopState]] = {
    LoopState.IDLE: frozenset({LoopState.RUNNING}),
    LoopState.RUNNING: frozenset(
        {
            LoopState.WAITING_PERMISSION,
            LoopState.WAITING_QUESTION,
            LoopState.WAITING_PLAN_REVIEW,
            LoopState.COMPACTING,
            LoopState.IDLE,
        }
    ),
    LoopState.WAITING_PERMISSION: frozenset({LoopState.RUNNING, LoopState.IDLE}),
    LoopState.WAITING_QUESTION: frozenset({LoopState.RUNNING, LoopState.IDLE}),
    LoopState.WAITING_PLAN_REVIEW: frozenset({LoopState.RUNNING, LoopState.IDLE}),
    LoopState.COMPACTING: frozenset({LoopState.RUNNING, LoopState.IDLE}),
    LoopState.STOPPED: frozenset({LoopState.RUNNING}),
}

#: Event type used for ordinary state transitions.
STATE_EVENT_TYPE = "state"
#: Event type written when a stale post-crash state is recovered.
STATE_RECOVERED_EVENT_TYPE = "state_recovered"
#: Event types that carry loop-runtime state semantics (used by rebuild).
_STATE_EVENT_TYPES = frozenset({STATE_EVENT_TYPE, STATE_RECOVERED_EVENT_TYPE})

#: Phase journal: one event per pipeline phase that ran to completion
#: within a turn (payload: ``{"turn": int, "phase": str}``).
PHASE_COMPLETED_EVENT_TYPE = "phase_completed"
#: Phase journal: closes a turn whose whole pipeline unwound
#: (payload: ``{"turn": int}``).
TURN_COMPLETED_EVENT_TYPE = "turn_completed"
#: Phase journal: written on restart when the previous process left a
#: turn open (see :meth:`StateEventLog.last_incomplete_turn`).
TURN_INTERRUPTED_EVENT_TYPE = "turn_interrupted"

#: Maximum characters kept of any serialised payload / preview.  Small lines
#: are the atomicity argument (see module docstring).
_PAYLOAD_LIMIT = 2000
#: How much of the file tail to scan when re-syncing the sequence counter.
_TAIL_SCAN_BYTES = 65536


class LoopTransitionError(RuntimeError):
    """Raised when a state transition is not in the allowed table."""


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO8601 string."""
    return datetime.now(UTC).isoformat()


def _state_value(state: Any) -> str:
    """Normalise a state-like value to its string form."""
    if state is None:
        return ""
    return str(getattr(state, "value", state))


def _bounded(value: Any) -> Any:
    """Bound a payload to keep every event line small."""
    if value is None or isinstance(value, int | float | bool):
        return value
    try:
        text = (
            value
            if isinstance(value, str)
            else json.dumps(
                value,
                ensure_ascii=False,
                default=str,
            )
        )
    except Exception:
        text = str(value)
    if len(text) <= _PAYLOAD_LIMIT:
        return text
    return text[:_PAYLOAD_LIMIT] + "...[truncated]"


class StateEventLog:
    """Append-only JSONL event log bound to one session directory.

    Stores runtime state transitions (and related loop events such as
    steering-inbox activity) as
    ``{seq, ts, policy_versions, type, from, to, payload}`` lines in
    ``<session_dir>/events.jsonl``.  Sequence numbers continue from the
    existing file content so restarts append rather than reset.
    """

    def __init__(self, session_dir: str | Path) -> None:
        self._path = Path(session_dir) / "events.jsonl"
        self._lock = threading.Lock()
        self._seq = 0
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._seq = self._scan_max_seq()
        except Exception:
            logger.debug("[loop-state] event store init failed", exc_info=True)

    @property
    def path(self) -> Path:
        """Path of the underlying ``events.jsonl`` file."""
        return self._path

    # -- Append ------------------------------------------------------------

    def append(
        self,
        event_type: str,
        from_state: Any = None,
        to_state: Any = None,
        payload: Any = None,
    ) -> dict[str, Any] | None:
        """Append one event line; returns the record or ``None`` on failure.

        Recording must never break the loop, so I/O errors are logged and
        swallowed.  The sequence number is re-synced from the file tail
        before each append so co-located writers (the trajectory recorder
        shares this file) cannot desynchronise the counter.
        """
        try:
            with self._lock:
                self._seq = max(self._seq, self._scan_max_seq())
                record: dict[str, Any] = {
                    "seq": self._seq + 1,
                    "ts": _utc_now_iso(),
                    "policy_versions": _POLICY_VERSIONS_STAMP,
                    "type": event_type,
                    "from": _state_value(from_state),
                    "to": _state_value(to_state),
                    "payload": _bounded(payload),
                }
                line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
                data = line.encode("utf-8")
                try:
                    fd = os.open(
                        self._path,
                        os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                        0o644,
                    )
                except FileNotFoundError:
                    # The session directory vanished mid-run (e.g. the session
                    # was deleted while the loop is still finishing its turn).
                    # Re-create the directory and retry once so recording
                    # keeps working instead of silently dropping events.
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(
                        self._path,
                        os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                        0o644,
                    )
                try:
                    # Single bounded write on an append handle (see module
                    # docstring); the loop below is purely defensive against
                    # short writes on regular files.
                    view = memoryview(data)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                finally:
                    os.close(fd)
                self._seq += 1
                return record
        except Exception:
            logger.debug("[loop-state] append failed for %s", event_type, exc_info=True)
            return None

    # -- Replay / recovery ---------------------------------------------------

    def replay(self, target_seq: int | None = None) -> list[dict[str, Any]]:
        """Return all events, skipping corrupt lines (damaged crash tails).

        Args:
            target_seq: When given, only events with ``seq <= target_seq``
                are returned (inclusive truncation).

        Returns:
            The list of parsed event dicts in file order.
        """
        events: list[dict[str, Any]] = []
        try:
            with self._path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if target_seq is not None:
                        seq = event.get("seq")
                        if not isinstance(seq, int) or seq > target_seq:
                            continue
                    events.append(event)
        except FileNotFoundError:
            return []
        except Exception:
            logger.debug("[loop-state] replay failed", exc_info=True)
        return events

    def last_state_event(self) -> dict[str, Any] | None:
        """Return the most recent state/recovered event, or ``None``."""
        last: dict[str, Any] | None = None
        for event in self.replay():
            if event.get("type") in _STATE_EVENT_TYPES:
                last = event
        return last

    def rebuild_state(self) -> LoopState:
        """Derive the current :class:`LoopState` from the persisted events.

        Defaults to :attr:`LoopState.IDLE` when no state event exists or the
        stored value is not a known state.
        """
        last = self.last_state_event()
        if last is None:
            return LoopState.IDLE
        try:
            return LoopState(str(last.get("to") or ""))
        except ValueError:
            return LoopState.IDLE

    def last_incomplete_turn(self) -> dict[str, Any] | None:
        """Detect a turn the previous process never finished.

        Compares the last ``phase_completed`` journal event (P) with the
        last ``turn_completed`` event (C): when P exists and its turn
        number is greater than C's (or C is absent entirely), the previous
        process completed at least one phase of a turn without the
        pipeline ever unwinding -- i.e. it died mid-turn.

        Returns:
            ``{"turn", "last_phase", "phases_completed"}`` describing the
            interrupted turn, or ``None`` when every journalled turn is
            properly closed.
        """
        last_phase: dict[str, Any] | None = None
        last_phase_turn = -1
        completed_this_turn = 0
        last_closed_turn = -1
        for event in self.replay():
            etype = event.get("type")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            turn = payload.get("turn")
            if not isinstance(turn, int):
                continue
            if etype == PHASE_COMPLETED_EVENT_TYPE:
                if turn > last_phase_turn:
                    last_phase = payload
                    last_phase_turn = turn
                    completed_this_turn = 1
                elif turn == last_phase_turn:
                    completed_this_turn += 1
            elif etype == TURN_COMPLETED_EVENT_TYPE and turn > last_closed_turn:
                last_closed_turn = turn
        if last_phase is None or last_closed_turn >= last_phase_turn:
            return None
        return {
            "turn": last_phase_turn,
            "last_phase": last_phase.get("phase"),
            "phases_completed": completed_this_turn,
        }

    # -- Internals -----------------------------------------------------------

    def _scan_max_seq(self) -> int:
        """Scan the file tail for the highest sequence number.

        Only the last ``_TAIL_SCAN_BYTES`` bytes are read: appends are
        strictly increasing, so the maximum always lives in the tail.  Lines
        that fail to parse (corrupt tail) are skipped.
        """
        try:
            size = self._path.stat().st_size
        except OSError:
            return 0
        if size == 0:
            return 0
        max_seq = 0
        try:
            with self._path.open("rb") as f:
                if size > _TAIL_SCAN_BYTES:
                    f.seek(size - _TAIL_SCAN_BYTES)
                    f.readline()  # discard the (possibly partial) first line
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        seq = int(json.loads(raw).get("seq", 0))
                    except Exception:
                        continue
                    if seq > max_seq:
                        max_seq = seq
        except OSError:
            return max_seq
        return max_seq


class LoopStateMachine:
    """Validated transition machine over :class:`LoopState` + :class:`StateEventLog`.

    The machine is the single authority for the loop's runtime state.  Every
    accepted transition is persisted; illegal transitions raise
    :class:`LoopTransitionError` (callers that must never crash the agent
    loop wrap this via the loop's ``_transition_loop`` helper, which logs
    instead of propagating).  ``* -> STOPPED`` is always forced.
    """

    def __init__(self, session_dir: str | Path) -> None:
        self._store = StateEventLog(session_dir)
        # Start from the persisted state so a restart sees the pre-crash
        # state until :meth:`recover_from_crash` reconciles it.
        self._state: LoopState = self._store.rebuild_state()
        self._lock = threading.Lock()

    @property
    def state(self) -> LoopState:
        """The current runtime state."""
        return self._state

    @property
    def store(self) -> StateEventLog:
        """The underlying event store (for replay/inspection)."""
        return self._store

    def transition(self, new_state: LoopState, payload: Any = None) -> LoopState:
        """Transition to ``new_state``, validating against the table.

        Same-state transitions are idempotent no-ops (no event).  Illegal
        transitions raise :class:`LoopTransitionError` after being logged.

        Args:
            new_state: The target runtime state.
            payload: Optional JSON-serialisable context for the event.

        Returns:
            The (possibly unchanged) current state.

        Raises:
            LoopTransitionError: If the transition is not allowed.
        """
        with self._lock:
            current = self._state
            if new_state is current:
                return current
            if new_state not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
                exc = LoopTransitionError(f"illegal loop transition {current.value} -> {new_state.value}")
                logger.warning("[loop-state] %s", exc)
                raise exc
            self._store.append(STATE_EVENT_TYPE, current, new_state, payload)
            self._state = new_state
            return new_state

    def mark_stopped(self, payload: Any = None) -> LoopState:
        """Force the terminal ``stopped`` state (``* -> stopped`` always legal).

        Only for real session termination; ordinary run endings go back to
        ``idle`` via :meth:`transition`.

        Args:
            payload: Optional context for the event.

        Returns:
            The terminal state.
        """
        with self._lock:
            current = self._state
            if current is LoopState.STOPPED:
                return current
            self._store.append(STATE_EVENT_TYPE, current, LoopState.STOPPED, payload)
            self._state = LoopState.STOPPED
            return LoopState.STOPPED

    def recover_from_crash(self) -> dict[str, Any] | None:
        """Reconcile a stale pre-crash state; returns recovery info or ``None``.

        If the rebuilt state is a *waiting* state or ``compacting``, the
        previous process died mid-gate: log it, persist a
        ``state_recovered`` event, and resume at ``running``.  Recovery does
        NOT skip any approval gate -- the live permission/question/plan flow
        naturally re-asks on the next tool execution.  ``compacting`` is
        idempotent and simply re-runs on the next trigger.
        """
        with self._lock:
            stale = self._state
            if stale not in (
                LoopState.WAITING_PERMISSION,
                LoopState.WAITING_QUESTION,
                LoopState.WAITING_PLAN_REVIEW,
                LoopState.COMPACTING,
            ):
                return None
            self._store.append(
                STATE_RECOVERED_EVENT_TYPE,
                stale,
                LoopState.RUNNING,
                {"reason": "stale state from a previous process"},
            )
            self._state = LoopState.RUNNING
            logger.warning(
                "[loop-state] recovered stale state '%s' after restart -> running",
                stale.value,
            )
            return {"from": stale.value, "to": LoopState.RUNNING.value}

    def record(self, event_type: str, payload: Any = None) -> None:
        """Append a non-transition event (e.g. ``inbox_enqueue``/``inbox_drain``).

        The event carries the current state as both ``from`` and ``to`` so
        replay can correlate it with the surrounding transitions.

        Args:
            event_type: Namespaced event type string.
            payload: Optional JSON-serialisable context.
        """
        self._store.append(event_type, self._state, self._state, payload)
