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
Real test runner that drives pytest, vitest, jest and cargo test.

The tool is intentionally framework-aware: it inspects the workspace
to pick the right test runner, invokes it as a real subprocess, and
parses the resulting report into a structured JSON document that the
agent can consume (and that the desktop UI can render as a red/green
test panel).

Supported frameworks
--------------------
- **pytest** -- discovered from ``pyproject.toml`` (``[tool.pytest]``),
  ``pytest.ini``, ``setup.cfg`` or ``tox.ini``.  Output is parsed from
  JUnit XML (``--junit-xml``) which is built into pytest.
- **vitest** -- discovered from ``package.json`` (dependency
  ``vitest`` or config file ``vitest.config.*``).  Output is parsed
  from JSON mode (``--reporter=json``).  # noqa: E402
- **jest** -- discovered from ``package.json`` (dependency ``jest``).
  Output is parsed from JSON mode (``--json``).
- **cargo** -- discovered from ``Cargo.toml``.  Output is parsed from
  Cargo's JSON messages (``--message-format=json``).

All subprocesses are launched with the same window-hiding flags as
the rest of the framework (Windows: ``CREATE_NO_WINDOW``,
Unix: ``start_new_session=True``) so running tests in the background
does not flash a console window in the desktop session.

Output schema
-------------
The tool returns a JSON string with the following fields:

::

    {
      "framework": "pytest",
      "passed": 12,
      "failed": 2,
      "skipped": 1,
      "errors": 0,
      "duration_s": 4.18,
      "tests": [
        {
          "name": "tests/test_foo.py::test_bar",
          "status": "failed" | "passed" | "skipped" | "error",
          "duration_s": 0.21,
          "file": "tests/test_foo.py",
          "line": 42,
          "message": "AssertionError: ...",
          "stdout": ""
        },
        ...
      ],
      "raw_output": "..."
    }
"""

import asyncio
import contextlib
import json
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from encre.tools.base import build_tool
from encre.tools._encoding import decode_bytes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _popen_kwargs() -> dict[str, Any]:
    """Return the platform-specific kwargs used to hide a subprocess console.

    On Windows the returned dict includes ``CREATE_NO_WINDOW`` so that
    ``pytest``, ``jest``, ``vitest``, or ``cargo`` does not flash a
    black console window in the desktop session. On Unix the dict
    contains ``start_new_session=True`` so the child runs in its own
    process group and cannot inherit the desktop's terminal signals.

    The kwargs are imported from :mod:`encre.capabilities.process` at
    call time so that this module can be loaded even on platforms where
    the capabilities module is unavailable, without breaking test
    discovery or documentation builds.

    Returns:
        A dict of keyword arguments safe to splat into
        :func:`asyncio.create_subprocess_exec`.
    """
    from encre.capabilities.process import hidden_subprocess_kwargs
    return hidden_subprocess_kwargs()


async def _exec(cmd: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
    """Run *cmd* in *cwd* and capture (returncode, stdout, stderr).

    Central subprocess helper for every per-framework runner. It
    wraps :func:`asyncio.create_subprocess_exec` with:

    * The window-hiding kwargs from :func:`_popen_kwargs`, so no
      console flash on any platform.
    * A wall-clock timeout enforced by :func:`asyncio.wait_for`. On
      :class:`TimeoutError` the child is killed (best effort) and a
      synthetic return code of ``124`` — the conventional value used
      by GNU ``timeout`` — is returned so downstream callers can
      distinguish "runner exited normally" from "we had to abort it".
    * :func:`decode_bytes` on both streams to handle the platform's
      common non-UTF-8 locale encodings (notably Windows codepages
      used by legacy build tools).

    Args:
        cmd: The argv list to run. No shell interpretation is
            performed — call sites must pass a list of already-quoted
            tokens.
        cwd: The working directory the child should run in. Typically
            the project workspace root.
        timeout: Wall-clock budget in seconds. Zero or negative values
            would hang indefinitely; callers should validate before
            calling this function.

    Returns:
        A 3-tuple ``(returncode, stdout, stderr)``. ``returncode`` is
        ``124`` on timeout, ``0`` when the child exits cleanly, and
        the actual exit code otherwise. ``stdout`` and ``stderr`` are
        decoded strings, never bytes.
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


