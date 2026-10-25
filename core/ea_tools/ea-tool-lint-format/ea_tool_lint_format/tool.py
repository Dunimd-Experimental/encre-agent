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

"""
Real linter / formatter that drives ruff, eslint, prettier and cargo fmt.

The tool is intentionally framework-aware: it inspects the workspace
to pick the right toolchain, invokes the linter / formatter as a real
subprocess, and parses the diagnostic stream into a structured JSON
document that the agent can consume and that the desktop UI can
render as a yellow / red panel.

Supported linters / formatters
------------------------------
- **Python -- ruff** (linter + auto-fixer + formatter).  One tool
  handles ``--check``, ``--fix`` and ``format`` modes, which is why
  this is the single entry point for Python projects.
- **JavaScript / TypeScript -- eslint** (linter + auto-fixer) and
  **prettier** (formatter).  ``auto`` mode runs eslint ``--fix`` then
  prettier ``--write`` on the targeted paths.
- **Rust -- cargo fmt** (formatter) and ``cargo clippy`` (linter).
  ``auto`` mode runs ``cargo fmt`` plus ``cargo clippy --fix``.

All subprocesses are launched with the same window-hiding flags as
the rest of the framework (Windows: ``CREATE_NO_WINDOW``,
Unix: ``start_new_session=True``) so background linting never flashes
a console window in the desktop session.

Output schema
-------------
The tool returns a JSON string with the following fields::

    {
      "linter": "ruff",
      "mode": "check" | "fix" | "format",
      "ok": true | false,
      "duration_s": 1.42,
      "summary": "5 errors, 12 warnings, 34 fixed",
      "diagnostics": [
        {
          "file": "src/example.py",
          "line": 42,
          "column": 5,
          "code": "E501",
          "severity": "error" | "warning" | "info",
          "message": "line too long (110 > 100)",
          "fixed": false
        }
      ],
      "fixed_files": ["src/example.py"],
      "raw_output": "..."
    }
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from encre.tools.base import build_tool
from encre.tools._encoding import decode_bytes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _popen_kwargs() -> dict[str, Any]:
    """Return subprocess kwargs that keep the console window hidden.

    The values come from the shared process-capability helper rather
    than being hard-coded here so that any platform-specific tweak
    (e.g. enabling POSIX job control for Unix) only has to be made in
    one place.
    """
    from encre.capabilities.process import hidden_subprocess_kwargs
    return hidden_subprocess_kwargs()


async def _exec(cmd: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
    """Run *cmd* in *cwd* and capture ``(returncode, stdout, stderr)``.

    The timeout is enforced with :func:`asyncio.wait_for`, which is
    the only reliable way to kill a runaway subprocess without leaking
    the pipe readers. On timeout we kill the child with
    ``ProcessLookupError`` suppressed -- the process may already have
    exited between the timeout firing and the ``kill`` call.

    The return code ``124`` is the conventional timeout sentinel
    (same as GNU ``timeout``) so callers can distinguish "timed out"
    from "exited with a non-zero status" without inspecting the
    stderr string.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_popen_kwargs(),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return 124, "", f"timeout after {timeout}s"
    out = decode_bytes(stdout_b)
    err = decode_bytes(stderr_b)
    return proc.returncode or 0, out, err


# ---------------------------------------------------------------------------
# Framework detection
# ---------------------------------------------------------------------------


def _detect_toolchain(workspace: str, hint: str | None) -> str:
    """Return one of ``"ruff"``, ``"eslint"``, ``"cargo"``.

    The ``hint`` parameter lets the caller force a specific
    toolchain; when omitted we inspect the workspace layout. The
    order of checks matters: a workspace can contain both Python and
    JavaScript files, so the first match wins (Cargo > JS/TS > Python
    > any Python source > any JS/TS source > Python by default).
    """
    if hint and hint in ("ruff", "eslint", "cargo"):
        return hint
    ws = Path(workspace)
    if (ws / "Cargo.toml").exists():
        return "cargo"
    pkg = ws / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        deps = {
            *(data.get("dependencies") or {}).keys(),
            *(data.get("devDependencies") or {}).keys(),
        }
        if "eslint" in deps or "prettier" in deps:
            return "eslint"
    if (ws / "pyproject.toml").exists() or (ws / "setup.cfg").exists() or (ws / "requirements.txt").exists():
        return "ruff"
    if next(ws.rglob("*.py"), None):
        return "ruff"
    if next(ws.rglob("*.js"), None) or next(ws.rglob("*.ts"), None):
        return "eslint"
    return "ruff"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class LintDiagnostic:
    """A single diagnostic emitted by a linter / formatter.

    Field semantics:

    - ``file``: workspace-relative or absolute path to the affected
      file (whichever the underlying tool reported).
    - ``line`` / ``column``: 1-based position within the file; a
      value of ``0`` means the position is unknown (tool did not
      report it).
    - ``code``: rule identifier (``E501``, ``no-unused-vars``,
      ``unnecessary_operation``, etc.).
    - ``severity``: one of ``error`` / ``warning`` / ``info``. The
      mapping is tool-specific (see :func:`_run_ruff` and
      :func:`_run_cargo`).
    - ``fixed``: whether the tool auto-fixed this diagnostic in the
      same run.
    """
    file: str
    line: int = 0
    column: int = 0
    code: str = ""
    severity: str = "info"  # error | warning | info
    message: str = ""
    fixed: bool = False


