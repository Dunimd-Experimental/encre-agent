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

"""Tests for encre.telemetry: EncreTelemetry aggregation, session summaries,
and the ToolCallRecord, TurnRecord, and RetryRecord dataclasses.

test_security_config.py already covers the basic happy paths for
record_tool_call, record_turn, record_retry, get_summary, flush, reset, and
disabled telemetry; this file adds the edge cases, aggregation math,
reset semantics, and timestamp-ordering invariants.
"""

import os
import tempfile
import time

from encre.telemetry import EncreTelemetry, RetryRecord, ToolCallRecord, TurnRecord

# Redirect telemetry data to a temp directory so tests never pollute the
# real production telemetry directory (~/.dunimd/encre/telemetry/).
_test_telemetry_dir = tempfile.mkdtemp(prefix="encre_test_telemetry_")
os.environ["ENCRE_DATA_DIR"] = _test_telemetry_dir

# ---- Edge Cases: Empty Telemetry ------------------------------------------


class TestEmptyTelemetry:
    """Validate that get_summary and flush return sensible defaults on empty state.

    This class exercises the summary and flush paths with zero recorded
    events across 2 scenarios to ensure the aggregation functions never raise
    on empty lists and always return zeroed metrics rather than NaN or None,
    which downstream dashboards and log formatters expect.
    """

    def test_verify_get_summary_returns_all_zero_fields_when_empty(self):
        """Validate that get_summary returns zeroed counters and empty dicts on no recorded events.

        The test asserts total_tool_calls, total_turns, successful_tool_calls,
        failed_tool_calls, avg_tool_latency_ms, avg_turn_latency_ms, total_events,
        compactions, tool_usage, total_retries, and retry_by_error all equal their
        zero/empty defaults because the summary is consumed by observability
        systems that must handle an empty session without special-casing.
        """
        tel = EncreTelemetry()
        summary = tel.get_summary()
        assert summary["total_tool_calls"] == 0
        assert summary["total_turns"] == 0
        assert summary["successful_tool_calls"] == 0
        assert summary["failed_tool_calls"] == 0
        assert summary["avg_tool_latency_ms"] == 0.0
        assert summary["avg_turn_latency_ms"] == 0.0
        assert summary["total_events"] == 0
        assert summary["compactions"] == 0
        assert summary["tool_usage"] == {}
        assert summary["total_retries"] == 0
        assert summary["retry_by_error"] == {}

    def test_verify_flush_returns_empty_summary_dict_when_no_data(self):
        """Validate that flush returns a dict with zeroed counters when no events have been recorded.

        The test asserts the result is a dict and total_tool_calls == 0 because
        flush is expected to be callable at any time (including before any
        telemetry is captured) without raising or returning a non-dict sentinel.
        """
        tel = EncreTelemetry()
        result = tel.flush()
        assert isinstance(result, dict)
        assert result["total_tool_calls"] == 0


# ---- Tool Call Records ----------------------------------------------------


class TestToolCallRecordFields:
    """Validate the ToolCallRecord default fields and optional overrides.

    This class exercises tokens_used default, error_message default,
    auto-generated timestamp, and explicit error_message setting across
    4 scenarios to ensure the record carries correct defaults and can store
    failure diagnostics without requiring every field to be explicitly
    supplied by the caller.
    """

    def test_verify_default_tokens_used_is_zero(self):
        """Validate that tokens_used defaults to 0 when not supplied.

        The test constructs a ToolCallRecord without tokens_used and asserts
        rec.tokens_used == 0 because the telemetry layer records token usage
        separately via the LLM response path; the tool call record itself
        does not track tokens and must not invent a value.
        """
        rec = ToolCallRecord(tool_name="test", latency_ms=100.0, success=True)
        assert rec.tokens_used == 0

    def test_verify_default_error_message_is_empty_string(self):
        """Validate that error_message defaults to '' when not supplied.

        The test constructs a successful record and asserts rec.error_message
        == '' because the error field is optional and must be distinguishable
        from a missing key; an empty string lets callers check truthiness
        to determine whether an error was recorded.
        """
        rec = ToolCallRecord(tool_name="test", latency_ms=100.0, success=True)
        assert rec.error_message == ""

    def test_verify_timestamp_is_auto_generated_within_call_bounds(self):
        """Validate that timestamp is set automatically between before and after time calls.

        The test records time.time() before and after constructing the record
        and asserts before <= rec.timestamp <= after because the telemetry
        system relies on wall-clock timestamps to compute session duration
        and to order events correctly when multiple agents record concurrently.
        """
        before = time.time()
        rec = ToolCallRecord(tool_name="test", latency_ms=100.0, success=True)
        after = time.time()
        assert before <= rec.timestamp <= after

    def test_verify_error_message_stores_failure_diagnostic(self):
        """Validate that error_message stores the supplied failure string on a failed call.

        The test constructs a failed record with error_message='command not found'
        and asserts both error_message and success match, because the telemetry
        store must preserve the exact diagnostic text so post-mortem analysis
        can attribute failures to specific commands and error patterns.
        """
        rec = ToolCallRecord(
            tool_name="bash",
            latency_ms=0.0,
            success=False,
            error_message="command not found",
        )
        assert rec.error_message == "command not found"
        assert rec.success is False


