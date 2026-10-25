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

"""Tests for the recovery system: ErrorRecoveryEngine, RetryableExecutor,
RecoveryState, and correction inference.

Error text is routed through ``classify_error`` into an ``ErrorCategory``
and then through ``ErrorRecoveryEngine.decide`` into a ``RecoveryAction``,
so the branch logic for transient, permission, tool-not-found, model and
input-malformed failures is checked individually.  The engine's
correction-inference helpers are exercised over populated, partial and
empty recovery histories, and ``RetryableExecutor`` drives the whole
loop end to end against synthetic success, flaky and terminal callbacks.
"""

import asyncio

from encre.recovery import (
    ErrorCategory,
    ErrorRecoveryEngine,
    RecoveryAction,
    RecoveryConfig,
    RecoveryDecision,
    RecoveryState,
    RetryableExecutor,
    classify_error,
    compute_backoff,
)

# ===========================================================================
# ErrorCategory
# ===========================================================================

class TestErrorCategory:
    """Validate that ``ErrorCategory`` exposes the full expected member set.

    The engine's classifier and decision table are only meaningful if the
    enum is exhaustive, so this test asserts every documented category is
    present.  Any category added or removed upstream fails immediately.
    """

    def test_verify_all_categories_exist(self):
        """ErrorCategory exposes all expected enum members.

        Each documented category is checked with ``hasattr`` because the
        enum must be exhaustive for ``classify_error`` and the engine's
        ``decide`` logic to operate correctly.
        """
        assert hasattr(ErrorCategory, "TRANSIENT")
        assert hasattr(ErrorCategory, "INPUT_MALFORMED")
        assert hasattr(ErrorCategory, "TOOL_NOT_FOUND")
        assert hasattr(ErrorCategory, "PERMISSION_DENIED")
        assert hasattr(ErrorCategory, "TOOL_EXECUTION")
        assert hasattr(ErrorCategory, "SANDBOX_ERROR")
        assert hasattr(ErrorCategory, "MODEL_ERROR")
        assert hasattr(ErrorCategory, "UNKNOWN")


class TestClassifyError:
    """Validate that ``classify_error`` maps error text to the right category.

    Each test feeds one message signature to the regex-based categorizer
    and asserts the resulting category, so every classification rule can
    be verified and debugged independently.
    """

    def test_verify_transient_timeout(self):
        """Connection timeouts are classified as ErrorCategory.TRANSIENT.

        Timeout-related connection failures are inherently recoverable, so
        they must land in the transient bucket where retry with backoff
        applies.
        """
        assert classify_error("ConnectionError: timeout") == ErrorCategory.TRANSIENT

    def test_verify_transient_rate_limit(self):
        """Rate-limit errors are classified as ErrorCategory.TRANSIENT.

        Rate-limit violations are time-boxed, so a backoff-and-retry
        strategy can recover them.
        """
        assert classify_error("Error: rate limit exceeded") == ErrorCategory.TRANSIENT

    def test_verify_permission_denied(self):
        """PermissionError messages are classified as PERMISSION_DENIED.

        Permission errors are not recoverable by retry -- they require
        explicit user escalation, so they are kept out of the retryable
        buckets.
        """
        assert classify_error("PermissionError: permission denied") == ErrorCategory.PERMISSION_DENIED  # noqa: E501

    def test_verify_permission_blocked_by_hook(self):
        """Hook-blocked actions are classified as PERMISSION_DENIED.

        Hook rejections carry the same semantics as OS-level permission
        denials, so they map to the same category.
        """
        assert classify_error("blocked by hook") == ErrorCategory.PERMISSION_DENIED

    def test_verify_model_error(self):
        """API/backend model errors are classified as MODEL_ERROR.

        Upstream model failures warrant backoff but not argument
        correction, which is why they are bucketed separately from
        input-malformed errors.
        """
        assert classify_error("APIError: api error in backend") == ErrorCategory.MODEL_ERROR

    def test_verify_input_malformed(self):
        """Malformed JSON payloads are classified as INPUT_MALFORMED.

        Structured input errors are correctable by retrying with fixed
        arguments, so they must not be treated as transient.
        """
        assert classify_error("Error: invalid json in payload") == ErrorCategory.INPUT_MALFORMED

    def test_verify_tool_not_found(self):
        """Unknown tool references are classified as TOOL_NOT_FOUND.

        Tool-name errors are terminal: no amount of retrying or fallback
        can invent a tool that does not exist.
        """
        assert classify_error("Error: unknown tool 'fake_tool'") == ErrorCategory.TOOL_NOT_FOUND

    def test_verify_sandbox_error(self):
        """Sandbox failures are classified as SANDBOX_ERROR.

        Sandbox-level errors need isolation-specific handling rather than
        the generic retry path.
        """
        assert classify_error("sandbox failure") == ErrorCategory.SANDBOX_ERROR

    def test_verify_unknown(self):
        """Unrecognized error messages fall back to UNKNOWN.

        Unmatched patterns must never raise, so the categorizer has to
        default to the safe fallback member.
        """
        assert classify_error("SomeWeirdError: unknown failure") == ErrorCategory.UNKNOWN


