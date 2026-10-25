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

"""Centralised path validation for builtin file tools.

Every file-family tool (``file_read`` / ``file_write`` / ``file_edit``)
funnels its path argument through :func:`guard_tool_path` before touching
the filesystem.  The guard performs, in order:

1. Normalisation: ``~`` expansion, virtual ``/workspace/`` prefix
   stripping, ``..`` traversal rejection and symlink-resolving
   ``resolve()`` (Windows backslash compatible).
2. Workspace containment: the resolved path must live inside one of the
   allowed workspace roots.  Explicit user-authorized paths from
   ``permissions.extra_paths`` (config) are the only exemption.
3. Dangerous write blacklist (write operations only): system directories
   (Windows ``C:\\Windows`` / ``Program Files`` and POSIX ``/etc`` etc.)
   plus the immune path fragments from :mod:`encre.safety`
   (``.git/``, ``.ssh/``, shell rc files, ...).

Relative / virtual paths are resolved with the existing
:func:`encre.capabilities.process.sandbox.remap_path` so the historical
session-files-dir behaviour is preserved; this module adds the explicit
rejection verdicts on top instead of duplicating the resolution logic.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from encre.capabilities.process.sandbox import remap_path
from encre.tools.runtime import _resolve_loop

#: Virtual prefix some models emit instead of the real workspace path.
_VIRTUAL_WORKSPACE_PREFIX = "/workspace/"

# Protected system directories (first path component, lowercased) that
# write operations must never touch.  Windows entries match drive-root
# components (``C:\\Windows``, ``C:\\Program Files``); POSIX entries match
# the root-level component (``/etc``, ``/boot``, ...).
_WINDOWS_PROTECTED_DIRS = frozenset({
    "windows", "program files", "program files (x86)", "programdata",
})
_POSIX_PROTECTED_DIRS = frozenset({
    "etc", "boot", "usr", "bin", "sbin", "lib", "lib64",
    "dev", "proc", "sys",
})


@dataclass(frozen=True)
class PathGuardResult:
    """Outcome of a path validation.

    ``allowed`` is ``False`` when the operation must be refused and
    ``reason`` carries a ready-to-use error message fragment.  When
    allowed, ``resolved`` is the normalized absolute path to operate on.
    """

    allowed: bool
    reason: str = ""
    resolved: str = ""


def _iter_workspace_roots() -> list[str]:
    """Resolve the allowed workspace roots from the active loop config."""
    loop = _resolve_loop()
    if loop is None:
        return []
    ws = str(getattr(loop.config, "workspace", "") or "")
    return [ws] if ws and os.path.isdir(ws) else []


def _extra_paths(loop: Any = None) -> list[str]:
    """Load user-authorized exemption paths from ``permissions.extra_paths``.

    Each entry is ``~``-expanded and resolved to an absolute path; entries
    that cannot be resolved are skipped so a typo never widens the sandbox.
    """
    loop = loop if loop is not None else _resolve_loop()
    if loop is None:
        return []
    perms = getattr(loop.config, "permissions", None)
    raw = getattr(perms, "extra_paths", None)
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            continue
        try:
            out.append(str(Path(os.path.expanduser(entry.strip())).resolve()))
        except (OSError, ValueError):
            continue
    return out


def _strip_virtual_prefix(path: str) -> str:
    """Strip the model's virtual ``/workspace/`` prefix, if present."""
    normalized = path.replace("\\", "/")
    if normalized.startswith(_VIRTUAL_WORKSPACE_PREFIX):
        return normalized[len(_VIRTUAL_WORKSPACE_PREFIX):]
    if normalized == _VIRTUAL_WORKSPACE_PREFIX.rstrip("/"):
        return "."
    return path


def _is_under(child: Path, root: Path) -> bool:
    """Return True when *child* equals or lives below *root*."""
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


def _matches_extra(resolved: Path, extra: list[str]) -> bool:
    """Return True when *resolved* falls under an explicit exemption path."""
    return any(
        resolved == Path(raw) or _is_under(resolved, Path(raw)) for raw in extra
    )


def _write_blacklist_hit(resolved: Path) -> str:
    """Return the protected-area name a write target hits, or ``""``."""
    # Immune fragments (.git/, .claude/, .ssh/, shell rc files, ...) reuse
    # the safety engine's always-deny list so both layers stay in sync.
    from encre.safety import matches_immune_path  # lazy: keep tool import light

    hit = matches_immune_path(str(resolved))
    if hit:
        return f"immune path ({hit})"
    parts = [p for p in resolved.parts if p != resolved.anchor]
    first = parts[0].lower() if parts else ""
    if resolved.drive:
        if first in _WINDOWS_PROTECTED_DIRS:
            return f"Windows system directory ({first})"
    elif first in _POSIX_PROTECTED_DIRS:
        return f"system directory (/{first})"
    return ""


