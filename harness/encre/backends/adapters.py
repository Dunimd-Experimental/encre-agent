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

"""Built-in backend adapters -- the real dispatch table of the factory (P1.2).

Every entry the former hard-coded ``backend_map`` in :mod:`encre.backend`
served is now a real :class:`~encre.backends.adapter_api.BackendAdapter`
instance listed in :data:`BUILTIN_ADAPTERS` (same names, same registration
order, so ``matches()`` reproduces the historical dispatch semantics
exactly -- first registered match wins).

Two adapter shapes exist:

* :class:`SimpleBackendAdapter` -- one concrete backend class matched by
  exact ``backend_type``.  Its ``create()`` reproduces the former factory
  preprocessing verbatim: strip the failover-only ``models`` kwarg, pop
  ``thinking_config`` and merge the provider-specific thinking kwargs
  (``_build_thinking_kwargs``, moved here unchanged), then construct.
* :class:`FailoverAdapter` -- the composite backend.  It keeps the
  historical special case: it receives the raw kwargs (including
  ``models``) and delegates to :func:`_create_failover`, which builds the
  children through the public ``create_backend`` so every child gets the
  same adapter treatment (thinking kwargs, per-model context window).

``model_info`` deliberately returns ``None`` on all built-ins: their model
metadata already lives in the static registry
(:mod:`encre.backends.registry`), so they always decline enrichment.
"""


from typing import Any

from encre.backends.adapter_api import BackendConfig
from encre.backends.aigateway import AIGatewayBackend
from encre.backends.alibaba import AlibabaBackend
from encre.backends.anthropic import AnthropicBackend
from encre.backends.arcee import ArceeBackend
from encre.backends.base import BaseBackend
from encre.backends.bedrock import BedrockBackend
from encre.backends.deepseek import DeepSeekBackend
from encre.backends.failover import FailoverBackend
from encre.backends.gateway_providers import (
    AtomicChatBackend,
    AzureCognitiveBackend,
    AzureOpenAIBackend,
    BasetenBackend,
    CerebrasBackend,
    CortecsBackend,
    DeepInfraBackend,
    FireworksBackend,
    FrogBotBackend,
    HeliconeBackend,
    IONetBackend,
    LlamaCppBackend,
    NebiusBackend,
    OVHCloudBackend,
    OllamaCloudBackend,
    OpenAI302Backend,
    ScalewayBackend,
    STACKITBackend,
    TogetherBackend,
    VeniceBackend,
    VercelAIGatewayBackend,
    XAIBackend,
    ZAIBackend,
    ZenMuxBackend,
)
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
from encre.backends.registry import ModelInfo
from encre.backends.router import RouterBackend
from encre.backends.tencent import TencentBackend
from encre.backends.volcengine import VolcengineArkBackend
from encre.backends.xiaomi import XiaomiBackend
from encre.utils.types import (
    AdaptiveThinking,
    EnabledThinking,
    ThinkingConfig,
)


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

    # OpenAI-protocol backends that accept reasoning_effort (o-series, GPT-5.x,
    # and the gateway/infra providers verified to forward it -- the level probe
    # in encre.backends.multimodal decides the real set per endpoint live).
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
        "302ai",
        "azure-openai",
        "azure-cognitive",
        "baseten",
        "cerebras",
        "cortecs",
        "deep-infra",
        "fireworks",
        "helicone",
        "ionet",
        "nebius",
        "ollama-cloud",
        "ovhcloud",
        "scaleway",
        "stackit",
        "together",
        "venice",
        "vercel",
        "xai",
        "zenmux",
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
        "zai",
    ):
        return {"thinking_enabled": enabled}

    return {}


