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

"""Channel directory: a cached snapshot of which channels and contacts every
connected platform can currently reach.

Position in the gateway: adapters know their own reachable channels, but they do
so through vendor-specific APIs and only while connected.  This module flattens
that into one in-memory map, refreshed periodically by the gateway runner, and
persists it so that tooling can answer "who can I message?" without waking every
platform connection.

Consumers:

* The ``send_message`` tool reads this directory for its ``action="list"`` path
  and to resolve a human-friendly channel name (``"general"``) into the numeric
  id an adapter's ``send()`` requires.
* :class:`encre.gateway.delivery.DeliveryRouter` is the sibling path that
  performs the actual send; this module only answers lookup questions.

Persistence is via :mod:`encre.secure_io`, so the file on disk is encrypted the
same way as every other Encre data file.  The file is a cache, never a source of
truth: adapters win on the next :meth:`ChannelDirectory.build`.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from encre.config import get_data_dir

logger = logging.getLogger("encre.gateway.channel_directory")

# Filesystem location of the persisted snapshot.  Computed at import time because
# :func:`encre.config.get_data_dir` is stable for a process lifetime; the runner
# imports this module once and reuses the constant.
DIRECTORY_PATH = Path(get_data_dir()) / "channel_directory.json"


class ChannelDirectory:
    """In-memory plus on-disk map of reachable channels per platform.

    Shape: ``{platform_name: [channel_entry, ...]}`` where each
    ``channel_entry`` is an adapter-provided dict carrying at minimum an
    ``"id"`` and a ``"name"`` key.  Extra keys (topic, member count, privacy
    flags) are passed through untouched, which is what lets each adapter
    advertise platform-specific fields without this module learning about them.

    Lifecycle: constructed empty -> :meth:`build` (populates from live adapters
    and persists) -> read via :meth:`lookup` / :meth:`list_platform` /
    :meth:`list_all` -> optionally :meth:`load` at startup to seed a warm cache
    before the first refresh completes.

    Thread safety: there is no lock.  ``build()`` rewrites ``_platforms`` from a
    cleared state, and readers observe either the pre-build or post-build map.
    Callers in the gateway runner drive refreshes from a single scheduler, so a
    torn read is not expected; do not call :meth:`build` concurrently with
    itself.
    """

    def __init__(self) -> None:
        """Start with an empty in-memory map and a "never refreshed" timestamp.

        Both fields are plain defaults because nothing is read from disk during
        construction: the live adapters that supply channel entries are only
        discovered by :meth:`encre.gateway.run.GatewayRunner.start` after the
        directory exists, so there is no source to seed from here. Loading a
        previously persisted snapshot is left to the explicit :meth:`load` call
        rather than done eagerly, which keeps a warm cache optional instead of
        mandatory.

        Invariant: the returned instance is empty, so a ``0.0``
        :attr:`_last_build` unambiguously means "no :meth:`build` has run yet"
        for the refresh scheduler that reads it, and :meth:`platforms` never
        returns stale data on a cold start.

        Args:
            None.
        """
        self._platforms: Dict[str, List[Dict[str, Any]]] = {}
        self._last_build: float = 0.0

    @property
    def platforms(self) -> Dict[str, List[Dict[str, Any]]]:
        """Per-platform channel entries.

        Returns:
            The live internal mapping, not a copy.  Callers must treat it as
            read-only; mutation bypasses :meth:`_save` and would silently desync
            the on-disk snapshot.
        """
        return self._platforms

    def build(self, adapters: Dict[str, Any]) -> None:
        """Rebuild the directory from the currently connected adapters.

        Each adapter exposing a callable ``get_channels`` contributes its
        entries.  Adapters that lack the method, or whose method returns an empty
        list, are skipped silently so that platforms without channel introspection
        do not appear as empty entries and do not log noise.  A raising adapter
        is logged and skipped; one broken vendor must not abort the refresh.

        Args:
            adapters: Mapping of platform name to live adapter instance, as held
                by the gateway's adapter manager.
        """
        self._platforms.clear()
        for name, adapter in adapters.items():
            get_channels = getattr(adapter, "get_channels", None)
            if callable(get_channels):
                try:
                    channels = get_channels()
                    if channels:
                        self._platforms[name] = channels
                except Exception as e:
                    logger.warning("[channel-dir] %s.get_channels() failed: %s", name, e)
        self._last_build = time.time()
        self._save()

    def lookup(self, platform: str, query: str) -> Optional[Dict[str, Any]]:
        """Resolve a channel by id or by name on one platform.

        Matching is intentionally asymmetric: an id is compared against the raw,
        unstripped query (ids are opaque vendor strings and must match exactly),
        while a name is compared case-insensitively after stripping whitespace and
        a leading ``#`` so that ``"#general"`` and ``"general"`` resolve to the
        same channel the way users type them.

        Args:
            platform: Platform name as used in the directory keys.
            query: Either a channel id or a human channel name.

        Returns:
            The matching channel entry, or ``None`` when nothing matched.
        """
        entries = self._platforms.get(platform, [])
        # Normalise the query: drop surrounding whitespace, lowercase, and strip
        # a leading '#' so "#general" and "general" match the same channel name.
        query_lower = query.strip().lower().lstrip("#")
        for entry in entries:
            if str(entry.get("id", "")) == query:
                return entry
            if str(entry.get("name", "")).lower() == query_lower:
                return entry
        return None

    def list_platform(self, platform: str) -> List[Dict[str, Any]]:
        """List all channels for one platform.

        Args:
            platform: Platform name.

        Returns:
            That platform's channel entries, or an empty list when the platform
            is unknown or has no introspectable channels.
        """
        return self._platforms.get(platform, [])

    def list_all(self) -> Dict[str, List[Dict[str, Any]]]:
        """List all channels across all platforms.

        Returns:
            A shallow copy of the mapping so callers can iterate or serialise
            without holding a reference to the internal state that
            :meth:`build` may replace.
        """
        return dict(self._platforms)

    def _save(self) -> None:
        """Persist the directory to disk as encrypted JSON.

        Failures are logged and swallowed.  Saving is a durability convenience,
        not a correctness requirement: the in-memory map is authoritative for
        this process and will be rebuilt from adapters on the next refresh.
        """
        from encre.secure_io import write_json

        try:
            write_json(
                DIRECTORY_PATH,
                {"platforms": self._platforms, "built_at": self._last_build},
            )
        except Exception as e:
            logger.warning("[channel-dir] save failed: %s", e)

    def load(self) -> bool:
        """Load a previously persisted directory snapshot.

        Intended for startup: it gives tooling a usable directory before the
        first adapter refresh completes, at the cost of possibly stale entries
        until :meth:`build` replaces them.

        Returns:
            ``True`` if a snapshot was read and applied, ``False`` if the file
            is missing or unreadable.
        """
        if not DIRECTORY_PATH.exists():
            return False
        try:
            from encre.secure_io import read_json

            data = read_json(DIRECTORY_PATH, default={})
            self._platforms = data.get("platforms", {})
            self._last_build = data.get("built_at", 0.0)
            return True
        except Exception as e:
            logger.warning("[channel-dir] load failed: %s", e)
            return False
