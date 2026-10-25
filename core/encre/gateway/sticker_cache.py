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

"""Persistent vision-description cache for Telegram stickers.

Stickers are images: turning one into text costs a vision-model round-trip.
The same sticker (identified by ``file_unique_id``, which is stable across
bots and downloads, unlike ``file_id``) is reused constantly inside a chat, so
the gateway describes each one once and reuses the result.

The cache is process-local in memory for speed and mirrored to a JSON file so
descriptions survive restarts.  Writes are atomic (temp file + ``os.replace``)
so a crash mid-write cannot leave a half-serialised cache behind, and a
corrupt file is discarded rather than fatal.
"""

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:
    from encre.logging_config import get_logger

    logger = get_logger(__name__)
except Exception:  # pragma: no cover - logging is best-effort
    import logging

    logger = logging.getLogger(__name__)

__all__ = [
    "STICKER_VISION_PROMPT",
    "get_cached_description",
    "cache_sticker_description",
    "build_sticker_injection",
    "build_animated_sticker_injection",
    "clear_sticker_cache",
]

# Instructions handed to the vision model when describing a sticker.
STICKER_VISION_PROMPT = (
    "You are looking at a single chat sticker. Reply with one short sentence "
    "(at most 20 words) describing the emotion, action, or object it shows. "
    "Do not mention that it is a sticker, do not use markdown, and do not "
    "greet the user."
)

# Descriptions are cosmetic context, not durable data: 30 days is generous.
DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60

# Bound the on-disk cache so a busy chat cannot grow it without limit.  When
# the cap is exceeded the oldest entries are evicted.
_MAX_ENTRIES = 4096

_CACHE_FILENAME = "sticker_cache.json"

_lock = threading.RLock()
# Loaded lazily on first use so importing this module never touches the disk
# (adapters import it at module import time).
_memory: dict[str, dict[str, Any]] | None = None


def _cache_path() -> Path:
    """Return the JSON cache path, creating the parent directory on demand."""
    from encre.paths import get_data_dir

    return get_data_dir("gateway") / _CACHE_FILENAME


def _load_locked() -> dict[str, dict[str, Any]]:
    """Return the in-memory cache, loading (and pruning) it once from disk.

    Caller must hold :data:`_lock`.
    """
    global _memory
    if _memory is not None:
        return _memory

    cache: dict[str, dict[str, Any]] = {}
    path = _cache_path()
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                now = time.time()
                for key, entry in raw.items():
                    if isinstance(entry, dict) and not _is_expired(entry, now):
                        cache[key] = entry
    except Exception as exc:
        logger.warning("Discarding unreadable sticker cache at %s: %s", path, exc)
        cache = {}
    _memory = cache
    return _memory


def _is_expired(entry: dict[str, Any], now: float | None = None) -> bool:
    """Return whether a cache *entry* has outlived its TTL."""
    expires_at = entry.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return False
    return (now if now is not None else time.time()) >= float(expires_at)


def _prune_locked(cache: dict[str, dict[str, Any]]) -> None:
    """Drop expired entries and enforce :data:`_MAX_ENTRIES`. Caller holds lock."""
    now = time.time()
    for key in [k for k, v in cache.items() if _is_expired(v, now)]:
        cache.pop(key, None)
    if len(cache) <= _MAX_ENTRIES:
        return
    # Evict the least-recently-stored entries first.
    ordered = sorted(cache.items(), key=lambda kv: kv[1].get("cached_at", 0))
    for key, _ in ordered[: len(cache) - _MAX_ENTRIES]:
        cache.pop(key, None)


def _persist_locked(cache: dict[str, dict[str, Any]]) -> None:
    """Atomically write *cache* to disk. Caller holds :data:`_lock`."""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".sticker_cache.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(cache, handle, ensure_ascii=False)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass
    except Exception as exc:
        # A cache that cannot persist is still useful in-memory; never raise.
        logger.warning("Failed to persist sticker cache to %s: %s", path, exc)


def get_cached_description(file_unique_id: str) -> dict[str, Any] | None:
    """Return the cached description record for a sticker, or ``None``.

    Args:
        file_unique_id: The sticker's ``file_unique_id``.

    Returns:
        A mapping with at least ``description``, and optionally ``emoji``,
        ``set_name``, ``cached_at`` and ``expires_at``.  ``None`` when the
        sticker was never described or its entry expired.
    """
    if not file_unique_id:
        return None
    with _lock:
        cache = _load_locked()
        entry = cache.get(str(file_unique_id))
        if entry is None:
            return None
        if _is_expired(entry):
            cache.pop(str(file_unique_id), None)
            _persist_locked(cache)
            return None
        return dict(entry)


def cache_sticker_description(
    file_unique_id: str,
    description: str,
    emoji: str = "",
    set_name: str = "",
    ttl: int | None = None,
) -> None:
    """Store a sticker's vision description and mirror the cache to disk.

    Args:
        file_unique_id: The sticker's stable ``file_unique_id``.
        description: The vision model's description.
        emoji: The sticker's associated emoji, if known.
        set_name: The sticker set name, if known.
        ttl: Optional override for the entry's lifetime in seconds.
    """
    if not file_unique_id or not description:
        return
    lifetime = DEFAULT_TTL_SECONDS if ttl is None else max(int(ttl), 0)
    now = time.time()
    entry = {
        "description": str(description),
        "emoji": str(emoji or ""),
        "set_name": str(set_name or ""),
        "cached_at": now,
        "expires_at": now + lifetime if lifetime else None,
    }
    with _lock:
        cache = _load_locked()
        cache[str(file_unique_id)] = entry
        _prune_locked(cache)
        _persist_locked(cache)


def build_sticker_injection(
    description: str, emoji: str = "", set_name: str = ""
) -> str:
    """Render a cached description into the text injected into the prompt.

    Args:
        description: The sticker's description.
        emoji: Optional associated emoji.
        set_name: Optional sticker set name.

    Returns:
        A single-line bracketed annotation, e.g.
        ``[Sticker: a cat nodding, emoji 👍, set "Cats"]``.
    """
    parts = [str(description).strip() or "an unnamed sticker"]
    if emoji:
        parts.append(f"emoji {emoji}")
    if set_name:
        parts.append(f'set "{set_name}"')
    return "[Sticker: " + ", ".join(parts) + "]"


def build_animated_sticker_injection(emoji: str = "") -> str:
    """Render the annotation used when only an animated sticker's emoji is known.

    Animated stickers cannot be described without a costly multi-frame vision
    pass, so the gateway falls back to their emoji.

    Args:
        emoji: The sticker's associated emoji, if any.

    Returns:
        A single-line bracketed annotation.
    """
    emoji = str(emoji or "").strip()
    if emoji:
        return f"[Animated sticker: emoji {emoji}]"
    return "[Animated sticker]"


def clear_sticker_cache() -> None:
    """Drop every cached sticker description, in memory and on disk."""
    global _memory
    with _lock:
        _memory = {}
        try:
            path = _cache_path()
            if path.is_file():
                os.remove(path)
        except Exception as exc:
            logger.warning("Failed to remove sticker cache file: %s", exc)
