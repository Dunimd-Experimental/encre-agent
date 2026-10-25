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

"""Tests for EncreConfig: defaults, keyword overrides, serialization, and feature flags.

Covers the default value of every field on EncreConfig, single- and
multi-field keyword overrides, backend_kwargs passthrough isolation from the
top-level fields, to_dict() serialization plus **data round-trip
reconstruction, and the optional thinking_config / enable_prompt_caching
features.
"""


from encre.config import EncreConfig


class TestEncreConfigDefaults:
    """Tests that EncreConfig initializes every field with its documented default.

    Each default controls agent behavior (model selection, token budget,
    permission policy, sandboxing, telemetry, logging, checkpoint limits and
    session TTL). Pinning them here catches regressions that would silently
    shift runtime behavior.
    """

    def test_verify_default_model_is_empty_string(self):
        """The default model field is empty rather than a hardcoded vendor name.

        An empty default forces the caller to select a model explicitly,
        keeping the product vendor-agnostic.
        """
        config = EncreConfig()
        assert config.model == ""

    def test_verify_default_backend_type_is_empty_string(self):
        """The default backend_type is empty and is derived from the active ModelConfig.

        backend_type is a denormalized convenience field, populated only after
        a model is chosen so the config object stays consistent.
        """
        config = EncreConfig()
        assert config.backend_type == ""

    def test_verify_default_max_tokens_is_four_thousand_ninety_six(self):
        """The default max_tokens is 4096, a conservative starting context budget.

        Small enough to avoid excess API cost on default runs while still
        accommodating short tool-call chains.
        """
        config = EncreConfig()
        assert config.max_tokens == 4096

    def test_verify_temperature_field_is_not_present_on_default_config(self):
        """Temperature does not exist on the top-level EncreConfig instance.

        Temperature belongs to the backend-specific model config; asserting its
        absence keeps callers from depending on a field in another layer.
        """
        config = EncreConfig()
        assert not hasattr(config, "temperature")

    def test_verify_default_permission_mode_is_bypass(self):
        """The default permission_mode is 'bypass', allowing tools without prompting.

        This matches the developer-oriented posture of the agent and keeps
        integration tests from blocking on permission responses.
        """
        config = EncreConfig()
        assert config.permission_mode == "bypass"

    def test_verify_default_max_turns_is_zero_for_unlimited(self):
        """The default max_turns is 0, which is the sentinel for an unlimited budget.

        Quota enforcement code relies on 0 consistently meaning "no limit"
        rather than an arbitrary positive integer.
        """
        config = EncreConfig()
        assert config.max_turns == 0

    def test_verify_default_sandbox_enabled_is_true(self):
        """The default sandbox_enabled is True, keeping execution contained.

        Sandboxing is a security requirement, so unconfigured deployments must
        not run arbitrary commands without containment.
        """
        config = EncreConfig()
        assert config.sandbox_enabled is True

    def test_verify_default_telemetry_enabled_is_true(self):
        """The default telemetry_enabled is True, enabling usage observation.

        Telemetry drives product metrics and error tracking unless the operator
        explicitly opts out.
        """
        config = EncreConfig()
        assert config.telemetry_enabled is True

    def test_verify_default_log_level_is_info(self):
        """The default log_level is 'INFO', a balanced observability setting.

        INFO captures enough structure for debugging without the noise of DEBUG.
        """
        config = EncreConfig()
        assert config.log_level == "INFO"

    def test_verify_default_enable_prompt_caching_is_true(self):
        """The default enable_prompt_caching is True, reducing repeated-prefix API cost."""
        config = EncreConfig()
        assert config.enable_prompt_caching is True

    def test_verify_default_checkpoint_max_count_is_ten(self):
        """The default checkpoint_max_count is 10, capping stored snapshots.

        The cap prevents unbounded growth in long-running sessions while still
        offering enough rollback depth for typical workflows.
        """
        config = EncreConfig()
        assert config.checkpoint_max_count == 10

    def test_verify_default_tool_result_max_chars_is_eighty_thousand(self):
        """The default tool_result_max_chars is 80000, capping tool output size.

        Truncating oversized tool results prevents context-window exhaustion.
        """
        config = EncreConfig()
        assert config.tool_result_max_chars == 80000

    def test_verify_default_session_max_age_hours_is_twenty_four(self):
        """The default session_max_age_hours is 24.0, the session TTL.

        A 24-hour TTL balances persistent context against stale-session
        resource leakage.
        """
        config = EncreConfig()
        assert config.session_max_age_hours == 24.0

    def test_verify_default_api_key_is_empty_string(self):
        """The default api_key is empty, requiring explicit configuration.

        A blank default prevents accidentally picking up a developer's local key
        and makes credential-leak regressions visible.
        """
        config = EncreConfig()
        assert config.api_key == ""

    def test_verify_default_base_url_is_empty_string(self):
        """The default base_url is empty, requiring explicit configuration.

        Leaving it blank lets the backend choose its own canonical endpoint and
        prevents unintended proxy routing.
        """
        config = EncreConfig()
        assert config.base_url == ""

    def test_verify_default_workspace_is_empty_string(self):
        """The default workspace is empty, requiring explicit configuration.

        Workspace defines the root directory the agent operates in; leaving it
        unset stops the agent from silently working in the wrong directory.
        """
        config = EncreConfig()
        assert config.workspace == ""


