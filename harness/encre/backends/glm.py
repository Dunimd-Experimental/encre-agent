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
GLM (Zhipu AI) backend -- GLM-4.5, GLM-4.6, GLM-4.7 (2026 lineup).

Zhipu AI's GLM series models are among the leading Chinese LLMs, offering
strong reasoning, coding, and multilingual capabilities.  The API is
OpenAI-compatible and supports thinking/reasoning tokens.

Models:
- GLM-4.5: Balanced general-purpose model
- GLM-4.6: Enhanced reasoning with tool calling
- GLM-4.7: Latest flagship with extended context

Base URL: https://open.bigmodel.cn/api/paas/v4
Authentication: GLM_API_KEY environment variable or explicit api_key.
"""

from typing import Any

from encre.backends.openai_sse import OpenAISSEBackend


class GLMBackend(OpenAISSEBackend):
    """GLM (Zhipu AI) backend for the GLM-4.x model series.

    Supports GLM-4.5, GLM-4.6, and GLM-4.7 via Zhipu AI's OpenAI-compatible
    API.  Thinking/reasoning tokens are extracted from ``reasoning_content``.
    """

    # Zhipu's OpenAI-compatible API supports stream_options.include_usage.
    _include_stream_usage = True

    DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "glm-5.3",
        **kwargs: Any,
    ) -> None:
        """Initialize the GLM (Zhipu AI) backend.

        Args:
            api_key: GLM API key. Falls back to the GLM_API_KEY environment
                variable when empty.
            base_url: API endpoint; defaults to the Zhipu AI open API URL.
            model: Default GLM model identifier.
            **kwargs: Additional options forwarded to the parent backend.
        """
        if not base_url:
            # No explicit endpoint given: use the Zhipu AI open API URL.
            base_url = self.DEFAULT_BASE_URL
        super().__init__(api_key=api_key, base_url=base_url, model=model, **kwargs)

    def _thinking_request_param(self) -> dict[str, Any] | None:
        """GLM-4.6 / GLM-4.7 use the DeepSeek-style ``thinking`` envelope.

        Zhipu AI's GLM-4.6 and later reasoning models accept the same
        ``{"thinking": {"type": "enabled"}}`` parameter as DeepSeek.  Older
        GLM-4 / GLM-3 models do not advertise a thinking toggle, so we
        only emit the parameter for models that look reasoning-capable.
        """
        if not self.model:
            return None
        m = self.model.lower()
        # GLM-4.5+ and GLM-5.x are reasoning-capable.
        # Unknown/older SKUs skip the thinking toggle to avoid a 400.
        if "glm-4.5" in m or "glm-4.6" in m or "glm-4.7" in m or "glm-5" in m:
            # GLM-5.3 is FORCED thinking: an explicit ``disabled`` envelope
            # is rejected with an error.  Omit the parameter so the default
            # (thinking on) applies instead.
            if "glm-5.3" in m and not self.thinking_enabled:
                return None
            # GLM-4.5+ and GLM-5.x expose the DeepSeek-style thinking toggle.
            return {
                "thinking": {
                    "type": "enabled" if self.thinking_enabled else "disabled",
                }
            }
        return None

    def context_window_size(self) -> int:
        """Return context window for GLM models.

        2026: GLM-5.3/5.2: 1M, GLM-5.1/5/4.7/4.6: 200K, GLM-4-Long: 1M.
        """
        m = self.model.lower()
        # GLM-5.3 / GLM-5.2 flagships expose a 1M token context window.
        if "glm-5.3" in m or "glm-5.2" in m:
            return 1_000_000
        # GLM-4-Long is optimized for extremely long documents (up to 1M).
        if "glm-4-long" in m or "glm-4long" in m:
            return 1_000_000
        # GLM-5.1 / GLM-5 / GLM-5-Turbo / GLM-4.7 / GLM-4.6 expose 200K.
        if "glm-5" in m or "glm-4.6" in m or "glm-4.7" in m:
            return 200_000
        # Older GLM-4.x models default to a 128K (131072) window.
        return 131_072
