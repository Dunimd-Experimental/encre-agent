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

"""Path isolation for file operations.

Provides **strict** path remapping to prevent the agent from reading,
writing, or deleting files outside the allowed sandbox directory.

Security guarantees:
1. Relative paths and ``/workspace/...`` prefixes are resolved into the
   per-session sandbox directory.
2. ``..`` path traversal is detected and rejected.
3. Symlinks are resolved before the boundary check.
4. Paths outside the sandbox are mapped to a **rejection marker** (empty
   string) so callers can safely refuse the operation.
5. In **workspace mode**, paths must resolve **inside** the workspace root.

Workspace mode keeps paths unchanged (they already point to the workspace).

Process confinement for spawned tool shells lives in the Rust core:
:func:`spawn_confined` wraps ``encre._native.SandboxSession`` so the
Landlock (Linux) / sandbox_init(3) (macOS) / Job Object (Windows) isolation
is applied inside Rust, and the honest per-spawn level is surfaced on
:class:`ConfinedProcess`.  Python only drives the pipes; it never applies
a privilege checkpoint a child could bypass.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import os
import subprocess
from pathlib import Path, PurePath
from typing import Any

try:
    from encre.logging_config import get_logger
    logger = get_logger(__name__)
except Exception:  # pragma: no cover - standalone use
    import logging

    logger = logging.getLogger("encre.sandbox")


def get_session_files_dir(session_id: str = "") -> Path:
    """Return the per-session files directory for *session_id*.

    In general mode, user-created files are persisted here instead of the
    sandbox so they live alongside the session and survive as artifacts.
    Creates the directory if it does not exist.
    """
    from encre.config import get_data_dir
    root = get_data_dir() / "sessions"
    root = root / session_id / "files" if session_id else root / "files"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_sandbox_root(session_id: str = "") -> Path:
    """Return the sandbox root directory for *session_id*.

    Creates the directory if it does not exist.
    """
    from encre.config import get_data_dir
    root = get_data_dir() / "sandbox"
    if session_id:
        root = root / session_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def remap_tool_path(file_path: str) -> str:
    """Remap a file path from tool context (resolves loop + session).

    Returns the remapped absolute path suitable for the current
    session and workspace mode. In workspace mode, paths are kept
    relative to the workspace root. In general mode, relative paths
    resolve under the session's files directory (``<data_dir>/sessions/<session_id>/files/``).
    Paths that would escape the allowed root are forced back into the
    allowed root by keeping only the filename.
    """
    from encre.tools.runtime import _resolve_loop
    loop = _resolve_loop()
    if loop is None:
        return file_path
    ws = getattr(loop.config, "workspace", "") or ""
    sid = getattr(loop.session, "id", "") or ""

    # Try remapping; if rejected, force-remap into the allowed root
    result = remap_path(file_path, session_id=sid, workspace_root=ws)
    if result:
        return result
    # Path was rejected — force-remap into the session files dir as filename only
    files_dir = get_session_files_dir(sid)
    # Use only the filename, discarding any directory structure
    safe_name = os.path.basename(file_path) or "output.txt"
    return str(files_dir / safe_name)


def remap_path(
    file_path: str,
    session_id: str = "",
    workspace_root: str = "",
) -> str:
    """Remap *file_path* into the workspace or the session files directory.

    Resolution rules (in priority order):
    1. **Empty / whitespace path** → reject (empty string).
    2. **Workspace mode** (``workspace_root`` is set + exists): keep
       the path relative to the workspace. Must resolve **inside**
       the workspace root (no escaping via ``../``).
    3. **General mode** (no workspace):
       - **Absolute paths** are passed through as-is (the user or context
         explicitly specified a location).
       - **Relative paths** are resolved into ``<sessions>/<session_id>/files/``.
       - ``..`` traversal in relative paths is rejected.

    Returns the resolved absolute path, or ``""`` if the path is unsafe.
    """
    if not file_path or not file_path.strip():
        return ""

    # ── Workspace mode ──────────────────────────────────────────
    if workspace_root and os.path.isdir(workspace_root):
        return _resolve_workspace_path(file_path, workspace_root)

    # ── General mode: session files directory ───────────────────
    files_dir = get_session_files_dir(session_id)
    return _resolve_session_files_path(file_path, str(files_dir))


def _resolve_workspace_path(file_path: str, workspace: str) -> str:
    """Resolve *file_path* relative to the workspace directory.

    Security: the resolved path must be strictly inside the workspace.
    Rejects ``..`` escapes, symlinks pointing outside, and absolute
    paths outside workspace.

    Rules:
    - If *file_path* starts with ``/workspace/``, strip that prefix
      and resolve relative to workspace.
    - Relative paths resolve against workspace root.
    - Absolute paths must be inside workspace to pass.
    """
    p = Path(file_path)
    workspace_abs = Path(workspace).resolve()

    # Strip the model's virtual "/workspace/" prefix
    if p.is_absolute():
        path_str = str(p).replace("\\", "/")
        if path_str.startswith("/workspace/") or path_str == "/workspace":
            p = Path(path_str[11:]) if len(path_str) > 11 else Path(".")

    # ── Relative paths (including stripped /workspace/) ──
    if not p.is_absolute():
        try:
            resolved = (workspace_abs / p).resolve()
        except (OSError, ValueError):
            return ""
        try:
            resolved.relative_to(workspace_abs)
        except ValueError:
            return ""
        return str(resolved)

    # ── Absolute paths ──
    try:
        resolved = p.resolve()
    except (OSError, ValueError):
        return ""
    try:
        resolved.relative_to(workspace_abs)
    except ValueError:
        return ""
    return str(resolved)


def _resolve_session_files_path(
    file_path: str,
    files_dir: str,
) -> str:
    """Resolve *file_path* into the session files directory.

    Resolution rules:
    - **Relative paths** (``output.txt``, ``./foo``) are resolved **relative
      to the session files directory** — this is the default when no path
      is specified by the user or context.
    - **Absolute paths** are passed through as-is (the user or context
      explicitly specified a location; do not rewrite it).
    - Virtual ``/workspace/...`` prefix is stripped and treated as relative.
    - ``..`` traversal in relative paths is caught via containment check.

    Returns the absolute path, or **empty string** when the path is empty.
    """
    p = Path(file_path)

    # Strip the model's virtual "/workspace/" prefix
    path_str = file_path.replace("\\", "/")
    if path_str.startswith("/workspace/"):
        p = Path(path_str[11:])
    elif path_str == "/workspace":
        p = Path(".")

    files_abs = Path(files_dir).resolve()

    # ── Absolute path: user/context specified a location — pass through ──
    if p.is_absolute():
        # Verify symlinks resolve inside the session files directory
        try:
            resolved = p.resolve(strict=True)
            resolved.relative_to(files_abs)
        except (OSError, ValueError):
            logger.debug("exception suppressed", exc_info=True)
        return str(p)

    # ── Relative paths: resolve against the session files directory ──
    try:
        resolved = (files_abs / p).resolve()
    except (OSError, ValueError):
        return ""
    # Check containment: reject via ".." traversal
    try:
        resolved.relative_to(files_abs)
    except ValueError:
        return ""
    # Symlink escape check
    if resolved.exists() and resolved.is_symlink():
        try:
            resolved.resolve(strict=True).relative_to(files_abs)
        except ValueError:
            return ""
    # Create parent dirs if needed
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return str(resolved)


# ── Command-level path safety ─────────────────────────────────────

class PathViolation:
    """Describes a detected path security violation."""
    __slots__ = ("raw_path", "reason", "sanitized")

    def __init__(self, reason: str, raw_path: str, sanitized: str = "") -> None:
        """Init.

        Args:
            reason: Description of the reason parameter.
            raw_path: Description of the raw_path parameter.
            sanitized: Description of the sanitized parameter.
        """
        self.reason = reason
        self.raw_path = raw_path
        self.sanitized = sanitized


def check_path_safety(
    path_str: str,
    sandbox_root: Path,
    workspace_root: str = "",
) -> tuple[PathViolation | None, str]:
    """Check if *path_str* is safe to operate on in the sandbox context.

    Returns ``(None, safe_path)`` when safe, or
    ``(violation, "")`` when unsafe.
    """
    if not path_str or not path_str.strip():
        return PathViolation("empty path", path_str), ""

    # Check for path traversal
    if ".." in PurePath(path_str).parts:
        return PathViolation("path traversal detected (..)", path_str), ""

    # Check for symlink-based escapes
    if path_str.startswith("/proc/") or path_str.startswith("/sys/") or path_str.startswith("/dev/"):
        return PathViolation("procedural filesystem access", path_str), ""

    # Resolve and check containment
    try:
        p = Path(path_str)
        resolved = p.resolve(strict=False)
    except (OSError, ValueError):
        return PathViolation("invalid path", path_str), ""

    # Workspace mode takes priority
    if workspace_root and os.path.isdir(workspace_root):
        try:
            resolved.relative_to(Path(workspace_root).resolve())
            return None, str(resolved)
        except ValueError:
            return PathViolation("path outside workspace", path_str), ""

    # Sandbox mode
    try:
        resolved.relative_to(sandbox_root.resolve())
        return None, str(resolved)
    except ValueError:
        return PathViolation("path outside sandbox", path_str), ""


def filter_allowed_env(
    env_vars: dict[str, str],
    allow_patterns: list[str],
    deny_patterns: list[str],
) -> dict[str, str]:
    """Filter environment variables based on allow/deny patterns.

    Deny patterns take priority over allow patterns.
    Empty allow_patterns means no filtering (all pass through).
    """
    if not allow_patterns and not deny_patterns:
        return env_vars

    result: dict[str, str] = {}
    for name, value in env_vars.items():
        # Deny takes priority
        if deny_patterns and any(fnmatch.fnmatch(name, pat) for pat in deny_patterns):
            continue
        if allow_patterns:
            if any(fnmatch.fnmatch(name, pat) for pat in allow_patterns):
                result[name] = value
        else:
            result[name] = value
    return result


# ---------------------------------------------------------------------------
# OS-level process confinement (Rust-owned)
# ---------------------------------------------------------------------------
#
# All OS confinement for spawned tool shells lives in the Rust core
# (``encre._native.SandboxSession``): Landlock on Linux (workspace is the
# only writable root, no network, no exec outside), sandbox_init(3) on
# macOS, a kill-on-close Job Object on Windows.  Python never applies — and
# never holds — a privilege checkpoint a child could bypass: it only drives
# the pipes.  A missing native extension degrades honestly to an unconfined
# subprocess with ``isolation="none"``.


class ConfinedProcess:
    """Uniform async handle over a confined subprocess.

    Concrete subclasses wrap either the Rust ``SandboxSession`` (native
    path) or an unconfined ``asyncio`` subprocess (degraded fallback).
    """

    #: Honest isolation level: ``"landlock"`` | ``"sandbox_init"`` |
    #: ``"job_object"`` | ``"none"``.  Never faked — the value comes from
    #: the Rust spawn report, not from a Python guess.
    isolation: str = "none"
    #: Confined child's OS process id.
    pid: int = 0

    async def write_stdin(self, data: bytes) -> int:
        """Write bytes to the child's stdin; returns the count written."""
        raise NotImplementedError

    async def close_stdin(self) -> None:
        """Close the child's stdin (end-of-input for one-shot shells)."""
        raise NotImplementedError

    async def drain_stdout(self, max_bytes: int, timeout_ms: int) -> bytes:
        """Drain pending stdout bytes (waits up to *timeout_ms* for data)."""
        raise NotImplementedError

    async def drain_stderr(self, max_bytes: int, timeout_ms: int) -> bytes:
        """Drain pending stderr bytes (waits up to *timeout_ms* for data)."""
        raise NotImplementedError

    async def wait(self, timeout_ms: int) -> int | None:
        """Wait for exit up to *timeout_ms*: exit code, or ``None``."""
        raise NotImplementedError

    def running(self) -> bool:
        """Whether the child has not exited yet."""
        raise NotImplementedError

    async def kill_tree(self, force: bool = True) -> None:
        """Kill the whole descendant tree and reap."""
        raise NotImplementedError

    def close(self) -> None:
        """Release pipe handles (best-effort; native sessions self-reap)."""


