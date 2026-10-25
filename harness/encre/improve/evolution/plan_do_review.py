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

# Plan-Do-Review engine: decompose a complex task into a step graph,
# execute it turn-by-turn, and review each step (lightweight + periodic
# deep review) so the agent can self-correct before continuing.

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class StepStatus(Enum):
    # Lifecycle status of a single plan step.
    PENDING = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    FAILED = auto()
    BLOCKED = auto()
    SKIPPED = auto()


class ReviewGrade(Enum):
    # Outcome grade assigned to a step by the review phase.
    PASS = auto()
    PASS_WITH_ISSUES = auto()
    FAIL = auto()
    NEEDS_RETRY = auto()


@dataclass
class _ToolCallRecord:
    # English: one recorded tool invocation inside a step (name, args/result
    # summaries, error flag and latency).
    turn: int
    tool_name: str
    args_summary: str
    result_summary: str
    is_error: bool
    latency_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class StepNode:
    # English: a single node in the plan graph with its success criteria,
    # execution status, recorded tool calls, retry budget and dependencies.
    id: str
    description: str
    success_criteria: str
    status: StepStatus = StepStatus.PENDING
    tool_calls: list[_ToolCallRecord] = field(default_factory=list)
    result_summary: str = ""
    error: str = ""
    retry_count: int = 0
    max_retries: int = 2
    depends_on: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        # Render a one-line status summary (icon + description + status name).
        icon = {
            StepStatus.PENDING: "--",
            StepStatus.IN_PROGRESS: ">>",
            StepStatus.COMPLETED: "OK",
            StepStatus.FAILED: "XX",
            StepStatus.BLOCKED: "!!",
            StepStatus.SKIPPED: "--",
        }.get(self.status, "??")
        return f"[{icon}] {self.description}  ({self.status.name})"


@dataclass
class RuntimePlan:
    """The live plan for the current session."""
    # English: the live plan for the current session -- its steps, the index
    # of the step in progress, and aggregate progress counters.
    original_task: str = ""
    steps: list[StepNode] = field(default_factory=list)
    current_step_index: int = -1
    created_at: float = field(default_factory=time.time)
    completed_steps: int = 0
    failed_steps: int = 0
    total_tool_calls: int = 0
    retry_count: int = 0
    deep_reviews_done: int = 0

    @property
    def current_step(self) -> StepNode | None:
        # Return the step currently in progress, or None.
        if 0 <= self.current_step_index < len(self.steps):
            return self.steps[self.current_step_index]
        return None

    @property
    def is_complete(self) -> bool:
        # True once every step is completed or skipped.
        return all(s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED) for s in self.steps)

    def to_prompt_context(self) -> str:
        """Build the progress block injected into the system prompt."""
        # English: build the progress block injected into the system prompt.
        if not self.steps:
            return ""
        lines = ["## Plan-Do-Review Progress", ""]
        done = self.completed_steps
        total = len(self.steps)
        pct = int(done / total * 100) if total > 0 else 0
        lines.append(f"Progress: {done}/{total} ({pct}%)")
        lines.append("")
        lines.append("Steps:")
        for i, step in enumerate(self.steps):
            marker = "<-- current" if i == self.current_step_index else ""
            lines.append(f"  {i+1}. {step.summary_line()} {marker}")
            if step.notes:
                for note in step.notes[-2:]:
                    lines.append(f"      | {note}")
        lines.append("")
        if self.completed_steps + self.failed_steps > 0:
            parts = []
            if self.completed_steps > 0:
                parts.append(f"Completed: {self.completed_steps}")
            if self.failed_steps > 0:
                parts.append(f"Failed: {self.failed_steps}")
            if self.retry_count > 0:
                parts.append(f"Retries: {self.retry_count}")
            if parts:
                lines.append(f"Summary: {' | '.join(parts)}")
                lines.append("")
        curr = self.current_step
        if curr and curr.status == StepStatus.IN_PROGRESS:
            lines.append(f"Current task: {curr.description}")
            if curr.success_criteria:
                lines.append(f"Success criteria: {curr.success_criteria}")
            if curr.tool_calls:
                last = curr.tool_calls[-1]
                icon = "!" if last.is_error else ">"
                lines.append(f"{icon} Last tool call: `{last.tool_name}` > {last.result_summary[:120]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Complex task detection
# ---------------------------------------------------------------------------

# Structural complexity estimation -- language-agnostic by design.  No
# keyword tables: a word list is a vocabulary-shaped hole (it silently
# degrades for every language it was not written for).  The signals below
# are pure text statistics that behave identically across scripts:
#   * length pressure (whitespace tokens)
#   * line/block structure (explicit enumeration or blank-line sections)
#   * interrogative share (a mostly-questioning prompt is conversation,
#     not a work order)
# Interrogative detection uses terminal punctuation only -- "?"/"？" and
# the sentence split -- never vocabulary.

_LIST_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?。！？]+[)」』\]]*\s+")
_QUESTION_ENDINGS = ("?", "？")


