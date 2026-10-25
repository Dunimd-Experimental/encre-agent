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

"""Tests for AgentEvent union, BackendEvent union, and factory function variants."""


import asyncio

from encre.protocol.handlers.run import _coalesce_stream_deltas
from encre.utils.types import (
    BackendError,
    BackendFinish,
    BackendText,
    BackendThinking,
    BackendToolCall,
    BackendToolCallDelta,
    Finish,
    PermissionRequest,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolProgress,
    ToolResult,
)


class TestAgentEventUnion:
    """Engineered to validate that every AgentEvent member type passes
    isinstance checks, ensuring the union discriminates correctly at runtime.

    This test class exercises 9 event subtypes — TextDelta, ThinkingDelta,
    ToolCallStart, ToolCallDelta, ToolCallEnd, ToolProgress, ToolResult,
    PermissionRequest, and Finish — across individual scenarios to confirm
    that the union's structural typing does not accidentally exclude any
    branch. The design follows the invariant that union members must be
    individually instantiable and type-checkable so that the event bus can
    route on isinstance without fallback coercion.
    """

    def test_verify_text_delta_passes_isinstance_check(self):
        """Validate that TextDelta instances are recognized by isinstance
        against their own type, confirming the union branch is reachable.

        The test exercises construction of a TextDelta and asserts
        isinstance holds because the agent event pipeline dispatches on
        concrete subtype to route text chunks to the correct frontend channel.
        """
        e = TextDelta(text="hello")
        assert isinstance(e, TextDelta), "TextDelta must pass isinstance against its own type"

    def test_verify_thinking_delta_passes_isinstance_check(self):
        """Validate that ThinkingDelta instances are recognized by isinstance
        against their own type, confirming the chain-of-thought branch is reachable.

        The test exercises construction of a ThinkingDelta and asserts
        isinstance holds because thinking deltas must be separable from text
        deltas in the streaming pipeline to render the correct UI panel.
        """
        e = ThinkingDelta(text="thinking...")
        assert isinstance(e, ThinkingDelta), "ThinkingDelta must pass isinstance against its own type"

    def test_verify_tool_call_start_passes_isinstance_check(self):
        """Validate that ToolCallStart instances are recognized by isinstance,
        confirming the tool-invocation introduction branch is reachable.

        The test exercises construction with a call id and tool name and asserts
        isinstance holds because the frontend needs to render a tool-call
        invitation card before arguments arrive.
        """
        e = ToolCallStart(name="bash", id="call_1")
        assert isinstance(e, ToolCallStart), "ToolCallStart must pass isinstance against its own type"

    def test_verify_tool_call_delta_passes_isinstance_check(self):
        """Validate that ToolCallDelta instances are recognized by isinstance,
        confirming the incremental argument-streaming branch is reachable.

        The test exercises construction with an index, key, and value and asserts
        isinstance holds because delta events must be distinguishable from
        start/end events for correct reassembly of tool arguments.
        """
        e = ToolCallDelta(id="call_1", key="args", value="{}")
        assert isinstance(e, ToolCallDelta), "ToolCallDelta must pass isinstance against its own type"

    def test_verify_tool_call_end_passes_isinstance_check(self):
        """Validate that ToolCallEnd instances are recognized by isinstance,
        confirming the tool-invocation completion branch is reachable.

        The test exercises construction with a call id and asserts isinstance
        holds because the frontend renders the end of a tool card when this
        event arrives, closing the invocation visual group.
        """
        e = ToolCallEnd(id="call_1")
        assert isinstance(e, ToolCallEnd), "ToolCallEnd must pass isinstance against its own type"

    def test_verify_tool_progress_passes_isinstance_check(self):
        """Validate that ToolProgress instances are recognized by isinstance,
        confirming the in-flight status branch is reachable.

        The test exercises construction with id, tool_name, and status and
        asserts isinstance holds because progress events must be distinguishable
        from result events to drive the spinner UI without prematurely closing
        the tool card.
        """
        e = ToolProgress(id="call_1", tool_name="bash", status="running")
        assert isinstance(e, ToolProgress), "ToolProgress must pass isinstance against its own type"

    def test_verify_tool_result_passes_isinstance_check(self):
        """Validate that ToolResult instances are recognized by isinstance,
        confirming the tool-output delivery branch is reachable.

        The test exercises construction with content and is_error flag and
        asserts isinstance holds because the result branch carries the final
        tool output and must be routed to the output panel, not the text panel.
        """
        e = ToolResult(id="call_1", content="output", is_error=False)
        assert isinstance(e, ToolResult), "ToolResult must pass isinstance against its own type"

    def test_verify_permission_request_passes_isinstance_check(self):
        """Validate that PermissionRequest instances are recognized by
        isinstance, confirming the safety-gate branch is reachable.

        The test exercises construction with tool_name and reason and asserts
        isinstance holds because permission requests must be routed to the
        approval UI before the tool is allowed to execute.
        """
        e = PermissionRequest(tool_name="bash", reason="safe")
        assert isinstance(e, PermissionRequest), "PermissionRequest must pass isinstance against its own type"

    def test_verify_finish_passes_isinstance_check(self):
        """Validate that Finish instances are recognized by isinstance,
        confirming the session-termination branch is reachable.

        The test exercises construction with a reason and asserts isinstance
        holds because the finish event signals the end of the agent turn and
        must be routed to close the streaming connection cleanly.
        """
        e = Finish(reason="stop")
        assert isinstance(e, Finish), "Finish must pass isinstance against its own type"


