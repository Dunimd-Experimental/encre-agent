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

"""Context collapse layer: projected views over full conversation history.

Mirrors Claude Code's ``src/services/contextCollapse/`` module.  The full
history is preserved, but the model sees a collapsed view where old
interactions are replaced by summaries.  Summaries are committed at write
boundaries (tool results, file modifications) and drained on PTL errors.

Key concepts:
- **Projected view**: The model sees summaries, not raw history
- **Write boundaries**: Summaries are committed after tool results,
  file writes, and other state-changing events
- **Drain on overflow**: When a 413 error occurs, pending collapses
  are drained from the staged pipeline
"""

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Default collapse chunk size: how many messages to summarize at once
COLLAPSE_CHUNK_SIZE = 10
# Maximum number of staged collapses before draining
MAX_STAGED_COLLAPSES = 3
# Minimum number of messages before collapse is considered
MIN_MESSAGES_FOR_COLLAPSE = 5


@dataclass
class CollapseChunk:
    """A chunk of messages that should be collapsed into a summary."""

    start_index: int
    end_index: int
    summary: str = ""
    committed: bool = False


@dataclass
class ContextCollapseState:
    """Tracks the collapse pipeline state for a session."""

    chunks: list[CollapseChunk] = field(default_factory=list)
    collapsed_count: int = 0
    last_collapse_index: int = 0

    @property
    def has_pending(self) -> bool:
        return any(not c.committed for c in self.chunks)

    @property
    def pending_count(self) -> int:
        return sum(1 for c in self.chunks if not c.committed)


def compute_collapse_boundaries(
    messages: list[dict[str, Any]],
    last_collapse_index: int = 0,
    chunk_size: int = COLLAPSE_CHUNK_SIZE,
) -> list[CollapseChunk]:
    """Compute chunks of messages that should be collapsed.

    Boundaries are placed at write events (tool results, file writes) so
    that summaries capture complete units of work.

    Returns chunks starting from *last_collapse_index*.
    """
    if len(messages) - last_collapse_index < MIN_MESSAGES_FOR_COLLAPSE:
        return []

    chunks: list[CollapseChunk] = []
    start = max(last_collapse_index, 0)
    current_start = start

    for i in range(start, len(messages)):
        msg = messages[i]
        role = msg.get("role", "")
        is_write_boundary = False

        if role == "tool":
            # Tool results are write boundaries
            is_write_boundary = True
        elif role == "assistant" and msg.get("tool_calls"):
            # Tool calls precede results — boundary at the call
            is_write_boundary = True

        if is_write_boundary and i - current_start >= MIN_MESSAGES_FOR_COLLAPSE:
            chunks.append(
                CollapseChunk(start_index=current_start, end_index=i)
            )
            current_start = i + 1

        # Also chunk if we've accumulated enough messages
        if i - current_start >= chunk_size and current_start > start:
            chunks.append(
                CollapseChunk(start_index=current_start, end_index=i)
            )
            current_start = i + 1

    # Remaining messages form final chunk
    remaining = len(messages) - current_start
    if remaining >= MIN_MESSAGES_FOR_COLLAPSE and current_start > start:
        chunks.append(
            CollapseChunk(start_index=current_start, end_index=len(messages))
        )

    return chunks


def build_collapsed_messages(
    messages: list[dict[str, Any]],
    chunks: list[CollapseChunk],
    system_prompt: str = "",
) -> list[dict[str, Any]]:
    """Build a collapsed view of messages by replacing chunks with summaries.

    Only committed chunks are replaced.  Uncommitted chunks pass through
    as-is (for now — they will be committed on the next collapse cycle).
    """
    if not chunks:
        return list(messages)

    collapsed: list[dict[str, Any]] = []
    last_end = 0

    for chunk in chunks:
        if not chunk.committed:
            continue

        # Add messages before this chunk
        collapsed.extend(messages[last_end : chunk.start_index])

        # Add summary as a system message
        collapsed.append(
            {
                "role": "system",
                "content": f"[Previous conversation summary]\n{chunk.summary}",
            }
        )

        last_end = chunk.end_index

    # Add remaining messages after the last chunk
    collapsed.extend(messages[last_end:])

    return collapsed


def should_drain_collapses(state: ContextCollapseState) -> bool:
    """Return True if pending collapses should be drained (e.g. on 413)."""
    return state.has_pending and state.pending_count >= MAX_STAGED_COLLAPSES


# How many of the most-recent tool results to keep verbatim.  Older tool
# results beyond this window are collapsed to one-line stubs.
COLLAPSE_KEEP_RECENT_TOOL_RESULTS = 8
# Minimum tool-result length before it's worth collapsing -- short results
# are already cheap to keep.
COLLAPSE_MIN_RESULT_CHARS = 200


