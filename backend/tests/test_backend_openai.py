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

"""Tests for OpenAIBackend -- construction, capabilities, context window,
token counting, request-body assembly, prompt caching, and lifecycle."""

import asyncio

from encre.backends.openai import OpenAIBackend

# ===========================================================================
# Construction
# ===========================================================================


class TestOpenAIBackendConstruction:
    """Validate ``OpenAIBackend`` instantiation across parameter combinations.

    Covers default values, custom model selection, custom base URL overrides,
    nano/reasoning model variants, empty API key handling, and HTTP timeout
    forwarding. The backend must initialize with predictable defaults and
    propagate user-supplied parameters to internal attributes so downstream
    request building uses the intended model and endpoint.
    """

    def test_verify_default_model_and_base_url(self):
        """Validate that defaults are ``gpt-5.6`` and the official OpenAI endpoint.

        Construction with only an API key must leave ``model``, ``api_key``,
        and ``api_base_url`` at their documented defaults so that users who
        omit optional parameters get a working backend out of the box.
        """
        be = OpenAIBackend(api_key="sk-test")
        assert be.model == "gpt-5.6"
        assert be.api_key == "sk-test"
        assert be.api_base_url == "https://api.openai.com/v1"

    def test_verify_custom_model_is_stored(self):
        """Validate that an explicit model name is preserved on the backend instance.

        Model selection must be honored so callers can target specific model
        generations without post-construction mutation.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-4.1-mini")
        assert be.model == "gpt-4.1-mini"

    def test_verify_custom_base_url_overrides_default(self):
        """Validate that a custom base_url replaces the default OpenAI endpoint.

        A custom ``base_url`` must be stored verbatim so proxy deployments
        and compatible API gateways can redirect requests to an alternate
        origin.
        """
        be = OpenAIBackend(
            api_key="sk-test",
            base_url="https://custom.openai.example.com/v1",
        )
        assert be.api_base_url == "https://custom.openai.example.com/v1"

    def test_verify_nano_model_variant(self):
        """Validate that the GPT-4.1 Nano model is accepted and stored.

        The nano tier is a distinct model variant with different pricing
        and capability characteristics than the base model.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-4.1-nano")
        assert be.model == "gpt-4.1-nano"

    def test_verify_o3_reasoning_model(self):
        """Validate that the o3 reasoning model is accepted and stored.

        Reasoning models require different capability flags (e.g.
        ``supports_thinking``) and context-window sizing, so the model
        name must round-trip through construction.
        """
        be = OpenAIBackend(api_key="sk-test", model="o3")
        assert be.model == "o3"

    def test_verify_gpt5_5_top_tier_model(self):
        """Validate that the GPT-5.5 top-tier model is accepted and stored.

        The highest-tier model has a larger context window than
        mid-tier variants, so the model name must be preserved exactly.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-5.5")
        assert be.model == "gpt-5.5"

    def test_verify_empty_api_key_is_allowed(self):
        """Validate that construction without an API key does not raise.

        ``api_key`` must default to an empty string and ``model`` to
        ``gpt-5.6`` because some deployments inject the API key via
        environment variables or a secrets manager after construction.
        """
        be = OpenAIBackend()
        assert be.api_key == ""
        assert be.model == "gpt-5.6"

    def test_verify_http_timeout_is_forwarded(self):
        """Validate that the ``http_timeout`` kwarg is propagated to the parent SSE backend.

        HTTP client timeout configuration must be transparently forwarded
        so callers can tune request deadlines per backend.
        """
        be = OpenAIBackend(api_key="sk-test", http_timeout=60.0)
        assert be.http_timeout == 60.0


# ===========================================================================
# Capability checks
# ===========================================================================


class TestOpenAIBackendCapabilities:
    """Validate capability flags across OpenAI model variants.

    Exercises ``supports_tool_calling``, ``supports_thinking``, and
    ``supports_prompt_caching`` across nine scenarios covering the full
    model range (GPT-4.1, GPT-4.1 Mini, GPT-5.2, o3, o4-mini, GPT-5.5).
    Each flag must return the correct boolean so the agent loop can make
    informed decisions about tool-use availability, reasoning token
    passthrough, and prompt-caching eligibility per model.
    """

    def test_verify_supports_tool_calling_default(self):
        """Validate that the default model supports tool calling.

        ``supports_tool_calling()`` must return ``True`` because all
        supported OpenAI models expose the function-calling API required
        by the agent tool-use loop.
        """
        be = OpenAIBackend(api_key="sk-test")
        assert be.supports_tool_calling() is True

    def test_verify_supports_tool_calling_across_all_models(self):
        """Validate that tool calling is enabled for every supported OpenAI model.

        Iterates over the full model list and asserts ``True`` for each
        because the agent framework requires uniform tool-call support
        across all available model backends.
        """
        models = ["gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-5.5", "o3"]
        for m in models:
            be = OpenAIBackend(api_key="sk-test", model=m)
            assert be.supports_tool_calling() is True, f"model={m}"

    def test_verify_supports_thinking_gpt4_1(self):
        """Validate that GPT-4.1 passes through thinking tokens.

        ``supports_thinking()`` must be ``True`` because the 2026 OpenAI
        backend line forwards reasoning-content tokens when the model
        emits them.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-4.1")
        assert be.supports_thinking() is True

    def test_verify_supports_thinking_gpt4_1_mini(self):
        """Validate that GPT-4.1 Mini passes through thinking tokens.

        The 2026 backend line uniformly supports thinking token
        passthrough regardless of model tier.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-4.1-mini")
        assert be.supports_thinking() is True

    def test_verify_supports_thinking_gpt5(self):
        """Validate that GPT-5.2 passes through thinking tokens.

        Mid-tier 2026 models retain the reasoning-token passthrough
        path, so ``supports_thinking()`` must be ``True``.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-5.2")
        assert be.supports_thinking() is True

    def test_verify_supports_thinking_o3(self):
        """Validate that the o3 reasoning model reports thinking support.

        o3 is explicitly a reasoning model that emits extended
        chain-of-thought tokens which the backend must forward.
        """
        be = OpenAIBackend(api_key="sk-test", model="o3")
        assert be.supports_thinking() is True

    def test_verify_supports_thinking_o4_mini(self):
        """Validate that the o4-mini reasoning model reports thinking support.

        o4-mini is a reasoning-model variant in the o-series line, so
        thinking support must be advertised.
        """
        be = OpenAIBackend(api_key="sk-test", model="o4-mini")
        assert be.supports_thinking() is True

    def test_verify_supports_prompt_caching_returns_bool(self):
        """Validate that ``supports_prompt_caching()`` returns a boolean value.

        The return type must be ``bool`` because the agent loop branches
        on this flag to decide whether to attach prompt-cache control
        headers to the request.
        """
        be = OpenAIBackend(api_key="sk-test")
        result = be.supports_prompt_caching()
        assert isinstance(result, bool)


