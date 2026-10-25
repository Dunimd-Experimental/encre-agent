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

"""Persistent terminal session manager.

When the model calls ``bash`` with a ``terminal`` parameter, a long-running
shell process is started and kept alive until the current turn ends.  Every
subsequent ``bash`` call with the same terminal type reuses the same process,
preserving working directory, environment variables, and shell state.

Commands are piped through stdin; the output is collected until a unique
delimiter marker is seen in the stdout stream.

Every result envelope carries an honest ``isolation`` level reported by the
Rust confinement core (``SandboxSession``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from encre.capabilities.process.sandbox import ConfinedProcess, spawn_confined
from encre.tools._encoding import decode_bytes, encode_text


from encre.logging_config import get_logger


logger = get_logger(__name__)

_ENCRE_MARKER = "__ENCRE_DONE_{:08x}__"
_MARKER_COUNTER = 0


def _next_marker() -> str:
    global _MARKER_COUNTER
    _MARKER_COUNTER += 1
    return _ENCRE_MARKER.format(_MARKER_COUNTER)


# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------

# How to write a unique "done" marker per shell type.  The marker line also
# carries the previous command's exit code (``MARK rc``) so the manager can
# report the real exit status instead of always faking 0.  Interpreter-style
# sessions (python/node/...) have no shell-level exit-code concept and omit
# it; the manager falls back to 0 for them.

_SHELL_MARKER_CMDS: dict[str, str] = {
    "auto": 'echo {} $?',
    "cmd": 'echo {} %ERRORLEVEL%',
    "powershell": 'Write-Output "{} $LASTEXITCODE"',
    "pwsh": 'Write-Output "{} $LASTEXITCODE"',
    "bash": 'echo {} $?',
    "zsh": 'echo {} $?',
    "sh": 'echo {} $?',
    "python": 'print("{}")',
    "node": 'console.log("{}")',
    "irb": 'puts("{}")',
    "julia": 'println("{}")',
    "lua": 'print("{}")',
    "php": 'echo "{}";',
    "R": 'cat("{}")',
}


def marker_cmd(terminal: str, marker: str) -> str:
    """Return the shell command that prints *marker* (plus exit code)."""
    fmt = _SHELL_MARKER_CMDS.get(terminal, "echo {}")
    return fmt.format(marker)


def _split_marker_line(stdout_text: str, marker: str) -> tuple[str, int]:
    """Drop everything from the marker line onwards and extract its exit code.

    Returns ``(stdout_without_marker, exit_code)``; the exit code is ``0``
    when the marker line carries no parseable status.
    """
    if marker not in stdout_text:
        return stdout_text, 0
    lines = stdout_text.splitlines()
    clean: list[str] = []
    rc = 0
    for line in lines:
        if marker in line:
            tail = line.split(marker, 1)[1].strip()
            try:
                rc = int(tail.split()[-1]) if tail else 0
            except (ValueError, IndexError):
                rc = 0
            break
        clean.append(line)
    return "\n".join(clean).rstrip("\r\n"), rc


# ---------------------------------------------------------------------------

@dataclass
class _SessionRecord:
    terminal: str
    cwd: str
    process: ConfinedProcess
    started_at: float
    cmd_count: int = 0  # number of commands sent to this session
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # serialize access to this session's pipes
    # Honest isolation report from the Rust confinement core:
    # "landlock" / "sandbox_init" / "job_object" / "none".  Never faked —
    # the value comes from the Rust spawn report.
    isolation: str = "none"

    @property
    def running(self) -> bool:
        return self.process.running()


class TerminalSessionManager:
    """Singleton that manages persistent terminal sessions per terminal type."""

    _instance: TerminalSessionManager | None = None

    def __init__(self) -> None:
        # Keyed by (terminal, resolved cwd): same shell type in different
        # directories gets independent sessions, so commands targeting
        # different workspaces run in parallel and never inherit a stale cwd
        # (the session's spawn cwd was previously reused regardless of the
        # requested cwd, silently ignoring the parameter).
        self._sessions: dict[tuple[str, str], _SessionRecord] = {}
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
        on_chunk=None,
        stop_event: asyncio.Event | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Send *command* to a persistent *terminal* session and return the output.

        If no session exists for this terminal type yet, one is created first.
        Returns a dict with keys: success, exit_code, stdout, stderr, elapsed_ms.

        When *on_chunk* is provided, decoded text is delivered incrementally
        (``on_chunk(text)``) as output arrives, before the command finishes,
        so long-running commands stream to the UI in real time.

        When *stop_event* is provided, the command aborts as soon as the event
        is set: the underlying shell is killed, the session is dropped, and the
        returned dict carries ``interrupted=True`` plus whatever output already
        arrived.  This is how the user's Stop action stops a running command
        immediately instead of letting it run to completion in the background.

        *workspace* is the confinement root for the Rust Landlock / Seatbelt
        paths (ignored on Windows); it is applied once at session spawn.
        """
        cwd = cwd or os.getcwd()
        session = await self._get_or_create(terminal, cwd, workspace)
        started = time.monotonic()
        return await self._send_command(
            session, command, timeout, started,
            on_chunk=on_chunk, stop_event=stop_event,
        )

    async def _get_or_create(
        self, terminal: str, cwd: str, workspace: str | None = None,
    ) -> _SessionRecord:
        key = (terminal, os.path.abspath(cwd or os.getcwd()))
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and existing.running:
                return existing
            rec = await self._spawn(terminal, cwd, workspace)
            self._sessions[key] = rec
            return rec

    async def _spawn(
        self, terminal: str, cwd: str, workspace: str | None = None,
    ) -> _SessionRecord:
        launch = SHELL_LAUNCH.get(terminal)
        if launch is None:
            launch = SHELL_LAUNCH.get("auto", ["cmd.exe", "/Q"])
        launch = list(launch)
        # Resolve the shell binary through PATH when the hardcoded absolute
        # path does not exist (e.g. MSYS-installed zsh on Windows, or a
        # non-standard bash location); fall back to the literal on failure.
        exe = launch[0]
        if not Path(exe).exists():
            resolved = shutil.which(Path(exe).name) or shutil.which(exe)
            if resolved:
                launch[0] = resolved
        # Rust-owned confinement: the OS sandbox (Landlock / sandbox_init /
        # Job Object) is applied inside SandboxSession at spawn time; Python
        # only drives the pipes afterwards.
        proc = await spawn_confined(
            launch, cwd=cwd or None, workspace=workspace,
        )
        return _SessionRecord(
            terminal=terminal,
            cwd=cwd or os.getcwd(),
            process=proc,
            started_at=time.time(),
            isolation=proc.isolation,
        )

    async def _send_command(
        self,
        session: _SessionRecord,
        command: str,
        timeout: int,
        started: float,
        on_chunk=None,
        stop_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        marker = _next_marker()
        marker_line = marker_cmd(session.terminal, marker)

        stdin_data = f"{command}\r\n{marker_line}\r\n"
        session.cmd_count += 1

        async with session.lock:
            try:
                await session.process.write_stdin(
                    encode_text(stdin_data, terminal=session.terminal)
                )
            except Exception as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                async with self._lock:
                    self._sessions.pop((session.terminal, os.path.abspath(session.cwd)), None)
                return {
                    "success": False,
                    "exit_code": -1,
                    "isolation": session.isolation,
                    "stdout": "",
                    "stderr": f"stdin write failed: {exc}",
                    "elapsed_ms": elapsed,
                }

            stdout_parts: list[bytes] = []
            stderr_parts: list[bytes] = []
            found_marker = False
            deadline = time.monotonic() + timeout
            flushed_len = 0  # bytes already emitted via on_chunk

            async def _read_stream(drain, parts, marker_bytes, emit_text):
                nonlocal found_marker, flushed_len
                carry = b""
                while time.monotonic() < deadline and not found_marker:
                    remaining = max(0.1, deadline - time.monotonic())
                    # drain waits up to its timeout for data and returns b""
                    # on timeout or EOF (ConfinedProcess contract).
                    chunk = await drain(4096, int(remaining * 1000))
                    if not chunk:
                        break
                    parts.append(chunk)
                    if not found_marker:
                        if marker_bytes in carry + chunk:
                            found_marker = True
                            marker_pos = (carry + chunk).find(marker_bytes)
                            # Only emit the text up to the marker so the UI
                            # never shows the internal done-sentinel.
                            emit_text(parts, marker_pos)
                            break
                        carry = chunk[-(len(marker_bytes) - 1):] if len(chunk) >= len(marker_bytes) - 1 else chunk
                        emit_text(parts, None)
                return found_marker

            marker_bytes = marker.encode("utf-8")

            def _make_emit():
                """Return a callback that streams new decoded text via on_chunk."""
                if on_chunk is None:
                    return lambda *_: None

                def _emit(parts, limit):
                    nonlocal flushed_len
                    joined = b"".join(parts)
                    if limit is not None:
                        joined = joined[:limit]
                    new_data = joined[flushed_len:]
                    flushed_len = len(joined)
                    if new_data:
                        text = decode_bytes(new_data)
                        if text:
                            on_chunk(text)

                return _emit

            emit_stdout = _make_emit()
            read_task = asyncio.create_task(
                _read_stream(
                    session.process.drain_stdout,
                    stdout_parts, marker_bytes, emit_stdout,
                )
            )
            emit_stderr = _make_emit()
            stderr_task = asyncio.create_task(
                _read_stream(
                    session.process.drain_stderr,
                    stderr_parts, marker_bytes, emit_stderr,
                )
            )
            stop_wait_task: asyncio.Task[Any] | None = None
            if stop_event is not None:
                stop_wait_task = asyncio.create_task(stop_event.wait())

            reader_tasks = [t for t in (read_task, stderr_task, stop_wait_task) if t is not None]
            try:
                done, pending = await asyncio.wait(
                    set(reader_tasks),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                # The turn is being torn down (Stop button / task cancel).
                # Kill the underlying shell so the command stops immediately
                # instead of running to completion in the background, then
                # drop the session so the next command starts a fresh shell.
                for task in reader_tasks:
                    task.cancel()
                if reader_tasks:
                    await asyncio.gather(*reader_tasks, return_exceptions=True)
                await self._terminate_session(session)
                raise

            stop_requested = stop_wait_task is not None and stop_wait_task in done
            if stop_requested:
                # User stop: kill the shell first — this closes the writer side
                # of the pipes, so the reader tasks hit EOF and drain whatever
                # output is still buffered.  Only afterwards do we drop the
                # session; nothing the command already produced is lost.
                await self._kill_session(session)
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*[t for t in reader_tasks if t is not stop_wait_task]),
                        timeout=2.0,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    for task in reader_tasks:
                        if task is not stop_wait_task:
                            task.cancel()
                    await asyncio.gather(*reader_tasks, return_exceptions=True)
                stdout_text = decode_bytes(b"".join(stdout_parts))
                stderr_text = decode_bytes(b"".join(stderr_parts))
                elapsed = int((time.monotonic() - started) * 1000)
                await self._drop_session(session)
                # The shell was killed before the marker echoed, so there is
                # no reliable exit status; report -1 with whatever streamed.
                stdout_text, _ = _split_marker_line(stdout_text, marker)
                return {
                    "success": False,
                    "exit_code": -1,
                    "isolation": session.isolation,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "elapsed_ms": elapsed,
                    "interrupted": True,
                    "stop_requested": True,
                }
            # Grace window: let pending tasks drain buffered output before
            # cancelling — the marker is echoed after the command completes,
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
                stdout_text, exit_code = _split_marker_line(stdout_text, marker)
            else:
                # Marker not found within the deadline -- the command is still
                # running inside the persistent shell.  Kill and drop the
                # session: leaving it alive would let the runaway command
                # interleave its output into the next command's transcript.
                await self._terminate_session(session)
                return {
                    "success": False,
                    "exit_code": -1,
                    "isolation": session.isolation,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "elapsed_ms": elapsed,
                    "timed_out": True,
                }

        return {
            "success": True,
            "exit_code": exit_code,
            "isolation": session.isolation,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "elapsed_ms": elapsed,
        }

    # ------------------------------------------------------------------
    # Interrupt support
    # ------------------------------------------------------------------

    async def _kill_session(self, session: _SessionRecord) -> None:
        """Kill *session*'s process tree without dropping the record.

        Pipe handles are left open on purpose: killing the writer side makes
        the reader tasks hit EOF, so callers can drain whatever output is
        still buffered before the session is dropped.
        """
        try:
            if session.running:
                # Rust kill reaps the whole descendant tree synchronously
                # (process group SIGKILL on POSIX, TerminateProcess +
                # kill-on-close job handle on Windows).
                await session.process.kill_tree(force=True)
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
    async def _drop_session(self, session: _SessionRecord) -> None:
        """Release pipe handles and forget the session record."""
        # Native sessions self-reap on Drop inside Rust; the degraded
        # asyncio fallback closes its pipe handles here so readers unblock
        # even if the process lingers.
        session.process.close()
        async with self._lock:
            if self._sessions.get((session.terminal, os.path.abspath(session.cwd))) is session:
                self._sessions.pop((session.terminal, os.path.abspath(session.cwd)), None)

    async def _terminate_session(self, session: _SessionRecord) -> None:
        """Kill *session*'s process tree and drop it from the live list.

        Called when a command is interrupted (user Stop / task cancellation)
        so any child processes are reaped immediately and the next command
        creates a fresh shell instead of writing into dead pipes.
        """
        await self._kill_session(session)
        await self._drop_session(session)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup_all(self) -> None:
        """Kill all active terminal sessions and release pipe handles."""
        async with self._lock:
            for _key, rec in list(self._sessions.items()):
                if rec.running:
                    # Tree-wide kill inside Rust; reaps synchronously.
                    with contextlib.suppress(Exception):
                        await rec.process.kill_tree(force=True)
                rec.process.close()
            self._sessions.clear()
