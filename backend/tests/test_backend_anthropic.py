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

"""Tests for AnthropicBackend -- construction, capabilities, context window,
thinking config, prompt caching, and token counting.

The suite instantiates the backend with dummy credentials and checks the
model defaults, the per-tier capability predicates, the 200K context budget,
the ephemeral cache_control injection in _apply_prompt_caching, the chat()
signature, and the async close lifecycle.
"""

import asyncio

from encre.backends.anthropic import AnthropicBackend

# ===========================================================================
# Construction
# ===========================================================================


class TestAnthropicBackendConstruction:
    """Engineered to validate ``AnthropicBackend`` instantiation across parameter combinations.

    This test class exercises constructor behavior across 5 scenarios covering
    default values, custom model selection, haiku-tier model, empty API key
    handling, and HTTP client initialization. The design ensures the backend
    initializes with the correct Claude model default and creates an active
    httpx async client so that subsequent chat requests can be issued immediately.
    """

    def test_verify_default_model_and_api_key(self):
        """Validate that defaults are ``claude-sonnet-5`` and the supplied API key is stored.

        The test exercises construction with only an API key and asserts the model,
        api_key, and default values match expectations because the default Claude
        model must be the current sonnet generation for optimal cost-quality balance.
        """
        be = AnthropicBackend(api_key="sk-ant-test")
        assert be.model == "claude-sonnet-5"
        assert be.api_key == "sk-ant-test"

    def test_verify_custom_model_is_stored(self):
        """Validate that an explicit opus model name is preserved on the backend instance.

        The test exercises construction with ``model="claude-opus-4-20250514"``
        and asserts the attribute matches because opus is the highest-tier
        Claude model and must be selectable without post-construction mutation.
        """
        be = AnthropicBackend(
            api_key="sk-ant-test",
            model="claude-opus-4-20250514",
        )
        assert be.model == "claude-opus-4-20250514"

    def test_verify_haiku_model_variant(self):
        """Validate that the haiku-tier model is accepted and stored correctly.

        The test exercises construction with ``model="claude-haiku-4-20250514"``
        and asserts the attribute matches because haiku is a distinct low-cost
        model variant with different capability and pricing characteristics.
        """
        be = AnthropicBackend(
            api_key="sk-ant-test",
            model="claude-haiku-4-20250514",
        )
        assert be.model == "claude-haiku-4-20250514"

    def test_verify_empty_api_key_is_allowed(self):
        """Validate that construction without an API key does not raise.

        The test exercises parameterless construction and asserts ``api_key`` is
        empty string and the model defaults to ``claude-sonnet-5`` because some
        deployments inject credentials via environment variables after construction.
        """
        be = AnthropicBackend()
        assert be.api_key == ""
        assert be.model == "claude-sonnet-5"

    def test_verify_http_client_is_initialized(self):
        """Validate that construction creates an active httpx.AsyncClient.

        The test exercises construction and asserts ``_client`` is not None and
        has a ``base_url`` attribute because the Anthropic backend creates its
        own HTTP client eagerly (unlike the OpenAI backend which uses lazy init)
        so that connection pooling is ready immediately.
        """
        be = AnthropicBackend(api_key="sk-ant-test")
        assert be._client is not None
        assert hasattr(be._client, "base_url")


# ===========================================================================
# Capability checks
# ===========================================================================