# ===========================================================================
# Context window size
# ===========================================================================


class TestOpenAIBackendContextWindow:
    """Validate context-window sizes for every OpenAI model variant.

    Exercises ``context_window_size()`` across nine scenarios covering
    GPT-4.1 (1M), o3 (200K), o4-mini (200K), GPT-5.2 (128K default
    fallback), GPT-5.4 (400K), and GPT-5.5 (1M). The agent loop must
    truncate conversations correctly based on the actual API limit per
    model so requests never exceed the provider's context cap.
    """

    def test_verify_context_gpt4_1(self):
        """Validate that GPT-4.1 reports a 1,048,576-token context window.

        GPT-4.1 supports the full million-token context required for
        long-document analysis workflows.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-4.1")
        assert be.context_window_size() == 1048576

    def test_verify_context_gpt4_1_mini(self):
        """Validate that GPT-4.1 Mini reports a 1,048,576-token context window.

        The Mini tier retains the full context capacity of the base
        GPT-4.1 line.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-4.1-mini")
        assert be.context_window_size() == 1048576

    def test_verify_context_gpt4_1_nano(self):
        """Validate that GPT-4.1 Nano reports a 1,048,576-token context window.

        The Nano tier shares the same context budget as the broader
        GPT-4.1 family.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-4.1-nano")
        assert be.context_window_size() == 1048576

    def test_verify_context_o3(self):
        """Validate that o3 reports a 200,000-token context window.

        Reasoning models have a smaller context budget than the standard
        GPT line.
        """
        be = OpenAIBackend(api_key="sk-test", model="o3")
        assert be.context_window_size() == 200000

    def test_verify_context_o4_mini(self):
        """Validate that o4-mini reports a 200,000-token context window.

        The o-series lookup runs before the generic fallback, so
        o4-mini resolves to the reasoning-model context budget.
        """
        be = OpenAIBackend(api_key="sk-test", model="o4-mini")
        assert be.context_window_size() == 200000

    def test_verify_context_gpt5_2(self):
        """Validate that GPT-5.2 falls back to the 128,000-token default.

        GPT-5.2 is not explicitly listed in the context map, so it must
        resolve to the default fallback value.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-5.2")
        assert be.context_window_size() == 128000

    def test_verify_context_gpt5_4(self):
        """Validate that GPT-5.4 reports a 400,000-token context window.

        This mid-high tier model has an expanded context window above
        the default fallback.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-5.4")
        assert be.context_window_size() == 400000

    def test_verify_context_gpt5_5(self):
        """Validate that GPT-5.5 reports the maximum 1,048,576-token context window.

        The top-tier model supports the full million-token context for
        longest-horizon tasks.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-5.5")
        assert be.context_window_size() == 1048576

    def test_verify_context_always_positive_integer(self):
        """Validate that every supported model returns a positive integer context size.

        A non-positive or non-integer context size would cause the agent
        loop to truncate all conversation history immediately.
        """
        models = ["gpt-4.1", "gpt-4.1-mini", "gpt-5.2", "o3", "o4-mini"]
        for m in models:
            be = OpenAIBackend(api_key="sk-test", model=m)
            assert be.context_window_size() > 0, f"model={m}"
            assert isinstance(be.context_window_size(), int), f"model={m}"


