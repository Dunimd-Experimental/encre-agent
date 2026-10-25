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

"""Todo tool -- structured task list for the current session.

The list is the single source of truth for multi-step work: it is rendered on
the frontend as a plan card (via the PlanUpdate channel), persisted on the
session so it survives across turns, and returned in full on every call so
the model never has to reconstruct it from context.  Semantics are
replace-the-whole-list (each call submits the complete up-to-date list),
with stable per-item ids preserved across calls.

ISOLATION: each session owns its own list.  The active session is resolved
per call through the active-loop context variable, so the main agent, a
``workflow``/team teammate, and an ``agent`` sub-agent each see only their
own list -- a sub-agent's todo updates can never overwrite the parent's
plan, and sibling sub-agents run in their own sessions too.  Nothing is
merged back on completion: the parent's list is the delivery list.

Design notes:

* **Stable IDs.** Each item is assigned a ``todo-N`` id that is preserved
  across replace operations whenever the item's text has not changed.
  The id is stable *per session*; it is not globally unique and does not
  survive ``reset=True``.
* **Content-keyed diff.** When a caller submits a new list, the tool
  matches items by their ``content`` (after strip) against the previous
  list. Matches keep their previous id; new items get a fresh ``todo-N``.
  This lets the model submit the whole list without re-serialising ids.
* **Capped at 50.** The list is hard-capped at ``_MAX_TODOS = 50`` so a
  runaway caller cannot grow the payload to sizes that overflow the
  model's working context or the frontend plan card.
* **Warnings, not errors.** The tool does not fail when the caller
  submits more than one ``in_progress`` item or an all-completed list.
  Instead it appends a warning to the reply so the model can course-
  correct without a full round-trip.
* **Session-scoped fallback.** When no session is resolvable (headless
  or test mode), items live in :data:`_FALLBACK_STATE` keyed by session
  id or ``_headless`` for the missing case. This is real storage, not a
  mock — it just does not outlive the process.
"""

import json
from typing import Any

from encre.logging_config import get_logger
from encre.tools.base import build_tool

logger = get_logger(__name__)

_MAX_TODOS = 50
_STATUSES = ("pending", "in_progress", "completed")
_PRIORITIES = ("high", "medium", "low")

# In-memory fallback when no session is resolvable (headless/test mode).
# Real storage, not a mock; it simply does not outlive the process.  Keyed
# per session so concurrent runs never collide -- a plain module global
# would let one sub-agent overwrite another's list.
_FALLBACK_STATE: dict[str, list[dict[str, Any]]] = {}


def _storage_key(session: Any) -> str:
    """Identity key for a session's todo list (synthetic id when no session).

    The key is used to bucket the fallback dict so concurrent headless
    runs do not clobber each other's lists. It falls through three
    tiers of preference:

    1. ``None`` → the sentinel ``"_headless"`` bucket, used only when
       the caller has no active session at all.
    2. A session with a truthy ``id`` attribute → that id as a string.
    3. A session with no ``id`` (rare, e.g. a plain ``object()`` in
       unit tests) → ``str(id(session))`` — Python's object identity
       hash, which is unique per process.

    Args:
        session: The session object the tool is writing to, or ``None``.

    Returns:
        A string key suitable for use as a dict key.
    """
    if session is None:
        return "_headless"
    sid = str(getattr(session, "id", "") or "")
    return sid or str(id(session))


def _current_items(session: Any) -> list[dict[str, Any]]:
    """Read the session's current todo list.

    Prefers the session's own ``plan_items`` attribute when it is a
    list of dicts. Otherwise falls back to the in-memory
    :data:`_FALLBACK_STATE` bucket keyed by session id. The
    returned dicts are shallow copies so callers cannot accidentally
    mutate the stored list by holding a reference.

    Args:
        session: The session whose list to read, or ``None`` for the
            headless fallback bucket.

    Returns:
        A fresh list of shallow-copied item dicts, or an empty list
        when the session has never written one.
    """
    items = getattr(session, "plan_items", None)
    if isinstance(items, list):
        return [dict(i) for i in items if isinstance(i, dict)]
    return [dict(i) for i in _FALLBACK_STATE.get(_storage_key(session), [])]


def _store_items(session: Any, items: list[dict[str, Any]]) -> None:
    """Persist the todo list on the session, or in the fallback bucket.

    First attempts ``session.plan_items = items`` — this is the path
    that actually survives across turns and drives the frontend plan
    card update. If the session object does not accept attribute
    assignment (rare, e.g. a frozen dataclass in tests), the write
    falls through to :data:`_FALLBACK_STATE`.

    Args:
        session: The session to write to, or ``None`` for the
            headless fallback bucket.
        items: The canonical list of items to store. Must already be
            normalised by :func:`_normalize_items`.
    """
    if session is not None:
        try:
            session.plan_items = items
            return
        except Exception:  # pragma: no cover - session is a plain object
            logger.warning("[todo] failed to persist items on session; using memory fallback")
    _FALLBACK_STATE[_storage_key(session)] = [dict(i) for i in items]


