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
Tencent TokenHub backend -- Hunyuan and third-party model access.

Tencent Cloud's TokenHub platform provides access to Hunyuan models and
third-party models (DeepSeek, GLM, Kimi, MiniMax, etc.) through an
OpenAI-compatible API.

Models:
- Hunyuan Hy3 (preview): Latest flagship
- Hunyuan TurboS / Turbo: Previous generation
- Third-party: DeepSeek, GLM, Kimi, MiniMax, etc.

Base URL: https://tokenhub.tencentmaas.com/v1
Authentication: TOKENHUB_API_KEY environment variable or explicit api_key.

Aliases: tencent, tokenhub, tencentmaas
"""

from typing import Any

from encre.backends.openai_sse import OpenAISSEBackend


class TencentBackend(OpenAISSEBackend):
    """Tencent TokenHub backend for Hunyuan and third-party models.

    Provides access to Tencent Cloud's LLM platform via an OpenAI-compatible
    API.  Both Hunyuan native models and third-party models are supported.
    """

    DEFAULT_BASE_URL = "https://tokenhub.tencentmaas.com/v1"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "hunyuan-a13b",
        **kwargs: Any,
    ) -> None:
        if not base_url:
            base_url = self.DEFAULT_BASE_URL
        super().__init__(api_key=api_key, base_url=base_url, model=model, **kwargs)

    def _thinking_request_param(self) -> dict[str, Any] | None:
        """Return the provider-specific thinking toggle for Tencent Hunyuan.

        The legacy Hunyuan platform (``enable_thinking`` boolean, only for
        ``-think``/``-t1``/``-a13b`` SKUs) is scheduled for shutdown
        (2026-12-21); its capability moved to TokenHub
        (``tokenhub.tencentmaas.com``).  TokenHub's current models
        (``hy4-*``/``hy3``) use the GLM-style ``{"thinking": {"type": ...}}``
        envelope, which is on by default and can be switched off.
        """
        if not self.model:
            return None
        m = self.model.lower()
        # Legacy reasoning SKUs keep the old boolean toggle.
        if "think" in m or "t1" in m or "a13b" in m:
            return {"enable_thinking": True} if self.thinking_enabled else None
        # TokenHub current-generation models accept the thinking envelope.
        if "hy4" in m or "hy3" in m:
            return {
                "thinking": {
                    "type": "enabled" if self.thinking_enabled else "disabled",
                }
            }
        return None

    def context_window_size(self) -> int:
        m = self.model.lower()
        if "hy4" in m:
            return 1_024_000
        if "a13b" in m:
            return 224_000
        if "vision" in m or "role" in m:
            return 28_000
        return 131_072
