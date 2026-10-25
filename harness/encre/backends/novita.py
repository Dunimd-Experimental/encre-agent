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
NovitaAI backend -- 200+ models, Model API, Agent Sandbox, GPU Cloud.

NovitaAI provides access to 200+ open-source and proprietary models through
an OpenAI-compatible API.  It also offers Agent Sandbox and GPU Cloud services.

Base URL: https://api.novita.ai/v3/openai
Authentication: NOVITA_API_KEY environment variable or explicit api_key.
"""

from typing import Any

from encre.backends.openai_sse import OpenAISSEBackend


class NovitaBackend(OpenAISSEBackend):
    """NovitaAI backend for 200+ model access.

    Provides access to open-source and proprietary models through NovitaAI's
    OpenAI-compatible API gateway.
    """

    DEFAULT_BASE_URL = "https://api.novita.ai/openai/v1"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "deepseek/deepseek-v4-pro",
        **kwargs: Any,
    ) -> None:
        if not base_url:
            # Fall back to NovitaAI's OpenAI-compatible gateway default when no URL is supplied.
            base_url = self.DEFAULT_BASE_URL
        super().__init__(api_key=api_key, base_url=base_url, model=model, **kwargs)

    def context_window_size(self) -> int:
        m = self.model.lower()
        # DeepSeek V4 / Kimi K3 / GLM 5.x / MiniMax M3 expose ~1M windows.
        if "deepseek-v4" in m or "kimi-k3" in m or "glm-5" in m or "minimax-m3" in m or "mimo-v2.5" in m:
            return 1_000_000
        # Qwen3.8 family uses a ~977K window.
        if "qwen3.8" in m:
            return 977_000
        # Qwen3.5 / Kimi K2.x use a 256K window.
        if "qwen3.5" in m or "kimi-k2" in m:
            return 262_000
        # DeepSeek V3.2 uses a 160K window.
        if "deepseek-v3" in m:
            return 160_000
        return 128_000