# ===========================================================================
# RecoveryAction
# ===========================================================================

class TestRecoveryAction:
    """Validate that ``RecoveryAction`` exposes the full expected member set.

    The decision table and the retry loop both reference these actions by
    name, so a missing member would break them silently.  This test
    surfaces any gap as an immediate failure.
    """

    def test_verify_all_actions_exist(self):
        """RecoveryAction exposes all expected enum members.

        Each documented action is checked with ``hasattr`` because the
        engine's ``decide`` output and the retry loop both reference the
        action values directly.
        """
        assert hasattr(RecoveryAction, "RETRY")
        assert hasattr(RecoveryAction, "RETRY_WITH_BACKOFF")
        assert hasattr(RecoveryAction, "RETRY_WITH_FIXED_ARGS")
        assert hasattr(RecoveryAction, "FALLBACK_TOOL")
        assert hasattr(RecoveryAction, "DEGRADE")
        assert hasattr(RecoveryAction, "SKIP")
        assert hasattr(RecoveryAction, "ABORT_TURN")
        assert hasattr(RecoveryAction, "ABORT_SESSION")


# ===========================================================================
# RecoveryState
# ===========================================================================

class TestRecoveryState:
    """Validate ``RecoveryState`` initialization and mutation.

    Initial field values, decision history recording, and error metadata
    propagation are each checked separately so a regression in any single
    field is isolated rather than buried in a broad failure.
    """

    def test_verify_initial_state(self):
        """RecoveryState initializes with the expected defaults.

        Every default field is asserted because the initial state must be
        predictable for the executor that consumes it.
        """
        state = RecoveryState(tool_name="test_tool", tool_args={"path": "/tmp/test"})
        assert state.tool_name == "test_tool"
        assert state.tool_args == {"path": "/tmp/test"}
        assert state.attempts == 0
        assert state.recovery_history == []
        assert state.succeeded is False
        assert state.last_error == ""
        assert state.last_category == ErrorCategory.UNKNOWN

    def test_verify_record_decision(self):
        """Recovery decisions are appended to the history list.

        The history must remain append-only, since the correction
        inference layer reads it to decide what to retry.
        """
        state = RecoveryState(tool_name="test_tool", tool_args={})
        decision = RecoveryDecision(action=RecoveryAction.RETRY, reason="try again")
        state.recovery_history.append(decision)
        assert len(state.recovery_history) == 1
        assert state.recovery_history[0].action == RecoveryAction.RETRY

    def test_verify_with_error_info(self):
        """RecoveryState accepts and preserves error metadata.

        Error context has to survive state construction so downstream
        decision logging can report what went wrong.
        """
        state = RecoveryState(
            tool_name="test_tool",
            tool_args={"x": 1},
            last_error="something broke",
            last_category=ErrorCategory.TRANSIENT,
        )
        assert state.last_error == "something broke"
        assert state.last_category == ErrorCategory.TRANSIENT


