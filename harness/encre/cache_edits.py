#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (c) 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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


"""Cached microcompact -- delete old tool results via ``cache_edits``.

This mirrors Claude Code's ``cachedMicrocompact`` path (see
``services/compact/microCompact.ts`` and the cache-edits handling in
``services/api/claude.ts``).  The idea:

Normal microcompact **replaces** an old tool_result with a ``[cleared]``
stub.  That changes the message bytes, which busts the prompt-cache prefix
-- every subsequent turn re-sends the whole conversation (no cache discount).

``cache_edits`` is an Anthropic Messages-API content-block type that tells
the server to **evict specific cached tool_results by reference** without the
client rewriting the message content.  The prefix stays byte-identical, so
the cache hit is preserved and the deleted tokens stop counting against the
context window.

Lifecycle (mirrors Claude Code exactly):

1. **Register** -- before each API call, walk the messages and record every
   tool_result tool_use_id in the state.
2. **Decide what to delete** -- when the registered tool count exceeds the
   trigger threshold, pick the oldest tool_results beyond the keep-recent
   window.
3. **Build the block** -- ``{"type": "cache_edits", "edits": [{"type":
   "delete_tool_result", "tool_use_id": ...}]}``.
4. **Insert at the API layer** -- the block is spliced into the *last* user
   message, or re-inserted at its pinned position after compaction.
5. **Consume once** -- after the block is attached to an outgoing request it
   is marked sent so it is never attached twice.

NOTE: ``cache_edits`` is an Anthropic first-party capability.  It only takes
effect when Encre is pointed at the real Anthropic Messages API (``backend_type
= "anthropic"``).  Other backends (OpenAI-compatible / DeepSeek / Ollama ...)
silently ignore the block, so this is a no-op there -- cached microcompact
simply doesn't run on non-Anthropic backends.
"""


from dataclasses import dataclass, field
from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.cache_edits")

# Beta header Anthropic requires for cache editing.  Mirrors Claude Code's
# cache-editing header latch (services/api/claude.ts:1431).  Latch-on so the
# header stays for the rest of the session once enabled, matching Anthropic's
# requirement that cache_control / cache_edits betas not be toggled mid-stream
# (toggling busts the cache prefix).
CACHE_EDITING_BETA_HEADER = "context-management-2025-06-27"

# Default keep-recent window: never delete a tool_result that is among the most
# recent N registered ones.  Mirrors Claude Code's ``keepRecent`` config (the
# GrowthBook ``tengu_hawthorn_cached_mc_keep_recent`` default).
DEFAULT_KEEP_RECENT = 6

# Default trigger threshold: don't bother deleting until at least this many
# tool_results are registered.  Matches Claude Code's triggerThreshold.
DEFAULT_TRIGGER_THRESHOLD = 8


@dataclass
class CacheEditsBlock:
    """A serializable ``cache_edits`` content block."""

    edits: list[dict[str, str]] = field(default_factory=list)

    def to_block(self) -> dict[str, Any]:
        return {"type": "cache_edits", "edits": self.edits}


@dataclass
class PinnedEdits:
    """A cache_edits block pinned at a fixed user-message position."""

    user_message_index: int
    block: CacheEditsBlock


@dataclass
class CacheEditsState:
    """Mutable bookkeeping for the cache-editing lifecycle."""

    registered: list[str] = field(default_factory=list)
    tool_order: list[str] = field(default_factory=list)
    sent_to_api: set[str] = field(default_factory=set)
    deleted_refs: set[str] = field(default_factory=set)
    pending: CacheEditsBlock | None = None
    pinned: list[PinnedEdits] = field(default_factory=list)
    keep_recent: int = DEFAULT_KEEP_RECENT
    trigger_threshold: int = DEFAULT_TRIGGER_THRESHOLD


def create_state(
    keep_recent: int = DEFAULT_KEEP_RECENT,
    trigger_threshold: int = DEFAULT_TRIGGER_THRESHOLD,
) -> CacheEditsState:
    """Create a fresh :class:`CacheEditsState` with the given tuning knobs."""
    return CacheEditsState(keep_recent=keep_recent, trigger_threshold=trigger_threshold)


def _collect_tool_use_ids(messages: list[dict[str, Any]]) -> list[str]:
    """Return every ``tool_use`` id referenced in the message list."""
    ids: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id")
            ):
                ids.append(str(block["id"]))
    return ids


def register_tool_results(
    state: CacheEditsState, messages: list[dict[str, Any]]
) -> None:
    """Record newly-seen tool_use ids and append them to the insertion order."""
    for tool_use_id in _collect_tool_use_ids(messages):
        if tool_use_id in state.registered:
            continue
        state.registered.append(tool_use_id)
        state.tool_order.append(tool_use_id)


