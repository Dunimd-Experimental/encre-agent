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

"""Single source of truth for ALL subprocess spawning in Encre.

Merges the former ``tools/builtin/_popen`` and
``tools/builtin/_suppress_window`` (they implemented the same
window-suppression kwarg injection twice).  Every process spawn in the
codebase goes through this module so platform-specific window suppression
(``CREATE_NO_WINDOW`` + ``SW_HIDE`` on Windows, ``setsid`` on Unix) is
applied exactly once, in one place.

Confinement-capable spawns (model-driven tool shells) do NOT go through
this module: they use :func:`encre.capabilities.process.sandbox.spawn_confined`,
which applies the OS-level isolation inside the Rust core.  This module
stays a pure window-suppression seam for internal, short-lived helper
processes (rg, file watchers, ...).

Usage::

    from encre.capabilities.process import create_subprocess_exec

    proc = await create_subprocess_exec("rg", "pattern")
    stdout, stderr = await proc.communicate()
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Any



from encre.logging_config import get_logger
logger = get_logger(__name__)


def hidden_subprocess_kwargs() -> dict[str, Any]:
    """Return ``**kwargs`` that guarantee no visible terminal window.

    Windows:
      - ``creationflags = CREATE_NO_WINDOW``
      - ``startupinfo`` with ``STARTF_USESHOWWINDOW | SW_HIDE``
        (OS-level "do not show", blocks the conhost flash on Win 11)

    Linux / macOS:
      - ``start_new_session = True`` (``setsid(2)``)
    """
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags = subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        return {
            "creationflags": 0x08000000,  # CREATE_NO_WINDOW only -- DETACHED_PROCESS blocks stdout
            "startupinfo": si,
        }
    return {
        "start_new_session": True,
    }


def _inject_creationflags(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Inject window-suppression kwargs unless the caller already set them."""
    for key, value in hidden_subprocess_kwargs().items():
        kwargs.setdefault(key, value)
    return kwargs


async def create_subprocess_exec(
    *args: Any, **kwargs: Any
) -> asyncio.subprocess.Process:
    """Spawn a child process with window suppression applied.

    Args:
        args: Positional arguments forwarded to ``asyncio.create_subprocess_exec``.
        kwargs: Keyword arguments forwarded with suppression flags injected.
    """
    _inject_creationflags(kwargs)
    return await asyncio.create_subprocess_exec(*args, **kwargs)


def create_subprocess_run(
    cmd_parts: list[str],
    timeout: float | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run a synchronous subprocess with window suppression applied.

    Args:
        cmd_parts: Command vector to execute.
        timeout: Optional timeout in seconds.
        kwargs: Keyword arguments forwarded with suppression flags injected.
    """
    _inject_creationflags(kwargs)
    return subprocess.run(cmd_parts, timeout=timeout, **kwargs)


def kill_process_tree(pid: int, force: bool = False) -> None:
    """Terminate *pid* and all its children, cross-platform.

    Windows uses ``taskkill /T``; POSIX kills the process group.  The
    former ``_win_kill_tree`` duplicates in ``_shell_manager`` and
    ``_terminal_manager`` are merged here.
    """
    import signal
    import sys

    if sys.platform == "win32":
        args = ["taskkill", "/T", "/PID", str(pid)]
        if force:
            args.insert(1, "/F")
        subprocess.run(args, capture_output=True, timeout=5)
        return
    try:
        pgid = os.getpgid(pid)
        sig = signal.SIGKILL if force else signal.SIGTERM
        os.killpg(pgid, sig)
    except ProcessLookupError:
        logger.debug("exception suppressed", exc_info=True)
