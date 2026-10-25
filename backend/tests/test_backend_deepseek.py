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

"""Tests for DeepSeekBackend -- construction, capabilities, context window, tokens.

The suite instantiates DeepSeekBackend with dummy credentials and checks model
defaults and base-URL wiring, the legacy deepseek-chat/deepseek-reasoner alias
mapping to the V4-Flash and V4-Pro variants, and the per-feature capability
predicates. A separate section exercises _build_request_data to confirm
DeepSeek-specific sanitization (internal field stripping, null-content coercion,
JSON Schema keyword normalization) and finally covers the async close lifecycle.
"""

import asyncio

from encre.backends.deepseek import DeepSeekBackend

# ===========================================================================
# Construction
# ===========================================================================

class TestDeepSeekBackendConstruction:
    """Engineered to validate ``DeepSeekBackend`` instantiation across parameter combinations.

    This test class exercises constructor behavior across 8 scenarios covering
    default model and base URL, custom model selection, V4-Pro variant, legacy
    model name mapping (``deepseek-chat`` to V4-Flash, ``deepseek-reasoner`` to
    V4-Pro), custom base URL override, empty API key handling, and HTTP timeout
    forwarding. The design ensures the backend points at ``api.deepseek.com``
    by default and correctly resolves deprecated model aliases to their V4
    equivalents so that existing configurations continue to work without
    migration.
    """

    def test_verify_default_model_and_base_url(self):
        """Validate that defaults are ``deepseek-v4-flash`` and the DeepSeek endpoint.

        The test exercises construction with only an API key and asserts the
        model, api_key, and api_base_url attributes match the documented
        defaults because the default configuration must target DeepSeek's
        API endpoint so that requests route correctly out of the box.
        """
        be = DeepSeekBackend(api_key="sk-test")
        assert be.model == "deepseek-v4-flash"
        assert be.api_key == "sk-test"
        assert be.api_base_url == "https://api.deepseek.com"

    def test_verify_custom_model_is_stored(self):
        """Validate that an explicit model name is preserved on the backend instance.

        The test exercises construction with ``model="deepseek-v4-flash"`` and
        asserts the attribute matches because model selection must be honored
        so that callers can target specific variants without post-construction mutation.
        """
        be = DeepSeekBackend(api_key="sk-test", model="deepseek-v4-flash")
        assert be.model == "deepseek-v4-flash"

    def test_verify_v4_pro_model_variant(self):
        """Validate that the DeepSeek V4-Pro model is accepted and stored correctly.

        The test exercises construction with ``model="deepseek-v4-pro"`` and
        asserts the attribute matches because Pro is a distinct higher-capacity
        model variant with different pricing and performance characteristics.
        """
        be = DeepSeekBackend(api_key="sk-test", model="deepseek-v4-pro")
        assert be.model == "deepseek-v4-pro"

    def test_verify_legacy_chat_model_maps_to_v4_flash(self):
        """Validate that the deprecated ``deepseek-chat`` alias resolves to ``deepseek-v4-flash``.

        The test exercises construction with the legacy model name and asserts
        the stored model is ``deepseek-v4-flash`` because backward compatibility
        requires old configuration values to map silently to their modern equivalents.
        """
        be = DeepSeekBackend(api_key="sk-test", model="deepseek-chat")
        assert be.model == "deepseek-v4-flash"

    def test_verify_legacy_reasoner_model_maps_to_v4_pro(self):
        """Validate that the deprecated ``deepseek-reasoner`` alias resolves to ``deepseek-v4-pro``.

        The test exercises construction with the legacy model name and asserts
        the stored model is ``deepseek-v4-pro`` because backward compatibility
        requires old configuration values to map silently to their modern equivalents.
        """
        be = DeepSeekBackend(api_key="sk-test", model="deepseek-reasoner")
        assert be.model == "deepseek-v4-pro"

    def test_verify_custom_base_url_overrides_default(self):
        """Validate that a custom base_url replaces the default DeepSeek endpoint.

        The test exercises construction with a custom ``base_url`` and asserts
        the attribute holds the provided URL because proxy deployments and
        self-hosted compatible servers require alternate origins.
        """
        be = DeepSeekBackend(
            api_key="sk-test",
            base_url="https://custom.deepseek.example.com/v1",
        )
        assert be.api_base_url == "https://custom.deepseek.example.com/v1"

    def test_verify_empty_api_key_is_allowed(self):
        """Validate that construction without an API key does not raise.

        The test exercises parameterless construction and asserts ``api_key`` is
        empty string and the model defaults to ``deepseek-v4-flash`` because
        some deployments inject credentials via environment variables after
        backend construction.
        """
        be = DeepSeekBackend()
        assert be.api_key == ""
        assert be.model == "deepseek-v4-flash"

    def test_verify_http_timeout_is_forwarded(self):
        """Validate that the ``http_timeout`` kwarg is propagated to the parent SSE backend.

        The test exercises construction with ``http_timeout=90.0`` and asserts
        the attribute is 90.0 because HTTP client timeout configuration must
        be transparently forwarded so callers can tune request deadlines per-backend.
        """
        be = DeepSeekBackend(api_key="sk-test", http_timeout=90.0)
        assert be.http_timeout == 90.0


