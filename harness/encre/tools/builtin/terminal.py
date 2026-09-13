#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 漏 2025-2026 Wenze Wei. All Rights Reserved.
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

from __future__ import annotations

from __future__ import annotations

import asyncio
import functools
import json
import sys
import time
from typing import Any

from encre.tools.base import build_tool
from encre.tools._encoding import decode_bytes
from encre.capabilities.process import BackgroundShellManager
from encre.capabilities.process import TerminalSessionManager

from encre.tools.runtime import (  # noqa: F401
    _current_workspace,
    _get_workspace,
    reset_workspace,
    set_workspace,
)


_DEFAULT_MAX_CHARS = 30_000
_BINARY_PROBE = 1024
_BINARY_THRESHOLD = 0.30

# `auto` = the platform default shell, and must agree with what
# `encre/prompts/environment/shell_*.prompt` tells the model.  Only Windows is
# redirected: its Rust one-shot shell is cmd.exe, not PowerShell.  Linux /
# macOS keep `auto` (Rust one-shot) to retain Landlock / process-group
# isolation 鈥?their shell is already POSIX (bash / sh).
_PLATFORM_DEFAULT_TERMINAL: dict[str, str] = {
    "win32": "powershell",
}


def _resolve_terminal(terminal: str) -> str:
    """Map ``auto`` onto this platform's default shell (see table above)."""
    if terminal != "auto":
        return terminal
    return _PLATFORM_DEFAULT_TERMINAL.get(sys.platform, "auto")


def _decode(value: Any) -> tuple[str, dict]:
    if value is None:
        return "", {"encoding": "utf-8", "binary": False, "output_bytes": 0}
    raw = value.encode("utf-8", errors="replace") if isinstance(value, str) else bytes(value)
    n = len(raw)
    if n == 0:
        return "", {"encoding": "utf-8", "binary": False, "output_bytes": 0}
    sample = raw[:_BINARY_PROBE]
    non_print = sum(1 for b in sample if b < 0x09 or (0x0E <= b <= 0x1F) or b == 0x7F)
    return decode_bytes(raw), {"encoding": "utf-8", "binary": (non_print / max(1, len(sample))) > _BINARY_THRESHOLD, "output_bytes": n}


def _trunc(text: str, limit: int) -> tuple[str, bool, int]:
    if limit <= 0 or len(text) <= limit:
        return text, False, 0
    return text[:limit] + f"\n...(truncated, {len(text) - limit} chars omitted)", True, len(text) - limit


def _envelope(cmd: str, stdout: str, stderr: str, exit_code: int, cwd: str | None,
              elapsed_ms: int, sm: dict, em: dict, max_chars: int) -> str:
    sc, st, ss = _trunc(stdout, max_chars)
    ec, et, es = _trunc(stderr, max_chars)
    ok = exit_code == 0
    summary = "command succeeded" if ok else f"command exited with code {exit_code}"
    if st or et:
        summary += f" (output truncated: {ss + es} chars omitted)"
    if sm.get("binary") or em.get("binary"):
        summary += " [binary detected]"
    return json.dumps({
        "success": ok, "exit_code": exit_code, "command": cmd, "cwd": cwd or "",
        "elapsed_ms": elapsed_ms, "stdout": sc, "stderr": ec,
        "stdout_truncated": st, "stderr_truncated": et,
        "stdout_bytes": sm.get("output_bytes", 0), "stderr_bytes": em.get("output_bytes", 0),
        "stdout_binary": sm.get("binary", False), "stderr_binary": em.get("binary", False),
        "summary": summary,
    }, ensure_ascii=False)


async def _bash_execute(**kwargs: Any) -> str:
    command = kwargs.get("command", "")
    if not command:
        return json.dumps({"success": False, "error": "command is required", "summary": "no command"}, ensure_ascii=False)

    # `auto` -> platform default shell (PowerShell on Windows, zsh on macOS).
    terminal = _resolve_terminal(str(kwargs.get("terminal", "auto")).lower())
    cwd = kwargs.get("cwd") or None
    timeout = min(max(1, int(kwargs.get("timeout", 120))), 600)
    max_chars = min(max(0, int(kwargs.get("max_output_chars", _DEFAULT_MAX_CHARS))), 10_000_000)

    if bool(kwargs.get("run_in_background", False)):
        mgr = BackgroundShellManager.instance()
        try:
            rec = await mgr.spawn(command, cwd=cwd)
        except Exception as exc:
            return json.dumps({"success": False, "error": f"spawn failed: {exc}", "summary": "background spawn failed"}, ensure_ascii=False)
        return json.dumps({"success": True, "id": rec.id, "running": True, "command": rec.command,
                           "cwd": rec.cwd, "started_at": rec.started_at, "terminal": terminal,
                           "summary": f"background shell started as {rec.id}",
                           "hint": "Use bash_output with this id to read output."}, ensure_ascii=False)

    if terminal != "auto":
        mgr = TerminalSessionManager.instance()
        try:
            result = await mgr.execute(terminal, command, cwd=cwd, timeout=timeout)
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc), "command": command,
                               "terminal": terminal, "cwd": cwd or "", "summary": "terminal error"}, ensure_ascii=False)
        st, sm = _decode(result.get("stdout", ""))
        et, em = _decode(result.get("stderr", ""))
        return _envelope(command, st, et, result.get("exit_code", 0), cwd,
                         result.get("elapsed_ms", 0), sm, em, max_chars)

    from encre import native as _native
    started = asyncio.get_running_loop().time()
    workspace = _get_workspace()
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(_native.sandbox_execute, command, timeout, workspace))
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc), "command": command,
                           "cwd": cwd or "", "summary": "execution error"}, ensure_ascii=False)

    elapsed = int((asyncio.get_running_loop().time() - started) * 1000)
    st, sm = _decode(result.get("stdout", ""))
    et, em = _decode(result.get("stderr", ""))
    return _envelope(command, st, et, int(result.get("exit_code", -1)), cwd, elapsed, sm, em, max_chars)


terminal_tool = build_tool(
    name="bash",
    description=(
        "Execute a shell command in a sandboxed terminal. "
        "Prefer dedicated tools first (file_read, file_edit, web_search, grep, glob, etc.). "
        "Use bash for builds, installs, custom scripts. "
        "terminal='auto' is the platform default (PowerShell on Windows, zsh on macOS, "
        "bash on Linux) and is what you should normally pass; specific terminals "
        "(bash/powershell/pwsh/cmd/zsh/python/node) create persistent sessions preserving cwd/env. "
        "Returns JSON: {success, exit_code, stdout, stderr, elapsed_ms, summary}."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command (required)."},
            "terminal": {"type": "string", "description": "Terminal: auto (platform default: PowerShell on Windows, zsh on macOS, bash on Linux), bash, powershell, pwsh, cmd, zsh, python, node (required)."},
            "timeout": {"type": "integer", "description": "Timeout seconds (default 120)."},
            "cwd": {"type": "string", "description": "Working directory (optional)."},
            "run_in_background": {"type": "boolean", "description": "Run in background (optional)."},
            "max_output_chars": {"type": "integer", "description": "Output truncation threshold (default 30000)."},
        },
        "required": ["command", "terminal"],
    },
    execute=_bash_execute,
    is_concurrency_safe=lambda _: False,
    intents=["general", "coding", "data"], category="shell",
    triggers=["shell", "terminal", "command", "run", "bash"],
    semantic_type="exec", is_destructive=True, cost_level="high", retryability="guarded",
)