def _next_id(pools: list[list[dict[str, Any]]]) -> str:
    """Next free ``todo-N`` id across the given item pools.

    Walks every pool of items (typically ``[existing, items]`` or
    ``[existing, items, temporary]``) and finds the maximum numeric
    suffix already used by an item whose id starts with ``todo-``.
    The returned id is that max + 1, formatted as ``todo-N``.

    Non-numeric suffixes are silently skipped — callers may pass in
    items with custom ids, and :func:`_next_id` must not crash on
    them.

    Args:
        pools: A list of lists of item dicts to scan.

    Returns:
        A string of the form ``"todo-N"`` where ``N`` is strictly
        greater than any previously-observed numeric suffix.
    """
    max_n = 0
    for pool in pools:
        for item in pool:
            iid = str(item.get("id", ""))
            if iid.startswith("todo-"):
                try:
                    max_n = max(max_n, int(iid[5:]))
                except ValueError:
                    continue
    return f"todo-{max_n + 1}"


def _normalize_items(
    raw: list[dict[str, Any]],
    existing: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Validate, assign stable ids, and diff against the previous list.

    The caller submits the whole list every time (replace semantics),
    so this function does three jobs:

    1. **Filter.** Items that are not dicts, have empty ``content``
       after strip, or have invalid ``status``/``priority`` values
       are dropped. Invalid enum values fall back to ``pending`` /
       ``medium`` rather than being rejected, so a single typo in one
       field does not nuke the entire plan.
    2. **Assign stable ids.** Each surviving item is matched against
       the previous list by its ``content`` string. Matches inherit
       the previous item's id; new items get a fresh ``todo-N`` via
       :func:`_next_id`, avoiding collision with any id that already
       appears in the previous list or in the items processed so far
       in this call.
    3. **Compute the diff.** The ``changes`` dict counts how many
       items were added, updated (status changed but content
       preserved), or removed (content no longer present in the new
       list). This is surfaced to the model so it can narrate what
       happened in the reply.

    Duplicate ``content`` strings in the incoming list collapse to a
    single item because the ``seen_contents`` set is used to detect
    repeats. Callers who submit the same content twice will only see
    one entry in the reply.

    Args:
        raw: The caller-supplied raw items list.
        existing: The current stored list (or ``[]`` when
            ``reset=True``).

    Returns:
        A 2-tuple ``(items, changes)`` where ``items`` is the
        canonical list of dicts to store and ``changes`` is a dict of
        ``added``, ``updated``, and ``removed`` counters.
    """
    by_content = {str(i.get("text", i.get("content", ""))): i for i in existing}
    used_ids = {str(i.get("id", "")) for i in existing}

    items: list[dict[str, Any]] = []
    changes = {"added": 0, "updated": 0, "removed": 0}
    seen_contents: set[str] = set()

    for raw_item in raw:
        if not isinstance(raw_item, dict):
            continue
        content = str(raw_item.get("content", "")).strip()
        if not content:
            continue
        status = str(raw_item.get("status", "pending"))
        if status not in _STATUSES:
            status = "pending"
        priority = str(raw_item.get("priority", "medium"))
        if priority not in _PRIORITIES:
            priority = "medium"

        prev = by_content.get(content)
        if prev is not None:
            iid = str(prev.get("id", "")) or _next_id([existing, items])
            if str(prev.get("status", "")) != status:
                changes["updated"] += 1
        else:
            iid = str(raw_item.get("id", "")).strip() or _next_id([existing, items])
            if iid in used_ids or any(i["id"] == iid for i in items):
                iid = _next_id([existing, items, [{"id": iid}]])
            changes["added"] += 1
        used_ids.add(iid)
        seen_contents.add(content)
        items.append({"id": iid, "text": content, "status": status, "priority": priority})

    # Items that vanished from the new list count as removed.
    changes["removed"] = sum(1 for i in existing if str(i.get("text", "")) not in seen_contents)
    return items, changes


async def _todo_execute(**kwargs: Any) -> str:
    """Replace the session's todo list and return a rendered markdown view.

    This is the callable that the Encre Agent tool-dispatch layer
    invokes once per tool call. The flow is:

    1. **Coerce inputs.** ``todos`` must be a list; anything else is
       treated as empty so a caller passing a JSON string by
       accident does not crash the tool. ``reset`` and ``summary``
       are read as their natural types with empty defaults.
    2. **Resolve the session.** The active session is looked up via
       :func:`encre.tools.runtime._resolve_loop`, which reads the
       active-loop context variable set by the harness. When there is
       no loop (headless test mode), ``session`` is ``None`` and the
       fallback dict is used.
    3. **Load existing items.** When ``reset=True`` the previous list
       is treated as empty; otherwise :func:`_current_items` is
       called.
    4. **Normalise.** :func:`_normalize_items` filters, assigns
       stable ids, and computes the diff.
    5. **Enforce the cap.** If the normalised list exceeds
       :data:`_MAX_TODOS` the tool returns an error JSON without
       mutating state.
    6. **Persist.** :func:`_store_items` writes the canonical list to
       the session (or the fallback bucket).
    7. **Render.** A human-readable markdown block is built first (so
       the model can quote it directly), then a machine-readable JSON
       block is appended on the last line so downstream callers can
       parse structured fields.

    Args:
        **kwargs: The caller-supplied tool input. Recognised keys:

            * ``todos`` (required, list): the complete up-to-date
              list of items. Each item is a dict with ``content``,
              ``status``, and ``priority`` fields.
            * ``reset`` (optional, bool): when true, treat the
              previous list as empty before normalising. Used to
              start a fresh plan unrelated to the previous one.
            * ``summary`` (optional, str): one-line summary shown in
              the card header.

    Returns:
        A markdown block containing the plan, followed by a JSON
        line with ``success``, ``items``, and ``changes``. When the
        list exceeds the cap, returns a JSON error object instead.

    Raises:
        Not caught. All validation failures are returned as structured
        strings so the model can reason about the outcome.
    """
    todos = kwargs.get("todos") or []
    if not isinstance(todos, list):
        todos = []
    reset = bool(kwargs.get("reset", False))
    summary = str(kwargs.get("summary", "")).strip()

    from encre.tools.runtime import _resolve_loop

    loop = _resolve_loop()
    session = getattr(loop, "session", None) if loop is not None else None

    existing = [] if reset else _current_items(session)
    items, changes = _normalize_items(todos, existing)
    if len(items) > _MAX_TODOS:
        return json.dumps({
            "success": False,
            "error": f"too many items: {len(items)} (max {_MAX_TODOS})",
        }, ensure_ascii=False)

    _store_items(session, items)

    in_progress = [i for i in items if i["status"] == "in_progress"]
    completed = sum(1 for i in items if i["status"] == "completed")
    warnings: list[str] = []
    if len(in_progress) > 1:
        warnings.append(
            f"{len(in_progress)} items are in_progress; keep exactly one active "
            "unless the tasks are truly parallel"
        )
    if items and completed == len(items):
        warnings.append("all items completed -- mark the work done in your reply")

    lines = [f"## Todo list -- {summary}"] if summary else ["## Todo list"]
    for item in items:
        icon = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
        lines.append(f"{icon} ({item['id']}) {item['text']} [{item['priority']}]")
    lines.append(
        f"\nProgress: {completed}/{len(items)} completed, {len(in_progress)} in progress"
    )
    for w in warnings:
        lines.append(f"Warning: {w}")
    lines.append(json.dumps({
        "success": True,
        "items": items,
        "changes": changes,
    }, ensure_ascii=False))
    return "\n".join(lines)


todo_tool = build_tool(
    name="todo",
    description=(
        "Maintain the session's structured task list. "
        "WHAT: replaces the whole list with the one you submit; every item gets a "
        "stable id; the list is shown live on the frontend plan card and returned "
        "in full so you never need to remember it. "
        "WHEN: use for complex multi-step work (3+ steps) or when the user asks to "
        "track tasks; update status the moment work happens, one step at a time. "
        "WHEN NOT: single trivial actions, pure Q&A, or tasks you finish in one "
        "tool call -- skip the list. "
        "TIPS: keep exactly one in_progress item (parallel work may have a few); "
        "mark completed immediately, never batch; submit the FULL updated list "
        "every time, reusing unchanged items as-is. "
        "PITFALLS: omitting items deletes them from the list (replacement "
        "semantics); duplicate contents collapse to one id; list is capped at 50."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Task text (required)."},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"],
                                   "description": "Item state (required)."},
                        "priority": {"type": "string", "enum": ["high", "medium", "low"],
                                     "description": "Importance (required; drives ordering hints on the card)."},
                    },
                    "required": ["content", "status", "priority"],
                },
                "description": "The complete, up-to-date list (replacement semantics: "
                               "missing items are removed).",
            },
            "summary": {
                "type": "string",
                "description": "One-line summary of what changed and why (shown in the card header).",
            },
            "reset": {
                "type": "boolean",
                "description": "Clear the previous list first (default false). Use true to start "
                               "a fresh list unrelated to the current one; with todos set it "
                               "replaces everything, without todos it empties the list.",
            },
        },
        "required": ["todos"],
    },
    execute=_todo_execute,
    intents=["general", "coding", "data", "research"],
)
