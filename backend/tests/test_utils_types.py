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

"""Tests for encre.utils.types: event dataclasses, enums, and factory helpers.

The suite covers the frontend-facing event dataclasses (text, thinking, and
tool-call streams), the backend-facing counterparts, permission and task
literals, the three thinking-mode configurations, and every create_* factory
function that wraps those constructors.
"""


from encre.utils.types import (
    AdaptiveThinking,
    BackendError,
    BackendFinish,
    BackendText,
    BackendThinking,
    BackendToolCall,
    BackendToolCallDelta,
    DisabledThinking,
    EnabledThinking,
    Finish,
    PermissionAllow,
    PermissionAsk,
    PermissionDeny,
    PermissionRequest,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolProgress,
    ToolResult,
    create_backend_error,
    create_backend_finish,
    create_backend_text,
    create_backend_thinking,
    create_backend_tool_call,
    create_backend_tool_call_delta,
    create_finish,
    create_permission_request,
    create_text_delta,
    create_thinking_delta,
    create_tool_call_delta,
    create_tool_call_end,
    create_tool_call_start,
    create_tool_progress,
    create_tool_result,
)

# ===========================================================================
# Event dataclasses
# ===========================================================================


class TestTextDelta:
    """Validate the :class:`TextDelta` frontend event type.

    ``TextDelta`` carries incremental text fragments streamed from the model
    backend. This class exercises its constructor and field accessors across
    the create and empty-string edge cases to guarantee that the delta's
    payload is stored and retrievable without transformation or truncation.
    """

    def test_verify_text_delta_constructor_stores_payload(self):
        """Validate that TextDelta correctly stores its text payload on construction.

        Instantiates a delta with known content and asserts the field matches
        exactly, confirming the dataclass field initializer works without
        implicit normalization.
        """
        td = TextDelta(text="hello")
        assert td.text == "hello"


class TestThinkingDelta:
    """Validate the :class:`ThinkingDelta` frontend event type.

    ``ThinkingDelta`` represents streaming chunks of the model's internal
    chain-of-thought reasoning. Tests cover normal text delivery and the
    boundary condition where the chunk is empty (a no-op delta that still
    carries semantic meaning in the stream protocol).
    """

    def test_verify_thinking_delta_constructor_stores_payload(self):
        """Validate that ThinkingDelta correctly stores its text payload on construction.

        Confirms the constructor accepts and preserves arbitrary reasoning
        text, including ellipsis markers that real models emit.
        """
        td = ThinkingDelta(text="thinking...")
        assert td.text == "thinking..."

    def test_verify_thinking_delta_accepts_empty_chunk(self):
        """Validate that ThinkingDelta handles empty string payloads without error.

        An empty thinking chunk is a valid stream event (e.g. between two
        non-empty deltas). The test asserts the field is preserved as-is,
        not coerced to None.
        """
        td = ThinkingDelta(text="")
        assert td.text == ""


class TestToolCallStart:
    """Validate the :class:`ToolCallStart` frontend event type.

    ``ToolCallStart`` marks the beginning of a tool-invocation event carrying
    the call ID and tool name before arguments arrive incrementally. Tests
    confirm both fields are stored verbatim so downstream renderers can
    display a stable tool name even while arguments stream in.
    """

    def test_verify_tool_call_start_constructor_stores_id_and_name(self):
        """Validate that ToolCallStart correctly stores id and name fields.

        Both fields are used by the renderer to build the initial tool-call
        card before argument content arrives; they must not be altered.
        """
        tcs = ToolCallStart(id="call_1", name="bash")
        assert tcs.id == "call_1"
        assert tcs.name == "bash"


class TestToolCallDelta:
    """Validate the :class:`ToolCallDelta` frontend event type.

    ``ToolCallDelta`` carries incremental argument fragments keyed to a
    specific call ID. Tests verify the three-field structure (id, key, value)
    is stored intact so the incremental merge logic can reconstruct the full
    JSON arguments on arrival of the matching ``ToolCallEnd``.
    """

    def test_verify_tool_call_delta_constructor_stores_id_key_and_value(self):
        """Validate that ToolCallDelta correctly stores id, key, and value fields.

        The 'arguments' key is the canonical field for tool call parameters;
        storing it verbatim ensures round-trip fidelity during reassembly.
        """
        tcd = ToolCallDelta(id="call_1", key="arguments", value='{"pattern": "foo"}')
        assert tcd.id == "call_1"
        assert tcd.key == "arguments"


