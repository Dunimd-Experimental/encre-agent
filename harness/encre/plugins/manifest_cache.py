#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
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

"""Disk cache for parsed ``pyproject.toml`` manifests.

Parsing 285+ TOML files on every startup is I/O-bound.  This module
caches the parsed result keyed by file path + mtime, so subsequent
startups skip re-parsing unchanged files entirely.

Cache file location: ``~/.dunimd/encre/ea_manifest_cache.json``

**Performance design**: the on-disk JSON is loaded **once** per process
into an in-memory dict.  All lookups and updates hit the in-memory dict.
The cache is flushed to disk lazily — at most once per process lifetime
or when explicitly requested.  This avoids the O(n²) I/O pattern of
reading+writing the entire JSON file for each of the 285 packages.

Invalidation is per-file mtime-based: if ``pyproject.toml`` was modified
since the cache entry was written, that entry is re-parsed and updated.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import time
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.plugins.cache")

_CACHE_DIR = Path.home() / ".dunimd" / "encre"
_CACHE_FILE = _CACHE_DIR / "ea_manifest_cache.json"

#: In-memory cache loaded once per process (lazy init).
_mem_cache: dict[str, Any] | None = None
#: Whether the cache has been modified and needs flushing.
_dirty = False


def _ensure_cache_dir() -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _load_cache_once() -> dict[str, Any]:
    """Load the on-disk cache into memory once per process."""
    global _mem_cache
    if _mem_cache is not None:
        return _mem_cache
    try:
        if _CACHE_FILE.exists():
            with open(_CACHE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "entries" in data:
                _mem_cache = data
                return _mem_cache
    except Exception:
        pass
    _mem_cache = {"_version": 1, "_built_at": 0.0, "entries": {}}
    return _mem_cache


def _flush_cache() -> None:
    """Write the in-memory cache to disk if dirty."""
    global _dirty
    if not _dirty or _mem_cache is None:
        return
    try:
        _ensure_cache_dir()
        _mem_cache["_built_at"] = time.time()
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_mem_cache, f, ensure_ascii=False)
        _dirty = False
    except Exception as exc:
        logger.debug("Failed to write manifest cache: %s", exc)


def get_cached_pyproject(path: Path) -> dict[str, Any] | None:
    """Return cached pyproject.toml data if fresh, else None.

    Uses mtime to detect staleness.  O(1) in-memory lookup after the
    initial load.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cache = _load_cache_once()
    entries = cache.get("entries", {})
    entry = entries.get(str(path))
    if isinstance(entry, dict) and entry.get("mtime") == mtime:
        data = entry.get("data")
        if isinstance(data, dict):
            return data
    return None


def put_cached_pyproject(path: Path, data: dict[str, Any]) -> None:
    """Store a parsed pyproject.toml result in the in-memory cache.

    Marks the cache as dirty; the disk write happens lazily on
    :func:`flush` or at process exit.
    """
    global _dirty
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    cache = _load_cache_once()
    entries = cache.setdefault("entries", {})
    entries[str(path)] = {"mtime": mtime, "data": data}
    _dirty = True


def parse_pyproject_cached(path: Path) -> dict[str, Any]:
    """Parse a pyproject.toml with in-memory disk-cache fallback.

    Tries the cache first (O(1) dict lookup); on miss, parses the file
    and stores the result in memory.  Never raises.
    """
    cached = get_cached_pyproject(path)
    if cached is not None:
        return cached
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            tomllib = None  # type: ignore[assignment]
    if tomllib is None:
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        put_cached_pyproject(path, data)
        return data
    except Exception as exc:
        logger.debug("Failed to parse %s: %s", path, exc)
        return {}


def flush() -> None:
    """Flush the in-memory cache to disk."""
    _flush_cache()


def prune_cache(valid_paths: set[Path]) -> int:
    """Remove cache entries for paths not in *valid_paths*."""
    global _dirty
    cache = _load_cache_once()
    entries = cache.get("entries", {})
    valid_strs = {str(p) for p in valid_paths}
    before = len(entries)
    entries = {k: v for k, v in entries.items() if k in valid_strs}
    pruned = before - len(entries)
    if pruned:
        cache["entries"] = entries
        _dirty = True
        _flush_cache()
    return pruned


def clear_cache() -> None:
    """Delete the cache file and reset the in-memory cache."""
    global _mem_cache, _dirty
    _mem_cache = None
    _dirty = False
    with contextlib.suppress(Exception):
        _CACHE_FILE.unlink(missing_ok=True)


atexit.register(flush)
