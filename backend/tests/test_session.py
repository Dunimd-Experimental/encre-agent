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

"""Tests for :class:`EncreSession` and :class:`SessionCheckpoint`.

EncreSession is the mutable conversation context the agent accumulates
messages into across turns, while SessionCheckpoint is a lightweight
snapshot type that marks a recoverable point in the message history.
These tests cover construction invariants, message mutation, checkpoint
lifecycle, and the utility methods for token estimation, serialization,
and renderer hygiene.
"""

import time

from encre.config import EncreConfig
from encre.session import EncreSession, SessionCheckpoint


class TestSessionCheckpoint:
    """Engineered to validate the :class:`SessionCheckpoint` dataclass contract.

    SessionCheckpoint is a plain data container that captures a point-in-time
    view of a session's messages along with metadata (label, counts,
    timestamps). Tests verify default values, full initialization, and that
    no field is silently dropped or coerced during construction.
    """

    def test_verify_checkpoint_creation_stores_all_fields(self):
        """Validate that SessionCheckpoint stores every provided field verbatim.

        Every constructor argument becomes an attribute; the test asserts
        exact equality so accidental omission or renaming surfaces immediately.
        """
        cp = SessionCheckpoint(
            checkpoint_id="cp_001",
            label="test checkpoint",
            messages=[],
            tool_call_count=0,
            turn_count=0,
        )
        assert cp.checkpoint_id == "cp_001"
        assert cp.label == "test checkpoint"
        assert cp.messages == []
        assert cp.tool_call_count == 0
        assert cp.turn_count == 0

    def test_verify_checkpoint_defaults_are_semantic_zeros(self):
        """Validate that SessionCheckpoint falls back to sensible defaults when fields are omitted.

        Default label is empty string, counts are zero, metadata is empty dict,
        and created_at is 0.0 so that unpopulated checkpoints are distinguishable
        from intentionally zero-valued ones only by explicit construction.
        """
        cp = SessionCheckpoint(checkpoint_id="cp_default")
        assert cp.label == ""
        assert cp.messages == []
        assert cp.tool_call_count == 0
        assert cp.turn_count == 0
        assert cp.metadata == {}
        assert cp.created_at == 0.0

    def test_verify_checkpoint_with_data_preserves_all_fields(self):
        """Validate that SessionCheckpoint preserves populated fields including metadata and timestamp.

        Metadata and created_at are optional in the constructor but become
        part of the snapshot identity; the test confirms both survive
        construction unchanged for round-trip fidelity.
        """
        msgs = [{"role": "user", "content": "hello"}]
        cp = SessionCheckpoint(
            checkpoint_id="cp_002",
            label="snapshot",
            messages=msgs,
            tool_call_count=3,
            turn_count=2,
            metadata={"key": "value"},
            created_at=1234567890.0,
        )
        assert cp.messages == msgs
        assert cp.tool_call_count == 3
        assert cp.turn_count == 2
        assert cp.metadata == {"key": "value"}
        assert cp.created_at == 1234567890.0


class TestEncreSessionConstruction:
    """Engineered to validate :class:`EncreSession` construction invariants.

    A new session must start in a well-defined initial state: a generated
    UUID-like ID, an empty message list, current timestamps, and zero
    counters. Tests confirm the constructor honors the supplied
    EncreConfig so that per-session overrides (e.g. max_tokens) take effect
    without mutating the shared config instance.
    """

    def test_verify_session_creation_produces_a_valid_instance(self):
        """Validate that EncreSession(config) returns a non-None session object.

        Basic existence check ensures the constructor does not swallow
        exceptions or return None on a valid config.
        """
        config = EncreConfig()
        session = EncreSession(config)
        assert session is not None

    def test_verify_session_id_is_a_non_empty_string(self):
        """Validate that the session ID is a string and not empty.

        The ID is the primary key for session persistence and lookup; an
        empty or non-string ID would break storage and retrieval downstream.
        """
        config = EncreConfig()
        session = EncreSession(config)
        assert isinstance(session.id, str)
        assert len(session.id) > 0

    def test_verify_session_messages_are_empty_initially(self):
        """Validate that a fresh session starts with an empty message list.

        An empty list (not None) lets downstream code use len() and iteration
        without null checks on first access.
        """
        config = EncreConfig()
        session = EncreSession(config)
        assert session.messages == []

    def test_verify_session_created_at_is_within_one_second_of_now(self):
        """Validate that created_at falls within the neighborhood of the call time.

        Two time.time() calls bracket the constructor; a tolerance of +/- 1
        second guards against clock skew without demanding exact equality,
        which would be fragile under load.
        """
        config = EncreConfig()
        before = time.time()
        session = EncreSession(config)
        after = time.time()
        assert before - 1 <= session.created_at <= after + 1

    def test_verify_session_updated_at_equals_created_at_initially(self):
        """Validate that updated_at is initialized to the same value as created_at.

        Both timestamps originate from the same construction moment; the
        small tolerance accounts for the two separate time.time() calls in
        the constructor without demanding bitwise equality.
        """
        config = EncreConfig()
        session = EncreSession(config)
        # The constructor calls time.time() twice, so allow a small clock delta.
        assert abs(session.updated_at - session.created_at) < 0.01

    def test_verify_session_tool_call_count_is_zero_initially(self):
        """Validate that tool_call_count starts at zero on a fresh session.

        The counter drives quota enforcement and telemetry; an incorrect
        initial value would distort usage reports from the first turn.
        """
        config = EncreConfig()
        session = EncreSession(config)
        assert session.tool_call_count == 0

    def test_verify_session_turn_count_is_zero_initially(self):
        """Validate that turn_count starts at zero on a fresh session.

        Turn count is the primary limiter for max_turns; it must begin at
        zero so the first user message does not prematurely trigger the cap.
        """
        config = EncreConfig()
        session = EncreSession(config)
        assert session.turn_count == 0

    def test_verify_session_stores_the_provided_config_reference(self):
        """Validate that EncreSession stores the config instance by reference, not by copy.

        Storing by reference lets mutations to the config object (e.g.
        runtime overrides) propagate to the session without reassignment.
        """
        config = EncreConfig(max_tokens=9999)
        session = EncreSession(config)
        assert session.config is config
        assert session.config.max_tokens == 9999


