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

"""Background shell manager.

Maintains running shell processes started via the bash tool with
``run_in_background=true``. Output is accumulated into per-shell buffers,
exposing an offset-based read protocol so the bash_output tool can stream
new bytes since the last read without re-sending what the model already saw.

Each record carries an honest ``isolation`` level reported by the Rust
confinement core (``SandboxSession``); it is surfaced in every result
envelope.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from encre.capabilities.process.sandbox import ConfinedProcess, spawn_confined
from encre.tools._encoding import decode_bytes


from encre.logging_config import get_logger


logger = get_logger(__name__)

# One-shot (single command, then exit) launch flags per shell type.  Used by
# ``spawn`` so a background shell honours the model's terminal choice instead
# of always falling back to the platform default (cmd.exe / /bin/bash -lc).
_ONE_SHOT_FLAGS: dict[str, tuple[str, ...]] = {
    "powershell": ("-NoProfile", "-Command"),
    "pwsh": ("-NoProfile", "-Command"),
    "cmd": ("/C",),
    "bash": ("-lc",),
    "zsh": ("-lc",),
    "sh": ("-c",),
}


def _one_shot_launch(terminal: str, command: str) -> list[str] | None:
    """Build the argv for a one-shot *terminal* running *command*.

    Returns ``None`` when *terminal* is ``auto`` or unknown, so the caller
    keeps its platform default; interpreter-style terminals (python/node/...)
    run the command directly through the interpreter's ``-c`` equivalent.
    """
    exe = shutil.which(terminal) if terminal not in ("cmd",) else shutil.which("cmd.exe")
    if exe is None:
        return None
    flags = _ONE_SHOT_FLAGS.get(terminal)
    if flags is not None:
        return [exe, *flags, command]
    if terminal == "python":
        return [exe, "-c", command]
    if terminal == "node":
        return [exe, "-e", command]
    return None


@dataclass
class _ShellRecord:
    """ShellRecord."""
    id: str
    command: str
    cwd: str
    started_at: float
    process: ConfinedProcess
    stdout_buf: bytearray = field(default_factory=bytearray)
    stderr_buf: bytearray = field(default_factory=bytearray)
    stdout_offset: int = 0
    stderr_offset: int = 0
    exit_code: int | None = None
    reader_tasks: list[asyncio.Task] = field(default_factory=list)
    finished_at: float | None = None
    # Honest isolation report from the Rust confinement core:
    # "landlock" / "sandbox_init" / "job_object" / "none".  Never faked —
    # the value comes from the Rust spawn report.
    isolation: str = "none"

    @property
    def running(self) -> bool:
        """Running."""
        return self.exit_code is None and self.process.running()


class BackgroundShellManager:
    """Process-wide registry of backgrounded shells."""

    _instance: BackgroundShellManager | None = None
    _MAX_BUFFER_BYTES = 4 * 1024 * 1024  # 4 MiB ring per stream

    def __init__(self) -> None:
        """Init."""
        self._shells: dict[str, _ShellRecord] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def instance(cls) -> BackgroundShellManager:
        """Instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    async def spawn(
        self,
        command: str,
        cwd: str | None = None,
        terminal: str = "auto",
        workspace: str | None = None,
    ) -> _ShellRecord:
        """Spawn a shell command in the background and return its record.

        *terminal* selects the shell that runs *command*: ``auto`` keeps the
        platform one-shot default (cmd.exe on Windows, /bin/bash -lc
        elsewhere); any other value (powershell/pwsh/bash/zsh/sh/...) is
        launched with the appropriate one-shot flags so the model's shell
        choice applies to background shells too.

        The child is spawned through the Rust confinement core
        (:func:`spawn_confined`); *workspace* is the confinement root for
        the Landlock / Seatbelt paths (ignored on Windows).
        """
        shell_id = "shell_" + secrets.token_hex(4)

        launch = _one_shot_launch(terminal, command)
        if launch is None:
            launch = (
                ["cmd.exe", "/C", command]
                if sys.platform == "win32"
                else ["/bin/bash", "-lc", command]
            )

        proc = await spawn_confined(launch, cwd=cwd or None, workspace=workspace)

        rec = _ShellRecord(
            id=shell_id,
            command=command,
            cwd=cwd or os.getcwd(),
            started_at=time.time(),
            process=proc,
            isolation=proc.isolation,
        )

        rec.reader_tasks.append(asyncio.create_task(self._drain(rec, "stdout")))
        rec.reader_tasks.append(asyncio.create_task(self._drain(rec, "stderr")))
        rec.reader_tasks.append(asyncio.create_task(self._wait(rec)))

        async with self._lock:
            self._shells[shell_id] = rec

        return rec

    async def _drain(self, rec: _ShellRecord, stream: str) -> None:
        """Drain.

        Args:
            rec: Description of the rec parameter.
            stream: Description of the stream parameter.
        """
        drain = (
            rec.process.drain_stdout
            if stream == "stdout"
            else rec.process.drain_stderr
        )
        buf = rec.stdout_buf if stream == "stdout" else rec.stderr_buf
        try:
            while True:
                chunk = await drain(4096, 250)
                if chunk:
                    buf.extend(chunk)
                    self._trim(rec, stream, buf)
                    continue
                if not rec.process.running():
                    # Child exited: flush whatever the reader thread still
                    # holds from the pipe buffer before leaving.
                    while True:
                        chunk = await drain(65536, 100)
                        if not chunk:
                            break
                        buf.extend(chunk)
                        self._trim(rec, stream, buf)
                    break
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
    def _trim(self, rec: _ShellRecord, stream: str, buf: bytearray) -> None:
        """Trim *buf* to the ring size, advancing the offset cursor."""
        if len(buf) <= self._MAX_BUFFER_BYTES:
            return
        # Drop the oldest half, but advance the offset cursor so consumers
        # don't see a phantom rewind.
        drop = len(buf) - self._MAX_BUFFER_BYTES // 2
        del buf[:drop]
        if stream == "stdout":
            rec.stdout_offset = max(0, rec.stdout_offset - drop)
        else:
            rec.stderr_offset = max(0, rec.stderr_offset - drop)

    async def _wait(self, rec: _ShellRecord) -> None:
        """Wait.

        Args:
            rec: Description of the rec parameter.
        """
        try:
            while True:
                code = await rec.process.wait(1000)
                if code is not None:
                    rec.exit_code = code
                    rec.finished_at = time.time()
                    return
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read_new_output(self, shell_id: str) -> dict[str, Any]:
        """Read new output.

        Args:
            shell_id: Description of the shell_id parameter.
        """
        rec = self._shells.get(shell_id)
        if rec is None:
            return {"error": f"Unknown shell id: {shell_id}"}

        out_bytes = bytes(rec.stdout_buf[rec.stdout_offset:])
        err_bytes = bytes(rec.stderr_buf[rec.stderr_offset:])
        rec.stdout_offset = len(rec.stdout_buf)
        rec.stderr_offset = len(rec.stderr_buf)

        return {
            "id": rec.id,
            "command": rec.command,
            "running": rec.running,
            "exit_code": rec.exit_code,
            "isolation": rec.isolation,
            "stdout": decode_bytes(out_bytes),
            "stderr": decode_bytes(err_bytes),
            "stdout_total_bytes": len(rec.stdout_buf),
            "stderr_total_bytes": len(rec.stderr_buf),
            "started_at": rec.started_at,
            "finished_at": rec.finished_at,
        }

    # ------------------------------------------------------------------
    # Kill
    # ------------------------------------------------------------------

    async def kill(self, shell_id: str, force: bool = False) -> dict[str, Any]:
        """Kill.

        Args:
            shell_id: Description of the shell_id parameter.
            force: Description of the force parameter.
        """
        rec = self._shells.get(shell_id)
        if rec is None:
            return {"error": f"Unknown shell id: {shell_id}"}

        if not rec.running:
            return {"id": rec.id, "already_exited": True, "exit_code": rec.exit_code}

        try:
            # Rust kill_tree reaps the whole descendant tree synchronously.
            await rec.process.kill_tree(force=force)
        except Exception as exc:
            return {"error": f"Failed to signal shell: {exc}"}

        # Give the waiter task a moment to record the exit code.
        for _ in range(50):
            if rec.exit_code is not None:
                break
            await asyncio.sleep(0.05)

        return {
            "id": rec.id,
            "killed": True,
            "exit_code": rec.exit_code,
        }

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def list_shells(self) -> list[dict[str, Any]]:
        """List shells."""
        out: list[dict[str, Any]] = []
        for rec in self._shells.values():
            out.append({
                "id": rec.id,
                "command": rec.command,
                "cwd": rec.cwd,
                "started_at": rec.started_at,
                "finished_at": rec.finished_at,
                "running": rec.running,
                "exit_code": rec.exit_code,
                "isolation": rec.isolation,
                "stdout_total_bytes": len(rec.stdout_buf),
                "stderr_total_bytes": len(rec.stderr_buf),
            })
        return out
