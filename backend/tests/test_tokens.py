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

"""Tests for the token estimation utilities in encre.utils.tokens.

The suite covers estimate_tokens and its estimate_tokens_simple alias on
plain, long, Unicode, code, and whitespace inputs, count_message_tokens over
the OpenAI-style message shapes the agent loop emits, and the
is_tiktoken_available detection helper that selects between estimators.
"""


from encre.utils.tokens import (
    count_message_tokens,
    estimate_tokens,
    estimate_tokens_simple,
    is_tiktoken_available,
)


class TestEstimateTokens:
    """Validate the estimate_tokens utility across input types and edge cases.

    This class exercises empty string, short string, long string, integer return
    type, non-negative guarantee, length proportionality, model kwarg acceptance,
    Unicode text, code snippets, special characters, and pure whitespace across
    11 scenarios to ensure the estimator returns stable, monotonic, non-negative
    integers that grow with input length and handle non-ASCII and special-character
    text without raising. The design uses a character-count heuristic so that
    token estimation remains fast and deterministic without requiring tiktoken.
    """
    """Estimate the token cost of a single string for budget accounting."""

    def test_verify_empty_string_returns_zero(self):
        """Validate that estimate_tokens('') returns 0.

        The test asserts 0 because an empty string contains no tokens and the
        estimator must not return a positive count for zero-length input; a
        non-zero result would inflate cost estimates and break budget checks.
        """
        assert estimate_tokens("") == 0

    def test_verify_short_string_returns_positive_integer(self):
        """Validate that estimate_tokens('hello world') returns a positive integer.

        The test asserts isinstance(count, int) and count > 0 because a short
        non-empty string must map to at least one token estimate; zero would
        undercount and break per-message budget accounting.
        """
        count = estimate_tokens("hello world")
        assert isinstance(count, int)
        assert count > 0

    def test_verify_long_string_returns_sensible_positive_integer(self):
        """Validate that estimate_tokens on a repeated long string returns a positive integer above a minimal threshold.

        The test repeats 'hello world ' 100 times and asserts count > 50 because
        a 1100-character string must estimate well over 50 tokens; a value
        below that threshold would indicate a broken scaling factor.
        """
        count = estimate_tokens("hello world " * 100)
        assert isinstance(count, int)
        assert count > 50

    def test_verify_result_is_always_an_integer(self):
        """Validate that estimate_tokens always returns an int, not a float or string.

        The test asserts isinstance(count, int) because callers pass the result
        directly into integer budget comparisons (e.g. budget -= count); a
        float return would cause TypeError in those arithmetic paths.
        """
        count = estimate_tokens("any text")
        assert isinstance(count, int)

    def test_verify_result_is_never_negative(self):
        """Validate that estimate_tokens never returns a negative value.

        The test asserts count >= 0 because a negative token estimate would
        artificially inflate remaining budget and could allow overflow beyond
        the model's declared context window, causing API errors.
        """
        count = estimate_tokens("test")
        assert count >= 0

    def test_verify_estimate_grows_monotonically_with_input_length(self):
        """Validate that a longer string estimates more tokens than a shorter one.

        The test compares estimate_tokens('hi') against estimate_tokens('hi ' * 200)
        and asserts long > short because monotonic growth is the fundamental
        contract of any tokenizer approximation; non-monotonic behavior would
        make budget tracking unreliable.
        """
        short = estimate_tokens("hi")
        long = estimate_tokens("hi " * 200)
        assert long > short

    def test_verify_model_kwarg_is_accepted_without_error(self):
        """Validate that passing model='gpt-5.6' does not raise and returns a positive integer.

        The test asserts isinstance(count, int) and count > 0 because the model
        kwarg is part of the public API and must be accepted gracefully even if
        the current implementation ignores it; rejecting unknown models would
        break forward compatibility.
        """
        count = estimate_tokens("hello", model="gpt-5.6")
        assert isinstance(count, int)
        assert count > 0

    def test_verify_different_model_kwarg_is_accepted(self):
        """Validate that passing model='gpt-4' does not raise and returns a positive integer.

        The test asserts isinstance(count, int) and count > 0 because the estimator
        must accept any model string without error so that callers can pass the
        resolved model name from the config layer without pre-filtering.
        """
        count = estimate_tokens("hello", model="gpt-4")
        assert isinstance(count, int)
        assert count > 0

    def test_verify_unicode_text_returns_positive_integer(self):
        """Validate that estimate_tokens handles multi-byte CJK Unicode text without error.

        The test passes a four-character CJK string and asserts
        isinstance(count, int) and count > 0 because the estimator must handle
        multi-byte characters; an error or zero would break token accounting
        for non-ASCII agent conversations.
        """
        count = estimate_tokens("浣犲ソ涓栫晫")
        assert isinstance(count, int)
        assert count > 0

    def test_verify_code_snippet_returns_positive_integer(self):
        """Validate that estimate_tokens handles Python code strings correctly.

        The test passes a multi-line code snippet and asserts count > 0 because
        code is a common agent input and must be estimated without special-casing;
        a zero result would silently undercount the token cost of code blocks.
        """
        code = "def foo():\n    return 42\n"
        count = estimate_tokens(code)
        assert count > 0

    def test_verify_special_characters_returns_integer(self):
        """Validate that estimate_tokens handles punctuation-heavy strings without error.

        The test passes a string of ASCII special characters and asserts
        isinstance(count, int) because special-character density should not
        cause parser errors or type regressions in the estimation path.
        """
        text = "!@#$%^&*()_+{}|:\"<>?[];',./"
        count = estimate_tokens(text)
        assert isinstance(count, int)

    def test_verify_pure_whitespace_returns_integer(self):
        """Validate that estimate_tokens handles a string of spaces without error.

        The test passes five spaces and asserts isinstance(count, int) because
        whitespace-only strings are common in formatted agent outputs and must
        not trigger division-by-zero or type errors in the estimator.
        """
        count = estimate_tokens("     ")
        assert isinstance(count, int)


