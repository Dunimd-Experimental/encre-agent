#!/usr/bin/env python3

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

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import functools
import json
import os
import re
import shutil
import sys
from typing import Any

from encre.capabilities.process import (
    BackgroundShellManager,
    TerminalSessionManager,
    get_session_files_dir,
)
from encre.tools._encoding import decode_bytes
from encre.tools.base import build_tool
from encre.tools.runtime import (  # noqa: F401
    _current_workspace,
    _get_progress_emitter,
    _get_workspace,
    _resolve_loop,
    clear_bash_abort_event,
    get_bash_abort_event,
    get_loop_cancel_event,
    get_session_id,
    reset_workspace,
    set_bash_abort_event,
    set_workspace,
)
from encre.tools.tool_ui import TERMINAL_LABELS, ui_dict

_DEFAULT_MAX_CHARS = 30_000
_BINARY_PROBE = 1024
_BINARY_THRESHOLD = 0.30

# `auto` resolves to the platform's recommended default shell.  These are
# recommendations only -- the model can always pass an explicit `terminal`
# to switch.  If the default binary is missing, the fallback chain picks the
# first available shell (so "if it does not exist, use another one").
# Windows: its Rust one-shot shell is cmd.exe, not PowerShell.
# macOS ships zsh as the login shell; Linux standardises on bash.
_PLATFORM_DEFAULT_TERMINAL: dict[str, str] = {
    "win32": "powershell",
    "darwin": "zsh",
    "linux": "bash",
}

# Ordered availability fallbacks per shell type.  The first binary found on
# PATH wins; plain "cmd" on POSIX resolves to nothing and keeps the literal.
_SHELL_FALLBACKS: dict[str, tuple[str, ...]] = {
    "powershell": ("powershell", "pwsh", "cmd"),
    "pwsh": ("pwsh", "powershell", "cmd"),
    "cmd": ("cmd",),
    "zsh": ("zsh", "bash", "sh"),
    "bash": ("bash", "zsh", "sh"),
    "sh": ("sh", "bash"),
}

# Interpreter-style terminals have no meaningful fallback: if the interpreter
# is missing we surface the error instead of silently running a different one.
_NO_FALLBACK = frozenset({"python", "node", "irb", "julia", "lua", "php", "R"})


@functools.cache
def _shell_available(name: str) -> bool:
    """Return True if *name* resolves to an executable on PATH."""
    return shutil.which(name) is not None


def _resolve_terminal(terminal: str) -> str:
    """Map ``auto`` and unavailable shells onto a concrete, usable shell.

    ``auto`` -> platform default (see table above) -> first available entry
    of its fallback chain.  Explicit non-``auto`` terminals only fall back
    when a chain exists for them; interpreter terminals are returned as-is.
    """
    if terminal == "auto":
        terminal = _PLATFORM_DEFAULT_TERMINAL.get(sys.platform, "bash")
    if terminal in _NO_FALLBACK or terminal not in _SHELL_FALLBACKS:
        return terminal
    for cand in _SHELL_FALLBACKS[terminal]:
        if _shell_available(cand):
            return cand
    return terminal


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


def _envelope(
    cmd: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    cwd: str | None,
    elapsed_ms: int,
    sm: dict,
    em: dict,
    max_chars: int,
    interrupted: bool = False,
    timed_out: bool = False,
) -> str:
    sc, st, ss = _trunc(stdout, max_chars)
    ec, et, es = _trunc(stderr, max_chars)
    ok = exit_code == 0 and not timed_out
    if interrupted:
        summary = "command interrupted by user stop"
    elif timed_out:
        summary = f"command timed out after {elapsed_ms} ms and its shell was terminated"
    else:
        summary = "command succeeded" if ok else f"command exited with code {exit_code}"
    if st or et:
        summary += f" (output truncated: {ss + es} chars omitted)"
    if sm.get("binary") or em.get("binary"):
        summary += " [binary detected]"
    return json.dumps(
        {
            "success": ok,
            "exit_code": exit_code,
            "command": cmd,
            "cwd": cwd or "",
            "elapsed_ms": elapsed_ms,
            "stdout": sc,
            "stderr": ec,
            "stdout_truncated": st,
            "stderr_truncated": et,
            "stdout_bytes": sm.get("output_bytes", 0),
            "stderr_bytes": em.get("output_bytes", 0),
            "stdout_binary": sm.get("binary", False),
            "stderr_binary": em.get("binary", False),
            "interrupted": interrupted,
            "timed_out": timed_out,
            "summary": summary,
        },
        ensure_ascii=False,
    )


