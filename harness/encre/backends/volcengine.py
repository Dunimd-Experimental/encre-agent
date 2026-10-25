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
Volcengine Ark (Volcano Ark) backend -- Doubao and partner models via Ark API.

This is a thin wrapper around ``OpenAICompatibleBackend``.  Volcengine Ark
exposes an OpenAI-compatible ``/chat/completions`` endpoint so no special
request formatting is needed.  The backend exists to provide a curated
catalog entry (base_url + model presets) so the frontend can auto-configure
the provider instead of requiring the user to type everything manually.

Two endpoint variants exist:

1. **Standard endpoint** (deployed model endpoints):
   ``https://ark.cn-beijing.volces.com/api/v3``
   Requires a deployed endpoint ID (``ep-xxxxxxxx``) as the model.

2. **Coding Plan (2026+)**:
   ``https://ark.cn-beijing.volces.com/api/coding/v3``
   Subscription-based; uses coding model names (e.g. ``ark-code-latest``).
"""

from typing import Any

import re

from encre.backends.openai_compatible import OpenAICompatibleBackend

# Ark models from doubao-seed-1.8 / 2.x onward keep historical
# ``reasoning_content`` in the request themselves; older models require the
# caller to strip it from the history.
_ARK_KEEP_REASONING_RE = re.compile(r"seed[-._]?1[.-]8|seed[-._]?2")


class VolcengineArkBackend(OpenAICompatibleBackend):
    """Volcengine Ark backend -- same as OpenAI compatible.

    The only reason this class exists (instead of just using
    ``OpenAICompatibleBackend`` directly) is so that the catalog knows
    about it and the frontend can auto-populate the correct base URL
    and model list.

    Doubao thinking-capable models (Doubao-1.5 / 1.6 / Seed-2.0 /
    Seed-1.6 thinking variants) accept the same DeepSeek-style
    ``{"thinking": {"type": "enabled"}}`` envelope.  ``reasoning_content``
    is emitted in the response and is extracted by the default
    :meth:`OpenAISSEBackend._extract_extra_stream_events` / non-stream
    helper via the multi-field ``_REASONING_FIELD_NAMES`` lookup.

    Note: Volcengine's responses API also accepts an alternative
    ``{"reasoning": {"effort": "low|medium|high"}}`` parameter, but we
    use the chat-completions-style ``thinking`` parameter here for
    consistency with the rest of the OpenAI-protocol providers.
    """

    DEFAULT_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/v3"

    def __init__(self, **kwargs: Any) -> None:
        # Ensure the Ark base URL is set unless the caller overrode it.
        kwargs.setdefault("base_url", self.DEFAULT_BASE_URL)
        super().__init__(**kwargs)

    def _thinking_request_param(self) -> dict[str, Any] | None:
        """Doubao / Seed thinking models use the DeepSeek-style envelope.

        Only reasoning-capable SKUs accept ``thinking.type``; omit it for
        standard chat models to avoid a 400.
        """
        if not self.model:
            return None
        m = self.model.lower()
        if any(k in m for k in ("doubao", "seed", "thinking", "reasoner")):
            return {"thinking": {"type": "enabled" if self.thinking_enabled else "disabled"}}
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
        """Build the request body, stripping history reasoning for old Ark models.

        Ark models BEFORE doubao-seed-1.8 require historical
        ``reasoning_content`` to be removed from the request; models from
        seed-1.8 / 2.x onward keep it.  The copy is shallow per message so
        the caller's message dicts are never mutated.
        """
        data = super()._build_request_data(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
        )
        if not _ARK_KEEP_REASONING_RE.search((self.model or "").lower()):
            cleaned: list[dict[str, Any]] = []
            for msg in data.get("messages", []):
                if (
                    isinstance(msg, dict)
                    and msg.get("role") == "assistant"
                    and "reasoning_content" in msg
                ):
                    msg = {k: v for k, v in msg.items() if k != "reasoning_content"}
                cleaned.append(msg)
            data["messages"] = cleaned
        return data
