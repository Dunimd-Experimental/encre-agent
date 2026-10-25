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

"""Authoritative provider catalog.

Every entry describes one service provider:
  - ``id``       : the backend_type string accepted by ``create_backend``
  - ``label``    : human-readable provider name
  - ``base_url`` : official documented endpoint (prefill for the provider form)
  - ``docs``     : provider documentation URL (so users can verify)
  - ``auth``     : credential style ("api_key", "none", "aws_iam", ...)

No preset model lists are shipped: every model is user-defined under its
provider (all models are custom).  Users can discover available model IDs
through the provider's documentation or the ``fetch_models`` API probe.

To extend: add to ``PROVIDERS`` below and re-export through
``encre/backends/__init__.py`` and ``encre/__init__.py``.
"""

from typing import Any

# Default per-turn output token budgets, keyed by provider id.
# Providers cap differently from input context; these match documented limits.
DEFAULT_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "anthropic": 128000,
    "openai": 128000,
    "deepseek": 32768,
    "google": 8192,
    "groq": 32768,
    "ollama": 8192,
    "lmstudio": 8192,
    "bedrock": 8192,
    "openai_compatible": 8192,
    "openrouter": 128000,
    "novita": 8192,
    "aigateway": 8192,
    "glm": 65536,
    "kimi": 65536,
    "arcee": 8192,
    "gmi": 8192,
    "minimax": 65536,
    "alibaba": 65536,
    "kilocode": 128000,
    "xiaomi": 8192,
    "tencent": 65536,
    "huggingface": 8192,
    "opencode-zen": 65536,
    "opencode-go": 8192,
    "github-copilot": 65536,
    "volcengine-ark": 128000,
    "failover": 8192,
    "router": 8192,
    "local": 4096,
    # OpenAI-compatible gateway providers (official docs verified).
    "302ai": 8192,
    "atomic-chat": 8192,
    "azure-openai": 16384,
    "azure-cognitive": 16384,
    "baseten": 8192,
    "cerebras": 8192,
    "cortecs": 8192,
    "deep-infra": 8192,
    "fireworks": 8192,
    "frogbot": 8192,
    "helicone": 8192,
    "ionet": 8192,
    "llamacpp": 8192,
    "nebius": 8192,
    "ollama-cloud": 8192,
    "ovhcloud": 8192,
    "scaleway": 8192,
    "stackit": 8192,
    "together": 8192,
    "venice": 8192,
    "vercel": 8192,
    "xai": 8192,
    "zai": 65536,
    "zenmux": 8192,
}


PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "anthropic",
        "label": "Anthropic",
        "base_url": "https://api.anthropic.com",
        "docs": "https://docs.anthropic.com/en/docs/about-claude/models",
        "auth": "api_key",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "docs": "https://platform.openai.com/docs/models",
        "auth": "api_key",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "docs": "https://api-docs.deepseek.com/quick_start/pricing",
        "auth": "api_key",
    },
    {
        "id": "google",
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com",
        "docs": "https://ai.google.dev/gemini-api/docs/models",
        "auth": "api_key",
    },
    {
        "id": "groq",
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "docs": "https://console.groq.com/docs/models",
        "auth": "api_key",
    },
    {
        "id": "ollama",
        "label": "Ollama (local)",
        "base_url": "http://localhost:11434",
        "docs": "https://ollama.com/library",
        "auth": "none",
    },
    {
        "id": "lmstudio",
        "label": "LM Studio (local)",
        "base_url": "http://localhost:1234/v1",
        "docs": "https://lmstudio.ai/docs/api",
        "auth": "none",
    },
    {
        "id": "local",
        "label": "Local (Transformers)",
        "base_url": "",
        "docs": "https://huggingface.co/docs/transformers",
        "auth": "none",
    },
    {
        "id": "bedrock",
        "label": "Amazon Bedrock",
        "base_url": "",
        "docs": "https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html",
        "auth": "aws_iam",
    },
    {
        "id": "openai_compatible",
        "label": "OpenAI-Compatible (custom)",
        "base_url": "",
        "docs": "",
        "auth": "api_key",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "docs": "https://openrouter.ai/models",
        "auth": "api_key",
    },
    {
        "id": "novita",
        "label": "Novita AI",
        "base_url": "https://api.novita.ai/openai/v1",
        "docs": "https://novita.ai/docs/api-reference/llms-llm-api",
        "auth": "api_key",
    },
    {
        "id": "aigateway",
        "label": "Cloudflare AI Gateway",
        "base_url": "https://gateway.ai.cloudflare.com/v1",
        "docs": "https://developers.cloudflare.com/ai-gateway/",
        "auth": "api_key",
    },
    {
        "id": "huggingface",
        "label": "Hugging Face Inference Providers",
        "base_url": "https://router.huggingface.co/v1",
        "docs": "https://huggingface.co/docs/inference-providers",
        "auth": "api_key",
    },
    {
        "id": "github-copilot",
        "label": "GitHub Copilot",
        "base_url": "https://api.githubcopilot.com",
        "docs": "https://docs.github.com/en/copilot",
        "auth": "github_oauth",
    },
    {
        "id": "glm",
        "label": "BigModel (GLM)",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "docs": "https://docs.bigmodel.cn/cn/guide/models/text",
        "auth": "api_key",
    },
    {
        "id": "kimi",
        "label": "Moonshot AI (Kimi)",
        "base_url": "https://api.moonshot.cn/v1",
        "docs": "https://platform.moonshot.cn/docs/models",
        "auth": "api_key",
    },
    {
        "id": "minimax",
        "label": "MiniMax",
        "base_url": "https://api.minimax.io/v1",
        "docs": "https://platform.minimax.io/docs/llms.txt",
        "auth": "api_key",
    },
    {
        "id": "alibaba",
        "label": "Alibaba Cloud Model Studio (Qwen)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "docs": "https://help.aliyun.com/zh/model-studio/text-generation-model/",
        "auth": "api_key",
    },
    {
        "id": "tencent",
        "label": "Tencent Hunyuan",
        "base_url": "https://tokenhub.tencentmaas.com/v1",
        "docs": "https://cloud.tencent.com/document/product/1823/132252",
        "auth": "api_key",
    },
    {
        "id": "xiaomi",
        "label": "Xiaomi MiMo",
        "base_url": "https://api.xiaomimimo.com/v1",
        "docs": "https://huggingface.co/XiaomiMiMo",
        "auth": "api_key",
    },
    # Volcengine Ark accepts BOTH universal model IDs (doubao-*) and
    # account-specific deployed endpoint IDs (ep-xxxxxx) as the model field.
    {
        "id": "volcengine-ark",
        "label": "Volcengine Ark",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "docs": "https://www.volcengine.com/docs/82379/1330310",
        "auth": "api_key",
    },
    {
        "id": "arcee",
        "label": "Arcee AI",
        "base_url": "https://api.arcee.ai/v2",
        "docs": "https://docs.arcee.ai/",
        "auth": "api_key",
    },
    {
        "id": "gmi",
        "label": "GMI Cloud",
        "base_url": "https://api.gmi-serving.com/v1",
        "docs": "https://docs.gmicloud.ai/model-quickstarts/text/overview.md",
        "auth": "api_key",
    },
    {
        "id": "kilocode",
        "label": "Kilo Code",
        "base_url": "https://kilocode.ai/api/openrouter",
        "docs": "https://kilocode.ai/docs/",
        "auth": "api_key",
    },
    {
        "id": "opencode-zen",
        "label": "OpenCode Zen",
        "base_url": "https://opencode.ai/zen/v1",
        "docs": "https://opencode.ai/docs/zen/",
        "auth": "api_key",
    },
    {
        "id": "opencode-go",
        "label": "OpenCode Go",
        "base_url": "https://opencode.ai/zen/go/v1",
        "docs": "https://opencode.ai/docs/go/",
        "auth": "api_key",
    },
    {
        "id": "failover",
        "label": "Failover (composite)",
        "base_url": "",
        "docs": "",
        "auth": "none",
    },
    {
        "id": "router",
        "label": "Router (composite)",
        "base_url": "",
        "docs": "",
        "auth": "none",
    },
    # OpenAI-compatible gateway providers -- every base URL below was
    # verified against the provider's OWN documentation (never aggregator
    # sites).  See gateway_providers.py for the source table.
    {
        "id": "302ai",
        "label": "302.AI",
        "base_url": "https://api.302.ai/v1",
        "docs": "https://doc.302.ai/",
        "auth": "api_key",
    },
    {
        "id": "azure-openai",
        "label": "Azure OpenAI",
        "base_url": "",
        "docs": "https://learn.microsoft.com/azure/foundry/openai/",
        "auth": "api_key",
    },
    {
        "id": "azure-cognitive",
        "label": "Azure Cognitive Services",
        "base_url": "",
        "docs": "https://learn.microsoft.com/azure/ai-services/openai/",
        "auth": "api_key",
    },
    {
        "id": "baseten",
        "label": "Baseten",
        "base_url": "https://inference.baseten.co/v1",
        "docs": "https://docs.baseten.co/quickstart",
        "auth": "api_key",
    },
    {
        "id": "cerebras",
        "label": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "docs": "https://inference-docs.cerebras.ai/",
        "auth": "api_key",
    },
    {
        "id": "cortecs",
        "label": "Cortecs",
        "base_url": "https://api.cortecs.ai/v1",
        "docs": "https://docs.cortecs.ai/",
        "auth": "api_key",
    },
    {
        "id": "deep-infra",
        "label": "Deep Infra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "docs": "https://docs.deepinfra.com/",
        "auth": "api_key",
    },
    {
        "id": "fireworks",
        "label": "Fireworks AI",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "docs": "https://fireworks.ai/docs/tools-sdks/openai-compatibility",
        "auth": "api_key",
    },
    {
        "id": "helicone",
        "label": "Helicone AI Gateway",
        "base_url": "https://ai-gateway.helicone.ai",
        "docs": "https://docs.helicone.ai/getting-started/quick-start",
        "auth": "api_key",
    },
    {
        "id": "ionet",
        "label": "IO Intelligence (io.net)",
        "base_url": "https://api.intelligence.io.solutions/api/v1",
        "docs": "https://io.net/docs/reference/ai-models/get-started-with-io-intelligence-api",
        "auth": "api_key",
    },
    {
        "id": "nebius",
        "label": "Nebius Token Factory",
        "base_url": "https://api.tokenfactory.nebius.com/v1",
        "docs": "https://docs.tokenfactory.nebius.com/quickstart",
        "auth": "api_key",
    },
    {
        "id": "ollama-cloud",
        "label": "Ollama Cloud",
        "base_url": "https://ollama.com/v1",
        "docs": "https://docs.ollama.com/api/openai-compatibility",
        "auth": "api_key",
    },
    {
        "id": "ovhcloud",
        "label": "OVHcloud AI Endpoints",
        "base_url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        "docs": "https://docs.ovhcloud.com/",
        "auth": "api_key",
    },
    {
        "id": "scaleway",
        "label": "Scaleway Generative APIs",
        "base_url": "https://api.scaleway.ai/v1",
        "docs": "https://www.scaleway.com/en/docs/generative-apis/reference-content/openai-compatibility/",
        "auth": "api_key",
    },
    {
        "id": "stackit",
        "label": "STACKIT AI Model Serving",
        "base_url": "https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1",
        "docs": "https://docs.stackit.cloud/",
        "auth": "api_key",
    },
    {
        "id": "together",
        "label": "Together AI",
        "base_url": "https://api.together.ai/v1",
        "docs": "https://docs.together.ai/docs/inference/openai-compatibility",
        "auth": "api_key",
    },
    {
        "id": "venice",
        "label": "Venice AI",
        "base_url": "https://api.venice.ai/api/v1",
        "docs": "https://docs.venice.ai/api-reference/endpoint/chat/completions",
        "auth": "api_key",
    },
    {
        "id": "vercel",
        "label": "Vercel AI Gateway",
        "base_url": "https://ai-gateway.vercel.sh/v1",
        "docs": "https://vercel.com/docs/ai-gateway/openai-compat",
        "auth": "api_key",
    },
    {
        "id": "xai",
        "label": "xAI (Grok)",
        "base_url": "https://api.x.ai/v1",
        "docs": "https://docs.x.ai/docs/api-reference",
        "auth": "api_key",
    },
    {
        "id": "zai",
        "label": "Z.AI",
        "base_url": "https://api.z.ai/api/paas/v4",
        "docs": "https://docs.z.ai/",
        "auth": "api_key",
    },
    {
        "id": "zenmux",
        "label": "ZenMux",
        "base_url": "https://zenmux.ai/api/v1",
        "docs": "https://zenmux.ai/docs/zh/guide/quickstart.html",
        "auth": "api_key",
    },
    # Local / self-hosted gateways (endpoint fixed by the tool itself).
    {
        "id": "atomic-chat",
        "label": "Atomic Chat (local)",
        "base_url": "http://127.0.0.1:1337/v1",
        "docs": "",
        "auth": "none",
    },
    {
        "id": "llamacpp",
        "label": "llama.cpp (local)",
        "base_url": "http://127.0.0.1:8080/v1",
        "docs": "https://github.com/ggml-org/llama.cpp",
        "auth": "none",
    },
    {
        "id": "frogbot",
        "label": "FrogBot Gateway (self-hosted)",
        "base_url": "http://localhost:3939/v1",
        "docs": "https://docs.frogbot.ai/",
        "auth": "api_key",
    },
]


