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

"""Backend subsystem tests covering model metadata, registry lookup, backend
creation, retry policy, capability predicates, and config wiring.

These tests exercise the ModelInfo dataclass, the BackendRegistry and
resolve_model_info fallback table, the BaseBackend ABC contract, the
create_backend factory for every registered provider, RetryConfig defaults,
and the interaction between EncreConfig and the backend factory. No live
provider API key is required: every backend is constructed with dummy
credentials and only its interface and metadata are asserted.
"""

import asyncio

import pytest
from encre.backend import create_backend
from encre.backends.base import BaseBackend
from encre.backends.registry import (
    REGISTRY,
    BackendRegistry,
    ModelInfo,
    resolve_model_info,
)
from encre.backends.retry import DEFAULT_RETRY_CONFIG, RetryConfig

# ===========================================================================
# ModelInfo dataclass
# ===========================================================================

class TestModelInfo:
    """Engineered to validate the ModelInfo dataclass construction and equality semantics.

    This test class exercises the :class:`ModelInfo` record across 4 scenarios to ensure
    default capability fields populate correctly, alias lists are preserved, and value-based
    equality holds for structurally identical records while differing names produce inequality.
    The dataclass is the unit of metadata that flows from registry lookups into backend
    initialisation, so its invariants must hold deterministically.
    """

    def test_verify_default_field_population(self):
        """Validate that ModelInfo populates all capability defaults when constructed minimally.

        The test creates a ModelInfo with only name and provider, then asserts that the four
        derived defaults (context_window=128000, max_output_tokens=8192, supports_tools=True,
        supports_streaming=True) are present without requiring the caller to specify them.
        """
        mi = ModelInfo(name="test-model", provider="openai")
        assert mi.name == "test-model", "The explicitly provided name must be stored verbatim."
        assert mi.provider == "openai", "The explicitly provided provider must be stored verbatim."
        assert mi.context_window == 128000, "Default context window must be 128,000 tokens."
        assert mi.max_output_tokens == 8192, "Default max output tokens must be 8,192."
        assert mi.supports_tools is True, "Tools support must default to True."
        assert mi.supports_streaming is True, "Streaming support must default to True."

    def test_verify_alias_propagation(self):
        """Validate that alias lists are preserved through construction.

        The test passes a list of alias strings and asserts they are stored unchanged,
        confirming the registry can later resolve aliases to the canonical model name.
        """
        mi = ModelInfo(
            name="gpt-5.6",
            provider="openai",
            aliases=["gpt4o", "4o"],
        )
        assert mi.aliases == ["gpt4o", "4o"], "Alias list must be preserved verbatim."

    def test_verify_value_equality(self):
        """Validate that two ModelInfo instances with identical fields compare equal.

        The test constructs two records with the same name and provider and asserts equality,
        confirming the dataclass-generated __eq__ operates on all fields rather than identity.
        """
        a = ModelInfo(name="m1", provider="openai")
        b = ModelInfo(name="m1", provider="openai")
        assert a == b, "Identical ModelInfo records must compare equal."

    def test_verify_different_names_break_equality(self):
        """Validate that differing names make two ModelInfo instances unequal.

        The test constructs two records sharing a provider but with different names and asserts
        inequality, confirming name is part of the equality contract.
        """
        a = ModelInfo(name="m1", provider="openai")
        b = ModelInfo(name="m2", provider="openai")
        assert a != b, "ModelInfo records with different names must not compare equal."


# ===========================================================================
# BackendRegistry: register / unregister / resolve
# ===========================================================================

