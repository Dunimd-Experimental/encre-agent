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

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CacheContext:
    """Context for sharing prompt cache between parent and sub-agent.

    When a sub-agent is forked, the parent can pass its CacheContext so the
    sub-agent's system prompt begins with the *same prefix bytes* as the
    parent's cached context.  This allows Anthropic's prompt caching (and
    similar backend-level caching) to return a cache hit for the shared
    prefix instead of re-encoding it.

    The ``prefix_text`` is the exact text that appeared at the start of the
    parent's system prompt.  On API backends that support prompt caching
    (e.g. Anthropic with ``enable_prompt_caching=True``), the first N bytes
    are cached after the initial API call; sharing the same bytes on a
    sub-agent call means the cache is still warm.
    """

    prefix_text: str = ""
    parent_session_id: str = ""
    prefix_hash: str = ""

    def __post_init__(self) -> None:
        if not self.prefix_hash and self.prefix_text:
            self.prefix_hash = hashlib.sha256(self.prefix_text.encode()).hexdigest()[:16]

    def is_empty(self) -> bool:
        return not self.prefix_text

    @classmethod
    def from_parent_context(cls, parent_system_prompt: str, parent_session_id: str = "") -> "CacheContext":
        prefix = _extract_cacheable_prefix(parent_system_prompt)
        return cls(prefix_text=prefix, parent_session_id=parent_session_id)

    def wrap_prompt(self, sub_agent_prompt: str) -> str:
        if not self.prefix_text:
            return sub_agent_prompt
        return (
            f"[SHARED CACHED CONTEXT FROM PARENT]\n"
            f"{self.prefix_text}\n"
            f"[/SHARED CACHED CONTEXT]\n\n"
            f"[SUB-AGENT INSTRUCTIONS]\n"
            f"{sub_agent_prompt}"
        )


def _extract_cacheable_prefix(system_prompt: str | None, max_chars: int = 4096) -> str:
    """Extract the cacheable prefix from a system prompt.

    Takes the first ``max_chars`` bytes — the portion most likely to be
    cached by backend prompt caching systems.  For Anthropic this is the
    system message + initial tool definitions.

    Returns an empty string when ``system_prompt`` is ``None`` or not a
    string — the parent loop's ``_sys_prompt_cache`` is ``None`` until the
    prompt phase has populated it, and spawning a sub-agent before that
    (or from a loop that never built a prompt) must not crash.
    """
    if not system_prompt:
        return ""
    return system_prompt[:max_chars]
