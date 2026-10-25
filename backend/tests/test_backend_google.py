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

"""Tests for GoogleBackend -- construction, capabilities, context window,
grounding, message conversion, token counting, finish-reason mapping, and
lifecycle cleanup."""

import asyncio

from encre.backends.google import GoogleBackend

# ===========================================================================
# Construction
# ===========================================================================


class TestGoogleBackendConstruction:
    """Validate ``GoogleBackend`` instantiation across parameter combinations.

    Covers default model and base URL, Flash model variant, custom base URL
    override, grounding toggle, grounding default, empty API key handling,
    and HTTP client initialization. The backend must point at Google AI
    Studio by default and faithfully propagate user-supplied parameters so
    downstream requests route to the intended endpoint with the right flags.
    """

    def test_verify_default_model_and_base_url(self):
        """Validate that defaults are ``gemini-3.7-flash`` and the Google AI Studio endpoint.

        Construction with only an API key must leave ``model``, ``api_key``,
        and ``base_url`` at their documented defaults so requests succeed
        out of the box against Google's generative-language endpoint.
        """
        be = GoogleBackend(api_key="fake-key")
        assert be.model == "gemini-3.7-flash"
        assert be.api_key == "fake-key"
        assert "generativelanguage.googleapis.com" in be.base_url

    def test_verify_flash_model_variant(self):
        """Validate that the Gemini 2.5 Flash model is accepted and stored correctly.

        Construction with ``model="gemini-2.5-flash"`` must persist the
        requested model, since Flash is a distinct tier with different
        latency and cost characteristics than Pro.
        """
        be = GoogleBackend(api_key="fake-key", model="gemini-2.5-flash")
        assert be.model == "gemini-2.5-flash"

    def test_verify_custom_base_url_overrides_default(self):
        """Validate that a custom base_url replaces the default Google endpoint.

        Construction with a custom ``base_url`` must store the provided URL
        so proxy deployments and compatible API gateways can redirect
        requests to an alternate origin.
        """
        be = GoogleBackend(
            api_key="fake-key",
            base_url="https://custom-google.example.com/v1beta",
        )
        assert be.base_url == "https://custom-google.example.com/v1beta"

    def test_verify_grounding_can_be_enabled_at_construction(self):
        """Validate that ``enable_grounding=True`` is stored on the backend instance.

        Google Search grounding must be opt-in and preserved on the
        instance so the request builder can attach grounding config later.
        """
        be = GoogleBackend(api_key="fake-key", enable_grounding=True)
        assert be.enable_grounding is True

    def test_verify_grounding_is_disabled_by_default(self):
        """Validate that grounding defaults to ``False`` so it does not fire unintentionally.

        Grounding adds network latency and cost, so it must be
        explicitly enabled by the caller rather than on by default.
        """
        be = GoogleBackend(api_key="fake-key")
        assert be.enable_grounding is False

    def test_verify_empty_api_key_is_allowed(self):
        """Validate that construction without an API key does not raise.

        ``api_key`` must default to an empty string and ``model`` to
        ``gemini-3.7-flash`` because some deployments inject credentials
        via environment variables after the backend is constructed.
        """
        be = GoogleBackend()
        assert be.api_key == ""
        assert be.model == "gemini-3.7-flash"

    def test_verify_http_client_is_initialized(self):
        """Validate that construction creates an active httpx.AsyncClient.

        ``_client`` must not be ``None`` because the Google backend
        builds its own HTTP client eagerly so connection pooling is
        ready for chat requests immediately.
        """
        be = GoogleBackend(api_key="fake-key")
        assert be._client is not None


# ===========================================================================
# Capability checks
# ===========================================================================