# --- Outbound network policy gate (permissions.network) --------------------

# Network-capable client verbs and the tail of the command they may carry
# targets in; the scan stops at shell separators so tokens of subsequent
# commands are not mistaken for targets of the client.
_NETWORK_CLIENT_RE = re.compile(r"(?i)\b(curl|wget|ping|nc|ncat|netcat|telnet|ssh|scp|sftp|ftp)\b\s+([^\n;|&]*)")
_NETWORK_URL_HOST_RE = re.compile(r"(?i)\bhttps?://([A-Za-z0-9._-]+)")
_HOST_LIKE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SCHEME_RE = re.compile(r"(?i)^[a-z][a-z0-9+.\-]*://")


def _clean_host(token: str) -> str:
    """Extract a bare hostname from a URL / userinfo / port bearing token."""
    tok = token.strip("\"'")
    tok = _SCHEME_RE.sub("", tok)
    tok = tok.rsplit("@", 1)[-1].split("/")[0]
    tok = tok[1:].split("]", 1)[0] if tok.startswith("[") else tok.split(":", 1)[0]
    tok = tok.strip(".").lower()
    return tok if tok and _HOST_LIKE_RE.match(tok) else ""


def _extract_network_hosts(command: str) -> list[str]:
    """Best-effort extraction of target domains from a network command."""
    hosts: list[str] = []
    for m in _NETWORK_URL_HOST_RE.finditer(command):
        host = _clean_host(m.group(1))
        if host:
            hosts.append(host)
    for m in _NETWORK_CLIENT_RE.finditer(command):
        for tok in m.group(2).split():
            if tok.startswith("-"):
                continue
            host = _clean_host(tok)
            if host:
                hosts.append(host)
    return hosts


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    """Match *host* against the allowlist (exact or fnmatch wildcard)."""
    return any(fnmatch.fnmatchcase(host, pat) for pat in allowlist)


def _network_policy_violation(command: str) -> str:
    """Return a rejection message when *command* violates the network policy.

    Reads ``permissions.network`` through the active loop's config.  An
    empty return means the command may proceed (mode ``allow``, no network
    features detected, or every target domain allowlisted).  In allowlist
    mode a network-capable command whose targets cannot be parsed fails
    closed.
    """
    loop = _resolve_loop()
    config = getattr(loop, "config", None) if loop is not None else None
    if config is None or not hasattr(config, "get_network_policy"):
        return ""
    try:
        mode, allowlist = config.get_network_policy()
    except Exception:
        return ""
    if mode == "allow":
        return ""
    # Reuse the safety engine's network-feature detection so this gate and
    # the permission layer agree on what counts as network access.
    from encre.safety import analyze_bash_command

    if not analyze_bash_command(command).contains_network_access:
        return ""
    hosts = _extract_network_hosts(command)
    if mode == "deny":
        detail = f"targets: {sorted(set(hosts))}" if hosts else "network-capable command"
        return f"blocked by network policy (mode=deny): {detail}"
    blocked = sorted({h for h in hosts if not _host_allowed(h, allowlist)})
    if hosts and not blocked:
        return ""
    detail = f"domains not in allowlist: {blocked}" if blocked else "network targets could not be parsed for the allowlist check"
    return f"blocked by network policy (mode=allowlist): {detail}"