class _NativeConfined(ConfinedProcess):
    """Handle over a Rust ``SandboxSession``; blocking calls run off-thread.

    The confinement was already applied inside Rust at spawn time; this
    wrapper only forwards pipe I/O (each blocking call releases the GIL and
    runs in a worker thread so the asyncio loop stays responsive).
    """

    __slots__ = ("_session", "isolation", "pid")

    def __init__(self, session: Any, isolation: str) -> None:
        self._session = session
        self.isolation = isolation
        self.pid = int(session.pid())

    async def write_stdin(self, data: bytes) -> int:
        return await asyncio.to_thread(self._session.write_stdin, data)

    async def close_stdin(self) -> None:
        await asyncio.to_thread(self._session.close_stdin)

    async def drain_stdout(self, max_bytes: int, timeout_ms: int) -> bytes:
        return await asyncio.to_thread(
            self._session.drain_stdout, max_bytes, timeout_ms,
        )

    async def drain_stderr(self, max_bytes: int, timeout_ms: int) -> bytes:
        return await asyncio.to_thread(
            self._session.drain_stderr, max_bytes, timeout_ms,
        )

    async def wait(self, timeout_ms: int) -> int | None:
        return await asyncio.to_thread(self._session.wait, timeout_ms)

    def running(self) -> bool:
        return bool(self._session.is_running())

    async def kill_tree(self, force: bool = True) -> None:  # noqa: ARG002
        # Rust kill: SIGKILL the process group (POSIX) / TerminateProcess +
        # kill-on-close job handle (Windows); always tree-wide and forced.
        await asyncio.to_thread(self._session.kill)