class TestAnthropicBackendCapabilities:
    """Engineered to validate capability flags across Claude model variants.

    This test class exercises ``supports_tool_calling``, ``supports_thinking``,
    and ``supports_prompt_caching`` across 7 scenarios covering opus, sonnet,
    and haiku tiers. The design ensures each capability flag returns the
    correct boolean so that the agent loop can enable tool-use, reasoning
    token passthrough, and prompt-caching headers per model correctly.
    """

    def test_verify_supports_tool_calling_default(self):
        """Validate that the default Claude model supports native tool_use.

        The test exercises the default backend and asserts ``supports_tool_calling()``
        returns ``True`` because all Claude models expose the native ``tool_use``
        block type required by the agent tool-execution loop.
        """
        be = AnthropicBackend(api_key="sk-ant-test")
        assert be.supports_tool_calling() is True

    def test_verify_supports_tool_calling_across_all_models(self):
        """Validate that tool calling is enabled for every Claude model tier.

        The test iterates over opus, sonnet, and haiku and asserts ``True`` for
        each because the agent framework requires uniform tool-call support
        across all available Claude backends.
        """
        models = [
            "claude-opus-4-20250514",
            "claude-sonnet-4-20250514",
            "claude-haiku-4-20250514",
        ]
        for m in models:
            be = AnthropicBackend(api_key="sk-ant-test", model=m)
            assert be.supports_tool_calling() is True, f"model={m}"

    def test_verify_supports_thinking_opus(self):
        """Validate that Claude Opus reports thinking support.

        The test exercises the opus model and asserts ``supports_thinking()``
        is ``True`` because opus supports extended-thinking mode which emits
        ``thinking`` blocks that the backend must forward to the client.
        """
        be = AnthropicBackend(api_key="sk-ant-test", model="claude-opus-4-20250514")
        assert be.supports_thinking() is True

    def test_verify_supports_thinking_sonnet(self):
        """Validate that Claude Sonnet reports thinking support.

        The test exercises the sonnet model and asserts ``supports_thinking()``
        is ``True`` because sonnet also supports the extended-thinking mode
        introduced in the 2025 model series.
        """
        be = AnthropicBackend(api_key="sk-ant-test", model="claude-sonnet-4-20250514")
        assert be.supports_thinking() is True

    def test_verify_supports_thinking_haiku(self):
        """Validate that Claude Haiku reports thinking support.

        The test exercises the haiku model and asserts ``supports_thinking()``
        is ``True`` because all Claude models in the 2025 line uniformly
        support the thinking flag even if haiku's reasoning depth is limited.
        """
        be = AnthropicBackend(api_key="sk-ant-test", model="claude-haiku-4-20250514")
        assert be.supports_thinking() is True

    def test_verify_supports_prompt_caching_default(self):
        """Validate that the default model supports prompt caching at 90% discount.

        The test exercises the default backend and asserts ``supports_prompt_caching()``
        is ``True`` because Anthropic offers a 90% caching discount on system
        prompt storage which the backend must advertise to the agent loop.
        """
        be = AnthropicBackend(api_key="sk-ant-test")
        assert be.supports_prompt_caching() is True

    def test_verify_supports_prompt_caching_across_all_models(self):
        """Validate that prompt caching is enabled for every Claude model tier.

        The test iterates over opus, sonnet, and haiku and asserts ``True`` for
        each because Anthropic's caching feature is available across the entire
        Claude family and must be advertised uniformly.
        """
        models = [
            "claude-opus-4-20250514",
            "claude-sonnet-4-20250514",
            "claude-haiku-4-20250514",
        ]
        for m in models:
            be = AnthropicBackend(api_key="sk-ant-test", model=m)
            assert be.supports_prompt_caching() is True, f"model={m}"


# ===========================================================================
# Context window size
# ===========================================================================


class TestAnthropicBackendContextWindow:
    """Engineered to validate context-window sizes for all Claude model tiers.

    This test class exercises ``context_window_size()`` across 4 scenarios
    covering opus, sonnet, haiku, and a positivity check. The design ensures
    the agent loop can truncate conversations to the correct per-model limit
    so that API requests never exceed Anthropic's context cap of 200K tokens.
    """

    def test_verify_context_window_size_opus(self):
        """Validate that Claude Opus reports a 200,000-token context window.

        The test exercises the opus model and asserts 200000 because opus
        shares the same context budget as the rest of the Claude 4 family.
        """
        be = AnthropicBackend(api_key="sk-ant-test", model="claude-opus-4-20250514")
        assert be.context_window_size() == 200000

    def test_verify_context_window_size_sonnet(self):
        """Validate that Claude Sonnet reports a 200,000-token context window.

        The test exercises the sonnet model and asserts 200000 because sonnet
        supports the full 200K context required for long-document workflows.
        """
        be = AnthropicBackend(api_key="sk-ant-test", model="claude-sonnet-4-20250514")
        assert be.context_window_size() == 200000

    def test_verify_context_window_size_haiku(self):
        """Validate that Claude Haiku reports a 200,000-token context window.

        The test exercises the haiku model and asserts 200000 because the
        haiku tier retains the same context capacity as the higher tiers.
        """
        be = AnthropicBackend(api_key="sk-ant-test", model="claude-haiku-4-20250514")
        assert be.context_window_size() == 200000

    def test_verify_context_window_is_positive_integer(self):
        """Validate that the default model returns a positive integer context size.

        The test exercises the default backend and asserts the result is an
        ``int`` greater than zero because a non-positive context size would
        cause the agent loop to discard all conversation history immediately.
        """
        be = AnthropicBackend(api_key="sk-ant-test")
        assert be.context_window_size() > 0
        assert isinstance(be.context_window_size(), int)


# ===========================================================================
# Token counting
# ===========================================================================