# ---- Turn Records ---------------------------------------------------------


class TestTurnRecordFields:
    """Validate the TurnRecord default fields and optional overrides.

    This class exercises compact_triggered default, token_usage default,
    explicit token_usage, and explicit compact_triggered across 4 scenarios
    to ensure the turn record accurately captures LLM turn metrics including
    whether a context compaction was triggered during the turn.
    """

    def test_verify_default_compact_triggered_is_false(self):
        """Validate that compact_triggered defaults to False when not supplied.

        The test constructs a TurnRecord without compact_triggered and asserts
        rec.compact_triggered is False because compaction is an optional
        optimization that only fires when context length exceeds a threshold;
        the default must be False to avoid false-positive compaction signals.
        """
        rec = TurnRecord(turn_number=1, event_count=5, latency_ms=3000.0)
        assert rec.compact_triggered is False

    def test_verify_default_token_usage_is_empty_dict(self):
        """Validate that token_usage defaults to {} when not supplied.

        The test asserts rec.token_usage == {} because token usage per turn
        is measured separately by the LLM adapter; the turn record itself
        does not calculate tokens and must not fabricate a default mapping.
        """
        rec = TurnRecord(turn_number=1, event_count=5, latency_ms=3000.0)
        assert rec.token_usage == {}

    def test_verify_token_usage_stores_prompt_and_completion_counts(self):
        """Validate that token_usage stores the supplied prompt and completion token counts.

        The test constructs a turn with token_usage={'prompt': 1000, 'completion': 200}
        and asserts both values are preserved because the telemetry summary
        aggregates these counts to compute total context cost and to detect
        when a model is approaching its token limit.
        """
        rec = TurnRecord(
            turn_number=2,
            event_count=10,
            latency_ms=5000.0,
            token_usage={"prompt": 1000, "completion": 200},
        )
        assert rec.token_usage["prompt"] == 1000
        assert rec.token_usage["completion"] == 200

    def test_verify_compact_triggered_stores_true_when_set(self):
        """Validate that compact_triggered stores True when explicitly supplied.

        The test constructs a turn with compact_triggered=True and asserts the
        flag is preserved because the compaction signal is consumed by the
        telemetry summarizer to report how often context windows were
        compressed, which is a key observability metric for long sessions.
        """
        rec = TurnRecord(
            turn_number=3,
            event_count=8,
            latency_ms=4000.0,
            compact_triggered=True,
        )
        assert rec.compact_triggered is True


# ---- Retry Records --------------------------------------------------------


class TestRetryRecordFields:
    """Validate the RetryRecord default fields and full-record construction.

    This class exercises auto-generated timestamp and full-field construction
    across 2 scenarios to ensure retry records carry attempt number, error
    type, error detail, delay, and a wall-clock timestamp so that retry
    histograms can be rendered by the observability layer.
    """

    def test_verify_retry_timestamp_is_auto_generated_within_call_bounds(self):
        """Validate that RetryRecord.timestamp falls between the times recorded before and after construction.

        The test records time.time() before and after constructing the record
        and asserts before <= rec.timestamp <= after because retry events
        must be timestamped at creation time so that retry delay histograms
        align with real-wall clock measurements rather than monotonic counters.
        """
        before = time.time()
        rec = RetryRecord(attempt=1, error_type="exception", error_detail="timeout", delay_s=2.0)
        after = time.time()
        assert before <= rec.timestamp <= after

    def test_verify_full_retry_record_stores_all_fields(self):
        """Validate that RetryRecord preserves attempt, error_type, error_detail, and delay_s.

        The test constructs a record with attempt=3, error_type='http_status',
        error_detail='503 Service Unavailable', delay_s=5.0 and asserts all
        four fields match because retry metadata is the primary input to the
        retry-rate observability dashboard and must be stored verbatim.
        """
        rec = RetryRecord(
            attempt=3,
            error_type="http_status",
            error_detail="503 Service Unavailable",
            delay_s=5.0,
        )
        assert rec.attempt == 3
        assert rec.error_type == "http_status"
        assert rec.error_detail == "503 Service Unavailable"
        assert rec.delay_s == 5.0