def mark_sent_to_api(state: CacheEditsState) -> None:
    """Mark the pending block as consumed so it is never re-attached."""
    if state.pending is None:
        return
    for edit in state.pending.edits:
        ref = edit.get("tool_use_id")
        if ref:
            state.sent_to_api.add(ref)
    state.pending = None


def get_tool_results_to_delete(state: CacheEditsState) -> list[str]:
    """Compute the list of tool_use ids eligible for eviction.

    Candidates are registered results older than the keep-recent window that
    have already been sent to the API and not yet deleted.
    """
    if len(state.tool_order) < state.trigger_threshold:
        return []
    # The most recent keep_recent are preserved; everything older is a
    # candidate.  tool_order is insertion order, so the head is oldest.
    candidates = state.tool_order[:-state.keep_recent]
    result = []
    for tid in candidates:
        if tid in state.sent_to_api and tid not in state.deleted_refs:
            result.append(tid)
    return result


def create_cache_edits_block(
    state: CacheEditsState, tool_use_ids: list[str]
) -> CacheEditsBlock | None:
    """Build a cache_edits block deleting the given tool_use ids."""
    if not tool_use_ids:
        return None
    edits = [
        {"type": "delete_tool_result", "tool_use_id": tid} for tid in tool_use_ids
    ]
    return CacheEditsBlock(edits=edits)


def consume_pending(state: CacheEditsState) -> CacheEditsBlock | None:
    """Return the pending block, mark it sent, and clear the slot."""
    block = state.pending
    mark_sent_to_api(state)
    return block


def get_pinned(state: CacheEditsState) -> list[PinnedEdits]:
    """Return the list of pinned edits blocks."""
    return state.pinned


def pin_edits(
    state: CacheEditsState, user_message_index: int, block: CacheEditsBlock
) -> None:
    """Record a cache_edits block to re-insert at a fixed message position."""
    state.pinned.append(PinnedEdits(user_message_index=user_message_index, block=block))


def deduplicate(block: CacheEditsBlock, seen: set[str]) -> CacheEditsBlock:
    """Return a copy of *block* dropping edits whose refs are already in *seen*."""
    deduped = CacheEditsBlock()
    for edit in block.edits:
        ref = edit.get("tool_use_id")
        if ref in seen:
            continue
        seen.add(ref)
        deduped.edits.append(edit)
    return deduped


def reset(state: CacheEditsState) -> None:
    """Reset the state to a blank slate (e.g. on session switch)."""
    state.registered.clear()
    state.tool_order.clear()
    state.sent_to_api.clear()
    state.deleted_refs.clear()
    state.pending = None
    state.pinned.clear()


# -----------------------------------------------------------------------------
# API-layer insertion (mirrors addCacheBreakpoints in claude.ts)
# -----------------------------------------------------------------------------


def attach_cache_edits(
    messages: list[dict[str, Any]], state: CacheEditsState
) -> list[dict[str, Any]]:
    """Splice pending / pinned cache_edits blocks into the outgoing messages.

    Returns a shallow copy of the message list with the blocks inserted into
    the appropriate user messages.  The caller's list is never mutated.
    """
    pending = consume_pending(state)
    pinned = state.pinned

    if not pending and not pinned:
        return messages

    # Shallow-copy the list and each message dict so we never mutate caller state.
    result = [dict(m) for m in messages]

    # Re-insert pinned blocks at their recorded positions.
    for p in pinned:
        if 0 <= p.user_message_index < len(result):
            msg = result[p.user_message_index]
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            deduped = deduplicate(p.block, set(state.deleted_refs))
            if deduped.edits:
                msg["content"] = content + [deduped.to_block()]

    # Insert the pending block into the last user message.
    if pending is not None:
        deduped = deduplicate(pending, set(state.deleted_refs))
        if deduped.edits:
            for i in range(len(result) - 1, -1, -1):
                msg = result[i]
                if msg.get("role") == "user":
                    content = msg.get("content")
                    if not isinstance(content, list):
                        content = []
                        msg["content"] = content
                    content.append(deduped.to_block())
                    break

    return result


def _insert_after_tool_results(
    content: list[dict[str, Any]], block: CacheEditsBlock
) -> list[dict[str, Any]]:
    """Insert a cache_edits block right after the last tool_result block."""
    new_content = list(content)
    for j in range(len(new_content) - 1, -1, -1):
        if new_content[j].get("type") == "tool_result":
            new_content.insert(j + 1, block.to_block())
            return new_content
    new_content.append(block.to_block())
    return new_content
