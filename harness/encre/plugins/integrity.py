#!/usr/bin/env python3

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

"""Plugin integrity verification.

Computes a SHA-256 hash over all ``.py`` files in a plugin package directory.
The hash is persisted to ``~/.dunimd/encre/integrity.json`` and verified on
subsequent loads.  This detects tampering or corruption of plugin source
code between loads.

Trusted tiers (``mandatory`` / ``system-default``) are exempt: they ship
with the application and their code is reviewed alongside the core.  Only
``user`` and project-local plugins are verified.

Integrity checking can be disabled with ``ENCRE_INTEGRITY_CHECK=0``.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.plugins.integrity")

_TRUSTED_TIERS = frozenset({"mandatory", "system-default"})
_CHUNK = 65536
_CACHE_DIR = Path.home() / ".dunimd" / "encre"
_CACHE_FILE = _CACHE_DIR / "integrity.json"
#: On-disk memo of computed package hashes, keyed by a cheap
#: (relpath, mtime_ns, size) fingerprint.  Any content/size/rename change
#: invalidates the entry; unchanged packages are not re-read and re-hashed
#: on every startup — the dominant discovery cost for user-tier plugins.
_HASH_CACHE_FILE = _CACHE_DIR / "integrity_hash_cache.json"

#: In-process memos (loaded once, flushed at exit / on write).
_BASELINE_MEM: dict[str, str] | None = None
_HASH_MEM: dict[str, dict[str, str]] | None = None
_HASH_DIRTY = False


def _load_json_cache(path: Path) -> dict[str, Any]:
    """Load a JSON cache file, returning {} on any failure."""
    try:
        if path.exists():
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        logger.debug("Failed to load cache %s", path, exc_info=True)
    return {}


def _flush_hash_cache() -> None:
    """Persist the computed-hash memo to disk when it has new entries."""
    global _HASH_DIRTY
    if not _HASH_DIRTY or _HASH_MEM is None:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _HASH_CACHE_FILE.write_text(json.dumps(_HASH_MEM), "utf-8")
        _HASH_DIRTY = False
    except Exception:
        logger.debug("Failed to save integrity hash cache", exc_info=True)


atexit.register(_flush_hash_cache)


def _iter_hash_files(pkg_dir: Path) -> list[Path]:
    """Deterministic file list hashed for a package.

    Source (``.py``), bundled skill docs (``skills/*.md``) and — since the
    frontend SDK can execute plugin JS — every UI asset under ``ui/`` are
    part of the trust boundary.  Without the last group a tampered
    ``ui/plugin.js`` would never invalidate the package hash.
    """
    py_files = sorted(pkg_dir.rglob("*.py"))
    md_files = sorted((pkg_dir / "skills").rglob("*.md")) if (pkg_dir / "skills").is_dir() else []
    ui_root = pkg_dir / "ui"
    ui_exts = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".svg"}
    ui_files = (
        sorted(p for p in ui_root.rglob("*") if p.is_file() and p.suffix.lower() in ui_exts)
        if ui_root.is_dir()
        else []
    )
    return py_files + md_files + ui_files


def _hash_fingerprint(files: list[Path], pkg_dir: Path) -> str:
    """Cheap validity digest over each file's (relpath, mtime_ns, size)."""
    h = hashlib.sha1()
    for f in files:
        try:
            st = f.stat()
            h.update(f"{f.relative_to(pkg_dir)}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
        except OSError:
            h.update(f"{f.relative_to(pkg_dir)}\0missing\n".encode())
    return h.hexdigest()


def compute_package_hash(pkg_dir: Path) -> str:
    """Compute SHA-256 over all ``.py`` and ``skills/*.md`` files in *pkg_dir*.

    Memoised per process and on disk, keyed by a fingerprint over each
    file's (relpath, mtime_ns, size): stat-ing the package is orders of
    magnitude cheaper than streaming every byte, so unchanged packages skip
    the read entirely.  The hash itself is byte-identical to the uncached
    computation (same file order, same streaming content), so existing
    baselines and pinned hashes stay valid.
    """
    global _HASH_DIRTY, _HASH_MEM
    key = str(pkg_dir)
    files = _iter_hash_files(pkg_dir)
    fp = _hash_fingerprint(files, pkg_dir)
    if _HASH_MEM is None:
        _HASH_MEM = _load_json_cache(_HASH_CACHE_FILE)
    entry = _HASH_MEM.get(key)
    if isinstance(entry, dict) and entry.get("fp") == fp and entry.get("hash"):
        return str(entry["hash"])
    h = hashlib.sha256()
    for f in files:
        h.update(str(f.relative_to(pkg_dir)).encode("utf-8"))
        h.update(b"\0")
        try:
            with open(f, "rb") as fh:
                while True:
                    chunk = fh.read(_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError:
            continue
    digest = h.hexdigest()
    _HASH_MEM[key] = {"fp": fp, "hash": digest}
    _HASH_DIRTY = True
    return digest


def _load_cache() -> dict[str, str]:
    """Load the integrity hash baseline cache (memoised per process)."""
    global _BASELINE_MEM
    if _BASELINE_MEM is None:
        _BASELINE_MEM = {str(k): str(v) for k, v in _load_json_cache(_CACHE_FILE).items()}
    return _BASELINE_MEM


def _save_cache(cache: dict[str, str]) -> None:
    """Persist the integrity hash baseline cache and refresh the memo."""
    global _BASELINE_MEM
    _BASELINE_MEM = dict(cache)
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(cache, indent=2), "utf-8")
    except Exception:
        logger.debug("Failed to save integrity cache", exc_info=True)


def forget_integrity(name: str) -> None:
    """Drop the stored baseline hash for *name*.

    Used by the hot-reload path: the developer has intentionally modified
    the plugin source, so the next discovery re-baselines instead of
    failing the (now guaranteed) mismatch and rejecting the plugin.
    """
    cache = _load_cache()
    if name in cache:
        cache.pop(name)
        _save_cache(cache)
        logger.debug("Dropped integrity baseline for plugin '%s'", name)


def verify_integrity(manifest: Any, pkg_dir: Path) -> bool:
    """Verify that *pkg_dir*'s hash matches the expected integrity value.

    Two verification sources, in priority order:

    1. **Pinned hash** — a non-empty ``manifest.integrity`` (SHA-256 hex).
       The computed hash must match it exactly; no baseline is written, so
       a distributor can ship a tamper-evident package.
    2. **Persisted baseline** — the cache in
       ``~/.dunimd/encre/integrity.json``.  On first run (no cached hash)
       the computed hash is stored as the baseline; later runs must match.

    Returns ``True`` when:
    - The plugin tier is trusted (exempt from checking), or
    - Integrity checking is disabled via env, or
    - The pinned hash matches, or
    - No baseline exists (first run — computes and persists), or
    - The computed hash matches the baseline.

    Returns ``False`` and logs a warning when a hash mismatches.
    """
    if os.environ.get("ENCRE_INTEGRITY_CHECK", "1") == "0":
        return True
    tier = getattr(manifest, "tier", "user")
    if tier in _TRUSTED_TIERS:
        return True
    name = getattr(manifest, "name", "?")
    try:
        computed = compute_package_hash(pkg_dir)
    except Exception:
        logger.warning("Integrity hash could not be computed for %s; refusing the plugin (fail-closed)", pkg_dir, exc_info=True)
        return False
    pinned = str(getattr(manifest, "integrity", "") or "").strip().lower()
    if pinned:
        if computed != pinned:
            logger.warning(
                "Integrity mismatch for plugin '%s': pinned %s, got %s",
                name,
                pinned[:16],
                computed[:16],
            )
            return False
        return True
    cache = _load_cache()
    stored = cache.get(name, "")
    if not stored:
        cache[name] = computed
        _save_cache(cache)
        return True
    if computed != stored:
        logger.warning(
            "Integrity mismatch for plugin '%s': expected %s, got %s",
            name,
            stored[:16],
            computed[:16],
        )
        return False
    return True
