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
The hash is stored in the manifest's ``integrity`` field and verified on
subsequent loads.  This detects tampering or corruption of plugin source
code between loads.

Trusted tiers (``mandatory`` / ``system-default``) are exempt: they ship
with the application and their code is reviewed alongside the core.  Only
``user`` and project-local plugins are verified.

Integrity checking can be disabled with ``ENCRE_INTEGRITY_CHECK=0``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.plugins.integrity")

_TRUSTED_TIERS = frozenset({"mandatory", "system-default"})
_CHUNK = 65536


def compute_package_hash(pkg_dir: Path) -> str:
    """Compute SHA-256 over all ``.py`` files in *pkg_dir* (sorted by name).

    Non-recursive: only top-level ``.py`` files and immediate ``skills/``
    markdown files are included.  This keeps the hash fast for packages
    with hundreds of skill files.
    """
    h = hashlib.sha256()
    py_files = sorted(pkg_dir.rglob("*.py"))
    md_files = sorted((pkg_dir / "skills").rglob("*.md")) if (pkg_dir / "skills").is_dir() else []
    for f in py_files + md_files:
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
    return h.hexdigest()


def verify_integrity(manifest: Any, pkg_dir: Path) -> bool:
    """Verify that *pkg_dir*'s hash matches the manifest's ``integrity`` field.

    Returns ``True`` when:
    - The plugin tier is trusted (exempt from checking), or
    - Integrity checking is disabled via env, or
    - The manifest has no stored hash (first run — computes and stores), or
    - The computed hash matches the stored hash.

    Returns ``False`` and logs a warning when the hash mismatches.
    """
    if os.environ.get("ENCRE_INTEGRITY_CHECK", "1") == "0":
        return True
    tier = getattr(manifest, "tier", "user")
    if tier in _TRUSTED_TIERS:
        return True
    stored = getattr(manifest, "integrity", "")
    try:
        computed = compute_package_hash(pkg_dir)
    except Exception:
        logger.debug("Failed to compute integrity hash for %s", pkg_dir, exc_info=True)
        return True
    if not stored:
        manifest.integrity = computed
        return True
    if computed != stored:
        logger.warning(
            "Integrity mismatch for plugin '%s': expected %s, got %s",
            getattr(manifest, "name", "?"),
            stored[:16],
            computed[:16],
        )
        return False
    return True
