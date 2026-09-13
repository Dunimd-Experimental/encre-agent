#!/usr/bin/env python3

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

"""Automatic discovery of bundled EA extension packages.

Every EA package is a self-contained pip project whose root directory is
placed under ``ea_tools/``.  Discovery is fully directory-driven: dropping a
new package folder (e.g. ``ea-tool-foo/`` or ``ea-skill-bar/``) is enough to
register it — no hardcoded module lists anywhere.

Two runtime layouts are supported:

* **Development**: packages live in ``<harness>/ea_tools/<pkg>/``.  If a
  package is not already importable, its directory is prepended to
  ``sys.path`` (no pip install step required).
* **Frozen (PyInstaller)**: the whole ``ea_tools/`` tree is shipped as data
  under ``sys._MEIPASS/ea_tools/``; the same scan + ``sys.path`` logic applies.

Each package's ``pyproject.toml`` is the single source of truth for metadata
(``name``, ``version``, ``authors``, ``description``) and carries an optional
``[tool.ea]`` table for EA-specific fields — most importantly ``tier``:

* ``mandatory``      — hardcoded core tools, cannot be uninstalled
* ``system-default`` — ships with the app, can be uninstalled/rebuilt
* ``user`` (default) — third-party, installed externally via pip
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.plugins.ea_scan")

#: Value of ``[tool.ea] tier`` for each supported plugin tier.
TIER_MANDATORY = "mandatory"
TIER_SYSTEM_DEFAULT = "system-default"
TIER_USER = "user"

#: Directory name that groups all bundled EA packages.
_EA_TOOLS_DIRNAME = "ea_tools"

#: Accepted names for the entry-point group that maps a package to its plugin.
_ENTRY_POINT_GROUP = "ea.plugins"


@dataclass(frozen=True)
class EaPackageInfo:
    """Metadata + resolution info for one discovered EA package."""

    name: str
    version: str
    description: str
    author: str
    tier: str
    module_name: str
    factory_target: str
    pkg_dir: Path
    skills_dir: Path | None


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _ea_tools_candidates(here: Path) -> list[Path]:
    """Return dev-tree candidate locations of the ``ea_tools/`` directory.

    Ordered by preference.  In a source checkout ``ea_scan.py`` lives at
    ``harness/encre/plugins/ea_scan.py``, so ``parents[2]`` is the harness
    root and ``parents[3]`` the repository root.  The packages historically
    sat at ``harness/ea_tools/`` and now live at ``core/ea_tools/`` (the EA
    extension set belongs to the encre-core distribution).  A bare
    ``pip install encre-harness`` has none of them — discovery then relies
    on entry points, which is the intended embedding behaviour.
    """
    harness_root = here.parents[2]          # .../harness
    repo_root = here.parents[3]             # .../encre (repository root)
    return [
        repo_root / "core" / _EA_TOOLS_DIRNAME,   # current: <repo>/core/ea_tools/
        repo_root / _EA_TOOLS_DIRNAME,            # repo-level: <repo>/ea_tools/
        harness_root / _EA_TOOLS_DIRNAME,        # legacy: harness/ea_tools/
    ]


def ea_tools_root() -> Path:
    """Return the directory that contains the bundled ``ea-tool-*`` packages.

    Resolution order:

    1. ``ENCRE_EA_TOOLS_ROOT`` environment variable (explicit override).
    2. ``sys._MEIPASS/ea_tools`` in frozen (PyInstaller) builds.
    3. The first existing dev-tree candidate (see
       :func:`_ea_tools_candidates`).

    Returns a nonexistent path when nothing matches; the scanner treats a
    missing directory as "no bundled packages" (entry-point discovery only).
    """
    # 1. Explicit override wins.
    env_root = os.environ.get("ENCRE_EA_TOOLS_ROOT")
    if env_root:
        return Path(env_root)
    if _is_frozen():
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        return meipass / _EA_TOOLS_DIRNAME
    here = Path(__file__).resolve()
    for candidate in _ea_tools_candidates(here):
        if candidate.is_dir():
            return candidate
    # Nothing found: return the legacy path so callers see a consistent
    # (missing) location instead of an odd half-composed one.
    return _ea_tools_candidates(here)[0]


def _parse_pyproject(path: Path) -> dict[str, Any]:
    """Parse a ``pyproject.toml`` into a plain dict, or return {} on failure.

    Delegates to :mod:`encre.plugins.manifest_cache` for disk-cached
    parsing — the first startup pays the TOML parse cost, subsequent
    startups hit the mtime-validated cache and skip I/O entirely.
    """
    from encre.plugins.manifest_cache import parse_pyproject_cached
    data = parse_pyproject_cached(path)
    if not data:
        logger.debug("Empty/failed pyproject.toml at %s", path)
    return data


def _extract_tier(data: dict[str, Any]) -> str:
    """Read ``[tool.ea] tier``; bundled packages default to system-default."""
    tool_ea = data.get("tool", {}).get("ea", {})
    if isinstance(tool_ea, dict) and tool_ea.get("tier") in (TIER_MANDATORY, TIER_SYSTEM_DEFAULT, TIER_USER):
        return tool_ea["tier"]
    return TIER_SYSTEM_DEFAULT


def _derive_module_name(pkg_dir: Path, data: dict[str, Any]) -> str | None:
    """Derive the top-level importable package name for a package.

    Prefers the ``ea.plugins`` entry-point value (``module:factory``) — using
    only its first dotted component so ``ea_tool_foo.plugin:create_plugin``
    yields the top-level package ``ea_tool_foo`` — falling back to the
    standard ``ea-tool-foo -> ea_tool_foo`` name transform.
    """
    eps = data.get("project", {}).get("entry-points", {}).get(_ENTRY_POINT_GROUP)
    if isinstance(eps, dict) and eps:
        for target in eps.values():
            if isinstance(target, str) and ":" in target:
                top = target.split(":", 1)[0].split(".", 1)[0]
                if top.startswith("ea_"):
                    return top
    # Fallback: ea-tool-file-read -> ea_tool_file_read
    name = data.get("project", {}).get("name", pkg_dir.name)
    if isinstance(name, str):
        module = name.replace("-", "_")
        # Reject non-package names (e.g. bare "ea_tools")
        if module.startswith("ea_") and module != _EA_TOOLS_DIRNAME:
            return module
    return None


def _ensure_importable(module_name: str, pkg_dir: Path) -> bool:
    """Make sure *module_name* resolves from *pkg_dir*.

    ``pkg_dir`` is prepended to ``sys.path`` so the top-level package it
    contains (``pkg_dir/<module_name>/``) becomes importable.  Existence is
    verified with a single filesystem probe rather than
    ``importlib.util.find_spec`` — the latter walks every ``sys.path`` entry
    (~170 ``stat`` calls per package), which dominated discovery time across
    hundreds of packages.
    """
    pkg_dir_str = str(pkg_dir)
    if pkg_dir_str not in sys.path:
        sys.path.insert(0, pkg_dir_str)
    return (pkg_dir / module_name).is_dir() or (pkg_dir / f"{module_name}.py").is_file()


def iter_ea_packages() -> Iterator[EaPackageInfo]:
    """Yield one :class:`EaPackageInfo` per bundled EA package directory.

    Scans ``ea_tools/`` for ``ea-tool-*`` and ``ea-skill-*`` directories, reads
    each package's
    ``pyproject.toml``, and resolves its plugin factory.  Packages whose
    metadata is unreadable or whose module cannot be resolved are skipped
    with a warning — they never break the rest of discovery.
    """
    root = ea_tools_root()
    if not root.is_dir():
        return

    for pkg_dir in sorted(root.iterdir()):
        if not pkg_dir.is_dir() or not pkg_dir.name.startswith(("ea-tool-", "ea-skill-")):
            continue
        data = _parse_pyproject(pkg_dir / "pyproject.toml")
        module_name = _derive_module_name(pkg_dir, data)
        if module_name is None:
            logger.warning(f"Could not resolve module name for EA package {pkg_dir.name}")
            continue
        _ensure_importable(module_name, pkg_dir)

        project = data.get("project", {}) if isinstance(data, dict) else {}
        name = project.get("name", pkg_dir.name) if isinstance(project, dict) else pkg_dir.name
        version = project.get("version", "0.0.0") if isinstance(project, dict) else "0.0.0"
        description = project.get("description", "") if isinstance(project, dict) else ""
        authors = project.get("authors") if isinstance(project, dict) else []
        author = ""
        if isinstance(authors, list) and authors:
            first = authors[0]
            if isinstance(first, dict):
                author = first.get("name", "") or (first.get("email") or "")

        eps = project.get("entry-points", {}).get(_ENTRY_POINT_GROUP) if isinstance(project, dict) else None
        factory_target = ""
        if isinstance(eps, dict) and eps:
            factory_target = next(iter(eps.values()), "")
        if not isinstance(factory_target, str):
            factory_target = ""
        if factory_target and ":" not in factory_target:
            factory_target = f"{module_name}:create_plugin" if factory_target else ""

        skills_dir = pkg_dir / module_name / "skills"
        yield EaPackageInfo(
            name=str(name),
            version=str(version),
            description=str(description),
            author=author,
            tier=_extract_tier(data),
            module_name=module_name,
            factory_target=factory_target or f"{module_name}:create_plugin",
            pkg_dir=pkg_dir,
            skills_dir=skills_dir if skills_dir.is_dir() else None,
        )