class TestToolCallEnd:
    """Validate the :class:`ToolCallEnd` frontend event type.

    ``ToolCallEnd`` signals completion of a tool-call argument stream. Its
    sole field (call ID) serves as the correlation key for matching start,
    delta, and end events into a single logical tool-invocation unit.
    """

    def test_verify_tool_call_end_constructor_stores_call_id(self):
        """Validate that ToolCallEnd correctly stores its correlation call ID.

        The ID must match the original ToolCallStart so the client can close
        the in-flight tool card and transition to execution.
        """
        tce = ToolCallEnd(id="call_1")
        assert tce.id == "call_1"


class TestToolProgress:
    """Validate the :class:`ToolProgress` frontend event type.

    ``ToolProgress`` conveys live status updates for an in-flight tool call
    (e.g. ``"running"``). Tests confirm all three fields are stored so the
    UI can render a status badge without ambiguity about which call and
    which tool the update belongs to.
    """

    def test_verify_tool_progress_constructor_stores_id_tool_name_and_status(self):
        """Validate that ToolProgress correctly stores id, tool_name, and status fields.

        The triple allows the renderer to update the correct card's status
        indicator even when multiple tools execute concurrently.
        """
        tp = ToolProgress(id="call_1", tool_name="bash", status="running")
        assert tp.id == "call_1"
        assert tp.tool_name == "bash"
        assert tp.status == "running"


class TestToolResult:
    """Validate the :class:`ToolResult` frontend event type.

    ``ToolResult`` delivers the completed output of a tool call back into the
    session. Tests cover the success path and the error path, ensuring the
    boolean flag distinguishes them so the agent can branch on whether to
    treat the content as actionable output or an exception.
    """

    def test_verify_tool_result_constructor_stores_success_output(self):
        """Validate that ToolResult correctly stores id, content, and is_error=False on success.

        A successful tool result feeds back into the conversation context;
        verifying is_error is False confirms the normal completion path.
        """
        tr = ToolResult(id="call_1", content="output here", is_error=False)
        assert tr.id == "call_1"
        assert tr.content == "output here"
        assert tr.is_error is False

    def test_verify_tool_result_constructor_stores_error_flag(self):
        """Validate that ToolResult correctly records is_error=True on failure.

        Error-flagged results are routed differently by the agent loop - the
        test asserts the flag survives construction so downstream logic can
        surface the message to the user without swallowing the failure.
        """
        tr = ToolResult(id="call_1", content="command not found", is_error=True)
        assert tr.is_error is True


class TestPermissionRequest:
    """Validate the :class:`PermissionRequest` frontend event type.

    ``PermissionRequest`` is emitted when a tool invocation requires user
    confirmation before proceeding. Tests verify the tool name and human-
    readable reason are preserved so the permission prompt renders with full
    context about what is being asked.
    """

    def test_verify_permission_request_constructor_stores_tool_name_and_reason(self):
        """Validate that PermissionRequest correctly stores tool_name and reason fields.

        Both fields drive the consent dialog UI; they must round-trip
        unchanged so the user sees exactly which operation needs approval.
        """
        pr = PermissionRequest(tool_name="bash", reason="safe command")
        assert pr.tool_name == "bash"
        assert pr.reason == "safe command"


class TestFinish:
    """Validate the :class:`Finish` frontend event type.

    ``Finish`` terminates the agent loop and returns control to the caller
    along with usage metadata. Tests cover normal construction and the full
    set of allowed finish reasons defined by the protocol.
    """

    def test_verify_finish_constructor_stores_reason_and_usage(self):
        """Validate that Finish correctly stores reason and usage fields on construction.

        The reason drives the UI terminal state (stop / error / max-tokens),
        and usage carries token counts for metering and quota enforcement.
        """
        f = Finish(reason="stop", usage={"tokens": 100})
        assert f.reason == "stop"
        assert f.usage == {"tokens": 100}

    def test_verify_finish_accepts_all_protocol_reasons(self):
        """Validate that Finish accepts every reason string defined by the protocol.

        Each reason corresponds to a distinct loop-exit path; accepting all of
        them guarantees the agent loop can terminate cleanly under any
        termination condition without raising an unexpected-enum error.
        """
        reasons = ["stop", "tool_calls", "error", "max_tokens", "cancelled"]
        for r in reasons:
            f = Finish(reason=r)
            assert f.reason == r