# ===========================================================================
# Capability checks
# ===========================================================================

class TestDeepSeekBackendCapabilities:
    """Engineered to validate capability flags across DeepSeek V4 model variants.

    This test class exercises ``supports_tool_calling``, ``supports_thinking``,
    and ``supports_prompt_caching`` across 5 scenarios covering the default
    model, multiple model names, and per-feature iteration. The design ensures
    each capability flag returns the correct boolean so that the agent loop
    can enable tool-use, reasoning token passthrough, and prompt-caching
    headers (80-92% discount) for all DeepSeek V4 models uniformly.
    """

    def test_verify_supports_tool_calling_default(self):
        """Validate that the default DeepSeek model supports tool calling.

        The test exercises the default backend and asserts ``supports_tool_calling()``
        returns ``True`` because DeepSeek V4 models expose the function-calling
        API required by the agent tool-use loop.
        """
        be = DeepSeekBackend(api_key="sk-test")
        assert be.supports_tool_calling() is True

    def test_verify_supports_tool_calling_across_models(self):
        """Validate that tool calling is enabled for all DeepSeek V4 models tested.

        The test iterates over the legacy chat alias, V4-Flash, and V4-Pro and
        asserts ``True`` for each because the agent framework requires uniform
        tool-call support across all DeepSeek backends including legacy aliases.
        """
        models = ["deepseek-chat", "deepseek-v4-flash", "deepseek-v4-pro"]
        for m in models:
            be = DeepSeekBackend(api_key="sk-test", model=m)
            assert be.supports_tool_calling() is True, f"model={m}"

    def test_verify_supports_thinking_default(self):
        """Validate that the default DeepSeek model supports reasoning/thinking tokens.

        The test exercises the default backend and asserts ``supports_thinking()``
        is ``True`` because DeepSeek V4 models emit reasoning-content tokens
        that the backend must forward to the client.
        """
        be = DeepSeekBackend(api_key="sk-test")
        assert be.supports_thinking() is True

    def test_verify_supports_thinking_across_models(self):
        """Validate that thinking is supported by all DeepSeek V4 models tested.

        The test iterates over the legacy chat alias, V4-Flash, and V4-Pro and
        asserts ``True`` for each because reasoning token passthrough is a
        uniform capability across the entire V4 product line.
        """
        models = ["deepseek-chat", "deepseek-v4-flash", "deepseek-v4-pro"]
        for m in models:
            be = DeepSeekBackend(api_key="sk-test", model=m)
            assert be.supports_thinking() is True, f"model={m}"

    def test_verify_supports_prompt_caching(self):
        """Validate that DeepSeek V4 reports prompt-caching support with 80-92% discount.

        The test exercises the default backend and asserts ``supports_prompt_caching()``
        is ``True`` because DeepSeek offers a significant caching discount on
        repeated system prompts which the backend must advertise to the agent loop.
        """
        be = DeepSeekBackend(api_key="sk-test")
        assert be.supports_prompt_caching() is True


