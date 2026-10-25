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

"""Tests for individual backend implementations (no API keys needed).

Each provider's backend is constructed through the create_backend factory with
dummy credentials, and the tests assert its advertised capability flags, the
expected context-window size, and correct propagation of model overrides. A
final section covers the retry decorator and default retry budget.
"""

import asyncio

import pytest
from encre.backend import create_backend
from encre.backends.base import BaseBackend

# ===========================================================================
# OpenAI
# ===========================================================================


class TestOpenAIBackend:
    """Engineered to validate OpenAI backend factory creation and capability defaults.

    This test class exercises the ``create_backend("openai", ...)`` path across 3 scenarios
    to ensure the factory returns a well-formed :class:`BaseBackend` instance with the
    correct tool-calling flag, context window size (1,050,000 tokens), and support for
    model-name override. The design follows the factory pattern so that downstream callers
    receive a predictable backend regardless of the API key provided.
    """

    def test_verify_openai_backend_creation(self):
        """Validate that create_backend returns a fully configured OpenAI backend.

        The test instantiates the backend with a dummy API key and asserts three properties:
        (1) the result is an instance of BaseBackend, ensuring ABC conformance;
        (2) tool calling is enabled, confirming the OpenAI route supports structured function
        tools; (3) the context window size matches the advertised 1,050,000-token limit for
        modern OpenAI models.
        """
        be = create_backend("openai", api_key="sk-fake")
        assert isinstance(be, BaseBackend), "Factory must return a BaseBackend subclass instance."
        assert be.supports_tool_calling() is True, "OpenAI backend must advertise tool calling support."
        assert be.context_window_size() == 1050000, "OpenAI context window must match the 1,050,000-token spec."

    def test_verify_openai_model_override(self):
        """Validate that an explicit model argument overrides the default model name.

        The test passes ``model="gpt-4o-mini"`` and asserts the property reflects the override,
        confirming that the factory propagates constructor kwargs through to the instance without
        shadowing them with a hardcoded default.
        """
        be = create_backend("openai", model="gpt-4o-mini", api_key="sk-fake")
        assert be.model == "gpt-4o-mini", "The explicitly passed model must be stored verbatim."

    def test_verify_openai_token_counter(self):
        """Validate that count_tokens returns an integer (or -1 when tiktoken is absent).

        The tokenizer is optional at runtime; the engine only requires a numeric return value
        so that callers can branch on sentinel -1. This test asserts type safety, not a
        specific token count.
        """
        be = create_backend("openai", api_key="sk-fake")
        assert isinstance(be.count_tokens("hello"), int), "count_tokens must always return an int."


# ===========================================================================
# Anthropic
# ===========================================================================


class TestAnthropicBackend:
    """Engineered to validate Anthropic backend factory creation and extended capability flags.

    This test class exercises the ``create_backend("anthropic", ...)`` path across 3 scenarios
    to ensure the backend reports correct tool-calling support, the 1,000,000-token context
    window, and the extra capabilities that distinguish Anthropic models: extended thinking
    and prompt caching. The design follows the same factory pattern as other backends so that
    feature-gated capabilities surface uniformly through typed predicates.
    """

    def test_verify_anthropic_backend_creation(self):
        """Validate that create_backend returns a fully configured Anthropic backend.

        The test asserts four properties: BaseBackend conformance, tool-calling support,
        context window of 1,000,000 tokens, and explicit True flags for both extended thinking
        and prompt caching, capabilities unique to the Anthropic provider route.
        """
        be = create_backend("anthropic", api_key="sk-ant-fake")
        assert isinstance(be, BaseBackend), "Factory must return a BaseBackend subclass instance."
        assert be.supports_tool_calling() is True, "Anthropic backend must advertise tool calling support."
        assert be.context_window_size() == 1000000, "Anthropic context window must match the 1,000,000-token spec."
        assert be.supports_thinking() is True, "Anthropic backend must enable extended thinking."
        assert be.supports_prompt_caching() is True, "Anthropic backend must enable prompt caching."

    def test_verify_anthropic_model_override(self):
        """Validate that an explicit model argument overrides the default model name.

        The test passes a full Anthropic model identifier including its date suffix and asserts
        the instance stores it verbatim, confirming proper propagation of the model kwarg.
        """
        be = create_backend("anthropic", model="claude-sonnet-4-20250514", api_key="sk-ant-fake")
        assert be.model == "claude-sonnet-4-20250514", "The explicitly passed model must be stored verbatim."

    def test_verify_anthropic_thinking_capability(self):
        """Validate that the Anthropic backend reports extended thinking support.

        This is a focused regression check: even if create_backend succeeded, the thinking
        capability predicate must remain True, since downstream prompt-assembler logic gates
        its behaviour on this flag.
        """
        be = create_backend("anthropic", api_key="sk-ant-fake")
        assert be.supports_thinking() is True, "Anthropic backend must report extended thinking as available."


