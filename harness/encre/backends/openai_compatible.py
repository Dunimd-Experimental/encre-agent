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

"""
OpenAI-compatible backend -- generic adapter for any OpenAI-compatible API.

Many LLM providers offer APIs that are compatible with OpenAI's chat
completion format, including:

- **vLLM**: Self-hosted inference server
- **Together AI**: Cloud API for open-source models
- **Fireworks AI**: Fast inference platform
- **Perplexity**: Search-augmented LLM API
- **OpenRouter**: Unified API for multiple providers
- **Any local/self-hosted server** using the OpenAI protocol

This backend extends :class:`OpenAISSEBackend` and supports:

- SSE streaming with text, tool calls, and reasoning/thinking tokens
- Provider-specific reasoning content (``reasoning_content`` delta field)
- Context window sizing from the model config (defaults to 1M for large models)
- Automatic URL normalisation (appends ``/v1`` if missing)
"""

import re
from typing import Any

from encre.backends.openai_sse import OpenAISSEBackend



from encre.logging_config import get_logger
logger = get_logger(__name__)

# Known 1M-context model families (prefix or regex patterns) for auto-detection
_LARGE_CONTEXT_PATTERNS: list[str] = [
    "gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-4.1", "gpt-6",
    "deepseek-v4", "deepseek-chat", "deepseek-reasoner",
    "claude-opus-4-7", "claude-sonnet-4-6", "claude-4",
    "gemini-2.5", "gemini-2.0", "gemini-3",
    "qwen3", "qwen-max", "qwen-plus", "qwen-long",
    "llama-4", "llama-3.3",
    "mixtral", "mistral-large", "mistral-medium",
    "yi-large", "yi-medium",
    "hunyuan", "ernie-4",
    "doubao", "glm-5",
    "kimi-k2", "minimax-m2",
]

_SMALL_CONTEXT_PATTERNS: list[str] = [
    # Legacy models that are NOT 1M
    "gpt-4o-2024", "gpt-4-1106", "gpt-4-0125",
    "gpt-3.5", "text-embedding",
    "deepseek-v1", "deepseek-v2",
    "glm-4", "glm-3",
]


class OpenAICompatibleBackend(OpenAISSEBackend):
    """Generic backend for any OpenAI-compatible API endpoint.

    Works with any provider serving an OpenAI-compatible chat completions API.
    Supports reasoning/thinking tokens (``reasoning_content``) from providers
    like OpenRouter, Together AI, Fireworks, vLLM, and others.

    Configure with the provider's base URL and desired model name.

    Examples:
        - vLLM: ``base_url="http://localhost:8000/v1"``
        - Together AI: ``base_url="https://api.together.xyz/v1"``
        - Fireworks AI: ``base_url="https://api.fireworks.ai/inference/v1"``
        - Perplexity: ``base_url="https://api.perplexity.ai"``
        - OpenRouter: ``base_url="https://openrouter.ai/api/v1"``

    Args:
        api_key: API key for the provider.
        base_url: Base URL of the OpenAI-compatible API endpoint.
        model: Model name to use.
        context_window: Optional explicit context window size override.
        **kwargs: Additional arguments passed to :class:`OpenAISSEBackend`.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "gpt-5.6-luna",
        context_window: int = 0,
        **kwargs: Any,
    ) -> None:
        # Normalise the base URL: many providers use /v1, but users may omit it
        # -- and some users paste the full chat-completions endpoint.  Strip a
        # trailing ``/chat/completions`` first so the path is not doubled (the
        # request layer appends ``/chat/completions`` itself); only then decide
        # whether ``/v1`` needs to be added.
        base_url = (base_url or "").rstrip("/")
        # Strip a pasted full chat-completions path so it is not doubled later.
        base_url = base_url.removesuffix("/chat/completions").rstrip("/")
        if base_url and not base_url.endswith("/v1") and not re.search(r"/v\d+$", base_url):
            base_url = base_url + "/v1"
        super().__init__(api_key=api_key, base_url=base_url, model=model, **kwargs)
        self._context_window = context_window

    def _thinking_request_param(self) -> dict[str, Any] | None:
        """Generic OpenAI-compatible endpoints should NOT send ``thinking``.

        The base :meth:`OpenAISSEBackend._thinking_request_param` defaults to
        the DeepSeek-style envelope (``{"thinking": {"type": "enabled"}}``),
        which only a handful of providers accept.  Unknown third-party
        routers (e.g. ``api.tokenrouter.com``) often reject ``"enabled"``
        with HTTP 400 -- many of them only accept ``"adaptive"`` /
        ``"disabled"`` (the Anthropic vocabulary) or no parameter at all.

        Because :class:`OpenAICompatibleBackend` is the catch-all for "looks
        like OpenAI but isn't a known provider", the safe default is to omit
        the thinking envelope.  Providers that want it (DeepSeek, GLM, Kimi,
        Alibaba, Tencent, OpenRouter, OpenAI native, Xiaomi) each have their
        own backend subclass that overrides this method, so they are
        unaffected.
        """
        if not self.thinking_enabled:
            return None
        if self.reasoning_effort:
            return {"reasoning_effort": self.reasoning_effort}
        return None

    # ── Context window ──────────────────────────────────────────────────

    def context_window_size(self) -> int:
        """Return the context window size for this model.

        Resolution order: (1) explicitly configured value, (2) the
        data-driven model registry / provider defaults, (3) model-name
        heuristics, (4) the 256 000 fallback.  The registry is consulted
        BEFORE the heuristics so switching models always takes effect
        immediately for the compaction math instead of being shadowed by
        stale name-pattern guesses.
        """
        if self._context_window > 0:
            return self._context_window
        try:
            from encre.backends.registry import resolve_model_info
            info = resolve_model_info(self.model)
            if info is not None and getattr(info, "context_window", 0) and info.context_window > 0:
                return int(info.context_window)
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
        return _detect_context_window(self.model)


def _detect_context_window(model: str) -> int:
    """Guess the context window size from the model name.

    For known large-context model families (GPT-4.1, DeepSeek V4, Claude 4,
    etc.) returns 1 048 576 (1M).  For unreckognised models returns 256 000
    as a reasonable modern default since most 2026 models have >= 200K.
    """
    model_lower = model.lower()
    for pattern in _SMALL_CONTEXT_PATTERNS:
        if pattern in model_lower:
            return 128_000
    for pattern in _LARGE_CONTEXT_PATTERNS:
        if pattern in model_lower:
            return 1_048_576
    return 256_000
