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

"""Tests for GroqBackend -- construction, capabilities, context window, tokens.

Covers ``GroqBackend`` construction and default configuration, capability flags
(tool calling, thinking, prompt caching), the context-window report, token-counting
resilience, ``_build_request_data`` request-body assembly and the ``aclose()``
lifecycle path. No network calls are made; every assertion is against local
state built by the constructor and the request builder.
"""

import asyncio

from encre.backends.groq import GroqBackend

# ===========================================================================
# Construction
# ===========================================================================


class TestGroqBackendConstruction:
    """Validates ``GroqBackend`` instantiation across parameter combinations.

    This test class exercises constructor behavior across 6 scenarios covering
    default model and base URL, custom model selection, GPT-OSS variant, custom
    base URL override, empty API key handling, and HTTP timeout forwarding.
    The design ensures the backend points at ``api.groq.com/openai/v1`` by
    default and correctly propagates user-supplied parameters to internal
    attributes so that downstream request building targets the intended model.
    """

    def test_verify_default_model_and_base_url(self):
        """Validate that defaults are the Llama 4 Scout model and the Groq endpoint.

        The test exercises construction with only an API key and asserts the
        model, api_key, and api_base_url attributes match the documented
        defaults because the default configuration must target Groq's OpenAI-
        compatible endpoint so that requests route correctly out of the box.
        """
        be = GroqBackend(api_key="gsk-test")
        assert be.model == "meta-llama/llama-4-scout-17b-16e-instruct"
        assert be.api_key == "gsk-test"
        assert be.api_base_url == "https://api.groq.com/openai/v1"

    def test_verify_custom_model_is_stored(self):
        """Validate that an explicit model name is preserved on the backend instance.

        The test exercises construction with ``model="llama-4-scout"`` and asserts
        the attribute matches because model selection must be honored so that
        callers can target specific models without post-construction mutation.
        """
        be = GroqBackend(api_key="gsk-test", model="llama-4-scout")
        assert be.model == "llama-4-scout"

    def test_verify_gpt_oss_model_variant(self):
        """Validate that the GPT-OSS 120B model is accepted and stored correctly.

        The test exercises construction with ``model="gpt-oss-120b"`` and asserts
        the attribute matches because GPT-OSS is a distinct open-source model
        hosted on Groq with different performance characteristics than Llama.
        """
        be = GroqBackend(api_key="gsk-test", model="gpt-oss-120b")
        assert be.model == "gpt-oss-120b"

    def test_verify_custom_base_url_overrides_default(self):
        """Validate that a custom base_url replaces the default Groq endpoint.

        The test exercises construction with a custom ``base_url`` and asserts
        the attribute holds the provided URL because proxy deployments and
        self-hosted OpenAI-compatible servers require alternate origins.
        """
        be = GroqBackend(
            api_key="gsk-test",
            base_url="https://custom-groq.example.com/openai/v1",
        )
        assert be.api_base_url == "https://custom-groq.example.com/openai/v1"

    def test_verify_empty_api_key_is_allowed(self):
        """Validate that construction without an API key does not raise.

        The test exercises parameterless construction and asserts ``api_key`` is
        empty string and the model defaults correctly because some deployments
        inject credentials via environment variables or a secrets manager after
        backend construction.
        """
        be = GroqBackend()
        assert be.api_key == ""
        assert be.model == "meta-llama/llama-4-scout-17b-16e-instruct"

    def test_verify_http_timeout_is_forwarded(self):
        """Validate that the ``http_timeout`` kwarg is propagated to the parent SSE backend.

        The test exercises construction with ``http_timeout=30.0`` and asserts
        the attribute is 30.0 because HTTP client timeout configuration must
        be transparently forwarded so callers can tune request deadlines.
        """
        be = GroqBackend(api_key="gsk-test", http_timeout=30.0)
        assert be.http_timeout == 30.0


# ===========================================================================
# Capability checks
# ===========================================================================


