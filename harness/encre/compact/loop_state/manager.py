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

"""Unified state pipeline for the agent main loop.

Centralises all mutable loop state that was previously scattered across
``session.metadata`` key-value access.  Provides typed getters/setters,
automatic change tracking (snapshot diffs), a full change log for audit/replay,
and a ``snapshot()`` method that produces the ``AgentStateSnapshot`` dict
the frontend consumes.

Usage::

    from encre.compact.loop_state.manager import StateManager

    mgr = StateManager(session, hook_system=hook_system)
    mgr.task_stage = "plan"
    print(mgr.working_set)
    state = mgr.snapshot()          # AgentStateSnapshot dict
    history = mgr.change_log()      # list of changes
"""

import copy
import time
from collections.abc import Callable
from typing import Any

from encre.logging_config import get_logger
from encre.modes.goal import GOAL_META_KEY

logger = get_logger(__name__)

# -- Metadata key constants to avoid string drift ----------------------------

META_CHANNEL = "channel"
META_TEMP_CHAT = "temp_chat"
META_WORKSPACE = "workspace"
META_TASK_STAGE = "task_stage"
META_TASK_STAGE_HISTORY = "task_stage_history"
META_WORKING_SET = "working_set"
META_TURN_SUMMARIES = "turn_summaries"
META_TOOL_SEMANTICS = "tool_semantics"
META_STUCK_EVENTS = "stuck_events"
META_DELEGATE_HISTORY = "delegate_history"
META_USER_REQ_SUMMARY = "user_requirements_summary"
META_SLASH_COMMAND_MODE = "slash_command_mode"
META_PLAN_MODE_ACTIVE = "plan_mode_active"
#: The standing goal is owned by :mod:`encre.modes.goal`; its metadata key is
#: defined once there and reused here so the two cannot drift apart.
META_ACTIVE_GOAL = GOAL_META_KEY
META_ACTIVE_COMMAND = "active_command"
META_MILESTONE_SUMMARIES = "milestone_summaries"

# Sentinel for change detection
_UNSET: dict[str, Any] = {}

# Change-log snapshots cap.  Accumulator values such as ``turn_summaries``
# grow on every turn, so snapshotting them in full per change would make
# the change log (and its ``state_changed`` mirror on the event backbone)
# retain O(n^2) state over a long run.  Oversized containers are recorded
# as a bounded tail instead, which keeps the audit trail usable.
_SNAPSHOT_MAX_ITEMS = 32


def _snapshot(value: Any) -> Any:
    """Return a deepcopied snapshot of *value*, bounded for large containers.

    Args:
        value: The value to snapshot for the change log.

    Returns:
        A deep copy for small values; for oversized lists a bounded tail
        summary, and for oversized dicts a size-only summary.
    """
    if isinstance(value, list):
        if len(value) > _SNAPSHOT_MAX_ITEMS:
            return {
                "_snapshot": "list_tail",
                "len": len(value),
                "tail": copy.deepcopy(value[-_SNAPSHOT_MAX_ITEMS:]),
            }
        return copy.deepcopy(value)
    if isinstance(value, dict) and len(value) > _SNAPSHOT_MAX_ITEMS:
        return {"_snapshot": "dict", "len": len(value)}
    return copy.deepcopy(value)


