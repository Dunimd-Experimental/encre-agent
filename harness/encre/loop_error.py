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

"""Unified error orchestrator for the agent loop.

Replaces the inline error recovery logic scattered across
``_run_impl()`` with a single ``ErrorOrchestrator`` that owns all
recovery state and produces typed ``RecoveryAction`` values.

Priority order (first-match wins):
  1. Reactive compact    (context overflow)
  2. Model fallback      (rate limit / overload)
  3. Retryable           (network retry with backoff, credential rotate)
  4. Auth                (surface immediately)
  5. Unknown             (one silent retry)
  6. Consumed            (recovery exhausted -- withhold)
  7. Terminal            (unrecoverable -- surface to user)
"""

import random
from dataclasses import dataclass
from enum import Enum
from typing import Any

from encre.recovery import RetryBudget

# --- Recovery limits ---

MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3
EMPTY_RESPONSE_RETRY_LIMIT = 2
TRUNCATED_TOOL_CALL_RETRY_LIMIT = 2
UNKNOWN_ERROR_RETRY_LIMIT = 1
NETWORK_RETRY_LIMIT = 2
SERVER_RETRY_LIMIT = 2
MAX_TOTAL_RECOVERIES_PER_TURN = 6
ESCALATED_MAX_TOKENS = 64_000


class RecoveryAction(str, Enum):
    """What the caller (the model phase) should do next."""

    CONTINUE = "continue"  # re-issue the backend call (one attempt reserved)
    FALLBACK_CONTINUE = "fallback_continue"  # switch model and retry
    COMPACT_CONTINUE = "compact_continue"  # compact and retry
    CONSUMED = "consumed"  # withhold the error, finish the turn quietly
    RELEASE = "release"  # surface error to user (terminal)
    RETRY = "retry"  # retry after delay


@dataclass
class RecoveryDecision:
    """Result of an error classification and recovery attempt."""

    action: RecoveryAction
    delay: float = 0.0
    message: str = ""
    error_code: str = "unknown"
    error_category: str = "unknown"
    detail: str = ""


# --- Post-stream recovery decisions ---


class PostStreamAction(str, Enum):
    """What to do after the model finishes streaming."""

    CONTINUE = "continue"  # retry the model call
    FALL_THROUGH = "fall_through"  # proceed with normal turn processing
    STOP = "stop"  # end the session gracefully


@dataclass
class PostStreamDecision:
    """Result of post-stream recovery classification."""

    action: PostStreamAction
    message: str = ""
    detail: str = ""


# --- The orchestrator ---