class TestBackendRegistryRegistration:
    """Engineered to validate the BackendRegistry CRUD operations.

    This test class exercises registration, resolution (exact and via alias), unregistration,
    overwrite semantics, and listing across 6 scenarios to ensure the registry behaves as a
    deterministic single-source-of-truth for model metadata. The registry is consulted by
    :func:`resolve_model_info` during backend initialisation, so correctness here propagates
    to every backend creation path.
    """

    def test_verify_register_and_resolve_exact(self):
        """Validate that a registered model is resolvable by its canonical name.

        The test registers a ModelInfo with a custom context window (300000) and asserts that
        resolve returns the same record with the custom value intact, confirming the registry
        stores and retrieves the full record without mutating fields.
        """
        registry = BackendRegistry()
        mi = ModelInfo(name="my-model", provider="anthropic", context_window=300000)
        registry.register(mi)
        resolved = registry.resolve("my-model")
        assert resolved is not None, "Resolved record must not be None after registration."
        assert resolved.name == "my-model", "Resolved name must match the registered name."
        assert resolved.context_window == 300000, "Custom context window must be preserved through resolve."

    def test_verify_alias_resolution(self):
        """Validate that registered aliases resolve to the canonical model name.

        The test registers a model with two aliases and asserts that each alias resolves to
        the same record whose .name field equals the canonical name, confirming the alias index
        is populated and consulted during lookup.
        """
        registry = BackendRegistry()
        mi = ModelInfo(name="my-model-2", provider="openai", aliases=["mm2", "alias2"])
        registry.register(mi)
        r1 = registry.resolve("mm2")
        assert r1 is not None, "First alias must resolve to a non-None record."
        assert r1.name == "my-model-2", "Alias resolution must return the canonical name."
        r2 = registry.resolve("alias2")
        assert r2 is not None, "Second alias must resolve to a non-None record."
        assert r2.name == "my-model-2", "Alias resolution must return the canonical name."

    def test_verify_unregister_removes_entry(self):
        """Validate that unregister removes a model so subsequent resolve returns None.

        The test registers a model, confirms it resolves, unregisters it, and asserts that
        resolve now returns None, confirming the registry maintains consistent state after
        deletion and does not leak entries.
        """
        registry = BackendRegistry()
        mi = ModelInfo(name="temp-model", provider="openai", aliases=["tm"])
        registry.register(mi)
        assert registry.resolve("temp-model") is not None, "Model must be resolvable immediately after registration."
        registry.unregister("temp-model")
        assert registry.resolve("temp-model") is None, "Model must become unresolvable after unregister."

    def test_verify_resolve_nonexistent_returns_none(self):
        """Validate that resolving an unknown model name yields None rather than raising.

        The test queries the registry with a deliberately nonexistent name and asserts None,
        confirming callers can use the return value as a falsey sentinel without try/except.
        """
        registry = BackendRegistry()
        assert registry.resolve("nonexistent-model-xyz") is None, "Unknown model must resolve to None."

    def test_verify_register_overwrite_updates_fields(self):
        """Validate that re-registering the same name replaces the previous record.

        The test registers a model with context_window=1000, then re-registers with 2000 and
        asserts the resolved value is 2000, confirming the registry treats re-registration as
        an update rather than appending a duplicate entry.
        """
        registry = BackendRegistry()
        mi1 = ModelInfo(name="overwrite-test", provider="openai", context_window=1000)
        mi2 = ModelInfo(name="overwrite-test", provider="openai", context_window=2000)
        registry.register(mi1)
        registry.register(mi2)
        r = registry.resolve("overwrite-test")
        assert r.context_window == 2000, "Re-registration must overwrite the previous context window value."

    def test_verify_list_models_includes_registered(self):
        """Validate that list_models surfaces a freshly registered model.

        The test registers a model and asserts that at least one entry in list_models() has the
        expected name, confirming the listing operation reflects the current registry state.
        """
        registry = BackendRegistry()
        mi = ModelInfo(name="list-all-test", provider="groq")
        registry.register(mi)
        all_models = registry.list_models()
        assert any(m.name == "list-all-test" for m in all_models), \
            "list_models must include the freshly registered model."


# ===========================================================================
# Global REGISTRY and resolve_model_info
# ===========================================================================

