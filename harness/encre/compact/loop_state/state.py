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

"""Unified per-run loop state for the agent main loop.

Centralises the ~30+ scalar member variables that ``EncreLoop`` currently
carries across ``__init__``, ``run()``, and ``_run_impl()`` into a single
dataclass.  This makes it possible to:

* Snapshot the full loop state for diagnostics / telemetry at any point.
* Reset cleanly between runs without risking leaked state.
* Observe the loop's execution path via the embedded ``TransitionHistory``.

Usage::

    from encre.compact.loop_state.state import LoopState

    # At the top of `_run_impl()`:
    state = LoopState(turn_count=self.session.turn_count)

    # After each transition:
    state.transitions.record(TurnTransition.NEXT_TURN, turn=state.turn_count)

    # For diagnostics at any point:
    snapshot = state.snapshot()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from encre.compact.loop_state.transition import TransitionHistory


@dataclass
class LoopState:
    """All mutable loop state for a single ``_run_impl()`` invocation.

    This dataclass is the **single source of truth** for per-run counters,
    tracking variables, and transition history that were previously spread
    across ``EncreLoop.__init__`` member variables.  The ``EncreLoop`` still
    owns long-lived references (session, config, backends); this is purely
    the **ephemeral run state** that resets at the start of each user turn.

    Migration strategy (phased, no behavioral changes):

    Phase 1 鈥?present: Own the transition history and provide ``snapshot()``.
           Inline counters (``_empty_response_retry_count`` etc.) remain on
           ``EncreLoop`` for now; this class shadows them for observability.

    Phase 2 鈥?next: Move the per-run counters into this class and have
           ``_run_impl()`` reference ``self._state.empty_response_retry_count``
           instead of ``self._empty_response_retry_count``.

    Phase 3 鈥?future: Merge with ``RecoveryStateMachine`` so the RSM consumes
           state from here rather than duplicating counters.
    """

    # 鈹€鈹€ Turn tracking 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    turn_count: int = 0
    """Current turn number within this run (mirrors session.turn_count)."""

    chain_id: str = ""
    """Stable identifier for the chain of continuations within a single user
    query (mirrors Claude Code's ``chainId`` in ``QueryChainTracking``)."""

    chain_depth: int = 0
    """Number of continuations within this chain (incremented on each
    ``NEXT_TURN`` transition)."""

    # 鈹€鈹€ Transition history 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    transitions: TransitionHistory = field(default_factory=TransitionHistory)
    """Ordered log of every turn transition, used for post-mortem
    diagnostics and telemetry."""

    # 鈹€鈹€ Per-run counters (Phase 2 target) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    # These currently live as EncreLoop member variables but are tracked
    # here for snapshot/observability.  EncreLoop's own counters are the
    # authoritative source until Phase 2.
    empty_response_retry_count: int = 0
    truncated_tool_call_retry_count: int = 0
    max_output_tokens_recovery_count: int = 0
    unknown_error_retry_count: int = 0

    # 鈹€鈹€ Streaming tool execution 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    streaming_tool_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Maps ``client_id`` 鈫?pre-executed tool result.  Populated during the
    streaming phase and consumed during the post-streaming execution phase."""

    # 鈹€鈹€ Slot / budget state 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    slot_escalated: bool = False
    """True when ``default_slot_tokens`` has been escalated to
    ``max_tokens`` for this run."""

    max_output_tokens_override: int | None = None
    """Explicit ``max_tokens`` override set during max-output-tokens
    recovery.  Cleared after one turn."""

    reactive_compacted: bool = False
    """True when a reactive compact has been performed in the current turn."""

    # 鈹€鈹€ Session metadata cache 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    last_backend_usage: dict[str, Any] | None = None
    """Token usage from the most recent backend.chat() call."""

    # 鈹€鈹€ Factory / reset 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    @classmethod
    def create(
        cls,
        turn_count: int = 0,
        chain_id: str = "",
        *,
        event_bus: Any = None,
        session_id: str = "",
    ) -> LoopState:
        """Create a fresh LoopState for a new ``_run_impl()`` invocation.

        ``event_bus`` (the session's event backbone) and ``session_id`` are
        forwarded to the embedded :class:`TransitionHistory` so recorded
        transitions are mirrored onto the backbone.
        """
        state = cls(
            turn_count=turn_count,
            chain_id=chain_id,
        )
        state.transitions.event_bus = event_bus
        state.transitions.session_id = session_id
        return state

    def reset_for_new_turn(self) -> None:
        """Reset per-turn counters.  Called at the start of each turn.

        Keeps cross-turn state (slot_escalated, chain_id, chain_depth).
        """
        self.empty_response_retry_count = 0
        self.truncated_tool_call_retry_count = 0
        self.unknown_error_retry_count = 0
        self.streaming_tool_results.clear()
        self.reactive_compacted = False
        self.last_backend_usage = None

    # 鈹€鈹€ Snapshot 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def snapshot(self) -> dict[str, Any]:
        """Return a diagnostic snapshot of the current loop state.

        Safe to call at any point during ``_run_impl()``.  Returns a flat
        dict suitable for logging, telemetry, or debug UI.
        """
        return {
            "turn_count": self.turn_count,
            "chain_id": self.chain_id,
            "chain_depth": self.chain_depth,
            "transitions": self.transitions.to_dict(),
            "transition_count": len(self.transitions),
            "recovery_count": self.transitions.recovery_count,
            "last_transition": self.transitions.last.value if self.transitions.last else None,
            "counters": {
                "empty_response_retry": self.empty_response_retry_count,
                "truncated_tool_call_retry": self.truncated_tool_call_retry_count,
                "max_output_tokens_recovery": self.max_output_tokens_recovery_count,
                "unknown_error_retry": self.unknown_error_retry_count,
            },
            "slot": {
                "escalated": self.slot_escalated,
                "max_output_tokens_override": self.max_output_tokens_override,
            },
            "reactive_compacted": self.reactive_compacted,
            "streaming_results_pending": len(self.streaming_tool_results),
            "last_backend_usage": self.last_backend_usage is not None,
        }