# ---- Comprehensive Summary Tests ------------------------------------------


class TestSummaryComprehensive:
    """Validate that get_summary aggregates recorded events correctly.

    This class exercises tool-usage counting, success/failure separation,
    average latency computation, turn latency averaging, total event counting,
    compaction counting, session duration, and retry counting across 8
    scenarios to ensure the summary function computes every metric from the
    raw record lists without hard-coded defaults that would mask aggregation
    bugs.
    """

    def setup_method(self):
        """Initialize a fresh EncreTelemetry instance before each test for isolation."""
        self.tel = EncreTelemetry()

    def test_verify_tool_usage_counts_are_per_tool(self):
        """Validate that tool_usage in the summary groups counts by tool name.

        The test records three bash calls, one edit call, and one grep call,
        then asserts summary['tool_usage']['bash'] == 3, 'edit' == 1, and
        'grep' == 1 because the per-tool breakdown is the primary observability
        signal used to detect tool skew and to plan rate-limiting per tool.
        """
        self.tel.record_tool_call("bash", 100.0, True)
        self.tel.record_tool_call("bash", 200.0, True)
        self.tel.record_tool_call("edit", 150.0, True)
        self.tel.record_tool_call("grep", 50.0, True)
        self.tel.record_tool_call("bash", 300.0, True)
        summary = self.tel.get_summary()
        assert summary["tool_usage"]["bash"] == 3
        assert summary["tool_usage"]["edit"] == 1
        assert summary["tool_usage"]["grep"] == 1

    def test_verify_successful_and_failed_tool_call_counts_are_separate(self):
        """Validate that total_tool_calls equals successful_tool_calls plus failed_tool_calls.

        The test records two successful and one failed call and asserts
        total_tool_calls == 3, successful_tool_calls == 2, and failed_tool_calls
        == 1 because the separation is required for calculating success rates
        and for surfacing failure ratios in the observability dashboard.
        """
        self.tel.record_tool_call("bash", 100.0, True)
        self.tel.record_tool_call("bash", 100.0, False, error_message="fail")
        self.tel.record_tool_call("edit", 100.0, True)
        summary = self.tel.get_summary()
        assert summary["total_tool_calls"] == 3
        assert summary["successful_tool_calls"] == 2
        assert summary["failed_tool_calls"] == 1

    def test_verify_avg_tool_latency_is_arithmetic_mean(self):
        """Validate that avg_tool_latency_ms equals the arithmetic mean of recorded latencies.

        The test records latencies 100, 200, and 300 and asserts the average
        is 200.0 because the mean is the standard aggregation for latency
        reporting and any deviation would indicate a bug in the reducer.
        """
        self.tel.record_tool_call("a", 100.0, True)
        self.tel.record_tool_call("b", 200.0, True)
        self.tel.record_tool_call("c", 300.0, True)
        summary = self.tel.get_summary()
        assert summary["avg_tool_latency_ms"] == 200.0

    def test_verify_avg_turn_latency_is_arithmetic_mean(self):
        """Validate that avg_turn_latency_ms equals the arithmetic mean of recorded turn latencies.

        The test records turns with latencies 1000 and 3000 and asserts the
        average is 2000.0 because turn-level latency is the primary user-facing
        responsiveness metric and must be computed as a simple mean.
        """
        self.tel.record_turn(1, 5, 1000.0)
        self.tel.record_turn(2, 3, 3000.0)
        summary = self.tel.get_summary()
        assert summary["avg_turn_latency_ms"] == 2000.0

    def test_verify_total_events_is_sum_of_event_counts_across_turns(self):
        """Validate that total_events equals the sum of event_count across all recorded turns.

        The test records three turns with event_counts 5, 3, and 7 and asserts
        total_events == 15 because the event count tracks the number of
        individual tool calls and LLM interactions within each turn, and the
        sum is the standard measure of total telemetry volume.
        """
        self.tel.record_turn(1, 5, 1000.0)
        self.tel.record_turn(2, 3, 2000.0)
        self.tel.record_turn(3, 7, 1500.0)
        summary = self.tel.get_summary()
        assert summary["total_events"] == 15

    def test_verify_compactions_count_matches_turns_with_compact_triggered_true(self):
        """Validate that compactions equals the number of turns with compact_triggered=True.

        The test records three turns, two of which have compact_triggered=True,
        and asserts summary['compactions'] == 2 because the compaction counter
        is the primary metric for detecting how often the context window
        compression path is invoked during a session.
        """
        self.tel.record_turn(1, 5, 1000.0, compact_triggered=False)
        self.tel.record_turn(2, 3, 2000.0, compact_triggered=True)
        self.tel.record_turn(3, 7, 1500.0, compact_triggered=True)
        summary = self.tel.get_summary()
        assert summary["compactions"] == 2

    def test_verify_session_duration_is_non_negative(self):
        """Validate that session_duration_s is >= 0.0 for a freshly created telemetry instance.

        The test constructs a new telemetry object and asserts summary['session_duration_s']
        >= 0.0 because the session duration is computed from _session_started_at
        and must never be negative; a negative value would indicate a clock
        regression bug.
        """
        tel = EncreTelemetry()
        summary = tel.get_summary()
        assert summary["session_duration_s"] >= 0.0

    def test_verify_retry_summary_counts_all_recorded_retries(self):
        """Validate that total_retries equals the number of recorded retry events.

        The test records three retries and asserts summary['total_retries'] == 3
        because the retry counter feeds the retry-rate calculation and must
        faithfully reflect every recorded retry regardless of error type.
        """
        self.tel.record_retry(1, "http_status", "429", 1.0)
        self.tel.record_retry(2, "http_status", "503", 2.0)
        self.tel.record_retry(1, "exception", "timeout", 3.0)
        summary = self.tel.get_summary()
        assert summary["total_retries"] == 3