def default_output_tokens(provider_id: str) -> int:
    """Return a safe default for the per-turn output token budget."""
    # Fall back to a conservative 8192 tokens for unlisted providers.
    return DEFAULT_MAX_OUTPUT_TOKENS.get(provider_id, 8192)


# ---------------------------------------------------------------------------
# Model context windows -- single source of truth
# ---------------------------------------------------------------------------
# One lookup path for per-model context windows.  ``registry.resolve_model_info``
# consults :func:`get_context_window` before applying provider-level defaults,
# and the OpenAI / Anthropic / Kimi backends consult it before their own
# family heuristics.  Values mirror the documented 2026 limits that the
# per-backend reference tables used to duplicate.

MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # OpenAI
    "gpt-4.1": 1048576,
    "gpt-4.1-mini": 1048576,
    "gpt-4.1-nano": 1048576,
    "gpt-4o": 128000,
    "gpt-5.1": 128000,
    "gpt-5.2": 128000,
    "gpt-5.3": 128000,
    "gpt-5.4": 400000,
    "gpt-5.5": 1048576,
    "gpt-5.6": 1050000,
    "gpt-6": 1048576,
    "o3": 200000,
    "o4-mini": 200000,
    "o5": 400000,
    # Anthropic (API-style hyphenated model names)
    "claude-opus-5": 1000000,
    "claude-fable-5": 1000000,
    "claude-sonnet-5": 1000000,
    "claude-opus-4-8": 1000000,
    "claude-opus-4-7": 1000000,
    "claude-sonnet-4-6": 1000000,
    # Kimi / Moonshot
    "kimi-k3": 1000000,
    "moonshot-v1-128k": 131072,
    "moonshot-v1-32k": 32768,
    "moonshot-v1-8k": 8192,
}


