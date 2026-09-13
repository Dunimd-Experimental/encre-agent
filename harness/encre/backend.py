#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

from typing import Any

from encre.backends.aigateway import AIGatewayBackend
from encre.backends.alibaba import AlibabaBackend
from encre.backends.anthropic import AnthropicBackend
from encre.backends.arcee import ArceeBackend
from encre.backends.base import BaseBackend
from encre.backends.bedrock import BedrockBackend
from encre.backends.deepseek import DeepSeekBackend
from encre.backends.failover import FailoverBackend
from encre.backends.github_copilot import GitHubCopilotBackend
from encre.backends.glm import GLMBackend
from encre.backends.gmi import GMIBackend
from encre.backends.google import GoogleBackend
from encre.backends.groq import GroqBackend
from encre.backends.huggingface import HuggingFaceBackend
from encre.backends.kilocode import KiloCodeBackend
from encre.backends.kimi import KimiBackend
from encre.backends.lmstudio import LMStudioBackend
from encre.backends.local import LocalBackend
from encre.backends.minimax import MiniMaxBackend
from encre.backends.novita import NovitaBackend
from encre.backends.ollama import OllamaBackend
from encre.backends.openai import OpenAIBackend
from encre.backends.openai_compatible import OpenAICompatibleBackend
from encre.backends.opencode import OpenCodeGoBackend, OpenCodeZenBackend
from encre.backends.openrouter import OpenRouterBackend
from encre.backends.router import RouterBackend
from encre.utils.types import (
    AdaptiveThinking,
    EnabledThinking,
    ThinkingConfig,
)
from encre.backends.tencent import TencentBackend
from encre.backends.volcengine import VolcengineArkBackend
from encre.backends.xiaomi import XiaomiBackend


# 鈹€鈹€ Helpers 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def _build_failover_backends(
    models: list[Any],
    default_api_key: str = "",
    default_base_url: str = "",
    default_model: str = "",
) -> list[tuple[str, BaseBackend]]:
    """Convert a list of model configs into ``(name, backend)`` pairs."""
    result: list[tuple[str, BaseBackend]] = []
    for m in models:
        if hasattr(m, "enabled") and not m.enabled:
            continue
        if hasattr(m, "name"):  # ModelConfig dataclass
            name = m.name or m.model_id or "unknown"
            bt = m.backend_type or "openai"
            ak = m.api_key or default_api_key
            bu = m.base_url or default_base_url
            md = m.model_id or default_model
            tc = getattr(m, "thinking_config", None)
            cw = getattr(m, "context_window", 0) or 0
        elif isinstance(m, dict):  # Raw dict
            name = m.get("name") or m.get("model_id", "unknown")
            bt = m.get("backend_type", "openai")
            ak = m.get("api_key", default_api_key)
            bu = m.get("base_url", default_base_url)
            md = m.get("model_id", default_model)
            tc = m.get("thinking_config")
            cw = m.get("context_window", 0) or 0
        else:
            continue
        be = create_backend(
            bt,
            api_key=ak,
            base_url=bu,
            model=md,
            thinking_config=tc,
            context_window=cw,
        )
        if be is not None:
            result.append((name, be))
    return result


_FAILOVER_ACCEPTED = frozenset({"retry_config", "connection_monitor"})


def _create_failover(**kwargs: Any) -> FailoverBackend:
    """Construct a ``FailoverBackend`` from factory kwargs."""
    # Pre-instantiated backends (programmatic use).
    backends = kwargs.pop("backends", None)
    if backends is not None:
        return FailoverBackend(backends=backends, **{
            k: v for k, v in kwargs.items() if k in _FAILOVER_ACCEPTED
        })

    # Model config list (from EncreConfig.models).
    models = kwargs.pop("models", None)
    if models:
        built = _build_failover_backends(
            models,
            default_api_key=kwargs.get("api_key", ""),
            default_base_url=kwargs.get("base_url", ""),
            default_model=kwargs.get("model", ""),
        )
        if built:
            return FailoverBackend(backends=built, **{
                k: v for k, v in kwargs.items() if k in _FAILOVER_ACCEPTED
            })

    raise ValueError(
        "FailoverBackend requires either 'backends' (list of tuple) "
        "or 'models' (list of ModelConfig / dict) parameter"
    )