def _record_network_block(reason: str) -> None:
    """Write the interception into the telemetry permission channel."""
    loop = _resolve_loop()
    telemetry = getattr(loop, "telemetry", None) if loop is not None else None
    if telemetry is None:
        return
    # Never crash tool execution on telemetry failure.
    with contextlib.suppress(Exception):
        telemetry.record_permission_decision(
            tool_name="bash",
            effect="deny",
            source="network_policy",
            matched_rule=f"network_blocked: {reason[:200]}",
        )


def _default_shell_cwd(session_id: str) -> str:
    """Directory a shell runs in when the caller passed no ``cwd``.

    This used to fall through to the process launch directory, which made a
    general (no-workspace) session ``ls`` / ``git status`` / ``npm`` inside the
    app's own source tree -- the same leak that put file writes there.  A bound
    workspace keeps its workspace; without one the shell gets the session's own
    files directory, which is where the prompt says output belongs.  An explicit
    ``cwd`` always wins.
    """
    ws = str(_get_workspace() or "").strip()
    if ws and os.path.isdir(ws):
        return ws
    try:
        return str(get_session_files_dir(session_id))
    except Exception:
        return ""


async def _bash_execute(**kwargs: Any) -> str:
    command = kwargs.get("command", "")
    if not command:
        return json.dumps({"success": False, "error": "command is required", "summary": "no command"}, ensure_ascii=False)

    # Network policy gate: refuse the command before any shell is spawned
    # when permissions.network forbids its targets.
    violation = _network_policy_violation(command)
    if violation:
        _record_network_block(f"{violation}; command: {command[:200]}")
        return json.dumps(
            {"success": False, "error": violation, "command": command, "summary": "blocked by network policy"},
            ensure_ascii=False,
        )

    # `auto` -> platform default shell (PowerShell on Windows, zsh on macOS,
    # bash on Linux) with availability fallbacks; the model may always pass
    # an explicit terminal to switch.
    terminal = _resolve_terminal(str(kwargs.get("terminal", "auto")).lower())
    cwd = str(kwargs.get("cwd") or "").strip() or _default_shell_cwd(get_session_id()) or None
    timeout = min(max(1, int(kwargs.get("timeout", 120))), 600)
    max_chars = min(max(0, int(kwargs.get("max_output_chars", _DEFAULT_MAX_CHARS))), 10_000_000)

    if bool(kwargs.get("run_in_background", False)):
        mgr = BackgroundShellManager.instance()
        try:
            rec = await mgr.spawn(
                command, cwd=cwd, terminal=terminal, workspace=_get_workspace(),
            )
        except Exception as exc:
            return json.dumps({"success": False, "error": f"spawn failed: {exc}", "summary": "background spawn failed"}, ensure_ascii=False)
        return json.dumps(
            {
                "success": True,
                "id": rec.id,
                "running": True,
                "command": rec.command,
                "cwd": rec.cwd,
                "started_at": rec.started_at,
                "terminal": terminal,
                "summary": f"background shell started as {rec.id}",
                "hint": "Use bash_output with this id to read output.",
            },
            ensure_ascii=False,
        )

    # Every foreground command runs through the terminal-session path so the
    # user's Stop action kills it immediately (stop_event -> shell killed)
    # and output streams to the frontend while it runs.  The Rust one-shot
    # sandbox_execute is only a fallback for platforms where no session
    # shell could be spawned at all.
    mgr = TerminalSessionManager.instance()
    emitter = _get_progress_emitter()
    on_chunk = emitter if emitter is not None else None
    session_id = get_session_id()
    event = asyncio.Event()
    set_bash_abort_event(session_id, event)
    try:
        result = await mgr.execute(
            terminal,
            command,
            cwd=cwd,
            timeout=timeout,
            on_chunk=on_chunk,
            stop_event=event,
            workspace=_get_workspace(),
        )
    except asyncio.CancelledError:
        # The turn is being aborted: the manager already killed the shell
        # and dropped the session, so propagate the cancellation signal
        # for the loop to emit its usual cancelled-tool tombstone.
        raise
    except FileNotFoundError as exc:
        # No usable session shell on this platform -- fall back to the Rust
        # one-shot sandbox executor (no streaming, no cooperative stop).
        from encre import native as _native

        started = asyncio.get_running_loop().time()
        workspace = _get_workspace()
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, functools.partial(_native.sandbox_execute, command, timeout, workspace)
            )
        except Exception as exc2:
            return json.dumps(
                {
                    "success": False,
                    "error": f"{exc}; fallback failed: {exc2}",
                    "command": command,
                    "terminal": terminal,
                    "cwd": cwd or "",
                    "summary": "terminal error",
                },
                ensure_ascii=False,
            )
        elapsed = int((asyncio.get_running_loop().time() - started) * 1000)
        st, sm = _decode(result.get("stdout", ""))
        et, em = _decode(result.get("stderr", ""))
        return _envelope(command, st, et, int(result.get("exit_code", -1)), cwd, elapsed, sm, em, max_chars)
    except Exception as exc:
        return json.dumps(
            {"success": False, "error": str(exc), "command": command, "terminal": terminal, "cwd": cwd or "", "summary": "terminal error"},
            ensure_ascii=False,
        )
    finally:
        clear_bash_abort_event(session_id)
    st, sm = _decode(result.get("stdout", ""))
    et, em = _decode(result.get("stderr", ""))
    return _envelope(
        command,
        st,
        et,
        result.get("exit_code", 0),
        cwd,
        result.get("elapsed_ms", 0),
        sm,
        em,
        max_chars,
        interrupted=bool(result.get("interrupted")),
        timed_out=bool(result.get("timed_out")),
    )