class TestAnthropicBackendTokens:
    """Engineered to validate token-counting resilience for the Anthropic backend.

    This test class exercises ``count_tokens()`` across 3 scenarios covering
    normal text, empty strings, and long repeated text. The design ensures
    token counting never raises on any valid string input so that the agent
    loop can safely estimate context usage before sending requests.
    """

    def test_verify_count_tokens_returns_int(self):
        """Validate that ``count_tokens`` returns an integer for normal text.

        The test exercises a short string and asserts the result is an ``int``
        because the token counter must always produce an integer return type
        even when the underlying tokenizer is unavailable.
        """
        be = AnthropicBackend(api_key="sk-ant-test")
        result = be.count_tokens("hello world")
        assert isinstance(result, int)

    def test_verify_count_tokens_empty_string(self):
        """Validate that ``count_tokens`` does not crash on an empty string.

        The test exercises ``""`` and asserts the result is an ``int`` because
        the tokenizer must handle the zero-length edge case without raising.
        """
        be = AnthropicBackend(api_key="sk-ant-test")
        result = be.count_tokens("")
        assert isinstance(result, int)

    def test_verify_count_tokens_long_text(self):
        """Validate that ``count_tokens`` does not crash on long repeated text.

        The test exercises a 200-repetition string and asserts the result is
        an ``int`` because token counting must scale gracefully to large inputs
        without throwing, even if the count itself is approximate.
        """
        be = AnthropicBackend(api_key="sk-ant-test")
        result = be.count_tokens("Testing token counting. " * 200)
        assert isinstance(result, int)


# ===========================================================================
# Prompt caching -- _apply_prompt_caching static method
# ===========================================================================


class TestAnthropicBackendPromptCaching:
    """Engineered to validate the ``_apply_prompt_caching`` static method for cache-control injection.

    This test class exercises cache-control annotation across 6 scenarios
    covering system-message caching, last-user-message caching, combined
    system-plus-last-user caching, image-content skipping, assistant-message
    exclusion, and empty-input handling. The design ensures the method wraps
    cacheable text blocks in ``{"cache_control": {"type": "ephemeral"}}``
    so that Anthropic's 90% prompt-caching discount is applied to the
    static system prompt and the most recent user turn.
    """

    def test_verify_caches_system_message(self):
        """Validate that the system message receives a cache_control breakpoint.

        The test exercises a two-message conversation and asserts the system
        message content is converted to a list containing a block with
        ``cache_control`` because Anthropic requires cacheControl to be
        attached to a content block, not the message itself.
        """
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]
        result = AnthropicBackend._apply_prompt_caching(messages)
        assert len(result) == 2
        sys_content = result[0]["content"]
        assert isinstance(sys_content, list)
        assert "cache_control" in sys_content[-1]

    def test_verify_caches_last_user_message_only(self):
        """Validate that only the last user message receives a cache_control breakpoint.

        The test exercises two user messages and asserts the first does not
        have cache_control while the last one does because Anthropic's caching
        policy only allows caching the system prompt and the single most
        recent user turn to maximize cache hit rate.
        """
        messages = [
            {"role": "user", "content": "First question"},
            {"role": "user", "content": "Second question"},
        ]
        result = AnthropicBackend._apply_prompt_caching(messages)
        assert len(result) == 2
        content0 = result[0]["content"]
        if isinstance(content0, list):
            has_cache_0 = any("cache_control" in str(b) for b in content0)
            assert not has_cache_0
        content1 = result[1]["content"]
        assert isinstance(content1, list)
        assert "cache_control" in content1[-1]

    def test_verify_caches_system_and_last_user_together(self):
        """Validate that both the system message and the last user message are cached.

        The test exercises a four-message conversation and asserts the system
        message and last user message have cache_control while the assistant
        message does not because only those two positions are cacheable under
        Anthropic's prompt-caching rules.
        """
        messages = [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
        ]
        result = AnthropicBackend._apply_prompt_caching(messages)
        sys_content = result[0]["content"]
        assert isinstance(sys_content, list)
        assert "cache_control" in sys_content[-1]
        asst_content = result[2]["content"]
        if isinstance(asst_content, list):
            has_cache = any("cache_control" in str(b) for b in asst_content)
            assert not has_cache
        elif isinstance(asst_content, str):
            pass
        last_user_content = result[3]["content"]
        assert isinstance(last_user_content, list)
        assert "cache_control" in last_user_content[-1]

    def test_verify_image_blocks_skip_caching_while_text_blocks_get_it(self):
        """Validate that image content blocks are left uncacheable and text blocks are cached.

        The test exercises a user message with an image block followed by a
        text block and asserts the image block has no cache_control while
        the text block has ``{"type": "ephemeral"}`` because image payloads
        cannot be cached by Anthropic's CDN but text blocks can.
        """
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "data": "abc123", "media_type": "image/png"}},
                    {"type": "text", "text": "Describe this image."},
                ],
            },
        ]
        result = AnthropicBackend._apply_prompt_caching(messages)
        blocks = result[0]["content"]
        assert "cache_control" not in blocks[0]
        assert "cache_control" in blocks[1]
        assert blocks[1]["cache_control"] == {"type": "ephemeral"}

    def test_verify_assistant_messages_are_not_cached(self):
        """Validate that assistant messages are not annotated with cache_control.

        The test exercises a single assistant message and asserts its content
        remains a plain string without cache_control because Anthropic does
        not allow caching model-generated content blocks.
        """
        messages = [
            {"role": "assistant", "content": "I am Claude."},
        ]
        result = AnthropicBackend._apply_prompt_caching(messages)
        assert isinstance(result[0]["content"], str)

    def test_verify_empty_message_list_returns_empty(self):
        """Validate that an empty input list returns an empty output list.

        The test exercises ``[]`` and asserts the result is ``[]`` because
        the caching method must be a no-op on empty input rather than
        raising or returning a sentinel value.
        """
        result = AnthropicBackend._apply_prompt_caching([])
        assert result == []


