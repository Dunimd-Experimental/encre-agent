#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

"""Persistent terminal session manager.

When the model calls ``bash`` with a ``terminal`` parameter, a long-running
shell process is started and kept alive until the current turn ends.  Every
subsequent ``bash`` call with the same terminal type reuses the same process,
preserving working directory, environment variables, and shell state.

Commands are piped through stdin; the output is collected until a unique
delimiter marker is seen in the stdout stream.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from encre.capabilities.process.spawn import (
    create_subprocess_exec,
    kill_process_tree,
)
from encre.tools._encoding import decode_bytes, encode_text


_ENCRE_MARKER = "__ENCRE_DONE_{:08x}__"
_MARKER_COUNTER = 0


def _next_marker() -> str:
    global _MARKER_COUNTER
    _MARKER_COUNTER += 1
    return _ENCRE_MARKER.format(_MARKER_COUNTER)


# 鈹€鈹€ Per-terminal-type shell launch commands 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

SHELL_LAUNCH: dict[str, list[str]] = {
    # `auto` is also the fallback for unknown terminal types, so it must stay
    # the platform's canonical shell: PowerShell on Windows, bash elsewhere.
    "auto": (["powershell", "-NoProfile", "-Command", "-"]
             if sys.platform == "win32" else ["/bin/bash", "--noediting"]),
    "cmd": ["cmd.exe", "/Q"],
    "powershell": ["powershell", "-NoProfile", "-Command", "-"],
    "pwsh": ["pwsh", "-NoProfile", "-Command", "-"],
    "bash": (["C:\\Program Files\\Git\\bin\\bash.exe", "--noediting"]
             if sys.platform == "win32" else ["/bin/bash", "--noediting"]),
    "zsh": ["/bin/zsh", "--noediting"],
    "sh": ["/bin/sh"],
    "python": [sys.executable or "python", "-u", "-i"],
    "node": ["node", "-i"],
    "irb": ["irb", "-f", "--noreadline"],
    "julia": ["julia", "--startup-file=no"],
    "lua": ["lua", "-i"],
    "php": ["php", "-a"],
    "R": ["R", "--no-save", "--no-restore", "-q"],
}

# 鈹€鈹€ How to write a unique "done" marker per shell type 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

_SHELL_MARKER_CMDS: dict[str, str] = {
    "auto": 'echo {}',
    "cmd": 'echo {}',
    "powershell": 'Write-Output "{}"',
    "pwsh": 'Write-Output "{}"',
    "bash": 'echo {}',
    "zsh": 'echo {}',
    "sh": 'echo {}',
    "python": 'print("{}")',
    "node": 'console.log("{}")',
    "irb": 'puts("{}")',
    "julia": 'println("{}")',
    "lua": 'print("{}")',
    "php": 'echo "{}";',
    "R": 'cat("{}")',
}


def marker_cmd(terminal: str, marker: str) -> str:
    """Return the shell command that prints *marker* on stdout."""
    fmt = _SHELL_MARKER_CMDS.get(terminal, "echo {}")
    return fmt.format(marker)


# 鈹€鈹€ Session record 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

@dataclass
class _SessionRecord:
    terminal: str
    cwd: str
    process: asyncio.subprocess.Process
    started_at: float
    cmd_count: int = 0  # number of commands sent to this session
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # serialize access to this session's pipes

    @property
    def running(self) -> bool:
        return self.process.returncode is None


class TerminalSessionManager:
    """Singleton that manages persistent terminal sessions per terminal type."""

    _instance: TerminalSessionManager | None = None

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionRecord] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def instance(cls) -> TerminalSessionManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Execute a command in a terminal session
    # ------------------------------------------------------------------

    async def execute(
        self,
        terminal: str,
        command: str,
        cwd: str | None = None,
        timeout: int = 120,
    ) -> dict[str, Any]:
        """Send *command* to a persistent *terminal* session and return the output.

        If no session exists for this terminal type yet, one is created first.
        Returns a dict with keys: success, exit_code, stdout, stderr, elapsed_ms.
        """
        cwd = cwd or os.getcwd()
        session = await self._get_or_create(terminal, cwd)
        started = time.monotonic()
        return await self._send_command(session, command, timeout, started)

    async def _get_or_create(self, terminal: str, cwd: str) -> _SessionRecord:
        async with self._lock:
            existing = self._sessions.get(terminal)
            if existing is not None and existing.running:
                return existing
            proc = await self._spawn(terminal, cwd)
            rec = _SessionRecord(
                terminal=terminal,
                cwd=cwd,
                process=proc,
                started_at=time.time(),
            )
            self._sessions[terminal] = rec
            return rec

    async def _spawn(self, terminal: str, cwd: str) -> asyncio.subprocess.Process:
        launch = SHELL_LAUNCH.get(terminal)
        if launch is None:
            launch = SHELL_LAUNCH.get("auto", ["cmd.exe", "/Q"])
        # create_subprocess_exec injects window-suppression kwargs itself.
        proc = await create_subprocess_exec(
            *launch,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd or None,
        )
        return proc

    async def _send_command(
        self,
        session: _SessionRecord,
        command: str,
        timeout: int,
        started: float,
    ) -> dict[str, Any]:
        marker = _next_marker()
        marker_line = marker_cmd(session.terminal, marker)

        stdin_data = f"{command}\r\n{marker_line}\r\n"
        session.cmd_count += 1

        async with session.lock:
            try:
                session.process.stdin.write(
                    encode_text(stdin_data, terminal=session.terminal)
                )
                await session.process.stdin.drain()
            except Exception as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                async with self._lock:
                    self._sessions.pop(session.terminal, None)
                return {
                    "success": False,
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"stdin write failed: {exc}",
                    "elapsed_ms": elapsed,
                }

            stdout_parts: list[bytes] = []
            stderr_parts: list[bytes] = []
            found_marker = False
            deadline = time.monotonic() + timeout

            async def _read_stream(pipe, parts, marker_bytes):
                nonlocal found_marker
                carry = b""
                while time.monotonic() < deadline and not found_marker:
                    try:
                        remaining = max(0.1, deadline - time.monotonic())
                        chunk = await asyncio.wait_for(pipe.read(4096), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    if not chunk:
                        break
                    parts.append(chunk)
                    if not found_marker:
                        if marker_bytes in carry + chunk:
                            found_marker = True
                            break
                        carry = chunk[-(len(marker_bytes) - 1):] if len(chunk) >= len(marker_bytes) - 1 else chunk
                return found_marker

            marker_bytes = marker.encode("utf-8")
            read_task = asyncio.create_task(
                _read_stream(session.process.stdout, stdout_parts, marker_bytes)
            )
            stderr_task = asyncio.create_task(
                _read_stream(session.process.stderr, stderr_parts, marker_bytes)
            )
            done, pending = await asyncio.wait(
                [read_task, stderr_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Grace window: let pending tasks drain buffered output before
            # cancelling 鈥?the marker is echoed after the command completes,
            # so any output still pending is already in the pipe buffer.
            if pending:
                _, still_pending = await asyncio.wait(
                    pending, timeout=0.25,
                    return_when=asyncio.ALL_COMPLETED,
                )
                for task in still_pending:
                    task.cancel()
                await asyncio.gather(*still_pending, return_exceptions=True)

            elapsed = int((time.monotonic() - started) * 1000)
            stdout_text = decode_bytes(b"".join(stdout_parts))
            stderr_text = decode_bytes(b"".join(stderr_parts))

            # Strip everything from the marker line onwards
            if found_marker:
                lines = stdout_text.splitlines()
                clean: list[str] = []
                for line in lines:
                    if marker in line:
                        break
                    clean.append(line)
                stdout_text = "\n".join(clean).rstrip("\r\n")
            else:
                # Marker not found within the deadline 鈥?return a timeout
                # error instead of faking success with possibly empty output.
                return {
                    "success": False,
                    "exit_code": -1,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "elapsed_ms": elapsed,
                }

        return {
            "success": True,
            "exit_code": 0,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "elapsed_ms": elapsed,
        }

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup_all(self) -> None:
        """Kill all active terminal sessions and close pipe handles."""
        async with self._lock:
            for term, rec in list(self._sessions.items()):
                if rec.running:
                    try:
                        if sys.platform == "win32":
                            kill_process_tree(rec.process.pid, force=True)
                        else:
                            rec.process.terminate()
                        try:
                            await asyncio.wait_for(rec.process.wait(), timeout=5.0)
                        except asyncio.TimeoutError:
                            if sys.platform == "win32":
                                kill_process_tree(rec.process.pid, force=True)
                            else:
                                rec.process.kill()
                            await asyncio.wait_for(rec.process.wait(), timeout=2.0)
                    except Exception:
                        pass
                # Close pipe handles 鈥?StreamWriter (stdin) has is_closing(),
                # StreamReader (stdout/stderr) does not.
                for pipe in (rec.process.stdin, rec.process.stdout, rec.process.stderr):
                    if pipe is not None:
                        if hasattr(pipe, "is_closing"):
                            if not pipe.is_closing():
                                pipe.close()
            self._sessions.clear()