class StateManager:
    """Unified typed state manager for EncreLoop.

    Wraps ``session.metadata`` with typed getters/setters, records every
    change in a sequenced change log (``_changes``), and can produce a
    frontend-facing snapshot via ``snapshot()``.

    Thread-compatible: all operations are synchronous and isolated to the
    asyncio loop that owns the session.
    """

    def __init__(
        self,
        session: Any,
        on_change: Callable[[str, Any, Any], None] | None = None,
    ) -> None:
        self._session = session
        self._on_change = on_change
        # Change log: list of {"key", "old", "new", "timestamp", "seq"}
        self._changes: list[dict[str, Any]] = []
        self._change_seq: int = 0
        # Run-scoped: set when the user approves a plan, cleared when the mode
        # changes or a new run starts. Never persisted.
        self._plan_gate_released: bool = False
        self._set_defaults()

    # ── Internal helpers ──────────────────────────────────────────────

    def _set_defaults(self) -> None:
        meta = self._session.metadata
        meta.setdefault(META_TASK_STAGE, "discover")
        meta.setdefault(META_TASK_STAGE_HISTORY, [])
        meta.setdefault(META_WORKING_SET, {})
        meta.setdefault(META_TURN_SUMMARIES, [])
        meta.setdefault(META_TOOL_SEMANTICS, {})
        meta.setdefault(META_STUCK_EVENTS, [])
        meta.setdefault(META_DELEGATE_HISTORY, [])

    def _get(self, key: str, default: Any = None) -> Any:
        return self._session.metadata.get(key, default)

    def _set(self, key: str, value: Any) -> None:
        old = self._session.metadata.get(key, _UNSET)
        if old is _UNSET or old != value:
            self._session.metadata[key] = value
            self._record_change(key, old if old is not _UNSET else None, value)

    def _record_change(self, key: str, old: Any, new: Any) -> None:
        self._change_seq += 1
        rec = {
            "key": key,
            "old": _snapshot(old),
            "new": _snapshot(new),
            "timestamp": time.time(),
            "seq": self._change_seq,
        }
        self._changes.append(rec)
        if len(self._changes) > 10000:
            self._changes.pop(0)
        self._publish_change(rec)
        if self._on_change:
            try:
                self._on_change(key, old, new)
            except Exception:
                logger.exception("StateManager.on_change callback failed for key=%s", key)

    def _publish_change(self, rec: dict[str, Any]) -> None:
        """Mirror a recorded change onto the session's event backbone.

        The local change log remains the authoritative audit trail (see
        :meth:`change_log` / :meth:`replay`); when the session has no backbone
        attached (lightweight embeddings, unit tests) this degrades to a
        no-op instead of raising.
        """
        bus = getattr(self._session, "event_bus", None)
        if bus is None:
            return
        try:
            bus.publish(
                "state_changed",
                session_id=getattr(self._session, "id", "") or "",
                payload={
                    "key": rec["key"],
                    "old": rec["old"],
                    "new": rec["new"],
                    "seq": rec["seq"],
                },
            )
        except Exception:
            logger.warning(
                "StateManager failed to publish change for key=%s", rec["key"],
                exc_info=True,
            )

    # ── Typed accessors ───────────────────────────────────────────────

    @property
    def task_stage(self) -> str:
        return self._get(META_TASK_STAGE, "discover")

    @task_stage.setter
    def task_stage(self, value: str) -> None:
        self._set(META_TASK_STAGE, value)

    @property
    def task_stage_history(self) -> list[dict[str, Any]]:
        return self._get(META_TASK_STAGE_HISTORY, [])

    @task_stage_history.setter
    def task_stage_history(self, value: list[dict[str, Any]]) -> None:
        self._set(META_TASK_STAGE_HISTORY, value)

    @property
    def working_set(self) -> dict[str, Any]:
        return self._get(META_WORKING_SET, {})

    @working_set.setter
    def working_set(self, value: dict[str, Any]) -> None:
        self._set(META_WORKING_SET, value)

    @property
    def turn_summaries(self) -> list[dict[str, Any]]:
        return self._get(META_TURN_SUMMARIES, [])

    @turn_summaries.setter
    def turn_summaries(self, value: list[dict[str, Any]]) -> None:
        self._set(META_TURN_SUMMARIES, value)

    @property
    def tool_semantics(self) -> dict[str, Any]:
        return self._get(META_TOOL_SEMANTICS, {})

    @tool_semantics.setter
    def tool_semantics(self, value: dict[str, Any]) -> None:
        self._set(META_TOOL_SEMANTICS, value)

    @property
    def stuck_events(self) -> list[dict[str, Any]]:
        return self._get(META_STUCK_EVENTS, [])

    @stuck_events.setter
    def stuck_events(self, value: list[dict[str, Any]]) -> None:
        self._set(META_STUCK_EVENTS, value)

    @property
    def delegate_history(self) -> list[dict[str, Any]]:
        return self._get(META_DELEGATE_HISTORY, [])

    @delegate_history.setter
    def delegate_history(self, value: list[dict[str, Any]]) -> None:
        self._set(META_DELEGATE_HISTORY, value)

    @property
    def user_requirements_summary(self) -> str:
        return self._get(META_USER_REQ_SUMMARY, "")

    @user_requirements_summary.setter
    def user_requirements_summary(self, value: str) -> None:
        self._set(META_USER_REQ_SUMMARY, value)

    @property
    def slash_command_mode(self) -> str:
        return self._get(META_SLASH_COMMAND_MODE, "")

    @slash_command_mode.setter
    def slash_command_mode(self, value: str) -> None:
        self._set(META_SLASH_COMMAND_MODE, value)

    @property
    def plan_mode_active(self) -> bool:
        return self._get(META_PLAN_MODE_ACTIVE, False)

    @plan_mode_active.setter
    def plan_mode_active(self, value: bool) -> None:
        self._set(META_PLAN_MODE_ACTIVE, value)

    @property
    def plan_gate_released(self) -> bool:
        """True while an approved plan authorises writes without a per-write
        proposal, for the run that was approved.

        Deliberately an instance attribute and not ``session.metadata``: a
        run-scoped gate must not survive into a reloaded session, where it
        would leave writes ungated with nothing ever approved them.  A
        standing goal (``active_goal``) is what makes the gate survive a run:
        it is persisted, and every run start re-derives the gate from it.
        """
        return self._plan_gate_released

    @plan_gate_released.setter
    def plan_gate_released(self, value: bool) -> None:
        self._plan_gate_released = bool(value)

    @property
    def active_goal(self) -> dict[str, Any] | None:
        """The standing plan/spec objective, or ``None``.

        Persisted in ``session.metadata`` -- an approved plan is a commitment
        that outlives the run, so unlike the write gate it must survive a
        reload.  Read through :class:`~encre.modes.goal.ActiveGoal.from_dict`
        for the typed view.
        """
        raw = self._get(META_ACTIVE_GOAL, None)
        return raw if isinstance(raw, dict) and raw else None

    @active_goal.setter
    def active_goal(self, value: dict[str, Any] | None) -> None:
        self._set(META_ACTIVE_GOAL, value or None)

    @property
    def active_command(self) -> dict[str, Any] | None:
        return self._get(META_ACTIVE_COMMAND, None)

    @active_command.setter
    def active_command(self, value: dict[str, Any] | None) -> None:
        self._set(META_ACTIVE_COMMAND, value)

    @property
    def milestone_summaries(self) -> list[dict[str, Any]]:
        return self._get(META_MILESTONE_SUMMARIES, [])

    @milestone_summaries.setter
    def milestone_summaries(self, value: list[dict[str, Any]]) -> None:
        self._set(META_MILESTONE_SUMMARIES, value)

    # ── Dict-style access for existing code that reads raw metadata ──

    def get(self, key: str, default: Any = None) -> Any:
        return self._session.metadata.get(key, default)

    def setdefault(self, key: str, default: Any) -> Any:
        if key not in self._session.metadata:
            self._set(key, default)
            return default
        return self._session.metadata[key]

    # ── Bulk operations ───────────────────────────────────────────────

    def update(self, **kwargs: Any) -> None:
        """Set multiple fields at once, recording a single change entry."""
        for key, value in kwargs.items():
            self._set(key, value)

    def clear(self) -> None:
        """Clear all tracked state (for test teardown / session reset)."""
        meta = self._session.metadata
        for key in (
            META_TASK_STAGE, META_TASK_STAGE_HISTORY, META_WORKING_SET,
            META_TURN_SUMMARIES, META_TOOL_SEMANTICS, META_STUCK_EVENTS,
            META_DELEGATE_HISTORY, META_USER_REQ_SUMMARY,
            META_SLASH_COMMAND_MODE, META_PLAN_MODE_ACTIVE,
            META_ACTIVE_COMMAND, META_MILESTONE_SUMMARIES,
        ):
            meta.pop(key, None)
        self._changes.clear()
        self._set_defaults()

    # ── Snapshot (frontend-facing AgentStateSnapshot) ─────────────────

    def snapshot(self) -> dict[str, Any]:
        """Produce a dict matching the frontend ``AgentStateSnapshot``."""
        meta = self._session.metadata
        return {
            "task_stage": meta.get(META_TASK_STAGE, "discover"),
            "task_stage_history": meta.get(META_TASK_STAGE_HISTORY, []),
            "working_set": meta.get(META_WORKING_SET, {}),
            "turn_summaries": meta.get(META_TURN_SUMMARIES, []),
            "delegate_history": meta.get(META_DELEGATE_HISTORY, []),
            "stuck_events": meta.get(META_STUCK_EVENTS, []),
            "tool_semantics": meta.get(META_TOOL_SEMANTICS, {}),
        }

    # ── Change log (audit / replay) ──────────────────────────────────

    def change_log(self) -> list[dict[str, Any]]:
        """Return the full sequenced change log."""
        return list(self._changes)

    def clear_change_log(self) -> None:
        """Clear the change log (e.g. after persisting)."""
        self._changes.clear()

    def replay(self, changes: list[dict[str, Any]]) -> None:
        """Replay a sequence of changes onto the current state.

        Used for audit playback or test setup.
        """
        for change in changes:
            key = change["key"]
            new = change["new"]
            self._session.metadata[key] = copy.deepcopy(new)


def snapshot_to_event(mgr: StateManager, session_id: str = "") -> dict[str, Any]:
    """Convert a StateManager snapshot into an ``agent_state`` event dict."""
    snap = mgr.snapshot()
    return {
        "task_stage": snap.get("task_stage", "discover"),
        "task_stage_history": snap.get("task_stage_history", []),
        "working_set": snap.get("working_set", {}),
        "turn_summaries": snap.get("turn_summaries", []),
        "delegate_history": snap.get("delegate_history", []),
        "stuck_events": snap.get("stuck_events", []),
        "tool_semantics": snap.get("tool_semantics", {}),
    }