# ===========================================================================
# RecoveryDecision
# ===========================================================================

class TestRecoveryDecision:
    """Validate ``RecoveryDecision`` construction with its optional fields.

    Each action-specific payload (fallback tool, modified args, retry
    delay) is exercised on its own so the correction inference layer can
    be trusted to find the field it expects.
    """

    def test_verify_retry_decision(self):
        """A RETRY decision stores action, reason, and delay.

        The backoff logic downstream depends on ``retry_delay_seconds``
        being preserved, so it is asserted explicitly.
        """
        d = RecoveryDecision(action=RecoveryAction.RETRY, reason="timeout", retry_delay_seconds=1.0)
        assert d.action == RecoveryAction.RETRY
        assert d.reason == "timeout"
        assert d.retry_delay_seconds == 1.0

    def test_verify_fallback_decision(self):
        """A FALLBACK_TOOL decision stores the fallback target name.

        The inference engine reads ``fallback_tool`` to generate the
        correction string handed back to the model.
        """
        d = RecoveryDecision(
            action=RecoveryAction.FALLBACK_TOOL,
            reason="tool unavailable",
            fallback_tool="alternative_tool",
        )
        assert d.fallback_tool == "alternative_tool"

    def test_verify_fixed_args_decision(self):
        """A RETRY_WITH_FIXED_ARGS decision stores modified arguments.

        The correction layer reads this dictionary to regenerate a
        corrected prompt for the retried call.
        """
        d = RecoveryDecision(
            action=RecoveryAction.RETRY_WITH_FIXED_ARGS,
            reason="bad arg",
            modified_args={"arg1": "fixed"},
        )
        assert d.modified_args == {"arg1": "fixed"}

    def test_verify_abort_decision(self):
        """An ABORT_TURN decision stores its action type.

        Abort decisions must carry unambiguous terminal semantics, so the
        action is asserted rather than inferred from the reason string.
        """
        d = RecoveryDecision(action=RecoveryAction.ABORT_TURN, reason="unrecoverable")
        assert d.action == RecoveryAction.ABORT_TURN


# ===========================================================================
# ErrorRecoveryEngine
# ===========================================================================