# ===========================================================================
# Context window size
# ===========================================================================

class TestDeepSeekBackendContextWindow:
    """Engineered to validate context-window sizes for all DeepSeek V4 models.

    This test class exercises ``context_window_size()`` across 5 scenarios
    covering the default model, V4-Flash, V4-Pro, the legacy chat alias,
    and a positivity check. The design ensures the agent loop can truncate
    conversations to the correct 1,048,576-token limit so that API requests
    never exceed DeepSeek's context cap regardless of which model alias is used.
    """

    def test_verify_context_window_size_default(self):
        """Validate that the default DeepSeek model reports a 1,048,576-token context window.

        The test exercises the default backend and asserts 1048576 because all
        DeepSeek V4 models share the same one-million-token context budget.
        """
        be = DeepSeekBackend(api_key="sk-test")
        assert be.context_window_size() == 1048576

    def test_verify_context_window_size_v4_flash(self):
        """Validate that V4-Flash reports a 1,048,576-token context window.

        The test exercises the flash model and asserts 1048576 because the
        Flash tier retains the full million-token context capacity.
        """
        be = DeepSeekBackend(api_key="sk-test", model="deepseek-v4-flash")
        assert be.context_window_size() == 1048576

    def test_verify_context_window_size_v4_pro(self):
        """Validate that V4-Pro reports a 1,048,576-token context window.

        The test exercises the pro model and asserts 1048576 because the Pro
        tier shares the same context budget as the Flash tier.
        """
        be = DeepSeekBackend(api_key="sk-test", model="deepseek-v4-pro")
        assert be.context_window_size() == 1048576

    def test_verify_context_window_size_legacy_chat_alias(self):
        """Validate that the legacy ``deepseek-chat`` alias reports a 1M-token context window.

        The test exercises the legacy model name and asserts 1048576 because
        the alias maps to V4-Flash internally and must report the same context
        size as its target model.
        """
        be = DeepSeekBackend(api_key="sk-test", model="deepseek-chat")
        assert be.context_window_size() == 1048576

    def test_verify_context_window_is_positive_integer(self):
        """Validate that the default model returns a positive integer context size.

        The test exercises the default backend and asserts the result is an
        ``int`` greater than zero because a non-positive context size would
        cause the agent loop to discard all conversation history immediately.
        """
        be = DeepSeekBackend(api_key="sk-test")
        assert be.context_window_size() > 0
        assert isinstance(be.context_window_size(), int)


# ===========================================================================
# Token counting and model attribute
# ===========================================================================

class TestDeepSeekBackendTokens:
    """Engineered to validate token-counting resilience and model attribute access.

    This test class exercises ``count_tokens()`` across 4 scenarios covering
    normal text, empty strings, long text, and direct model attribute access.
    The design ensures token counting never raises on valid input and that
    the ``model`` attribute faithfully reflects the (possibly resolved)
    constructor argument.
    """

    def test_verify_count_tokens_returns_int(self):
        """Validate that ``count_tokens`` returns an integer for normal text.

        The test exercises a short string and asserts the result is an ``int``
        because the token counter must always produce an integer return type
        even when the underlying tokenizer is unavailable.
        """
        be = DeepSeekBackend(api_key="sk-test")
        result = be.count_tokens("hello world")
        assert isinstance(result, int)

    def test_verify_count_tokens_empty_string(self):
        """Validate that ``count_tokens`` does not crash on an empty string.

        The test exercises ``""`` and asserts the result is an ``int`` because
        the tokenizer must handle the zero-length edge case without raising.
        """
        be = DeepSeekBackend(api_key="sk-test")
        result = be.count_tokens("")
        assert isinstance(result, int)

    def test_verify_count_tokens_long_text(self):
        """Validate that ``count_tokens`` does not crash on long repeated text.

        The test exercises a 500-repetition string and asserts the result is
        an ``int`` because token counting must scale gracefully to large inputs
        without throwing, even if the count is approximate.
        """
        be = DeepSeekBackend(api_key="sk-test")
        result = be.count_tokens("Test " * 500)
        assert isinstance(result, int)

    def test_verify_model_attribute_matches_constructor(self):
        """Validate that the ``model`` attribute stores the resolved constructor argument exactly.

        The test exercises construction with ``model="deepseek-v4-pro"`` and asserts
        the attribute is that exact string and is an instance of ``str`` because
        the model name is used in every API request and must not be mutated.
        """
        be = DeepSeekBackend(api_key="sk-test", model="deepseek-v4-pro")
        assert be.model == "deepseek-v4-pro"
        assert isinstance(be.model, str)


