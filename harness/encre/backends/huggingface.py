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
Hugging Face backend -- Inference Providers for open-source models.

Hugging Face provides access to thousands of open-source models through
Inference Providers, which expose an OpenAI-compatible chat completion
endpoint.

Base URL: https://router.huggingface.co/v1
Authentication: HF_TOKEN environment variable or explicit api_key.

Aliases: huggingface, hf
"""

from typing import Any

from encre.backends.openai_sse import OpenAISSEBackend


class HuggingFaceBackend(OpenAISSEBackend):
    """Hugging Face Inference Providers backend.

    Provides access to thousands of open-source models via Hugging Face's
    OpenAI-compatible router endpoint (router.huggingface.co).  The legacy
    ``api-inference.huggingface.co`` endpoint is deprecated upstream.
    """

    DEFAULT_BASE_URL = "https://router.huggingface.co/v1"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "meta-llama/Llama-4-Maverick-17B-Instruct",
        **kwargs: Any,
    ) -> None:
        """Initialize the Hugging Face Inference API backend.

        Args:
            api_key: Hugging Face access token. Falls back to the HF_TOKEN
                environment variable when empty.
            base_url: API endpoint; defaults to the HF Inference API URL.
            model: Default model repository id (e.g. a Llama variant).
            **kwargs: Additional options forwarded to the parent backend.
        """
        if not base_url:
            # No explicit endpoint given: use the HF Inference API URL.
            base_url = self.DEFAULT_BASE_URL
        super().__init__(api_key=api_key, base_url=base_url, model=model, **kwargs)

    def context_window_size(self) -> int:
        """Return the context window size (in tokens) for HF models."""
        m = self.model.lower()
        if "llama-4-scout" in m:
            return 1_310_720
        if "llama-4" in m or "qwen3.8" in m or "deepseek-v4" in m or "glm-5.3" in m:
            return 1_048_576
        # Older hosted Llama/Qwen/DeepSeek SKUs use a 128K window.
        return 128000