class TestEstimateTokensSimple:
    """Validate the estimate_tokens_simple compatibility wrapper.

    This class exercises return type, empty-string behavior, and consistency
    with the main estimate_tokens function across 3 scenarios to ensure the
    simple wrapper is a drop-in alias that preserves the same contract as the
    primary estimation function.
    """
    """Backward-compatible alias for estimate_tokens."""

    def test_verify_simple_wrapper_returns_an_integer(self):
        """Validate that estimate_tokens_simple('hello') returns an int.

        The test asserts isinstance(count, int) because the wrapper must
        preserve the return type contract of the primary function so that
        callers can substitute one for the other without type checks.
        """
        count = estimate_tokens_simple("hello")
        assert isinstance(count, int)

    def test_verify_simple_wrapper_returns_zero_for_empty_string(self):
        """Validate that estimate_tokens_simple('') returns 0.

        The test asserts count == 0 because the empty-string case is a
        fundamental invariant of any token estimator; returning non-zero
        would cause every empty message to consume budget unnecessarily.
        """
        count = estimate_tokens_simple("")
        assert count == 0

    def test_verify_simple_wrapper_is_consistent_with_estimate_tokens(self):
        """Validate that estimate_tokens_simple and estimate_tokens return the same value for identical input.

        The test asserts c1 == c2 because the simple wrapper must be a strict
        alias; any divergence would indicate a regression in the wrapper
        implementation that could silently change cost estimates.
        """
        c1 = estimate_tokens_simple("hello world")
        c2 = estimate_tokens("hello world")
        assert c1 == c2