terminal_tool = build_tool(
    name="bash",
    description=(
        "Execute a shell command in a terminal. "
        "Prefer dedicated tools first (file_read, file_edit, web_search, grep, glob, etc.). "
        "Use bash for builds, installs, custom scripts. "
        "terminal='auto' (default) is the platform's recommended shell -- PowerShell on "
        "Windows, zsh on macOS, bash on Linux -- with automatic fallback if that binary "
        "is missing (powershell->pwsh->cmd, zsh<->bash->sh). This default is a "
        "recommendation, not a constraint: pass an explicit terminal (bash/powershell/"
        "pwsh/cmd/zsh/sh/python/node) to switch whenever the task needs a different "
        "shell; specific terminals run in persistent sessions that preserve cwd/env "
        "across calls. "
        "Permission gate: dangerous commands require user confirmation (allow/deny/ask "
        "policy). The user can stop a running command; stopped commands return whatever "
        "output already streamed, flagged interrupted=true. "
        "Returns JSON: {success, exit_code, stdout, stderr, elapsed_ms, summary}."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command (required)."},
            "terminal": {
                "type": "string",
                "description": "Terminal to use (optional, default "
                "'auto' = platform default: PowerShell on Windows, zsh on macOS, bash on "
                "Linux, with automatic fallback if missing). Explicit values: bash, "
                "powershell, pwsh, cmd, zsh, sh, python, node -- pass one to switch shells "
                "when the task needs it.",
            },
            "timeout": {"type": "integer", "description": "Timeout seconds (default 120, max 600)."},
            "cwd": {
                "type": "string",
                "description": "Working directory (optional). Defaults to the bound workspace; "
                "with no workspace, to this session's own files directory -- never the app's "
                "launch folder. Pass it explicitly whenever the command targets a specific project.",
            },
            "run_in_background": {"type": "boolean", "description": "Run in background and return a shell id for bash_output (optional)."},
            "max_output_chars": {"type": "integer", "description": "Output truncation threshold (default 30000)."},
        },
        "required": ["command"],
    },
    execute=_bash_execute,
    is_concurrency_safe=lambda _: False,
    ui=ui_dict(
        icon="terminal",
        body="terminal",
        result="json",
        keys=("command", "cmd", "input", "shell_command", "script"),
        terminal_labels=dict(TERMINAL_LABELS),
    ).as_dict(),
    intents=["general", "coding", "data"],
    category="shell",
    triggers=["shell", "terminal", "command", "run", "bash"],
    semantic_type="exec",
    is_destructive=True,
    cost_level="high",
    retryability="guarded",
)