class TestGoogleBackendCapabilities:
    """Validate capability flags for Gemini models.

    Exercises ``supports_tool_calling``, ``supports_thinking``,
    ``supports_grounding``, and ``supports_prompt_caching`` across five
    scenarios. Each flag must return the correct boolean so the agent
    loop can enable function calling, reasoning modes, Google Search
    grounding, and prompt-caching headers per model.
    """

    def test_verify_supports_tool_calling_default(self):
        """Validate that the default Gemini model supports function calling.

        ``supports_tool_calling()`` must return ``True`` because all
        Gemini 2.5 models expose the function-calling API required by
        the agent tool-use loop.
        """
        be = GoogleBackend(api_key="fake-key")
        assert be.supports_tool_calling() is True

    def test_verify_supports_tool_calling_across_models(self):
        """Validate that tool calling is enabled for all tested Gemini 2.5 models.

        Iterates over Pro and Flash and asserts ``True`` for each
        because the agent framework requires uniform tool-call support
        across all available Gemini backends.
        """
        models = ["gemini-2.5-pro", "gemini-2.5-flash"]
        for m in models:
            be = GoogleBackend(api_key="fake-key", model=m)
            assert be.supports_tool_calling() is True, f"model={m}"

    def test_verify_supports_thinking(self):
        """Validate that Gemini 2.5 models support thinking/reasoning mode.

        ``supports_thinking()`` must be ``True`` because the Gemini 2.5
        line supports extended thinking tokens that the backend must
        forward to the client.
        """
        be = GoogleBackend(api_key="fake-key")
        assert be.supports_thinking() is True

    def test_verify_supports_grounding(self):
        """Validate that Gemini models advertise grounding capability.

        ``supports_grounding()`` must be ``True`` because the backend
        exposes the grounding feature flag so the agent loop can
        conditionally enable Google Search lookups.
        """
        be = GoogleBackend(api_key="fake-key")
        assert be.supports_grounding() is True

    def test_verify_supports_prompt_caching_returns_bool(self):
        """Validate that ``supports_prompt_caching`` returns a boolean value.

        The result must be a ``bool`` because the agent loop branches
        on this flag to decide whether to attach prompt-cache control
        headers to the request.
        """
        be = GoogleBackend(api_key="fake-key")
        result = be.supports_prompt_caching()
        assert isinstance(result, bool)


# ===========================================================================
# Context window size
# ===========================================================================


class TestGoogleBackendContextWindow:
    """Validate context-window sizes for Gemini models.

    Exercises ``context_window_size()`` across three scenarios: the
    default Pro model, the Flash variant, and a positivity check. The
    agent loop must truncate conversations to the correct 1,048,576
    token limit so requests never exceed Google's context cap.
    """

    def test_verify_context_window_size_default(self):
        """Validate that the default Gemini model reports a 1,048,576-token context window.

        Gemini 2.5 Pro supports the full million-token context required
        for long-document analysis workflows.
        """
        be = GoogleBackend(api_key="fake-key")
        assert be.context_window_size() == 1048576

    def test_verify_context_window_size_flash(self):
        """Validate that Gemini 2.5 Flash reports a 1,048,576-token context window.

        The Flash tier shares the same million-token context capacity
        as Pro.
        """
        be = GoogleBackend(api_key="fake-key", model="gemini-2.5-flash")
        assert be.context_window_size() == 1048576

    def test_verify_context_window_is_positive_integer(self):
        """Validate that the default model returns a positive integer context size.

        A non-positive or non-integer context size would cause the agent
        loop to discard all conversation history immediately.
        """
        be = GoogleBackend(api_key="fake-key")
        assert be.context_window_size() > 0
        assert isinstance(be.context_window_size(), int)


# ===========================================================================
# Token counting and model attribute
# ===========================================================================


class TestGoogleBackendTokens:
    """Validate token-counting resilience and model attribute access.

    Exercises ``count_tokens()`` across four scenarios: normal text,
    empty strings, long text, and direct model attribute access. Token
    counting must never raise on valid input, and the ``model``
    attribute must faithfully reflect the constructor argument.
    """

    def test_verify_count_tokens_returns_int(self):
        """Validate that ``count_tokens`` returns an integer for normal text.

        The token counter must always produce an ``int`` return type,
        even when the underlying tokenizer is unavailable.
        """
        be = GoogleBackend(api_key="fake-key")
        result = be.count_tokens("hello world")
        assert isinstance(result, int)

    def test_verify_count_tokens_empty_string(self):
        """Validate that ``count_tokens`` does not crash on an empty string.

        The tokenizer must handle the zero-length edge case without
        raising and still return an ``int``.
        """
        be = GoogleBackend(api_key="fake-key")
        result = be.count_tokens("")
        assert isinstance(result, int)

    def test_verify_count_tokens_long_text(self):
        """Validate that ``count_tokens`` does not crash on long repeated text.

        Token counting must scale gracefully to large inputs without
        throwing, even if the returned count is approximate.
        """
        be = GoogleBackend(api_key="fake-key")
        result = be.count_tokens("Google Gemini token counting. " * 300)
        assert isinstance(result, int)

    def test_verify_model_attribute_matches_constructor(self):
        """Validate that the ``model`` attribute stores the constructor argument exactly.

        The model name is used in every API request and must not be
        mutated, so it must be stored verbatim as a ``str``.
        """
        be = GoogleBackend(api_key="fake-key", model="gemini-2.5-flash")
        assert be.model == "gemini-2.5-flash"
        assert isinstance(be.model, str)