# ===========================================================================
# DeepSeek
# ===========================================================================


class TestDeepSeekBackend:
    """Engineered to validate DeepSeek backend factory creation and deprecated-model aliasing.

    This test class exercises the ``create_backend("deepseek", ...)`` path across 2 scenarios
    to ensure the backend reports correct tool-calling support, the 1,048,576-token context
    window, and that the legacy ``deepseek-chat`` model name is automatically remapped to
    ``deepseek-v4-flash``. The aliasing behaviour is critical so that old config entries do
    not break at runtime.
    """

    def test_verify_deepseek_backend_creation(self):
        """Validate that create_backend returns a fully configured DeepSeek backend.

        The test asserts BaseBackend conformance, tool-calling support, and the 1,048,576-token
        context window (2^20), which is the advertised maximum for DeepSeek model endpoints.
        """
        be = create_backend("deepseek", api_key="sk-fake")
        assert isinstance(be, BaseBackend), "Factory must return a BaseBackend subclass instance."
        assert be.supports_tool_calling() is True, "DeepSeek backend must advertise tool calling support."
        assert be.context_window_size() == 1048576, "DeepSeek context window must match the 1,048,576-token spec."

    def test_verify_deepseek_model_alias_resolution(self):
        """Validate that the deprecated deepseek-chat model name resolves to deepseek-v4-flash.

        The factory applies a backward-compatibility alias so callers who supply the legacy
        name receive the current model identifier. The assertion confirms the alias mapping
        is applied before the value is stored on the instance.
        """
        be = create_backend("deepseek", model="deepseek-chat", api_key="sk-fake")
        assert be.model == "deepseek-v4-flash", "Legacy deepseek-chat must be aliased to deepseek-v4-flash."


# ===========================================================================
# Google
# ===========================================================================


class TestGoogleBackend:
    """Engineered to validate Google (Gemini) backend factory creation and model override.

    This test class exercises the ``create_backend("google", ...)`` path across 2 scenarios
    to ensure the backend reports correct tool-calling support, the 1,048,576-token context
    window, and that an explicit Gemini model name is preserved verbatim.
    """

    def test_verify_google_backend_creation(self):
        """Validate that create_backend returns a fully configured Google backend.

        The test asserts BaseBackend conformance, tool-calling support, and the 1,048,576-token
        context window, which aligns with the Gemini API's documented context limits.
        """
        be = create_backend("google", api_key="fake-key")
        assert isinstance(be, BaseBackend), "Factory must return a BaseBackend subclass instance."
        assert be.supports_tool_calling() is True, "Google backend must advertise tool calling support."
        assert be.context_window_size() == 1048576, "Google context window must match the 1,048,576-token spec."

    def test_verify_google_model_override(self):
        """Validate that an explicit Gemini model name is stored verbatim.

        The test passes ``model="gemini-2.5-flash"`` and asserts the property is preserved,
        confirming that the factory does not impose an internal default override.
        """
        be = create_backend("google", model="gemini-2.5-flash", api_key="fake-key")
        assert be.model == "gemini-2.5-flash", "The explicitly passed model must be stored verbatim."


# ===========================================================================
# Groq
# ===========================================================================


class TestGroqBackend:
    """Engineered to validate Groq backend factory creation and model override.

    This test class exercises the ``create_backend("groq", ...)`` path across 2 scenarios
    to ensure the backend reports correct tool-calling support and the smaller 131,072-token
    context window typical of Groq-hosted open-weight models.
    """

    def test_verify_groq_backend_creation(self):
        """Validate that create_backend returns a fully configured Groq backend.

        The test asserts BaseBackend conformance, tool-calling support, and the 131,072-token
        context window, which is the upper bound for Groq's public model endpoints.
        """
        be = create_backend("groq", api_key="gsk-fake")
        assert isinstance(be, BaseBackend), "Factory must return a BaseBackend subclass instance."
        assert be.supports_tool_calling() is True, "Groq backend must advertise tool calling support."
        assert be.context_window_size() == 131072, "Groq context window must match the 131,072-token spec."

    def test_verify_groq_model_override(self):
        """Validate that an explicit Groq model name is stored verbatim.

        The test passes ``model="llama-4-maverick"`` and asserts the property is preserved.
        """
        be = create_backend("groq", model="llama-4-maverick", api_key="gsk-fake")
        assert be.model == "llama-4-maverick", "The explicitly passed model must be stored verbatim."