class TestEncreConfigKeywordOverrides:
    """Tests that keyword arguments override EncreConfig defaults correctly.

    Each test supplies one or more overrides and asserts the field matches the
    supplied value. The multi-field case confirms that combined overrides do
    not interfere with each other.
    """

    def test_verify_model_override_applies_correctly(self):
        """Passing model='gpt-4o-mini' sets config.model to that value.

        Model selection is the most fundamental override, so the keyword route
        must reach the field without intermediate transformation.
        """
        config = EncreConfig(model="gpt-4o-mini")
        assert config.model == "gpt-4o-mini"

    def test_verify_max_tokens_override_applies_correctly(self):
        """Passing max_tokens=8192 sets config.max_tokens to that value.

        Doubling the default budget confirms the override path handles values
        above the default without clamping.
        """
        config = EncreConfig(max_tokens=8192)
        assert config.max_tokens == 8192

    def test_verify_max_turns_override_applies_correctly(self):
        """Passing max_turns=5 sets config.max_turns to that value.

        A finite limit replaces the default unlimited (0) so the loop's
        termination check sees the new value.
        """
        config = EncreConfig(max_turns=5)
        assert config.max_turns == 5

    def test_verify_permission_mode_override_applies_correctly(self):
        """Passing permission_mode='bypass' sets config.permission_mode to that value.

        Permission mode controls whether the agent prompts before tool
        execution, so the override must reach the safety layer intact.
        """
        config = EncreConfig(permission_mode="bypass")
        assert config.permission_mode == "bypass"

    def test_verify_backend_type_override_applies_correctly(self):
        """Passing backend_type='anthropic' sets config.backend_type to that value.

        Backend type selects the function-calling protocol and message format,
        so the routing layer depends on this propagating correctly.
        """
        config = EncreConfig(backend_type="anthropic")
        assert config.backend_type == "anthropic"

    def test_verify_log_level_override_applies_correctly(self):
        """Passing log_level='DEBUG' sets config.log_level to that value."""
        config = EncreConfig(log_level="DEBUG")
        assert config.log_level == "DEBUG"

    def test_verify_sandbox_enabled_override_applies_correctly(self):
        """Passing sandbox_enabled=False disables the execution sandbox.

        Overrides must flip the flag so the execution wrapper can skip the
        containment layer when a trusted environment is used.
        """
        config = EncreConfig(sandbox_enabled=False)
        assert config.sandbox_enabled is False

    def test_verify_telemetry_enabled_override_applies_correctly(self):
        """Passing telemetry_enabled=False disables telemetry collection.

        Privacy-sensitive runs must be able to suppress metrics.
        """
        config = EncreConfig(telemetry_enabled=False)
        assert config.telemetry_enabled is False

    def test_verify_session_max_age_hours_override_applies_correctly(self):
        """Passing session_max_age_hours=48.0 extends the session TTL.

        Confirms floating-point overrides are preserved without integer
        truncation.
        """
        config = EncreConfig(session_max_age_hours=48.0)
        assert config.session_max_age_hours == 48.0

    def test_verify_multiple_overrides_apply_simultaneously(self):
        """Multiple keyword overrides coexist without field clobbering.

        Combined overrides are the common production path; three independent
        fields must each retain their supplied value in one constructor call.
        """
        config = EncreConfig(model="deepseek-chat", max_tokens=32000, permission_mode="accept_edits")  # noqa: E501
        assert config.model == "deepseek-chat"
        assert config.max_tokens == 32000
        assert config.permission_mode == "accept_edits"