# ===========================================================================
# Message conversion (internal protocol mapping)
# ===========================================================================


class TestGoogleBackendMessageConversion:
    """Validate OpenAI-to-Google message and tool format conversion.

    Exercises ``_convert_messages`` and ``_convert_tools`` across eight
    scenarios: simple user messages, system-message extraction into
    ``systemInstruction``, assistant-role mapping, empty-input handling,
    OpenAI-to-Google tool conversion, non-function tool skipping,
    empty tool lists, and missing-description tolerance. The backend
    must translate the internal message protocol into Google's Gemini
    API format so conversations round-trip without losing role semantics
    or tool definitions.
    """

    def test_verify_simple_user_message_conversion(self):
        """Validate that a user message is converted to Google format with the correct role and parts.

        A single user message must produce one entry with role
        ``"user"`` and a single text part matching the original content,
        so user text passes through inside the Gemini ``Part`` structure.
        """
        be = GoogleBackend(api_key="fake-key")
        messages = [{"role": "user", "content": "Hello, Gemini!"}]
        contents, _system_instruction = be._convert_messages(messages)
        assert len(contents) == 1
        assert contents[0]["role"] == "user"
        assert len(contents[0]["parts"]) == 1
        assert contents[0]["parts"][0]["text"] == "Hello, Gemini!"

    def test_verify_system_message_extracted_into_system_instruction(self):
        """Validate that system messages are extracted into ``systemInstruction`` rather than contents.

        Gemini's API separates system context from the conversational
        contents stream, so a system message must land in
        ``systemInstruction`` while the contents list holds only the
        user turn.
        """
        be = GoogleBackend(api_key="fake-key")
        messages = [
            {"role": "system", "content": "You are a helpful bot."},
            {"role": "user", "content": "Help me."},
        ]
        contents, system_instruction = be._convert_messages(messages)
        assert len(contents) == 1
        assert system_instruction is not None
        assert system_instruction["parts"][0]["text"] == "You are a helpful bot."

    def test_verify_assistant_role_mapped_to_model(self):
        """Validate that the assistant role is mapped to Gemini's ``model`` role.

        Gemini uses ``model`` instead of ``assistant`` to denote
        AI-generated turns in the contents array.
        """
        be = GoogleBackend(api_key="fake-key")
        messages = [{"role": "assistant", "content": "I can help with that."}]
        contents, _ = be._convert_messages(messages)
        assert len(contents) == 1
        assert contents[0]["role"] == "model"

    def test_verify_empty_message_list_produces_empty_contents(self):
        """Validate that an empty message list returns empty contents and no system instruction.

        A conversation with no messages must not produce any Gemini API
        fields that could be interpreted as spurious input.
        """
        be = GoogleBackend(api_key="fake-key")
        contents, system_instruction = be._convert_messages([])
        assert contents == []
        assert system_instruction is None

    def test_verify_tools_converted_to_function_declarations(self):
        """Validate that OpenAI tool definitions are converted to Google ``functionDeclarations``.

        Gemini requires tools to be declared under the
        ``functionDeclarations`` key rather than the OpenAI ``function``
        wrapper, so a single function tool must produce one declaration
        with the correct name and description.
        """
        be = GoogleBackend(api_key="fake-key")
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ]
        result = be._convert_tools(tools)
        assert len(result) == 1
        assert "functionDeclarations" in result[0]
        decls = result[0]["functionDeclarations"]
        assert len(decls) == 1
        assert decls[0]["name"] == "get_weather"
        assert decls[0]["description"] == "Get current weather"

    def test_verify_non_function_tools_are_skipped(self):
        """Validate that non-function tool types are filtered out during conversion.

        Gemini only supports function-calling tools, so mixed input
        containing ``code_interpreter`` must yield only the function
        tool in the declaration list rather than passing unsupported
        types through.
        """
        be = GoogleBackend(api_key="fake-key")
        tools = [
            {"type": "code_interpreter"},
            {"type": "function", "function": {"name": "calc"}},
        ]
        result = be._convert_tools(tools)
        assert len(result) > 0
        decls = result[0]["functionDeclarations"]
        assert len(decls) == 1
        assert decls[0]["name"] == "calc"

    def test_verify_empty_tool_list_produces_empty_declarations(self):
        """Validate that an empty tool list produces a ``functionDeclarations`` entry with an empty list.

        The Gemini API expects the ``tools`` field to be present even
        when empty rather than omitted, so ``[]`` input must still
        produce one entry containing ``functionDeclarations == []``.
        """
        be = GoogleBackend(api_key="fake-key")
        result = be._convert_tools([])
        assert len(result) == 1
        assert result[0]["functionDeclarations"] == []

    def test_verify_tool_without_description_is_still_converted(self):
        """Validate that a tool missing a description is converted without the description field.

        Descriptions are optional in Gemini's function schema, so a tool
        that supplies only a name must still be declared and must not
        be dropped solely due to the missing description.
        """
        be = GoogleBackend(api_key="fake-key")
        tools = [
            {
                "type": "function",
                "function": {"name": "simple_tool"},
            }
        ]
        result = be._convert_tools(tools)
        decls = result[0]["functionDeclarations"]
        assert len(decls) == 1
        assert decls[0]["name"] == "simple_tool"
        assert "description" not in decls[0]