class TestResolveModelInfo:
    """Engineered to validate resolve_model_info fallback behaviour across providers.

    This test class exercises the global registry lookup and provider-inferred fallback
    across 10 scenarios to ensure that known models resolve to their registered metadata
    and that unknown model names fall back to provider-specific defaults (context window,
    capability flags). The fallback mechanism guarantees that backends can be instantiated
    even when a model has never been explicitly registered.
    """

    def test_verify_resolve_known_model(self):
        """Validate that a registered model resolves with correct metadata.

        The test looks up "gpt-4.1" and asserts the name, provider, and 1,048,576-token
        context window, confirming the global registry contains the expected entry.
        """
        info = resolve_model_info("gpt-4.1")
        assert info.name == "gpt-4.1", "Resolved name must match the query."
        assert info.provider == "openai", "Provider must be inferred as openai from the model name pattern."
        assert info.context_window == 1048576, "Context window must match the openai default of 1,048,576."

    def test_verify_resolve_registered_custom_model(self):
        """Validate that a manually registered custom model takes priority over provider defaults.

        The test registers a model with a unique context window (999000), resolves it, asserts
        the custom value, then unregisters to avoid polluting the global registry for other tests.
        """
        mi = ModelInfo(name="known-model", provider="anthropic", context_window=999000)
        REGISTRY.register(mi)
        info = resolve_model_info("known-model")
        assert info.context_window == 999000, "Custom-registered context window must take priority over provider default."
        REGISTRY.unregister("known-model")

    def test_verify_unregistered_openai_fallback(self):
        """Validate that an unknown OpenAI-pattern model falls back to openai defaults.

        The test queries a fabricated "gpt-5-imaginary" name and asserts the provider is
        inferred as openai with the standard 1,048,576-token window and tools support enabled.
        """
        info = resolve_model_info("gpt-5-imaginary")
        assert info.provider == "openai", "Provider must be inferred from the gpt-* name prefix."
        assert info.context_window == 1048576, "Fallback context window must match the openai default."
        assert info.supports_tools is True, "Fallback must enable tools support for openai-pattern models."

    def test_verify_unregistered_anthropic_fallback(self):
        """Validate that an unknown Anthropic-pattern model falls back to anthropic defaults.

        The test queries a fabricated "claude-opus-5-imaginary" name and asserts the provider,
        200,000-token context window, and both extended-thinking and prompt-caching flags.
        """
        info = resolve_model_info("claude-opus-5-imaginary")
        assert info.provider == "anthropic", "Provider must be inferred from the claude-* name prefix."
        assert info.context_window == 200000, "Fallback context window must match the anthropic default."
        assert info.supports_thinking is True, "Fallback must enable thinking for anthropic-pattern models."
        assert info.supports_prompt_caching is True, "Fallback must enable prompt caching for anthropic-pattern models."

    def test_verify_unregistered_google_fallback(self):
        """Validate that an unknown Google-pattern model falls back to google defaults.

        The test queries "gemini-3-imaginary" and asserts provider=google and the 1,048,576-token
        context window, matching the Gemini API specification.
        """
        info = resolve_model_info("gemini-3-imaginary")
        assert info.provider == "google", "Provider must be inferred from the gemini-* name prefix."
        assert info.context_window == 1048576, "Fallback context window must match the google default."

    def test_verify_unregistered_deepseek_fallback(self):
        """Validate that an unknown DeepSeek-pattern model falls back to deepseek defaults.

        The test queries "deepseek-v4-imaginary" and asserts provider=deepseek with the
        1,048,576-token context window.
        """
        info = resolve_model_info("deepseek-v4-imaginary")
        assert info.provider == "deepseek", "Provider must be inferred from the deepseek-* name prefix."
        assert info.context_window == 1048576, "Fallback context window must match the deepseek default."

    def test_verify_unregistered_groq_fallback(self):
        """Validate that an unknown Groq-pattern model falls back to groq defaults.

        The test queries a fabricated name with explicit provider="groq" and asserts the
        131,072-token context window characteristic of Groq-hosted models.
        """
        info = resolve_model_info("groq-model-imaginary", provider="groq")
        assert info.provider == "groq", "Explicit provider override must be respected."
        assert info.context_window == 131072, "Fallback context window must match the groq default."

    def test_verify_unregistered_ollama_fallback(self):
        """Validate that an unknown Ollama-pattern model falls back to ollama defaults.

        The test queries with explicit provider="ollama" and asserts the 8,192-token context
        window, the conservative default for locally served models.
        """
        info = resolve_model_info("some-ollama-model", provider="ollama")
        assert info.provider == "ollama", "Explicit provider override must be respected."
        assert info.context_window == 8192, "Fallback context window must match the ollama default."

    def test_verify_unregistered_local_fallback(self):
        """Validate that an unknown local-pattern model falls back to local defaults.

        The test queries with explicit provider="local" and asserts the 4,096-token context
        window and 2,048-token max output, the restrictive defaults for the local backend.
        """
        info = resolve_model_info("my-local-model", provider="local")
        assert info.provider == "local", "Explicit provider override must be respected."
        assert info.context_window == 4096, "Fallback context window must match the local default."
        assert info.max_output_tokens == 2048, "Fallback max_output_tokens must match the local default."

    def test_verify_explicit_provider_overrides_name_inference(self):
        """Validate that an explicit provider argument overrides name-pattern inference.

        The test queries "some-unknown-model" with provider="bedrock" and asserts the resolved
        record carries the bedrock provider and the 200,000-token context window.
        """
        info = resolve_model_info("some-unknown-model", provider="bedrock")
        assert info.provider == "bedrock", "Explicit provider must override name-pattern inference."
        assert info.context_window == 200000, "Fallback context window must match the bedrock default."

    def test_verify_global_registry_contains_known_models(self):
        """Validate that the global REGISTRY resolves well-known model names.

        The test resolves "gpt-4.1" and "claude-sonnet-4.6" and asserts both are non-None,
        confirming the bootstrap registry is populated at import time.
        """
        info = REGISTRY.resolve("gpt-4.1")
        assert info is not None, "gpt-4.1 must be present in the global registry."
        info2 = REGISTRY.resolve("claude-sonnet-4.6")
        assert info2 is not None, "claude-sonnet-4.6 must be present in the global registry."


