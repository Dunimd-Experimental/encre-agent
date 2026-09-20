#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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

import os
import re
from dataclasses import dataclass, field
from typing import Any

from encre.memdir.age import memory_age_text, memory_freshness_text
from encre.memdir.entrypoint import load_entrypoint_raw, write_entrypoint

FRONTMATTER_MAX_LINES = 30
MAX_MEMORY_FILES = 200
ENTRYPOINT_NAME = "MEMORY.md"
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000


@dataclass
class MemoryHeader:
    """Metadata extracted from a memory file's front matter and mtime."""
    filename: str
    file_path: str
    mtime_ms: float
    description: str | None = None
    memory_type: str | None = None
    tags: list[str] = field(default_factory=list)
    age_text: str = ""


@dataclass
class EntrypointResult:
    """Holds the loaded MEMORY.md entrypoint plus size/truncation metadata."""
    content: str
    line_count: int
    byte_count: int
    was_line_truncated: bool
    was_byte_truncated: bool


class EncreMemorySystem:
    """File-I/O facade over the memory directory (no retrieval of its own).

    This class is deliberately **file I/O only**: it knows how to scan the
    encrypted ``.md`` memory files, read/write the ``MEMORY.md`` entrypoint,
    and parse front matter.  It does **not** search, recall, or assemble any
    prompt.

    Division of labour (do not conflate the two):

    * :class:`~encre.memdir.layers.MemoryLayerStore` is the **single store**
      of memories; retrieval flows only through
      :class:`~encre.memdir.recall.RecallEngine`.
    * This facade feeds that store via :meth:`~MemoryLayerStore.ingest_memory_dir`
      (which reuses :meth:`scan` and the front-matter parser owned here), and
      :meth:`refresh` provides the *ingestion trigger* the production loop
      already calls.
    """

    MAX_ENTRYPOINT_LINES = MAX_ENTRYPOINT_LINES
    MAX_ENTRYPOINT_BYTES = MAX_ENTRYPOINT_BYTES
    FRONTMATTER_MAX_LINES = FRONTMATTER_MAX_LINES
    ENTRYPOINT_NAME = ENTRYPOINT_NAME

    def __init__(self, auto_memory_path: str, layer_store: Any = None):
        self.auto_memory_path = auto_memory_path
        self._ensure_dir()
        # Optional collaborator: when attached, :meth:`refresh` ingests the
        # memory directory into the layer store.  When unattached, ``refresh``
        # degrades to a harmless no-op so call sites that only construct the
        # facade stay safe.
        self.layer_store = layer_store

    def _ensure_dir(self) -> None:
        """Create the memory directory if it does not already exist."""
        os.makedirs(self.auto_memory_path, exist_ok=True)

    def scan(self) -> list[MemoryHeader]:
        """Enumerate memory files and extract their headers.

        Walks the memory directory, skips the entrypoint, dotfiles, internal
        underscore-prefixed files, and non-markdown files, then parses each
        remaining file's front matter for description/type/tags and computes
        a human-readable age. Results are sorted newest-first and capped at
        ``MAX_MEMORY_FILES``.

        Returns:
            List of :class:`MemoryHeader` objects (empty on OS errors).
        """
        memories: list[MemoryHeader] = []
        try:
            with os.scandir(self.auto_memory_path) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if entry.name == ENTRYPOINT_NAME:
                        continue
                    # Hide dotfiles and underscore-prefixed internal files
                    # (e.g. _profile.md) from the user-facing memory list.
                    # They are still loaded by the system; just not shown.
                    if entry.name.startswith(".") or entry.name.startswith("_"):
                        continue
                    if not entry.name.endswith(".md"):
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    # st_mtime_ns is nanoseconds; convert to milliseconds
                    mtime_ms = stat.st_mtime_ns / 1_000_000.0
                    header = MemoryHeader(
                        filename=entry.name,
                        file_path=entry.path,
                        mtime_ms=mtime_ms,
                    )
                    try:
                        with open(entry.path, encoding="utf-8") as f:
                            raw_head = "".join(
                                f.readline() for _ in range(FRONTMATTER_MAX_LINES + 5)
                            )
                        # Decrypt if needed
                        if raw_head.strip() and not raw_head.strip().startswith("---") and not raw_head.strip().startswith("#"):
                            try:
                                from encre.crypto import decrypt
                                head = decrypt(raw_head.strip())
                            except Exception:
                                head = raw_head
                        else:
                            head = raw_head
                    except (OSError, UnicodeDecodeError):
                        memories.append(header)
                        continue
                    fm = self._parse_frontmatter(head)
                    if fm:
                        header.description = fm.get("description")
                        header.memory_type = fm.get("type")
                        header.tags = fm.get("tags", [])
                    header.age_text = memory_age_text(header.mtime_ms)
                    memories.append(header)
        except OSError:
            return []

        memories.sort(key=lambda m: m.mtime_ms, reverse=True)
        if len(memories) > MAX_MEMORY_FILES:
            memories = memories[:MAX_MEMORY_FILES]
        return memories

    def _parse_frontmatter(self, content: str) -> dict[str, Any] | None:
        """Extract a YAML front-matter block delimited by ``---`` lines.

        Args:
            content: Raw (possibly decrypted) file content.

        Returns:
            Parsed mapping, or ``None`` when no front matter is present.
        """
        pattern = r"^---\s*\n(.*?)\n---"
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            return None
        # Pull the text between the opening and closing '---' delimiters
        yaml_block = match.group(1)
        return self._parse_simple_yaml(yaml_block)

    def _parse_simple_yaml(self, yaml_block: str) -> dict[str, Any]:
        """Parse a minimal subset of YAML used in memory front matter.

        Supports ``key: value`` scalars and ``key:\\n  - item`` lists; it is
        intentionally not a full YAML parser to avoid extra dependencies.

        Args:
            yaml_block: Text between the ``---`` delimiters.

        Returns:
            Mapping of front-matter keys to string or list values.
        """
        result: dict[str, Any] = {}
        current_key: str | None = None
        current_list: list[str] = []

        for line in yaml_block.split("\n"):
            stripped = line.rstrip()
            if not stripped or stripped.startswith("#"):
                continue

            list_match = re.match(r"^\s+-\s+(.+)$", stripped)
            if list_match and current_key:
                current_list.append(list_match.group(1).strip().strip("\"'"))
                continue

            if current_key is not None and current_list:
                result[current_key] = current_list
                current_list = []
                current_key = None

            kv_match = re.match(r"^(\w[\w_-]*)\s*:\s*(.*)$", stripped)
            if kv_match:
                key = kv_match.group(1)
                value = kv_match.group(2).strip()
                if not value:
                    current_key = key
                    current_list = []
                else:
                    val = value.strip("\"'")
                    result[key] = val

        if current_key is not None and current_list:
            result[current_key] = current_list

        return result

    def load_entrypoint(self) -> EntrypointResult:
        """Load and size-limit the MEMORY.md entrypoint for prompts."""
        raw = load_entrypoint_raw(self.auto_memory_path)
        return EntrypointResult(
            content=raw["content"],
            line_count=raw["line_count"],
            byte_count=raw["byte_count"],
            was_line_truncated=raw["was_line_truncated"],
            was_byte_truncated=raw["was_byte_truncated"],
        )

    def write_entrypoint(self, content: str) -> None:
        """Encrypt and persist the MEMORY.md entrypoint, with plaintext fallback."""
        from encre.crypto import encrypt
        file_path = os.path.join(self.auto_memory_path, ENTRYPOINT_NAME)
        os.makedirs(self.auto_memory_path, exist_ok=True)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                # Encrypt before persisting so memory stays confidential at rest
                f.write(encrypt(content))
        except Exception:
            write_entrypoint(self.auto_memory_path, content)

    def _get_file_age(self, mtime_ms: float) -> str:
        """Return the human-readable age string for a timestamp."""
        return memory_age_text(mtime_ms)

    def _get_freshness_text(self, mtime_ms: float) -> str:
        """Return the staleness reminder snippet for a timestamp."""
        return memory_freshness_text(mtime_ms)

    def get_memory_path(self) -> str:
        """Return the underlying memory directory path."""
        return self.auto_memory_path

    # ---- ingestion trigger ------------------------------------------------

    def refresh(self) -> None:
        """Trigger ingestion of the memory directory into the layer store.

        The layer store is the single store and :class:`RecallEngine` reads its
        entries live on every recall, so this method is the *ingestion trigger*
        the production loop already calls after a successful compaction: it
        ingests every ``.md`` memory file into the store and re-reads the layer
        files so the recall pool matches disk exactly.

        When no store is attached (a facade-only construction, e.g. pure file
        tooling or a test) this degrades to a harmless no-op -- never raises.
        """
        store = self.layer_store
        if store is None:
            return
        store.ingest_memory_dir(self.auto_memory_path)
        store.reload()