def _estimate_complexity(task: str, max_turns: int = 0) -> float:
    """Score task complexity in ``0.0``-``1.0`` from structural signals.

    The score is a plain weighted sum of normalised statistics; the weights
    are hand-tuned constants, but every term is a *measurement*, not a
    vocabulary lookup.  Deterministic and O(len(task)).

    Args:
        task: The raw task text.
        max_turns: Configured turn budget for the run (a generous budget
            raises the bar for what is worth planning -- informational).

    Returns:
        The complexity score in ``[0.0, 1.0]``.
    """
    text = task.strip()
    if not text:
        return 0.0
    # Length pressure: ~120 whitespace tokens saturate the term.
    token_factor = min(len(text.split()) / 120, 0.35)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # Line structure: many distinct lines often encode structured intent.
    line_factor = min(len(lines) / 12, 0.15)
    # Explicit enumeration: numbered/bulleted items are the strongest
    # structural hint that the author already thinks in steps.
    listed = sum(1 for ln in lines if _LIST_MARKER_RE.match(ln))
    list_factor = min(listed / 5, 0.25)
    # Blank-line sections: multi-paragraph prompts carry more constraints.
    blocks = len(re.split(r"\n\s*\n", text))
    block_factor = min(max(blocks - 1, 0) / 4, 0.10)
    # Interrogative share: a prompt that is mostly questions is a
    # conversation opener, not a work order -- subtract it.
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) >= 2:
        q_share = sum(
            1 for s in sentences if s.rstrip().endswith(_QUESTION_ENDINGS)
        ) / len(sentences)
        question_penalty = min(q_share * 0.3, 0.3)
    else:
        question_penalty = 0.0
    turn_bonus = 0.15 if max_turns > 10 else 0.0
    score = token_factor + line_factor + list_factor + block_factor + turn_bonus - question_penalty
    return round(min(max(score, 0.0), 1.0), 3)


def should_plan(task: str, max_turns: int = 0) -> bool:
    """Plan when estimated complexity reaches the activation threshold."""
    return _estimate_complexity(task, max_turns) >= 0.45


# ---------------------------------------------------------------------------
# Plan decomposer
# ---------------------------------------------------------------------------

def decompose_task(task: str) -> list[dict[str, str]]:
    """Turn a free-form task into an ordered list of step dicts.

    Parsing order: numbered list -> bullet list -> single-step fallback.
    Both list parsers are language-agnostic structural readers.  The rich
    decomposition path (multi-step plans for unstructured prose) is the
    model-assisted channel in :func:`decompose_task_with_model`; this sync
    function never guesses steps from wording.

    Args:
        task: The raw task text.

    Returns:
        A list of step dicts with ``id``/``description``/``success_criteria``.
    """
    text = task.strip()
    steps = _parse_numbered_list(text)
    if steps and len(steps) >= 2:
        return steps
    steps = _parse_bullet_list(text)
    if steps and len(steps) >= 2:
        return steps
    return [
        {"id": "step_1", "description": text[:300],
         "success_criteria": "Task completed successfully"},
    ]