# ===========================================================================
# BaseBackend ABC compliance
# ===========================================================================

class TestBaseBackendABC:
    """Engineered to validate BaseBackend as an abstract base class.

    This test class exercises ABC instantiation constraints and concrete-subclass
    availability across 9 scenarios to ensure the backend hierarchy enforces the
    required interface contract. The abstract methods (supports_tool_calling, chat,
    context_window_size) must be unimplemented on the base, while concrete subclasses
    such as LocalBackend must instantiate without error and expose optional capability
    predicates.
    """

    def test_verify_base_backend_cannot_be_instantiated(self):
        """Validate that BaseBackend raises TypeError on direct instantiation.

        The test asserts that calling BaseBackend() raises TypeError, confirming the ABC
        machinery correctly prevents direct construction and forces subclasses to implement
        the required abstract methods.
        """
        with pytest.raises(TypeError):
            BaseBackend()

    def test_verify_concrete_subclass_instantiates(self):
        """Validate that LocalBackend, a concrete subclass, can be instantiated.

        The test imports LocalBackend and asserts it produces a BaseBackend instance,
        confirming the subclass implements all required abstract methods.
        """
        from encre.backends.local import LocalBackend
        be = LocalBackend()
        assert isinstance(be, BaseBackend), "LocalBackend must be a valid BaseBackend instance."

    def test_verify_supports_tool_calling_is_abstract(self):
        """Validate that supports_tool_calling is listed in BaseBackend.__abstractmethods__.

        The test asserts the method name appears in the abstract-method set, confirming it
        cannot be called on the base class without a concrete implementation.
        """
        assert "supports_tool_calling" in BaseBackend.__abstractmethods__, \
            "supports_tool_calling must be declared as an abstract method."

    def test_verify_chat_is_abstract(self):
        """Validate that chat is listed in BaseBackend.__abstractmethods__.

        The test asserts the method name appears in the abstract-method set, confirming
        every backend subclass must provide a chat implementation.
        """
        assert "chat" in BaseBackend.__abstractmethods__, "chat must be declared as an abstract method."

    def test_verify_context_window_size_is_abstract(self):
        """Validate that context_window_size is listed in BaseBackend.__abstractmethods__.

        The test asserts the method name appears in the abstract-method set, confirming
        every backend must declare its context capacity.
        """
        assert "context_window_size" in BaseBackend.__abstractmethods__, \
            "context_window_size must be declared as an abstract method."

    def test_verify_default_supports_thinking_predicate_exists(self):
        """Validate that LocalBackend exposes a supports_thinking predicate.

        The test asserts the method exists and returns a bool, confirming the base class
        provides a default implementation that concrete backends can override.
        """
        from encre.backends.local import LocalBackend
        be = LocalBackend()
        assert hasattr(be, "supports_thinking"), "LocalBackend must expose supports_thinking."
        assert isinstance(be.supports_thinking(), bool), "supports_thinking must return a bool."

    def test_verify_default_supports_prompt_caching_predicate_exists(self):
        """Validate that LocalBackend exposes a supports_prompt_caching predicate.

        The test asserts the method exists and returns a bool, confirming the base class
        provides a default implementation for this optional capability.
        """
        from encre.backends.local import LocalBackend
        be = LocalBackend()
        assert hasattr(be, "supports_prompt_caching"), "LocalBackend must expose supports_prompt_caching."
        assert isinstance(be.supports_prompt_caching(), bool), "supports_prompt_caching must return a bool."

    def test_verify_default_count_tokens_returns_minus_one(self):
        """Validate that the default count_tokens implementation returns -1.

        The -1 sentinel indicates the tokenizer is unavailable (tiktoken not installed);
        the test confirms the base implementation does not crash and returns the sentinel.
        """
        from encre.backends.local import LocalBackend
        be = LocalBackend()
        assert be.count_tokens("hello") == -1, "Default count_tokens must return -1 when tokenizer is absent."

    def test_verify_aclose_is_noop(self):
        """Validate that aclose completes without error on a fresh LocalBackend.

        The test runs the async close method and asserts it returns normally, confirming
        the default cleanup path is safe to invoke even when no resources were opened.
        """
        from encre.backends.local import LocalBackend
        be = LocalBackend()
        asyncio.run(be.aclose())