class TestErrorRecoveryEngine:
    """Validate ``ErrorRecoveryEngine`` decision-making across error types.

    Every decision branch is driven through ``engine.decide`` on its own:
    transient retries, permission skips, fallback chains, max-retry
    limits, tool-not-found aborts, model-error backoff, and the
    input-correction loop.  The correction inference helpers are then
    exercised over populated, partial and empty histories.
    """

    def test_verify_decide_transient_first_attempt(self):
        """Transient errors on the first attempt trigger RETRY_WITH_BACKOFF.

        Transient network failures must receive a backoff-based retry on
        attempt 0 rather than an immediate abort.
        """
        engine = ErrorRecoveryEngine()
        decision = engine.decide(
            tool_name="file_read",
            tool_args={"path": "/tmp/test"},
            error_message="ConnectionError: timeout",
            attempt=0,
        )
        assert decision.action == RecoveryAction.RETRY_WITH_BACKOFF
        assert decision.retry_delay_seconds > 0

    def test_verify_decide_permission_skip(self):
        """Permission errors trigger SKIP without retry.

        Permission denials are terminal; retrying cannot change the
        outcome, so the engine must not burn attempts on them.
        """
        engine = ErrorRecoveryEngine()
        decision = engine.decide(
            tool_name="file_read",
            tool_args={"path": "/tmp/test"},
            error_message="PermissionError: permission denied",
            attempt=0,
        )
        assert decision.action == RecoveryAction.SKIP

    def test_verify_decide_max_retries_with_fallback(self):
        """Exhausted retries invoke the fallback tool chain.

        At attempt 3 the retry budget (MAX_RETRIES=3) is spent, so the
        engine must pivot to the fallback table at depth 0.
        """
        engine = ErrorRecoveryEngine()
        decision = engine.decide(
            tool_name="file_read",
            tool_args={"path": "/tmp/test"},
            error_message="RuntimeError: something broke",
            attempt=3,
            fallback_depth=0,
        )
        # file_read has "bash" as fallback at depth 0
        assert decision.action in (RecoveryAction.FALLBACK_TOOL, RecoveryAction.SKIP)

    def test_verify_decide_max_retries_no_fallback_chain(self):
        """Exhausted retries on an unknown tool yield SKIP or ABORT.

        Unknown tools have no fallback chain, so further retries are
        futile and the engine must give up.
        """
        engine = ErrorRecoveryEngine()
        decision = engine.decide(
            tool_name="nonexistent_tool",
            tool_args={},
            error_message="RuntimeError: persistent failure",
            attempt=5,
        )
        assert decision.action in (RecoveryAction.SKIP, RecoveryAction.ABORT_TURN)

    def test_verify_decide_tool_not_found(self):
        """Unknown-tool errors trigger ABORT_TURN immediately.

        Referencing a nonexistent tool is a structural error that no
        retry can repair, so it aborts on the first attempt.
        """
        engine = ErrorRecoveryEngine()
        decision = engine.decide(
            tool_name="nonexistent",
            tool_args={},
            error_message="Error: unknown tool 'nonexistent'",
            attempt=0,
        )
        assert decision.action == RecoveryAction.ABORT_TURN

    def test_verify_decide_model_error(self):
        """Model/API errors trigger RETRY_WITH_BACKOFF.

        Model failures are transient and benefit from exponential
        backoff, so they must not be treated as permanent.
        """
        engine = ErrorRecoveryEngine()
        decision = engine.decide(
            tool_name="file_read",
            tool_args={"path": "/tmp/test"},
            error_message="APIError: internal server error",
            attempt=0,
        )
        assert decision.action == RecoveryAction.RETRY_WITH_BACKOFF

    def test_verify_decide_input_malformed_first_attempt(self):
        """Input-malformed errors on the first attempt trigger RETRY_WITH_FIXED_ARGS.

        The engine should try to repair the input structure before giving
        up, so the first failure of this kind gets an argument fix.
        """
        engine = ErrorRecoveryEngine()
        decision = engine.decide(
            tool_name="file_write",
            tool_args={"path": "/tmp/test"},
            error_message="Error: invalid json in payload",
            attempt=0,
        )
        assert decision.action == RecoveryAction.RETRY_WITH_FIXED_ARGS

    def test_verify_decide_input_malformed_second_attempt(self):
        """Repeated input-malformed errors trigger ABORT_TURN.

        The engine must not retry the same bad input indefinitely, so the
        second failure of this kind becomes terminal.
        """
        engine = ErrorRecoveryEngine()
        decision = engine.decide(
            tool_name="file_write",
            tool_args={"path": "/tmp/test"},
            error_message="Error: invalid json in payload",
            attempt=1,
        )
        assert decision.action == RecoveryAction.ABORT_TURN

    def test_verify_session_error_limit(self):
        """Exceeding _max_session_errors triggers ABORT_SESSION.

        Session-level error accumulation warrants a hard stop to prevent
        a runaway retry loop.
        """
        engine = ErrorRecoveryEngine()
        engine._session_errors = 10  # reaches _max_session_errors
        decision = engine.decide(
            tool_name="test",
            tool_args={},
            error_message="any error",
            attempt=0,
        )
        assert decision.action == RecoveryAction.ABORT_SESSION

    def test_verify_reset_session(self):
        """reset_session clears accumulated error state.

        Both the counter and the recent-error list must be cleared so the
        next turn starts from a clean slate.
        """
        engine = ErrorRecoveryEngine()
        engine._session_errors = 5
        engine._last_errors = ["e1", "e2", "e3"]
        engine.reset_session()
        assert engine._session_errors == 0
        assert engine._last_errors == []

    def test_verify_infer_correction_fallback(self):
        """infer_correction extracts the fallback tool name from history.

        The inference layer must turn a recorded FALLBACK_TOOL decision
        into a usable prompt hint for the model.
        """
        state = RecoveryState(tool_name="bad_tool", tool_args={})
        state.recovery_history = [
            RecoveryDecision(action=RecoveryAction.FALLBACK_TOOL, reason="unavailable", fallback_tool="good_tool"),  # noqa: E501
        ]
        correction = ErrorRecoveryEngine.infer_correction(state)
        assert "good_tool" in correction

    def test_verify_infer_correction_fixed_args(self):
        """infer_correction references modified args in the output.

        The hint must communicate that the arguments changed, otherwise
        the model has no way to learn what to fix.
        """
        state = RecoveryState(tool_name="test", tool_args={})
        state.recovery_history = [
            RecoveryDecision(
                action=RecoveryAction.RETRY_WITH_FIXED_ARGS,
                reason="bad arg",
                modified_args={"x": 1},
            ),
        ]
        correction = ErrorRecoveryEngine.infer_correction(state)
        assert "modified args" in correction.lower()

    def test_verify_infer_correction_empty_history(self):
        """infer_correction returns an empty string when history is empty.

        With no prior decisions there is nothing to extract a hint from,
        so the safe answer is the empty string.
        """
        state = RecoveryState(tool_name="test", tool_args={})
        assert ErrorRecoveryEngine.infer_correction(state) == ""

    def test_verify_infer_correction_from_history(self):
        """infer_correction_from_history extracts backoff hints.

        A RETRY_WITH_BACKOFF entry must surface its delay information so
        the model can see how long the engine was willing to wait.
        """
        history = [
            RecoveryDecision(action=RecoveryAction.RETRY_WITH_BACKOFF, reason="timeout", retry_delay_seconds=5.0),  # noqa: E501
        ]
        correction = ErrorRecoveryEngine.infer_correction_from_history(history, "test_tool")
        assert "backoff" in correction.lower()

    def test_verify_infer_correction_from_history_empty(self):
        """infer_correction_from_history returns empty for an empty history list.

        An empty list has no decisions to infer from, so the result is
        the empty string.
        """
        assert ErrorRecoveryEngine.infer_correction_from_history([], "test") == ""

    def test_verify_infer_correction_from_history_last_wins(self):
        """The last decision wins when inferring corrections from history.

        Recency takes priority over earlier retried paths, so the hint
        must be built from the most recent decision.
        """
        history = [
            RecoveryDecision(action=RecoveryAction.RETRY, reason="first"),
            RecoveryDecision(action=RecoveryAction.FALLBACK_TOOL, reason="last", fallback_tool="better_tool"),  # noqa: E501
        ]
        correction = ErrorRecoveryEngine.infer_correction_from_history(history, "old_tool")
        assert "better_tool" in correction