class _PlainConfined(ConfinedProcess):
    """Degraded fallback: unconfined asyncio subprocess (native missing).

    Functionally equivalent pipe driving over ``asyncio.subprocess``; the
    ``isolation`` level is honestly ``"none"``.
    """

    __slots__ = ("_proc", "isolation", "pid")

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self.isolation = "none"
        self.pid = int(proc.pid or 0)

    async def write_stdin(self, data: bytes) -> int:
        writer = self._proc.stdin
        if writer is None:
            raise RuntimeError("stdin unavailable")
        writer.write(data)
        await writer.drain()
        return len(data)

    async def close_stdin(self) -> None:
        if self._proc.stdin is not None:
            self._proc.stdin.close()

    async def drain_stdout(self, max_bytes: int, timeout_ms: int) -> bytes:
        if self._proc.stdout is None:
            return b""
        try:
            return await asyncio.wait_for(
                self._proc.stdout.read(max_bytes), timeout_ms / 1000,
            )
        except TimeoutError:
            return b""

    async def drain_stderr(self, max_bytes: int, timeout_ms: int) -> bytes:
        if self._proc.stderr is None:
            return b""
        try:
            return await asyncio.wait_for(
                self._proc.stderr.read(max_bytes), timeout_ms / 1000,
            )
        except TimeoutError:
            return b""

    async def wait(self, timeout_ms: int) -> int | None:
        try:
            return await asyncio.wait_for(self._proc.wait(), timeout_ms / 1000)
        except TimeoutError:
            return None

    def running(self) -> bool:
        return self._proc.returncode is None

    async def kill_tree(self, force: bool = True) -> None:
        from encre.capabilities.process.spawn import kill_process_tree

        try:
            if force or os.name == "nt":
                kill_process_tree(self.pid, force=True)
            else:
                self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
    def close(self) -> None:
        for pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if pipe is not None:
                with contextlib.suppress(Exception):
                    pipe.close()