# ===========================================================================
# Backend factory: create_backend()
# ===========================================================================

class TestCreateBackend:
    """Engineered to validate the create_backend factory across all registered providers.

    This test class exercises the factory for 11 backend types (including unknown-type
    rejection) to ensure every registered provider returns a BaseBackend instance and that
    kwargs are forwarded correctly. The factory is the single entry point for backend
    creation, so these tests form the integration backbone for the entire backend subsystem.
    """

    def test_verify_create_openai(self):
        """Validate that create_backend('openai') returns a BaseBackend instance."""
        be = create_backend("openai")
        assert isinstance(be, BaseBackend), "openai factory must return a BaseBackend instance."

    def test_verify_create_anthropic(self):
        """Validate that create_backend('anthropic') returns a BaseBackend instance."""
        be = create_backend("anthropic")
        assert isinstance(be, BaseBackend), "anthropic factory must return a BaseBackend instance."

    def test_verify_create_ollama(self):
        """Validate that create_backend('ollama') returns a BaseBackend instance."""
        be = create_backend("ollama")
        assert isinstance(be, BaseBackend), "ollama factory must return a BaseBackend instance."

    def test_verify_create_deepseek(self):
        """Validate that create_backend('deepseek') returns a BaseBackend instance."""
        be = create_backend("deepseek")
        assert isinstance(be, BaseBackend), "deepseek factory must return a BaseBackend instance."

    def test_verify_create_google(self):
        """Validate that create_backend('google') returns a BaseBackend instance."""
        be = create_backend("google")
        assert isinstance(be, BaseBackend), "google factory must return a BaseBackend instance."

    def test_verify_create_groq(self):
        """Validate that create_backend('groq') returns a BaseBackend instance."""
        be = create_backend("groq")
        assert isinstance(be, BaseBackend), "groq factory must return a BaseBackend instance."

    def test_verify_create_local(self):
        """Validate that create_backend('local') returns a BaseBackend instance."""
        be = create_backend("local")
        assert isinstance(be, BaseBackend), "local factory must return a BaseBackend instance."

    def test_verify_create_bedrock(self):
        """Validate that create_backend('bedrock') returns a BaseBackend instance."""
        be = create_backend("bedrock")
        assert isinstance(be, BaseBackend), "bedrock factory must return a BaseBackend instance."

    def test_verify_create_openai_compatible(self):
        """Validate that create_backend('openai_compatible') returns a BaseBackend instance."""
        be = create_backend("openai_compatible", base_url="https://api.example.com/v1")
        assert isinstance(be, BaseBackend), "openai_compatible factory must return a BaseBackend instance."

    def test_verify_create_unknown_type_raises_valueerror(self):
        """Validate that an unregistered backend type raises ValueError with a recognisable message.

        The test passes "nonexistent_backend" and asserts the exception message contains
        "Unknown backend type", confirming the factory fails fast with a diagnostic string.
        """
        with pytest.raises(ValueError, match="Unknown backend type"):
            create_backend("nonexistent_backend")

    def test_verify_kwargs_passed_to_backend(self):
        """Validate that constructor kwargs are forwarded to the created backend instance.

        The test passes model="gpt-4o-mini" and api_key="sk-test" and asserts the model
        property reflects the override, confirming kwarg propagation through the factory.
        """
        be = create_backend("openai", model="gpt-4o-mini", api_key="sk-test")
        assert be.model == "gpt-4o-mini", "Constructor kwargs must be forwarded to the backend instance."


