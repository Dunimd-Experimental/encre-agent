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

"""Session -> renderer-message projection, shared by every transport.

The conversation a *client* may see is not the conversation the *model*
sees. Model-facing compaction blocks (the summary that replaces the old
turns, the active archive, the archive hint, the workspace context) exist
only to keep the model inside its token budget; showing them to a user
would leak the compressed context back into the UI.

This module owns that projection so the WebSocket handler, the history
endpoints, the headless JSONL transport, and every protocol handler mixin
all agree on it. It is called both directly and through the thin delegating
wrapper
:meth:`encre.protocol.handlers.base.BaseHandlers._renderer_session_messages`,
which keeps the legacy ``self.`` call sites working without a second copy of
the projection logic.

Layer position: this sits at the very end of the response path. The Rust
core produces events, :mod:`encre.protocol.session_router` delivers them,
and this module decides what a renderer is allowed to store or display. It
is pure Python with no async, no I/O, and no session mutation, so it is safe
to call synchronously from inside a websocket handler while the event loop
is otherwise busy.

Public API:
     :func:`renderer_session_messages` -- project a session to visible history.
"""

from typing import Any


def renderer_session_messages(
    session: Any,
    start: int = 0,
    stop: int | None = None,
) -> list[dict[str, Any]]:
    """Return the session's visible history for a renderer.

    The single decision point for "may the UI see this message?". Five
    classes of message are handled, in this order:

    - Role-``system`` messages are dropped entirely. They carry the model's
      system prompt, tool descriptions, and injected context -- none of it
      is user content, and serialising it into a history payload would both
      leak internals and bloat every history response.
    - Synthetic ``user`` messages (``is_synthetic``) are dropped entirely.
      They are loop plumbing -- context updates, retry nudges, grace
      prompts -- written with role ``user`` only because providers have no
      mid-conversation system role. Rendering them as user input would
      misattribute machine-generated context to the human.
    - A compaction summary or boundary is *not* dropped, it is replaced by a
      lightweight ``compact_marker`` entry. The client draws that as an
      inline "history compacted" divider, which keeps the visual timeline
      honest about where turns were lost. Carrying only id/role/created_at
      deliberately avoids shipping the summary text itself.
    - Raw archive payloads (``is_compact_active_archive``,
      ``is_compact_archive_hint``, ``is_compact_context``) are dropped. They
      are the largest and most sensitive of the compaction blocks; the
      ``compact_marker`` already communicates that something happened.
    - UI notices (``compact`` / ``model_switch`` records from
      ``Session.record_ui_notice``) are SPLICED IN at their anchored
      position as ``compact_notice`` / ``model_switch`` entries. They are
      recorded in session metadata, not in the message branch, so the
      model never sees them -- but the renderer must, exactly like a tool
      call: the notice survives reload and marks where the context was
      compressed or the model changed.

    Everything else passes through untouched, including renderer-only tool
    ids, which the model-facing projection strips and the client genuinely
    needs for message correlation.

    Args:
        session: A session exposing :meth:`encre.session.Session.get_renderer_messages`,
            which returns unsanitised active-branch message copies. Typed as
            :obj:`Any` on purpose so the function works with both the real
            session object and the lightweight stubs used in tests and in
            headless replay.
        start: Optional slice start over the *filtered* stream. The tail-first
            resume path projects only the most recent messages through this so
            the ``session_ready`` frame stays small and the session opens
            instantly; the older range follows in a backfill frame.
        stop: Optional slice stop over the *filtered* stream (``None`` = end).

    Returns:
        A new list of message dicts. Entries that passed through are the
        caller's objects as returned by the session's own copy step;
        nothing on the session is mutated, so this projection is safe to
        call repeatedly without converging the session state.

    Note:
        ``id`` falls back to ``compact-marker-<index>`` when a summary
        message has no id of its own, which happens for blocks produced
        before ids were assigned. ``created_at`` falls back to ``timestamp``
        and then to ``0`` for the same reason. The divider must never be
        dropped from the stream just because metadata is missing. The
        fallback index is offset by ``start`` so sliced projections can never
        collide with markers produced by a sibling slice of the same session.
    """
    messages = session.get_renderer_messages()
    if start or stop is not None:
        messages = messages[start:stop]
    result: list[dict[str, Any]] = []
    # Parallel seq map: notices are anchored by ``seq_in_branch``, so the
    # splice below needs each projected entry's seq (``-1`` for the
    # synthesised compact markers and legacy messages without one).
    entry_seqs: list[int] = []
    for message in messages:
        if message.get("role") == "system":
            continue
        # Loop plumbing (context updates, retry nudges, grace prompts) is
        # role "user" with is_synthetic=True; it is never user content.
        if message.get("role") == "user" and message.get("is_synthetic"):
            continue
        # Summaries and boundaries stay in the stream, but only as a
        # divider-shaped marker; the text is stripped so compressed context
        # never reaches the renderer payload.
        if message.get("is_compact_summary") or message.get("is_compact_boundary"):
            result.append({
                "id": message.get("id") or f"compact-marker-{start + len(result)}",
                "role": "compact_marker",
                "created_at": message.get("created_at") or message.get("timestamp") or 0,
            })
            entry_seqs.append(-1)
            continue
        # The raw archive/context blocks carry the whole compressed history
        # and must be dropped outright rather than markerised.
        if (
            message.get("is_compact_active_archive")
            or message.get("is_compact_archive_hint")
            or message.get("is_compact_context")
        ):
            continue
        result.append(message)
        seq = message.get("seq_in_branch")
        entry_seqs.append(seq if isinstance(seq, int) else -1)
    _splice_ui_notices(session, result, entry_seqs)
    return result