async def decompose_task_with_model(
    task: str,
    backend: Any,
    max_steps: int = 12,
) -> list[dict[str, str]] | None:
    """Model-assisted task decomposition (the rich path).

    One bounded, non-streaming, thinking-disabled call to the currently
    active backend; the model must answer with a JSON array of step
    objects (``description`` / ``success_criteria`` / optional
    ``depends_on``).  Fail-closed: a missing backend, a failed call, an
    unparsable answer, an empty array or any non-dict element yields
    ``None`` -- the caller then falls back to the structural parsers, and
    the plan degrades instead of being silently fabricated.  Steps are
    capped at *max_steps* (overflow is truncated, never invented).

    Args:
        task: The raw task text.
        backend: The loop backend (will be thinking-disabled-cloned).
        max_steps: Upper bound on accepted steps.

    Returns:
        Step dicts on success, else ``None``.
    """
    from encre.logging_config import get_logger
    from encre.prompts.loader import PromptLoader
    from encre.utils.backend_clone import thinking_off_clone
    from encre.utils.types import BackendFinish, BackendText

    logger = get_logger(__name__)
    if backend is None or not task.strip():
        return None
    scorer = thinking_off_clone(backend)
    if scorer is None:
        return None
    try:
        prompt = PromptLoader().load("task_decomposer", category="evolution")
    except Exception:
        logger.warning("[pdr] task_decomposer prompt missing; structural fallback")
        return None
    full_text = ""
    try:
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": task[:4000]},
        ]
        async for event in scorer.chat(messages=messages, max_tokens=1200, stream=False):
            if isinstance(event, BackendText):
                full_text += event.text
            elif isinstance(event, BackendFinish):
                break
    except Exception:
        logger.warning("[pdr] model decomposition call failed", exc_info=True)
        return None
    # The model is instructed to answer with a bare JSON array; strip a
    # possible markdown fence before parsing.
    payload = full_text.strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        if payload.startswith("json"):
            payload = payload[4:]
        payload = payload.strip()
    try:
        data = json.loads(payload)
    except Exception:
        logger.warning("[pdr] model decomposition unparsable; structural fallback")
        return None
    if not isinstance(data, list) or not data:
        return None
    steps: list[dict[str, str]] = []
    for i, item in enumerate(data[:max_steps]):
        if not isinstance(item, dict):
            return None
        description = str(item.get("description", "")).strip()
        if not description:
            return None
        criteria = str(item.get("success_criteria", "")).strip() or (
            f"Step {i + 1} completed successfully"
        )
        steps.append({
            "id": f"step_{i + 1}",
            "description": description[:300],
            "success_criteria": criteria[:200],
        })
    if len(steps) < 2:
        # A single-step "plan" from the model is not a plan.
        return None
    return steps


def _parse_numbered_list(text: str) -> list[dict[str, str]]:
    # Parse "1. ..." / "Step N: ..." style numbered step lists.
    pattern = re.compile(
        r'(?:^|\n)\s*(?:\d+[\.\)]|Step\s+\d+[:\-])\s*(.+?)(?=\n\s*(?:\d+[\.\)]|Step\s+\d+[:\-]|\Z))',
        re.DOTALL,
    )
    matches = list(pattern.finditer(text))
    if len(matches) >= 2:
        return [
            {"id": f"step_{i+1}", "description": m.group(1).strip().rstrip("."),
             "success_criteria": f"Step {i+1} completed successfully"}
            for i, m in enumerate(matches)
        ]
    return []


def _parse_bullet_list(text: str) -> list[dict[str, str]]:
    # Parse "- item" / "* item" bullet step lists.
    items = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and stripped[0] in ("-", "*") and len(stripped) > 2:
            items.append(stripped[1:].strip())
    if len(items) >= 2:
        return [
            {"id": f"step_{i+1}", "description": item,
             "success_criteria": f"Step {i+1} completed successfully"}
            for i, item in enumerate(items)
        ]
    return []


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------