# ---------------------------------------------------------------------------
# Background shell session management (previously the ea-tool-bash-io EA
# plugin; inlined so the shell lifecycle lives beside the bash tool that
# spawns the sessions).
# ---------------------------------------------------------------------------


async def _bash_output_execute(**kwargs: Any) -> str:
    """Read new output from a backgrounded shell started via bash."""
    shell_id = str(kwargs.get("id", "")).strip()
    if not shell_id:
        return "Error: id is required"

    mgr = BackgroundShellManager.instance()

    wait = bool(kwargs.get("wait", False))
    if wait:
        try:
            timeout = max(0.0, min(60.0, float(kwargs.get("wait_seconds", 5.0))))
        except (TypeError, ValueError):
            timeout = 5.0
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            snap = mgr.read_new_output(shell_id)
            if "error" in snap:
                return f"Error: {snap['error']}"
            if snap["stdout"] or snap["stderr"] or not snap["running"]:
                return json.dumps(snap, ensure_ascii=False)
            if asyncio.get_running_loop().time() >= deadline:
                return json.dumps(snap, ensure_ascii=False)
            await asyncio.sleep(0.15)

    snap = mgr.read_new_output(shell_id)
    if "error" in snap:
        return f"Error: {snap['error']}"
    return json.dumps(snap, ensure_ascii=False)


async def _bash_kill_execute(**kwargs: Any) -> str:
    """Stop a backgrounded shell started via bash."""
    shell_id = str(kwargs.get("id", "")).strip()
    if not shell_id:
        return "Error: id is required"
    force = bool(kwargs.get("force", False))
    result = await BackgroundShellManager.instance().kill(shell_id, force=force)
    if "error" in result:
        return f"Error: {result['error']}"
    return json.dumps(result, ensure_ascii=False)


async def _bash_list_execute(**_kwargs: Any) -> str:
    """List all backgrounded shells tracked in this session."""
    shells = BackgroundShellManager.instance().list_shells()
    return json.dumps(shells, ensure_ascii=False)


bash_output_tool = build_tool(
    name="bash_output",
    description=(
        "Read new output from a backgrounded shell started via bash with "
        "run_in_background=true. Returns only bytes accumulated since the "
        "last read for that shell id; call repeatedly to stream progress. "
        "With wait=true, blocks up to wait_seconds for new output or for "
        "the shell to exit. "
        "WHEN to use: streaming progress from long-running builds, tests, or "
        "dev servers started in the background. "
        "WHEN NOT to use: for foreground commands (they return directly); "
        "for one-shot commands (use bash without run_in_background). "
        "TIP: Use wait=true for long-running builds/tests so you do not have "
        "to poll repeatedly. "
        "PITFALLS: polling in a tight loop with wait=false returns empty and "
        "burns tokens; output is incremental -- once read, it is not returned "
        "again on the next call."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Shell id returned from bash(run_in_background=true) (required).",
            },
            "wait": {
                "type": "boolean",
                "description": "If true, poll until new bytes arrive or the shell exits (optional, default false).",
            },
            "wait_seconds": {
                "type": "number",
                "description": "Max seconds to wait when wait=true (optional, default 5, capped at 60).",
            },
        },
        "required": ["id"],
    },
    execute=_bash_output_execute,
    is_concurrency_safe=lambda _: True,
    ui=ui_dict(icon="terminal", label="Bash Output", body="terminal", result="json").as_dict(),
    intents=["general", "coding"],
    category="shell",
    semantic_type="exec",
)


