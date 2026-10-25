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

"""Encre data lifecycle -- single source of truth for on-disk layout and deletion.

Why this module exists
======================

Every feature that touched the data directory used to invent its own path
helper *and* its own delete logic::

    sessions/<sid>/            session_manager.delete_session_from_disk()
    telemetry/<sid>.jsonl      (never deleted)
    rollback/<sid>/            (never deleted)
    sub_agents/<sid>/          (never deleted)
    spillover/<sid>/           (never deleted)
    iwork/<ws_id>/             workspaces._h_remove_workspace()

The result was five independent places accumulating orphans, and a workspace
folder that outlived its own deletion because the index was cleared while the
directory was left behind.

Contract
========

* :func:`session_paths` / :func:`workspace_paths` enumerate **every** path an
  entity owns.  **New per-session storage must be registered here** -- if it is
  not, :func:`purge_session` will not clean it and it becomes an orphan.
* :func:`purge_session` / :func:`purge_workspace` delete everything at once.
* :func:`purge_orphans` scans the data tree for data whose owner no longer
  exists (disk facts only -- an index entry is not required).

Scope: this module touches **only the filesystem**.  Callers stay responsible
for pulling the in-memory object out of their own caches and for rewriting the
indices (``SessionManager``, ``workspaces`` handler).
"""


import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from encre.config import get_data_dir

logger = logging.getLogger("encre.lifecycle")

__all__ = [
    "PurgeReport",
    "SessionPaths",
    "WorkspacePaths",
    "purge_orphans",
    "purge_session",
    "purge_workspace",
    "rollback_root",
    "legacy_rollback_root",
    "session_paths",
    "workspace_paths",
]

# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------

#: Rollback object store root -- now inside the main data tree.  It used to
#: live in a *second* root (``~/.encre``), which is exactly why it was absent
#: from backups and invisible to :func:`purge_session`: deleting a session
#: never touched it, so the directory grew without bound.  The legacy path is
#: kept only so :func:`purge_orphans` can sweep whatever is left there.
_LEGACY_ROLLBACK_ROOT = Path("~/.encre/rollback").expanduser()


def rollback_root() -> Path:
    """Return the rollback object-store root (single source of truth)."""
    return get_data_dir("rollback", ensure=False)


def legacy_rollback_root() -> Path:
    """Return the pre-migration rollback root (sweep target only)."""
    return _LEGACY_ROLLBACK_ROOT


# ---------------------------------------------------------------------------
# Path enumeration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionPaths:
    """Every on-disk path owned by a single session.

    This is the *only* place a per-session path may be constructed.  Adding a
    new per-session store without adding a field here is what turns data into
    orphans -- see the module docstring.
    """

    session_id: str
    #: ``sessions/<sid>/`` -- holds ``meta.json``, ``turn_*.json``, ``session.db``.
    dir: Path
    #: ``telemetry/<sid>.jsonl``
    telemetry: Path
    #: ``rollback/<sid>/`` (object store for conversation rollback)
    rollback: Path
    #: ``sub_agents/<sid>/``
    sub_agents: Path
    #: ``spillover/<sid>/`` (oversized tool output)
    spillover: Path

    def owned(self) -> tuple[Path, ...]:
        """Return every path that must disappear with this session."""
        return (self.dir, self.telemetry, self.rollback, self.sub_agents, self.spillover)


@dataclass(frozen=True)
class WorkspacePaths:
    """Every on-disk path owned by a registered workspace."""

    ws_id: str
    #: ``iwork/<ws_id>/`` -- holds ``icon.*``, ``sessions/``, index caches.
    dir: Path

    def owned(self) -> tuple[Path, ...]:
        return (self.dir,)


def session_paths(session_id: str) -> SessionPaths:
    """Resolve every path owned by *session_id* under the data directory."""
    data = get_data_dir()
    return SessionPaths(
        session_id=session_id,
        dir=data / "sessions" / session_id,
        telemetry=data / "telemetry" / f"{session_id}.jsonl",
        rollback=rollback_root() / session_id,
        sub_agents=data / "sub_agents" / session_id,
        spillover=data / "spillover" / session_id,
    )


