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

"""Notebook session: execute code cells in a persistent Python kernel.

:class:`EncreNotebookSession` spawns a hidden Python subprocess that speaks
a tiny JSON line protocol (see ``_kernel_script``), runs each cell's code
with captured stdout/stderr, and records the result.  Cells are addressed
by id and can be created, edited, executed, inspected and deleted.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

from encre.tools._encoding import decode_bytes


@dataclass
class _CellData:
    """Internal per-cell state (code, output, status, timing)."""
    code: str
    cell_type: str
    output: str = ""
    error: str = ""
    status: str = "idle"
    execution_time: float = 0.0


class EncreNotebookSession:
    """A notebook-like session backed by an out-of-process Python kernel."""
    def __init__(self, kernel_name: str = "python3") -> None:
        """Create a fresh session (kernel lazily started on first execute)."""
        self.kernel_name = kernel_name
        self.session_id: str = str(uuid.uuid4())
        self._cells: dict[str, _CellData] = {}
        self._cell_order: list[str] = []
        self._process: subprocess.Popen[bytes] | None = None
        self._started: bool = False
        self._kernel_script: str = (
            # Inline Python REPL: reads JSON {"code": ...} lines and writes
            # back JSON {"ok", "output", "error"} results; "__SHUTDOWN__"
            # terminates the kernel cleanly.
            "import sys, json, traceback\n"
            "import builtins as _bi\n"
            "_ns = {}\n"
            'sys.stdout.write("READY\\n")\n'
            "sys.stdout.flush()\n"
            "for _line in sys.stdin:\n"
            "    _line = _line.strip()\n"
            "    if not _line:\n"
            "        continue\n"
            "    try:\n"
            "        _req = json.loads(_line)\n"
            "    except Exception:\n"
            "        continue\n"
            "    _code = _req.get('code', '')\n"
            "    if _code == '__SHUTDOWN__':\n"
            "        sys.stdout.write(json.dumps({'ok': True}))\n"
            "        sys.stdout.write('\\n')\n"
            "        sys.stdout.flush()\n"
            "        break\n"
            "    from io import StringIO\n"
            "    _out_io = StringIO()\n"
            "    _err_io = StringIO()\n"
            "    _old_stdout = sys.stdout\n"
            "    _old_stderr = sys.stderr\n"
            "    sys.stdout = _out_io\n"
            "    sys.stderr = _err_io\n"
            "    _ok = True\n"
            "    try:\n"
            "        exec(_code, _ns)\n"
            "    except Exception:\n"
            "        _ok = False\n"
            "        _err_io.write(traceback.format_exc())\n"
            "    finally:\n"
            "        sys.stdout = _old_stdout\n"
            "        sys.stderr = _old_stderr\n"
            "    _result = {'ok': _ok, 'output': _out_io.getvalue(), 'error': _err_io.getvalue()}\n"
            "    sys.stdout.write(json.dumps(_result) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )

    def _ensure_kernel(self) -> None:
        """Start the kernel subprocess if it is not already running."""
        if self._started and self._process is not None and self._process.poll() is None:
            return
        # ``kernel_name`` is only a real interpreter path when it points at
        # an existing file; the conventional aliases ("python3"/"python")
        # and Windows setups must resolve to the running interpreter, since
        # this kernel is a plain Python subprocess, not a Jupyter kernelspec.
        name = self.kernel_name or ""
        python_exe = name if name and os.path.isfile(name) else sys.executable
        from encre.capabilities.process import hidden_subprocess_kwargs
        popen_kwargs = hidden_subprocess_kwargs()
        self._process = subprocess.Popen(
            [python_exe, "-u", "-c", self._kernel_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            **popen_kwargs,
        )
        ready_line = decode_bytes(self._process.stdout.readline()).strip() if self._process.stdout else ""
        if ready_line != "READY":
            self._started = False
            raise RuntimeError("Kernel failed to start")
        self._started = True

    def create_cell(self, code: str, cell_type: str = "code") -> str:
        """Create a new idle cell and return its id."""
        cell_id = str(uuid.uuid4())[:8]
        self._cells[cell_id] = _CellData(code=code, cell_type=cell_type, status="idle")
        self._cell_order.append(cell_id)
        return cell_id

    def edit_cell(self, cell_id: str, code: str) -> bool:
        """Replace a cell's code (resetting its output/status)."""
        if cell_id not in self._cells:
            return False
        self._cells[cell_id].code = code
        self._cells[cell_id].status = "idle"
        self._cells[cell_id].output = ""
        self._cells[cell_id].error = ""
        return True

    async def execute_cell(self, cell_id: str, timeout: int = 60) -> dict[str, str | float]:
        """Run one cell in the kernel; return its output/error/time."""
        if cell_id not in self._cells:
            return {"output": "", "error": f"Cell {cell_id} not found", "execution_time": 0.0}
        cell = self._cells[cell_id]
        self._ensure_kernel()
        t0 = time.time()
        request = json.dumps({"code": cell.code}) + "\n"
        try:
            proc = self._process
            if proc is None or proc.stdin is None or proc.stdout is None:
                cell.status = "error"
                cell.error = "Kernel process not available"
                return {"output": "", "error": "Kernel process not available", "execution_time": 0.0}
            proc.stdin.write(request.encode("utf-8"))
            proc.stdin.flush()
            loop = asyncio.get_running_loop()
            try:
                line_bytes = await asyncio.wait_for(
                    loop.run_in_executor(None, proc.stdout.readline),
                    timeout=timeout,
                )
            except TimeoutError:
                cell.status = "error"
                cell.error = f"Execution timed out after {timeout}s"
                cell.execution_time = time.time() - t0
                return {"output": "", "error": cell.error, "execution_time": cell.execution_time}
            result = json.loads(decode_bytes(line_bytes))
            elapsed = time.time() - t0
            cell.output = result.get("output", "")
            cell.error = result.get("error", "")
            cell.status = "completed" if result.get("ok", True) else "error"
            cell.execution_time = elapsed
            return {"output": cell.output, "error": cell.error, "execution_time": elapsed}
        except Exception as e:
            cell.status = "error"
            cell.error = str(e)
            cell.execution_time = time.time() - t0
            return {"output": "", "error": str(e), "execution_time": cell.execution_time}

    async def execute_all(self, timeout: int = 300) -> list[dict[str, str | float]]:
        """Execute every cell in order, returning per-cell results."""
        results: list[dict[str, str | float]] = []
        for cell_id in self._cell_order:
            result = await self.execute_cell(cell_id, timeout=max(timeout // max(len(self._cell_order), 1), 10))
            results.append({"cell_id": cell_id, **result})
        return results

    def get_state(self) -> dict:
        """Return a serialisable snapshot of the whole session."""
        cells = []
        for cell_id in self._cell_order:
            cell = self._cells.get(cell_id)
            if cell is None:
                continue
            cells.append({
                "id": cell_id,
                "code": cell.code,
                "cell_type": cell.cell_type,
                "output": cell.output,
                "error": cell.error,
                "status": cell.status,
                "execution_time": cell.execution_time,
            })
        return {
            "session_id": self.session_id,
            "kernel_name": self.kernel_name,
            "cells": cells,
            "cell_count": len(cells),
        }

    def get_output(self, cell_id: str) -> str:
        """Return the last captured stdout for a cell."""
        cell = self._cells.get(cell_id)
        if cell is None:
            return ""
        return cell.output

    def get_error(self, cell_id: str) -> str:
        """Return the last captured stderr/traceback for a cell."""
        cell = self._cells.get(cell_id)
        if cell is None:
            return ""
        return cell.error

    def delete_cell(self, cell_id: str) -> bool:
        """Remove a cell from the session; return True if it existed."""
        if cell_id not in self._cells:
            return False
        del self._cells[cell_id]
        self._cell_order.remove(cell_id)
        return True

    def close(self) -> None:
        """Shut the kernel down and release the subprocess."""
        if self._process is not None and self._process.stdin is not None:
            try:
                request = json.dumps({"code": "__SHUTDOWN__"}) + "\n"
                self._process.stdin.write(request.encode("utf-8"))
                self._process.stdin.flush()
                self._process.wait(timeout=5)
            except Exception:
                self._process.kill()
        self._started = False
        self._process = None
