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
Kilo Code backend -- unified API gateway for multi-model access.

Kilo Code's AI Gateway provides a unified API endpoint that routes requests
to many models (Anthropic, OpenAI, Google, etc.) through a single API key
and endpoint.

Base URL: https://api.kilo.ai/api/gateway
Authentication: KILOCODE_API_KEY environment variable or explicit api_key.
"""

from typing import Any

from encre.backends.openai_sse import OpenAISSEBackend


class KiloCodeBackend(OpenAISSEBackend):
    """Kilo Code Gateway backend for unified multi-model access.

    Routes requests through Kilo Code's AI Gateway, which supports various
    models from multiple providers through a single endpoint.
    """

    DEFAULT_BASE_URL = "https://api.kilo.ai/api/gateway"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "kilocode/kilo/auto",
        **kwargs: Any,
    ) -> None:
        """Initialize the Kilo Code Gateway backend.

        Args:
            api_key: Kilo Code API key. Falls back to the KILOCODE_API_KEY
                environment variable when empty.
            base_url: Gateway endpoint; defaults to the Kilo Code gateway.
            model: Default routed model identifier (supports auto-routing).
            **kwargs: Additional options forwarded to the parent backend.
        """
        if not base_url:
            # No explicit endpoint given: use the Kilo Code gateway URL.
            base_url = self.DEFAULT_BASE_URL
        super().__init__(api_key=api_key, base_url=base_url, model=model, **kwargs)

    def context_window_size(self) -> int:
        """Return the largest context window (tokens) the gateway supports."""
        # The gateway routes to many models; 1M covers the largest
        # (Claude/GPT-5.5/Gemini) so downstream compaction is conservative.
        return 1000000