# ===========================================================================
# Request data building
# ===========================================================================

class TestDeepSeekBackendRequestBuilding:
    """Engineered to validate ``_build_request_data`` including DeepSeek-specific sanitization.

    This test class exercises request-body construction across 5 scenarios
    covering max_tokens propagation, model injection, temperature passthrough,
    internal-field stripping from messages, and tool-schema normalization.
    The design ensures the request body conforms to DeepSeek's API schema
    by removing Encre-internal fields (``branch_id``, ``seq_in_branch``,
    ``reasoning_content``, etc.), coercing null content to empty strings,
    and stripping unsupported JSON Schema keywords so that DeepSeek's
    validator accepts the request without schema-errors.
    """

    def test_verify_max_tokens_is_propagated(self):
        """Validate that ``max_tokens`` is included in the request body when provided.

        The test exercises ``_build_request_data`` with ``max_tokens=512`` and
        asserts the key exists and equals 512 because the agent loop must be
        able to cap model output length per-request.
        """
        be = DeepSeekBackend(api_key="sk-test")
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=512,
        )
        assert data["max_tokens"] == 512

    def test_verify_model_is_included_in_request_body(self):
        """Validate that the request body carries the configured model name.

        The test exercises ``_build_request_data`` with a custom model and
        asserts ``data["model"]`` matches because the DeepSeek API endpoint
        must know which model to route the request to.
        """
        be = DeepSeekBackend(api_key="sk-test", model="deepseek-v4-flash")
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert data["model"] == "deepseek-v4-flash"

    def test_verify_temperature_is_passthrough(self):
        """Validate that ``temperature`` is included in the request body when provided.

        The test exercises ``_build_request_data`` with ``temperature=0.7`` and
        asserts the key exists and equals 0.7 because sampling parameters must
        be forwarded to the API so callers can control output randomness.
        """
        be = DeepSeekBackend(api_key="sk-test")
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.7,
        )
        assert data["temperature"] == 0.7

    def test_verify_internal_message_fields_are_stripped(self):
        """Validate that Encre-internal fields are removed before sending to DeepSeek.

        The test exercises a message carrying internal fields -- ``branch_id``,
        ``seq_in_branch``, ``id``, ``parent_id``, ``usage``, ``segments`` and
        ``_client_id`` -- and asserts none survive, that ``content`` null is
        coerced to an empty string, and that ``tool_calls`` lose their internal
        keys, because DeepSeek's API rejects unknown fields and null content.

        ``reasoning_content`` is the one field with a documented exception: the
        official thinking-mode rule requires an assistant message that carries
        ``tool_calls`` to have its reasoning passed back on every later request
        while thinking is enabled, so it is kept there and stripped everywhere
        else.  All four positions are pinned here.
        """
        be = DeepSeekBackend(api_key="sk-test")
        data = be._build_request_data(
            messages=[{
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                    "_client_id": "call_0_0",
                }],
                "branch_id": "br_0001",
                "seq_in_branch": 5,
                "id": "msg_1",
                "parent_id": "msg_0",
                "usage": {"prompt_tokens": 10},
                "segments": [{"kind": "tool", "tool_id": "call_1"}],
                "reasoning_content": "think",
            }, {
                "role": "tool",
                "content": "ok",
                "tool_call_id": "call_1",
                "_client_id": "call_0_0",
                "branch_id": "br_0001",
            }],
        )
        assistant = data["messages"][0]
        for bad in ("branch_id", "seq_in_branch", "id", "parent_id", "usage", "segments", "_client_id"):
            assert bad not in assistant, bad
        assert assistant["content"] == ""
        # Thinking is on by default and this assistant carries tool_calls, so
        # its reasoning must be passed back.
        assert assistant["reasoning_content"] == "think"
        assert assistant["tool_calls"][0] == {
            "id": "call_1",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"command": "ls"}'},
        }
        tool_msg = data["messages"][1]
        assert tool_msg == {"role": "tool", "content": "ok", "tool_call_id": "call_1"}

        # Thinking off: nothing reasoning-shaped may go back to the API, even
        # on an assistant that carries tool_calls.
        be.thinking_enabled = False
        off = be._build_request_data(messages=[{
            "role": "assistant",
            "content": "done",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }],
            "reasoning_content": "think",
        }])["messages"][0]
        assert "reasoning_content" not in off
        assert off["tool_calls"][0]["id"] == "call_1"

        # Thinking on but no tool_calls: the API ignores that reasoning, so it
        # stays stripped rather than bloating every request.
        plain = DeepSeekBackend(api_key="sk-test")._build_request_data(messages=[{
            "role": "assistant",
            "content": "an answer",
            "reasoning_content": "think",
        }])["messages"][0]
        assert "reasoning_content" not in plain

    def test_verify_tool_schemas_are_normalized_for_deepseek_validation(self):
        """Validate that tool parameter schemas are normalized to pass DeepSeek's validator.

        The test exercises a tool definition with ``minLength`` on a property and
        asserts that all properties are listed in ``required``, ``additionalProperties``
        is set to ``False``, and unsupported keywords like ``minLength`` are stripped
        because DeepSeek's schema validator rejects certain JSON Schema keywords
        that other providers accept.
        """
        be = DeepSeekBackend(api_key="sk-test")
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hi"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "Run shell command",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "minLength": 1},
                            "cwd": {"type": "string"},
                        },
                    },
                },
            }],
        )
        params = data["tools"][0]["function"]["parameters"]
        assert sorted(params["required"]) == ["command", "cwd"]
        assert params["additionalProperties"] is False
        assert "minLength" not in params["properties"]["command"]


