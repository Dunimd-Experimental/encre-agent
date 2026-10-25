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

"""Gateway status and process utilities.

Provides:
- Scoped identity locking for platform adapters (IRC nick, LINE token, etc.)
- Process PID liveness and start-time verification (WhatsApp bridge cleanup)
- Runtime status parsing for /api/status and /v1/ready endpoints
"""


import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scoped identity locks
# ---------------------------------------------------------------------------

# Process-wide lock table: platform -> {lock_key -> threading.Event}
# Used to prevent two adapter instances from claiming the same identity
# (e.g. IRC nickname, LINE channel access token) within the same process.
_lock_table: dict[str, dict[str, threading.Event]] = {}
_table_lock = threading.Lock()


def _get_lock(platform: str, lock_key: str) -> threading.Event:
    """Get or create a per-identity lock for the given platform."""
    with _table_lock:
        if platform not in _lock_table:
            _lock_table[platform] = {}
        if lock_key not in _lock_table[platform]:
            _lock_table[platform][lock_key] = threading.Event()
        return _lock_table[platform][lock_key]


def acquire_scoped_lock(platform: str, lock_key: str, timeout: float = 10) -> bool:
    """Try to acquire an exclusive identity lock.

    Two adapter instances sharing the same identity (e.g. IRC nick on one
    network, LINE token) must not run concurrently — the server will reject
    or flap the duplicate connection. This lock is process-wide because the
    competing instances are always on the same host.

    Args:
        platform: Platform tag (e.g. ``"irc"``, ``"line"``).
        lock_key: Identity string (e.g. ``"server:9000:nick"``, token hash).
        timeout: Seconds to wait before giving up.

    Returns:
        ``True`` if the lock was acquired, ``False`` if another instance holds it.
    """
    ev = _get_lock(platform, lock_key)
    if ev.is_set():
        return False
    acquired = ev.wait(timeout=timeout)
    if not acquired:
        # Another instance grabbed it during our wait — retry once.
        return ev.is_set() is False
    # Check again: another thread may have set it between our check and set.
    if not ev.is_set():
        ev.set()
        return True
    return False


def release_scoped_lock(platform: str, lock_key: str) -> None:
    """Release a previously acquired identity lock."""
    ev = _get_lock(platform, lock_key)
    ev.clear()


# ---------------------------------------------------------------------------
# Process liveness utilities (WhatsApp bridge cleanup)
# ---------------------------------------------------------------------------


def _pid_exists(pid: int) -> bool:
    """Return ``True`` if a process with the given PID exists.

    Cross-platform: uses ``os.kill(pid, 0)`` on POSIX and
    ``OpenProcess`` via ctypes on Windows.
    """
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            # Windows: OpenProcess with PROCESS_QUERY_INFORMATION
            import ctypes
            PROCESS_QUERY_INFORMATION = 0x0400
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
            if handle == 0:
                return False
            kernel32.CloseHandle(handle)
            return True
        else:
            os.kill(pid, 0)
            return True
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        return False


def get_process_start_time(pid: int) -> float | None:
    """Return the process start time (epoch seconds) or ``None`` if unknown.

    Uses ``psutil`` when available; falls back to ``None`` otherwise.
    """
    try:
        import psutil
        proc = psutil.Process(pid)
        return proc.create_time()
    except (ImportError, psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return None


def _read_process_cmdline(pid: int) -> str | None:
    """Return the command line of the given process, or ``None``.

    Used by WhatsApp bridge cleanup to verify a recycled PID is not a
    stranger's process before signalling it.
    """
    try:
        import psutil
        proc = psutil.Process(pid)
        return " ".join(proc.cmdline())
    except (ImportError, psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return None


# ---------------------------------------------------------------------------
# Runtime status (read from shared runtime.json written by the gateway loop)
# ---------------------------------------------------------------------------


def read_runtime_status(runtime_path: str | Path | None = None) -> dict[str, Any] | None:
    """Read the gateway runtime status from the JSON file on disk.

    The gateway loop writes this file on every state transition so external
    tools (and the /v1/ready endpoint) can inspect gateway state without
    calling into the process.

    Returns:
        Parsed dict, or ``None`` if the file is missing or corrupt.
    """
    if runtime_path is None:
        from encre.config import get_data_dir
        runtime_path = get_data_dir() / "runtime.json"
    path = Path(runtime_path)
    try:
        text = path.read_text(encoding="utf-8")
        import json
        return json.loads(text)
    except (OSError, ValueError, ImportError):
        return None


def parse_active_agents(value: Any) -> int:
    """Coerce a runtime ``active_agents`` value to a non-negative int."""
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except (ValueError, TypeError):
            return 0
    return 0


def derive_gateway_busy(
    gateway_running: bool,
    gateway_state: str | None,
    active_agents: int,
) -> bool:
    """Return ``True`` if the gateway is actively processing work.

    A gateway is "busy" when it is running, not draining, and has at least
    one active agent turn in flight.
    """
    if not gateway_running:
        return False
    if gateway_state in ("draining", "stopping", "exited", "crashed"):
        return False
    return active_agents > 0


def derive_gateway_drainable(
    gateway_running: bool,
    gateway_state: str | None,
) -> bool:
    """Return ``True`` if the gateway can accept new connections (not draining).

    Used by /v1/ready to signal whether the gateway will accept new work.
    """
    if not gateway_running:
        return False
    return gateway_state not in ("draining", "stopping")