class TestEncreConfigBackendKwargs:
    """Tests for backend_kwargs handling on EncreConfig.

    backend_kwargs is a free-form dict passed through to the model backend
    client. These tests confirm it is stored as-is and does not leak into the
    top-level config fields.
    """

    def test_verify_default_backend_kwargs_is_an_empty_dict(self):
        """backend_kwargs defaults to an empty dict rather than None."""
        config = EncreConfig()
        assert config.backend_kwargs == {}

    def test_verify_backend_kwargs_is_stored_as_supplied(self):
        """backend_kwargs stores the exact dict passed at construction.

        The dict is forwarded to the HTTP client, so mutation during storage
        would silently change the transport parameters.
        """
        config = EncreConfig(backend_kwargs={"temperature": 0.7, "top_p": 0.9})
        assert config.backend_kwargs == {"temperature": 0.7, "top_p": 0.9}

    def test_verify_backend_kwargs_does_not_affect_top_level_model_field(self):
        """A 'model' key inside backend_kwargs does not change config.model.

        backend_kwargs is transport-layer passthrough; it must not shortcut the
        explicit model field, which would make resolution ambiguous.
        """
        config = EncreConfig(backend_kwargs={"model": "fake"})
        assert config.model == ""  # passthrough must not touch the explicit field
        assert config.backend_kwargs["model"] == "fake"


class TestEncreConfigToDict:
    """Tests for to_dict() serialization on EncreConfig.

    to_dict is the persistence and transmission path. These tests confirm the
    output is a plain dict, contains defaults, reflects overrides, carries
    backend_kwargs, and round-trips through **data reconstruction.
    """

    def test_verify_to_dict_returns_a_plain_dict(self):
        """to_dict() returns an instance of dict rather than a custom mapping.

        Callers depend on standard dict methods; a custom mapping would break
        JSON serialization and keyword unpacking.
        """
        config = EncreConfig()
        result = config.to_dict()
        assert isinstance(result, dict)

    def test_verify_to_dict_contains_default_values_for_key_fields(self):
        """to_dict() emits the expected defaults for model, max_tokens, permission_mode, backend_type.

        The serialized form is used for diffs and audits, so default values
        must appear explicitly without a schema lookup.
        """
        config = EncreConfig()
        result = config.to_dict()
        assert result["model"] == ""
        assert result["max_tokens"] == 4096
        assert result["permission_mode"] == "bypass"
        assert result["backend_type"] == ""

    def test_verify_to_dict_reflects_user_overrides(self):
        """to_dict() emits overridden values rather than the hard-coded defaults.

        Overrides must survive serialization so persistence carries the caller's
        intent.
        """
        config = EncreConfig(model="claude-sonnet-4-20250514", backend_type="anthropic")
        result = config.to_dict()
        assert result["model"] == "claude-sonnet-4-20250514"
        assert result["backend_type"] == "anthropic"

    def test_verify_to_dict_includes_backend_kwargs(self):
        """to_dict() carries the backend_kwargs dict into the output.

        Transport-layer settings must travel with the config, or the HTTP client
        would reset to defaults on reconstruction.
        """
        config = EncreConfig(backend_kwargs={"timeout": 60})
        result = config.to_dict()
        assert result["backend_kwargs"] == {"timeout": 60}

    def test_verify_to_dict_round_trip_preserves_key_fields(self):
        """to_dict() output can reconstruct an equivalent config via EncreConfig(**data).

        Round-trip fidelity is the persistence contract: the reconstructed
        config must match the original on the key fields.
        """
        config1 = EncreConfig(model="gemini-pro", max_tokens=10000, permission_mode="dont_ask")
        data = config1.to_dict()
        config2 = EncreConfig(**data)
        assert config2.model == config1.model
        assert config2.max_tokens == config1.max_tokens
        assert config2.permission_mode == config1.permission_mode


class TestEncreConfigSpecialization:
    """Spot-checks for specialized optional flags on EncreConfig.

    thinking_config and enable_prompt_caching are optional features that must
    exist on the config object even when not supplied at construction time.
    These tests confirm the default state and that both can be overridden.
    """

    def test_verify_thinking_config_defaults_to_none(self):
        """thinking_config is None by default, meaning no thinking mode is selected.

        None is the sentinel callers branch on, so the field must exist rather
        than raising AttributeError.
        """
        config = EncreConfig()
        assert config.thinking_config is None

    def test_verify_thinking_config_is_settable_to_an_adaptive_thinking_instance(self):
        """thinking_config accepts an AdaptiveThinking instance and stores it by reference.

        Storing by reference lets the agent inspect enabled, min_tokens and
        max_tokens at runtime without re-parsing.
        """
        from encre.utils.types import AdaptiveThinking
        tc = AdaptiveThinking(enabled=True, min_tokens=1024, max_tokens=8192)
        config = EncreConfig(thinking_config=tc)
        assert config.thinking_config is tc
        assert config.thinking_config.enabled is True

    def test_verify_enable_prompt_caching_is_settable_to_false(self):
        """enable_prompt_caching can be overridden to False at construction time."""
        config = EncreConfig(enable_prompt_caching=False)
        assert config.enable_prompt_caching is False