def _build_failover_backends(
    models: list[Any],
    default_api_key: str = "",
    default_base_url: str = "",
    default_model: str = "",
) -> list[tuple[str, BaseBackend]]:
    """Convert a list of model configs into ``(name, backend)`` pairs."""
    # Deferred import: the factory facade composes this module, while the
    # children must go through the full public factory path (adapter
    # resolution, thinking kwargs, per-model context window).
    from encre.backend import create_backend

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
            eh = getattr(m, "headers", None) or None
            # Provider "advanced config" sampling overrides (None = unset).
            temp = getattr(m, "temperature", None)
            topp = getattr(m, "top_p", None)
            topk = getattr(m, "top_k", None)
        elif isinstance(m, dict):  # Raw dict
            name = m.get("name") or m.get("model_id", "unknown")
            bt = m.get("backend_type", "openai")
            ak = m.get("api_key", default_api_key)
            bu = m.get("base_url", default_base_url)
            md = m.get("model_id", default_model)
            tc = m.get("thinking_config")
            cw = m.get("context_window", 0) or 0
            eh = m.get("headers") or None
            temp = m.get("temperature")
            topp = m.get("top_p")
            topk = m.get("top_k")
        else:
            continue
        be = create_backend(
            bt,
            api_key=ak,
            base_url=bu,
            model=md,
            thinking_config=tc,
            context_window=cw,
            extra_headers=eh,
            temperature=temp,
            top_p=topp,
            top_k=topk,
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


class SimpleBackendAdapter:
    """Adapter for one concrete backend class, matched by exact type string.

    ``create()`` reproduces the former factory preprocessing verbatim:
    strip the failover-only ``models`` kwarg, pop ``thinking_config`` and
    merge the provider-specific thinking kwargs, then construct.
    """

    def __init__(self, name: str, backend_cls: type[BaseBackend]) -> None:
        self.name = name
        self.backend_cls = backend_cls

    def matches(self, config: BackendConfig) -> bool:
        """Serve only the exact ``backend_type`` this adapter was built for."""
        return config.backend_type == self.name

    def create(self, config: BackendConfig) -> BaseBackend:
        """Construct the backend with the historical kwarg preprocessing."""
        kwargs = dict(config.kwargs)
        kwargs.pop("models", None)
        thinking_config: ThinkingConfig | None = kwargs.pop("thinking_config", None)
        thinking_kwargs = _build_thinking_kwargs(self.name, thinking_config)
        for key, value in thinking_kwargs.items():
            kwargs.setdefault(key, value)
        return self.backend_cls(**kwargs)

    def model_info(
        self, model_name: str, config: BackendConfig | None = None,
    ) -> ModelInfo | None:
        """Decline: built-in model metadata lives in the static registry."""
        return None


class FailoverAdapter:
    """Adapter for the composite failover backend.

    Unlike simple backends, failover consumes the raw ``models`` kwarg to
    build its children through the public factory, so ``create()`` forwards
    the kwargs untouched; leftover meta-params are filtered inside
    :func:`_create_failover` (preserving the historical behaviour).
    """

    name = "failover"

    def matches(self, config: BackendConfig) -> bool:
        """Serve only ``backend_type == "failover"``."""
        return config.backend_type == self.name

    def create(self, config: BackendConfig) -> BaseBackend:
        """Delegate to the failover constructor (children built lazily)."""
        return _create_failover(**config.kwargs)

    def model_info(
        self, model_name: str, config: BackendConfig | None = None,
    ) -> ModelInfo | None:
        """Decline: failover delegates capability queries to its primary."""
        return None


# ---------------------------------------------------------------------------
# Built-in adapters -- same names and same order as the former hard-coded
# ``backend_map`` (registration order is dispatch precedence).
# ---------------------------------------------------------------------------

BUILTIN_ADAPTERS: tuple[Any, ...] = (
    SimpleBackendAdapter("openai", OpenAIBackend),
    SimpleBackendAdapter("anthropic", AnthropicBackend),
    SimpleBackendAdapter("ollama", OllamaBackend),
    SimpleBackendAdapter("deepseek", DeepSeekBackend),
    SimpleBackendAdapter("google", GoogleBackend),
    SimpleBackendAdapter("groq", GroqBackend),
    SimpleBackendAdapter("local", LocalBackend),
    SimpleBackendAdapter("bedrock", BedrockBackend),
    SimpleBackendAdapter("openai_compatible", OpenAICompatibleBackend),
    FailoverAdapter(),
    SimpleBackendAdapter("router", RouterBackend),
    SimpleBackendAdapter("openrouter", OpenRouterBackend),
    SimpleBackendAdapter("novita", NovitaBackend),
    SimpleBackendAdapter("aigateway", AIGatewayBackend),
    SimpleBackendAdapter("glm", GLMBackend),
    SimpleBackendAdapter("kimi", KimiBackend),
    SimpleBackendAdapter("arcee", ArceeBackend),
    SimpleBackendAdapter("gmi", GMIBackend),
    SimpleBackendAdapter("minimax", MiniMaxBackend),
    SimpleBackendAdapter("alibaba", AlibabaBackend),
    SimpleBackendAdapter("kilocode", KiloCodeBackend),
    SimpleBackendAdapter("xiaomi", XiaomiBackend),
    SimpleBackendAdapter("tencent", TencentBackend),
    SimpleBackendAdapter("huggingface", HuggingFaceBackend),
    SimpleBackendAdapter("opencode-zen", OpenCodeZenBackend),
    SimpleBackendAdapter("opencode-go", OpenCodeGoBackend),
    SimpleBackendAdapter("lmstudio", LMStudioBackend),
    SimpleBackendAdapter("github-copilot", GitHubCopilotBackend),
    SimpleBackendAdapter("volcengine-ark", VolcengineArkBackend),
    # OpenAI-compatible gateway providers (base URLs verified against each
    # provider's official documentation; see gateway_providers.py).
    SimpleBackendAdapter("302ai", OpenAI302Backend),
    SimpleBackendAdapter("atomic-chat", AtomicChatBackend),
    SimpleBackendAdapter("azure-openai", AzureOpenAIBackend),
    SimpleBackendAdapter("azure-cognitive", AzureCognitiveBackend),
    SimpleBackendAdapter("baseten", BasetenBackend),
    SimpleBackendAdapter("cerebras", CerebrasBackend),
    SimpleBackendAdapter("cortecs", CortecsBackend),
    SimpleBackendAdapter("deep-infra", DeepInfraBackend),
    SimpleBackendAdapter("fireworks", FireworksBackend),
    SimpleBackendAdapter("frogbot", FrogBotBackend),
    SimpleBackendAdapter("helicone", HeliconeBackend),
    SimpleBackendAdapter("ionet", IONetBackend),
    SimpleBackendAdapter("llamacpp", LlamaCppBackend),
    SimpleBackendAdapter("nebius", NebiusBackend),
    SimpleBackendAdapter("ollama-cloud", OllamaCloudBackend),
    SimpleBackendAdapter("ovhcloud", OVHCloudBackend),
    SimpleBackendAdapter("scaleway", ScalewayBackend),
    SimpleBackendAdapter("stackit", STACKITBackend),
    SimpleBackendAdapter("together", TogetherBackend),
    SimpleBackendAdapter("venice", VeniceBackend),
    SimpleBackendAdapter("vercel", VercelAIGatewayBackend),
    SimpleBackendAdapter("xai", XAIBackend),
    SimpleBackendAdapter("zai", ZAIBackend),
    SimpleBackendAdapter("zenmux", ZenMuxBackend),
)