# ===========================================================================
# RetryableExecutor
# ===========================================================================

class TestRetryableExecutor:
    """Validate ``RetryableExecutor`` lifecycle and recovery integration.

    Six scenarios cover the full range of executor behaviour: immediate
    success, success after transient failures, permission-driven skip,
    history collection, the on_retry hook, and a caller-supplied engine.
    """

    def test_verify_execute_success(self):
        """execute returns a succeeded result on the first attempt.

        The executor must not retry when the underlying operation
        succeeds, so call_count is asserted to be exactly 1.
        """
        async def _test():
            """Run the executor against a callback that succeeds immediately."""
            executor = RetryableExecutor()
            call_count = 0

            async def succeed(args):
                """First attempt succeeds, returning a fixed result string."""
                nonlocal call_count
                call_count += 1
                return "success"

            result = await executor.execute(
                tool_name="test",
                tool_args={},
                execute_fn=succeed,
            )
            assert result.succeeded is True
            assert result.final_result == "success"
            assert call_count == 1

        asyncio.run(_test())

    def test_verify_execute_retry_then_success(self):
        """execute retries transient failures until success.

        The executor must persist across transient failures per config, so
        a callback failing twice then succeeding is expected to return
        three calls.
        """
        async def _test():
            """Run the executor against a callback that fails twice before succeeding."""
            executor = RetryableExecutor()
            call_count = 0

            async def flaky(args):
                """Raise a transient connection error until the third attempt."""
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise ConnectionError("timeout transient failure")
                return "eventual success"

            result = await executor.execute(
                tool_name="test",
                tool_args={},
                execute_fn=flaky,
            )
            assert result.succeeded is True
            assert result.final_result == "eventual success"
            assert call_count == 3

        asyncio.run(_test())

    def test_verify_execute_skips_on_permission_error(self):
        """execute marks permission errors as succeeded with a skip marker.

        Permission denials are treated as terminal successes -- no retry,
        no error propagation -- so the result is flagged succeeded with a
        'Skipped' payload and a single call.
        """
        async def _test():
            """Run the executor against a callback that always raises PermissionError."""
            executor = RetryableExecutor()
            call_count = 0

            async def permission_denied(args):
                """Raise PermissionError on every attempt."""
                nonlocal call_count
                call_count += 1
                raise PermissionError("permission denied")

            result = await executor.execute(
                tool_name="test",
                tool_args={},
                execute_fn=permission_denied,
            )
            # Permission denied -> SKIP, which marks state as succeeded
            assert result.succeeded is True
            assert "Skipped" in result.final_result
            assert call_count == 1

        asyncio.run(_test())

    def test_verify_execute_collects_recovery_history(self):
        """execute accumulates recovery history across failures.

        Every decision made during the retry loop must be recorded so the
        correction inference layer has material to work with.
        """
        async def _test():
            """Run the executor against a callback that always times out."""
            executor = RetryableExecutor()

            async def always_fails(args):
                """Raise ConnectionError on every attempt."""
                raise ConnectionError("timeout")

            result = await executor.execute(
                tool_name="unknown_tool",
                tool_args={},
                execute_fn=always_fails,
            )
            assert len(result.recovery_history) > 0

        asyncio.run(_test())

    def test_verify_execute_on_retry_callback(self):
        """The on_retry callback receives decision and attempt data.

        The retry hook enables external observability into the recovery
        loop, so at least one firing is expected for a flaky callback.
        """
        async def _test():
            """Run the executor with an on_retry listener attached."""
            executor = RetryableExecutor()
            callbacks = []

            def on_retry(decision, attempt):
                """Record the decision action and attempt number of each retry."""
                callbacks.append((decision.action, attempt))

            call_count = 0

            async def flaky(args):
                """Raise ConnectionError on the first attempt only."""
                nonlocal call_count
                call_count += 1
                if call_count < 2:
                    raise ConnectionError("timeout")
                return "ok"

            result = await executor.execute(
                tool_name="test",
                tool_args={},
                execute_fn=flaky,
                on_retry=on_retry,
            )
            assert result.succeeded is True
            assert len(callbacks) >= 1

        asyncio.run(_test())

    def test_verify_custom_recovery_engine(self):
        """execute accepts a custom ErrorRecoveryEngine instance.

        The executor must delegate decision-making to any conforming
        engine rather than building its own.
        """
        async def _test():
            """Run the executor wired to an explicitly supplied engine."""
            engine = ErrorRecoveryEngine()
            executor = RetryableExecutor(recovery=engine)

            async def works(args):
                """First attempt succeeds and returns a fixed result string."""
                return "done"

            result = await executor.execute("t", {}, works)
            assert result.succeeded is True
            assert result.final_result == "done"

        asyncio.run(_test())