def _detect_framework(workspace: str, hint: str | None) -> str:
    """Return one of ``"pytest"``, ``"vitest"``, ``"jest"``, ``"cargo"``.

    Framework auto-detection follows a precedence order tuned for the
    most common monorepo and single-project layouts:

    1. An explicit ``hint`` value in the accepted set short-circuits
       everything else — callers who know the framework should use it,
       and it prevents false positives when, say, a Python project
       happens to contain a stray ``package.json``.
    2. ``Cargo.toml`` — Rust projects are unambiguous.
    3. ``package.json`` with a ``vitest`` dependency — checked before
       ``jest`` because some projects list both and prefer vitest.
    4. ``package.json`` with a ``jest`` dependency.
    5. Any of the Python config files (``pyproject.toml``,
       ``pytest.ini``, ``setup.cfg``) — pytest is the default Python
       runner.
    6. Fallback by source-file extension: ``.py`` → pytest,
       ``.js``/``.ts`` → jest.
    7. Last-resort default of ``pytest`` so the tool always returns
       a usable framework name even for empty workspaces.

    Args:
        workspace: Absolute path to the project root to inspect.
        hint: Optional caller-supplied override; one of the accepted
            framework names, or ``None`` to enable auto-detection.

    Returns:
        The detected framework name.
    """
    if hint and hint in ("pytest", "vitest", "jest", "cargo"):
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
        if "vitest" in deps:
            return "vitest"
        if "jest" in deps:
            return "jest"
    if (ws / "pyproject.toml").exists() or (ws / "pytest.ini").exists() or (ws / "setup.cfg").exists():
        return "pytest"
    if next(ws.rglob("*.py"), None):
        return "pytest"
    if next(ws.rglob("*.js"), None) or next(ws.rglob("*.ts"), None):
        return "jest"
    return "pytest"


# ---------------------------------------------------------------------------
# Per-framework runners
# ---------------------------------------------------------------------------


@dataclass
class TestRecord:
    """A single test case's outcome in a :class:`TestReport`.

    Attributes:
        name: The test's fully-qualified name, e.g.
            ``tests/test_foo.py::test_bar`` for pytest, the ``describe``/
            ``it`` path for jest/vitest, or the Rust test name for cargo.
        status: One of ``passed``, ``failed``, ``skipped``, or
            ``error``. ``error`` is used when the test never actually
            ran (e.g. a compile failure in Rust).
        duration_s: Wall-clock time in seconds for this single test.
            Not always available; ``0.0`` is emitted when the runner
            does not report it.
        file: Source file path (relative to the workspace) that
            contains the test. Empty string when the runner did not
            report a location.
        line: Source line number of the test's definition. ``0`` when
            unknown.
        message: Failure message, traceback, or skip reason. Empty
            string when the test passed or no message was reported.
            Truncated to 2000 characters for all frameworks so a
            single stack trace cannot blow up the report.
        stdout: Captured stdout from a test that wrote directly to
            the terminal. Rarely populated; most frameworks buffer
            this and it is surfaced via ``message`` instead.
    """
    name: str
    status: str
    duration_s: float
    file: str = ""
    line: int = 0
    message: str = ""
    stdout: str = ""