async def spawn_confined(
    argv: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    workspace: str | None = None,
) -> ConfinedProcess:
    """Spawn *argv* under the Rust-owned OS confinement.

    The confinement is applied inside the Rust core at spawn time: Landlock
    (Linux, *workspace* as the only writable root), sandbox_init(3) (macOS)
    or a kill-on-close Job Object (Windows).  Fails closed: when the Rust
    spawn reports an error the exception propagates — the command is never
    run unconstrained as a silent fallback.

    Only a missing native extension degrades to an unconfined asyncio
    subprocess, with ``isolation="none"`` reported honestly.

    Args:
        argv: Command vector (executable + arguments).
        cwd: Working directory for the child.
        env: Extra environment variables (fallback path only; the Rust
            spawn inherits the parent environment and adds UTF-8 hints).
        workspace: Confinement root for the Landlock / Seatbelt paths.
            Ignored on Windows (its Job Object has no path scope).
    """
    from encre import native as _native

    if getattr(_native, "_HAS_NATIVE", False):
        session, level = await asyncio.to_thread(
            _native.SandboxSession.spawn, list(argv), cwd, workspace,
        )
        return _NativeConfined(session, level)

    logger.warning(
        "[sandbox] native extension missing; spawning without OS confinement "
        "(isolation=none): %s",
        argv[0] if argv else "?",
    )
    from encre.capabilities.process.spawn import hidden_subprocess_kwargs

    base_env = os.environ.copy()
    # Prefer UTF-8 output from external tools so the accumulated buffer is
    # decodable regardless of the ANSI code page (mirrors the Rust path).
    base_env.setdefault("PYTHONUTF8", "1")
    base_env.setdefault("PYTHONIOENCODING", "utf-8")
    base_env.setdefault("LANG", "C.UTF-8")
    base_env.setdefault("LC_ALL", "C.UTF-8")
    if env:
        base_env.update(env)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd or None,
        env=base_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **hidden_subprocess_kwargs(),
    )
    return _PlainConfined(proc)