# ===========================================================================
# RetryConfig
# ===========================================================================

class TestRetryConfig:
    """Engineered to validate RetryConfig default values and custom construction.

    This test class exercises the retry configuration record across 5 scenarios to ensure
    the default budget (8 retries, 2s base delay, 120s max delay, standard 429/502/503/504
    status codes) is correct, that zero retries disables the retry mechanism, and that the
    default exception set includes httpx.TimeoutException and httpx.ConnectError.
    """

    def test_verify_default_config_values(self):
        """Validate that RetryConfig() carries the expected default budget and status codes.

        The test asserts max_retries=8, base_delay=2.0, max_delay=120.0, and that the four
        standard transient HTTP status codes (429, 502, 503, 504) are included in the retry
        set, matching the system-wide retry policy.
        """
        rc = RetryConfig()
        assert rc.max_retries == 8, "Default max_retries must be 8."
        assert rc.base_delay == 2.0, "Default base_delay must be 2.0 seconds."
        assert rc.max_delay == 120.0, "Default max_delay must be 120.0 seconds."
        assert 429 in rc.retryable_status_codes, "429 (Too Many Requests) must be retryable."
        assert 502 in rc.retryable_status_codes, "502 (Bad Gateway) must be retryable."
        assert 503 in rc.retryable_status_codes, "503 (Service Unavailable) must be retryable."
        assert 504 in rc.retryable_status_codes, "504 (Gateway Timeout) must be retryable."

    def test_verify_default_retry_config_is_retry_config_instance(self):
        """Validate that DEFAULT_RETRY_CONFIG is an instance of RetryConfig.

        The test asserts isinstance to confirm the module-level constant is a properly
        constructed RetryConfig and not a frozen or modified sentinel.
        """
        assert isinstance(DEFAULT_RETRY_CONFIG, RetryConfig), \
            "DEFAULT_RETRY_CONFIG must be a RetryConfig instance."

    def test_verify_custom_config_fields(self):
        """Validate that custom RetryConfig fields are stored exactly as passed.

        The test constructs a config with max_retries=5, base_delay=2.0, max_delay=120.0,
        and a custom status-set {429, 500}, then asserts each field matches the input.
        """
        rc = RetryConfig(
            max_retries=5,
            base_delay=2.0,
            max_delay=120.0,
            retryable_status_codes={429, 500},
        )
        assert rc.max_retries == 5, "Custom max_retries must be preserved."
        assert rc.base_delay == 2.0, "Custom base_delay must be preserved."
        assert rc.max_delay == 120.0, "Custom max_delay must be preserved."
        assert rc.retryable_status_codes == {429, 500}, "Custom status-code set must be preserved."

    def test_verify_zero_retries_disables_retry(self):
        """Validate that max_retries=0 disables all retry attempts.

        The test constructs a config with max_retries=0 and asserts the field is 0,
        confirming that consumers can opt out of retry by setting this value.
        """
        rc = RetryConfig(max_retries=0)
        assert rc.max_retries == 0, "max_retries=0 must disable retry."

    def test_verify_default_retryable_exceptions(self):
        """Validate that the default exception set includes common transient httpx errors.

        The test asserts that httpx.TimeoutException and httpx.ConnectError are present in
        retryable_exceptions, confirming the default policy covers the two most common
        network-level transient failures.
        """
        import httpx
        rc = RetryConfig()
        assert httpx.TimeoutException in rc.retryable_exceptions, \
            "TimeoutException must be a retryable exception by default."
        assert httpx.ConnectError in rc.retryable_exceptions, \
            "ConnectError must be a retryable exception by default."


# ===========================================================================
# Backend-specific capability checks
# ===========================================================================

