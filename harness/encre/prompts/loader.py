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

"""
Prompt file loader.

Reads plaintext ``.prompt`` files from ``blocks/``, ``skills/``, etc. at
runtime and caches them for the lifetime of the process.

Usage::

    loader = PromptLoader()
    content = loader.load("identity")
    content = loader.load("bypass", category="permission")
    content = loader.load_with_context("datetime", category="blocks", year="2026")
"""

import os
import re
from typing import Any

# Absolute path to the directory that contains the ``.prompt`` block files.
_PROMPTS_ROOT: str = os.path.abspath(os.path.dirname(__file__))
# Process-wide cache mapping "<category>/<name>" -> prompt text.
_CACHE: dict[str, str] = {}


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML-style frontmatter from a prompt file.

    The frontmatter is delimited by ``---`` lines at the start of the file::

        ---
        name: identity
        priority: 0
        condition: ~
        ---

    Returns ``(metadata, body)`` where *body* is the text after the closing
    ``---``.  If no frontmatter is found, *metadata* is empty and *body* is
    the original text.
    """
    # Heuristic: must start with "---" on the very first line.
    if not text.startswith("---"):
        return {}, text

    # Find the closing ---
    end_idx = text.find("\n---", 3)
    if end_idx == -1:
        return {}, text

    header = text[3:end_idx].strip()
    body = text[end_idx + 4 :].strip()

    meta: dict[str, Any] = {}
    lines = header.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        idx += 1
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\w+)\s*:\s*(.*)", line)
        if not m:
            continue
        key = m.group(1)
        raw = m.group(2).strip()

        # null / None / ~
        if raw in ("~", "null", "None", ""):
            # Block-style list: key: followed by ``- item`` lines
            if raw == "" and idx < len(lines):
                block_items: list[str] = []
                while idx < len(lines):
                    next_line = lines[idx]
                    stripped = next_line.strip()
                    if stripped.startswith("- "):
                        block_items.append(stripped[2:].strip().strip("'\""))
                        idx += 1
                    elif not stripped or stripped.startswith("#"):
                        idx += 1
                    else:
                        break
                if block_items:
                    meta[key] = block_items
                else:
                    meta[key] = None
            else:
                meta[key] = None
        # list: [a, b, c]
        elif raw.startswith("[") and raw.endswith("]"):
            items = raw[1:-1].split(",")
            meta[key] = [i.strip().strip("'\"") for i in items if i.strip()]
        # float (allows priorities between integer blocks, e.g. ``priority: 0.5``)
        elif re.match(r"^-?\d+\.\d+$", raw):
            meta[key] = float(raw)
        # integer
        elif raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
            meta[key] = int(raw)
        # string
        else:
            meta[key] = raw.strip("'\"")

    return meta, body


class PromptLoader:
    """Loads and caches prompt files.

    Prompt files live under ``<prompts_root>/<category>/<name>.prompt``.
    """

    def __init__(self, root: str | None = None) -> None:
        self._root = root or _PROMPTS_ROOT

    def load(self, name: str, category: str = "blocks") -> str:
        """Load a prompt file, strip frontmatter, return body.

        Parameters
        ----------
        name:
            Prompt file name (without ``.prompt`` suffix), e.g. ``"identity"``.
        category:
            Sub-directory under the prompts root, e.g. ``"blocks"``, ``"skills"``.
        """
        raw = self._read_raw(name, category)
        _meta, body = _parse_frontmatter(raw)
        return body

    def load_with_context(self, name: str, category: str = "blocks", **ctx: Any) -> str:
        """Load a prompt (stripping frontmatter) and substitute ``{{key}}`` placeholders."""
        body = self.load(name, category=category)
        for key, val in ctx.items():
            body = body.replace(f"{{{{{key}}}}}", str(val))
        return body

    def load_full(self, name: str, category: str = "blocks") -> tuple[dict[str, Any], str]:
        """Load a prompt file with frontmatter, return ``(metadata, body)``.

        *metadata* is a dict with keys like ``name``, ``priority``, ``condition``
        parsed from the YAML frontmatter.  *body* is the content after the
        frontmatter, with ``{{placeholder}}`` variables still present.
        """
        raw = self._read_raw(name, category)
        meta, body = _parse_frontmatter(raw)
        return meta, body

    def _read_raw(self, name: str, category: str) -> str:
        """Read the raw file content from cache or disk."""
        cache_key = f"{category}/{name}"
        if cache_key in _CACHE:
            return _CACHE[cache_key]

        path = os.path.join(self._root, category, f"{name}.prompt")
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read().strip()
        except FileNotFoundError:
            raise FileNotFoundError(f"Prompt file not found: {path}. Ensure the file exists under prompts/{category}/.") from None

        _CACHE[cache_key] = content
        return content

    def get_block_path(self, name: str, category: str = "blocks") -> str:
        """Return the absolute path to a prompt file (for diagnostics)."""
        return os.path.join(self._root, category, f"{name}.prompt")

    def clear_cache(self) -> None:
        """Drop all cached prompt files (useful for tests or hot-reload)."""
        _CACHE.clear()

    @property
    def root(self) -> str:
        """Absolute path to the prompts root directory."""
        return self._root