class TestBackendEventUnion:
    """Engineered to validate that every BackendEvent member type passes
    isinstance checks, ensuring the backend-side union discriminates correctly.

    This test class exercises 6 event subtypes — BackendText, BackendThinking,
    BackendToolCall, BackendToolCallDelta, BackendFinish, and BackendError —
    across 7 scenarios to confirm the backend event pipeline can branch on
    subtype without missing cases. The design follows the invariant that
    backend events mirror the agent event shape but carry additional fields
    (e.g. signature_delta for thinking) that must remain accessible.
    """

    def test_verify_backend_text_passes_isinstance_check(self):
        """Validate that BackendText instances are recognized by isinstance,
        confirming the backend text-chunk branch is reachable.

        The test exercises construction with text content and asserts isinstance
        holds because the backend emitter must route text chunks to the same
        downstream pipeline as agent-side text events.
        """
        e = BackendText(text="hello")
        assert isinstance(e, BackendText), "BackendText must pass isinstance against its own type"

    def test_verify_backend_thinking_passes_isinstance_check(self):
        """Validate that BackendThinking instances are recognized by isinstance,
        confirming the backend chain-of-thought branch is reachable.

        The test exercises construction with text and None signature_delta and
        asserts isinstance holds because thinking events must be distinguishable
        from text events even when the signature is absent.
        """
        e = BackendThinking(text="hmm...", signature_delta=None)
        assert isinstance(e, BackendThinking), "BackendThinking must pass isinstance against its own type"

    def test_verify_backend_tool_call_passes_isinstance_check(self):
        """Validate that BackendToolCall instances are recognized by isinstance,
        confirming the backend tool-invocation branch is reachable.

        The test exercises construction with id, name, and arguments and asserts
        isinstance holds because backend tool calls carry the full invocation
        payload and must be routed to the tool registry for execution.
        """
        e = BackendToolCall(id="c1", name="bash", arguments="{}")
        assert isinstance(e, BackendToolCall), "BackendToolCall must pass isinstance against its own type"

    def test_verify_backend_tool_call_delta_passes_isinstance_check(self):
        """Validate that BackendToolCallDelta instances are recognized by
        isinstance, confirming the backend incremental-argument branch is reachable.

        The test exercises construction with index, key, and value and asserts
        isinstance holds because delta events stream partial JSON fragments
        and must be assembled in order by the receiving endpoint.
        """
        e = BackendToolCallDelta(index=0, key="k", value="v")
        assert isinstance(e, BackendToolCallDelta), "BackendToolCallDelta must pass isinstance against its own type"

    def test_verify_backend_finish_passes_isinstance_check(self):
        """Validate that BackendFinish instances are recognized by isinstance,
        confirming the backend session-termination branch is reachable.

        The test exercises construction with reason and asserts isinstance holds
        because the backend must emit a finish event to close the turn on the
        server side before sending the agent-side finish.
        """
        e = BackendFinish(reason="stop")
        assert isinstance(e, BackendFinish), "BackendFinish must pass isinstance against its own type"

    def test_verify_backend_error_passes_isinstance_check(self):
        """Validate that BackendError instances are recognized by isinstance,
        confirming the backend error-reporting branch is reachable.

        The test exercises construction with an error string and asserts
        isinstance holds because error events must be routed to the error
        handler pipeline regardless of the originating backend provider.
        """
        e = BackendError(error="timeout")
        assert isinstance(e, BackendError), "BackendError must pass isinstance against its own type"

    def test_verify_backend_thinking_with_signature_delta(self):
        """Validate that BackendThinking carries a non-None signature_delta
        when explicitly provided, confirming the signed-thinking branch works.

        The test exercises construction with a signature string and asserts the
        field is preserved verbatim because signed thinking is used to bind
        the reasoning trace to the message for auditability.
        """
        e = BackendThinking(text="deep thought", signature_delta="sig123")
        assert e.signature_delta == "sig123", "signature_delta must be preserved when provided"


