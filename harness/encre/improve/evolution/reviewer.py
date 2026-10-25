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


import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable



from encre.logging_config import get_logger


logger = get_logger(__name__)

@dataclass
class ReviewSuggestion:
    """A concrete improvement suggested by the background reviewer."""

    kind: str
    description: str
    reference: str = ""


class BackgroundReviewer:
    """Periodically reviews recent conversation turns in the background.

    Every ``review_interval`` turns, if enabled, a snapshot of the last few
    messages is formatted and fed to an LLM-backed review routine.  Parsed
    suggestions are delivered through ``suggestions_callback``.
    """

    def __init__(
        self,
        review_fn: Callable[[str], str] | None = None,
        suggestions_callback: Callable[[list[ReviewSuggestion]], None] | None = None,
        review_interval: int = 10,
        enabled: bool = True,
        recent_turn_count: int = 8,
    ) -> None:
        self.review_fn = review_fn
        self.suggestions_callback = suggestions_callback
        self.review_interval = review_interval
        self.enabled = enabled
        self.recent_turn_count = recent_turn_count
        self._turn_count = 0
        self._review_task: asyncio.Task | None = None
        self._messages: list[dict[str, Any]] = []

    async def on_turn_end(self, loop: Any) -> None:
        """Turn-end hook invoked by the loop (fire-and-forget).

        Extracts the session messages from the loop object and defers to
        :meth:`maybe_review`.  Tolerates loops without a session.
        """
        session = getattr(loop, "session", None)
        messages = getattr(session, "messages", None)
        if not isinstance(messages, list):
            return
        await self.maybe_review(messages)

    async def maybe_review(self, messages: list[dict[str, Any]]) -> None:
        """Kick off a background review when the turn interval is reached."""
        self._messages = messages
        self._turn_count += 1
        if not self.enabled:
            return
        if self._turn_count % self.review_interval != 0:
            return
        if self._review_task is not None and not self._review_task.done():
            return
        self._review_task = asyncio.create_task(self._run_review(messages))

    async def _run_review(self, messages: list[dict[str, Any]]) -> None:
        try:
            recent = _format_recent_turns(messages)
            if not recent:
                return
            if self.review_fn is None:
                return
            text = await asyncio.to_thread(self.review_fn, recent)
            suggestions = _parse_suggestions(text)
            if self.suggestions_callback:
                await asyncio.to_thread(self.suggestions_callback, suggestions)
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
def _format_recent_turns(messages: list[dict[str, Any]]) -> str:
    """Render the last N messages as a compact reviewable transcript."""
    lines = []
    for m in messages[-40:]:
        role = m.get("role", "unknown")
        content = m.get("content", "")
        if len(content) > 500:
            content = content[:500] + "..."
        if tool_calls := m.get("tool_calls"):
            content += f" [tool_calls: {len(tool_calls)}]"
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _parse_suggestions(text: str) -> list[ReviewSuggestion]:
    """Parse plain-text review output into structured suggestions."""
    suggestions = []
    for line in text.split("\n"):
        if not line:
            continue
        for kind, label in [("memory", "MEMORY"), ("skill", "SKILL")]:
            if f"[{label}]" in line.upper():
                suggestions.append(
                    ReviewSuggestion(kind=kind, description=line.strip())
                )
                break
    return suggestions