class TestGroqBackendCapabilities:
    """Validates capability flags for Groq-hosted models.

    This test class exercises ``supports_tool_calling``, ``supports_thinking``,
    and ``supports_prompt_caching`` across 4 scenarios. The design ensures
    tool calling returns ``True`` (all Groq models expose OpenAI-compatible
    function calling), while thinking and caching return booleans inherited
    from the parent class because Groq does not override those flags.
    """

    def test_verify_supports_tool_calling_default(self):
        """Validate that the default Groq model supports OpenAI-compatible tool calling.

        The test exercises the default backend and asserts ``supports_tool_calling()``
        returns ``True`` because Groq's API is OpenAI-compatible and exposes
        the function-calling endpoint required by the agent tool-use loop.
        """
        be = GroqBackend(api_key="gsk-test")
        assert be.supports_tool_calling() is True

    def test_verify_supports_tool_calling_across_all_models(self):
        """Validate that tool calling is enabled for every Groq-hosted model tested.

        The test iterates over three model names and asserts ``True`` for each
        because the agent framework requires uniform tool-call support across
        all available Groq backends.
        """
        models = ["llama-3.3-70b-versatile", "llama-4-scout", "gpt-oss-120b"]
        for m in models:
            be = GroqBackend(api_key="gsk-test", model=m)
            assert be.supports_tool_calling() is True, f"model={m}"

    def test_verify_supports_thinking_returns_bool(self):
        """Validate that ``supports_thinking`` returns a boolean (inherited default).

        The test exercises the default backend and asserts the result is an
        ``bool`` because Groq does not override the thinking flag and the
        agent loop must receive a predictable boolean type.
        """
        be = GroqBackend(api_key="gsk-test")
        result = be.supports_thinking()
        assert isinstance(result, bool)

    def test_verify_supports_prompt_caching_returns_bool(self):
        """Validate that ``supports_prompt_caching`` returns a boolean (inherited default).

        The test exercises the default backend and asserts the result is an
        ``bool`` because Groq inherits the parent's caching flag and the agent
        loop must receive a predictable boolean type.
        """
        be = GroqBackend(api_key="gsk-test")
        result = be.supports_prompt_caching()
        assert isinstance(result, bool)


# ===========================================================================
# Context window size
# ===========================================================================


class TestGroqBackendContextWindow:
    """Validates context-window sizes for Groq-hosted models.

    This test class exercises ``context_window_size()`` across 4 scenarios
    covering the default model, Llama 4 Scout, GPT-OSS 120B, and a positivity
    check. The design ensures the agent loop can truncate conversations to
    the correct 131,072-token limit so that API requests never exceed Groq's
    context cap regardless of which model is selected.
    """

    def test_verify_context_window_size_default(self):
        """Validate that the default Groq model reports a 131,072-token context window.

        The test exercises the default backend and asserts 131072 because all
        Groq-hosted models currently share the same 128K context budget.
        """
        be = GroqBackend(api_key="gsk-test")
        assert be.context_window_size() == 131072

    def test_verify_context_window_size_scout(self):
        """Validate that Llama 4 Scout reports a 131,072-token context window.

        The test exercises the scout model and asserts 131072 because the
        Llama 4 Scout tier matches the standard Groq context limit.
        """
        be = GroqBackend(api_key="gsk-test", model="llama-4-scout")
        assert be.context_window_size() == 131072

    def test_verify_context_window_size_gpt_oss(self):
        """Validate that GPT-OSS 120B reports a 131,072-token context window.

        The test exercises the GPT-OSS model and asserts 131072 because the
        open-source variant hosted on Groq shares the same context budget.
        """
        be = GroqBackend(api_key="gsk-test", model="gpt-oss-120b")
        assert be.context_window_size() == 131072

    def test_verify_context_window_is_positive_integer(self):
        """Validate that the default model returns a positive integer context size.

        The test exercises the default backend and asserts the result is an
        ``int`` greater than zero because a non-positive context size would
        cause the agent loop to discard all conversation history immediately.
        """
        be = GroqBackend(api_key="gsk-test")
        assert be.context_window_size() > 0
        assert isinstance(be.context_window_size(), int)


# ===========================================================================
# Token counting and model attribute
# ===========================================================================