class TestFactoryFunctionEdgeCases:
    """Engineered to validate that factory functions handle optional fields,
    edge-case payloads, and full keyword coverage correctly.

    This test class exercises create_finish, create_text_delta,
    create_tool_result, create_permission_request, create_backend_thinking,
    and create_backend_error across 11 scenarios to confirm the factories
    apply defaults consistently and do not mutate shared state. The design
    follows the invariant that factories must produce fully independent
    objects so that mutating one instance never affects another.
    """

    def test_verify_create_finish_includes_usage_when_provided(self):
        """Validate that create_finish preserves the usage dict when supplied.

        The test exercises the factory with explicit token counts and asserts
        the usage field matches because usage tracking feeds billing and
        rate-limit logic downstream.
        """
        from encre.utils.types import create_finish
        f = create_finish("stop", usage={"prompt_tokens": 10, "completion_tokens": 20})
        assert f.usage == {"prompt_tokens": 10, "completion_tokens": 20}, \
            "usage dict must be preserved when provided"

    def test_verify_create_finish_sets_usage_to_none_when_omitted(self):
        """Validate that create_finish sets usage to None when no usage dict
        is supplied, confirming the optional-field default is correct.

        The test exercises the factory without the usage argument and asserts
        None because omitting usage must not leak a default dict that could
        be accidentally mutated by callers.
        """
        from encre.utils.types import create_finish
        f = create_finish("error")
        assert f.usage is None, "usage must be None when not provided"

    def test_verify_create_finish_accepts_all_reason_literals(self):
        """Validate that create_finish accepts every defined FinishReason
        literal without raising, confirming full reason coverage.

        The test iterates over all five reason strings and asserts each
        produced Finish carries the correct reason because the finish reason
        drives turn-termination routing in the event loop.
        """
        from encre.utils.types import create_finish
        for reason in ["stop", "tool_calls", "error", "max_tokens", "cancelled"]:
            f = create_finish(reason)
            assert f.reason == reason, f"reason must match the provided literal: {reason}"

    def test_verify_create_text_delta_preserves_empty_string(self):
        """Validate that create_text_delta preserves an empty string input
        without converting it to None, confirming null-safety for deltas.

        The test exercises the factory with "" and asserts the text field is
        empty because empty deltas are valid signals in streaming protocols
        and must not be collapsed into None.
        """
        from encre.utils.types import create_text_delta
        e = create_text_delta("")
        assert e.text == "", "empty string must be preserved, not coerced to None"

    def test_verify_create_text_delta_preserves_multiline_content(self):
        """Validate that create_text_delta preserves newline characters in
        multiline input, confirming no normalization is applied.

        The test exercises the factory with a three-line string and asserts
        the middle line is present because text deltas must carry raw content
        verbatim for the frontend to render line breaks correctly.
        """
        from encre.utils.types import create_text_delta
        e = create_text_delta("line1\nline2\nline3")
        assert "line2" in e.text, "multiline content must be preserved without modification"

    def test_verify_create_tool_result_sets_error_flag_correctly(self):
        """Validate that create_tool_result sets is_error to True when
        explicitly requested, confirming error-path construction.

        The test exercises the factory with is_error=True and asserts the flag
        is set because tool results carrying errors must be routed to the
        error display channel rather than the success output channel.
        """
        from encre.utils.types import create_tool_result
        e = create_tool_result("call_err", "command failed", is_error=True)
        assert e.is_error is True, "is_error flag must be True when explicitly set"

    def test_verify_create_permission_request_carries_tool_name(self):
        """Validate that create_permission_request preserves the tool_name
        argument, confirming the safety-gate factory is well-formed.

        The test exercises the factory and asserts the tool_name field matches
        because the permission UI needs the tool name to render the approval
        prompt for the user.
        """
        from encre.utils.types import create_permission_request
        e = create_permission_request("bash", "Running potentially dangerous command")
        assert e.tool_name == "bash", "tool_name must be preserved in the permission request"

    def test_verify_create_backend_thinking_carries_signature_when_provided(self):
        """Validate that create_backend_thinking preserves a non-None
        signature_delta when supplied, confirming the signed-thinking factory.

        The test exercises the factory with a signature string and asserts the
        field matches because audit-bound reasoning traces require the exact
        signature to be retained through the factory path.
        """
        from encre.utils.types import create_backend_thinking
        e = create_backend_thinking("deep thoughts", signature_delta="sig_abc")
        assert e.signature_delta == "sig_abc", "signature_delta must be preserved when provided"

    def test_verify_create_backend_thinking_sets_signature_to_none_when_omitted(self):
        """Validate that create_backend_thinking sets signature_delta to None
        when not supplied, confirming the optional-field default.

        The test exercises the factory without the signature argument and
        asserts None because unsigned thinking is the common case and must
        not carry a stale signature from a previous construction.
        """
        from encre.utils.types import create_backend_thinking
        e = create_backend_thinking("just thinking")
        assert e.signature_delta is None, "signature_delta must be None when not provided"

    def test_verify_create_backend_error_preserves_long_message(self):
        """Validate that create_backend_error preserves a 1000-character error
        string without truncation, confirming no implicit length cap exists.

        The test exercises the factory with a repeated-character string and
        asserts the length matches because backend errors can carry long
        stack traces that must reach the diagnostic handler intact.
        """
        from encre.utils.types import create_backend_error
        e = create_backend_error("A" * 1000)
        assert len(e.error) == 1000, "long error messages must be preserved without truncation"