# ===========================================================================
# Finish reason mapping
# ===========================================================================


class TestGoogleBackendFinishReason:
    """Validate ``_map_finish_reason`` Google-to-unified finish-reason translation.

    Exercises the finish-reason mapper across five scenarios: stop,
    max-tokens, safety, recitation, and unknown-fallback. Gemini-specific
    finish reasons must be translated into the unified finish-reason
    strings the agent loop expects so termination logic works uniformly
    across all backend providers.
    """

    def test_verify_stop_reason_maps_to_stop(self):
        """Validate that Gemini's ``STOP`` reason maps to the unified ``"stop"`` string.

        A normal completed response must be recognized as a clean
        termination by the agent loop.
        """
        be = GoogleBackend(api_key="fake-key")
        assert be._map_finish_reason("STOP") == "stop"

    def test_verify_max_tokens_reason_maps_to_max_tokens(self):
        """Validate that Gemini's ``MAX_TOKENS`` reason maps to ``"max_tokens"``.

        Output-length exhaustion must be distinguished from normal
        completion so the agent can decide whether to continue the
        turn or truncate the response.
        """
        be = GoogleBackend(api_key="fake-key")
        assert be._map_finish_reason("MAX_TOKENS") == "max_tokens"

    def test_verify_safety_reason_maps_to_error(self):
        """Validate that Gemini's ``SAFETY`` reason maps to ``"error"``.

        Safety-blocked responses must be treated as failures by the
        agent loop rather than as successful completions.
        """
        be = GoogleBackend(api_key="fake-key")
        assert be._map_finish_reason("SAFETY") == "error"

    def test_verify_recitation_reason_maps_to_error(self):
        """Validate that Gemini's ``RECITATION`` reason maps to ``"error"``.

        Recitation-blocked responses must be treated as failures so the
        agent can retry with a rephrased prompt.
        """
        be = GoogleBackend(api_key="fake-key")
        assert be._map_finish_reason("RECITATION") == "error"

    def test_verify_unknown_reason_falls_back_to_stop(self):
        """Validate that unrecognized finish reasons fall back to ``"stop"``.

        Unknown reasons must not crash the mapper; treating them as
        benign termination lets the loop proceed rather than entering
        an error state.
        """
        be = GoogleBackend(api_key="fake-key")
        assert be._map_finish_reason("UNKNOWN_REASON") == "stop"


# ===========================================================================
# Lifecycle
# ===========================================================================


class TestGoogleBackendLifecycle:
    """Validate backend resource cleanup and lifecycle safety.

    Exercises ``aclose()`` across two scenarios: normal client closure
    and idempotent double-close. The async cleanup path must close the
    underlying httpx client and never raise on repeated calls, preventing
    unhandled exceptions during server shutdown.
    """

    def test_verify_aclose_closes_http_client(self):
        """Validate that ``aclose()`` closes the underlying httpx client.

        The HTTP client must be properly released back to the event
        loop on backend shutdown to avoid connection leaks.
        """

        async def _close():
            """Close the backend and verify the client is closed."""
            be = GoogleBackend(api_key="fake-key")
            assert be._client is not None
            await be.aclose()
            assert be._client.is_closed

        asyncio.run(_close())

    def test_verify_aclose_is_idempotent(self):
        """Validate that calling ``aclose()`` twice does not raise.

        Shutdown handlers may invoke cleanup multiple times, so
        idempotency prevents error propagation on the second call.
        """

        async def _double_close():
            """Close the backend twice in sequence."""
            be = GoogleBackend(api_key="fake-key")
            await be.aclose()
            await be.aclose()

        asyncio.run(_double_close())