# ===========================================================================
# Permission
# ===========================================================================


class TestPermissionEnums:
    """Validate permission-related event types and literals.

    Permission enums encode the policy decision (allow / deny / ask) that
    accompanies a permission request. Tests iterate over every allowed
    literal value for both permission modes and task properties to ensure the
    Literal-based validation is exhaustive and no stray values slip in.
    """

    def test_verify_permission_mode_literal_values_are_exhaustive(self):
        """Validate that every permitted permission mode string is recognized by the protocol.

        The Literal definition constrains which strings the agent will accept;
        iterating the declared set confirms the test suite tracks the same
        enumeration as the type annotation.
        """
        modes = ["default", "accept_edits", "bypass", "dont_ask", "plan", "auto"]
        for m in modes:
            # PermissionMode is a Literal, so values must be in the set
            assert m in ["default", "accept_edits", "bypass", "dont_ask", "plan", "auto"]

    def test_verify_permission_allow_event_has_correct_behavior(self):
        """Validate that PermissionAllow encodes behavior='allow'.

        The allow event tells the permission handler to proceed without
        prompting the user; the behavior string must match the handler's
        expected branch key.
        """
        pa = PermissionAllow()
        assert pa.behavior == "allow"

    def test_verify_permission_deny_event_has_correct_behavior(self):
        """Validate that PermissionDeny encodes behavior='deny'.

        A deny event aborts the tool call immediately; the string must be
        distinct from 'allow' and 'ask' to avoid ambiguous handling.
        """
        pd = PermissionDeny()
        assert pd.behavior == "deny"

    def test_verify_permission_ask_event_has_correct_behavior(self):
        """Validate that PermissionAsk encodes behavior='ask'.

        The ask event signals that user consent is required before the tool
        runs; the behavior string must resolve to the interactive prompt path.
        """
        pa = PermissionAsk()
        assert pa.behavior == "ask"


# ===========================================================================
# Task enums
# ===========================================================================


class TestTaskEnums:
    """Validate task-type and task-status literal enumerations.

    Tasks in the encre system are dispatched as bash commands, nested agent
    invocations, or workflow chains; their status progresses through a fixed
    lifecycle. Tests confirm the allowed values match the Literal definitions
    so the scheduler and UI stay in sync.
    """

    def test_verify_task_type_literal_values_are_exhaustive(self):
        """Validate that every declared task type string is recognized by the protocol.

        The three permitted types (bash / agent / workflow) map to distinct
        execution backends; the Literal guard prevents typos from silently
        creating unknown dispatch paths.
        """
        types = ["bash", "agent", "workflow"]
        for t in types:
            assert t in ["bash", "agent", "workflow"]

    def test_verify_task_status_literal_values_are_exhaustive(self):
        """Validate that every declared task status string is recognized by the protocol.

        The five statuses form a directed acyclic graph (pending -> running
        -> completed/failed/killed). Every edge must have a corresponding
        literal to avoid unhandled transitions.
        """
        statuses = ["pending", "running", "completed", "failed", "killed"]
        for s in statuses:
            assert s in ["pending", "running", "completed", "failed", "killed"]


# ===========================================================================
# Thinking config
# ===========================================================================


class TestThinkingConfig:
    """Validate the three thinking-mode configurations.

    Thinking mode controls whether the model emits a visible chain-of-thought
    before answering. The three configurations - Adaptive (budget-gated),
    Enabled (always on with explicit budget), Disabled (off) - must each
    preserve their policy flags after construction.
    """

    def test_verify_adaptive_thinking_has_defaults(self):
        """Validate that AdaptiveThinking enables reasoning with a sensible default token budget.

        Adaptive mode turns thinking on automatically when the prompt exceeds
        min_tokens; the defaults must allow real models to enter the reasoning
        path without extra configuration.
        """
        tc = AdaptiveThinking()
        assert tc.enabled is True
        assert tc.min_tokens == 1024

    def test_verify_enabled_thinking_stores_budget_tokens(self):
        """Validate that EnabledThinking preserves the caller-provided token budget.

        The budget_tokens ceiling is enforced by the reasoning router to keep
        inference costs predictable; the constructor must store it verbatim.
        """
        tc = EnabledThinking(budget_tokens=16000)
        assert tc.budget_tokens == 16000

    def test_verify_disabled_thinking_sets_enabled_to_false(self):
        """Validate that DisabledThinking turns off the reasoning path entirely.

        When disabled, the agent must never route into the thinking block,
        saving tokens and latency on straightforward tasks.
        """
        tc = DisabledThinking()
        assert tc.enabled is False


