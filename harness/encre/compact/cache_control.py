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

"""Cache-control breakpoint injection for provider prefix caching.

Mirrors Claude Code's ``CACHED_MICROCOMPACT`` pattern: by inserting
``cache_control`` markers at strategic positions in the message list,
the provider can reuse cached prefix computations across consecutive
requests, saving latency and cost.

Breakpoint placement
--------------------
1. **System prompt** -- ``{"type": "ephemeral"}`` so the provider knows
   the system prompt rarely changes and can cache it aggressively.
2. **First user message** (task anchor) -- marks the start of the
   per-session conversation.  Stable across turns.
3. **Compact boundary** -- after a compaction summary, the boundary
   message is a stable prefix for subsequent turn requests.
4. **Recent user message** (optional) -- the most recent non-tool user
   message, to mark where the active conversation segment begins.

Only injected for backends that support cache-control markers
(currently Anthropic Messages API via ``{"type": "ephemeral"}``).
"""

from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.compact.cache_control")

# Provider families that support cache-control markers.
_CACHE_CONTROL_PROVIDERS: frozenset[str] = frozenset({
    "anthropic",
    "google",  # Gemini context caching
})


def supports_cache_control(backend: Any) -> bool:
    """Return True if *backend* supports cache-control breakpoints.

    Checks a ``provider_family`` attribute on the backend instance.
    Falls back to the backend's class name if the attribute is absent.
    """
    family: str = getattr(backend, "provider_family", "") or type(backend).__name__
    return family.lower() in _CACHE_CONTROL_PROVIDERS


def inject_cache_breakpoints(
    messages: list[dict[str, Any]],
    *,
    backend: Any = None,
    provider_family: str = "",
    enabled: bool = True,
) -> list[dict[str, Any]]:
    """Inject ``cache_control`` breakpoints into *messages*.

    Parameters
    ----------
    messages
        The message list to modify.
    backend
        Backend instance (used to detect provider family when
        *provider_family* is not given).
    provider_family
        Explicit provider family string.  Takes precedence over
        *backend* when both are provided.
    enabled
        Master switch.  When ``False`` returns messages unchanged.

    Returns a **new** message list with breakpoints injected.
    Does not mutate the input.
    """
    if not enabled:
        return list(messages)

    family = provider_family or (
        getattr(backend, "provider_family", "") if backend else ""
    )
    if not family:
        return list(messages)
    if family.lower() not in _CACHE_CONTROL_PROVIDERS:
        return list(messages)

    result = list(messages)
    _set_ephemeral(result)
    _set_compacted_breakpoint(result)

    return result


def _set_ephemeral(messages: list[dict[str, Any]]) -> None:
    """Set ``cache_control: {"type": "ephemeral"}`` on the system prompt
    and the first user message (task anchor).
    """
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        if role == "system" and not _has_cache_control(msg):
            messages[i] = dict(msg)
            messages[i]["cache_control"] = {"type": "ephemeral"}
            break

    # First user message (task anchor) -- stable across turns.
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        if role == "user" and not msg.get("is_compact_summary"):
            if not _has_cache_control(msg):
                messages[i] = dict(msg)
                messages[i]["cache_control"] = {"type": "ephemeral"}
            break


def _set_compacted_breakpoint(messages: list[dict[str, Any]]) -> None:
    """Set ``cache_control`` on the compact boundary message so the
    post-compact prefix is cacheable.
    """
    for i, msg in enumerate(messages):
        if msg.get("is_compact_boundary") or msg.get("is_compact_summary"):
            if not _has_cache_control(msg):
                messages[i] = dict(msg)
                messages[i]["cache_control"] = {"type": "ephemeral"}
            break


def _has_cache_control(msg: dict[str, Any]) -> bool:
    return "cache_control" in msg and msg["cache_control"] is not None
