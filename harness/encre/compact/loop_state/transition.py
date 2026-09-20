#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

"""Turn transition types and history tracking for the agent loop.

Mirrors Claude Code's transition types in ``src/query.ts``.  Each turn of the
agent loop ends with one ``TurnTransition`` that describes *why* the next turn
started (or why the loop terminated), replacing the ad-hoc ``continue`` /
``return`` / ``break`` decisions scattered through ``_run_impl()``.

Usage::

    from encre.compact.loop_state.transition import TurnTransition, TransitionHistory

    history = TransitionHistory()
    history.record(TurnTransition.NEXT_TURN, turn=5, detail="tool_use")
    history.record(TurnTransition.TEXT_ONLY, turn=6, detail="no tool calls")

    latest = history.last  # TurnTransition.TEXT_ONLY
    for t in history:      # iterate newest first
        ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TurnTransition(Enum):
    """Every way a turn of the agent loop can end or continue.

    Mirrors Claude Code's transition types in ``query.ts`` (lines ~216-225)
    and extends them with Encre-specific transitions (e.g. ``AUTO_CONTINUE``,
    ``BUDGET_GRACE``, ``SPEC_PARSE``).
    """

    # 鈹€鈹€ Normal continuations 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    NEXT_TURN = "next_turn"
    """Normal tool_use continuation: the model issued tool calls,
    we executed them, and the next turn begins with the results."""

    TEXT_ONLY = "text_only"
    """Model produced a text-only response with no tool calls.  The
    turn ends normally; the outer loop receives it as a final answer."""

    # 鈹€鈹€ Output budget transitions 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    MAX_OUTPUT_TOKENS = "max_output_tokens"
    """Model hit the output-token limit (``max_tokens``/``length`` finish
    reason).  The slot budget is escalated or a continuation is injected."""

    SLOT_ESCALATION = "slot_escalation"
    """First occurrence of ``MAX_OUTPUT_TOKENS``: the ``default_slot_tokens``
    budget was too small, so we escalate to the full ``max_tokens`` budget
    without retrying."""

    AUTO_CONTINUE = "auto_continue"
    """Token-budget auto-continue: the user set a token budget (e.g. +500k)
    and the model hasn't exhausted it yet, so we inject an auto-continue
    user message and loop."""

    BUDGET_GRACE = "budget_grace"
    """Token budget exhausted: the model gets one final "wrap up" call
    (grace call) before termination."""

    # 鈹€鈹€ Error recovery transitions 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    REACTIVE_COMPACT = "reactive_compact"
    """API 413 / context overflow: compact the session and retry the turn."""

    MODEL_FALLBACK = "model_fallback"
    """Rate-limit or overload: switch to the fallback model and retry."""

    NETWORK_RETRY = "network_retry"
    """Transient network error: retry after a short delay."""

    EMPTY_RESPONSE = "empty_response"
    """Model returned no text and no tool calls: inject a "please respond"
    message and retry."""

    STUB_RESPONSE = "stub_response"
    """Model returned a brief text-only acknowledgment (e.g. "I'll help you
    with that.") without any tool calls or prior tool work.  Inject a
    forceful "act now" nudge and retry.  Catches the #1 task-delivery
    failure mode where the model acknowledges but never starts working."""

    TRUNCATED_TOOL_CALL = "truncated_tool_call"
    """Tool call arguments are truncated or invalid JSON: clear the broken
    buffers and inject a repair message."""

    UNKNOWN_ERROR = "unknown_error"
    """Unknown/uncategorised error: one retry then surface to user."""

    ERROR_CONSUMED = "error_consumed"
    """Error was silently consumed after recovery (auth, exhausted-unknown, etc.).
    The loop continues without surfacing the error to the user."""

    # 鈹€鈹€ Hook-driven transitions 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    STOP_HOOK_BLOCK = "stop_hook_block"
    """A stop hook returned a blocking result.  The hook output is
    injected as a user message and the loop continues."""

    # 鈹€鈹€ Terminal transitions 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    CANCELLED = "cancelled"
    """User cancelled the run (Stop button / keyboard interrupt)."""

    ERROR = "error"
    """Unrecoverable error surfaced to the user."""

    MAX_TURNS = "max_turns"
    """Session reached the maximum number of turns."""


@dataclass
class TransitionRecord:
    """A single turn transition with metadata."""

    transition: TurnTransition
    turn: int
    detail: str = ""
    """Free-text detail, e.g. the tool name that triggered the transition,
    the error message, or the number of retries."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Optional structured metadata for diagnostics (recovery count,
    token usage, latency, etc.)."""

    @property
    def is_terminal(self) -> bool:
        return self.transition in (
            TurnTransition.TEXT_ONLY,
            TurnTransition.CANCELLED,
            TurnTransition.ERROR,
            TurnTransition.MAX_TURNS,
        )

    @property
    def is_recovery(self) -> bool:
        return self.transition in (
            TurnTransition.REACTIVE_COMPACT,
            TurnTransition.MODEL_FALLBACK,
            TurnTransition.NETWORK_RETRY,
            TurnTransition.EMPTY_RESPONSE,
            TurnTransition.TRUNCATED_TOOL_CALL,
            TurnTransition.UNKNOWN_ERROR,
            TurnTransition.MAX_OUTPUT_TOKENS,
            TurnTransition.SLOT_ESCALATION,
        )


@dataclass
class TransitionHistory:
    """Ordered history of turn transitions for a single ``_run_impl`` call.

    Index 0 is the first transition (oldest).  Callers can iterate to
    produce a post-mortem of the agent's execution path.

    When ``event_bus`` is set (the loop injects the session's
    :class:`~encre.events.ledger.EventSourcedBus`), every recorded transition
    is also published to the backbone as a ``"state_transition"`` event; the
    in-memory ``records`` list stays the authoritative per-run history.
    """

    records: list[TransitionRecord] = field(default_factory=list)

    event_bus: Any = None
    """Optional event backbone; transitions are mirrored onto it."""

    session_id: str = ""
    """Session id stamped on published transition events."""

    # 鈹€鈹€ properties 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    @property
    def last(self) -> TurnTransition | None:
        """The most recent transition, or None if empty."""
        if not self.records:
            return None
        return self.records[-1].transition

    @property
    def last_record(self) -> TransitionRecord | None:
        """The most recent record, or None if empty."""
        if not self.records:
            return None
        return self.records[-1]

    @property
    def recovery_count(self) -> int:
        """Number of recovery transitions in the full history."""
        return sum(1 for r in self.records if r.is_recovery)

    @property
    def terminal(self) -> TransitionRecord | None:
        """The terminal transition that ended the run, or None."""
        for r in reversed(self.records):
            if r.is_terminal:
                return r
        return None

    # 鈹€鈹€ recording 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def record(
        self,
        transition: TurnTransition,
        turn: int,
        detail: str = "",
        **metadata: Any,
    ) -> TransitionRecord:
        """Append a new transition to the history.

        Returns the record for chaining.
        """
        rec = TransitionRecord(
            transition=transition,
            turn=turn,
            detail=detail,
            metadata=metadata,
        )
        self.records.append(rec)
        self._publish(rec)
        return rec

    def _publish(self, rec: TransitionRecord) -> None:
        """Mirror a transition onto the event backbone (no-op without one)."""
        bus = self.event_bus
        if bus is None:
            return
        try:
            bus.publish(
                "state_transition",
                session_id=self.session_id,
                payload={
                    "transition": rec.transition.value,
                    "turn": rec.turn,
                    "detail": rec.detail,
                    "metadata": dict(rec.metadata),
                },
            )
        except Exception:
            pass

    def clear(self) -> None:
        """Clear the history (called at start of a new ``_run_impl``)."""
        self.records.clear()

    # 鈹€鈹€ iteration (newest first) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def __iter__(self):
        return iter(reversed(self.records))

    def __len__(self) -> int:
        return len(self.records)

    def __bool__(self) -> bool:
        return bool(self.records)

    # 鈹€鈹€ serialization 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def to_dict(self) -> list[dict[str, Any]]:
        return [
            {
                "transition": r.transition.value,
                "turn": r.turn,
                "detail": r.detail,
                "metadata": dict(r.metadata),
                "is_terminal": r.is_terminal,
                "is_recovery": r.is_recovery,
            }
            for r in self.records
        ]

    @classmethod
    def from_dict(cls, data: list[dict[str, Any]]) -> TransitionHistory:
        history = cls()
        for item in data:
            try:
                transition = TurnTransition(item["transition"])
            except ValueError:
                continue
            history.records.append(
                TransitionRecord(
                    transition=transition,
                    turn=item.get("turn", 0),
                    detail=item.get("detail", ""),
                    metadata=item.get("metadata", {}),
                )
            )
        return history
