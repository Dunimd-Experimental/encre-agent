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

"""Tests for the extended-thinking helpers in encre.thinking.config.

resolve_thinking_config maps a caller-supplied config plus a model name onto
one of the three ThinkingConfig subtypes, and get_thinking_budget_tokens
collapses any of those subtypes to the single token budget the LLM provider
receives. The suite also inspects the resolver's signature to pin the public
parameter contract.
"""

import inspect

from encre.thinking.config import get_thinking_budget_tokens, resolve_thinking_config
from encre.utils.types import (
    AdaptiveThinking,
    DisabledThinking,
    EnabledThinking,
)


class TestResolveThinkingConfig:
    """Validate the thinking-config resolution logic for model family and explicit config.

    This class exercises the resolver across 9 scenarios: explicit config
    passthrough for EnabledThinking, DisabledThinking, and AdaptiveThinking;
    adaptive selection for Claude-family models (including the 'claude' and
    'sonnet' substrings); a disabled fallback for a non-supporting model
    (GPT-4.1); and adaptive fallback for DeepSeek and Gemini, which declare
    thinking support. The goal is to ensure the resolver maps model names to
    the correct ThinkingConfig subtype so downstream code can rely on the
    enabled flag and budget fields matching the model's real capabilities.
    """
    """Map a model name and optional override to a concrete ThinkingConfig."""

    def test_verify_explicit_config_is_returned_unmodified(self):
        """Validate that an explicitly provided EnabledThinking config is returned unchanged.

        The test passes an explicit config with enabled=True and budget_tokens=5000
        and asserts result is the same object and its fields are preserved because
        an explicit config represents a caller override that must not be second-guessed
        by the resolver, regardless of the model name supplied.
        """
        explicit = EnabledThinking(enabled=True, budget_tokens=5000)
        result = resolve_thinking_config(explicit, "any-model")
        assert result is explicit
        assert result.enabled is True
        assert result.budget_tokens == 5000

    def test_verify_none_config_with_claude_model_resolves_to_adaptive(self):
        """Validate that None config + a Claude model name resolves to AdaptiveThinking with default bounds.

        The test asserts isinstance(result, AdaptiveThinking), result.enabled is True,
        result.min_tokens == 1024, and result.max_tokens == 8192 because Claude models
        declare adaptive thinking support and the resolver must supply sensible default
        bounds when the caller does not provide an explicit config.
        """
        result = resolve_thinking_config(None, "claude-sonnet-5")
        assert isinstance(result, AdaptiveThinking)
        assert result.enabled is True
        assert result.min_tokens == 1024
        assert result.max_tokens == 8192

    def test_verify_none_config_with_sonnet_substring_resolves_to_adaptive(self):
        """Validate that None config + a model name containing 'sonnet' resolves to AdaptiveThinking.

        The test asserts isinstance(result, AdaptiveThinking) and result.enabled is True
        because the resolver uses substring matching on the model name to detect
        Claude-family models, and 'sonnet' is one of the recognized family markers.
        """
        result = resolve_thinking_config(None, "sonnet-20240229")
        assert isinstance(result, AdaptiveThinking)
        assert result.enabled is True

    def test_verify_none_config_with_opus_substring_resolves_to_adaptive(self):
        """Validate that None config + a model name containing 'claude' resolves to AdaptiveThinking.

        The test asserts isinstance(result, AdaptiveThinking) and result.enabled is True
        because 'claude' is the primary family substring the resolver matches, ensuring
        all Claude-model variants (sonnet, opus, haiku, etc.) receive adaptive thinking.
        """
        result = resolve_thinking_config(None, "claude-fable-5")
        assert isinstance(result, AdaptiveThinking)
        assert result.enabled is True

    def test_verify_none_config_with_non_supporting_model_resolves_to_disabled(self):
        """Validate that None config + a non-supporting model resolves to DisabledThinking.

        The test passes 'gpt-4.1' and asserts isinstance(result, DisabledThinking) and
        result.enabled is False because models that do not declare thinking capability
        must fall back to DisabledThinking so the agent loop does not attempt to
        allocate thinking tokens for an unsupported model.
        """
        result = resolve_thinking_config(None, "gpt-4.1")
        assert isinstance(result, DisabledThinking)
        assert result.enabled is False

    def test_verify_none_config_with_deepseek_model_resolves_to_adaptive(self):
        """Validate that None config + DeepSeek model resolves to AdaptiveThinking.

        The test asserts isinstance(result, AdaptiveThinking) and result.enabled is True
        because DeepSeek models declare thinking support in their API and the resolver
        must recognize them as adaptive-capable rather than falling back to disabled.
        """
        result = resolve_thinking_config(None, "deepseek-v3")
        assert isinstance(result, AdaptiveThinking)
        assert result.enabled is True

    def test_verify_none_config_with_gemini_model_resolves_to_adaptive(self):
        """Validate that None config + Gemini model resolves to AdaptiveThinking.

        The test asserts isinstance(result, AdaptiveThinking) and result.enabled is True
        because Gemini 2.5 and later declare thinking support and must be mapped to
        the adaptive branch so the agent can optionally expand its reasoning context.
        """
        result = resolve_thinking_config(None, "gemini-2.5-pro")
        assert isinstance(result, AdaptiveThinking)
        assert result.enabled is True

    def test_verify_disabled_config_is_passed_through_unmodified(self):
        """Validate that an explicit DisabledThinking config is returned unchanged.

        The test passes a DisabledThinking instance and asserts result is the same
        object and result.enabled is False because an explicit disabled config
        represents a deliberate caller choice that must not be overridden by
        model-family heuristics.
        """
        disabled = DisabledThinking()
        result = resolve_thinking_config(disabled, "claude-sonnet-5")
        assert result is disabled
        assert result.enabled is False

    def test_verify_adaptive_config_is_passed_through_unmodified(self):
        """Validate that an explicit AdaptiveThinking config is returned unchanged.

        The test passes an AdaptiveThinking instance with budget_ratio=0.75 and
        asserts result is the same object and result.budget_ratio == 0.75 because
        an explicit adaptive config carries caller-tuned parameters that the
        resolver must preserve rather than replacing with defaults.
        """
        adaptive = AdaptiveThinking(enabled=True, budget_ratio=0.75)
        result = resolve_thinking_config(adaptive, "gpt-5.6")
        assert result is adaptive
        assert result.budget_ratio == 0.75