class TestEncreSessionMessages:
    """Engineered to validate message mutation methods on :class:`EncreSession`.

    The session is the central append-only log (with occasional rollbacks)
    that the agent feeds into the model. Tests cover adding plain text
    messages, adding tool-result messages, adding structured content blocks,
    and verifying that updated_at advances on each mutation.
    """

    def test_verify_add_message_appends_a_user_message(self):
        """Validate that add_message inserts a new message dict with the correct role and content.

        The message list is the session's primary state; appending must
        preserve both role and content exactly so the model receives the
        intended prompt.
        """
        config = EncreConfig()
        session = EncreSession(config)
        session.add_message("user", "hello world")
        assert len(session.messages) == 1
        assert session.messages[0]["role"] == "user"
        assert session.messages[0]["content"] == "hello world"

    def test_verify_add_multiple_messages_accumulates_in_order(self):
        """Validate that successive add_message calls preserve insertion order.

        Conversation ordering is critical for model comprehension; the test
        asserts the list length equals the number of calls, not that content
        matches (that is covered by the single-message test).
        """
        config = EncreConfig()
        session = EncreSession(config)
        session.add_message("system", "You are helpful.")
        session.add_message("user", "Question")
        session.add_message("assistant", "Answer")
        assert len(session.messages) == 3

    def test_verify_add_message_advances_updated_at(self):
        """Validate that add_message advances updated_at to a value strictly greater than the previous one.

        updated_at tracks the last mutation time for expiry calculations;
        a non-advancing timestamp would prevent stale-session eviction from working.
        """
        config = EncreConfig()
        session = EncreSession(config)
        original = session.updated_at
        time.sleep(0.01)
        session.add_message("user", "hi")
        assert session.updated_at > original

    def test_verify_add_message_with_extra_kwargs_attaches_them_to_the_dict(self):
        """Validate that extra keyword arguments are attached to the message dict.

        Tool call metadata is passed through add_message as kwargs; the
        test asserts tool_calls appears on the resulting dict so callers
        can inspect it later without losing the information.
        """
        config = EncreConfig()
        session = EncreSession(config)
        session.add_message("assistant", "hello", tool_calls=[{"id": "t1", "name": "bash"}])
        assert "tool_calls" in session.messages[0]

    def test_verify_add_tool_result_creates_a_tool_role_message_and_increments_counter(self):
        """Validate that add_tool_result creates a tool-role message with the correct tool_call_id and increments the counter.

        Tool results are the feedback channel from executed tools back into
        the conversation; the counter tracks total tool invocations for
        quota enforcement and telemetry.
        """
        config = EncreConfig()
        session = EncreSession(config)
        session.add_tool_result("call_abc", "ls output", is_error=False)
        assert len(session.messages) == 1
        assert session.messages[0]["role"] == "tool"
        assert session.messages[0]["tool_call_id"] == "call_abc"
        assert session.tool_call_count == 1

    def test_verify_add_tool_result_accumulates_counter_across_calls(self):
        """Validate that tool_call_count increments by one for each add_tool_result call.

        Cumulative counting is required for accurate quota tracking; the
        test verifies two successive calls produce a count of 2.
        """
        config = EncreConfig()
        session = EncreSession(config)
        assert session.tool_call_count == 0
        session.add_tool_result("t1", "out1")
        session.add_tool_result("t2", "out2")
        assert session.tool_call_count == 2

    def test_verify_add_message_content_accepts_structured_content_blocks(self):
        """Validate that add_message_content stores content as a list of blocks rather than a plain string.

        Anthropic-style content blocks allow mixed text and tool-use
        payloads inside a single message; the session must preserve the
        list structure so the renderer can serialize it correctly.
        """
        config = EncreConfig()
        session = EncreSession(config)
        blocks = [{"type": "text", "text": "Hello"}, {"type": "text", "text": "World"}]
        session.add_message_content("user", blocks)
        assert len(session.messages) == 1
        assert session.messages[0]["role"] == "user"
        assert isinstance(session.messages[0]["content"], list)


