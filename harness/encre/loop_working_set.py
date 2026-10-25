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

import time
from typing import Any

from encre.logging_config import get_logger
from encre.modes.goal import ACTIVE as _GOAL_ACTIVE
from encre.modes.goal import DONE as _GOAL_DONE
from encre.modes.goal import ActiveGoal
from encre.utils.loop_helpers import (
    _SUMMARY_INTERVAL_TURNS,
    _TASK_STAGES,
    _WORKING_SET_ARTIFACT_LIMIT,
    _WORKING_SET_REFERENCE_LIMIT,
    _WORKING_SET_TOOL_LIMIT,
    _WRITE_TOOL_NAMES,
    _args_summary,
)

logger = get_logger(__name__)

#: Checklist entries carried into the working set (and the prompt block).
_WORKING_SET_PLAN_LIMIT = 12


def _plan_marker(status: Any) -> str:
    value = str(status or "").strip().lower()
    if value in (_GOAL_DONE, "completed", "complete", "done"):
        return "x"
    if value in (_GOAL_ACTIVE, "in_progress", "active", "doing"):
        return "~"
    return " "


class WorkingSetManager:
    """Task stage, working set, turn summary, and milestone management.

    Encapsulates all logic for tracking the agent's work phase (discover /
    plan / execute / verify / report), maintaining the working set snapshot,
    recording turn summaries for context, and writing periodic milestones.
    Composed into :class:`EncreLoop` via delegation.
    """

    def __init__(self, session: Any, state_mgr: Any, config: Any) -> None:
        self._session = session
        self._state_mgr = state_mgr
        self._config = config
        self._milestone_last_turn: int = -1
        self._staged_prepared: list[dict[str, Any]] = []

    # ── Task stage ───────────────────────────────────────────────────

    def set_task_stage(self, stage: str, reason: str = "") -> None:
        if stage not in _TASK_STAGES:
            return
        prev = str(self._state_mgr.task_stage)
        if prev == stage:
            return
        history = list(self._state_mgr.task_stage_history)
        history.append({
            "from": prev,
            "to": stage,
            "reason": reason[:240],
            "turn": self._session.turn_count,
            "timestamp": time.time(),
        })
        self._state_mgr.task_stage = stage
        self._state_mgr.task_stage_history = history

    def infer_task_stage(self, prompt: str, prepared: list[dict[str, Any]] | None = None) -> str:
        current = str(self._state_mgr.task_stage)
        prompt_lower = (prompt or "").lower()
        prepared = prepared or []
        names = [str(p.get("name", "")).lower() for p in prepared]
        semantic_types = [str(p.get("semantics", {}).get("semantic_type", "")).lower() for p in prepared]

        if any(x in prompt_lower for x in ("summary", "report", "what did", "结果", "总结", "汇报")):
            return "report"
        if any(x in prompt_lower for x in ("plan", "方案", "设计", "spec", "步骤")):
            return "plan"
        if any(t in {"write", "exec"} for t in semantic_types) or any(n in _WRITE_TOOL_NAMES for n in names):
            return "execute"
        if any(x in prompt_lower for x in ("verify", "test", "check", "确认", "验证")) or any("test" in n or "lint" in n for n in names):
            return "verify"
        return current

    # ── Working set snapshot ─────────────────────────────────────────

    def build_working_set(self) -> dict[str, Any]:
        stage = str(self._state_mgr.task_stage)
        tool_names = [str(p.get("name", "")) for p in self._staged_prepared]
        tools = tool_names[:_WORKING_SET_TOOL_LIMIT]
        artifacts = [
            {
                "path": str(getattr(a, "path", a.get("path", "") if isinstance(a, dict) else "")),
                "label": str(getattr(a, "label", a.get("label", "") if isinstance(a, dict) else "")),
                "mime": str(getattr(a, "mime", a.get("mime", "") if isinstance(a, dict) else "")),
            }
            for a in (self._session.artifacts or [])[-_WORKING_SET_ARTIFACT_LIMIT:]
        ]
        refs = [
            {
                "path": str(getattr(r, "path", r.get("path", "") if isinstance(r, dict) else "")),
                "kind": str(getattr(r, "kind", r.get("kind", "") if isinstance(r, dict) else "")),
                "summary": str(getattr(r, "summary", r.get("summary", "") if isinstance(r, dict) else "")),
            }
            for r in (self._session.references or [])[-_WORKING_SET_REFERENCE_LIMIT:]
        ]
        plans = self._plans_snapshot()
        ws = {
            "tools": tools,
            "artifacts": artifacts,
            "references": refs,
            "plans": plans,
            "stage": stage,
            "updated_at": time.time(),
        }
        self._state_mgr.working_set = ws
        return ws

    def _goal_header(self) -> list[str]:
        """Title and progress lines for a live standing goal (empty otherwise).

        The checklist rows alone do not say what the checklist is *for*; the
        header names the approved artifact and how far along it is, which is
        the frame the stop gate is enforcing.
        """
        goal = ActiveGoal.from_dict(self._state_mgr.active_goal)
        if goal is None or not goal.drives:
            return []
        return [
            f"Approved {goal.kind}: {goal.title or '(untitled)'}",
            f"Progress: {goal.done_count()}/{goal.total}",
        ]

    def _plans_snapshot(self) -> list[dict[str, Any]]:
        """Checklist snapshot for the working set.

        A standing goal wins when one is active: it is the contract the stop
        gate enforces, so the model is shown the approved checklist (with live
        statuses) rather than a possibly stale todo snapshot.  Without a goal
        the session's own task list is used.
        """
        raw_goal = self._state_mgr.active_goal
        goal = ActiveGoal.from_dict(raw_goal) if raw_goal else None
        if goal is not None and goal.drives:
            rows = [
                (item.id, item.text, item.status)
                for item in goal.items[:_WORKING_SET_PLAN_LIMIT]
            ]
        else:
            todos = list(self._session.plan_items or [])[-_WORKING_SET_PLAN_LIMIT:]
            rows = [self._todo_row(entry) for entry in todos]
        return [
            {"id": item_id, "description": f"[{_plan_marker(status)}] {text}"}
            for item_id, text, status in rows
            if text
        ]

    @staticmethod
    def _todo_row(entry: Any) -> tuple[str, str, str]:
        def pick(key: str) -> str:
            if isinstance(entry, dict):
                return str(entry.get(key, "") or "")
            return str(getattr(entry, key, "") or "")

        return pick("id"), pick("text") or pick("description"), pick("status")

    def _summarize_args(self, args: dict[str, Any]) -> str:
        return _args_summary(args)

    def refresh_working_set(self, prompt: str, prepared: list[dict[str, Any]] | None = None) -> None:
        self._staged_prepared = list(prepared) if prepared else []
        self._session._last_prompt = prompt
        self.build_working_set()

    def build_working_set_prompt(self) -> str:
        ws = self._state_mgr.working_set
        lines = ["## Current Task State"]
        prompt_part = getattr(self._session, "_last_prompt", "")
        if prompt_part:
            lines.append(f"User asked: {prompt_part[:200]}")
        stage = ws.get("stage", "discover")
        lines.append(f"Phase: {stage}")
        tools = ws.get("tools", [])
        if tools:
            lines.append(f"Active tools: {', '.join(tools[:10])}")
        artifacts = ws.get("artifacts", [])
        if artifacts:
            lines.append(f"Artifacts: {len(artifacts)}")
        refs = ws.get("references", [])
        if refs:
            for r in refs[-3:]:
                path = r.get("path", "") or ""
                summary = r.get("summary", "") or ""
                if path:
                    lines.append(f"  - {path}: {summary[:100]}")
        plans = ws.get("plans", [])
        if plans:
            lines.extend(self._goal_header())
            lines.append("Checklist:")
            for p in plans:
                lines.append(f"  {p.get('description', '')}")
        follow_ups = self.get_sub_agent_next_actions()
        if follow_ups:
            lines.append("Sub-agent follow-ups (pending):")
            for f in follow_ups[-5:]:
                lines.append(f"  - {f.get('action', '')}")
        return "\n".join(lines)

    # ── Turn summaries ──────────────────────────────────────────────

    def build_turn_summary(self, turn_num: int, outcome: str, detail: str = "") -> dict[str, Any]:
        return {
            "turn": turn_num,
            "outcome": outcome[:80],
            "detail": detail[:400],
            "timestamp": time.time(),
        }

    def maybe_write_turn_summary(self, outcome: str, detail: str = "") -> None:
        turn = self._session.turn_count
        if turn == 0:
            return
        if turn % _SUMMARY_INTERVAL_TURNS == 0:
            summary = self.build_turn_summary(turn, outcome, detail)
            summaries = list(self._state_mgr.turn_summaries)
            summaries.append(summary)
            self._state_mgr.turn_summaries = summaries

    def maybe_record_turn_summary(
        self,
        prompt: str,
        prepared: list[dict[str, Any]],
        tool_outcomes: list[dict[str, Any]],
    ) -> None:
        turn = self._session.turn_count
        if turn == 0:
            return
        outcome_parts: list[str] = []
        for o in tool_outcomes or []:
            name = (o or {}).get("tool_name", "") or ""
            is_err = (o or {}).get("is_error", False)
            outcome_parts.append(f"{'!' if is_err else '+'}{name}")
        outcome = ", ".join(outcome_parts) or "completed"
        summaries = list(self._state_mgr.turn_summaries)
        summaries.append({
            "turn": turn,
            "outcome": outcome[:80],
            "detail": prompt[:200],
            "timestamp": time.time(),
        })
        self._state_mgr.turn_summaries = summaries

    def get_turn_summaries(self) -> list[dict[str, Any]]:
        return list(self._state_mgr.turn_summaries)

    def build_turn_summary_prompt(self) -> str:
        summaries = self._state_mgr.turn_summaries
        if not summaries:
            return ""
        lines = ["## Prior Turns"]
        for s in summaries[-5:]:
            turn = s.get("turn", "?")
            outcome = s.get("outcome", "") or ""
            detail = s.get("detail", "") or ""
            ts = s.get("timestamp", 0)
            lines.append(f"  Turn {turn}: {outcome} | {detail[:120]}")
        return "\n".join(lines)

    def build_stage_prompt(self) -> str:
        stage = self._state_mgr.task_stage
        if not stage:
            stage = "discover"
        return (
            f"**Work Phase:** {stage}\n"
            "This work phase is an internal scheduling cue, not a user "
            "mode.  The current mode block above tells you the real mode.  "
            "When asked what mode you are in, do NOT answer "
            f"\"{stage} mode\"."
        )

    # ── Milestone summaries (checkpoint-like) ───────────────────────

    def write_milestone(self, outcome: str, detail: str = "") -> None:
        turn = self._session.turn_count
        if self._milestone_last_turn == turn:
            return
        self._milestone_last_turn = turn
        summaries = list(self._state_mgr.milestone_summaries)
        summaries.append({
            "turn": turn,
            "outcome": outcome[:200],
            "detail": detail[:1000],
        })
        self._state_mgr.milestone_summaries = summaries

    async def maybe_write_milestone(
        self,
        context_msgs: list[dict[str, Any]],
        backend: Any = None,
        compact_engine: Any = None,
    ) -> None:
        if self._session.turn_count == self._milestone_last_turn:
            return
        if self._session.turn_count % _SUMMARY_INTERVAL_TURNS != 0:
            return
        try:
            if backend is not None and compact_engine is not None:
                from encre.compact.milestone import summarize_milestone
                milestone = await summarize_milestone(
                    context_msgs, backend, compact_engine,
                )
                if milestone:
                    self.write_milestone(milestone.get("outcome", ""), milestone.get("detail", ""))
        except Exception:
            logger.warning("[milestone] failed to write milestone", exc_info=True)

    # ── Delegate history ────────────────────────────────────────────

    def record_delegate(self, sub_agent_id: str, task: str) -> None:
        history = list(self._state_mgr.delegate_history)
        history.append({
            "sub_agent_id": sub_agent_id,
            "task": task[:200],
            "timestamp": time.time(),
        })
        self._state_mgr.delegate_history = history

    def record_sub_agent_handoff(self, sub_agent_id: str, handoff: dict[str, Any]) -> None:
        """Record the structured handoff returned by a finished sub-agent.

        Keeps the sub-agent's ``next_actions`` as pending follow-ups that are
        surfaced in the working-set prompt so the parent model carries them
        across turns instead of losing them after the tool result scrolls out.

        Args:
            sub_agent_id: Session id of the finished sub-agent.
            handoff: ``{"summary", "artifacts", "next_actions"}`` dict.
        """
        if not isinstance(handoff, dict):
            return
        actions = handoff.get("next_actions") or []
        if not actions:
            return
        pending = list(getattr(self._state_mgr, "sub_agent_next_actions", []))
        for action in actions:
            pending.append({
                "sub_agent_id": sub_agent_id,
                "action": str(action)[:300],
                "timestamp": time.time(),
            })
        # Keep a bounded backlog; follow-ups older than the limit are dropped.
        self._state_mgr.sub_agent_next_actions = pending[-20:]

    def get_sub_agent_next_actions(self) -> list[dict[str, Any]]:
        return list(getattr(self._state_mgr, "sub_agent_next_actions", []))

    def clear_transient_state(self) -> None:
        """Reset in-memory-only working-set state after a history rewrite.

        ``sub_agent_next_actions`` lives as a plain attribute on the state
        manager (not in ``session.metadata``), so session-level pruning of
        the derived state never reaches it. Left behind, follow-ups recorded
        for turns that were edited / deleted / rolled back keep leaking into
        the working-set prompt.
        """
        self._state_mgr.sub_agent_next_actions = []

    # ── Stuck-event tracking ────────────────────────────────────────

    def record_stuck_event(self, event: dict[str, Any]) -> None:
        events = list(self._state_mgr.stuck_events)
        events.append(event)
        self._state_mgr.stuck_events = events

    def get_stuck_events(self) -> list[dict[str, Any]]:
        return list(self._state_mgr.stuck_events)

    # ── Tool semantics ──────────────────────────────────────────────

    def get_tool_semantics(self) -> dict[str, Any]:
        return dict(self._state_mgr.tool_semantics)

    def set_tool_semantics(self, semantics: dict[str, Any]) -> None:
        self._state_mgr.tool_semantics = semantics
