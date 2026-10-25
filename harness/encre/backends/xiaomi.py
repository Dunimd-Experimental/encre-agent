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
Xiaomi MiMo backend -- MiMo-V2-Flash, MiMo-V2.5, MiMo-V2.5-Pro (2026 lineup).

Xiaomi's MiMo series models offer open-source, high-performance LLMs with
blazing-fast inference speeds and strong coding capabilities.  The API is
OpenAI-compatible and supports reasoning tokens.

Models:
- MiMo-V2-Flash: 309B MoE, 150 tok/s, SWE-Bench 73.4%
- MiMo-V2-omni: Multimodal variant
- MiMo-V2-Pro: Enhanced reasoning
- MiMo-V2.5: Improved general performance
- MiMo-V2.5-Pro: Premium reasoning

Base URL: https://api.xiaomimimo.com/v1
           https://platform.xiaomimimo.com/v1 (alias)
Authentication: XIAOMI_API_KEY environment variable or explicit api_key.
"""

from typing import Any

from encre.backends.openai_sse import OpenAISSEBackend


class XiaomiBackend(OpenAISSEBackend):
    """Xiaomi MiMo backend for the MiMo model series.

    Supports MiMo-V2-Flash, MiMo-V2.5, and MiMo-V2.5-Pro via Xiaomi's
    OpenAI-compatible API.  Reasoning tokens are extracted from
    ``reasoning_content``.

    Xiaomi's MiMo models use the standard DeepSeek-style thinking
    envelope: ``{"thinking": {"type": "enabled"}}`` to enable thinking
    and ``{"thinking": {"type": "disabled"}}`` to disable it, optionally
    combined with a ``reasoning_effort`` (low/medium/high) to control
    reasoning depth.  ``mimo-v2.5-pro``, ``mimo-v2.5``, ``mimo-v2-pro``
    and ``mimo-v2-omni`` think by default; ``mimo-v2-flash`` has thinking
    off by default but can be toggled with the same parameter.

    For locally-served MiMo models (SGLang deployment) the parameter
    shape is slightly different (``chat_template_kwargs.enable_thinking``);
    that backend is not covered by this OpenAI-protocol wrapper.
    """

    DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "mimo-v2.5-pro",
        **kwargs: Any,
    ) -> None:
        if not base_url:
            # Fall back to Xiaomi MiMo's OpenAI-compatible endpoint default when no URL is supplied.
            base_url = self.DEFAULT_BASE_URL
        super().__init__(api_key=api_key, base_url=base_url, model=model, **kwargs)

    def _thinking_request_param(self) -> dict[str, Any] | None:
        """Return the provider-specific parameter that enables thinking.

        MiMo accepts the DeepSeek-style thinking envelope.  ``reasoning_effort``
        is NOT a documented chat-completions parameter (Responses API only),
        so it is deliberately not sent here.  MiMo also requires the full
        ``reasoning_content`` of assistant tool-call messages to be passed
        back in thinking mode, which the verbatim message pass-through
        already satisfies.
        """
        return {
            "thinking": {
                "type": "enabled" if self.thinking_enabled else "disabled",
            }
        }

    def context_window_size(self) -> int:
        m = self.model.lower()
        # MiMo V2.5 (Pro + base) expose a 1M context window.
        if "v2.5" in m or "v2-5" in m:
            return 1_000_000
        # MiMo V2 Omni/Flash expose a 256K (262144) context window.
        if "v2" in m:
            return 262_144
        # MiMo 7B legacy models expose a 32K context window.
        if "7b" in m:
            return 32_768
        return 262_144