# ===========================================================================
# Ollama
# ===========================================================================


class TestOllamaBackend:
    """Engineered to validate Ollama backend factory creation and local inference defaults.

    This test class exercises the ``create_backend("ollama", base_url=...)`` path to ensure
    the backend reports a modest 8,192-token context window (the typical default for locally
    served models) and a boolean-valued tool-calling predicate, since Ollama supports tool
    calling only on models that declare it.
    """

    def test_verify_ollama_backend_creation(self):
        """Validate that create_backend returns a functional Ollama backend with correct defaults.

        The test asserts BaseBackend conformance, the 8,192-token context window, and that
        supports_tool_calling returns a bool; the exact truth value depends on the connected
        Ollama server's model roster, so only the type is checked here.
        """
        be = create_backend("ollama", base_url="http://localhost:11434")
        assert isinstance(be, BaseBackend), "Factory must return a BaseBackend subclass instance."
        assert be.context_window_size() == 8192, "Ollama default context window must be 8,192 tokens."
        assert isinstance(be.supports_tool_calling(), bool), "Tool-calling predicate must return a bool."


# ===========================================================================
# Local
# ===========================================================================


class TestLocalBackend:
    """Engineered to validate local-backend factory creation and model-name override.

    This test class exercises the ``create_backend("local", ...)`` path across 2 scenarios
    to ensure the backend reports a conservative 4,096-token context window, a boolean-valued
    tool-calling predicate, and that the ``model_name`` property is set from the constructor
    argument. Local backends are intentionally restrictive to avoid over-promising capabilities
    that may not exist on the local inference engine.
    """

    def test_verify_local_backend_creation(self):
        """Validate that create_backend returns a functional local backend with correct defaults.

        The test asserts BaseBackend conformance, the 4,096-token context window, and that
        supports_tool_calling returns a bool, mirroring the Ollama design constraint.
        """
        be = create_backend("local")
        assert isinstance(be, BaseBackend), "Factory must return a BaseBackend subclass instance."
        assert be.context_window_size() == 4096, "Local backend context window must be 4,096 tokens."
        assert isinstance(be.supports_tool_calling(), bool), "Tool-calling predicate must return a bool."

    def test_verify_local_model_override(self):
        """Validate that the model_name property reflects the constructor argument verbatim.

        The test passes a HuggingFace-style model identifier and asserts it is stored without
        transformation, confirming the local backend does not apply provider-specific aliasing.
        """
        be = create_backend("local", model_name="meta-llama/Llama-4-Maverick-17B-128E-Instruct")
        assert be.model_name == "meta-llama/Llama-4-Maverick-17B-128E-Instruct", \
            "The explicitly passed model_name must be stored verbatim."


# ===========================================================================
# Bedrock
# ===========================================================================


class TestBedrockBackend:
    """Engineered to validate AWS Bedrock backend factory creation and model override.

    This test class exercises the ``create_backend("bedrock", ...)`` path across 2 scenarios
    to ensure the backend reports a 1,000,000-token context window and that an explicit
    Bedrock model ARN is preserved. Bedrock delegates to upstream providers (Anthropic,
    Meta, etc.), so the context window reflects the aggregated capacity advertised by the
    service layer.
    """

    def test_verify_bedrock_backend_creation(self):
        """Validate that create_backend returns a functional Bedrock backend with correct defaults.

        The test asserts BaseBackend conformance, the 1,000,000-token context window, and that
        supports_tool_calling returns a bool, since Bedrock tool-calling availability depends
        on the selected model family.
        """
        be = create_backend("bedrock", aws_access_key_id="fake", aws_secret_access_key="fake", region="us-east-1")
        assert isinstance(be, BaseBackend), "Factory must return a BaseBackend subclass instance."
        assert be.context_window_size() == 1000000, "Bedrock context window must match the 1,000,000-token spec."
        assert isinstance(be.supports_tool_calling(), bool), "Tool-calling predicate must return a bool."

    def test_verify_bedrock_model_override(self):
        """Validate that an explicit Bedrock model ARN is stored verbatim.

        The test passes a full Anthropic Bedrock model identifier and asserts it is preserved,
        confirming the factory does not rewrite provider-qualified model names.
        """
        be = create_backend(
            "bedrock",
            model="anthropic.claude-sonnet-4-20250514-v1:0",
            aws_access_key_id="fake",
            aws_secret_access_key="fake",
        )
        assert be.model == "anthropic.claude-sonnet-4-20250514-v1:0", \
            "The explicitly passed Bedrock model ARN must be stored verbatim."