class PlanDoReviewEngine:
    """Plan-Do-Review autonomous loop engine."""

    # English: drives the plan lifecycle -- builds the step graph from a task,
    # advances through steps, records tool calls and applies light/deep review.
    MAX_STEPS = 12
    DEEP_REVIEW_INTERVAL = 3

    def __init__(self) -> None:
        # Fresh plan + inactive until initialize() is called.
        self.plan = RuntimePlan()
        self._is_active = False

    def initialize(self, task: str, steps: list[dict[str, str]] | None = None) -> None:
        """Build the step graph (capped at MAX_STEPS) and activate the engine.

        Args:
            task: The raw task text.
            steps: Optional pre-decomposed steps (e.g. from
                :func:`decompose_task_with_model`).  When omitted, the
                structural parsers decide.
        """
        self.plan = RuntimePlan(original_task=task[:2000])
        raw_steps = steps if steps else decompose_task(task)
        for i, s in enumerate(raw_steps[:self.MAX_STEPS]):
            self.plan.steps.append(StepNode(
                id=s.get("id", f"step_{i+1}"),
                description=s["description"],
                success_criteria=s.get("success_criteria", "Step completed successfully"),
                depends_on=[raw_steps[j]["id"] for j in range(i) if j < len(raw_steps)],
            ))
        self._is_active = True
        self.plan.current_step_index = -1

    def start_next_step(self) -> StepNode | None:
        # Promote the next PENDING step (deps met => IN_PROGRESS, else BLOCKED).
        for i, step in enumerate(self.plan.steps):
            if step.status == StepStatus.PENDING:
                deps_met = all(
                    any(s.id == dep and s.status == StepStatus.COMPLETED for s in self.plan.steps)
                    for dep in step.depends_on
                )
                step.status = StepStatus.BLOCKED if not deps_met else StepStatus.IN_PROGRESS
                if deps_met:
                    self.plan.current_step_index = i
                    return step
        self.plan.current_step_index = -1
        return None

    def mark_step_complete(self, summary: str = "") -> None:
        # Mark the current step completed and record a short result summary.
        step = self.plan.current_step
        if step is None:
            return
        step.status = StepStatus.COMPLETED
        step.result_summary = summary[:500]
        self.plan.completed_steps += 1

    def mark_step_failed(self, error: str = "") -> None:
        # Retry while under budget, otherwise mark the step FAILED.
        step = self.plan.current_step
        if step is None:
            return
        step.error = error[:500]
        if step.retry_count < step.max_retries:
            step.retry_count += 1
            step.status = StepStatus.IN_PROGRESS
            self.plan.retry_count += 1
        else:
            step.status = StepStatus.FAILED
            self.plan.failed_steps += 1

    def skip_current_step(self, reason: str = "") -> None:
        # Skip the in-progress step, recording the reason as a note.
        step = self.plan.current_step
        if step is None:
            return
        step.status = StepStatus.SKIPPED
        step.notes.append(f"Skipped: {reason[:200]}")
        self.plan.completed_steps += 1

    def record_tool_call(
        self, turn: int, tool_name: str, args: dict[str, Any],
        result: str, is_error: bool, latency_ms: float = 0.0,
    ) -> None:
        # Append a tool-call record to the current step and bump the counter.
        step = self.plan.current_step
        if step is None:
            return
        step.tool_calls.append(_ToolCallRecord(
            turn=turn, tool_name=tool_name,
            args_summary=_summarize_args(args),
            result_summary=_truncate(result, 200) if result else "",
            is_error=is_error, latency_ms=latency_ms,
        ))
        self.plan.total_tool_calls += 1

    def lightweight_review(self) -> ReviewGrade:
        # Cheap per-step review from tool-call errors / repetition / empties.
        step = self.plan.current_step
        if step is None:
            return ReviewGrade.PASS
        calls = step.tool_calls
        if not calls:
            return ReviewGrade.PASS
        error_calls = [c for c in calls if c.is_error]
        if error_calls and len(error_calls) == len(calls):
            return ReviewGrade.FAIL
        if error_calls and len(error_calls) / len(calls) > 0.5:
            return ReviewGrade.NEEDS_RETRY
        tool_counts: dict[str, int] = {}
        for c in calls:
            tool_counts[c.tool_name] = tool_counts.get(c.tool_name, 0) + 1
        repeated = {t: n for t, n in tool_counts.items() if n >= 4}
        if repeated:
            step.notes.append(f"Repeated tool calls: {repeated}")
            return ReviewGrade.PASS_WITH_ISSUES
        empty_results = [
            c for c in calls
            if not c.result_summary or c.result_summary.strip() in ("", "[]", "{}", "null", "None")
        ]
        if empty_results and len(empty_results) / len(calls) > 0.4:
            return ReviewGrade.PASS_WITH_ISSUES
        return ReviewGrade.PASS

    def needs_deep_review(self) -> bool:
        # Trigger a deep review every DEEP_REVIEW_INTERVAL steps or on 2 fails.
        if self.plan.completed_steps > 0 and self.plan.completed_steps % self.DEEP_REVIEW_INTERVAL == 0:
            self.plan.deep_reviews_done += 1
            return True
        if self.plan.failed_steps >= 2:
            recent = self.plan.steps[-2:]
            if all(s.status == StepStatus.FAILED for s in recent if s.status != StepStatus.PENDING):
                return True
        return False

    def should_plan(self, task: str, max_turns: int = 0) -> bool:
        # Delegate to the module-level complexity heuristic.
        return should_plan(task, max_turns)

    def get_context(self) -> str:
        # Return the prompt-context block for the active plan (or empty).
        if not self._is_active or not self.plan.steps:
            return ""
        return self.plan.to_prompt_context()

    def reset(self) -> None:
        # Discard the plan and deactivate the engine.
        self.plan = RuntimePlan()
        self._is_active = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _summarize_args(args: dict[str, Any]) -> str:
    # JSON-summarise args, redacting obvious secret keys.
    if not args:
        return ""
    skip_keys = {"api_key", "password", "secret", "token", "key"}
    safe = {k: v for k, v in args.items() if k not in skip_keys}
    try:
        return json.dumps(safe, ensure_ascii=False)[:150]
    except (TypeError, ValueError):
        return str(safe)[:150]


def _truncate(s: str, max_len: int) -> str:
    # Truncate a string to max_len characters with an ellipsis.
    if len(s) <= max_len:
        return s
    return s[:max_len - 3] + "..."


__all__ = [
    "PlanDoReviewEngine",
    "ReviewGrade",
    "RuntimePlan",
    "StepNode",
    "StepStatus",
    "decompose_task",
    "decompose_task_with_model",
    "should_plan",
]