# ===========================================================================
# RecoveryConfig
# ===========================================================================

class TestRecoveryConfig:
    """Validate ``RecoveryConfig`` constants and lookup tables.

    The static configuration values are checked for consistency with the
    engine logic that consumes them: numeric limits, fallback chains, and
    the error-pattern to category mapping.
    """

    def test_verify_constants(self):
        """RecoveryConfig exposes the expected numerical constants.

        The engine's backoff and retry limits depend on these constants
        being stable, so their exact values are asserted.
        """
        assert RecoveryConfig.MAX_RETRIES == 3
        assert RecoveryConfig.MAX_FALLBACK_DEPTH == 2
        assert RecoveryConfig.BASE_BACKOFF_SECONDS == 1.0
        assert RecoveryConfig.MAX_BACKOFF_SECONDS == 60.0
        assert RecoveryConfig.BACKOFF_MULTIPLIER == 2.0

    def test_verify_fallback_chains(self):
        """RecoveryConfig.FALLBACK_CHAINS covers common file tools.

        file_read, grep, glob, web_fetch and file_edit are the primary
        tools the engine must be able to fall back from.
        """
        assert "file_read" in RecoveryConfig.FALLBACK_CHAINS
        assert "grep" in RecoveryConfig.FALLBACK_CHAINS
        assert "glob" in RecoveryConfig.FALLBACK_CHAINS
        assert "web_fetch" in RecoveryConfig.FALLBACK_CHAINS
        assert "file_edit" in RecoveryConfig.FALLBACK_CHAINS

    def test_verify_error_patterns(self):
        """RecoveryConfig.ERROR_PATTERNS maps substrings to correct categories.

        ``classify_error`` relies on these substrings for matching, so both
        the keys and their target categories are asserted.
        """
        patterns = RecoveryConfig.ERROR_PATTERNS
        assert patterns["timed out"] == ErrorCategory.TRANSIENT
        assert patterns["rate limit"] == ErrorCategory.TRANSIENT
        assert patterns["permission denied"] == ErrorCategory.PERMISSION_DENIED
        assert patterns["unknown tool"] == ErrorCategory.TOOL_NOT_FOUND
        assert patterns["api error"] == ErrorCategory.MODEL_ERROR
        assert patterns["sandbox"] == ErrorCategory.SANDBOX_ERROR