def workspace_paths(ws_id: str) -> WorkspacePaths:
    """Resolve every path owned by workspace *ws_id* under the data directory."""
    return WorkspacePaths(ws_id=ws_id, dir=get_data_dir() / "iwork" / ws_id)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass
class PurgeReport:
    """Outcome of a purge operation -- inspectable by callers and tests."""

    removed: list[Path] = field(default_factory=list)
    bytes_freed: int = 0
    failed: list[tuple[Path, str]] = field(default_factory=list)
    dry_run: bool = False

    def merge(self, other: "PurgeReport") -> None:
        self.removed.extend(other.removed)
        self.bytes_freed += other.bytes_freed
        self.failed.extend(other.failed)

    def __bool__(self) -> bool:
        """True when something was (or, in dry-run, would be) removed."""
        return bool(self.removed)

    def __len__(self) -> int:
        return len(self.removed)

    def describe(self) -> str:
        verb = "would remove" if self.dry_run else "removed"
        text = "%s %d path(s), %.1f KB" % (verb, len(self.removed), self.bytes_freed / 1024)
        if self.failed:
            text += "; %d failure(s)" % len(self.failed)
        return text


# ---------------------------------------------------------------------------
# Removal primitives
# ---------------------------------------------------------------------------


def _path_size(path: Path) -> int:
    """Best-effort size of a file or directory tree."""
    try:
        if path.is_file() and not path.is_symlink():
            return path.stat().st_size
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


def _remove(path: Path, report: PurgeReport) -> None:
    """Delete *path* (file or directory); failures are recorded, never raised.

    A failed removal must not abort the whole purge -- partial cleanup is still
    strictly better than none, and the caller gets an explicit failure list.
    """
    if not path.exists() and not path.is_symlink():
        return
    size = _path_size(path)
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
            if path.exists():
                report.failed.append((path, "rmtree left residue behind"))
                return
        else:
            path.unlink()
        report.removed.append(path)
        report.bytes_freed += size
    except Exception as exc:  # noqa: BLE001 -- cleanup must not raise
        report.failed.append((path, str(exc)[:200]))
        logger.warning("lifecycle: failed to remove %s", path, exc_info=True)


def _plan(path: Path, report: PurgeReport) -> None:
    """Record *path* as a removal candidate without touching the disk (dry-run)."""
    report.removed.append(path)
    report.bytes_freed += _path_size(path)


def _emit(path: Path, report: PurgeReport, dry_run: bool) -> None:
    _plan(path, report) if dry_run else _remove(path, report)


# ---------------------------------------------------------------------------
# Purge entry points
# ---------------------------------------------------------------------------


def purge_session(session_id: str, sessions_dir: str | Path | None = None) -> PurgeReport:
    """Delete a session and **all** of its associated storage.  Idempotent.

    Covers ``sessions/``, ``telemetry/``, ``rollback/``, ``sub_agents/`` and
    ``spillover/`` in one shot -- previously only the first was cleaned.

    Args:
        session_id: The session to purge.
        sessions_dir: Directory *containing* the session folder, when the
            session does not live in the default ``<data_dir>/sessions``
            (workspace sessions live under ``iwork/<ws_id>/sessions``).
            The derived stores (telemetry / rollback / sub_agents / spillover)
            are keyed by session id and are always shared, so they are cleaned
            regardless of which directory the session itself came from.

    The caller still owns the in-memory side: drop the object from caches and
    strip it from every index (see ``SessionManager.delete_session_from_disk``).
    """
    from dataclasses import replace

    paths = session_paths(session_id)
    if sessions_dir:
        paths = replace(paths, dir=Path(sessions_dir) / session_id)

    report = PurgeReport()
    for path in paths.owned():
        _remove(path, report)
    if report.removed:
        logger.info(
            "purge_session %s: %s", session_id[:8], report.describe()
        )
    return report


