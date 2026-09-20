#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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

"""Behaviour-to-safety-policy reflow: compile user corrections into policies.

The feedback learner (:class:`~encre.improve.feedback.learner.EncreFeedbackLearner`)
remembers the corrections a user applies to tool errors, but those corrections
only ever surfaced as advisory text in the prompt.  This module closes that
loop: it reads the stored corrections, extracts the ones that express a
*permission* preference, compiles them into tightening-only policy entries,
persists them (encrypted, via :mod:`encre.secure_io`) and pushes them into the
Rust permission engine, which is the single source of truth for authorization.

Hard safety invariants
======================
1. **Tightening only.**  A preference that would *widen* access is compiled to
   ``ask`` -- never ``allow``.  ``allow`` is not even a legal value here, and
   the Rust engine drops any ``allow`` entry it is handed.
2. **The dangerous-command blacklist and the immune-path hard deny always win.**
   Learned entries are evaluated in Rust as a floor *combined with* the
   dangerous-pattern check, where ``deny`` beats ``ask``; a learned entry can
   only ever make the verdict stricter, never looser.
3. **Learned ``deny`` takes effect normally.**  Tightening is always safe, so a
   deny compiled from a correction is applied in every permission mode.

Policy entry shape
==================
Each entry targets a tool or a capability and may be scoped by a path fragment
and/or a shell-command fragment, so "never read ~/.secrets" denies only that
path instead of the whole ``file_read`` tool::

    {"tool": "file_read", "path_scope": "~/.secrets", "policy": "deny", ...}

The persisted file is a JSON document written through
:func:`encre.secure_io.write_json`; the same entries are pushed to Rust with
``permission_set_learned_policies`` and can be read back with
``permission_get_learned_policies`` / ``permission_get_policies``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Iterable, Sequence

from encre.improve.feedback.learner import CorrectionRecord
from encre.paths import get_data_dir
from encre.secure_io import read_json, write_json

__all__ = [
    "EncreSafetyPolicyReflow",
    "PolicyEntry",
]

#: Event kind published on the backbone whenever learned policies change.
POLICY_EVENT_KIND = "safety_policy_updated"

#: Legal policy values.  ``allow`` is deliberately absent: a learned policy may
#: only ever restrict access.
_TIGHTENING_POLICIES: frozenset[str] = frozenset({"deny", "ask"})

# -- Intent detection -------------------------------------------------------
# A correction is classified by the *strongest* signal it carries.  Escalation
# phrases are checked first because some of them contain a bare negation
# ("don't ask me, always allow"), which would otherwise read as a deny.
_ESCALATE_STRONG: tuple[str, ...] = (
    "always allow", "always permit", "always run", "always execute",
    "without asking", "without prompting", "don't ask", "do not ask",
    "no need to ask", "no need to prompt", "skip the prompt", "auto approve",
    "automatically allow", "automatically approve", "from now on allow",
    "总是允许", "总是可以", "无需询问", "不用询问", "不要询问", "不要问",
    "自动允许", "自动批准", "直接执行", "以后都允许",
)
_DENY_MARKERS: tuple[str, ...] = (
    "don't", "do not", "never", "avoid", "stop", "prohibit", "forbid",
    "disallow", "must not", "shouldn't", "should not", "refuse", "block",
    "不要", "别", "禁止", "不能", "不得", "不许", "切勿", "勿", "避免", "停止",
)
_ESCALATE_WEAK: tuple[str, ...] = (
    "you may", "you can", "allow", "permit", "approve",
    "允许", "可以", "批准",
)

# -- Extraction patterns ----------------------------------------------------
# Absolute POSIX paths ("/a/b", "~/x") and Windows paths ("C:\\a\\b").
_PATH_RE = re.compile(r"(?:~|/|[A-Za-z]:[\\/])[^\s,;:'\"()\[\]{}<>|]+")
# A command quoted in backticks or straight quotes.
_QUOTED_RE = re.compile(r"`([^`]+)`|\"([^\"]+)\"|'([^']+)'")
# An unquoted command following an explicit run/execute verb.
_RUN_RE = re.compile(
    r"(?:running|run|executing|execute|invoking|invoke|执行|运行)\s+(.+)",
    re.IGNORECASE,
)
# Trailing qualifiers that are not part of the command itself.
_TRAILING_QUALIFIER_RE = re.compile(
    r"\s+(?:without asking|without prompting|from now on|in the future|"
    r"automatically|ever again)\b.*$",
    re.IGNORECASE,
)

#: Tool -> capability, mirroring ``tool_capabilities`` in the Rust engine.
_TOOL_CAPABILITY: dict[str, str] = {
    "file_read": "file", "read_file": "file", "file_write": "file",
    "write_file": "file", "file_edit": "file", "edit_file": "file",
    "apply_patch": "file", "patch": "file", "grep": "file", "glob": "file",
    "find_tool": "file",
    "bash": "bash_io", "bash_output": "bash_io", "shell": "bash_io",
    "execute": "bash_io",
    "rest_client": "network",
    "browser": "browser",
}
_BASH_TOOLS: frozenset[str] = frozenset({"bash", "bash_output", "shell", "execute"})


@dataclass
class PolicyEntry:
    """A single learned permission policy entry.

    Attributes:
        tool: Target tool name (empty when only ``capability`` is set).
        capability: Target capability name (empty when only ``tool`` is set).
        path_scope: When set, the entry applies only if a path extracted from
            the tool arguments contains this fragment.
        command_scope: When set, the entry applies only if the bash command
            extracted from the arguments contains this fragment.
        policy: ``"deny"`` or ``"ask"`` -- never ``"allow"``.
        reason: Human-readable justification surfaced to the user / model.
        rule: Rule identifier surfaced alongside ``ask`` decisions.
        source_tool: The tool the originating correction was recorded for.
        correction: The originating correction text (provenance).
    """

    tool: str = ""
    capability: str = ""
    path_scope: str = ""
    command_scope: str = ""
    policy: str = "ask"
    reason: str = ""
    rule: str = ""
    source_tool: str = ""
    correction: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise the entry (including provenance) for on-disk storage."""
        return {
            "tool": self.tool,
            "capability": self.capability,
            "path_scope": self.path_scope,
            "command_scope": self.command_scope,
            "policy": self.policy,
            "reason": self.reason,
            "rule": self.rule,
            "source_tool": self.source_tool,
            "correction": self.correction,
        }

    def to_native(self) -> dict[str, Any]:
        """Return only the fields the Rust ``LearnedPolicy`` understands."""
        return {
            "tool": self.tool,
            "capability": self.capability,
            "path_scope": self.path_scope,
            "command_scope": self.command_scope,
            "policy": self.policy,
            "reason": self.reason,
            "rule": self.rule,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyEntry":
        """Rebuild an entry from a dict produced by :meth:`to_dict`."""
        return cls(
            tool=str(data.get("tool", "") or ""),
            capability=str(data.get("capability", "") or ""),
            path_scope=str(data.get("path_scope", "") or ""),
            command_scope=str(data.get("command_scope", "") or ""),
            policy=str(data.get("policy", "ask") or "ask"),
            reason=str(data.get("reason", "") or ""),
            rule=str(data.get("rule", "") or ""),
            source_tool=str(data.get("source_tool", "") or ""),
            correction=str(data.get("correction", "") or ""),
        )


class EncreSafetyPolicyReflow:
    """Compiles correction records into tightening-only permission policies.

    Args:
        learner: The feedback learner whose corrections are compiled.  May be
            ``None`` when the caller supplies records directly to
            :meth:`compile_records`.
        storage_path: Where the compiled policy document is persisted.  Defaults
            to ``<data dir>/safety/learned_policies.json``.
        bus: Optional :class:`~encre.events.ledger.EventSourcedBus`; when given,
            every :meth:`apply` publishes a :data:`POLICY_EVENT_KIND` event.
    """

    #: Storage schema version, bumped when the persisted document shape changes.
    SCHEMA_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        learner: Any | None = None,
        *,
        storage_path: str | Path | None = None,
        bus: Any | None = None,
    ) -> None:
        self._learner = learner
        self._storage_path = (
            Path(storage_path)
            if storage_path is not None
            else get_data_dir("safety", "learned_policies.json")
        )
        self._bus = bus
        self._entries: list[PolicyEntry] = []

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def storage_path(self) -> Path:
        """Path of the encrypted policy document."""
        return self._storage_path

    @property
    def entries(self) -> list[PolicyEntry]:
        """The most recently compiled / loaded entries."""
        return list(self._entries)

    # ------------------------------------------------------------------
    # Compilation
    # ------------------------------------------------------------------

    def compile(self) -> list[PolicyEntry]:
        """Compile the learner's current corrections into policy entries."""
        records: Sequence[CorrectionRecord]
        if self._learner is not None and hasattr(self._learner, "iter_records"):
            records = self._learner.iter_records()
        else:
            records = getattr(self._learner, "_records", []) if self._learner else []
        return self.compile_records(records)

    def compile_records(
        self, records: Iterable[CorrectionRecord | dict[str, Any]]
    ) -> list[PolicyEntry]:
        """Compile an explicit iterable of correction records."""
        out: list[PolicyEntry] = []
        for raw in records:
            rec = raw if isinstance(raw, CorrectionRecord) else CorrectionRecord.from_dict(raw)
            out.extend(self._compile_record(rec))
        return self._merge(out)

    def _compile_record(self, rec: CorrectionRecord) -> list[PolicyEntry]:
        """Turn one correction into zero or more policy entries."""
        if getattr(rec, "stale", False):
            return []
        correction = (rec.user_correction or "").strip()
        if not correction:
            return []
        direction = self._direction(correction)
        if direction is None:
            return []
        # INVARIANT 1: an escalation preference becomes `ask`, never `allow`.
        policy = "ask" if direction == "escalate" else "deny"

        tool = (rec.tool_name or "").strip()
        paths = self._extract_paths(correction) or self._extract_paths(rec.error_context or "")
        command = self._extract_command(correction) or self._extract_command(rec.error_context or "")

        reason = f"Learned from user correction: {_clip(correction, 200)}"
        rule = f"learned:{tool or 'any'}:{policy}"

        entries: list[PolicyEntry] = []
        if tool:
            entry = PolicyEntry(
                tool=tool, policy=policy, reason=reason, rule=rule,
                source_tool=tool, correction=correction,
            )
            if paths:
                entry.path_scope = paths[0]
            elif command:
                entry.command_scope = command
            entries.append(entry)
        elif paths or command:
            # No usable tool name: fall back to the capability implied by the
            # target so the entry still has something to bind to.
            capability = "bash_io" if command and not paths else "file"
            entry = PolicyEntry(
                capability=capability, policy=policy, reason=reason, rule=rule,
                source_tool=tool, correction=correction,
            )
            if paths:
                entry.path_scope = paths[0]
            if command:
                entry.command_scope = command
            entries.append(entry)
        return entries

    def _direction(self, correction: str) -> str | None:
        """Classify a correction as ``"deny"``, ``"escalate"`` or ``None``."""
        lowered = correction.lower()
        if any(marker in lowered for marker in _ESCALATE_STRONG):
            return "escalate"
        if any(marker in lowered for marker in _DENY_MARKERS):
            return "deny"
        if any(marker in lowered for marker in _ESCALATE_WEAK):
            return "escalate"
        return None

    def _extract_paths(self, text: str) -> list[str]:
        """Extract absolute path fragments from free text, de-duplicated."""
        if not text:
            return []
        found: list[str] = []
        for match in _PATH_RE.finditer(text):
            path = match.group(0).strip().rstrip(".,;:!?)]}")
            if len(path) < 2 or path in ("/", "~"):
                continue
            if path not in found:
                found.append(path)
        return found

    def _extract_command(self, text: str) -> str:
        """Extract a shell command from free text (quoted, else after a verb)."""
        if not text:
            return ""
        quoted = _QUOTED_RE.search(text)
        if quoted:
            for group in quoted.groups():
                if group:
                    return group.strip().strip("`'\"")
        run = _RUN_RE.search(text)
        if run:
            command = _TRAILING_QUALIFIER_RE.sub("", run.group(1)).strip()
            return command.strip(" .,;:`'\"")
        return ""

    def _merge(self, entries: list[PolicyEntry]) -> list[PolicyEntry]:
        """De-duplicate entries and let a ``deny`` win over a matching ``ask``.

        Also enforces INVARIANT 1 a second time: any non-tightening policy is
        coerced to ``ask`` rather than allowed through.
        """
        merged: dict[tuple[str, str, str, str], PolicyEntry] = {}
        order: list[tuple[str, str, str, str]] = []
        for entry in entries:
            if entry.policy not in _TIGHTENING_POLICIES:
                entry.policy = "ask"
            key = (entry.tool, entry.capability, entry.path_scope, entry.command_scope)
            current = merged.get(key)
            if current is None:
                merged[key] = entry
                order.append(key)
            elif current.policy != "deny" and entry.policy == "deny":
                merged[key] = entry
        return [merged[key] for key in order]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def persist(self, entries: Sequence[PolicyEntry] | None = None) -> Path:
        """Encrypt and atomically write the policy document to disk."""
        payload = {
            "version": self.SCHEMA_VERSION,
            "updated_at": time.time(),
            "entries": [e.to_dict() for e in (entries if entries is not None else self._entries)],
        }
        write_json(self._storage_path, payload)
        return self._storage_path

    def load(self) -> list[PolicyEntry]:
        """Read the policy document back from disk into memory."""
        data = read_json(self._storage_path, default=None)
        raw_entries: list[Any] = []
        if isinstance(data, dict):
            raw_entries = list(data.get("entries") or [])
        elif isinstance(data, list):
            # Tolerate a bare list from an older / hand-written file.
            raw_entries = list(data)
        self._entries = [PolicyEntry.from_dict(d) for d in raw_entries if isinstance(d, dict)]
        return list(self._entries)

    # ------------------------------------------------------------------
    # Rust bridge
    # ------------------------------------------------------------------

    def sync_to_native(self, entries: Sequence[PolicyEntry] | None = None) -> None:
        """Push entries into the Rust permission engine (tightening layer)."""
        from encre import native as _native

        chosen = entries if entries is not None else self._entries
        payload = json.dumps([e.to_native() for e in chosen], ensure_ascii=False)
        _native.permission_set_learned_policies(payload)

    def load_from_native(self) -> list[PolicyEntry]:
        """Read the learned-policy layer back out of the Rust engine."""
        from encre import native as _native

        raw = _native.permission_get_learned_policies()
        parsed = json.loads(raw) if raw else []
        return [PolicyEntry.from_dict(d) for d in parsed if isinstance(d, dict)]

    # ------------------------------------------------------------------
    # End-to-end
    # ------------------------------------------------------------------

    def apply(
        self,
        *,
        persist: bool = True,
        publish: bool = True,
        session_id: str = "",
    ) -> list[PolicyEntry]:
        """Compile, persist and push the current corrections into Rust.

        Args:
            persist: Write the encrypted policy document to disk.
            publish: Publish a :data:`POLICY_EVENT_KIND` event on the backbone
                (only when a bus was supplied).
            session_id: Session id carried on the published event.

        Returns:
            The compiled entries that were applied.
        """
        entries = self.compile()
        self._entries = entries
        if persist:
            self.persist(entries)
        self.sync_to_native(entries)
        if publish and self._bus is not None:
            self._bus.publish(
                POLICY_EVENT_KIND,
                session_id=session_id,
                payload={
                    "count": len(entries),
                    "policies": [e.to_native() for e in entries],
                    "storage_path": str(self._storage_path),
                },
            )
        return entries

    def refresh(self) -> list[PolicyEntry]:
        """Reload the persisted document and re-push it into the Rust engine.

        Called at startup so the engine's in-memory learned layer is rebuilt
        from disk before any tool call is authorized.
        """
        entries = self.load()
        self.sync_to_native(entries)
        return entries


def _clip(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` characters with an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