# ---- Reset Behavior -------------------------------------------------------


class TestReset:
    """Validate that reset clears all telemetry state and reinitializes session tracking.

    This class exercises list clearing, session start reset, and post-reset
    summary emptiness across 3 scenarios to ensure reset is a complete
    teardown operation that leaves the telemetry object in the same state as
    a freshly constructed one, so it can be reused across sessions without
    leaking events.
    """

    def test_verify_reset_clears_tool_calls_turns_and_retries(self):
        """Validate that reset empties all three internal record lists.

        The test records one item in each list, asserts each has length 1,
        calls reset, and then asserts all three lengths are 0 because reset
        must clear every record type so that a subsequent session starts
        with a completely clean slate and no stale event cross-contamination.
        """
        tel = EncreTelemetry()
        tel.record_tool_call("bash", 100.0, True)
        tel.record_turn(1, 2, 1000.0)
        tel.record_retry(1, "e", "d", 1.0)
        assert len(tel.tool_calls) == 1
        assert len(tel.turns) == 1
        assert len(tel.retries) == 1

        tel.reset()
        assert len(tel.tool_calls) == 0
        assert len(tel.turns) == 0
        assert len(tel.retries) == 0

    def test_verify_reset_advances_session_start_timestamp(self):
        """Validate that reset sets _session_started_at to a time >= the previous value.

        The test records the old start time, calls reset, and asserts the new
        _session_started_at is >= the old value because reset must reinitialize
        the session timer so that subsequent session_duration_s measurements
        start from zero rather than accumulating across sessions.
        """
        tel = EncreTelemetry()
        old_start = tel._session_started_at
        tel.reset()
        assert tel._session_started_at >= old_start

    def test_verify_summary_after_reset_contains_all_zero_fields(self):
        """Validate that get_summary after reset returns the same zeroed structure as an empty telemetry object.

        The test records a tool call, resets, and asserts total_tool_calls,
        total_turns, and total_retries are all 0 because reset must make the
        telemetry object indistinguishable from a fresh instance for any code
        that inspects the summary post-reset.
        """
        tel = EncreTelemetry()
        tel.record_tool_call("bash", 100.0, True)
        tel.reset()
        summary = tel.get_summary()
        assert summary["total_tool_calls"] == 0
        assert summary["total_turns"] == 0
        assert summary["total_retries"] == 0


# ---- Disabled Telemetry ---------------------------------------------------