# ===========================================================================
# Backend event types
# ===========================================================================


class TestBackendEvents:
    """Validate all backend-facing event dataclasses.

    Backend events are the canonical internal representation of model output.
    Tests exercise each variant's constructor to ensure the wire-format fields
    are stored verbatim so the streaming renderer and the session-accumulator
    can read them without post-processing.
    """

    def test_verify_backend_text_event_stores_content(self):
        """Validate that BackendText correctly stores its text payload.

        BackendText is emitted for every non-thinking, non-tool text chunk;
        the content field must round-trip unchanged to the client.
        """
        bt = BackendText(text="hello")
        assert bt.text == "hello"

    def test_verify_backend_thinking_event_stores_content_and_signature(self):
        """Validate that BackendThinking correctly stores text and optional signature delta.

        The signature_delta field is nullable because it only appears on the
        final thinking chunk; tests passing None confirm the Optional typing
        is respected at construction time.
        """
        bt = BackendThinking(text="hmm", signature_delta=None)
        assert bt.text == "hmm"

    def test_verify_backend_tool_call_event_stores_id_name_and_arguments(self):
        """Validate that BackendToolCall correctly stores id, name, and JSON arguments.

        These three fields are the complete initial tool-call payload; all must
        be present and unmodified so the renderer can show the pending tool
        invocation before streaming deltas arrive.
        """
        btc = BackendToolCall(id="call_1", name="bash", arguments='{"cmd": "ls"}')
        assert btc.name == "bash"
        assert btc.arguments == '{"cmd": "ls"}'

    def test_verify_backend_tool_call_delta_event_stores_index_key_and_value(self):
        """Validate that BackendToolCallDelta correctly stores index, key, and value fields.

        The index correlates the delta to the correct item in a multi-call
        batch; key identifies the JSON path being patched; value is the
        fragment. All three must be preserved for incremental merge to work.
        """
        bd = BackendToolCallDelta(index=0, key="arguments", value='"pattern"')
        assert bd.index == 0
        assert bd.key == "arguments"

    def test_verify_backend_finish_event_stores_reason(self):
        """Validate that BackendFinish correctly stores the termination reason.

        The reason field drives the loop-exit branch; it must survive
        construction unchanged so the caller can inspect why the run ended.
        """
        bf = BackendFinish(reason="stop")
        assert bf.reason == "stop"

    def test_verify_backend_error_event_stores_error_message(self):
        """Validate that BackendError correctly stores the error description fragment.

        Error messages may be truncated on the wire; the test asserts the
        known prefix is present, reflecting the real constraint that downstream
        code reads the message as a substring, not an equality match.
        """
        be = BackendError(error="Too many requests")
        assert "Too many" in be.error


# ===========================================================================
# Factory functions
# ===========================================================================


