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

"""Embedded-terminal domain handlers: shell discovery, spawn, write, kill.

Owned client-message domain for the desktop terminal panel.
:class:`TerminalHandlers` is mixed into :class:`encre.transport.ws.EncreWSHandler`
and backs the ``ClientTerminalListShells`` / ``ClientTerminalSpawn`` /
``ClientTerminalWrite`` / ``ClientTerminalResize`` / ``ClientTerminalKill``
entries of that handler's ``_DISPATCH`` table.

This is the only domain handler that owns a live external process, so the
state model is different from the rest. A spawned shell is an
``asyncio.subprocess.Process`` kept in ``self._term_sessions`` under a
monotonically increasing id from ``self._term_seq``; the reader task keeps the
byte buffer in the same entry and is tracked in ``self._tasks``. Neither
:class:`encre.server.session_manager.SessionManager` nor the Encre Agent Core is
involved -- this terminal is a UI convenience, not agent infrastructure. The
agent's own bash tool uses the separate persistent-terminal path with its own
abort events (see :func:`encre.tools.runtime.get_bash_abort_event`).

Data flow:

- stdin: ``ClientTerminalWrite`` -> ``proc.stdin.write`` + ``drain``.
- stdout: reader task -> :func:`encre.tools._encoding.decode_bytes` ->
  ``terminal_data`` frames, one per 4 KiB chunk read.

Public API:

- :class:`TerminalHandlers`
"""

import asyncio
import os
from typing import Any

from encre.server.protocol import (
    ClientTerminalKill,
    ClientTerminalListShells,
    ClientTerminalResize,
    ClientTerminalSpawn,
    ClientTerminalWrite,
)
from encre.tools._encoding import decode_bytes