class ErrorOrchestrator:
    """Single owner of error recovery state for one agent loop.

    Usage in ``_run_impl``::

        # Before the backend call:
        result = orchestrator.handle_backend_exception(exc, ...)
        if result.action == RecoveryAction.CONTINUE:
            continue
        elif result.action == RecoveryAction.CONSUMED:
            _error_consumed = True
            continue
        elif result.action == RecoveryAction.RELEASE:
            yield create_finish("error", error=result.message, ...)
            return

        # After streaming:
        result = orchestrator.handle_post_stream(...)
        if result.action == PostStreamAction.CONTINUE:
            continue
        elif result.action == PostStreamAction.STOP:
            yield create_finish("stop")
            return
    """

    def __init__(self, loop: Any = None, budget: Any = None) -> None:
        self.loop = loop
        # Shared numeric retry budget (see encre.recovery.RetryBudget).  Each
        # granted model-round recovery spends one "model" unit; when the
        # budget is gone the orchestrator releases instead of looping.
        self.budget = budget if budget is not None else RetryBudget()

        # Reactive compact guard (once per turn)
        self._reactive_compacted = False

        # Max-output-tokens recovery
        self._slot_escalated = False
        self._max_output_tokens_recovery_count = 0

        # Empty response recovery
        self._empty_response_retry_count = 0

        # Truncated tool call recovery
        self._truncated_tool_call_retry_count = 0

        # Unknown error recovery
        self._unknown_error_retry_count = 0

        # Network error recovery
        self._network_retry_count = 0

        # Server error recovery (502/503/504 — transient, retry before fallback)
        self._server_retry_count = 0

        # Per-turn recovery counter (global cap)
        self._recoveries_this_turn = 0

        # Fallback tracking
        self._attempt_fallback = False

        # Fallback switches granted this turn (bounded by
        # ``model_pool_max_fallbacks`` when the user configured a cap).
        self._pool_fallbacks_this_turn = 0

    # --- Turn lifecycle ---

    def reset_for_new_turn(self) -> None:
        """Reset per-turn recovery counters."""
        self._reactive_compacted = False
        self._empty_response_retry_count = 0
        self._truncated_tool_call_retry_count = 0
        self._unknown_error_retry_count = 0
        self._network_retry_count = 0
        self._server_retry_count = 0
        self._recoveries_this_turn = 0
        self._attempt_fallback = False
        self._pool_fallbacks_this_turn = 0
        self.budget.reset_turn()

    def _grant_model_retry(self) -> bool:
        """Spend one "model" budget unit for a retry-family decision.

        Returns:
            ``True`` when the retry may proceed; ``False`` when the numeric
            budget is exhausted and the caller must release instead.
        """
        return self.budget.try_spend("model")

    def can_attempt_recovery(self) -> bool:
        return self._recoveries_this_turn < MAX_TOTAL_RECOVERIES_PER_TURN

    # --- Backend exception handling ---

    def handle_backend_exception(
        self,
        exc: BaseException,  # noqa: ARG002 -- classification uses error_code/category
        *,
        error_code: str = "unknown",
        error_category: str = "unknown",
        is_context_overflow: bool = False,
        is_rate_limit: bool = False,
        config: Any = None,
        compact_engine: Any = None,
        session: Any = None,  # noqa: ARG002 -- reserved for session-scoped recovery
        backend: Any = None,
        system_prompt: str = "",  # noqa: ARG002 -- reserved for recovery messages
        tool_call_buffers: dict | None = None,  # noqa: ARG002 -- reserved
        turn_count: int = 0,  # noqa: ARG002 -- reserved for audit
    ) -> RecoveryDecision:
        """Classify *exc* and decide what action the loop should take.

        Priority order:
          1. Context overflow   → compact and retry
          2. Rate limit         → fallback model if available
          3. Network error      → retry with backoff
          4. Auth error         → surface immediately
          5. Unknown error      → one silent retry
          6. Otherwise          → release to user
        """
        if not self.can_attempt_recovery():
            # The per-turn recovery budget is spent: withhold the error
            # (priority 6) instead of surfacing it, so the tail of a turn
            # that already burned every recovery does not end in an error
            # banner.  ``RecoveryAction.CONSUMED`` is the only action that
            # means "withhold": the loop flags the turn and finishes quietly.
            return RecoveryDecision(
                RecoveryAction.CONSUMED,
                error_code=error_code,
                error_category=error_category,
                detail="recovery budget exhausted",
            )

        # 1. Reactive compact
        if is_context_overflow and compact_engine is not None and backend is not None:
            if not self._grant_model_retry():
                return RecoveryDecision(
                    RecoveryAction.RELEASE,
                    error_code=error_code,
                    error_category=error_category,
                    detail="model retry budget exhausted",
                )
            self._recoveries_this_turn += 1
            return RecoveryDecision(
                RecoveryAction.COMPACT_CONTINUE,
                error_code=error_code,
                error_category=error_category,
                detail="context overflow",
            )

        # 2. Model fallback
        if (is_rate_limit or error_category == "model") and config is not None:
            from encre.recovery_loop import can_fallback

            _pool_on = getattr(config, "model_pool_enabled", True)
            _fallback_on = getattr(config, "model_pool_fallback_enabled", False)
            if _pool_on and _fallback_on and can_fallback(config):
                _max_fb = int(getattr(config, "model_pool_max_fallbacks", 0) or 0)
                if _max_fb > 0 and self._pool_fallbacks_this_turn >= _max_fb:
                    return RecoveryDecision(
                        RecoveryAction.RELEASE,
                        error_code=error_code,
                        error_category=error_category,
                        detail="pool fallback limit reached",
                    )
                if not self._grant_model_retry():
                    return RecoveryDecision(
                        RecoveryAction.RELEASE,
                        error_code=error_code,
                        error_category=error_category,
                        detail="model retry budget exhausted",
                    )
                self._recoveries_this_turn += 1
                self._pool_fallbacks_this_turn += 1
                self._attempt_fallback = True
                return RecoveryDecision(
                    RecoveryAction.FALLBACK_CONTINUE,
                    error_code=error_code,
                    error_category=error_category,
                    detail="model fallback",
                )

        # 3. Network retry
        if error_category == "network" and self._network_retry_count < NETWORK_RETRY_LIMIT:
            self._network_retry_count += 1
            self._recoveries_this_turn += 1
            delay = min(0.5 * (2 ** (self._network_retry_count - 1)), 10.0) + random.uniform(0, 0.25)
            return RecoveryDecision(
                RecoveryAction.RETRY,
                delay=delay,
                error_code=error_code,
                error_category=error_category,
                detail=f"network retry {self._network_retry_count}/{NETWORK_RETRY_LIMIT}",
            )

        # 3.5. Server error retry (502/503/504 — transient, retry with backoff before fallback)
        if error_category == "server" and self._server_retry_count < SERVER_RETRY_LIMIT:
            self._server_retry_count += 1
            self._recoveries_this_turn += 1
            delay = min(0.5 * (2 ** (self._server_retry_count - 1)), 10.0) + random.uniform(0, 0.25)
            return RecoveryDecision(
                RecoveryAction.RETRY,
                delay=delay,
                error_code=error_code,
                error_category=error_category,
                detail=f"server retry {self._server_retry_count}/{SERVER_RETRY_LIMIT}",
            )

        # 3.6. Server error fallback (retries exhausted — switch to next model in pool)
        if error_category == "server" and config is not None:
            from encre.recovery_loop import can_fallback

            _pool_on = getattr(config, "model_pool_enabled", True)
            _fallback_on = getattr(config, "model_pool_fallback_enabled", False)
            if _pool_on and _fallback_on and can_fallback(config):
                _max_fb = int(getattr(config, "model_pool_max_fallbacks", 0) or 0)
                if _max_fb > 0 and self._pool_fallbacks_this_turn >= _max_fb:
                    return RecoveryDecision(
                        RecoveryAction.RELEASE,
                        error_code=error_code,
                        error_category=error_category,
                        detail="pool fallback limit reached",
                    )
                if not self._grant_model_retry():
                    return RecoveryDecision(
                        RecoveryAction.RELEASE,
                        error_code=error_code,
                        error_category=error_category,
                        detail="model retry budget exhausted",
                    )
                self._recoveries_this_turn += 1
                self._pool_fallbacks_this_turn += 1
                self._attempt_fallback = True
                return RecoveryDecision(
                    RecoveryAction.FALLBACK_CONTINUE,
                    error_code=error_code,
                    error_category=error_category,
                    detail="server fallback",
                )

        # 4. Auth → surface
        if error_category == "auth":
            return RecoveryDecision(
                RecoveryAction.RELEASE,
                error_code=error_code,
                error_category=error_category,
                detail="auth error",
            )

        # 5. Unknown → one silent retry
        if error_code == "unknown" and self._unknown_error_retry_count < UNKNOWN_ERROR_RETRY_LIMIT:
            if not self._grant_model_retry():
                return RecoveryDecision(
                    RecoveryAction.RELEASE,
                    error_code=error_code,
                    error_category=error_category,
                    detail="model retry budget exhausted",
                )
            self._unknown_error_retry_count += 1
            self._recoveries_this_turn += 1
            return RecoveryDecision(
                RecoveryAction.CONTINUE,
                error_code=error_code,
                error_category=error_category,
                detail=f"unknown retry {self._unknown_error_retry_count}/{UNKNOWN_ERROR_RETRY_LIMIT}",
            )

        # 6. Terminal
        return RecoveryDecision(
            RecoveryAction.RELEASE,
            error_code=error_code,
            error_category=error_category,
        )

    # --- Post-stream recovery ---

    def handle_post_stream(
        self,
        *,
        finish_reason: str | None = None,
        is_empty: bool = False,
        is_truncated: bool = False,
        tool_call_buffers: dict | None = None,  # noqa: ARG002 -- reserved
        text_parts: list[str] | None = None,  # noqa: ARG002 -- reserved
        thinking_parts: list[str] | None = None,  # noqa: ARG002 -- reserved
        config: Any = None,  # noqa: ARG002 -- reserved for slot escalation
        session: Any = None,  # noqa: ARG002 -- reserved
        turn_count: int = 0,  # noqa: ARG002 -- reserved for audit
    ) -> PostStreamDecision:
        """Classify post-stream conditions and decide next action.

        Priority order:
          1. Max output tokens → slot escalation or recovery message
          2. Empty response    → retry prompt
          3. Truncated tool    → re-issue prompt
          4. Fall through      → normal turn processing
        """
        # 1. Max output tokens
        if finish_reason in ("max_tokens", "length"):
            if not self._slot_escalated:
                self._slot_escalated = True
                self._recoveries_this_turn += 1
                return PostStreamDecision(
                    PostStreamAction.CONTINUE,
                    detail="slot escalation",
                )
            if self._max_output_tokens_recovery_count < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT:
                self._max_output_tokens_recovery_count += 1
                self._recoveries_this_turn += 1
                return PostStreamDecision(
                    PostStreamAction.CONTINUE,
                    detail=f"max_tokens recovery {self._max_output_tokens_recovery_count}/{MAX_OUTPUT_TOKENS_RECOVERY_LIMIT}",
                )

        # 2. Empty response
        if is_empty and self._empty_response_retry_count < EMPTY_RESPONSE_RETRY_LIMIT:
            self._empty_response_retry_count += 1
            self._recoveries_this_turn += 1
            return PostStreamDecision(
                PostStreamAction.CONTINUE,
                detail=f"empty retry {self._empty_response_retry_count}/{EMPTY_RESPONSE_RETRY_LIMIT}",
            )

        # 3. Truncated tool call
        if is_truncated and self._truncated_tool_call_retry_count < TRUNCATED_TOOL_CALL_RETRY_LIMIT:
            self._truncated_tool_call_retry_count += 1
            self._recoveries_this_turn += 1
            return PostStreamDecision(
                PostStreamAction.CONTINUE,
                detail=f"truncated retry {self._truncated_tool_call_retry_count}/{TRUNCATED_TOOL_CALL_RETRY_LIMIT}",
            )

        # 4. Fall through (normal processing or exhausted retries)
        return PostStreamDecision(PostStreamAction.FALL_THROUGH)

    # --- Diagnostic ---

    def snapshot(self) -> dict[str, Any]:
        return {
            "reactive_compacted": self._reactive_compacted,
            "slot_escalated": self._slot_escalated,
            "max_output_tokens_count": self._max_output_tokens_recovery_count,
            "empty_response_count": self._empty_response_retry_count,
            "truncated_tool_call_count": self._truncated_tool_call_retry_count,
            "unknown_error_count": self._unknown_error_retry_count,
            "network_retry_count": self._network_retry_count,
            "server_retry_count": self._server_retry_count,
            "recoveries_this_turn": self._recoveries_this_turn,
            "attempt_fallback": self._attempt_fallback,
        }