bash_kill_tool = build_tool(
    name="bash_kill",
    description=(
        "Stop a backgrounded shell started via bash(run_in_background=true). "
        "By default sends SIGTERM (or terminate on Windows). Pass force=true "
        "to escalate to SIGKILL / hard-terminate after a short grace period. "
        "WHEN to use: dev servers, watchers, or long-running builds that "
        "no longer need to run. "
        "WHEN NOT to use: for foreground commands (they block until done); "
        "for system services (use the service manager); for Docker containers "
        "(use docker stop); for arbitrary system processes (use process_kill). "
        "TIP: Try force=false first for a clean shutdown; escalate to "
        "force=true only if the shell ignores SIGTERM. "
        "PITFALLS: force=true skips cleanup hooks and may leave temp files "
        "behind; on Windows, child processes may survive a non-force kill."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Shell id to kill (required).",
            },
            "force": {
                "type": "boolean",
                "description": "Use SIGKILL / hard-terminate instead of graceful SIGTERM (optional, default false).",
            },
        },
        "required": ["id"],
    },
    execute=_bash_kill_execute,
    is_concurrency_safe=lambda _: True,
    ui=ui_dict(icon="terminal", label="Bash", body="terminal", result="json").as_dict(),
    intents=["general", "coding"],
    category="shell",
    semantic_type="exec",
    is_destructive=True,
)


bash_list_tool = build_tool(
    name="bash_list",
    description=(
        "List all backgrounded shells (running and exited) tracked in this "
        "session. Returns ids, commands, running flags, and exit codes. "
        "WHEN to use: before calling bash_output or bash_kill when you forgot "
        "the shell id; to check whether a background process is still running. "
        "WHEN NOT to use: for foreground commands (they return directly); "
        "for system-wide process listing (use process_list). "
        "TIP: Call this first if you lost track of which background shells "
        "are active. "
        "PITFALLS: exited shells remain in the list until the session ends; "
        "shell ids are session-scoped and not portable across sessions."
    ),
    input_schema={
        "type": "object",
        "properties": {},
    },
    execute=_bash_list_execute,
    is_concurrency_safe=lambda _: True,
    is_readonly=True,
    ui=ui_dict(icon="terminal", label="Bash", body="terminal", result="json").as_dict(),
    intents=["general", "coding"],
    category="shell",
    semantic_type="exec",
)


# ---------------------------------------------------------------------------
# System-wide process management (fills the gap bash_io used to delegate to
# raw shell commands like tasklist / ps).
# ---------------------------------------------------------------------------

_PROTECTED_PIDS = frozenset({0, 1, 4})


