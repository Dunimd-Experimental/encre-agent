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

"""Append-only event log for the self-improvement / evolution subsystem.

An :class:`EventStore` is a thin journal of evolution-domain events: every
:class:`StoredEvent` published into it is written as one JSON object per
line to a single ``events.jsonl`` file.  Readers use :meth:`replay` to
reconstruct history (newest first, optionally narrowed by event type or
session) and :meth:`project` to fold that history into a plain dict.

Publishing is two-phase and order-sensitive: registered in-process handlers
are invoked synchronously *first*, and only after they return is the event
appended to disk.  That means a subscriber can observe an event before it
is durable, which is fine for the current consumers (the reviewer and the
UI notification path) because they only need eventual visibility, never
crash-exact durability.

Every filesystem failure is swallowed.  The journal exists to make
improvement data visible and recoverable; it must not be able to take down
the agent loop when the data directory is missing, read-only, or full.
"""

from __future__ import annotations


import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable



from encre.logging_config import get_logger
logger = get_logger(__name__)


@dataclass
class StoredEvent:
    """A single evolution-domain event persisted to the event log."""

    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    created_at: float = field(default_factory=time.time)


EventHandler = Callable[[StoredEvent], None]


class EventStore:
    """Append-only JSONL event store with in-process pub/sub handlers.

    Events are serialized one JSON object per line to ``path``.  Subscribers
    registered via :meth:`register_handler` are invoked synchronously on
    publish, and ``replay`` / ``project`` provide read access to history.
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path or self._default_path()
        self._handlers: dict[str, list[EventHandler]] = {}
        self._ensure_file()

    def publish(self, event: StoredEvent) -> None:
        for handler in self._handlers.get(event.event_type, []):
            try:
                handler(event)
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
        self._append_to_file(event)

    def register_handler(self, event_type: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def unregister_handler(self, event_type: str, handler: EventHandler) -> None:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    def replay(
        self,
        event_type: str | None = None,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        """Return stored events, newest first, optionally filtered."""
        events = []
        for event in self._iterate():
            if event_type and event.event_type != event_type:
                continue
            if session_id and event.session_id != session_id:
                continue
            events.append(event)
            if limit and len(events) >= limit:
                break
        events.reverse()
        return events

    def project(self, state: dict[str, Any], event: StoredEvent) -> dict[str, Any]:
        """Fold *event* into an accumulator state (reduce-style projection)."""
        state = dict(state)
        state[event.event_type] = event.payload
        return state

    def wire_hooks(self, hook_system: Any) -> None:
        """Bridge registered handlers onto an external hook bus."""
        for et in list(self._handlers.keys()):
            def _handler(event, _et: str = et) -> None:
                self.publish(event)
            try:
                hook_system.on(et, _handler)
            except Exception:
                logger.debug("exception suppressed", exc_info=True)

    def _append_to_file(self, event: StoredEvent) -> None:
        try:
            from encre.secure_io import append_jsonl

            append_jsonl(self._path, event.__dict__)
        except (OSError, IOError):
            logger.debug("exception suppressed", exc_info=True)

    def _iterate(self) -> list[StoredEvent]:
        result: list[StoredEvent] = []
        try:
            from encre.secure_io import read_jsonl

            for data in read_jsonl(self._path):
                try:
                    result.append(StoredEvent(**data))
                except (KeyError, TypeError):
                    continue
        except (OSError, IOError):
            logger.debug("exception suppressed", exc_info=True)
        return result

    def _ensure_file(self) -> None:
        if not os.path.isfile(self._path):
            try:
                with open(self._path, "w", encoding="utf-8") as f:
                    f.write("")
            except OSError:
                logger.debug("exception suppressed", exc_info=True)

    def _default_path(self) -> str:
        # Keep the event log beside ``evolution/state.json`` instead of in the
        # separate ``~/.encre`` root -- same data, previously two locations.
        try:
            from encre.paths import get_data_dir

            return str(get_data_dir("evolution", ensure=False) / "events.jsonl")
        except Exception:  # pragma: no cover - defensive
            return str(
                Path.home() / ".dunimd" / "encre" / "evolution" / "events.jsonl"
            )