# ===========================================================================
# Token counting and model attribute
# ===========================================================================


class TestOpenAIBackendTokens:
    """Validate token-counting resilience and model attribute access.

    Exercises ``count_tokens()`` across five scenarios: normal text,
    empty strings, long text, direct model attribute access, and the
    default model. Token counting must never raise on valid string input
    and the ``model`` attribute must always reflect the constructor
    argument.
    """

    def test_verify_count_tokens_returns_int(self):
        """Validate that ``count_tokens`` returns an integer for normal text.

        The token counter must always produce an ``int`` return type,
        even when the underlying tiktoken library is unavailable
        (returning -1 as a sentinel).
        """
        be = OpenAIBackend(api_key="sk-test")
        result = be.count_tokens("hello world")
        assert isinstance(result, int)

    def test_verify_count_tokens_empty_string(self):
        """Validate that ``count_tokens`` does not crash on an empty string.

        The tokenizer must handle the zero-length edge case without
        raising and still return an ``int``.
        """
        be = OpenAIBackend(api_key="sk-test")
        result = be.count_tokens("")
        assert isinstance(result, int)

    def test_verify_count_tokens_long_text(self):
        """Validate that ``count_tokens`` does not crash on long repeated text.

        Token counting must scale gracefully to inputs that approach
        the context window without throwing.
        """
        be = OpenAIBackend(api_key="sk-test")
        result = be.count_tokens("The quick brown fox jumps over the lazy dog. " * 100)
        assert isinstance(result, int)

    def test_verify_model_attribute_reflects_constructor(self):
        """Validate that the ``model`` attribute stores the constructor argument exactly.

        The model name is used in every API request and must not be
        mutated, so it must be stored verbatim as a ``str``.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-4.1-mini")
        assert be.model == "gpt-4.1-mini"
        assert isinstance(be.model, str)

    def test_verify_model_default(self):
        """Validate that the default model is ``gpt-5.6`` when not specified.

        The default must be a stable, known model so the backend is
        always usable without explicit configuration.
        """
        be = OpenAIBackend(api_key="sk-test")
        assert be.model == "gpt-5.6"


# ===========================================================================
# Request data / token parameter construction
# ===========================================================================


class TestOpenAIBackendRequestBuilding:
    """Validate ``_build_request_data`` request-body assembly.

    Exercises request-body construction across eight scenarios covering
    max_tokens defaults, model injection, message passthrough, streaming
    toggle, and tool-definition inclusion. The request body must conform
    to the OpenAI chat-completions schema so downstream API calls succeed
    without schema-validation errors.
    """

    def test_verify_max_tokens_is_propagated(self):
        """Validate that ``max_tokens`` is included in the request body when provided.

        ``max_tokens=2048`` must appear in the body so the agent loop
        can cap model output length per request.
        """
        be = OpenAIBackend(api_key="sk-test")
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=2048,
        )
        assert "max_tokens" in data
        assert data["max_tokens"] == 2048

    def test_verify_default_max_tokens_is_4096(self):
        """Validate that ``max_tokens`` defaults to 4096 when not provided.

        4096 is the documented default that balances cost and output
        length for typical agent turns.
        """
        be = OpenAIBackend(api_key="sk-test")
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert data["max_tokens"] == 4096

    def test_verify_model_is_included_in_request_body(self):
        """Validate that the request body carries the configured model name.

        ``data["model"]`` must match the configured model because the
        API endpoint needs to know which model to route the request to.
        """
        be = OpenAIBackend(api_key="sk-test", model="gpt-4.1-mini")
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert data["model"] == "gpt-4.1-mini"

    def test_verify_messages_are_passthrough(self):
        """Validate that the conversation messages are forwarded unchanged.

        The backend must not mutate or reorder user-provided messages
        before sending them to the API.
        """
        be = OpenAIBackend(api_key="sk-test")
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        data = be._build_request_data(messages=messages)
        assert data["messages"] == messages

    def test_verify_streaming_is_enabled_by_default(self):
        """Validate that ``stream`` defaults to ``True`` in the request body.

        The agent loop consumes the API as an SSE stream by default,
        so the request body must opt in to streaming when no override
        is given.
        """
        be = OpenAIBackend(api_key="sk-test")
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert data["stream"] is True

    def test_verify_non_streaming_mode_can_be_requested(self):
        """Validate that ``stream=False`` is respected in the request body.

        Non-streaming mode is needed for synchronous fallback paths
        and testing, so the flag must be forwarded verbatim.
        """
        be = OpenAIBackend(api_key="sk-test")
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
            stream=False,
        )
        assert data["stream"] is False

    def test_verify_tools_are_included_when_provided(self):
        """Validate that tool definitions and tool_choice are injected when tools are passed.

        The OpenAI API requires ``tool_choice`` to be set whenever
        ``tools`` is present, so both keys must appear in the body when
        tools are supplied.
        """
        be = OpenAIBackend(api_key="sk-test")
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search the web",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        data = be._build_request_data(
            messages=[{"role": "user", "content": "search for cats"}],
            tools=tools,
        )
        assert "tools" in data
        assert data["tools"] == tools
        assert "tool_choice" in data

    def test_verify_tools_and_tool_choice_are_omitted_when_no_tools(self):
        """Validate that tool-related keys are absent when no tools are provided.

        Omitting ``tools`` and ``tool_choice`` tells the API to use
        pure text mode without function-calling.
        """
        be = OpenAIBackend(api_key="sk-test")
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert "tools" not in data
        assert "tool_choice" not in data


# ===========================================================================
# Prompt caching
# ===========================================================================


class TestOpenAIBackendPromptCaching:
    """Validate the ``_apply_prompt_caching_openai`` static method.

    Exercises prompt-cache boundary splitting across nine scenarios:
    boundary detection, no-boundary passthrough, missing system messages,
    multiple system messages, empty split parts, non-string content
    preservation, ordering preservation, boundary-marker removal, and
    no-op when caching is disabled. The method must split the system
    message at ``__PROMPT_CACHE_BOUNDARY__`` into cacheable prefix and
    non-cacheable suffix portions so the OpenAI prompt-caching feature
    can discount the static system prompt on every subsequent request.
    """

    def test_verify_splits_system_at_boundary_marker(self):
        """Validate that a system message with the boundary marker is split into two messages.

        A system message containing ``__PROMPT_CACHE_BOUNDARY__`` must
        produce three messages: cached system prefix, non-cached system
        suffix, and the user message, since the boundary marks the
        transition between static cacheable content and dynamic content.
        """
        messages = [
            {"role": "system", "content": "You are helpful.__PROMPT_CACHE_BOUNDARY__\nMemory: foo"},
            {"role": "user", "content": "Hello"},
        ]
        result = OpenAIBackend._apply_prompt_caching_openai(messages)
        assert len(result) == 3
        assert result[0]["role"] == "system"
        assert "You are helpful." in result[0]["content"]
        assert result[1]["role"] == "system"
        assert "Memory: foo" in result[1]["content"]
        assert result[2]["role"] == "user"

    def test_verify_no_boundary_leaves_messages_unchanged(self):
        """Validate that messages without a boundary marker pass through unchanged.

        No split is needed when the marker is absent, so the original
        system content must remain intact.
        """
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        result = OpenAIBackend._apply_prompt_caching_openai(messages)
        assert len(result) == 2
        assert result[0]["content"] == "You are helpful."

    def test_verify_only_user_messages_no_split_occurs(self):
        """Validate that a conversation with only user messages is returned unchanged.

        With no system message there is nothing to split and no caching
        boundary to evaluate.
        """
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "World"},
        ]
        result = OpenAIBackend._apply_prompt_caching_openai(messages)
        assert len(result) == 2

    def test_verify_multiple_system_messages_only_boundaries_are_split(self):
        """Validate that only system messages containing the boundary marker are split.

        Two system messages where only the first carries the boundary
        must yield four messages: split prefix, split suffix, untouched
        second system message, and the user message. The splitter must
        only touch messages that carry the boundary token.
        """
        messages = [
            {"role": "system", "content": "Static rules.__PROMPT_CACHE_BOUNDARY__\nDynamic rules"},
            {"role": "system", "content": "Extra system"},
            {"role": "user", "content": "Hi"},
        ]
        result = OpenAIBackend._apply_prompt_caching_openai(messages)
        assert len(result) == 4
        assert result[0]["role"] == "system" and "Static rules" in result[0]["content"]
        assert result[1]["role"] == "system" and "Dynamic rules" in result[1]["content"]
        assert result[2]["role"] == "system"
        assert result[3]["role"] == "user"

    def test_verify_empty_parts_after_splitting_are_dropped(self):
        """Validate that empty prefix or suffix parts after splitting are removed.

        A boundary at the start leaves an empty prefix and a boundary at
        the end leaves an empty suffix; only the non-empty part should
        survive because empty system messages waste API tokens.
        """
        messages = [
            {"role": "system", "content": "__PROMPT_CACHE_BOUNDARY__\nOnly suffix"},
        ]
        result = OpenAIBackend._apply_prompt_caching_openai(messages)
        assert len(result) == 1
        assert "Only suffix" in result[0]["content"]

        messages2 = [
            {"role": "system", "content": "Only prefix\n__PROMPT_CACHE_BOUNDARY__"},
        ]
        result2 = OpenAIBackend._apply_prompt_caching_openai(messages2)
        assert len(result2) == 1
        assert "Only prefix" in result2[0]["content"]

    def test_verify_non_string_content_is_not_modified(self):
        """Validate that multimodal list content bypasses the string-splitting logic.

        A system message whose content is a list (multimodal format)
        must be returned unchanged and still be a list, because the
        splitter operates only on string content and must not corrupt
        structured content blocks.
        """
        messages = [
            {
                "role": "system",
                "content": [{"type": "text", "text": "Hello"}],
            },
        ]
        result = OpenAIBackend._apply_prompt_caching_openai(messages)
        assert len(result) == 1
        assert isinstance(result[0]["content"], list)

    def test_verify_non_system_message_order_is_preserved(self):
        """Validate that non-system messages retain their relative order after splitting.

        The resulting role sequence must be ``["system", "system",
        "user", "assistant", "user"]`` because the split inserts the
        suffix immediately after the prefix without reordering
        subsequent messages.
        """
        messages = [
            {"role": "system", "content": "A.__PROMPT_CACHE_BOUNDARY__\nB."},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
        ]
        result = OpenAIBackend._apply_prompt_caching_openai(messages)
        roles = [m["role"] for m in result]
        assert roles == ["system", "system", "user", "assistant", "user"]

    def test_verify_boundary_marker_is_removed_from_all_split_system_contents(self):
        """Validate that the boundary marker text is stripped from all resulting system messages.

        The marker is a protocol delimiter and must not reach the API
        where it would be treated as literal prompt text, so no
        resulting message content may contain the marker string.
        """
        messages = [
            {"role": "system", "content": "A.__PROMPT_CACHE_BOUNDARY__\nB."},
            {"role": "system", "content": "C.__PROMPT_CACHE_BOUNDARY__\nD."},
        ]
        result = OpenAIBackend._apply_prompt_caching_openai(messages)
        for m in result:
            assert "__PROMPT_CACHE_BOUNDARY__" not in m["content"]

    def test_verify_chat_without_caching_is_left_unchanged(self):
        """Validate that a message list with no boundary marker is returned unchanged.

        With nothing to split the method must be a no-op and return
        the input list as-is.
        """
        messages = [{"role": "user", "content": "Hello"}]
        result = OpenAIBackend._apply_prompt_caching_openai(messages)
        assert result == messages


# ===========================================================================
# Lifecycle
# ===========================================================================


class TestOpenAIBackendLifecycle:
    """Validate backend resource cleanup and lifecycle safety.

    Exercises ``aclose()`` across two scenarios: lazy client
    initialization and idempotent double-close. The async cleanup path
    must never raise whether or not a request has been made, preventing
    unhandled exceptions during server shutdown.
    """

    def test_verify_aclose_does_not_raise_with_lazy_client(self):
        """Validate that ``aclose()`` is safe when no request has been made.

        The HTTP client may be ``None`` (lazy initialization), so the
        cleanup path must handle that gracefully and not raise.
        """
        be = OpenAIBackend(api_key="sk-test")
        asyncio.run(be.aclose())

    def test_verify_aclose_is_idempotent(self):
        """Validate that calling ``aclose()`` twice does not raise.

        Shutdown handlers may call cleanup multiple times, so
        idempotency prevents error propagation on the second call.
        """
        be = OpenAIBackend(api_key="sk-test")

        async def _double_close():
            """Close the backend twice in sequence."""
            await be.aclose()
            await be.aclose()

        asyncio.run(_double_close())