def collapse_old_tool_outputs(
    messages: list[dict[str, Any]],
    keep_recent: int = COLLAPSE_KEEP_RECENT_TOOL_RESULTS,
    min_chars: int = COLLAPSE_MIN_RESULT_CHARS,
) -> list[dict[str, Any]]:
    """Collapse old tool-result messages into one-line stubs.

    A deterministic, LLM-free collapse pass: the most recent *keep_recent*
    tool results are preserved verbatim, and every older tool result longer
    than *min_chars* is replaced by a privacy-safe one-line stub that only
    records the tool name and total length (no call id, no preview text).
    Assistant messages with ``tool_calls`` are preserved (the stubs still
    reference the pair id so the API's tool-call/tool-result pairing stays
    valid).

    This is the cheap first stage of the collapse pipeline; the expensive
    model-driven summarisation in :class:`encre.compact.engine.CompactEngine`
    runs afterward only if pressure is still high.
    """
    if not messages:
        return list(messages)

    # Collect indices of tool messages, newest last.
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= keep_recent:
        return list(messages)

    # Indices of tool results that are "old" (beyond the recent window).
    old_tool_idx = set(tool_indices[:-keep_recent]) if keep_recent > 0 else set(tool_indices)

    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        if i in old_tool_idx and m.get("role") == "tool":
            content = m.get("content", "")
            if isinstance(content, str) and len(content) > min_chars:
                stub = (
                    f"[collapsed tool result: {m.get('name', 'tool')} "
                    f"({len(content)} chars)]"
                )
                collapsed = dict(m)
                collapsed["content"] = stub
                collapsed["_collapsed"] = True
                out.append(collapsed)
                continue
        out.append(dict(m))
    return out


def count_collapsed(messages: list[dict[str, Any]]) -> int:
    """Return how many messages in *messages* were collapsed to stubs."""
    return sum(1 for m in messages if m.get("_collapsed"))


async def drain_collapses(
    state: ContextCollapseState,
    backend: Any,
    messages: list[dict[str, Any]],
    system_prompt: str = "",
) -> list[dict[str, Any]]:
    """Drain all pending collapses by summarizing them urgently.

    Called when the model reports PTL (413) — commit all pending
    chunks to free up context space.
    """
    from encre.compact.engine import COMPACT_MAX_OUTPUT_TOKENS

    for chunk in state.chunks:
        if chunk.committed:
            continue
        try:
            chunk_messages = messages[chunk.start_index : chunk.end_index]
            if not chunk_messages:
                continue

            # Use a lightweight summary call
            summary = await _summarize_chunk(
                chunk_messages, backend, system_prompt
            )
            if summary:
                chunk.summary = summary
                chunk.committed = True
                state.collapsed_count += 1
                logger.info(
                    "[collapse] drained chunk %d-%d (%d msgs) turn=%d",
                    chunk.start_index, chunk.end_index,
                    len(chunk_messages),
                    state.collapsed_count,
                )
        except Exception as e:
            logger.warning("[collapse] failed to drain chunk: %s", e)

    return build_collapsed_messages(messages, state.chunks, system_prompt)


async def _summarize_chunk(
    messages: list[dict[str, Any]],
    backend: Any,
    system_prompt: str = "",
) -> str:
    """Summarize a chunk of messages into a brief summary."""
    from encre.compact.engine import COMPACT_MAX_OUTPUT_TOKENS

    summary_prompt = [
        {"role": "system", "content": "Summarize the following conversation chunk concisely. Include key decisions, tool calls, and outcomes. Be brief."},
        {"role": "user", "content": f"Summarize this conversation:\n\n{_format_messages(messages)}"},
    ]

    parts: list[str] = []
    try:
        async for event in backend.chat(
            messages=summary_prompt,
            max_tokens=COMPACT_MAX_OUTPUT_TOKENS,
            enable_caching=False,
        ):
            from encre.utils.types import BackendText, BackendFinish
            if isinstance(event, BackendText):
                parts.append(event.text)
            elif isinstance(event, BackendFinish):
                break
    except Exception:
        # Fallback: plain text summary
        return _plain_summary(messages)

    return "".join(parts).strip() or _plain_summary(messages)


def _plain_summary(messages: list[dict[str, Any]]) -> str:
    """Fallback: generate a plain text summary from message roles."""
    roles = [m.get("role", "?") for m in messages]
    user_count = roles.count("user")
    assistant_count = roles.count("assistant")
    tool_count = roles.count("tool")
    return (
        f"[{len(messages)} messages: "
        f"{user_count} user, {assistant_count} assistant, "
        f"{tool_count} tool results]"
    )


def _format_messages(messages: list[dict[str, Any]]) -> str:
    """Format messages for summarization."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, str):
            content = content[:500]
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)