class TestGroqBackendTokens:
    """Validates token-counting resilience and model attribute access.

    This test class exercises ``count_tokens()`` across 4 scenarios covering
    normal text, empty strings, long text, and direct model attribute access.
    The design ensures token counting never raises on valid input and that
    the ``model`` attribute faithfully reflects the constructor argument.
    """

    def test_verify_count_tokens_returns_int(self):
        """Validate that ``count_tokens`` returns an integer for normal text.

        The test exercises a short string and asserts the result is an ``int``
        because the token counter must always produce an integer return type
        even when the underlying tokenizer is unavailable.
        """
        be = GroqBackend(api_key="gsk-test")
        result = be.count_tokens("hello world")
        assert isinstance(result, int)

    def test_verify_count_tokens_empty_string(self):
        """Validate that ``count_tokens`` does not crash on an empty string.

        The test exercises ``""`` and asserts the result is an ``int`` because
        the tokenizer must handle the zero-length edge case without raising.
        """
        be = GroqBackend(api_key="gsk-test")
        result = be.count_tokens("")
        assert isinstance(result, int)

    def test_verify_count_tokens_long_text(self):
        """Validate that ``count_tokens`` does not crash on long repeated text.

        The test exercises a 200-repetition string and asserts the result is
        an ``int`` because token counting must scale gracefully to large inputs
        without throwing, even if the count is approximate.
        """
        be = GroqBackend(api_key="gsk-test")
        result = be.count_tokens("Groq ultra-low-latency inference. " * 200)
        assert isinstance(result, int)

    def test_verify_model_attribute_matches_constructor(self):
        """Validate that the ``model`` attribute stores the constructor argument exactly.

        The test exercises construction with ``model="llama-4-scout"`` and asserts
        the attribute is that exact string and is an instance of ``str`` because
        the model name is used in every API request and must not be mutated.
        """
        be = GroqBackend(api_key="gsk-test", model="llama-4-scout")
        assert be.model == "llama-4-scout"
        assert isinstance(be.model, str)


# ===========================================================================
# Request data building
# ===========================================================================


class TestGroqBackendRequestBuilding:
    """Validates ``_build_request_data`` request-body assembly.

    This test class exercises request-body construction across 3 scenarios
    covering max_tokens propagation, model injection, and streaming toggle.
    The design ensures the request body conforms to the OpenAI chat-completions
    schema (inherited from ``OpenAISSEBackend``) so that downstream API calls
    succeed without schema-validation errors on Groq's endpoint.
    """

    def test_verify_max_tokens_is_propagated(self):
        """Validate that ``max_tokens`` is included in the request body when provided.

        The test exercises ``_build_request_data`` with ``max_tokens=1024`` and
        asserts the key exists and equals 1024 because the agent loop must be
        able to cap model output length per-request.
        """
        be = GroqBackend(api_key="gsk-test")
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=1024,
        )
        assert data["max_tokens"] == 1024

    def test_verify_model_is_included_in_request_body(self):
        """Validate that the request body carries the configured model name.

        The test exercises ``_build_request_data`` with a custom model and
        asserts ``data["model"]`` matches because the Groq API endpoint must
        know which model to route the request to.
        """
        be = GroqBackend(api_key="gsk-test", model="llama-4-scout")
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert data["model"] == "llama-4-scout"

    def test_verify_streaming_flag_is_respected(self):
        """Validate that ``stream`` is set correctly in both true and false modes.

        The test exercises ``_build_request_data`` with ``stream=True`` and
        ``stream=False`` and asserts the flag is reflected in the body for
        each case because the agent loop consumes the API as an SSE stream
        by default but needs non-streaming mode for synchronous fallback paths.
        """
        be = GroqBackend(api_key="gsk-test")
        data_stream = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )
        assert data_stream["stream"] is True

        data_nostream = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
            stream=False,
        )
        assert data_nostream["stream"] is False


# ===========================================================================
# Lifecycle
# ===========================================================================


class TestGroqBackendLifecycle:
    """Validates backend resource cleanup and lifecycle safety.

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
        be = GroqBackend(api_key="gsk-test")
        asyncio.run(be.aclose())

    def test_verify_aclose_is_idempotent(self):
        """Validate that calling ``aclose()`` twice does not raise.

        The test exercises a double-close sequence inside an async helper and
        asserts no exception is raised because shutdown handlers may call
        cleanup multiple times and idempotency prevents error propagation.
        """

        async def _double():
            """Close the backend twice in sequence."""
            be = GroqBackend(api_key="gsk-test")
            await be.aclose()
            await be.aclose()

        asyncio.run(_double())