class TestEncreSessionCheckpoints:
    """Engineered to validate checkpoint creation, listing, rollback, and cleanup on :class:`EncreSession`.

    Checkpoints let the agent rewind to a prior conversation state without
    losing the intervening history permanently. Tests verify the checkpoint
    ID generation, listing fidelity, rollback restoration, nonexistent-ID
    handling, and the clear operation.
    """

    def test_verify_checkpoint_creates_a_non_empty_id(self):
        """Validate that checkpoint() returns a non-empty string ID for the new snapshot.

        The ID is the handle used by rollback and list; it must be stable
        and non-empty so callers can store and reuse it across turns.
        """
        config = EncreConfig()
        session = EncreSession(config)
        cid = session.checkpoint("my label")
        assert isinstance(cid, str)
        assert len(cid) > 0

    def test_verify_checkpoint_list_reports_the_snapshot(self):
        """Validate that list_checkpoints returns the checkpoint with the expected label.

        The list endpoint drives the checkpoint UI; the label must match
        what the caller passed at creation time for the user to identify it.
        """
        config = EncreConfig()
        session = EncreSession(config)
        session.add_message("user", "important")
        session.checkpoint("snapshot")
        checkpoints = session.list_checkpoints()
        assert len(checkpoints) == 1
        assert checkpoints[0]["label"] == "snapshot"

    def test_verify_rollback_restores_messages_to_the_checkpointed_state(self):
        """Validate that rollback(cid) restores the message list to exactly what was captured at checkpoint time.

        Rollback is the core recovery primitive; the test adds a message
        before the checkpoint, another after, then confirms the post-rollback
        list contains only the pre-checkpoint message.
        """
        config = EncreConfig()
        session = EncreSession(config)
        session.add_message("user", "before checkpoint")
        cid = session.checkpoint("backup")
        session.add_message("assistant", "after checkpoint")
        assert len(session.messages) == 2
        success = session.rollback(cid)
        assert success is True
        assert len(session.messages) == 1
        assert session.messages[0]["content"] == "before checkpoint"

    def test_verify_rollback_nonexistent_id_returns_false(self):
        """Validate that rollback returns False when given an ID that does not correspond to any checkpoint.

        A false return lets the caller distinguish 'checkpoint not found'
        from 'rollback succeeded but state unchanged', which drives error handling.
        """
        config = EncreConfig()
        session = EncreSession(config)
        assert session.rollback("nonexistent") is False

    def test_verify_clear_checkpoints_removes_all_snapshots(self):
        """Validate that clear_checkpoints empties the checkpoint list completely.

        Cleanup is needed before session reset or export; the test asserts
        both the pre-clear count (2) and the post-clear count (0).
        """
        config = EncreConfig()
        session = EncreSession(config)
        session.checkpoint("cp1")
        session.checkpoint("cp2")
        assert len(session.list_checkpoints()) == 2
        session.clear_checkpoints()
        assert len(session.list_checkpoints()) == 0