# ===========================================================================
# Chat method signature / parameter inspection
# ===========================================================================


class TestAnthropicBackendChatSignature:
    """Engineered to validate the ``chat()`` method signature via introspection.

    This test class exercises ``inspect`` checks across 4 scenarios covering
    async-generator status, ``enable_caching``, ``max_tokens``, and ``tools``
    parameter presence. The design ensures the public chat interface exposes
    the parameters required by the agent loop for caching control, output
    length limits, and tool-definition injection.
    """

    def test_verify_chat_is_async_function(self):
        """Validate that ``chat`` is registered as an async function or async generator.

        The test exercises ``inspect.iscoroutinefunction`` and
        ``inspect.isasyncgenfunction`` and asserts at least one is ``True``
        because the agent loop awaits the chat coroutine and must not call
        a synchronous function.
        """
        import inspect
        assert inspect.iscoroutinefunction(AnthropicBackend.chat) or inspect.isasyncgenfunction(AnthropicBackend.chat)

    def test_verify_chat_accepts_enable_caching(self):
        """Validate that the chat signature includes the ``enable_caching`` parameter.

        The test exercises ``inspect.signature`` and asserts ``enable_caching``
        is present because the agent loop passes this flag to control whether
        prompt-caching headers are injected into the request.
        """
        import inspect
        sig = inspect.signature(AnthropicBackend.chat)
        params = sig.parameters
        assert "enable_caching" in params

    def test_verify_chat_accepts_max_tokens_with_default_4096(self):
        """Validate that the chat signature includes ``max_tokens`` with default 4096.

        The test exercises ``inspect.signature`` and asserts ``max_tokens`` is
        present and its default is 4096 because the agent loop relies on this
        default to bound output length without explicitly passing the value.
        """
        import inspect
        sig = inspect.signature(AnthropicBackend.chat)
        params = sig.parameters
        assert "max_tokens" in params
        assert params["max_tokens"].default == 4096

    def test_verify_chat_accepts_tools(self):
        """Validate that the chat signature includes the optional ``tools`` parameter.

        The test exercises ``inspect.signature`` and asserts ``tools`` is present
        because the agent loop injects tool definitions on every turn that
        requires function calling.
        """
        import inspect
        sig = inspect.signature(AnthropicBackend.chat)
        assert "tools" in sig.parameters


# ===========================================================================
# Lifecycle
# ===========================================================================


class TestAnthropicBackendLifecycle:
    """Engineered to validate backend resource cleanup and lifecycle safety.

    This test class exercises ``aclose()`` across 2 scenarios covering normal
    client closure and idempotent double-close. The design ensures that the
    async cleanup path closes the underlying httpx client and never raises
    on repeated calls, preventing unhandled exceptions during server shutdown.
    """

    def test_verify_aclose_closes_http_client(self):
        """Validate that ``aclose()`` closes the underlying httpx client.

        The test exercises construction, asserts the client exists, calls
        ``aclose()``, then asserts ``is_closed`` is ``True`` because the
        HTTP client must be properly released back to the event loop on
        backend shutdown to avoid connection leaks.
        """

        async def _close():
            """Close the backend and verify the client is closed."""
            be = AnthropicBackend(api_key="sk-ant-test")
            assert be._client is not None
            await be.aclose()
            assert be._client.is_closed

        asyncio.run(_close())

    def test_verify_aclose_is_idempotent(self):
        """Validate that calling ``aclose()`` twice does not raise.

        The test exercises a double-close sequence inside an async helper and
        asserts no exception is raised because shutdown handlers may invoke
        cleanup multiple times and idempotency prevents error propagation.
        """

        async def _double_close():
            """Close the backend twice in sequence."""
            be = AnthropicBackend(api_key="sk-ant-test")
            await be.aclose()
            await be.aclose()  # Safe to call repeatedly.

        asyncio.run(_double_close())
