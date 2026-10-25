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

"""Stop hook retry: hooks can block and retry the agent loop.

Mirrors Claude Code's handleStopHooks() in query.ts.  When a stop hook
returns a blocking result, the hook output is injected as a user message
and the conversation continues instead of stopping.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Maximum number of consecutive stop hook blocks before forcing exit
MAX_STOP_HOOK_BLOCKS = 5


async def execute_stop_hooks(
    hook_system: Any,
    response_text: str,
    tool_count: int,
    turn_count: int,
) -> list[dict[str, Any]]:
    """Execute stop hooks and return blocking messages if any.

    Returns:
        A list of blocking messages to inject into the conversation.
        Empty list means no blocking — the turn can end normally.
    """
    if hook_system is None:
        return []

    try:
        results = await hook_system.emit_stop(
            response_text=response_text,
            tool_count=tool_count,
            turn_count=turn_count,
        )
    except Exception as e:
        logger.warning("[stop_hook] emit_stop failed: %s", e)
        return []

    if not results:
        return []

    blocking: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("action") == "block":
            message = result.get("message", "")
            if message:
                blocking.append(
                    {
                        "role": "user",
                        "content": f"[Hook output]\n{message}",
                    }
                )
                logger.info(
                    "[stop_hook] blocking — injecting hook output turn=%d",
                    turn_count,
                )

    return blocking


def should_block_stop(
    hook_results: list[dict[str, Any]],
    block_count: int,
    *,
    max_blocks: int = MAX_STOP_HOOK_BLOCKS,
) -> bool:
    """Return True if the stop should be blocked by hooks.

    *block_count* is incremented each time a hook blocks.  If it exceeds
    *max_blocks*, the block is overridden to prevent infinite loops.
    """
    if not hook_results:
        return False
    if block_count >= max_blocks:
        logger.warning(
            "[stop_hook] max blocks (%d) exceeded — forcing exit", max_blocks
        )
        return False
    return any(
        isinstance(r, dict) and r.get("action") == "block"
        for r in hook_results
    )
