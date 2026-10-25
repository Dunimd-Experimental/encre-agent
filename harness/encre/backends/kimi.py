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
Kimi / Moonshot backend -- Kimi-K2, Kimi-K2.5, Kimi-K2.6 (2026 lineup).

Moonshot AI's Kimi series models offer strong long-context reasoning and
coding capabilities.  The API is OpenAI-compatible.

Models:
- Kimi K2: Base reasoning model
- Kimi K2.5: Enhanced version with tool calling
- Kimi K2.6: Latest with extended capabilities

Two endpoints:
- Global: https://api.moonshot.cn/v1  (KIMI_API_KEY)
- China:  https://api.moonshot.cn/v1  (KIMI_CN_API_KEY, alias kimi-cn)

Base URL: https://api.moonshot.cn/v1
Authentication: KIMI_API_KEY environment variable or explicit api_key.
"""

from typing import Any

import re

from encre.backends.catalog import get_context_window
from encre.backends.openai_sse import OpenAISSEBackend


class KimiBackend(OpenAISSEBackend):
    """Kimi (Moonshot) backend for the Kimi K2 model series.

    Supports Kimi K2, K2.5, and K2.6 via Moonshot AI's OpenAI-compatible API.
    """

    # Moonshot's OpenAI-compatible API supports stream_options.include_usage.
    _include_stream_usage = True

    DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "kimi-k3",
        **kwargs: Any,
    ) -> None:
        """Initialize the Kimi (Moonshot) backend.

        Args:
            api_key: Moonshot API key. Falls back to the KIMI_API_KEY
                environment variable when empty.
            base_url: API endpoint; defaults to the Moonshot v1 URL.
            model: Default Kimi model identifier.
            **kwargs: Additional options forwarded to the parent backend.
        """
        if not base_url:
            # No explicit endpoint given: use the Moonshot v1 URL.
            base_url = self.DEFAULT_BASE_URL
        super().__init__(api_key=api_key, base_url=base_url, model=model, **kwargs)

    def _thinking_request_param(self) -> dict[str, Any] | None:
        """Return the provider-specific parameter that enables thinking.

        Moonshot AI's thinking model lineup uses three parameter shapes
        depending on the model generation:

        * ``kimi-k2.7-code`` (and ``kimi-k2.7-code-highspeed``): always
          thinks, and the official docs explicitly forbid sending any
          ``thinking`` parameter (``type: "disabled"`` is rejected).  We
          therefore return ``None`` to skip it entirely.

        * ``kimi-k3``: always reasons, accepts a top-level
          ``reasoning_effort`` (``"low"``/``"high"``/``"max"``, default
          ``"max"``) and also rejects the ``thinking`` envelope.  We send
          the top-level ``reasoning_effort`` when one was selected.

        * ``kimi-k2.6``: general-purpose thinking model; defaults to
          thinking on.  Accepts ``thinking.type`` (``"enabled"`` /
          ``"disabled"``) and ``thinking.keep`` (``"all"`` for Preserved
          Thinking).

        * ``kimi-k2.5``: legacy thinking model; defaults to thinking on
          and accepts ``thinking.type``.  Does not support
          ``thinking.keep`` (Preserved Thinking).

        Some third-party proxies (e.g. OpenRouter, Apiyi) translate to
        ``enable_thinking: true`` automatically, but Moonshot's native
        endpoint uses the DeepSeek-style ``thinking.type`` envelope.
        """
        if not self.model:
            return None
        m = self.model.lower()
        # kimi-k2.7-code always thinks; sending the param is rejected.
        if "k2.7-code" in m or "k2-7-code" in m:
            return None
        # Kimi K3 always reasons; uses a top-level reasoning_effort and
        # rejects the thinking envelope.  "k3" is matched as a standalone
        # token so unrelated names containing "k3" inside a longer word
        # are not misclassified.
        if re.search(r"\bk3\b", m):
            effort = getattr(self, "reasoning_effort", "") or "max"
            return {"reasoning_effort": effort}
        # kimi-k2.5 / k2.6 / k2-thinking all accept the DeepSeek shape.
        if "k2.5" in m or "k2.6" in m or "k2-thinking" in m or "k2.7" in m:
            return {
                "thinking": {
                    "type": "enabled" if self.thinking_enabled else "disabled",
                }
            }
        return None

    def _build_request_data(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = True,
    ) -> dict[str, Any]:
        """Build the request body, dropping sampling params for Kimi K3.

        K3 pins ``temperature``/``top_p`` internally; explicitly sending
        other values is rejected with an error, so they are omitted for
        K3 models.
        """
        data = super()._build_request_data(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
        )
        if re.search(r"\bk3\b", (self.model or "").lower()):
            data.pop("temperature", None)
            data.pop("top_p", None)
        return data

    def context_window_size(self) -> int:
        """Return the context window size (in tokens) for Kimi models.

        The model catalog (:func:`encre.backends.catalog.get_context_window`)
        is consulted first; the table below only handles unknown names.
        """
        catalog_window = get_context_window(self.model)
        if catalog_window is not None:
            return catalog_window
        m = self.model.lower()
        if "kimi-k3" in m or "-k3" in m:
            return 1_000_000
        if "moonshot-v1-128k" in m:
            return 131_072
        if "moonshot-v1-32k" in m:
            return 32_768
        if "moonshot-v1-8k" in m:
            return 8_192
        return 262_144  # Kimi K2.x series (256K)
