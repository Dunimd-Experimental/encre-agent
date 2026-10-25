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

"""Verification evidence ledger (port of Hermes verification_evidence.py).

Tracks which code files were edited this session and what verification evidence
(existing lint/typecheck/test/LSP checks) has been recorded for them, so the
verify-on-stop nudge can be *evidence-driven*: it distinguishes "never checked"
from "checked and failed", and only nags about files that lack passing evidence
after their most recent edit.  Pure state, no loop coupling; independently
testable.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

# Tools that count as verification evidence.  `bash`/`powershell` are included
# because the model may verify through a shell command (pytest, mypy, ...).
_VERIFY_EVIDENCE_TOOLS = frozenset({"test_run", "lint_format", "lsp", "bash", "powershell"})

# How long (seconds) a passing verification remains valid before it is treated
# as stale and a file returns to "unverified".
_VERIFY_EVIDENCE_STALE_SECONDS = 600.0

_NON_CODE_SUFFIXES = (
    ".md", ".markdown", ".rst", ".txt", ".csv", ".json", ".yaml", ".yml",
    ".toml", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".lock",
)
_NON_CODE_NAMES = ("LICENSE", "README", "CHANGELOG", "NOTICE")


@dataclass
class VerificationEvent:
    """A single recorded verification attempt."""

    tool: str
    passed: bool
    summary: str
    timestamp: float = field(default_factory=time.time)
    exit_code: int | None = None
    detail: str = ""


def classify_verification_outcome(tool: str, result: str) -> tuple[bool, str] | None:
    """Classify a verification tool result from its *real* pass/fail signal.

    The tool result envelope carries authoritative outcome data and this is
    the single place that reads it:

    * ``test_run``    -> ``{"passed": N, "failed": M, ...}``: passed iff
      ``failed == 0``.
    * ``lint_format`` -> ``{"ok": bool, ...}``: passed iff ``ok is True``.
    * ``bash``/``powershell`` -> ``{"exit_code": int, ...}``: the shell's own
      success channel; passed iff ``exit_code == 0``.  A command that ran but
      exited nonzero is a *failed* check, never a pass.
    * ``lsp``         -> ``{"diagnostics": [...]}``: passed iff no error-level
      diagnostics remain.

    Returns ``(passed, detail)`` where ``detail`` names the decisive field,
    or ``None`` when no real outcome signal could be extracted (unparseable
    envelope, missing decisive field).  ``None`` MUST be treated as *not
    passed* by callers: a fail-open here is exactly how an unexecuted check
    masquerades as a green one.

    Args:
        tool: The verification tool name (see ``_VERIFY_EVIDENCE_TOOLS``).
        result: The serialized tool result envelope.

    Returns:
        ``(passed, detail)`` on a decisive signal, else ``None``.
    """
    text = (result or "").strip()
    try:
        data: Any = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if tool == "test_run":
        if "failed" in data or "failed_count" in data:
            failed = int(data.get("failed", data.get("failed_count", 0)) or 0)
            return (failed == 0, f"failed={failed}")
        return None
    if tool == "lint_format":
        if "ok" in data:
            ok = bool(data.get("ok"))
            return (ok, f"ok={ok}")
        return None
    if tool in ("bash", "powershell"):
        code = data.get("exit_code")
        if code is None and "success" in data:
            # Envelope without an explicit exit code: fall back to the
            # manager's own success flag (it is derived from the exit code).
            ok = bool(data.get("success"))
            return (ok, f"success={ok}")
        if isinstance(code, int):
            return (code == 0, f"exit_code={code}")
        return None
    if tool == "lsp":
        if "diagnostics" in data:
            diags = data.get("diagnostics")
            n = len(diags) if isinstance(diags, list) else 0
            return (n == 0, f"diagnostics={n}")
        return None
    return None


def _extract_exit_code(result: str) -> int | None:
    """Best-effort extraction of the process exit code from a tool envelope.

    Purely for audit trails on :class:`VerificationEvent`; returns ``None``
    when the envelope does not carry one.

    Args:
        result: The serialized tool result envelope.

    Returns:
        The exit code, or ``None``.
    """
    try:
        data: Any = json.loads((result or "").strip())
    except Exception:
        return None
    if isinstance(data, dict):
        code = data.get("exit_code")
        if isinstance(code, int):
            return code
    return None


class VerificationLedger:
    """In-memory ledger of edited files and their verification evidence.

    A file is considered *verified* only if it has passing evidence recorded
    *after* its most recent edit and that evidence is not stale.  Editing a
    file invalidates any prior passing evidence for it.
    """

    def __init__(self, stale_seconds: float = _VERIFY_EVIDENCE_STALE_SECONDS) -> None:
        self._edited: dict[str, float] = {}
        self._events: list[VerificationEvent] = []
        self._passing_after_edit: set[str] = set()
        self._stale_seconds = stale_seconds

    # ── Recording ───────────────────────────────────────────────────

    def mark_edited(self, path: str) -> None:
        """Record that ``path`` was modified, invalidating prior evidence."""
        if not self._is_verifiable_code(path):
            return
        self._edited[path] = time.time()
        self._passing_after_edit.discard(path)

    def record_verification(
        self,
        tool: str,
        passed: bool,
        summary: str = "",
        paths: list[str] | None = None,
        exit_code: int | None = None,
        detail: str = "",
    ) -> None:
        """Record a verification attempt.

        If ``passed`` and ``paths`` is None, all currently-edited files are
        treated as verified (the common case: the model ran a suite that
        covered its changes).  If ``paths`` is given, only those that were
        edited are marked verified on success.

        Args:
            tool: The verification tool name.
            passed: Whether the check actually passed (from the real outcome
                signal, e.g. exit code or failure count -- never from mere
                "the command executed").
            summary: Short human-readable result summary.
            paths: Optional explicit file targets the evidence applies to.
            exit_code: The process exit code when the evidence came from a
                shell command, for auditability.
            detail: The decisive-field trace from
                :func:`classify_verification_outcome`.
        """
        if tool not in _VERIFY_EVIDENCE_TOOLS:
            return
        self._events.append(VerificationEvent(
            tool=tool, passed=passed, summary=summary,
            exit_code=exit_code, detail=detail,
        ))
        if not passed:
            return
        targets = paths if paths is not None else list(self._edited)
        for p in targets:
            if p in self._edited:
                self._passing_after_edit.add(p)

    # ── Queries ─────────────────────────────────────────────────────

    def edited_files(self) -> list[str]:
        return list(self._edited)

    def file_status(self, path: str) -> str:
        """One of ``unverified`` | ``verified`` | ``stale``."""
        if path not in self._edited:
            return "unverified"
        if path not in self._passing_after_edit:
            return "unverified"
        if time.time() - self._edited[path] > self._stale_seconds:
            return "stale"
        return "verified"

    def unverified_files(self) -> list[str]:
        """Edited files lacking current passing evidence."""
        return [p for p in self._edited if self.file_status(p) != "verified"]

    def verify_events(self) -> list[VerificationEvent]:
        return list(self._events)

    def preservation_notes(self, max_notes: int = 8) -> list[str]:
        """Compact-preservation notes for this session's verification state.

        Used by the compaction phase to build core-internal
        ``preserve_points`` so the summariser cannot drop verification
        evidence that the verify-on-stop gates still need.  Only facts are
        reported: unverified files and failed verification attempts.

        Args:
            max_notes: Upper bound on returned notes.

        Returns:
            Human-readable evidence notes (possibly empty).
        """
        notes: list[str] = []
        unverified = self.unverified_files()
        if unverified:
            shown = ", ".join(f"`{p}`" for p in unverified[:5])
            extra = len(unverified) - 5
            notes.append(
                f"Unverified edited files (still require passing verification): "
                f"{shown}" + (f" (+{extra} more)" if extra > 0 else "")
            )
        for ev in reversed(self._events):
            if ev.passed:
                continue
            notes.append(
                f"Failed verification via `{ev.tool}`: {ev.detail or ev.summary or 'no detail'}"
            )
            if len(notes) >= max_notes:
                break
        return notes[:max_notes]

    def has_failed_evidence(self) -> bool:
        """True if any recorded verification attempt failed since the last edit."""
        return any(not e.passed for e in self._events)

    @staticmethod
    def _is_verifiable_code(path: str) -> bool:
        lower = path.lower()
        if any(lower.endswith(s) for s in _NON_CODE_SUFFIXES):
            return False
        name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        return not any(name == n for n in _NON_CODE_NAMES)

    def build_nudge_message(self, failed: bool = False) -> str:
        """Evidence-driven nudge for verify-on-stop."""
        files = self.unverified_files()[:6]
        if not files:
            return ""
        _files = "\n".join(f"- `{f}`" for f in files)
        _more = f"\n  ... and {len(self.unverified_files()) - 6} more" if len(self.unverified_files()) > 6 else ""
        if failed:
            head = (
                "[VERIFY-ON-STOP] A verification run you performed reported "
                "failures and the task is not complete until the change passes. "
                "Please fix the reported errors and re-run the check on:\n"
            )
        else:
            head = (
                "[VERIFY-ON-STOP] You changed code files this session and no "
                "verification tool has reported passing on them since the change. "
                "The task is not complete until the change is verified. "
                "Run the appropriate lint/typecheck/test tool on:\n"
            )
        return (
            f"{head}{_files}{_more}\n"
            "Run `test_run`, `lint_format`, or `lsp` (or an equivalent command), "
            "then report the result. Fix any errors you find before finishing."
        )

    def build_forced_review_message(self, files: list[str]) -> str:
        """Escalation message when repeated verify nudges were ignored yet a
        verification still failed.  This is the review stage of the
        implement -> verify -> review -> fix loop: instead of silently
        finishing, the agent is told to run a critical review (the
        ``code-review`` skill or a ``critic`` sub-agent) over the failing
        change before it may stop."""
        shown = files[:10]
        _files = "\n".join(f"- `{f}`" for f in shown)
        _more = f"\n  ... and {len(files) - 10} more" if len(files) > 10 else ""
        return (
            "[FORCED-REVIEW] Verification of your change reported failures and "
            "you attempted to finish without fixing them. The task is not "
            "complete until the failing change is reviewed and resolved.\n"
            "Run a critical code review over your changes (activate the "
            "`code-review` skill, or delegate a `critic` sub-agent), address "
            "every reported failure, re-run the failing check, and confirm it "
            "passes before you finish. Review the following files:\n"
            f"{_files}{_more}"
        )

    def build_hard_gate_message(self, files: list[str], remaining: int) -> str:
        """Hard-gate escalation: verification is a non-negotiable requirement
        for delivery.  Fired only after the verify-on-stop nudges and the
        forced-review stage are exhausted, so the model cannot silently claim
        completion over an unverified or failing change.

        Unlike the earlier stages this message frames verification as a
        *project-level regression gate* (build + typecheck + lint + tests),
        not a per-file checkbox, and states the number of hard-gate turns left
        so the bound is transparent to the model.
        """
        shown = files[:10]
        _files = "\n".join(f"- `{f}`" for f in shown)
        _more = f"\n  ... and {len(files) - 10} more" if len(files) > 10 else ""
        return (
            "[VERIFICATION-GATE] The task is NOT complete. Production-grade "
            "delivery requires the full project to verify green -- run the "
            "project's build, typecheck, linter, and test suite and fix every "
            "failure until they pass. Do not summarize, do not skip, do not "
            "claim completion while any edited file lacks passing evidence.\n"
            f"{_files}{_more}\n"
            f"Hard-gate turns remaining: {remaining}. Use them to fix and "
            "re-verify, then report the passing evidence."
        )
