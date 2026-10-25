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
OpenRouter backend -- unified API for 200+ models across providers.

OpenRouter provides a single API endpoint that routes requests to 200+ models
from providers including OpenAI, Anthropic, Google, Meta, Mistral, and many  # noqa: E402
more.  It supports OpenAI-compatible chat completions with transparent cost
tracking and model fallback.

Base URL: https://openrouter.ai/api/v1
Authentication: OPENROUTER_API_KEY environment variable or explicit api_key.
"""

from typing import Any

from encre.backends.openai_sse import OpenAISSEBackend

# Context window sizes for known OpenRouter model families.
# Keys are checked as prefix or substring (first match wins), so list
# more-specific families before generic ones.
_CONTEXT_WINDOW_MAP: dict[str, int] = {
    # Anthropic (current 5.x = 1M; legacy 4.x = 200K).
    "claude-opus-5": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-opus": 200_000,
    "claude-sonnet": 200_000,
    "claude-haiku": 200_000,
    # OpenAI GPT-5.x = 1.05M, GPT-4.1 = 1M.
    "gpt-5.6": 1_050_000,
    "gpt-5.5": 1_050_000,
    "gpt-5": 1_050_000,
    "gpt-4.1": 1_048_576,
    # DeepSeek V4 = 1M.
    "deepseek-v4": 1_048_576,
    "deepseek-chat": 1_048_576,
    "deepseek-reasoner": 1_048_576,
    # Google Gemini 3.x / 2.5 = 1M.
    "gemini-3": 1_048_576,
    "gemini-2.5": 1_048_576,
    # Meta Llama 4 Scout = ~1.31M, Maverick = 1M.
    "llama-4-scout": 1_310_720,
    "llama-4": 1_048_576,
    "llama-3.3": 131_072,
    # xAI Grok 4.6/4.5 = 500K.
    "grok-4.6": 500_000,
    "grok-4.5": 500_000,
    "grok-4": 256_000,
    # Qwen 3.5+ = 1M, older Qwen3 = 131K.
    "qwen3.8": 1_000_000,
    "qwen3.7": 1_000_000,
    "qwen3.6": 1_000_000,
    "qwen3.5": 1_000_000,
    "qwen3": 131_072,
    "qwen-max": 131_072,
    # Mistral.
    "mistral-large": 128_000,
    "mixtral": 131_072,
}


def _detect_context_window_openrouter(model: str) -> int:
    """Guess context window from model name."""
    model_lower = model.lower()
    for prefix, size in _CONTEXT_WINDOW_MAP.items():
        # Match either an exact prefix or the family substring (e.g. "claude-opus-4-7" -> "claude-opus").
        if model_lower.startswith(prefix) or prefix in model_lower:
            return size
    # OpenRouter default: most models are at least 200k.
    return 200000  # OpenRouter default: most models are at least 200k


class OpenRouterBackend(OpenAISSEBackend):
    """OpenRouter backend for unified multi-provider access.

    Routes requests through OpenRouter's API gateway, which supports 200+
    models from various providers.  Supports reasoning/thinking tokens.
    """

    # OpenRouter's API supports stream_options.include_usage.
    _include_stream_usage = True

    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "openrouter/auto",
        context_window: int = 0,
        **kwargs: Any,
    ) -> None:
        if not base_url:
            base_url = self.DEFAULT_BASE_URL
        super().__init__(api_key=api_key, base_url=base_url, model=model, **kwargs)
        self._context_window = context_window

    def _thinking_request_param(self) -> dict[str, Any] | None:
        """OpenRouter uses the ``reasoning`` envelope.

        OpenRouter's reasoning API supports two equivalent shapes:
        ``{"reasoning": {"enabled": True}}`` (toggle) and
        ``{"reasoning": {"effort": "low|medium|high"}}`` (effort level).
        Per OpenRouter docs the ``reasoning`` map may include
        ``enabled``, ``effort``, ``max_tokens``, and ``exclude``.

        For models routed via OpenRouter we send ``{"reasoning":
        {"enabled": True}}`` so that all underlying providers that
        support thinking (OpenAI o-series / GPT-5, Anthropic Claude,
        DeepSeek, GLM, etc.) will emit their reasoning tokens.  The
        response field is ``reasoning`` (single string) or
        ``reasoning_details`` (array of step objects); both are picked
        up by :data:`_REASONING_FIELD_NAMES` in
        :class:`OpenAISSEBackend`.
        """
        if self.reasoning_effort:
            return {"reasoning": {"enabled": self.thinking_enabled, "effort": self.reasoning_effort}}
        return {"reasoning": {"enabled": self.thinking_enabled}}

    def context_window_size(self) -> int:
        if self._context_window > 0:
            return self._context_window
        return _detect_context_window_openrouter(self.model)