# 鈹€鈹€ Factory 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def _build_thinking_kwargs(
    backend_type: str,
    thinking_config: ThinkingConfig | None,
) -> dict[str, Any]:
    """Translate a ThinkingConfig into backend-specific constructor kwargs."""
    if thinking_config is None:
        return {}

    enabled = thinking_config.enabled
    level = getattr(thinking_config, "level", "") or ""

    if isinstance(thinking_config, AdaptiveThinking):
        budget = thinking_config.max_tokens
    elif isinstance(thinking_config, EnabledThinking):
        budget = thinking_config.budget_tokens
    else:
        budget = 0

    # Anthropic / Bedrock (Claude models) use mode + budget/effort.
    # Adaptive mode does NOT accept budget_tokens; it optionally accepts
    # an effort/level parameter instead.
    if backend_type in ("anthropic", "bedrock"):
        if isinstance(thinking_config, AdaptiveThinking):
            mode = "adaptive"
            budget = 0
        elif enabled:
            mode = "enabled"
        else:
            mode = "disabled"
            budget = 0
        result: dict[str, Any] = {"thinking_mode": mode}
        if budget > 0:
            result["thinking_budget_tokens"] = budget
        if level:
            result["thinking_effort"] = level
        return result

    # Google Gemini uses enable + level + budget.
    if backend_type == "google":
        return {
            "enable_thinking": enabled,
            "thinking_budget": budget,
            "thinking_level": level,
        }

    # DeepSeek uses the thinking envelope plus an optional reasoning_effort.
    if backend_type == "deepseek":
        result = {"thinking_enabled": enabled}
        if level:
            result["reasoning_effort"] = level
        return result

    # Alibaba Qwen uses enable_thinking plus an optional thinking_budget.
    if backend_type == "alibaba":
        result = {"thinking_enabled": enabled}
        if budget > 0:
            result["thinking_budget"] = budget
        return result

    # OpenAI-protocol backends that accept reasoning_effort (o-series, GPT-5.x).
    if backend_type in (
        "openai",
        "openai_compatible",
        "openrouter",
        "novita",
        "aigateway",
        "groq",
        "github-copilot",
        "lmstudio",
        "opencode-zen",
        "opencode-go",
        "huggingface",
    ):
        result = {"thinking_enabled": enabled}
        if level:
            result["reasoning_effort"] = level
        return result

    # Kimi accepts the DeepSeek-style envelope, and Kimi K3 additionally
    # honors a top-level ``reasoning_effort``; forward the level so the
    # request builder can use it for K3.
    if backend_type == "kimi":
        result = {"thinking_enabled": enabled}
        if level:
            result["reasoning_effort"] = level
        return result

    # Xiaomi MiMo accepts the DeepSeek-style thinking envelope together
    # with a ``reasoning_effort`` (low/medium/high); forward the level.
    if backend_type == "xiaomi":
        result = {"thinking_enabled": enabled}
        if level:
            result["reasoning_effort"] = level
        return result

    # OpenAI-protocol backends that use the DeepSeek-style thinking envelope.
    if backend_type in (
        "glm",
        "tencent",
        "volcengine-ark",
        "arcee",
        "gmi",
        "minimax",
        "kilocode",
        "ollama",
    ):
        return {"thinking_enabled": enabled}

    return {}


def _override_context_window(backend: BaseBackend, window: int) -> None:
    """Force ``context_window_size()`` to return an explicit per-model value.

    Only a handful of backends accept a ``context_window`` constructor kwarg
    (openai_compatible, openrouter).  This applies the override generically to
    ANY backend instance -- including ``FailoverBackend``, whose own
    ``context_window_size()`` delegates to the primary child -- so switching to
    a model with a different window takes effect immediately for compaction.
    """
    if not window or window <= 0:
        return
    def _size(_b: BaseBackend = backend, _w: int = window) -> int:
        return _w
    backend.context_window_size = _size  # type: ignore[method-assign]


def create_backend(type: str, **kwargs: Any) -> BaseBackend | None:
    if not type:
        return None

    # The explicit per-model context window is applied generically AFTER
    # construction (see _override_context_window) so every backend honours it
    # without each constructor needing a context_window parameter.
    explicit_window: int = kwargs.pop("context_window", 0) or 0

    # Failover requires special handling 鈥?it needs pre-built children.
    if type == "failover":
        return _create_failover(**kwargs)

    # For non-failover types, strip meta-params that the individual
    # backend constructors do not expect.
    kwargs.pop("models", None)
    thinking_config: ThinkingConfig | None = kwargs.pop("thinking_config", None)

    backend_map: dict[str, type[BaseBackend]] = {
        "openai": OpenAIBackend,
        "anthropic": AnthropicBackend,
        "ollama": OllamaBackend,
        "deepseek": DeepSeekBackend,
        "google": GoogleBackend,
        "groq": GroqBackend,
        "local": LocalBackend,
        "bedrock": BedrockBackend,
        "openai_compatible": OpenAICompatibleBackend,
        "failover": FailoverBackend,
        "router": RouterBackend,
        "openrouter": OpenRouterBackend,
        "novita": NovitaBackend,
        "aigateway": AIGatewayBackend,
        "glm": GLMBackend,
        "kimi": KimiBackend,
        "arcee": ArceeBackend,
        "gmi": GMIBackend,
        "minimax": MiniMaxBackend,
        "alibaba": AlibabaBackend,
        "kilocode": KiloCodeBackend,
        "xiaomi": XiaomiBackend,
        "tencent": TencentBackend,
        "huggingface": HuggingFaceBackend,
        "opencode-zen": OpenCodeZenBackend,
        "opencode-go": OpenCodeGoBackend,
        "lmstudio": LMStudioBackend,
        "github-copilot": GitHubCopilotBackend,
        "volcengine-ark": VolcengineArkBackend,
    }
    cls = backend_map.get(type)
    if cls is None:
        raise ValueError(f"Unknown backend type: {type}. Available: {sorted(backend_map.keys())}")

    # Merge per-model thinking configuration into backend-specific kwargs.
    # Existing kwargs take precedence so callers can still override explicitly.
    thinking_kwargs = _build_thinking_kwargs(type, thinking_config)
    for key, value in thinking_kwargs.items():
        kwargs.setdefault(key, value)

    backend = cls(**kwargs)
    _override_context_window(backend, explicit_window)
    return backend