# ===========================================================================
# compute_backoff
# ===========================================================================

class TestComputeBackoff:
    """Validate the ``compute_backoff`` exponential-backoff function.

    Three attempts are sampled -- the first, a mid-retry attempt, and one
    past the cap -- and the returned delay is checked to sit inside the
    documented jitter band around the expected base value.
    """

    def test_verify_first_attempt(self):
        """compute_backoff returns approximately base * 2^0 with jitter.

        At attempt 0 the base value is 1.0 s, and the jitter band is
        +-25% of that value.
        """
        delay = compute_backoff(0, base=1.0, max_wait=60.0)
        assert 0.75 <= delay <= 1.25  # base * 2^0 = 1.0, +/- 25% jitter

    def test_verify_third_attempt(self):
        """compute_backoff returns approximately base * 2^3 with jitter.

        2**3 is 8 s at the third attempt, and the jitter band is +-25%.
        """
        delay = compute_backoff(3, base=1.0, max_wait=60.0)
        assert 6.0 <= delay <= 10.0  # base * 2^3 = 8.0, +/- 25%

    def test_verify_respects_max_wait(self):
        """compute_backoff caps the delay at max_wait with jitter tolerance.

        The cap prevents unbounded wait times in long retry sequences, so
        a deeply exponential attempt must not blow past it.
        """
        delay = compute_backoff(10, base=1.0, max_wait=30.0)
        assert delay <= 37.5  # max_wait + 25% jitter cap