# ===========================================================================
# Lifecycle
# ===========================================================================

class TestDeepSeekBackendLifecycle:
    """Engineered to validate backend resource cleanup and lifecycle safety.

    This test class exercises ``aclose()`` across 2 scenarios covering lazy
    client initialization and idempotent double-close. The design ensures
    that the async cleanup path never raises whether or not a request has
    been made, preventing unhandled exceptions during server shutdown.
    """

    def test_verify_aclose_does_not_raise_with_lazy_client(self):
        """Validate that ``aclose()`` is safe when no request has been made.

        The test exercises ``aclose()`` on a freshly constructed backend and
        asserts no exception is raised because the HTTP client may be in a
        lazy-initialized state and the cleanup path must handle that gracefully.
        """
        be = DeepSeekBackend(api_key="sk-test")
        asyncio.run(be.aclose())

    def test_verify_aclose_is_idempotent(self):
        """Validate that calling ``aclose()`` twice does not raise.

        The test exercises a double-close sequence inside an async helper and
        asserts no exception is raised because shutdown handlers may call
        cleanup multiple times and idempotency prevents error propagation.
        """

        async def _double():
            """Close the backend twice in sequence."""
            be = DeepSeekBackend(api_key="sk-test")
            await be.aclose()
            await be.aclose()

        asyncio.run(_double())