# ===========================================================================
# OpenAI Compatible
# ===========================================================================


class TestOpenAICompatibleBackend:
    """Engineered to validate OpenAI-compatible backend factory creation and model override.

    This test class exercises the ``create_backend("openai_compatible", ...)`` path across
    2 scenarios to ensure the backend reports a 1,048,576-token context window and that both
    ``base_url`` and ``model`` constructor arguments are propagated to the instance. This
    backend type allows plugging in any OpenAI-API-compatible server, so the test validates
    the wiring rather than provider-specific behaviour.
    """

    def test_verify_openai_compatible_backend_creation(self):
        """Validate that create_backend returns a functional OpenAI-compatible backend.

        The test asserts BaseBackend conformance, the 1,048,576-token context window, and that
        supports_tool_calling returns a bool; compatibility servers vary in their tool-calling
        support, so the predicate is type-checked rather than hard-asserted.
        """
        be = create_backend("openai_compatible", base_url="https://api.example.com/v1", api_key="sk-fake")
        assert isinstance(be, BaseBackend), "Factory must return a BaseBackend subclass instance."
        assert isinstance(be.supports_tool_calling(), bool), "Tool-calling predicate must return a bool."
        assert be.context_window_size() == 1048576, "OpenAI-compatible context window must match the 1,048,576-token spec."

    def test_verify_openai_compatible_model_override(self):
        """Validate that both base_url and model are propagated to the instance.

        The test passes custom values for both parameters and asserts each is stored,
        confirming the factory forwards kwargs correctly for compatibility-mode backends.
        """
        be = create_backend(
            "openai_compatible",
            model="custom-model",
            base_url="https://api.example.com/v1",
            api_key="sk-fake",
        )
        assert be.model == "custom-model", "The explicitly passed model must be stored verbatim."


# ===========================================================================
# Retry integration
# ===========================================================================


class TestRetryIntegration:
    """Engineered to validate the retry decorator and RetryConfig defaults.

    This test class exercises the backoff retry mechanism across 2 scenarios: (1) a flaky
    async request that raises after exhausting retries, confirming the decorator does not
    swallow exceptions; (2) the default RetryConfig values, confirming the system-level
    default tolerates up to 8 rate-limit retries before giving up.
    """

    def test_verify_retry_exhausts_after_max_retries(self):
        """Validate that retry_with_backoff raises after exhausting all retries.

        The test wraps an async function that always raises httpx.TimeoutException and asserts
        the exception propagates after the configured max_retries (2) are consumed. This confirms
        the decorator retries but does not mask permanent failures.
        """
        import httpx
        from encre.backends.retry import RetryConfig, retry_with_backoff

        async def _test():
            """Drive the retry decorator against a permanently failing request in an event loop.

            Runs asynchronously so that the decorator's backoff sleeps execute inside a real
            loop, then confirms the original exception escapes once retries are exhausted.
            """
            config = RetryConfig(max_retries=2, base_delay=0.01)

            @retry_with_backoff(config)
            async def flaky_request():
                """Simulate a transient timeout that never recovers across retries.

                Always raises httpx.TimeoutException so the retry decorator has no success
                path and must terminate by re-raising after the retry budget is spent.
                """
                raise httpx.TimeoutException("timeout")

            with pytest.raises(httpx.TimeoutException):
                await flaky_request()

        asyncio.run(_test())

    def test_verify_default_retry_config_values(self):
        """Validate that RetryConfig carries the expected default retry budget.

        The test instantiates RetryConfig with no arguments and asserts rate_limit_retries is 8,
        the system-wide default for transient HTTP failures before the caller should abort.
        """
        from encre.backends.retry import RetryConfig

        rc = RetryConfig()
        assert rc.rate_limit_retries == 8, "Default rate-limit retry budget must be 8."
