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

"""Tool output budget management -- per-result and aggregate per-turn caps.

Mirrors Claude Code's ``toolLimits.ts``:

1. **Per-result cap**: each tool result is truncated if it exceeds the
   tool's ``max_result_size_chars`` (default 20‑000 chars).

2. **Aggregate per-turn cap**: the total size of all tool results in a
   single turn is bounded (default 500‑000 chars).  When exceeded, older
   results are truncated first so the most recent information survives.

The pipeline calls :func:`apply_tool_result_budget` **before** the API
request is built -- not at save time -- so the model never sees bloated
tool outputs that waste context.
"""

from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.tool_output_store")

# Default per-result cap (chars).  This is tighter than the old
# 100_000-char default so the model sees more of the conversation in
# the same window.
DEFAULT_MAX_TOOL_RESULT_CHARS = 20_000

# Aggregate per-turn cap (chars).  When a single turn produces many
# large tool results, we truncate the oldest ones to keep the total
# within budget.
DEFAULT_AGGREGATE_TOOL_BUDGET_PER_TURN = 500_000


def apply_tool_result_budget(
    messages: list[dict[str, Any]],
    *,
    max_per_result: int = DEFAULT_MAX_TOOL_RESULT_CHARS,
    max_aggregate: int = DEFAULT_AGGREGATE_TOOL_BUDGET_PER_TURN,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    """Apply per-result and aggregate tool output budgets.

    *max_per_result* caps each individual tool result.
    *max_aggregate* caps the total tool-result size in one contiguous
    block of tool messages.

    Returns a **new** message list.  Does not mutate the input.

    Stages
    ------
    1. Cap each tool result to *max_per_result* chars.
    2. If the aggregate of all tool results in this turn still exceeds
       *max_aggregate*, truncate the **oldest** results further until
       under budget, preserving the most recent outputs.
    """
    if not enabled or not messages:
        return list(messages)

    # ── Stage 1: per-result cap ──────────────────────────────────────
    capped: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > max_per_result:
                excess = len(content) - max_per_result
                content = content[:max_per_result]
                content += f"\n... (truncated {excess} chars by per-result budget)"
                msg = dict(msg)
                msg["content"] = content
        capped.append(msg)

    # ── Stage 2: aggregate per-turn cap ──────────────────────────────
    # Find contiguous tool-message blocks (one per assistant turn).
    result: list[dict[str, Any]] = list(capped)
    i = 0
    while i < len(result):
        if result[i].get("role") == "tool":
            # Find the end of this tool block.
            j = i
            while j < len(result) and result[j].get("role") == "tool":
                j += 1
            result = _apply_aggregate_budget(result, i, j, max_aggregate)
            i = j
        else:
            i += 1

    return result


def _apply_aggregate_budget(
    messages: list[dict[str, Any]],
    start: int,
    end: int,
    max_aggregate: int,
) -> list[dict[str, Any]]:
    """Truncate oldest tool results in *messages[start:end]* to fit
    *max_aggregate*.
    """
    total = sum(
        len(str(m.get("content", "")))
        for m in messages[start:end]
    )
    if total <= max_aggregate:
        return messages

    # Walk from oldest to newest, truncating each until under budget.
    result = list(messages)
    excess = total - max_aggregate
    for idx in range(start, end):
        if excess <= 0:
            break
        msg = result[idx]
        content = str(msg.get("content", ""))
        if len(content) <= 500:
            continue  # too short to bother
        trim = min(excess + 200, len(content) - 500)
        if trim <= 0:
            continue
        new_content = content[: len(content) - trim]
        new_content += f"\n... (truncated {trim} chars by aggregate budget)"
        result[idx] = dict(msg)
        result[idx]["content"] = new_content
        excess -= trim

    return result