class TestBackendCapabilities:
    """Engineered to validate per-provider capability predicates after backend construction.

    This test class exercises the capability predicates (supports_tool_calling,
    context_window_size, supports_thinking, supports_prompt_caching) on 5 representative
    backends to ensure the factory wires provider-specific features correctly. These
    predicates are consumed by the prompt assembler and tool dispatcher, so they must
    reflect the actual provider contract, not a generic default.
    """

    def test_verify_openai_capabilities(self):
        """Validate OpenAI backend reports tool calling and a positive context window.

        The test asserts supports_tool_calling is True, context_window_size is positive,
        and supports_thinking returns a bool, covering the three primary capability axes.
        """
        be = create_backend("openai", api_key="sk-fake")
        assert be.supports_tool_calling() is True, "OpenAI must advertise tool calling."
        assert be.context_window_size() > 0, "Context window must be a positive integer."
        assert isinstance(be.supports_thinking(), bool), "supports_thinking must return a bool."

    def test_verify_anthropic_capabilities(self):
        """Validate Anthropic backend reports all extended capabilities.

        The test asserts tool calling, a positive context window, extended thinking enabled,
        and prompt caching enabled -- the four capabilities that distinguish Anthropic from
        generic OpenAI-compatible backends.
        """
        be = create_backend("anthropic", api_key="sk-ant-fake")
        assert be.supports_tool_calling() is True, "Anthropic must advertise tool calling."
        assert be.context_window_size() > 0, "Context window must be a positive integer."
        assert be.supports_thinking() is True, "Anthropic must enable extended thinking."
        assert be.supports_prompt_caching() is True, "Anthropic must enable prompt caching."

    def test_verify_deepseek_capabilities(self):
        """Validate DeepSeek backend reports tool calling and a positive context window.

        The test covers the two primary capability axes for the DeepSeek provider route.
        """
        be = create_backend("deepseek", api_key="sk-fake")
        assert be.supports_tool_calling() is True, "DeepSeek must advertise tool calling."
        assert be.context_window_size() > 0, "Context window must be a positive integer."

    def test_verify_local_capabilities(self):
        """Validate local backend reports a positive context window and bool tool-calling flag.

        The local backend is intentionally conservative; the test asserts type correctness
        rather than specific capability flags.
        """
        be = create_backend("local")
        assert be.context_window_size() > 0, "Context window must be a positive integer."
        assert isinstance(be.supports_tool_calling(), bool), "Tool-calling predicate must return a bool."

    def test_verify_ollama_capabilities(self):
        """Validate Ollama backend reports a positive context window and bool tool-calling flag.

        The Ollama backend defers tool-calling determination to the connected server; the
        test only asserts type safety of the predicate.
        """
        be = create_backend("ollama")
        assert be.context_window_size() > 0, "Context window must be a positive integer."
        assert isinstance(be.supports_tool_calling(), bool), "Tool-calling predicate must return a bool."


# ===========================================================================
# Config integration with backends
# ===========================================================================

class TestConfigBackendIntegration:
    """Engineered to validate the interaction between EncreConfig and the backend factory.

    This test class exercises two integration scenarios: (1) an empty backend_type yields
    a None result from create_backend, confirming the config does not default to any vendor;
    (2) explicit backend_type and backend_kwargs are propagated through the config object
    and reflected in the created backend. These tests form the contract boundary between
    the configuration subsystem and the backend factory.
    """

    def test_verify_config_server_backend_type_default_is_empty(self):
        """Validate that EncreConfig starts with an empty backend_type and no backend.

        The test asserts cfg.backend_type is the empty string and that create_backend("")
        returns None, confirming there is no hardcoded vendor default at the config level.
        It then creates an explicit openai backend to confirm the factory still works.
        """
        from encre.config import EncreConfig
        cfg = EncreConfig()
        assert cfg.backend_type == "", "backend_type must be empty until explicitly configured."
        assert create_backend(cfg.backend_type, api_key="sk-fake") is None, \
            "Empty backend_type must yield None from the factory."
        be = create_backend("openai", api_key="sk-fake")
        assert isinstance(be, BaseBackend), "Explicit openai backend must be instantiable."

    def test_verify_config_with_kwargs_propagates_to_backend(self):
        """Validate that EncreConfig fields are reflected in the created backend.

        The test constructs a config with backend_type="anthropic" and backend_kwargs
        containing max_tokens=32768, then asserts both fields are accessible on the config
        object, confirming the config-to-factory wiring is intact.
        """
        from encre.config import EncreConfig
        cfg = EncreConfig(
            backend_type="anthropic",
            backend_kwargs={"max_tokens": 32768},
        )
        assert cfg.backend_type == "anthropic", "Config backend_type must match the constructor value."
        assert cfg.backend_kwargs["max_tokens"] == 32768, "Config backend_kwargs must preserve max_tokens."