@dataclass
class LintReport:
    """A whole-run linter / formatter report.

    ``ok`` is ``False`` when the run either produced at least one
    error-severity diagnostic, or the underlying subprocess exited
    non-zero in ``check`` mode. The desktop UI uses ``ok`` to decide
    whether to show a green or red panel; the agent uses ``summary``
    for a quick one-liner.
    """
    linter: str
    mode: str
    ok: bool
    duration_s: float = 0.0
    summary: str = ""
    diagnostics: list[LintDiagnostic] = field(default_factory=list)
    fixed_files: list[str] = field(default_factory=list)
    raw_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise this report to a plain dict for JSON output.

        We rely on :func:`dataclasses.asdict` because both this
        class and :class:`LintDiagnostic` are dataclasses with only
        primitive fields. If any field later becomes an object
        without a JSON-friendly representation, this needs to be
        replaced with an explicit converter.
        """
        return asdict(self)


# ---------------------------------------------------------------------------
# Per-toolchain runners
# ---------------------------------------------------------------------------


async def _run_ruff(workspace: str, paths: list[str], mode: str, timeout: float) -> LintReport:
    """Run ruff in ``check`` / ``fix`` / ``format`` mode.

    ``paths`` is the set of relative paths to operate on; an empty
    list means "the whole workspace".

    In ``fix`` and ``check`` modes we ask ruff for JSON output
    (``--output-format=json``) so the diagnostics can be parsed
    structurally. ``format`` mode does not produce a JSON stream, so
    in that case we fall back to a regex scan of the human-readable
    output.

    The regex fallback in ``check`` mode is only used when ruff
    fails to emit valid JSON (older ruff versions, or when the JSON
    flag is silently ignored); it keeps the tool useful in degraded
    environments rather than crashing on an unparseable stream.
    """
    if mode == "format":
        cmd: list[str] = [sys.executable, "-m", "ruff", "format"]
    elif mode == "fix":
        cmd = [sys.executable, "-m", "ruff", "check", "--fix", "--output-format=json"]
    else:
        cmd = [sys.executable, "-m", "ruff", "check", "--output-format=json"]
    if paths:
        cmd.extend(paths)
    t0 = time.time()
    code, out, err = await _exec(cmd, workspace, timeout)
    duration = round(time.time() - t0, 3)
    report = LintReport(linter="ruff", mode=mode, ok=code == 0, duration_s=duration,
                        raw_output=(out + "\n" + err).strip())
    diagnostics: list[LintDiagnostic] = []
    fixed_files: set[str] = set()
    if mode in ("fix", "check"):
        json_payload = _parse_ruff_json(out)
        if json_payload:
            for entry in json_payload:
                loc = entry.get("location") or {}
                file = str(entry.get("filename") or loc.get("row") or "")
                if file and not os.path.isabs(file):
                    file = str(Path(workspace) / file)
                diagnostics.append(
                    LintDiagnostic(
                        file=file,
                        line=int(loc.get("row") or 0),
                        column=int(loc.get("column") or 0),
                        code=str(entry.get("code") or ""),
                        severity=("error" if (entry.get("code") or "").startswith("E") else "warning"),
                        message=str(entry.get("message") or "").strip(),
                        fixed=bool(entry.get("fix")),
                    )
                )
                if entry.get("fix") and entry.get("filename"):
                    fixed_files.add(str(entry["filename"]))
    # Fall back to regex parsing of the concise output
    if not diagnostics:
        for line in (out + "\n" + err).splitlines():
            m = re.match(r"^(.+?):(\d+):(\d+):\s+([A-Z]+\d+)\s+(.*)$", line.strip())
            if not m:
                continue
            file, ln, col, code_, message = m.groups()
            diagnostics.append(
                LintDiagnostic(
                    file=file,
                    line=int(ln),
                    column=int(col),
                    code=code_,
                    severity=("error" if code_.startswith("E") else "warning"),
                    message=message.strip(),
                )
            )
    report.diagnostics = diagnostics
    report.fixed_files = sorted(fixed_files)
    report.summary = _summarise_diagnostics(diagnostics, fixed_count=len(fixed_files))
    return report


def _parse_ruff_json(output: str) -> list[dict[str, Any]]:
    """Parse ruff's JSON output from a string.

    Returns an empty list rather than raising on malformed input so
    the caller can fall through to the regex fallback without any
    special-casing.
    """
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


async def _run_eslint(workspace: str, paths: list[str], mode: str, timeout: float) -> LintReport:
    """Run eslint + prettier on JavaScript / TypeScript projects.

    The runner picks ``npx`` over ``npm`` when both are on PATH,
    because ``npx --no-install`` skips an implicit install of missing
    dependencies -- running ``npm`` directly would try to fetch the
    eslint / prettier packages from the registry, which is not what
    we want when the caller asked for a project-local run.

    In ``check`` mode we run both eslint (JSON output) and prettier
    ``--check``; the run is ``ok`` only if eslint reports no errors
    *and* prettier reports no files needing formatting. In ``fix``
    and ``format`` modes prettier writes back to disk and its
    rewritten-file list is harvested from stdout (format:
    ``src/foo.ts 45ms (1)``).
    """
    npx = shutil.which("npx") or shutil.which("npm")
    if npx is None:
        return LintReport(
            linter="eslint", mode=mode, ok=False, summary="npx/npm not found in PATH"
        )
    diagnostics: list[LintDiagnostic] = []
    fixed_files: set[str] = set()
    raw_lines: list[str] = []
    t0 = time.time()
    if mode in ("check", "fix"):
        cmd = [npx, "--no-install", "eslint", "--format=json"]
        if mode == "fix":
            cmd.append("--fix")
        if paths:
            cmd.extend(paths)
        code, out, err = await _exec(cmd, workspace, timeout)
        raw_lines.append(out + "\n" + err)
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            payload = []
        for file_entry in payload:
            file_path = str(file_entry.get("filePath") or file_entry.get("filePath") or "")
            for msg in file_entry.get("messages", []) or []:
                rule = msg.get("ruleId") or ""
                severity_raw = msg.get("severity")
                if severity_raw == 2:
                    severity = "error"
                elif severity_raw == 1:
                    severity = "warning"
                else:
                    severity = "info"
                diagnostics.append(
                    LintDiagnostic(
                        file=file_path,
                        line=int(msg.get("line") or 0),
                        column=int(msg.get("column") or 0),
                        code=rule,
                        severity=severity,
                        message=str(msg.get("message") or "").strip(),
                        fixed=bool(msg.get("fix")),
                    )
                )
                if msg.get("fix") and file_path:
                    fixed_files.add(file_path)
    if mode in ("fix", "format"):
        cmd = [npx, "--no-install", "prettier", "--write"]
        if paths:
            cmd.extend(paths)
        else:
            cmd.append(".")
        code, out, err = await _exec(cmd, workspace, timeout)
        raw_lines.append(out + "\n" + err)
        # Prettier writes a list of rewritten files to stdout
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("Checking"):
                continue
            # Prettier output: ``src/foo.ts 45ms (1)``
            m = re.match(r"^(\S+)\s+\d+(?:\.\d+)?(?:ms|s)\b", line)
            if m:
                fixed_files.add(m.group(1))
            elif line and not line.startswith("npm") and not line.startswith("="):
                fixed_files.add(line.split()[0])
    elif mode == "check":
        cmd = [npx, "--no-install", "prettier", "--check"]
        if paths:
            cmd.extend(paths)
        else:
            cmd.append(".")
        code, out, err = await _exec(cmd, workspace, timeout)
        raw_lines.append(out + "\n" + err)
    duration = round(time.time() - t0, 3)
    ok = not any(
        d.severity == "error"
        for d in diagnostics
    ) and (mode != "check" or code == 0)
    report = LintReport(
        linter="eslint", mode=mode, ok=ok, duration_s=duration,
        raw_output="\n".join(raw_lines).strip(),
        diagnostics=diagnostics,
        fixed_files=sorted(fixed_files),
    )
    report.summary = _summarise_diagnostics(diagnostics, fixed_count=len(fixed_files))
    return report


async def _run_cargo(workspace: str, paths: list[str], mode: str, timeout: float) -> LintReport:
    """Run ``cargo fmt`` / ``cargo clippy`` on Rust projects.

    ``cargo fmt --check`` does not emit per-file structured output;
    when it fails it prints a unified diff. Rather than parse the
    diff back into a list of diagnostics (which loses per-line
    accuracy), we record a single summary diagnostic so the agent
    knows formatting is required without getting a wall of noise.

    ``cargo clippy --message-format=json`` does emit structured
    output. We look for the ``compiler-message`` reason so we do not
    confuse a JSON line from another cargo subcommand's output with a
    clippy diagnostic.
    """
    diagnostics: list[LintDiagnostic] = []
    fixed_files: set[str] = set()
    raw_lines: list[str] = []
    t0 = time.time()
    if mode in ("check", "format"):
        cmd = ["cargo", "fmt", "--check"]
        code, out, err = await _exec(cmd, workspace, timeout)
        raw_lines.append(out + "\n" + err)
        if code != 0 and not paths:
            # diff format: not a structured report -- record a single
            # summary diagnostic so the agent knows fmt is unhappy.
            diagnostics.append(
                LintDiagnostic(
                    file="<workspace>",
                    severity="warning",
                    message="cargo fmt --check found formatting differences (run with mode='fix' to apply)",
                )
            )
    if mode in ("fix", "format"):
        code, out, err = await _exec(["cargo", "fmt"], workspace, timeout)
        raw_lines.append(out + "\n" + err)
        # ``cargo fmt`` does not tell us which files it wrote; we
        # scan the workspace for ``.rs`` files as a best-effort
        # approximation of the touched set.
        if code == 0:
            for entry in os.scandir(workspace):
                if entry.is_file() and entry.name.endswith(".rs"):
                    fixed_files.add(entry.name)
    if mode in ("check", "fix"):
        clippy_cmd: list[str] = ["cargo", "clippy", "--message-format=json", "--quiet"]
        if mode == "fix":
            clippy_cmd.append("--fix")
        code, out, err = await _exec(clippy_cmd, workspace, timeout)
        raw_lines.append(out + "\n" + err)
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = obj.get("message") or {}
            if not isinstance(message, dict):
                continue
            if obj.get("reason") != "compiler-message":
                continue
            spans = message.get("spans") or []
            primary = next(
                (s for s in spans if (s.get("is_primary") if isinstance(s, dict) else False)),
                spans[0] if spans else None,
            )
            if not isinstance(primary, dict):
                continue
            file_name = primary.get("file_name") or "<unknown>"
            severity_kind = (message.get("level") or "warning").lower()
            if severity_kind not in ("error", "warning", "note", "help"):
                severity_kind = "warning"
            diagnostics.append(
                LintDiagnostic(
                    file=str(file_name),
                    line=int(primary.get("line_start") or 0),
                    column=int(primary.get("column_start") or 0),
                    code=str((message.get("code") or {}).get("code", "")),
                    severity=severity_kind,
                    message=str(message.get("message") or "").strip(),
                    fixed=False,
                )
            )
    duration = round(time.time() - t0, 3)
    ok = not any(d.severity == "error" for d in diagnostics) and code == 0
    report = LintReport(
        linter="cargo", mode=mode, ok=ok, duration_s=duration,
        raw_output="\n".join(raw_lines).strip(),
        diagnostics=diagnostics,
        fixed_files=sorted(fixed_files),
    )
    report.summary = _summarise_diagnostics(diagnostics, fixed_count=len(fixed_files))
    return report


def _summarise_diagnostics(
    diagnostics: list[LintDiagnostic],
    fixed_count: int = 0,
) -> str:
    """Build a short human-readable summary from a list of diagnostics.

    Returns the literal string ``"clean"`` when there are no
    diagnostics and no fixed files, so the caller can render the
    summary directly in a status panel without extra conditional
    branching.
    """
    if not diagnostics and fixed_count == 0:
        return "clean"
    errors = sum(1 for d in diagnostics if d.severity == "error")
    warnings = sum(1 for d in diagnostics if d.severity == "warning")
    infos = sum(1 for d in diagnostics if d.severity == "info")
    parts: list[str] = []
    if errors:
        parts.append(f"{errors} error{'s' if errors != 1 else ''}")
    if warnings:
        parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
    if infos:
        parts.append(f"{infos} info")
    if fixed_count:
        parts.append(f"{fixed_count} fixed")
    return ", ".join(parts) if parts else "clean"


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------


async def _lint_format_execute(**kwargs: Any) -> str:
    """Run the linter / formatter and return a JSON report.

    The tool inspects the workspace to pick the right toolchain
    unless the caller passes ``linter`` explicitly, dispatches to
    the matching per-toolchain runner, and serialises the resulting
    :class:`LintReport` (plus the input ``workspace`` and ``paths``
    the agent may want to echo back to the UI) to JSON.

    Args:
        **kwargs: Tool arguments. Recognised keys:
            ``workspace`` / ``path`` -- project root (required).
            ``linter`` -- override toolchain (ruff / eslint / cargo).
            ``mode`` -- one of ``check`` (default), ``fix``,
                ``format``.
            ``paths`` / ``files`` -- list of relative paths to
                narrow the run scope; when omitted, the whole
                workspace is processed. Also accepts a comma-
                separated string for LLM convenience.
            ``max_duration`` / ``timeout`` -- subprocess timeout
                in seconds (default 120).

    Returns:
        A JSON string describing the run, or an ``Error:``-prefixed
        string on invalid input.
    """
    workspace = str(kwargs.get("workspace") or kwargs.get("path") or "").strip()
    if not workspace:
        return "Error: workspace is required"
    ws = Path(workspace)
    if not ws.is_dir():
        return f"Error: workspace does not exist or is not a directory: {workspace}"

    linter = _detect_toolchain(workspace, kwargs.get("linter"))
    mode = str(kwargs.get("mode") or "check").lower()
    if mode not in ("check", "fix", "format"):
        return f"Error: mode must be one of check|fix|format, got {mode!r}"

    raw_paths = kwargs.get("paths") or kwargs.get("files")
    if isinstance(raw_paths, str):
        raw_paths = [p.strip() for p in raw_paths.split(",") if p.strip()]
    elif raw_paths is None:
        raw_paths = []
    if not isinstance(raw_paths, list):
        raw_paths = []

    try:
        max_duration = float(kwargs.get("max_duration") or kwargs.get("timeout") or 120.0)
    except (TypeError, ValueError):
        max_duration = 120.0
    if max_duration <= 0:
        max_duration = 120.0

    if linter == "ruff":
        report = await _run_ruff(workspace, raw_paths, mode, max_duration)
    elif linter == "eslint":
        report = await _run_eslint(workspace, raw_paths, mode, max_duration)
    elif linter == "cargo":
        report = await _run_cargo(workspace, raw_paths, mode, max_duration)
    else:
        return f"Error: unsupported linter: {linter}"

    payload = report.to_dict()
    payload["workspace"] = workspace
    payload["paths"] = raw_paths
    return json.dumps(payload, ensure_ascii=False, indent=2)


lint_format_tool = build_tool(
    name="lint_format",
    description=(
        "Run a linter and/or formatter on the workspace and return a "
        "structured JSON report. Supports ruff (Python), eslint + prettier "
        "(JavaScript/TypeScript), and cargo fmt + cargo clippy (Rust). Use "
        "this instead of invoking ruff/eslint/cargo directly via bash -- it "
        "auto-detects the toolchain from project files, parses diagnostics "
        "into structured per-file/line/column entries, and handles window-"
        "hiding for desktop sessions. Mode 'check' reports diagnostics "
        "without writing to disk; 'fix' runs linter auto-fixers (ruff check "
        "--fix, eslint --fix, cargo clippy --fix, cargo fmt); 'format' runs "
        "the formatter (ruff format / prettier --write / cargo fmt). "
        "TIP: Run mode='check' first to see diagnostics before applying "
        "fixes; scope with 'paths' to keep it fast. "
        "AVOID: Running mode='fix' or 'format' across the whole workspace "
        "blindly -- review the check report first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "workspace": {
                "type": "string",
                "description": (
                    "Absolute path to the project root (required). Must be "
                    "an existing directory."
                ),
            },
            "linter": {
                "type": "string",
                "enum": ["ruff", "eslint", "cargo"],
                "description": (
                    "Force a specific toolchain (optional). When omitted the "
                    "tool auto-detects from project files (Cargo.toml, "
                    "package.json, pyproject.toml, etc.)."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["check", "fix", "format"],
                "description": (
                    "Operation mode (optional, default 'check'). 'check' "
                    "reports diagnostics without writing to disk; 'fix' "
                    "applies linter auto-fixes; 'format' runs the formatter."
                ),
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of file or directory paths to operate on, "
                    "relative to the workspace (optional). When omitted the "
                    "entire workspace is processed."
                ),
            },
            "max_duration": {
                "type": "number",
                "description": (
                    "Maximum wall-clock duration in seconds (optional, "
                    "default 120). The subprocess is killed if it overruns."
                ),
            },
        },
    },
    execute=_lint_format_execute,
    intents=["coding", "data", "general"],
    category="code_intel",
    semantic_type="exec",
    is_destructive=lambda args: args.get("action", "") in ("fix", "format"),
    is_concurrency_safe=lambda data: data.get("mode") == "check",
)