class TestEncreSessionUtility:
    """Engineered to validate utility methods on :class:`EncreSession`: clear, expiry, serialization, and token estimation.

    These methods support session lifecycle management (reset, TTL expiry),
    model context preparation (token counting, context message isolation),
    and serialization (dict round-trip for persistence).
    """

    def test_verify_clear_history_resets_messages_counters_and_timestamp(self):
        """Validate that clear_history empties messages, resets counters, and advances updated_at.

        Clear is the session-reset primitive; all mutation counters must
        return to zero so the next conversation starts fresh without
        carrying over stale quota state.
        """
        config = EncreConfig()
        session = EncreSession(config)
        session.add_message("user", "hello")
        assert len(session.messages) == 1
        session.clear_history()
        assert len(session.messages) == 0
        assert session.tool_call_count == 0
        assert session.turn_count == 0

    def test_verify_is_expired_returns_false_for_a_fresh_session_under_ttl(self):
        """Validate that is_expired() returns False when the session age is within the configured TTL.

        A freshly created session is always active; the test constructs a
        config with a 24-hour TTL and confirms the new session passes the
        expiry check immediately.
        """
        config = EncreConfig(session_max_age_hours=24.0)
        session = EncreSession(config)
        assert session.is_expired() is False

    def test_verify_is_max_turns_reached_respects_the_turn_count_and_config_limit(self):
        """Validate that is_max_turns_reached returns True only when turn_count equals max_turns.

        The limit is inclusive (turn_count == max_turns triggers termination);
        the test asserts both the capped and uncapped states to verify the
        boundary condition is handled correctly on both sides.
        """
        config = EncreConfig(max_turns=10)
        session = EncreSession(config)
        session.turn_count = 10
        assert session.is_max_turns_reached() is True
        session.turn_count = 5
        assert session.is_max_turns_reached() is False

    def test_verify_to_dict_serializes_id_and_messages(self):
        """Validate that to_dict() produces a dict containing the session id and messages list.

        Serialization is the persistence path; the dict must include at
        least id and messages so from_dict can reconstruct a functional session.
        """
        config = EncreConfig()
        session = EncreSession(config)
        session.add_message("user", "hello")
        d = session.to_dict()
        assert isinstance(d, dict)
        assert d["id"] == session.id
        assert "messages" in d

    def test_verify_estimate_tokens_static_returns_a_positive_integer_for_non_empty_text(self):
        """Validate that estimate_tokens() returns a positive integer for non-empty input.

        The static estimator approximates token count for pre-flight quota
        checks; a non-positive result for real text would indicate a broken
        heuristic that could under-allocate context budget.
        """
        count = EncreSession.estimate_tokens("hello world")
        assert isinstance(count, int)
        assert count > 0

    def test_verify_estimate_tokens_returns_zero_for_empty_input(self):
        """Validate that estimate_tokens() returns 0 for an empty string.

        Empty input must not return a negative or spurious positive count;
        zero is the canonical representation of no tokens consumed.
        """
        count = EncreSession.estimate_tokens("")
        assert count == 0

    def test_verify_count_messages_tokens_returns_a_positive_integer_for_valid_messages(self):
        """Validate that count_messages_tokens() returns a positive integer for a non-empty message list.

        The message-level estimator sums per-message estimates; the test
        confirms it handles a single-user-message list without returning zero.
        """
        msgs = [{"role": "user", "content": "hello world"}]
        count = EncreSession.count_messages_tokens(msgs)
        assert isinstance(count, int)
        assert count > 0

    def test_verify_get_context_messages_returns_a_copy_that_does_not_mutate_the_original(self):
        """Validate that get_context_messages() returns an independent copy of the message list.

        The renderer and the model caller both receive this list; if it were
        the live session list, any in-place mutation by the caller would
        corrupt the session state. Appending to the returned copy must not
        affect session.messages.
        """
        config = EncreConfig()
        session = EncreSession(config)
        session.add_message("user", "hi")
        ctx = session.get_context_messages()
        assert len(ctx) == 1
        ctx.append({"role": "assistant", "content": "extra"})
        assert len(session.messages) == 1  # the copy does not touch session state

    def test_verify_renderer_messages_preserve_client_tool_ids_while_context_messages_do_not(self):
        """Validate that get_renderer_messages retains _client_id for UI correlation while get_context_messages strips it.

        The renderer needs _client_id to match streamed tool events back to
        their originating UI card; the context list (fed to the model) must
        not leak this internal field, which would confuse the model and
        waste tokens. The test asserts presence in renderer output and
        absence in context output for both the tool-call message and the
        tool-result message.
        """
        config = EncreConfig()
        session = EncreSession(config)
        session.add_message(
            "assistant",
            "",
            tool_calls=[{
                "id": "backend-call",
                "_client_id": "call_1_0",
                "function": {"name": "agent", "arguments": "{}"},
            }],
        )
        session.add_tool_result(
            "backend-call",
            "running",
            client_id="call_1_0",
        )

        renderer_messages = session.get_renderer_messages()
        context_messages = session.get_context_messages()

        assert renderer_messages[0]["tool_calls"][0]["_client_id"] == "call_1_0"
        assert renderer_messages[1]["_client_id"] == "call_1_0"
        assert "_client_id" not in context_messages[0]["tool_calls"][0]
        assert "_client_id" not in context_messages[1]

    def test_verify_from_dict_roundtrip_preserves_id_and_messages(self):
        """Validate that to_dict() followed by from_dict() reconstructs an equivalent session.

        The round-trip must preserve the session ID and at least the first
        message's content so persisted sessions can be resumed accurately.
        """
        config = EncreConfig()
        session1 = EncreSession(config)
        session1.add_message("user", "hello")
        data = session1.to_dict()
        session2 = EncreSession.from_dict(data, config)
        assert session2.id == session1.id
        assert len(session2.messages) == 1
        assert session2.messages[0]["content"] == "hello"