@dataclass
class TestReport:
    """Aggregate report returned by every per-framework runner.

    The dataclass is deliberately flat so the same :func:`to_dict`
    method works for all frameworks and the desktop UI can render the
    ``passed``/``failed``/``skipped`` counters uniformly.

    Attributes:
        framework: One of ``pytest``, ``vitest``, ``jest``, ``cargo``.
        passed: Count of tests that passed.
        failed: Count of tests that ran and failed an assertion.
        skipped: Count of tests that were marked as skipped, todo,
            or pending.
        errors: Count of tests that errored out before running (e.g.
            a compile failure).
        duration_s: Total wall-clock time for the runner process,
            including setup.
        tests: Per-test records; may contain an ``<aggregate>`` record
            when a runner did not report individual results.
        raw_output: The runner's raw stdout+stderr, unmodified.
    """
    framework: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    duration_s: float = 0.0
    tests: list[TestRecord] = field(default_factory=list)
    raw_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise this report to a JSON-compatible dict.

        Uses :func:`dataclasses.asdict` so adding new fields later is
        a non-breaking change for callers.

        Returns:
            A dict whose keys match the dataclass's field names and
            whose ``tests`` entry is a list of per-test dicts.
        """
        d = asdict(self)
        return d


async def _run_pytest(workspace: str, test_filter: str | None, timeout: float) -> TestReport:
    """Run pytest and parse its JUnit XML into a :class:`TestReport`.

    The runner launches ``python -m pytest`` with:

    * ``-v`` for verbose per-test output.
    * ``--tb=short`` to keep tracebacks readable.
    * ``--junit-xml <workspace>/.encre/pytest-junit.xml`` — the
      canonical machine-readable report. The XML is written to a
      subdirectory of ``.encre/`` so it does not collide with any
      user-owned files and can be safely cleaned up between runs.
    * ``--color=no`` to keep the raw output free of ANSI escapes that
      would confuse the JSON parser downstream.
    * ``-k <filter>`` when a test filter is supplied.

    After the runner exits, the JUnit XML is parsed test-case by
    test-case. Each test-case element carries an optional ``<failure>``,
    ``<error>``, or ``<skipped>`` child; the presence of any of those
    (in that order of precedence) determines the record's status.
    Failure and error messages are truncated to 2000 characters to
    keep the report small enough for the model's working context.

    If pytest exits before writing the JUnit XML (a syntax error in
    the test files, a pytest config error, etc.), the report is
    returned with ``errors=1`` and only the raw stdout+stderr —
    the caller can surface that to the user.

    Args:
        workspace: Absolute path to the project root.
        test_filter: Optional pytest ``-k`` expression to narrow the
            tests that run. ``None`` runs the full suite.
        timeout: Wall-clock budget in seconds for the pytest process.

    Returns:
        A fully-populated :class:`TestReport` for pytest.
    """
    report = TestReport(framework="pytest")
    junit_path = Path(workspace) / ".encre" / "pytest-junit.xml"
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-v",
        "--tb=short",
        "--junit-xml",
        str(junit_path),
        "--color=no",
    ]
    if test_filter:
        cmd.extend(["-k", test_filter])
    t0 = time.time()
    _code, out, err = await _exec(cmd, workspace, timeout)
    report.duration_s = round(time.time() - t0, 3)
    report.raw_output = (out + "\n" + err).strip()
    if not junit_path.exists():
        # pytest failed before producing a JUnit report; record raw text.
        report.errors = 1
        return report
    try:
        tree = ET.parse(junit_path)
    except ET.ParseError:
        return report
    root = tree.getroot()
    for tc in root.iter("testcase"):
        classname = tc.get("classname", "")
        name = tc.get("name", "")
        file_path = tc.get("file", "")
        line_no = int(tc.get("line") or 0)
        time_s = float(tc.get("time") or 0.0)
        record = TestRecord(
            name=f"{classname}::{name}" if classname else name,
            status="passed",
            duration_s=round(time_s, 4),
            file=file_path,
            line=line_no,
        )
        failure = tc.find("failure")
        error = tc.find("error")
        skipped = tc.find("skipped")
        if failure is not None:
            record.status = "failed"
            record.message = (failure.get("message") or "") + "\n" + (failure.text or "")
            record.message = record.message.strip()[:2000]
            report.failed += 1
        elif error is not None:
            record.status = "error"
            record.message = (error.get("message") or "") + "\n" + (error.text or "")
            record.message = record.message.strip()[:2000]
            report.errors += 1
        elif skipped is not None:
            record.status = "skipped"
            record.message = (skipped.get("message") or "").strip()
            report.skipped += 1
        else:
            report.passed += 1
        report.tests.append(record)
    return report


async def _run_npm_test(
    workspace: str,
    framework: str,
    test_filter: str | None,
    timeout: float,
) -> TestReport:
    """Shared implementation for jest and vitest (both speak JSON).

    Both runners are invoked through ``npx --no-install`` so the
    workspace's local install is used without hitting the npm registry.
    The command shape differs slightly:

    * Vitest is called as ``npx --no-install vitest run --reporter=json
      --outputFile=<devnull>`` — the ``run`` subcommand is required
      because vitest's default mode is watch. The ``--outputFile`` is
      pointed at the OS devnull so the JSON report is emitted to
      stdout where we can capture it.
    * Jest is called as ``npx --no-install jest --json`` — the ``--json``
      flag makes jest print a single JSON document to stdout.

    The JSON payload is parsed with two strategies because runners
    often prefix the report with warnings or ANSI-escaped progress
    lines:

    1. :func:`json.loads` on the entire stdout.
    2. A brace-scanning fallback that tries ``raw_decode`` from every
       ``{`` position until one parses cleanly — this recovers the
       report even when the runner wraps it in prose.

    Aggregate counts are read from either the ``numTotal``/``numPassed``
    shape (jest) or the ``numTotalTests``/``numPassedTests`` shape
    (vitest); both are handled by the same ``or`` chains below.
    ``numTodo`` and ``numPending`` are folded into the ``skipped``
    counter because the desktop UI treats todo/pending as
    effectively-skipped.

    If a runner reports aggregate counts but no per-test breakdown
    (rare, but happens with some CI configs), a synthetic
    ``<aggregate>`` record is appended so the ``tests`` list is never
    empty — the desktop UI keys off ``tests`` for its red/green
    panel.

    Args:
        workspace: Absolute path to the project root.
        framework: One of ``vitest`` or ``jest`` — selects the argv
            shape.
        test_filter: Optional test name substring; both runners accept
            ``-t``.
        timeout: Wall-clock budget in seconds for the runner process.

    Returns:
        A fully-populated :class:`TestReport` for the requested
        framework.
    """
    report = TestReport(framework=framework)
    npx = shutil.which("npx") or shutil.which("npm")
    if npx is None:
        report.errors = 1
        report.raw_output = "npx/npm not found in PATH"
        return report
    bin_name = "vitest" if framework == "vitest" else "jest"
    _devnull = "NUL" if sys.platform == "win32" else "/dev/null"
    cmd: list[str] = [npx, "--no-install", bin_name, "run", "--reporter=json", f"--outputFile={_devnull}"]
    if framework == "jest":
        cmd = [npx, "--no-install", "jest", "--json"]
    if test_filter:
        if framework == "vitest":
            cmd.extend(["-t", test_filter])
        else:
            cmd.extend(["-t", test_filter])
    t0 = time.time()
    _code, out, err = await _exec(cmd, workspace, timeout)
    report.duration_s = round(time.time() - t0, 3)
    report.raw_output = (out + "\n" + err).strip()
    # The JSON document may be the entire stdout; the last JSON
    # object in the output is the actual report (some runners prefix
    # warnings).  Try strict parse, then fall back to a brace scan.
    payload: Any = None
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(out):
            brace = out.find("{", idx)
            if brace == -1:
                break
            try:
                obj, end = decoder.raw_decode(out[brace:])
                payload = obj
                idx = brace + end
            except json.JSONDecodeError:
                idx = brace + 1
    if not isinstance(payload, dict):
        # The runner may have written JSON to a file; nothing to do.
        return report
    num_total = int(payload.get("numTotalTests") or payload.get("numTotal") or 0)
    num_passed = int(payload.get("numPassedTests") or payload.get("numPassed") or 0)
    num_failed = int(payload.get("numFailedTests") or payload.get("numFailed") or 0)
    num_skipped = int(
        payload.get("numPendingTests")
        or payload.get("numPending")
        or payload.get("numSkipped")
        or 0
    )
    num_todo = int(payload.get("numTodoTests") or payload.get("numTodo") or 0)
    report.passed = num_passed
    report.failed = num_failed
    report.skipped = num_skipped + num_todo
    report.errors = 0
    # Build per-test records if available
    test_results = payload.get("testResults") or []
    for tr in test_results:
        file_path = tr.get("name") or tr.get("file") or ""
        for inner in tr.get("testResults", []) or tr.get("assertionResults", []) or []:
            full_name = inner.get("fullName") or inner.get("title") or inner.get("name") or ""
            status_raw = (inner.get("status") or "").lower()
            duration = float(inner.get("duration") or 0.0) / 1000.0
            message = ""
            if status_raw == "passed":
                status = "passed"
            elif status_raw in ("failed", "fail"):
                status = "failed"
                message_lines = inner.get("failureMessages") or []
                message = "\n".join(message_lines)[:2000]
            elif status_raw in ("skipped", "pending", "todo", "disabled"):
                status = "skipped"
            else:
                status = status_raw or "unknown"
            rec = TestRecord(
                name=full_name,
                status=status,
                duration_s=round(duration, 4),
                file=file_path,
                message=message,
            )
            report.tests.append(rec)
    if not test_results:
        # We still have the aggregate counts; emit a single roll-up.
        report.tests.append(
            TestRecord(
                name=f"<{framework} aggregate>",
                status="passed" if report.failed == 0 and report.errors == 0 else "failed",
                duration_s=report.duration_s,
            )
        )
    # ``numTotalTests`` is the most reliable total; if it is
    # inconsistent with the per-test results we still trust the
    # aggregate counts reported by the runner.
    if num_total and len(report.tests) == 0:
        report.tests.append(
            TestRecord(
                name=f"<{framework} aggregate>",
                status="passed" if report.failed == 0 else "failed",
                duration_s=report.duration_s,
            )
        )
    return report


async def _run_cargo(workspace: str, test_filter: str | None, timeout: float) -> TestReport:
    """Run ``cargo test`` and parse its line-delimited JSON events.

    Cargo's JSON output is one object per line. Each line carries a
    top-level ``reason`` field that identifies the event type:

    * ``test`` events (via ``msg["test"]``) describe the state of a
      single Rust test. The ``event`` sub-field is ``ok``, ``failed``,
      or ``ignored``, mapped to the standard ``passed``/``failed``/
      ``skipped`` statuses.
    * ``compiler-message`` events (via ``msg["message"]``) surface
      compile errors. These are appended as ``error`` test records so
      the report reflects that tests did not run because of a build
      failure.

    The runner uses ``--no-fail-fast`` so all tests run even if some
    fail — a partial run would otherwise hide failures later in the
    suite. ``--message-format=json`` is the canonical machine-readable
    mode.

    For each test name, only the *latest* event is used to determine
    status — cargo emits a ``start`` event followed by a terminal
    event, and the ``start`` should not count as a passed/failed
    outcome. Failure messages are extracted from the ``exec.stderr``
    rendered field when present; a 2000-character cap keeps long
    tracebacks from blowing up the report.

    Args:
        workspace: Absolute path to the project root.
        test_filter: Optional Rust test name to narrow the suite.
            Passed as a positional argument after ``cargo test``.
        timeout: Wall-clock budget in seconds for the cargo process.

    Returns:
        A fully-populated :class:`TestReport` for cargo.
    """
    report = TestReport(framework="cargo")
    cmd = ["cargo", "test", "--no-fail-fast", "--message-format=json"]
    if test_filter:
        cmd.append(test_filter)
    t0 = time.time()
    _code, out, err = await _exec(cmd, workspace, timeout)
    report.duration_s = round(time.time() - t0, 3)
    report.raw_output = (out + "\n" + err).strip()
    # Cargo emits one JSON object per line.  Collect ``test`` events
    # which carry a ``test`` name and a status (``ok``, ``failed``,
    # ``ignored``).  Errors are also surfaced as ``exec`` events.
    suite: dict[str, list[dict[str, Any]]] = {}
    failures: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message") or {}
        kind = obj.get("reason") or msg.get("reason") or ""
        if kind == "compiler-message" and msg.get("message"):
            failures.append(
                {
                    "name": "compile",
                    "status": "error",
                    "message": str(msg.get("message"))[:2000],
                }
            )
        if "test" in msg and isinstance(msg["test"], str):
            name = msg["test"]
            event = msg.get("event") or "ok"
            suite.setdefault(name, []).append({"event": event, "exec": obj})
    # Build per-test records from the latest event per name.
    for name, events in suite.items():
        latest = events[-1]
        event = latest["event"]
        status = "passed" if event == "ok" else ("skipped" if event == "ignored" else "failed")
        record = TestRecord(
            name=name,
            status=status,
            duration_s=0.0,
        )
        if status == "failed":
            # Find the matching failure message (the next ``test``
            # event with a ``stdout`` containing ``FAILED`` is rare;
            # fall back to the ``exec.stderr`` if present).
            exec_obj = latest.get("exec") or {}
            rendered = exec_obj.get("message", {}).get("rendered")
            if rendered:
                record.message = str(rendered)[:2000]
            report.failed += 1
        elif status == "skipped":
            report.skipped += 1
        else:
            report.passed += 1
        report.tests.append(record)
    # Attach any compile errors as test records too.
    for f in failures:
        report.tests.append(
            TestRecord(
                name=f.get("name", "compile"),
                status="error",
                duration_s=0.0,
                message=f.get("message", ""),
            )
        )
        report.errors += 1
    return report


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------


async def _test_run_execute(**kwargs: Any) -> str:
    """Run the workspace's test suite and return a structured JSON report.

    This is the callable that the Encre Agent tool-dispatch layer
    invokes once per tool call. The flow is:

    1. **Validate the workspace.** An empty, missing, or non-directory
       workspace is rejected with a plain ``Error: ...`` string rather
       than raising — the tool-dispatch layer treats missing required
       fields as a normal tool failure rather than a harness-level
       exception.
    2. **Detect the framework.** If the caller did not pass an
       explicit ``framework`` hint, :func:`_detect_framework` inspects
       the workspace to pick one. The result drives which runner is
       invoked.
    3. **Validate the timeout.** The ``max_duration`` / ``timeout``
       parameter is coerced to ``float``; invalid or non-positive
       values fall back to the 120-second default so the tool never
       hangs indefinitely on caller error.
    4. **Dispatch to the per-framework runner.** Each runner returns
       a fully-populated :class:`TestReport`.
    5. **Serialise.** The report is converted to a dict via
       :meth:`TestReport.to_dict`, enriched with the resolved
       ``workspace`` and ``filter`` values for caller inspection, and
       emitted as JSON with ``ensure_ascii=False`` so non-ASCII test
       names round-trip correctly.

    Args:
        **kwargs: The caller-supplied tool input. Recognised keys:

            * ``workspace`` (required): absolute path to the project
              root.
            * ``path`` (alias): alternate name for ``workspace``;
              callers that treat this as a path argument can use
              either name.
            * ``framework`` (optional): force one of ``pytest``,
              ``vitest``, ``jest``, or ``cargo``. When omitted,
              auto-detection runs.
            * ``filter`` (optional): a pytest ``-k`` expression, a
              jest/vitest ``-t`` substring, or a cargo test name.
            * ``test_filter`` (alias): alternate name for ``filter``.
            * ``max_duration`` (optional): wall-clock budget in
              seconds for the subprocess. Defaults to ``120``.
            * ``timeout`` (alias): alternate name for ``max_duration``.

    Returns:
        A JSON string containing the :class:`TestReport` fields plus
        ``workspace`` and ``filter``. Empty-result runners still
        return a JSON document with the ``raw_output`` populated.

    Raises:
        Not caught. All validation failures are returned as plain
        ``"Error: ..."`` strings.
    """
    workspace = str(kwargs.get("workspace") or kwargs.get("path") or "").strip()
    if not workspace:
        return "Error: workspace is required"
    ws = Path(workspace)
    if not ws.is_dir():
        return f"Error: workspace does not exist or is not a directory: {workspace}"
    framework = _detect_framework(workspace, kwargs.get("framework"))
    test_filter = kwargs.get("filter") or kwargs.get("test_filter")
    try:
        max_duration = float(kwargs.get("max_duration") or kwargs.get("timeout") or 120.0)
    except (TypeError, ValueError):
        max_duration = 120.0
    if max_duration <= 0:
        max_duration = 120.0

    if framework == "pytest":
        report = await _run_pytest(workspace, test_filter, max_duration)
    elif framework in ("vitest", "jest"):
        report = await _run_npm_test(workspace, framework, test_filter, max_duration)
    elif framework == "cargo":
        report = await _run_cargo(workspace, test_filter, max_duration)
    else:
        return f"Error: unsupported framework: {framework}"

    payload = report.to_dict()
    payload["workspace"] = workspace
    payload["filter"] = test_filter
    return json.dumps(payload, ensure_ascii=False, indent=2)


test_run_tool = build_tool(
    name="test_run",
    description=(
        "Run the project's test suite and return a structured JSON report. "
        "Auto-detects pytest, vitest, jest, and cargo test from project "
        "files. Use this instead of invoking the test runner directly via "
        "bash -- it parses results into per-test status (passed/failed/"
        "skipped/error), duration, file location, and a truncated failure "
        "message, so you can decide which tests to fix and where. Use the "
        "'filter' argument to scope to a single test (pytest -k, "
        "jest/vitest -t, cargo test name). "
        "TIP: After a code change, run with a narrow 'filter' on the "
        "affected tests first, then run the full suite. "
        "AVOID: Running the full suite repeatedly without a filter when "
        "iterating on one test -- use the filter to stay fast."
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
            "framework": {
                "type": "string",
                "enum": ["pytest", "vitest", "jest", "cargo"],
                "description": (
                    "Force a specific test framework (optional). When "
                    "omitted the tool auto-detects from project files "
                    "(Cargo.toml, package.json, pyproject.toml, etc.)."
                ),
            },
            "filter": {
                "type": "string",
                "description": (
                    "Optional test filter: pytest '-k' expression, "
                    "jest/vitest '-t' substring, or a cargo test name."
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
    execute=_test_run_execute,
    intents=["coding", "data", "general"],
    semantic_type="exec",
    cost_level="high",
    retryability="guarded",
    safe_fallback="Scope the test filter, inspect raw failures, or verify the workspace and dependencies before rerunning tests.",
    category="code_intel",
    is_concurrency_safe=lambda _: True,
)