def _process_kill_sync(pid: int, force: bool) -> dict[str, Any]:
    """Terminate a process tree via psutil; returns a result dict."""
    import os

    import psutil

    if pid in _PROTECTED_PIDS or pid == os.getpid():
        return {"error": f"refusing to kill protected or own process (pid {pid})"}
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return {"error": f"no such process: {pid}"}
    except psutil.AccessDenied:
        return {"error": f"access denied for pid {pid}"}
    try:
        name = proc.name()
        children = proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        return {"error": str(exc)}
    killed: list[int] = []
    targets = [*children, proc]
    # Graceful pass first (SIGTERM / terminate), then escalate when asked.
    for target in targets:
        try:
            target.terminate()
            killed.append(target.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(targets, timeout=3)
    if alive and force:
        for target in alive:
            try:
                target.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        _, alive = psutil.wait_procs(targets, timeout=2)
    return {
        "pid": pid,
        "name": name,
        "signalled": killed,
        "forced": force,
        "survived": [target.pid for target in alive],
    }


async def _process_kill_execute(**kwargs: Any) -> str:
    """Kill a system process (and its children) by pid."""
    try:
        pid = int(kwargs.get("pid", 0))
    except (TypeError, ValueError):
        return "Error: pid must be an integer"
    if pid <= 0:
        return "Error: pid is required"
    force = bool(kwargs.get("force", False))
    result = await asyncio.get_running_loop().run_in_executor(
        None,
        functools.partial(_process_kill_sync, pid, force),
    )
    if "error" in result:
        return f"Error: {result['error']}"
    return json.dumps(result, ensure_ascii=False)


async def _process_list_execute(**kwargs: Any) -> str:
    """List system processes, sorted by memory usage by default."""
    import psutil

    name_filter = str(kwargs.get("filter", "") or "").strip().lower()
    try:
        limit = max(1, min(200, int(kwargs.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    sort_key = str(kwargs.get("sort", "memory") or "memory").lower()

    rows = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "status", "memory_percent"]):
        info = proc.info
        name = str(info.get("name") or "")
        if name_filter and name_filter not in name.lower():
            continue
        rows.append(
            {
                "pid": info.get("pid"),
                "ppid": info.get("ppid"),
                "name": name,
                "status": info.get("status"),
                "memory_percent": round(float(info.get("memory_percent") or 0.0), 2),
            }
        )
    keymap = {
        "memory": lambda r: (-r["memory_percent"], r["pid"]),
        "name": lambda r: (r["name"].lower(), r["pid"]),
        "pid": lambda r: (r["pid"],),
    }
    rows.sort(key=keymap.get(sort_key, keymap["memory"]))
    return json.dumps({"count": len(rows), "processes": rows[:limit]}, ensure_ascii=False)


process_list_tool = build_tool(
    name="process_list",
    description=(
        "List running system processes with pid, parent pid, name, status, "
        "and memory usage. Sorted by memory usage by default. "
        "WHEN to use: finding a pid before process_kill; checking whether an "
        "app or service is actually running; investigating resource usage. "
        "WHEN NOT to use: for shells started via bash(run_in_background=true) "
        "in this session (use bash_list, which knows their ids). "
        "TIP: Pass filter to narrow by name substring (e.g. 'python') instead "
        "of scanning a long list. "
        "PITFALLS: the list is a snapshot; system-protected entries may hide "
        "their details; memory_percent is relative to total physical memory."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": "Case-insensitive substring matched against the process name (optional).",
            },
            "limit": {
                "type": "integer",
                "description": "Max entries to return (optional, default 50, capped at 200).",
            },
            "sort": {
                "type": "string",
                "enum": ["memory", "name", "pid"],
                "description": "Sort order (optional, default memory descending).",
            },
        },
    },
    execute=_process_list_execute,
    is_concurrency_safe=lambda _: True,
    is_readonly=True,
    ui=ui_dict(icon="terminal", label="Process List", body="terminal", result="json").as_dict(),
    intents=["general", "coding"],
    category="shell",
    semantic_type="read",
)


process_kill_tool = build_tool(
    name="process_kill",
    description=(
        "Terminate a system process by pid, including its child processes. "
        "Sends SIGTERM (terminate) first with a 3s grace period; pass "
        "force=true to escalate to SIGKILL for survivors. "
        "WHEN to use: stopping a stuck app, a leaked daemon, or a process "
        "found via process_list. "
        "WHEN NOT to use: for background shells started via bash (use "
        "bash_kill, which also cleans up session bookkeeping); for system "
        "services (use the service manager); for this agent's own processes."
        "TIP: Run process_list first to confirm the pid and name. "
        "PITFALLS: killing a parent takes down its children; protected pids "
        "(0/1/4) and the agent itself are refused; unsaved state in the "
        "target is lost."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pid": {
                "type": "integer",
                "description": "Process id to terminate (required).",
            },
            "force": {
                "type": "boolean",
                "description": "Escalate to SIGKILL after the grace period (optional, default false).",
            },
        },
        "required": ["pid"],
    },
    execute=_process_kill_execute,
    ui=ui_dict(icon="terminal", label="Process Kill", body="terminal", result="json").as_dict(),
    intents=["general", "coding"],
    category="shell",
    semantic_type="exec",
    is_destructive=True,
)