def purge_workspace(ws_id: str) -> PurgeReport:
    """Delete every on-disk artefact of workspace *ws_id*.

    Removes the whole ``iwork/<ws_id>/`` tree -- icon, session directories and
    index caches -- so no empty folder is left behind after the registry entry
    is dropped.  Deregistering from ``iwork/index.json`` is the caller's job.
    """
    report = PurgeReport()
    for path in workspace_paths(ws_id).owned():
        _remove(path, report)
    if report.removed:
        logger.info("purge_workspace %s: %s", ws_id, report.describe())
    return report


def _live_session_ids(data: Path) -> set[str]:
    """Session ids that still own a valid directory (containing ``meta.json``)."""
    sessions_root = data / "sessions"
    if not sessions_root.is_dir():
        return set()
    return {d.name for d in sessions_root.iterdir() if d.is_dir() and (d / "meta.json").exists()}


def _registered_workspace_ids(data: Path) -> set[str] | None:
    """Workspace ids present in ``iwork/index.json``.

    Returns ``None`` when the registry cannot be read -- the caller must then
    refrain from deleting workspace directories, because an unreadable registry
    is *not* evidence that a workspace was removed.
    """
    index = data / "iwork" / "index.json"
    if not index.exists():
        return None
    try:
        from encre.crypto import decrypt

        raw = index.read_text(encoding="utf-8").strip()
        items = json.loads(decrypt(raw)) if raw else []
        return {
            str(w["id"])
            for w in items
            if isinstance(w, dict) and w.get("id")
        }
    except Exception:  # noqa: BLE001
        logger.warning("lifecycle: cannot read %s", index, exc_info=True)
        return None


def _iter_entries(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return ()
    return sorted(root.iterdir())


def purge_orphans(dry_run: bool = True) -> PurgeReport:
    """Find (and optionally delete) data whose owning session/workspace is gone.

    Orphan detection is based on **disk facts**, not on index entries:

    * a ``sessions/<sid>/`` directory without ``meta.json`` is a shell left by
      a half-finished deletion;
    * ``telemetry/<sid>.jsonl``, ``sub_agents/<sid>/``, ``spillover/<sid>/``
      and ``rollback/<sid>/`` are orphans when ``sessions/<sid>/`` is gone;
    * ``iwork/<ws_id>/`` is an orphan when the id is absent from the registry
      (skipped entirely if the registry cannot be read).

    Defaults to ``dry_run=True``: the caller must opt in to actual deletion.
    """
    data = get_data_dir()
    report = PurgeReport(dry_run=dry_run)

    live = _live_session_ids(data)

    # 1) Session directories that are not real sessions (no meta.json).
    #    Directories only -- the sessions root also holds ``index.json``, the
    #    directory index, which must never be mistaken for a session.
    sessions_root = data / "sessions"
    if sessions_root.is_dir():
        for entry in sorted(sessions_root.iterdir()):
            if entry.is_dir() and entry.name not in live:
                _emit(entry, report, dry_run)

    # 2) Per-session derived storage whose owner is gone.  Each root has its
    #    own shape: telemetry holds one *file* per session, the others hold one
    #    *directory* per session -- and a root may carry its own index file
    #    (``rollback/index.json``) that belongs to nobody and is skipped.
    for root, kind in (
        (data / "telemetry", "file"),
        (data / "sub_agents", "dir"),
        (data / "spillover", "dir"),
        (rollback_root(), "dir"),
    ):
        for entry in _iter_entries(root):
            if kind == "file":
                if not entry.is_file():
                    continue
                sid = entry.stem
            else:
                if not entry.is_dir():
                    continue
                sid = entry.name
            if sid not in live:
                _emit(entry, report, dry_run)

    # 3) Workspace directories with no registry entry.
    registered = _registered_workspace_ids(data)
    if registered is not None:
        for entry in _iter_entries(data / "iwork"):
            if entry.is_dir() and entry.name not in registered:
                _emit(entry, report, dry_run)

    logger.info("purge_orphans(dry_run=%s): %s", dry_run, report.describe())
    return report