def _is_version_suffix(suffix: str) -> bool:
    """Return True when *suffix* looks like a version/date/build tag.

    Used by :func:`get_context_window` so that e.g. ``gpt-4.1-2026-05``
    fuzzy-matches the ``gpt-4.1`` entry while a fabricated name such as
    ``claude-opus-5-imaginary`` does not.
    """
    if not suffix:
        return True
    parts = [p for p in suffix.replace("_", "-").replace(".", "-").split("-") if p]
    if not parts:
        return True
    return all(part[0].isdigit() or part[0] in {"v", "V"} for part in parts)


def get_context_window(model_name: str) -> int | None:
    """Return the context window (tokens) for *model_name*, or ``None``.

    Matching strategy:
      1. Exact (case-insensitive) table hit.
      2. Fuzzy: the name starts with a table entry and the remainder is a
         version/date-like suffix (e.g. ``gpt-4.1-2026-05`` matches the
         ``gpt-4.1`` entry).

    ``None`` means "no catalog entry" -- callers fall back to their own
    provider default or family heuristic.
    """
    name = (model_name or "").strip().lower()
    if not name:
        return None
    window = MODEL_CONTEXT_WINDOWS.get(name)
    if window is not None:
        return window
    for entry, entry_window in MODEL_CONTEXT_WINDOWS.items():
        if name.startswith(entry) and _is_version_suffix(name[len(entry):]):
            return entry_window
    return None


def catalog_payload() -> dict[str, Any]:
    """Serializable snapshot used by the frontend provider form."""
    return {
        "providers": PROVIDERS,
        "default_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
    }


__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "MODEL_CONTEXT_WINDOWS",
    "PROVIDERS",
    "catalog_payload",
    "default_output_tokens",
    "get_context_window",
]
