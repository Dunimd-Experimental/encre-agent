#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

"""Periodic milestone summarization.

Produces a compact "what happened since the last milestone" note from the
recent assistant/tool turns without calling an LLM, so the working-set
manager can persist progress landmarks cheaply and deterministically.
"""

from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.compact.milestone")

# How many recent messages feed the summary.
_MILESTONE_LOOKBACK = 40
# Cap on each captured tool/assistant line.
_LINE_CAP = 300


async def summarize_milestone(
    messages: list[dict[str, Any]],
    backend: Any = None,
    compact_engine: Any = None,
) -> dict[str, str]:
    """Summarize the recent activity for a milestone note.

    Args:
        messages: Recent context messages to condense.
        backend: Unused (kept for call-site compatibility).
        compact_engine: Unused (kept for call-site compatibility).

    Returns ``{"outcome": "...", "detail": "..."}``.  ``outcome`` is a short
    status label ("in progress", "completed", ...) derived from the last
    message; ``detail`` is a condensed list of the notable assistant texts
    and tool names/outcomes.
    """
    if not messages:
        return {"outcome": "in progress", "detail": "no activity yet"}

    recent = messages[-_MILESTONE_LOOKBACK:]
    events: list[str] = []
    last_assistant_text = ""

    for msg in recent:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "assistant":
            if isinstance(content, str) and content.strip():
                text = content.strip().replace("\n", " ")[:_LINE_CAP]
                last_assistant_text = text
                events.append(f"assistant: {text}")
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    name = tc.get("function", {}).get("name", "") if isinstance(tc.get("function"), dict) else ""
                    args = tc.get("function", {}).get("arguments", "") if isinstance(tc.get("function"), dict) else ""
                    if name:
                        events.append(f"called {name}({args[:120]})")
        elif role == "tool":
            tc_id = msg.get("tool_call_id", "")
            if isinstance(content, str) and content.strip():
                outcome = "ok" if "error" not in content.lower()[:200] else "error"
                events.append(f"tool result {tc_id}: {outcome} ({content[:80].strip()})")

    if not events:
        return {"outcome": "in progress", "detail": last_assistant_text[:_LINE_CAP]}

    # Outcome: "completed" when the last assistant message reads like a wrap-up.
    detail = "\n".join(events[-12:])[:_LINE_CAP * 12]
    outcome = "in progress"
    tail = (last_assistant_text or "").lower()
    if any(k in tail for k in ("done", "complete", "finished", "final", "delivered", "success", "成功", "完成")):
        outcome = "completed"
    elif any(k in tail for k in ("fail", "error", "stuck", "failure", "失败", "错误")):
        outcome = "needs attention"
    return {"outcome": outcome, "detail": detail}