class TerminalHandlers:
    """Embedded terminal lifecycle handlers.

    Mixed into :class:`encre.transport.ws.EncreWSHandler`; holds no state of
    its own and uses ``_term_sessions``, ``_term_seq``, ``_tasks`` and
    ``_send`` from :class:`~encre.protocol.handlers.base.HandlerBase`.

    Lifecycle: stateless class; per-terminal state lives in
    ``self._term_sessions`` for the lifetime of the spawned process.

    Invariants:

    - Every id handed out is unique for the lifetime of the server process,
      since ``_term_seq`` is only ever incremented.
    - ``_term_sessions`` is pruned in exactly two places: the reader task's
      ``finally`` block when the process exits on its own, and
      :meth:`_h_terminal_kill`. A write or resize against a dead id must
      fail quietly rather than raise.
    - The reader task is the only thing that writes to the connection for a
      given terminal, so its frames are ordered by construction.

    Args:
        None. Inherits ``__init__`` from
        :class:`~encre.protocol.handlers.base.HandlerBase` through the composed
        handler.
    """

    async def _h_terminal_list_shells(self, ws: Any, msg: ClientTerminalListShells) -> None:
        """Advertise the shells this machine can launch.

        Args:
            ws: The connection to answer.
            msg: ``ClientTerminalListShells``; carries no payload.

        Returns:
            None. Sends ``terminal_shells``.

        Note:
            The list is probed rather than declared, so the panel never
            offers a shell that is not installed. Windows shells are located
            by their well-known install paths because they are not on PATH,
            while Unix shells are found by absolute path and interactive
            interpreters by ``shutil.which``.
        """
        is_windows = os.name == "nt"
        shells = []
        if is_windows:
            shells.append({"name": "PowerShell", "path": "powershell.exe", "args": []})
            if os.path.isfile("C:/Program Files/PowerShell/7/pwsh.exe"):
                shells.append({"name": "pwsh", "path": "pwsh.exe", "args": []})
            shells.append({"name": "cmd", "path": "cmd.exe", "args": []})
            if os.path.isfile("C:/Windows/System32/wsl.exe"):
                shells.append({"name": "WSL", "path": "wsl.exe", "args": []})
        else:
            for sp in ["/bin/bash", "/bin/zsh", "/bin/sh"]:
                if os.path.isfile(sp):
                    shells.append({"name": os.path.basename(sp), "path": sp, "args": []})
            import shutil as _shutil
            for exe in ["pwsh", "irb", "julia", "lua", "php", "R"]:
                resolved = _shutil.which(exe)
                if resolved:
                    shells.append({"name": exe, "path": resolved, "args": []})
        await self._send(ws, "terminal_shells", shells=shells)

    async def _h_terminal_spawn(self, ws: Any, msg: ClientTerminalSpawn) -> None:
        """Start a shell subprocess and begin streaming its output.

        Args:
            ws: The connection that owns this terminal.
            msg: ``ClientTerminalSpawn`` with the optional ``shell`` and
                ``shell_args``; a sensible default is chosen per platform.

        Returns:
            None. Sends ``terminal_spawned`` with the new terminal id, or an
            ``error`` frame with code ``terminal_error`` when the process
            cannot be created.

        Raises:
            Never.

        Note:
            ``stderr`` is merged into ``stdout`` so the panel renders a single
            interleaved stream, which is what a real terminal shows. The
            process is created with ``hidden_subprocess_kwargs()`` so it does
            not open a visible console window on Windows. The id is allocated
            only after the process starts, so a failed spawn never burns a
            number. The reader task is spawned last and tracked in
            ``self._tasks`` so it can be awaited or discarded on shutdown.
        """
        shell = msg.shell or ("powershell.exe" if os.name == "nt" else "/bin/bash")
        shell_args = msg.shell_args or []
        try:
            from encre.capabilities.process import (
                hidden_subprocess_kwargs,
            )
            term_kwargs = hidden_subprocess_kwargs()
            proc = await asyncio.create_subprocess_exec(
                shell, *shell_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **term_kwargs,
            )
        except Exception as e:
            await self._send(ws, "error", message=f"Terminal spawn failed: {e}", code="terminal_error")
            return
        tid = self._term_seq
        self._term_seq += 1
        term_info: dict[str, Any] = {"proc": proc, "buf": b""}
        self._term_sessions[tid] = term_info
        await self._send(ws, "terminal_spawned", id=tid)

        # The reader runs for the life of the process. It appends to the
        # buffer for completeness but streams decoded 4 KiB chunks as they
        # arrive, so the panel does not wait for the process to exit.
        async def _read_stdout(*, proc=proc, term_info=term_info, tid=tid):
            """Stream the spawned shell's stdout into the terminal panel.

            Runs for the whole life of the process, reading 4 KiB chunks,
            appending each to the session buffer, and forwarding a decoded copy
            as a ``terminal_data`` frame immediately so the panel paints as the
            process writes rather than after it exits.

            Args:
                proc: The ``asyncio`` subprocess being drained; captured as a
                    keyword default so the closure cannot pick up a later
                    terminal's process.
                term_info: The per-terminal dict whose ``buf`` accumulates raw
                    output for scrollback.
                tid: The terminal id echoed in every frame.

            Returns:
                None.

            Note:
                Output is decoded with the lossy terminal decoder first and
                falls back to a latin-1 replace decode, so an undecodable byte
                sequence still renders instead of stalling the stream. The
                ``finally`` clause pops the terminal from
                ``self._term_sessions`` and sends one empty ``terminal_data``
                frame as the EOF marker the panel uses to close the prompt.
            """
            try:
                while True:
                    data = await proc.stdout.read(4096)
                    if not data:
                        break
                    term_info["buf"] += data
                    try:
                        decoded = decode_bytes(data)
                    except Exception:
                        decoded = data.decode("latin-1", errors="replace")
                    await self._send(ws, "terminal_data", id=tid, data=decoded)
            except Exception:
                pass
            finally:
                self._term_sessions.pop(tid, None)
                await self._send(ws, "terminal_data", id=tid, data="")

        # Registered with the loop via the default executor so output
        # decoding never blocks the event loop.
        _t = asyncio.ensure_future(_read_stdout())
        self._tasks.add(_t)

    async def _h_terminal_write(self, ws: Any, msg: ClientTerminalWrite) -> None:
        """Forward keystrokes to a running terminal.

        Args:
            ws: The connection to answer.
            msg: ``ClientTerminalWrite`` with the terminal ``id`` and the
                ``data`` to send.

        Returns:
            None. Sends an ``error`` frame with code ``terminal_not_found``
            when the id is unknown.

        Raises:
            Never.

        Note:
            Writing to a closed stdin is swallowed rather than reported. The
            terminal may be in the middle of exiting at exactly this moment,
            and surfacing that as an error would make normal typing flaky.
            The reader task announces an ended terminal separately by sending
            an empty ``terminal_data`` frame.
        """
        tinfo = self._term_sessions.get(msg.id)
        if tinfo is None:
            await self._send(ws, "error", message="Terminal not found", code="terminal_not_found")
            return
        proc = tinfo["proc"]
        if proc.stdin and not proc.stdin.is_closed():
            try:
                proc.stdin.write(msg.data.encode("utf-8", errors="replace"))
                await proc.stdin.drain()
            except Exception:
                pass

    async def _h_terminal_resize(self, ws: Any, msg: ClientTerminalResize) -> None:
        """Recorded as accepted but deliberately a no-op.

        The panel is served by a byte stream over a pipe rather than a real
        PTY, so there is no controlling terminal to inform about a new size.
        Keeping the message type wired in means the desktop can keep sending
        resize requests unchanged when the backend does grow a PTY; the frame
        is simply ignored today.

        Args:
            ws: The connection to answer.
            msg: ``ClientTerminalResize`` with the new dimensions.

        Returns:
            None. No frame is sent.
        """
        pass

    async def _h_terminal_kill(self, ws: Any, msg: ClientTerminalKill) -> None:
        """Terminate a terminal, escalating to a hard kill if it hangs.

        Args:
            ws: The connection to answer.
            msg: ``ClientTerminalKill`` with the terminal ``id``.

        Returns:
            None. No frame is sent; the reader task announces termination on
            its own by emitting an empty ``terminal_data`` frame.

        Raises:
            Never.

        Note:
            The order is graceful first: ``terminate`` is given three seconds,
            and only if the process is still alive is ``kill`` used. A shell
            that exits quickly on terminate is not disturbed further. An id
            that is already gone is a no-op, since the reader task may have
            pruned the entry by the time the click arrives. The entry is
            removed unconditionally at the end so a later write cannot find a
            dead process.
        """
        tinfo = self._term_sessions.get(msg.id)
        if tinfo is None:
            return
        proc = tinfo["proc"]
        if proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
            except Exception:
                pass
        self._term_sessions.pop(msg.id, None)