def validate_path(
    path: str,
    *,
    tool: str,
    for_write: bool,
    workspace_roots: list[str],
    extra_paths: list[str] | None = None,
) -> PathGuardResult:
    """Validate *path* for a *tool* operation against *workspace_roots*.

    Checks normalization, ``..`` traversal, symlink-resolved workspace
    containment, the ``extra_paths`` exemption list and (for write
    operations) the dangerous-path blacklist.  Returns a
    :class:`PathGuardResult` whose ``reason`` is directly usable in tool
    error messages.  With empty *workspace_roots* (general mode) absolute
    paths pass through and only the write blacklist applies, mirroring the
    historical remap semantics.
    """
    if not path or not path.strip():
        return PathGuardResult(False, f"{tool}: path is required", "")

    roots: list[Path] = []
    for root in workspace_roots:
        try:
            roots.append(Path(root).resolve())
        except (OSError, ValueError):
            continue

    raw = _strip_virtual_prefix(path.strip().strip("'\""))
    if ".." in raw.replace("\\", "/").split("/"):
        return PathGuardResult(False, f"{tool}: path traversal rejected ('..' in {path!r})", "")

    try:
        expanded = os.path.expanduser(raw)
        resolved = (
            (roots[0] / expanded).resolve()
            if roots and not os.path.isabs(expanded)
            else Path(expanded).resolve()
        )
    except (OSError, ValueError):
        return PathGuardResult(False, f"{tool}: invalid path {path!r}", "")

    if roots:
        extra = extra_paths if extra_paths is not None else _extra_paths()
        contained = any(_is_under(resolved, r) for r in roots)
        if not contained and not _matches_extra(resolved, extra):
            return PathGuardResult(
                False,
                f"{tool}: path '{resolved}' is outside the workspace roots; "
                "grant access explicitly via permissions.extra_paths if intended",
                "",
            )

    if for_write:
        hit = _write_blacklist_hit(resolved)
        if hit:
            return PathGuardResult(
                False,
                f"{tool}: refused, '{resolved}' is a protected {hit} "
                "and cannot be written or deleted",
                "",
            )

    return PathGuardResult(True, "", str(resolved))


def guard_tool_path(path: str, *, tool: str, for_write: bool) -> PathGuardResult:
    """Single entry point used by the builtin file tools.

    Resolves relative / virtual paths into the allowed root (workspace
    mode) or the session files directory (general mode) via the existing
    :func:`remap_path`, then runs the full :func:`validate_path` checks.
    A remap rejection falls through to :func:`validate_path` so an
    explicit ``permissions.extra_paths`` exemption can still authorise
    the path; without one the rejection reason is returned.
    """
    loop = _resolve_loop()
    if loop is None:
        # No active loop: keep the historical pass-through semantics and
        # still apply the write blacklist.
        return validate_path(path, tool=tool, for_write=for_write, workspace_roots=[])

    sid = str(getattr(loop.session, "id", "") or "")
    roots = _iter_workspace_roots()
    extra = _extra_paths(loop)

    # Workspace mode resolves relative / virtual paths against the
    # workspace root; general mode resolves them into the session files
    # directory while absolute paths pass through (historical behaviour).
    pre = remap_path(
        path, session_id=sid,
        workspace_root=roots[0] if roots else "",
    )
    if pre:
        return validate_path(
            pre, tool=tool, for_write=for_write,
            workspace_roots=roots, extra_paths=extra,
        )
    # remap_path rejected the path (workspace escape / traversal): give
    # validate_path a chance to honour an explicit exemption, else reject.
    return validate_path(
        path, tool=tool, for_write=for_write,
        workspace_roots=roots, extra_paths=extra,
    )


def resolve_write_target(path: str, session: Any = None) -> str:
    """The on-disk path a tool reaches for when handed *path*.

    Rollback stores pre-write snapshots keyed by this value, so capture and
    restore must compute it the same way the file tools do -- through
    :func:`guard_tool_path`'s resolution chain (virtual prefix, ``~``,
    workspace root or session files dir, symlink-resolving ``resolve()``).
    A model that says ``notes.txt`` and a workspace at ``D:\\proj`` therefore
    snapshot and restore ``D:\\proj\\notes.txt``, not a same-named file under
    the server's working directory.

    Returns ``""`` when the path is empty or would land outside the allowed
    roots: the tool would have refused the write, so there is nothing to
    snapshot.
    """
    raw = (path or "").strip().strip("'\"")
    if not raw:
        return ""
    loop = _resolve_loop()
    src = session if session is not None else loop
    cfg = getattr(src, "config", None)
    sid = str(
        getattr(src, "id", "")
        or getattr(getattr(loop, "session", None), "id", "")
        or ""
    )
    ws = str(getattr(cfg, "workspace", "") or "") if cfg is not None else ""
    roots = [ws] if ws and os.path.isdir(ws) else []
    try:
        extra = _extra_paths(session if session is not None else loop)
    except Exception:
        extra = []
    try:
        pre = remap_path(raw, session_id=sid, workspace_root=roots[0] if roots else "")
    except Exception:
        pre = ""
    try:
        verdict = validate_path(
            pre or raw, tool="path_guard", for_write=False,
            workspace_roots=roots, extra_paths=extra,
        )
    except Exception:
        return os.path.abspath(os.path.expanduser(pre or raw))
    return verdict.resolved if verdict.allowed else ""


__all__ = ["PathGuardResult", "guard_tool_path", "resolve_write_target", "validate_path"]
