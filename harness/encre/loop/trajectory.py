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

"""Append-only trajectory event stream for agent sessions.

Every model-visible event of a session is recorded into a single
append-only JSONL file (``events.jsonl`` inside the session directory):
message appends, context injections, history replacements (compaction),
and session attachment.  The model-visible history can always be
re-derived from this event stream via :func:`derive_messages`, so
resume / fork / replay / audit all operate on the same source of truth
(DSH-style "every run is traceable").  Recording is strictly additive:
the existing turn-file persistence remains the load path for now; the
projection becomes the load path once trajectory replay is promoted.
"""


import json
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger

logger = get_logger(__name__)

# Content longer than this is truncated in recorded events; the full
# content still lives in the session turn files.
_CONTENT_PREVIEW_LIMIT = 8000


def _preview(content: Any) -> Any:
    """Return a size-bounded preview of message content for the event log."""
    if isinstance(content, str):
        if len(content) <= _CONTENT_PREVIEW_LIMIT:
            return content
        return content[:_CONTENT_PREVIEW_LIMIT] + "...[truncated]"
    if isinstance(content, list):
        try:
            text = json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            text = str(content)
        if len(text) <= _CONTENT_PREVIEW_LIMIT:
            return content
        return text[:_CONTENT_PREVIEW_LIMIT] + "...[truncated]"
    if content is None:
        return None
    return str(content)


def _preview_payload(payload: Any) -> Any:
    """Return a size-bounded preview of an arbitrary event payload."""
    if isinstance(payload, (str, list, dict, int, float, bool)) or payload is None:
        return _preview(payload)
    return str(payload)


class TrajectoryRecorder:
    """Append-only JSONL recorder bound to one session directory.

    Thread-safe: message appends may originate from streaming tool
    execution tasks as well as the main loop, so every write is serialised
    under a lock.  Sequence numbers resume from the existing file so a
    restart appends instead of resetting the ordering.
    """

    def __init__(self, session_dir: str | Path) -> None:
        self._path = Path(session_dir) / "events.jsonl"
        self._lock = threading.Lock()
        self._seq = 0
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.exists():
                with self._path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            seq = int(json.loads(line).get("seq", 0))
                        except Exception:
                            continue
                        if seq > self._seq:
                            self._seq = seq
        except Exception:
            logger.debug("[trajectory] init failed", exc_info=True)

    @property
    def path(self) -> Path:
        """Path of the underlying ``events.jsonl`` file."""
        return self._path

    def record(self, event_type: str, **fields: Any) -> dict[str, Any] | None:
        """Append one event; never raises (recording must not break the loop)."""
        try:
            with self._lock:
                event: dict[str, Any] = {
                    "seq": self._seq + 1,
                    "ts": int(time.time() * 1000),
                    "type": event_type,
                }
                event.update(fields)
                self._seq += 1
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                return event
        except Exception:
            logger.debug("[trajectory] record failed for %s", event_type, exc_info=True)
            return None

    def record_message(self, message: dict[str, Any]) -> None:
        """Record a ``message.appended`` event from a session message dict."""
        if not isinstance(message, dict):
            return
        self.record(
            "message.appended",
            role=message.get("role", ""),
            content=_preview(message.get("content")),
            tool_call_id=message.get("tool_call_id"),
            is_synthetic=bool(message.get("is_synthetic", False)),
            branch_id=message.get("branch_id"),
            seq_in_branch=message.get("seq_in_branch"),
            message_id=message.get("id"),
        )

    def on_session_event(self, event_type: str, payload: Any) -> None:
        """Sink bound to ``session.trajectory_sink``.

        ``message.appended`` payloads are session message dicts and are
        recorded as structured message events; any other event type is
        recorded verbatim with a bounded payload preview.
        """
        if event_type == "message.appended" and isinstance(payload, dict):
            self.record_message(payload)
        else:
            self.record(event_type, payload=_preview_payload(payload))


def load_events(path: str | Path) -> list[dict[str, Any]]:
    """Load all trajectory events from an ``events.jsonl`` file.

    Corrupt lines (partial writes, crash tails) are skipped so a damaged
    tail never blocks replay of the healthy prefix.
    """
    events: list[dict[str, Any]] = []
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except FileNotFoundError:
        return []
    except Exception:
        logger.debug("[trajectory] load_events failed", exc_info=True)
    return events


def derive_messages(
    events: Iterable[dict[str, Any]],
    upto_seq: int | None = None,
    branch_id: str | None = None,
) -> list[dict[str, Any]]:
    """Project model-visible messages from trajectory events.

    Replays ``message.appended`` events in sequence order up to
    ``upto_seq`` (inclusive; ``None`` means "to the end"), optionally
    filtered by branch.  This is the future load path: the session
    history is a projection of the event stream, never an independently
    mutated store.
    """
    messages: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "message.appended":
            continue
        seq = event.get("seq", 0)
        if upto_seq is not None and seq > upto_seq:
            continue
        event_branch = event.get("branch_id")
        if branch_id is not None and event_branch != branch_id:
            continue
        messages.append({
            "role": event.get("role", ""),
            "content": event.get("content"),
            "tool_call_id": event.get("tool_call_id"),
            "is_synthetic": bool(event.get("is_synthetic", False)),
            "branch_id": event_branch,
            "seq_in_branch": event.get("seq_in_branch"),
            "id": event.get("message_id"),
        })
    return messages


def iter_events(path: str | Path) -> Iterator[dict[str, Any]]:
    """Stream trajectory events without materialising the whole file."""
    yield from load_events(path)
