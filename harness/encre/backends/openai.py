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
OpenAI backend -- GPT-4.1 family, GPT-5.x, o3, o4-mini (2026 lineup).

As of May 2026, OpenAI's model lineup has evolved significantly:

- **GPT-4.1 family** (Nano / Mini / full): The mid-range workhorse replacing
  GPT-4o (deprecated Jan 2026). All variants support 1,048,576 (1M) token
  context windows and prompt caching at 75% off cached input tokens.
  Pricing: $2/$8 per 1M tokens (full), $0.40/$1.60 (Mini), $0.10/$0.40 (Nano).

- **GPT-5.x series** (5.2 / 5.4 / 5.5): Tiered flagship models with varying
  context windows (128K-1M) and capability levels. GPT-5.5 is the most capable
  with 1M context at $5/$30 per 1M tokens.

- **o3 / o4-mini**: Reasoning-optimised models with 200K context windows and
  extended output limits (100K tokens). Designed for complex multi-step
  reasoning, code generation, and analysis tasks.

- **GPT-4o**: Fully deprecated as of January 2026. Kept in the registry for
  backward compatibility only; all new sessions should use GPT-4.1.

This backend extends :class:`OpenAISSEBackend` and inherits all SSE streaming,
tool call buffering, and non-stream fallback logic.  The only customisation
is the default API endpoint (``https://api.openai.com/v1``) and the default
model (``gpt-5.6``).
"""

from typing import Any

import re

from encre.backends.catalog import get_context_window
from encre.backends.openai_sse import OpenAISSEBackend
from encre.backends.retry import DEFAULT_RETRY_CONFIG


class OpenAIBackend(OpenAISSEBackend):
    """OpenAI backend for the 2026 model lineup.

    Supports GPT-4.1 (Nano/Mini/full), GPT-5.x (5.2/5.4/5.5), o3, and
    gpt-5.6-luna.  The default model is ``gpt-5.6`` (alias for
    ``gpt-5.6-sol``), the latest flagship model.

    All models support tool calling, image inputs, and prompt caching.
    The GPT-4.1 family offers 1M token context windows at competitive prices.
    o3 and o4-mini add extended reasoning capabilities with 200K context and
    100K output token limits.

    2026 pricing summary:
        - GPT-4.1: $2.00/$8.00 per 1M tokens (input/output)
        - GPT-4.1 Mini: $0.40/$1.60 per 1M tokens
        - GPT-4.1 Nano: $0.10/$0.40 per 1M tokens
        - GPT-5.2: $1.75/$14.00 per 1M tokens (128K context)
        - GPT-5.4: $2.50/$15.00 per 1M tokens (400K context)
        - GPT-5.5: $5.00/$30.00 per 1M tokens (1M context)
        - o3: $2.00/$8.00 per 1M tokens (200K context)
        - o4-mini: $1.10/$4.40 per 1M tokens (200K context)
    """

    # OpenAI's API supports stream_options.include_usage.
    _include_stream_usage = True

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "gpt-5.6",
        **kwargs: Any,
    ) -> None:
        """Initialise the OpenAI backend.

        Args:
            api_key: OpenAI API key.  If empty, falls back to the
                ``OPENAI_API_KEY`` environment variable (handled by the caller).
            base_url: Custom API base URL.  Defaults to
                ``https://api.openai.com/v1``.  Can be changed to use Azure
                OpenAI or other OpenAI-compatible endpoints.
            model: Model name.  Defaults to ``gpt-5.6`` (alias for
                ``gpt-5.6-sol``, the latest flagship model).  Other valid
                values: ``gpt-5.6-terra``, ``gpt-5.6-luna``, ``gpt-5.5``,
                ``gpt-5.4``, ``gpt-5.2``, ``gpt-4.1``, ``gpt-4.1-mini``,
                ``gpt-4.1-nano``, ``o3``, ``o4-mini``.
            **kwargs: Additional arguments passed to :class:`OpenAISSEBackend`.
        """
        if not base_url:
            # Default to OpenAI's public API when no custom base URL is given.
            base_url = "https://api.openai.com/v1"
        # Preserve any custom retry config instead of silently dropping it.
        retry_config = kwargs.pop("retry_config", DEFAULT_RETRY_CONFIG)
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            model=model,
            retry_config=retry_config,
            **kwargs,
        )

    # ── Overrides ─────────────────────────────────────────────────────

    def _thinking_request_param(self) -> dict[str, Any] | None:
        """OpenAI chat completions uses ``reasoning_effort``.

        OpenAI's reasoning-capable models (o3, o4-mini, o5, GPT-5.x) accept
        ``reasoning_effort: "low"|"medium"|"high"`` in the chat completions
        API; ``"medium"`` is the default and is what we send here.  Older
        models (GPT-4.1 family, GPT-4o legacy, GPT-5-chat-latest) do not
        accept a thinking toggle, so we omit the parameter to avoid a 400.
        """
        if not self.model or not self.thinking_enabled:
            return None
        m = self.model.lower()
        effort = self.reasoning_effort or "medium"
        # Reasoning-capable families.
        if m.startswith("o3") or m.startswith("o4") or m.startswith("o5"):
            return {"reasoning_effort": effort}
        if m.startswith("gpt-5") and "chat-latest" not in m:
            return {"reasoning_effort": effort}
        # Non-reasoning models: do not send the parameter.
        return None

    def context_window_size(self) -> int:
        """Return the context window size for the current model.

        The model catalog (:func:`encre.backends.catalog.get_context_window`)
        is the single source of truth; the family heuristics below only run
        for names the catalog does not know.

        2026 reference:
            - GPT-4.1 / Mini / Nano: 1,048,576 tokens
            - GPT-5.2: 128,000 tokens
            - GPT-5.4: 400,000 tokens
            - GPT-5.5: 1,048,576 tokens
            - GPT-6: 1,048,576 tokens (expected)
            - o3: 200,000 tokens
            - o4-mini: 200,000 tokens
            - o5: 400,000 tokens
        """
        catalog_window = get_context_window(self.model)
        if catalog_window is not None:
            return catalog_window
        model_lower = self.model.lower()
        # Check o-series FIRST to avoid o4-mini/o3-mini matching "mini" below
        if model_lower.startswith("o3") or model_lower.startswith("o4"):
            return 200000
        if model_lower.startswith("o5"):
            return 400000
        # GPT-5.6 family (incl. luna/terra) -- 1.05M
        if "gpt-5.6" in model_lower:
            return 1050000
        # GPT-4.1 family (nano/mini/4.1) -- 1M context
        if "nano" in model_lower or "mini" in model_lower or "4.1" in model_lower:
            return 1048576
        # "6" as a standalone version token so e.g. "gpt-4o-16k" is not
        # mistaken for the GPT-6 family.
        if "5.5" in model_lower or re.search(r"\b6\b", model_lower):
            return 1048576
        if "5.4" in model_lower:
            return 400000
        if "5.2" in model_lower or "5.3" in model_lower or "5.1" in model_lower:
            return 128000
        # GPT-4o legacy -- 128K
        if "gpt-4o" in model_lower:
            return 128000
        return 128000