class TestGetThinkingBudget:
    """Validate the thinking-budget token calculation for each ThinkingConfig subtype.

    This class exercises AdaptiveThinking, EnabledThinking, and DisabledThinking
    across 5 scenarios to ensure get_thinking_budget_tokens returns max_tokens
    for adaptive configs, budget_tokens for enabled configs, and 0 for disabled
    configs, because the budget value is the scalar that the token allocator
    passes to the LLM provider to cap the extended-thinking output length.
    """
    """Reduce any ThinkingConfig subtype to its effective token budget."""

    def test_verify_adaptive_returns_max_tokens(self):
        """Validate that AdaptiveThinking budget equals its max_tokens field.

        The test constructs AdaptiveThinking(enabled=True, max_tokens=8192) and
        asserts the returned budget is 8192 because adaptive configs derive their
        budget from max_tokens rather than budget_tokens, reflecting the variable
        reasoning length the model allocates dynamically within the bounded range.
        """
        config = AdaptiveThinking(enabled=True, max_tokens=8192)
        assert get_thinking_budget_tokens(config) == 8192

    def test_verify_enabled_returns_budget_tokens(self):
        """Validate that EnabledThinking budget equals its budget_tokens field.

        The test constructs EnabledThinking(budget_tokens=16000) and asserts the
        returned budget is 16000 because enabled configs carry an explicit token
        cap that the allocator must honor without interpreting it as a ratio.
        """
        config = EnabledThinking(budget_tokens=16000)
        assert get_thinking_budget_tokens(config) == 16000

    def test_verify_disabled_returns_zero(self):
        """Validate that DisabledThinking always returns a budget of 0.

        The test constructs DisabledThinking() and asserts the returned budget
        is 0 because a disabled config signals that no thinking tokens should
        be allocated, regardless of any default or inherited values.
        """
        config = DisabledThinking()
        assert get_thinking_budget_tokens(config) == 0

    def test_verify_adaptive_custom_max_tokens_is_returned(self):
        """Validate that a custom max_tokens on AdaptiveThinking is returned unchanged.

        The test constructs AdaptiveThinking(enabled=True, max_tokens=16000) and
        asserts the budget is 16000 because custom bounds supplied by the caller
        must be preserved so that operators can tune the thinking budget per model.
        """
        config = AdaptiveThinking(enabled=True, max_tokens=16000)
        assert get_thinking_budget_tokens(config) == 16000

    def test_verify_enabled_custom_budget_tokens_of_zero_is_returned(self):
        """Validate that EnabledThinking with budget_tokens=0 returns 0.

        The test constructs EnabledThinking(budget_tokens=0) and asserts the
        budget is 0 because an explicit zero budget is a valid configuration
        that signals 'allow thinking but cap at zero tokens', which the
        allocator must honor rather than substituting a default.
        """
        config = EnabledThinking(budget_tokens=0)
        assert get_thinking_budget_tokens(config) == 0


class TestResolveThinkingConfigSignature:
    """Validate that the resolve_thinking_config signature matches its public contract.

    This class exercises signature introspection across 2 scenarios to ensure
    the function accepts at least two parameters named 'config' and 'model',
    because external callers and framework integrations depend on these
    parameter names for keyword-based invocation and for IDE autocompletion.
    """
    """Guard against accidental renames of the resolver's public parameters."""

    def test_verify_signature_contains_config_and_model_parameters(self):
        """Validate that resolve_thinking_config accepts 'config' and 'model' parameters.

        The test inspects the function signature and asserts 'config' and 'model'
        are both present in the parameter list because these are the documented
        arguments that callers must supply to resolve a thinking config for a
        given model, and renaming either would break all existing call sites.
        """
        sig = inspect.signature(resolve_thinking_config)
        params = list(sig.parameters.keys())
        assert "config" in params
        assert "model" in params

    def test_verify_signature_has_at_least_two_parameters(self):
        """Validate that resolve_thinking_config has at least two positional parameters.

        The test asserts len(sig.parameters) >= 2 because the function must accept
        both a config and a model argument; fewer parameters would indicate a
        signature regression that breaks the resolver's dual-input design.
        """
        sig = inspect.signature(resolve_thinking_config)
        assert len(sig.parameters) >= 2
