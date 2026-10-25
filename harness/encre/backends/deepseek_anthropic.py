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
DeepSeek Anthropic API backend -- V4-Flash, V4-Pro via Anthropic protocol.

DeepSeek exposes an Anthropic-compatible endpoint at
``https://api.deepseek.com/anthropic`` that speaks the Anthropic Messages
API protocol.  This backend extends :class:`AnthropicBackend` with
DeepSeek-specific defaults and capabilities:

- ``server_tool_use`` content blocks (server-side tool execution)
- ``web_search_tool_result`` content blocks (built-in web search)
- ``output_config.effort`` for reasoning depth control
- ``reasoning_effort`` alias (maps to DeepSeek's ``output_config.effort``)
- 1M token context window for all V4 models
- Simpler thinking parameter (DeepSeek ignores ``budget_tokens``)

The existing :class:`DeepSeekBackend` (OpenAI protocol) is unaffected.
"""

from typing import Any

from encre.backends.anthropic import AnthropicBackend


class DeepSeekAnthropicBackend(AnthropicBackend):
    """DeepSeek backend using the Anthropic-compatible API protocol.

    Uses ``https://api.deepseek.com/anthropic`` as the base URL and
    defaults to ``deepseek-v4-flash``.  Supports all V4 models via the
    Anthropic Messages API format, plus DeepSeek-specific content types
    (``server_tool_use``, ``web_search_tool_result``) and config
    (``output_config.effort``).
    """

    DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "deepseek-v4-flash",
        thinking_mode: str = "enabled",
        reasoning_effort: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialise the DeepSeek Anthropic backend.

        Args:
            api_key: DeepSeek API key.
            base_url: Custom API base URL.  Defaults to
                ``https://api.deepseek.com/anthropic``.
            model: Model name.  Defaults to ``deepseek-v4-flash``.
                Other valid values: ``deepseek-v4-pro``.
            thinking_mode: One of ``"enabled"`` (default) or
                ``"disabled"``.  ``budget_tokens`` is ignored by DeepSeek.
            reasoning_effort: Controls reasoning depth.  Maps to
                ``output_config.effort`` in the request body.
                Accepted values: ``"low"``, ``"medium"``, ``"high"``,
                ``"max"``.  When set, enables thinking mode automatically.
            **kwargs: Additional arguments passed to :class:`AnthropicBackend`.
        """
        if not base_url:
            # No explicit endpoint given: use DeepSeek's Anthropic-compatible URL.
            base_url = self.DEFAULT_BASE_URL
        # DeepSeek ignores budget_tokens, so we use a minimal budget
        # that satisfies Anthropic protocol requirements.
        super().__init__(
            api_key=api_key,
            model=model,
            thinking_budget_tokens=1024,
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
            **kwargs,
        )
        # Override the HTTP client base URL to point to DeepSeek.
        self._client.base_url = base_url
        # DeepSeek uses the same x-api-key auth header as Anthropic but
        # does not require the anthropic-version header.
        self._client.headers.pop("anthropic-version", None)

    # ── Overrides ─────────────────────────────────────────────────────

    def _build_thinking_param(self, max_tokens: int) -> dict[str, Any] | None:
        """Return the thinking parameter for DeepSeek's Anthropic endpoint.

        DeepSeek supports ``{"type": "enabled"}`` and ``{"type": "disabled"}``
        but ignores ``budget_tokens``.  When ``reasoning_effort`` is set,
        ``output_config.effort`` is used instead (handled in ``chat()``).
        """
        if self.thinking_mode == "disabled":
            return None
        # If reasoning_effort is set, output_config handles the depth;
        # we still send basic thinking enabled for compatibility.
        return {"type": "enabled"}

    def context_window_size(self) -> int:
        """DeepSeek V4 models support 1,048,576 (1M) token context."""
        # All DeepSeek V4 models expose a 1M token context window.
        return 1048576

    def supports_thinking(self) -> bool:
        """DeepSeek V4 models support reasoning/thinking tokens."""
        return True

    def supports_prompt_caching(self) -> bool:
        """DeepSeek V4 models support prompt caching (80-92% discount)."""
        return True