class TestDisabledTelemetry:
    """Validate that disabled telemetry performs no-ops without errors.

    This class exercises record_tool_call, record_turn, record_retry, the
    constructor default enabled state, explicit disabled construction, and
    flush behavior when disabled across 6 scenarios to ensure that turning
    telemetry off is a hard no-op: no records are stored, no side effects
    occur, and flush still returns a valid empty summary dict.
    """

    def test_verify_record_tool_call_is_noop_when_disabled(self):
        """Validate that record_tool_call does not append to tool_calls when enabled=False.

        The test constructs a disabled telemetry object, records a tool call,
        and asserts len(tel.tool_calls) == 0 because disabled mode must
        suppress all instrumentation overhead so that performance-sensitive
        paths do not pay any cost when telemetry is turned off.
        """
        tel = EncreTelemetry(enabled=False)
        tel.record_tool_call("bash", 100.0, True)
        assert len(tel.tool_calls) == 0

    def test_verify_record_turn_is_noop_when_disabled(self):
        """Validate that record_turn does not append to turns when enabled=False.

        The test constructs a disabled telemetry object, records a turn, and
        asserts len(tel.turns) == 0 because the turn recording path must be
        equally suppressed so that long-running sessions do not accumulate
        hidden turn records when telemetry is disabled.
        """
        tel = EncreTelemetry(enabled=False)
        tel.record_turn(1, 2, 1000.0)
        assert len(tel.turns) == 0

    def test_verify_record_retry_is_noop_when_disabled(self):
        """Validate that record_retry does not append to retries when enabled=False.

        The test constructs a disabled telemetry object, records a retry, and
        asserts len(tel.retries) == 0 because retry tracking must also be
        fully suppressed in disabled mode so that the error path does not
        leak telemetry writes when the feature is turned off.
        """
        tel = EncreTelemetry(enabled=False)
        tel.record_retry(1, "e", "d", 1.0)
        assert len(tel.retries) == 0

    def test_verify_constructor_defaults_enabled_to_true(self):
        """Validate that EncreTelemetry() constructs with enabled=True by default.

        The test asserts tel.enabled is True because the default must be
        enabled so that production sessions collect telemetry out of the box
        without requiring an explicit constructor argument.
        """
        tel = EncreTelemetry()
        assert tel.enabled is True

    def test_verify_explicitly_disabled_instance_has_enabled_false(self):
        """Validate that EncreTelemetry(enabled=False) sets the enabled flag to False.

        The test asserts tel.enabled is False because callers must be able to
        disable telemetry explicitly at construction time, for example in test
        fixtures or privacy-sensitive deployments.
        """
        tel = EncreTelemetry(enabled=False)
        assert tel.enabled is False

    def test_verify_flush_returns_empty_summary_when_disabled(self):
        """Validate that flush returns a zeroed summary dict when telemetry is disabled.

        The test constructs a disabled telemetry object, calls flush, and
        asserts the result is a dict with total_tool_calls == 0 because flush
        must remain callable and safe even when no records were ever collected
        during a disabled session.
        """
        tel = EncreTelemetry(enabled=False)
        result = tel.flush()
        assert isinstance(result, dict)
        assert result["total_tool_calls"] == 0


# ---- Timestamp Consistency ------------------------------------------------


class TestTimestampConsistency:
    """Validate that recorded events are timestamped in monotonically non-decreasing order.

    This class exercises tool call timestamp ordering and turn record timestamp
    ordering across 2 scenarios to ensure the telemetry system does not produce
    out-of-order timestamps, which would break time-series aggregation and
    session-duration calculations.
    """

    def test_verify_tool_call_records_are_ordered_by_timestamp(self):
        """Validate that tool call timestamps are in non-decreasing order after sequential recordings.

        The test records three tool calls in sequence, extracts their
        timestamps, and asserts the list equals its sorted version because
        chronological ordering is required for latency histograms and for
        detecting clock-skew anomalies in distributed agent deployments.
        """
        tel = EncreTelemetry()
        tel.record_tool_call("first", 100.0, True)
        tel.record_tool_call("second", 200.0, True)
        tel.record_tool_call("third", 300.0, True)
        timestamps = [t.timestamp for t in tel.tool_calls]
        assert timestamps == sorted(timestamps)

    def test_verify_turn_records_are_ordered_by_timestamp(self):
        """Validate that turn timestamps are in non-decreasing order after sequential recordings.

        The test records five turns, asserts len(tel.turns) == 5, and asserts
        the timestamp list equals its sorted version because turn ordering is
        the foundation of session timeline reconstruction and any inversion
        would corrupt the perceived session flow.
        """
        tel = EncreTelemetry()
        for i in range(5):
            tel.record_turn(i + 1, 2, 1000.0)
        assert len(tel.turns) == 5
        timestamps = [t.timestamp for t in tel.turns]
        assert timestamps == sorted(timestamps)