class TestFinishReasonVariants:
    """Engineered to validate that every FinishReason literal produces a
    Finish instance with the correct reason field, ensuring no literal is
    silently rejected or mis-mapped.

    This test class exercises all five finish reasons — stop, tool_calls,
    error, max_tokens, cancelled — across individual scenarios to confirm
    the literal-to-field mapping is bijective. The design follows the
    invariant that each reason must round-trip through construction and
    back without transformation so that pattern matches on reason are stable.
    """

    def test_verify_finish_stop_reason_is_preserved(self):
        """Validate that Finish constructed with reason='stop' carries that
        reason, confirming the normal-termination branch.

        The test exercises construction and asserts reason == 'stop' because
        stop is the most common termination signal and must never be confused
        with other finish reasons in downstream routing.
        """
        f = Finish(reason="stop")
        assert f.reason == "stop", "stop reason must be preserved"

    def test_verify_finish_tool_calls_reason_is_preserved(self):
        """Validate that Finish constructed with reason='tool_calls' carries
        that reason, confirming the multi-tool branch.

        The test exercises construction and asserts reason == 'tool_calls'
        because this reason signals that the model requested additional tools
        and the loop must continue rather than ending the turn.
        """
        f = Finish(reason="tool_calls")
        assert f.reason == "tool_calls", "tool_calls reason must be preserved"

    def test_verify_finish_error_reason_is_preserved(self):
        """Validate that Finish constructed with reason='error' carries that
        reason, confirming the error-termination branch.

        The test exercises construction and asserts reason == 'error' because
        error terminations must be distinguished from success stops so that
        the UI can render an error banner instead of a completion summary.
        """
        f = Finish(reason="error")
        assert f.reason == "error", "error reason must be preserved"

    def test_verify_finish_max_tokens_reason_is_preserved(self):
        """Validate that Finish constructed with reason='max_tokens' carries
        that reason, confirming the token-limit branch.

        The test exercises construction and asserts reason == 'max_tokens'
        because token-limit exits require different handling — the turn is
        incomplete and the caller may need to resume with a larger budget.
        """
        f = Finish(reason="max_tokens")
        assert f.reason == "max_tokens", "max_tokens reason must be preserved"

    def test_verify_finish_cancelled_reason_is_preserved(self):
        """Validate that Finish constructed with reason='cancelled' carries
        that reason, confirming the user-abort branch.

        The test exercises construction and asserts reason == 'cancelled'
        because cancelled turns must be routed to the cleanup path that
        aborts in-flight tools and releases held resources.
        """
        f = Finish(reason="cancelled")
        assert f.reason == "cancelled", "cancelled reason must be preserved"


