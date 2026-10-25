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

All packages install into exactly ONE directory: ``<data_root>/plugins``
(``~/.dunimd/encre/plugins`` by default, relocatable via ``ENCRE_DATA_DIR``;
the application installer's deployment target).  Discovery scans only that
directory — there are no dev-tree or frozen-bundle fallbacks.  The only
tools living outside it are the mandatory system tools hardcoded in the
harness itself.  If a package is not already importable, its directory is
prepended to ``sys.path`` (no pip install step required).

Both run modes materialize the shipped packages into the install directory
before scanning (:func:`_ensure_deployed`): a frozen app carries them as
PyInstaller data (``<bundle>/ea_tools``), a dev checkout keeps them at
``<repo>/core/ea_tools``.  Deployment is idempotent (fingerprint stamp) and
best-effort; the scan itself never leaves the install directory.

Each package's ``pyproject.toml`` is the single source of truth for metadata
(``name``, ``version``, ``authors``, ``description``) and carries an optional
``[tool.ea]`` table for EA-specific fields — most importantly ``tier``:

* ``mandatory``      — hardcoded core tools, cannot be uninstalled
* ``system-default`` — ships with the app, can be uninstalled/rebuilt
* ``user`` (default) — third-party, installed externally via pip
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
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
    #: Verbatim ``[tool.ea]`` table from pyproject.toml.  Carries the
    #: ``tier`` plus optional ``transport`` / ``endpoint`` /
    #: ``resource_limits`` / ``permissions`` / ``activation_events`` that the
    #: registry mirrors onto the manifest at discovery time.
    tool_ea: dict[str, Any] = field(default_factory=dict)


def packaged_skills(pkg: EaPackageInfo) -> list[str]:
    """Skill names a package actually ships, read off its own files.

    ``[tool.ea] provides_skills`` is optional and the official packages leave
    it out, yet each one carries ``<module>/skills/<name>/SKILL.md`` on disk.
    The directory is the evidence, so the registry can state what the plugin
    provides — and the market can show it — without anybody declaring it twice
    or guessing.  Empty when the package has no skills directory.
    """
    if pkg.skills_dir is None:
        return []
    try:
        found = [c.name for c in pkg.skills_dir.iterdir() if c.is_dir() and (c / "SKILL.md").is_file()]
        # Flat skill documents (``skills/<name>.md``) are first-class skills
        # too — the tool packages ship their use-instructions that way, and a
        # package can carry several.  Leaving them out made a multi-skill
        # package look like it had one skill.
        found += [
            f.stem for f in pkg.skills_dir.iterdir()
            if f.is_file() and f.suffix.lower() == ".md" and f.name != "MEMORY.md"
        ]
    except OSError:
        return []
    return sorted(set(found))


def _plugin_install_root() -> Path:
    """Return the unique plugin install directory.

    ``<data_root>/plugins`` is the single deployment target: the
    application installer puts every packaged plugin (``ea-tool-*`` /
    ``ea-skill-*`` bundles, tool packages, skills) there, and discovery
    scans only this directory.  The only tools living outside it are the
    mandatory system tools hardcoded in the harness itself.

    Resolved through :func:`encre.paths.get_data_dir` (the single source
    of truth for the data tree) so an ``ENCRE_DATA_DIR`` override keeps
    the WS ``plugin_install`` target and this scan root in lockstep — a
    hand-rolled ``~/.dunimd/encre`` here used to diverge from the WS
    handler whenever the env var was set (containers / CI / multi-instance).
    """
    from encre.paths import get_data_dir

    return get_data_dir("plugins")


def _bundled_source() -> Path | None:
    """Locate the read-only plugin packages shipped with the application.

    Frozen builds: ``<bundle>/ea_tools`` (PyInstaller data, ``sys._MEIPASS``
    already points at the ``_internal`` contents directory).  Dev checkouts:
    ``<repo>/core/ea_tools`` next to the source tree.  A pip-installed
    library carries no bundle, so ``None`` is returned and discovery falls
    back to entry points only.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate = Path(meipass) / _EA_TOOLS_DIRNAME
            if candidate.is_dir():
                return candidate
        return None
    repo_root = Path(__file__).resolve().parents[3]
    candidate = repo_root / "core" / _EA_TOOLS_DIRNAME
    return candidate if candidate.is_dir() else None


def _walk_files(root: Path) -> list[tuple[str, int, int]]:
    """List ``(relpath, mtime_ns, size)`` for the package tree, cheaply.

    ``Path.rglob`` + ``is_file()`` cost ~10x more than one ``scandir`` sweep
    on Windows (pathlib globs re-check every match); this runs before every
    discovery, so the walk itself must not dominate startup.
    """
    found: list[tuple[str, int, int]] = []
    base = str(root)
    stack = [base]
    while stack:
        directory = stack.pop()
        try:
            iterator = os.scandir(directory)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in ("__pycache__", "dist") or entry.name.endswith(".egg-info"):
                            continue
                        stack.append(entry.path)
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                rel = os.path.relpath(entry.path, base).replace(os.sep, "/")
                found.append((rel, st.st_mtime_ns, st.st_size))
    found.sort()
    return found


def _source_fingerprint(source: Path) -> str:
    """Fingerprint the bundled package tree (names + file mtimes/sizes).

    Frozen bundles take a cheap top-level path: the bundle is immutable for
    the process lifetime and an application update recreates the directory
    tree, so the root's own timestamps plus the package-name list detect
    every realistic change without stat-ing every file.  Dev checkouts keep
    the deep scan: in-place file edits do not change directory mtimes and
    dev redeployment must see them.
    """
    if getattr(sys, "frozen", False):
        try:
            st = source.stat()
            names = ",".join(sorted(p.name for p in source.iterdir()))
            return hashlib.sha1(f"frozen:{st.st_mtime_ns}:{st.st_ctime_ns}:{names}".encode()).hexdigest()
        except OSError:
            pass  # fall through to the deep scan
    digest = hashlib.sha1()
    for rel, mtime_ns, size in _walk_files(source):
        digest.update(f"{rel}\0{mtime_ns}\0{size}\n".encode())
    return digest.hexdigest()


#: Per-process guard: the deployment check (fingerprint of the bundled
#: tree) runs once per process.  Discovery calls :func:`ea_tools_root`
#: (and therefore this) several times per startup; the bundled source is
#: immutable while the process is alive — a frozen bundle never changes,
#: and dev-tree edits are only picked up on the next start — so every
#: re-check re-paid the full stat cost for nothing.
_DEPLOY_CHECKED = False


def _ensure_deployed(root: Path) -> None:
    """Materialize bundled plugin packages into the install directory.

    This is *deployment*, not discovery: the unique install dir remains the
    only place the scanner ever looks.  A fingerprint stamp skips the copy
    when the shipped tree has not changed.  Best-effort — any failure means
    nothing was deployed, never a crash of the importing application.

    Removal is part of deployment.  The install dir also holds plugins the
    user installed from the market, so pruning cannot be "delete whatever is
    not in the bundle" -- it deletes exactly the packages a previous bundle
    deployed and this bundle no longer ships, tracked in ``.bundled.names``.
    Without that ledger a package removed from ``ea_tools/`` would stay active
    forever in every install, shadowing the builtin of the same tool name.
    """
    global _DEPLOY_CHECKED
    if _DEPLOY_CHECKED:
        return
    _DEPLOY_CHECKED = True
    source = _bundled_source()
    if source is None:
        return
    try:
        root.mkdir(parents=True, exist_ok=True)
        stamp = root / ".bundled.stamp"
        ledger = root / ".bundled.names"
        fingerprint = _source_fingerprint(source)
        shipped = {p.name for p in source.iterdir() if p.is_dir()}
        if stamp.is_file() and stamp.read_text(encoding="utf-8") == fingerprint:
            return
        for pkg in source.iterdir():
            if pkg.is_dir():
                target = root / pkg.name
                # Replace, do not merge.  ``copytree(dirs_exist_ok=True)``
                # leaves files that were deleted or renamed upstream behind
                # (a stale ``tool-computer_use.md`` next to the current
                # ``tool-computer.md`` kept being registered as a skill), and
                # a bundled package is a mirror of the shipped tree, never a
                # place for local edits.
                try:
                    if target.is_dir():
                        shutil.rmtree(target)
                    shutil.copytree(
                        pkg, target,
                        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
                    )
                except Exception as exc:
                    logger.debug("Could not deploy %s: %s", pkg.name, exc)
        previous = _read_ledger(ledger)
        retired = sorted(name for name in previous if name not in shipped)
        for name in retired:
            stale = root / name
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
        ledger.write_text("\n".join(sorted(shipped)), encoding="utf-8")
        stamp.write_text(fingerprint, encoding="utf-8")
        if retired:
            logger.info(
                "Undeployed %d bundled package(s) no longer shipped: %s",
                len(retired), ", ".join(retired[:8]) + (" ..." if len(retired) > 8 else ""),
            )
        else:
            logger.info("Deployed bundled plugin packages to %s", root)
    except Exception as exc:
        logger.debug("Bundled plugin deployment skipped: %s", exc)


def _read_ledger(path: Path) -> set[str]:
    """Names this function previously deployed; empty on the first run.

    A pre-existing install has no ledger, so its leftovers are unknowns: the
    first run records what the bundle ships today and prunes only from then on,
    rather than guessing which directories the user installed themselves.
    """
    try:
        if path.is_file():
            return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    except Exception:
        logger.debug("Unreadable bundled ledger %s", path, exc_info=True)
    return set()


def ea_tools_root() -> Path:
    """Return the directory containing installed plugin packages.

    Always ``<data_root>/plugins`` (``~/.dunimd/encre/plugins`` by default,
    relocatable via ``ENCRE_DATA_DIR``) — the unique install location.  On
    the way there, packages shipped with the application (frozen bundle or
    dev checkout) are deployed into it if needed.  The path is returned
    even when it does not exist yet; the scanner treats a missing directory
    as "nothing installed" (entry-point discovery only).
    """
    root = _plugin_install_root()
    _ensure_deployed(root)
    return root


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


def _extract_tool_ea(data: dict[str, Any]) -> dict[str, Any]:
    """Return the verbatim ``[tool.ea]`` table (empty dict when absent)."""
    tool_ea = data.get("tool", {}).get("ea", {})
    return tool_ea if isinstance(tool_ea, dict) else {}


def _extract_tier(data: dict[str, Any]) -> str:
    """Read ``[tool.ea] tier``; bundled packages default to system-default."""
    tool_ea = _extract_tool_ea(data)
    if tool_ea.get("tier") in (TIER_MANDATORY, TIER_SYSTEM_DEFAULT, TIER_USER):
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

    ``pkg_dir`` is appended to ``sys.path`` so the top-level package it
    contains (``pkg_dir/<module_name>/``) becomes importable.  Appending —
    not prepending — is deliberate: a plugin-controlled directory at
    ``sys.path[0]`` could shadow stdlib or core modules (a stray
    ``logging.py`` would hijack the whole process) for every later import.
    Module names are unique ``ea_*`` identifiers, so tail placement never
    mis-resolves them.  Existence is verified with a single filesystem
    probe rather than ``importlib.util.find_spec`` — the latter walks every
    ``sys.path`` entry (~170 ``stat`` calls per package), which dominated
    discovery time across hundreds of packages.
    """
    pkg_dir_str = str(pkg_dir)
    if pkg_dir_str not in sys.path:
        sys.path.append(pkg_dir_str)
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
        # ea-tool-/ea-skill-: bundled or zip-installed packages;
        # ea-plugin-: central-repository wheels unpacked by the market
        # installer (encre.market_catalog), laid out like any EA package.
        if not pkg_dir.is_dir() or not pkg_dir.name.startswith(
                ("ea-tool-", "ea-skill-", "ea-plugin-", "ea-connector-")):
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
            tool_ea=_extract_tool_ea(data),
        )