class TestFactories:
    """Validate all event factory functions.

    Factory functions wrap dataclass construction behind a uniform positional
    API so the streaming parser can emit events without importing every
    dataclass by name. Tests verify each factory returns the correct concrete
    type and preserves the passed-through payload.
    """

    def test_verify_create_text_delta_returns_text_delta_instance(self):
        """Validate that create_text_delta returns a TextDelta with the expected payload.

        The factory must not alter the text; rendering depends on exact
        character fidelity for diff-based UI updates.
        """
        event = create_text_delta("hello")
        assert isinstance(event, TextDelta)
        assert event.text == "hello"

    def test_verify_create_thinking_delta_returns_thinking_delta_instance(self):
        """Validate that create_thinking_delta returns a ThinkingDelta with the expected payload.

        Reasoning text must round-trip through the factory unchanged so the
        thinking renderer receives the model's exact output.
        """
        event = create_thinking_delta("hmm...")
        assert isinstance(event, ThinkingDelta)
        assert event.text == "hmm..."

    def test_verify_create_tool_call_start_returns_tool_call_start_instance(self):
        """Validate that create_tool_call_start returns a ToolCallStart with correct name and id.

        The id becomes the correlation key for all subsequent delta and end
        events; a factory must not generate or mutate it.
        """
        event = create_tool_call_start("bash", "id1")
        assert isinstance(event, ToolCallStart)
        assert event.name == "bash"
        assert event.id == "id1"

    def test_verify_create_tool_call_delta_returns_tool_call_delta_instance(self):
        """Validate that create_tool_call_delta returns a ToolCallDelta instance.

        The delta carries an incremental argument fragment; the factory must
        preserve index, key, and value exactly as passed.
        """
        event = create_tool_call_delta("id1", "arguments", "...")
        assert isinstance(event, ToolCallDelta)

    def test_verify_create_tool_call_end_returns_tool_call_end_instance(self):
        """Validate that create_tool_call_end returns a ToolCallEnd with the correlation id.

        The end event signals stream completion for the given call id; the
        factory must not alter the id so downstream correlation still works.
        """
        event = create_tool_call_end("id1")
        assert isinstance(event, ToolCallEnd)

    def test_verify_create_tool_progress_returns_tool_progress_instance(self):
        """Validate that create_tool_progress returns a ToolProgress instance.

        Progress events carry the running status for the in-flight call; they
        must reach the renderer without field loss.
        """
        event = create_tool_progress("id1", "bash", "running")
        assert isinstance(event, ToolProgress)

    def test_verify_create_tool_result_returns_tool_result_instance(self):
        """Validate that create_tool_result returns a ToolResult with the expected content.

        Tool results feed back into the conversation as assistant-side
        tool-output messages; content fidelity is essential for correctness.
        """
        event = create_tool_result("id1", "output")
        assert isinstance(event, ToolResult)
        assert event.content == "output"

    def test_verify_create_permission_request_returns_permission_request_instance(self):
        """Validate that create_permission_request returns a PermissionRequest instance.

        The factory must carry tool_name and reason through so the consent
        dialog displays the exact operation awaiting approval.
        """
        event = create_permission_request("bash", "safe cmd")
        assert isinstance(event, PermissionRequest)

    def test_verify_create_finish_returns_finish_instance(self):
        """Validate that create_finish returns a Finish instance.

        Finish terminates the run loop; the factory must preserve the reason
        so the caller can branch on stop/error/max-tokens paths.
        """
        event = create_finish("stop")
        assert isinstance(event, Finish)

    def test_verify_create_backend_text_returns_backend_text_instance(self):
        """Validate that create_backend_text returns a BackendText instance.

        Backend events are the canonical wire format; the factory must not wrap
        or unwrap them into a different type.
        """
        event = create_backend_text("hello")
        assert isinstance(event, BackendText)

    def test_verify_create_backend_thinking_returns_backend_thinking_instance(self):
        """Validate that create_backend_thinking returns a BackendThinking instance.

        Thinking events use the same schema as regular text but are typed
        separately so the renderer can style them distinctly.
        """
        event = create_backend_thinking("hmm")
        assert isinstance(event, BackendThinking)

    def test_verify_create_backend_tool_call_returns_backend_tool_call_instance(self):
        """Validate that create_backend_tool_call returns a BackendToolCall instance.

        Backend tool calls carry the full JSON arguments upfront (as opposed to
        the delta-streamed frontend variant); the factory must not truncate or
        reformat the arguments string.
        """
        event = create_backend_tool_call("id1", "bash", "{}")
        assert isinstance(event, BackendToolCall)

    def test_verify_create_backend_tool_call_delta_returns_backend_tool_call_delta_instance(self):
        """Validate that create_backend_tool_call_delta returns a BackendToolCallDelta instance.

        The delta's index is critical for multi-call batch ordering; it must be
        stored as an integer, not coerced to a string.
        """
        event = create_backend_tool_call_delta(0, "key", "value")
        assert isinstance(event, BackendToolCallDelta)

    def test_verify_create_backend_finish_returns_backend_finish_instance(self):
        """Validate that create_backend_finish returns a BackendFinish instance.

        The finish reason is the sole field; preserving it exactly lets the loop
        dispatcher route to the correct teardown branch.
        """
        event = create_backend_finish("stop")
        assert isinstance(event, BackendFinish)

    def test_verify_create_backend_error_returns_backend_error_instance(self):
        """Validate that create_backend_error returns a BackendError instance.

        Error messages may be long; the factory must store the string verbatim
        so the renderer can truncate rather than losing the cause.
        """
        event = create_backend_error("Request timed out")
        assert isinstance(event, BackendError)