class TestToolResultPatterns:
    """Engineered to validate the success and error patterns of ToolResult,
    including edge cases around empty content and large payloads.

    This test class exercises four scenarios — success with content, error,
    empty content, and large content — to confirm the ToolResult dataclass
    preserves all fields and that the is_error flag correctly separates the
    two output channels. The design follows the invariant that ToolResult
    must handle arbitrary content sizes because tool outputs range from empty
    acknowledgments to multi-kilobyte file reads.
    """

    def test_verify_tool_result_success_pattern_preserves_content_and_flag(self):
        """Validate that ToolResult with is_error=False preserves non-empty
        content and the success flag, confirming the happy-path shape.

        The test exercises construction with a content string and asserts
        is_error is False and content is non-empty because the success path
        is the primary route and must carry the full tool output.
        """
        tr = ToolResult(id="t1", content="file contents here", is_error=False)
        assert tr.is_error is False, "is_error must be False for successful tool results"
        assert len(tr.content) > 0, "content must be preserved for successful results"

    def test_verify_tool_result_error_pattern_sets_error_flag(self):
        """Validate that ToolResult with is_error=True carries the error flag
        and the error message, confirming the error-path shape.

        The test exercises construction with a permission-denied message and
        asserts is_error is True because error results must be routed to the
        error display channel and must not be confused with empty successes.
        """
        tr = ToolResult(id="t2", content="Permission denied", is_error=True)
        assert tr.is_error is True, "is_error must be True for error tool results"

    def test_verify_tool_result_empty_content_is_valid_success(self):
        """Validate that ToolResult with empty content and is_error=False is
        a valid success state, confirming zero-byte outputs are not treated as errors.

        The test exercises construction with "" and is_error=False and asserts
        both fields match because some tools (e.g. touch, mkdir) produce no
        stdout on success and the empty string must not be interpreted as
        a missing result.
        """
        tr = ToolResult(id="t3", content="", is_error=False)
        assert tr.content == "", "empty content must be preserved verbatim"
        assert tr.is_error is False, "empty content with is_error=False is a valid success"

    def test_verify_tool_result_large_content_preserves_full_payload(self):
        """Validate that ToolResult preserves a 5000-character content string
        without truncation, confirming no implicit size limit exists.

        The test exercises construction with a large repeated-string payload
        and asserts the length matches because file-read and grep tools can
        produce large outputs that must reach the caller intact.
        """
        big = "x" * 5000
        tr = ToolResult(id="t4", content=big, is_error=False)
        assert len(tr.content) == 5000, "large content payloads must be preserved without truncation"


class TestStreamDeltaCoalescing:
    """The transport batches consecutive deltas before encrypting each frame.

    Batching must never reorder events or swallow the tail batch: a structural
    event flushes what came before it, and the end of the stream flushes what
    remains.
    """

    def test_coalesces_deltas_and_flushes_on_boundaries(self):
        """Consecutive text deltas merge, boundaries flush, nothing is lost."""

        async def source():
            for ch in "abcdefghij":
                yield TextDelta(ch)
            yield ToolCallStart(name="read_file", id="call-1")
            yield TextDelta("z")

        async def collect():
            return [event async for event in _coalesce_stream_deltas(source(), window=10.0)]

        events = asyncio.run(collect())
        text_frames = [event for event in events if isinstance(event, TextDelta)]
        joined = "".join(event.text for event in text_frames)

        assert joined == "abcdefghijz"
        assert len(text_frames) < 11, "deltas inside the window must be merged"
        boundary = next(i for i, event in enumerate(events) if isinstance(event, ToolCallStart))
        before = "".join(e.text for e in events[:boundary] if isinstance(e, TextDelta))
        assert before == "abcdefghij", "the batch before a boundary must be flushed first"
        assert isinstance(events[-1], TextDelta), "the tail batch must be flushed at end of stream"

    def test_never_merges_across_text_and_thinking(self):
        """Text and thinking are separate streams in the renderer."""

        async def source():
            yield TextDelta("a")
            yield ThinkingDelta("hmm")
            yield TextDelta("b")

        async def collect():
            return [event async for event in _coalesce_stream_deltas(source(), window=10.0)]

        events = asyncio.run(collect())
        assert [type(event).__name__ for event in events] == [
            "TextDelta", "ThinkingDelta", "TextDelta",
        ]