def _splice_ui_notices(
    session: Any,
    result: list[dict[str, Any]],
    entry_seqs: list[int],
) -> None:
    """Insert persisted UI notices into a projected renderer stream in place.

    A notice recorded with watermark ``seq`` belongs AFTER the last message
    whose ``seq_in_branch`` is <= that watermark, which is exactly where the
    compaction consumed the context (or where the conversation stood when a
    model switch happened). Notices whose anchor lies beyond the projected
    window are skipped -- they belong to a later chunk.
    """
    getter = getattr(session, "get_ui_notices", None)
    if not callable(getter):
        return
    try:
        notices = getter()
    except Exception:
        return
    if not notices:
        return
    max_seq = max(entry_seqs) if entry_seqs else -1
    for notice in sorted(notices, key=lambda n: (n.get("seq", -1), n.get("ts", 0))):
        seq = notice.get("seq", -1)
        if not isinstance(seq, int) or seq > max_seq:
            continue
        # Anchor ids embed a seq, so a rebuilt/renumbered transcript
        # invalidates both the id and the watermark: that is what dragged
        # every old notice to the very bottom of the conversation.
        # Resolve the id first, then fall back to the notice's timestamp,
        # which is stable across rebuilds -- the line then stays right
        # below the round the switch actually happened in.
        anchor_id = str(notice.get("anchor_id") or "")
        ts = notice.get("ts") or 0
        insert_at = -1
        if anchor_id:
            for i, entry in enumerate(result):
                if entry.get("id") == anchor_id:
                    insert_at = i + 1
                    break
        if insert_at < 0:
            for i, entry in enumerate(result):
                created = entry.get("created_at") or entry.get("timestamp") or 0
                if isinstance(created, int | float) and created and created <= ts:
                    insert_at = i + 1
        if insert_at < 0:
            # Older than this whole chunk (tail-first open): it belongs to
            # the earlier backfill chunk, not to the top of this one.
            continue
        kind = notice.get("kind", "")
        role = "compact_notice" if kind == "compact" else "model_switch"
        # A switch line only makes sense inside a conversation.  A session
        # whose transcript is empty (new chat, cleared history) must not
        # render a lone "model changed" divider floating on a blank page.
        if (
            role == "model_switch"
            and not any(e.get("role") in ("user", "assistant") for e in result)
        ):
            continue
        entry: dict[str, Any] = {
            "id": notice.get("id", ""),
            "role": role,
            "created_at": notice.get("ts", 0),
            "anchor_id": notice.get("anchor_id", ""),
        }
        for key in (
            "old_tokens", "new_tokens", "old_count", "new_count",
            "from_model", "to_model", "reason", "source",
        ):
            if key in notice:
                entry[key] = notice[key]
        result.insert(insert_at, entry)
        entry_seqs.insert(insert_at, seq)


__all__ = ["renderer_session_messages"]