class TestCountMessageTokens:
    """Validate the count_message_tokens utility for structured message dicts.

    This class exercises single message, empty list, multiple messages, empty
    content, missing content key, list-content blocks, tool calls, model kwarg
    acceptance, and batch monotonic growth across 9 scenarios to ensure the
    message-level estimator handles the full range of OpenAI-style message
    shapes that the agent loop produces.
    """
    """Estimate the total token cost of a conversation message list."""

    def test_verify_single_message_returns_positive_integer(self):
        """Validate that count_message_tokens on a single user message returns a positive int.

        The test asserts isinstance(count, int) and count > 0 because even a
        single short message must estimate at least one token; zero would
        collapse the per-message overhead accounting in the budget tracker.
        """
        msgs = [{"role": "user", "content": "hello"}]
        count = count_message_tokens(msgs)
        assert isinstance(count, int)
        assert count > 0

    def test_verify_empty_messages_list_returns_zero(self):
        """Validate that count_message_tokens([]) returns 0.

        The test asserts count == 0 because an empty message list represents
        no input to the model and must not consume any budget allocation.
        """
        count = count_message_tokens([])
        assert count == 0

    def test_verify_multiple_messages_returns_positive_integer(self):
        """Validate that count_message_tokens on a multi-turn list returns a positive int.

        The test supplies a system, user, and assistant message and asserts
        isinstance(count, int) and count > 0 because a conversation history
        must always estimate a positive token count when it contains content.
        """
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What is AI?"},
            {"role": "assistant", "content": "AI is..."},
        ]
        count = count_message_tokens(msgs)
        assert isinstance(count, int)
        assert count > 0

    def test_verify_message_with_empty_content_returns_at_least_four_tokens(self):
        """Validate that a message with empty content still estimates at least the per-message overhead.

        The test asserts count >= 4 because the OpenAI chat format attaches
        role and delimiter tokens even when content is blank, and the estimator
        must account for this fixed overhead so that empty-content messages do
        not cost zero and bypass budget limits.
        """
        msgs = [{"role": "user", "content": ""}]
        count = count_message_tokens(msgs)
        assert isinstance(count, int)
        assert count >= 4

    def test_verify_message_with_missing_content_key_does_not_raise(self):
        """Validate that count_message_tokens handles a message dict missing the 'content' key gracefully.

        The test passes [{'role': 'user'}] and asserts isinstance(count, int)
        because malformed messages can appear in wild input and the estimator
        must degrade safely rather than raising KeyError, which would crash the
        budget calculation mid-turn.
        """
        msgs = [{"role": "user"}]
        count = count_message_tokens(msgs)
        assert isinstance(count, int)

    def test_verify_list_content_blocks_are_counted(self):
        """Validate that messages with content as a list of text blocks are estimated correctly.

        The test supplies a content array with two text blocks and asserts
        isinstance(count, int) and count > 0 because multimodal-style content
        arrays are a supported message shape and must be flattened and counted
        rather than skipped or causing a type error.
        """
        msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
            ],
        }]
        count = count_message_tokens(msgs)
        assert isinstance(count, int)
        assert count > 0

    def test_verify_message_with_tool_calls_is_counted(self):
        """Validate that a message containing tool_calls estimates a positive token count.

        The test supplies an assistant message with two tool call objects and
        asserts isinstance(count, int) and count > 0 because tool call structures
        add tokens to the context window and must be included in the estimate
        rather than ignored, which would undercount the true model input size.
        """
        msgs = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "name": "bash", "arguments": '{"cmd": "ls"}'},
                {"id": "call_2", "name": "read", "arguments": '{"path": "/tmp"}'},
            ],
        }]
        count = count_message_tokens(msgs)
        assert isinstance(count, int)
        assert count > 0

    def test_verify_model_kwarg_is_accepted(self):
        """Validate that count_message_tokens accepts the model kwarg without error.

        The test passes model='gpt-4' and asserts isinstance(count, int) because
        the model argument is part of the public API and must be accepted
        gracefully even if the current implementation does not vary its
        estimate by model family.
        """
        msgs = [{"role": "user", "content": "hello"}]
        count = count_message_tokens(msgs, model="gpt-4")
        assert isinstance(count, int)

    def test_verify_batch_estimate_grows_monotonically_with_message_count(self):
        """Validate that adding a second message increases the total token estimate.

        The test compares a single-message estimate against a two-message estimate
        and asserts double > single because each additional message contributes
        role tokens and content tokens; a non-increasing result would indicate
        a deduplication or caching bug in the estimator.
        """
        single = count_message_tokens([{"role": "user", "content": "hello"}])
        double = count_message_tokens([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ])
        assert double > single


class TestTiktokenAvailability:
    """Validate the is_tiktoken_available detection helper.

    This class exercises the import-path check across 1 scenario to ensure the
    helper returns a bool regardless of whether tiktoken is installed, because
    callers branch on this value to select the accurate tiktoken-backed
    estimator versus the fast heuristic estimator.
    """
    """Report whether the accurate tiktoken backend is importable."""

    def test_verify_is_tiktoken_available_returns_a_bool(self):
        """Validate that is_tiktoken_available() returns a bool without raising.

        The test asserts isinstance(available, bool) because the helper is used
        in conditional branches throughout the token estimation layer; a
        non-bool return or an exception would break the estimator selection
        logic and force every caller to wrap the call in try/except.
        """
        available = is_tiktoken_available()
        assert isinstance(available, bool)
