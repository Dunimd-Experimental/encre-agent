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

"""Verification gate and versioning for auto-generated skills.

The learning subsystem crystallises repeated tool-use patterns into skills.
Before this module existed those skills were registered the instant they were
generated, which meant a single bad run could permanently pollute the agent's
long-term behaviour.  :class:`SkillVerifier` closes that hole by inserting an
explicit verification gate between *generation* and *activation*.

Lifecycle
---------
Every generated artifact moves through a small state machine::

    proposed ──verify(pass)──> active ──disable()──> disabled
        │
        └──verify(fail)──> rejected

* **proposed** -- written to a durable proposed store; never registered with
  the skill registry and therefore never injected into the main prompt.
* **active** -- verification passed, the artifact is registered with the skill
  registry and its markdown is written with ``status: active`` so it survives a
  restart.
* **rejected** -- verification failed; the artifact and the concrete failure
  reasons are retained in the proposed store for auditing.
* **disabled** -- an active artifact that was rolled back; it is unregistered
  and its markdown is renamed so the loader no longer sees it.

Verification evidence
---------------------
The checks are deliberately *quantifiable* -- no "LLM gave it a 7/10" scoring.
Each check inspects the artifact against ground truth the harness already
holds:

``schema``
    The artifact parses into the expected shape (safe name, description, a
    markdown body with a heading, at least one tool name).
``tools_exist``
    Every referenced tool actually exists in the agent's tool registry.
``no_conflict``
    The name does not collide with an existing non-auto skill, and no other
    *active* artifact already encodes the same tool signature (a duplicate).
``history``
    No referenced tool has a historical failure rate above the configured
    ceiling.  This check is skipped when no failure-history source is wired.

All applicable checks must pass; verification is fail-closed.

Events
------
Every state transition is published on the event backbone
(:class:`~encre.events.ledger.EventSourcedBus`) under the kinds
``skill_proposed``, ``skill_verified``, ``skill_promoted``, ``skill_rejected``
and ``skill_disabled``.

Procedural-memory hook
----------------------
:meth:`SkillVerifier.on_promoted` lets a later subsystem (procedural memory)
observe promotions without this module knowing anything about storage.  The
``skill_promoted`` event is the equivalent, decoupled signal.
"""


import contextlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from encre.logging_config import get_logger
from encre.secure_io import read_json, read_text, write_json, write_text

logger = get_logger("encre.improve.verification")

__all__ = [
    "SkillArtifact",
    "SkillStatus",
    "SkillVerifier",
    "VerificationCheck",
    "VerificationRecord",
    "VerificationResult",
]

#: A skill name must be lowercase and filesystem-safe.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: Matches a ``- `tool` (used N times)`` line in the ``## Tools`` body section.
_TOOL_LINE_RE = re.compile(r"^-\s+`([^`]+)`")


class SkillStatus(str, Enum):
    """Lifecycle status of a generated skill artifact."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    REJECTED = "rejected"
    DISABLED = "disabled"


@dataclass
class VerificationCheck:
    """Outcome of a single, independently reported verification check.

    Attributes:
        name: Check identifier (``schema``, ``tools_exist``, ...).
        passed: Whether the check passed.  A skipped check counts as passed but
            says so in ``detail``.
        detail: Human-readable explanation of the outcome.
    """

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of this check."""
        return {"name": self.name, "passed": self.passed, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VerificationCheck":
        """Rebuild a check from :meth:`to_dict` output."""
        return cls(
            name=str(data.get("name", "")),
            passed=bool(data.get("passed", False)),
            detail=str(data.get("detail", "")),
        )


@dataclass
class VerificationRecord:
    """The auditable result of verifying one artifact.

    Attributes:
        passed: Aggregate verdict; true only when every check passed.
        checks: Individual check outcomes, in evaluation order.
        reasons: Failure reasons (empty when ``passed`` is true).
        verified_at: Epoch timestamp of the verification.
        verifier_version: Version of the verifier rules that produced this
            record, so old verdicts remain interpretable after rule changes.
    """

    passed: bool
    checks: list[VerificationCheck] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    verified_at: float = 0.0
    verifier_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of this record."""
        return {
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "reasons": list(self.reasons),
            "verified_at": self.verified_at,
            "verifier_version": self.verifier_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VerificationRecord":
        """Rebuild a record from :meth:`to_dict` output."""
        return cls(
            passed=bool(data.get("passed", False)),
            checks=[VerificationCheck.from_dict(c) for c in data.get("checks", [])],
            reasons=[str(r) for r in data.get("reasons", [])],
            verified_at=float(data.get("verified_at", 0.0)),
            verifier_version=str(data.get("verifier_version", "")),
        )


@dataclass
class SkillArtifact:
    """A versioned, verifiable skill candidate.

    Attributes:
        name: Skill identifier (lowercase, filesystem-safe).
        description: Short human-readable summary.
        body: Markdown body injected when the skill is active.
        tool_names: Distinct tools the skill's pattern depends on.
        when_to_use: File-extension / applicability hint.
        aliases: Alternative invocation names.
        source: Provenance label (``auto_generated``, ``auto_generated:migrated``).
        trigger: What caused this proposal (e.g. ``repeated_tool_pattern``).
        run_id: Identifier of the run that produced the artifact.
        reason: Free-text rationale recorded at generation time.
        version: Monotonic per-name version, starting at 1.
        rollback_id: Stable, unique handle used for one-click disable/rollback.
        created_at: Epoch timestamp of creation.
        status: Current :class:`SkillStatus`.
        verification: Latest :class:`VerificationRecord`, if verified.
    """

    name: str
    description: str
    body: str
    tool_names: list[str] = field(default_factory=list)
    when_to_use: str = ""
    aliases: list[str] = field(default_factory=list)
    source: str = "auto_generated"
    trigger: str = ""
    run_id: str = ""
    reason: str = ""
    version: int = 1
    rollback_id: str = ""
    created_at: float = 0.0
    status: SkillStatus = SkillStatus.PROPOSED
    verification: VerificationRecord | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of this artifact."""
        return {
            "name": self.name,
            "description": self.description,
            "body": self.body,
            "tool_names": list(self.tool_names),
            "when_to_use": self.when_to_use,
            "aliases": list(self.aliases),
            "source": self.source,
            "trigger": self.trigger,
            "run_id": self.run_id,
            "reason": self.reason,
            "version": self.version,
            "rollback_id": self.rollback_id,
            "created_at": self.created_at,
            "status": self.status.value,
            "verification": self.verification.to_dict() if self.verification else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillArtifact":
        """Rebuild an artifact from :meth:`to_dict` output."""
        raw_status = str(data.get("status", SkillStatus.PROPOSED.value))
        try:
            status = SkillStatus(raw_status)
        except ValueError:
            status = SkillStatus.PROPOSED
        record_data = data.get("verification")
        return cls(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            body=str(data.get("body", "")),
            tool_names=[str(t) for t in data.get("tool_names", [])],
            when_to_use=str(data.get("when_to_use", "")),
            aliases=[str(a) for a in data.get("aliases", [])],
            source=str(data.get("source", "auto_generated")),
            trigger=str(data.get("trigger", "")),
            run_id=str(data.get("run_id", "")),
            reason=str(data.get("reason", "")),
            version=int(data.get("version", 1)),
            rollback_id=str(data.get("rollback_id", "")),
            created_at=float(data.get("created_at", 0.0)),
            status=status,
            verification=(
                VerificationRecord.from_dict(record_data)
                if isinstance(record_data, dict)
                else None
            ),
        )

    def to_skill_def(self) -> dict[str, Any]:
        """Return the plain skill-definition dict used by the skill generator."""
        return {
            "name": self.name,
            "description": self.description,
            "aliases": list(self.aliases),
            "source": self.source,
            "when_to_use": self.when_to_use,
            "body": self.body,
            "generated_at": self.created_at,
            "tool_names": list(self.tool_names),
            "reason": self.reason,
        }


@dataclass
class VerificationResult:
    """Pairing of an artifact with the record produced by verifying it."""

    artifact: SkillArtifact
    record: VerificationRecord

    @property
    def passed(self) -> bool:
        """Whether verification passed."""
        return self.record.passed

    @property
    def status(self) -> SkillStatus:
        """The artifact's status after verification."""
        return self.artifact.status


def _yaml_scalar(value: Any) -> str:
    """Render ``value`` as a YAML-safe scalar (JSON quoting for strings)."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _extract_tools_from_body(body: str) -> list[str]:
    """Collect tool names listed under a ``## Tools`` section.

    Legacy auto-generated markdown files did not persist ``tool_names`` in
    frontmatter; the names survive only as ``- `tool` (used N times)`` bullets.
    This recovers them so a migrated artifact can still be verified against the
    live tool registry.
    """
    tools: list[str] = []
    in_tools = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_tools = stripped[3:].strip().lower() == "tools"
            continue
        if in_tools:
            match = _TOOL_LINE_RE.match(stripped)
            if match:
                name = match.group(1).strip()
                if name and name not in tools:
                    tools.append(name)
    return tools


def _parse_aliases(raw: Any) -> list[str]:
    """Normalise the frontmatter ``aliases`` field into a list of strings."""
    if isinstance(raw, list):
        return [str(a).strip() for a in raw if str(a).strip()]
    if isinstance(raw, str) and raw.strip():
        return [a.strip() for a in raw.split(",") if a.strip()]
    return []


class SkillVerifier:
    """Verification gate that promotes or rejects generated skill artifacts.

    Args:
        agent: The owning agent.  Used to reach the ``tool_registry`` (ground
            truth for ``tools_exist``), the ``skill_registry`` (promotion and
            conflict checks) and an optional event bus.  May be ``None`` for a
            standalone verifier, in which case the tool check fails closed.
        bus: Explicit event backbone.  When omitted the verifier reuses
            ``agent.event_bus`` / ``agent.bus`` if present, else creates an
            in-memory :class:`~encre.events.ledger.EventSourcedBus`.
        skills_dir: Directory holding auto-generated artifacts and the proposed
            store.  Defaults to ``<data dir>/skills/auto_generated``.
        failure_stats: Optional ``() -> dict[str, float]`` provider mapping tool
            name to a historical failure rate in ``[0, 1]``.  When absent the
            ``history`` check is skipped.
        tool_name_provider: Optional ``() -> Iterable[str]`` override for the
            set of valid tool names (defaults to the agent's tool registry).
        max_failure_rate: Failure-rate ceiling for the ``history`` check.
    """

    #: Bumped whenever the verification rules change, so stored verdicts can be
    #: interpreted against the rule set that produced them.
    VERIFIER_VERSION = "1.0"

    def __init__(
        self,
        agent: Any = None,
        *,
        bus: Any = None,
        skills_dir: str | Path | None = None,
        failure_stats: Callable[[], dict[str, float]] | None = None,
        tool_name_provider: Callable[[], Any] | None = None,
        max_failure_rate: float = 0.5,
    ) -> None:
        self._agent = agent
        self._bus = bus
        self._skills_dir = Path(skills_dir) if skills_dir is not None else self._default_skills_dir()
        self._failure_stats = failure_stats
        self._tool_name_provider = tool_name_provider
        self._max_failure_rate = float(max_failure_rate)
        self._lock = threading.Lock()
        self._artifacts: list[SkillArtifact] = []
        self._on_promoted: list[Callable[[SkillArtifact], None]] = []
        self._load_store()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def bus(self) -> Any:
        """Return the event backbone this verifier publishes to."""
        return self._get_bus()

    @property
    def skills_dir(self) -> Path:
        """Return the directory holding auto-generated artifacts."""
        return self._skills_dir

    def all_artifacts(self) -> list[SkillArtifact]:
        """Return every artifact known to this verifier (any status)."""
        return list(self._artifacts)

    def pending(self) -> list[SkillArtifact]:
        """Return artifacts still awaiting verification."""
        return [a for a in self._artifacts if a.status == SkillStatus.PROPOSED]

    def active(self) -> list[SkillArtifact]:
        """Return artifacts that passed verification."""
        return [a for a in self._artifacts if a.status == SkillStatus.ACTIVE]

    def rejected(self) -> list[SkillArtifact]:
        """Return artifacts whose verification failed."""
        return [a for a in self._artifacts if a.status == SkillStatus.REJECTED]

    def get(self, key: str) -> SkillArtifact | None:
        """Look an artifact up by rollback id, then by name (active first)."""
        if not key:
            return None
        for artifact in self._artifacts:
            if artifact.rollback_id == key:
                return artifact
        matches = [a for a in self._artifacts if a.name == key]
        if not matches:
            return None
        for artifact in matches:
            if artifact.status == SkillStatus.ACTIVE:
                return artifact
        return matches[-1]

    # ------------------------------------------------------------------
    # Proposal
    # ------------------------------------------------------------------

    def propose(
        self,
        skill_def: dict[str, Any],
        *,
        run_id: str = "",
        trigger: str = "",
        source: str = "auto_generated",
    ) -> SkillArtifact:
        """Turn a generated skill definition into a proposed artifact.

        The artifact is recorded in the durable proposed store and published as
        ``skill_proposed``, but is **not** registered with the skill registry --
        unverified output never reaches the main prompt.

        Identical content already proposed or active is de-duplicated: the
        existing artifact is returned instead of minting a new version.

        Args:
            skill_def: Dictionary from
                :meth:`~encre.improve.learning.skill_generator.SkillGenerator.generate`.
            run_id: Identifier of the originating run.
            trigger: What triggered generation.
            source: Provenance label.

        Returns:
            The newly created (or de-duplicated) :class:`SkillArtifact`.

        Raises:
            ValueError: If ``name``, ``description`` or ``body`` is missing.
        """
        name = str(skill_def.get("name", "")).strip()
        description = str(skill_def.get("description", "")).strip()
        body = str(skill_def.get("body", "")).strip()
        if not name or not description or not body:
            raise ValueError("skill_def requires non-empty name, description and body")

        raw_tools = skill_def.get("tool_names") or []
        tool_names = sorted({str(t).strip() for t in raw_tools if str(t).strip()})
        aliases = [str(a) for a in (skill_def.get("aliases") or [])]

        with self._lock:
            duplicate = self._find_duplicate(name, body)
            if duplicate is not None:
                return duplicate

            version = self._next_version(name)
            artifact = SkillArtifact(
                name=name,
                description=description,
                body=body,
                tool_names=tool_names,
                when_to_use=str(skill_def.get("when_to_use", "")),
                aliases=aliases,
                source=source,
                trigger=trigger or str(skill_def.get("reason", "")),
                run_id=run_id,
                reason=str(skill_def.get("reason", "")),
                version=version,
                rollback_id=self._new_rollback_id(name, version),
                created_at=time.time(),
                status=SkillStatus.PROPOSED,
            )
            self._artifacts.append(artifact)
            self._persist_locked()

        self._publish("skill_proposed", artifact)
        logger.info(
            "Proposed skill %s v%d (rollback_id=%s, %d tools)",
            artifact.name, artifact.version, artifact.rollback_id, len(artifact.tool_names),
        )
        return artifact

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self, artifact: SkillArtifact | str) -> VerificationResult:
        """Verify an artifact and promote or reject it accordingly.

        Args:
            artifact: A :class:`SkillArtifact` or a rollback id / name.

        Returns:
            The :class:`VerificationResult` (artifact plus record).

        Raises:
            KeyError: If a string key cannot be resolved.
        """
        if isinstance(artifact, str):
            resolved = self.get(artifact)
            if resolved is None:
                raise KeyError(f"unknown skill artifact: {artifact}")
            artifact = resolved

        # Already active: nothing to do, keep the prior verdict.
        if artifact.status == SkillStatus.ACTIVE and artifact.verification is not None:
            return VerificationResult(artifact=artifact, record=artifact.verification)

        # Disabled artifacts must be re-proposed before they can be verified.
        if artifact.status == SkillStatus.DISABLED:
            record = VerificationRecord(
                passed=False,
                checks=[VerificationCheck("status", False, "artifact is disabled")],
                reasons=["artifact is disabled; re-propose to verify"],
                verified_at=time.time(),
                verifier_version=self.VERIFIER_VERSION,
            )
            return VerificationResult(artifact=artifact, record=record)

        checks = self._run_checks(artifact)
        reasons = [f"{c.name}: {c.detail}" for c in checks if not c.passed]
        record = VerificationRecord(
            passed=not reasons,
            checks=checks,
            reasons=reasons,
            verified_at=time.time(),
            verifier_version=self.VERIFIER_VERSION,
        )
        artifact.verification = record

        self._publish(
            "skill_verified",
            artifact,
            {
                "passed": record.passed,
                "reasons": list(record.reasons),
                "checks": [c.to_dict() for c in record.checks],
            },
        )

        if record.passed:
            self.promote(artifact)
        else:
            artifact.status = SkillStatus.REJECTED
            with self._lock:
                self._persist_locked()
            self._publish("skill_rejected", artifact, {"reasons": list(record.reasons)})
            logger.info("Rejected skill %s: %s", artifact.name, "; ".join(record.reasons))

        return VerificationResult(artifact=artifact, record=record)

    def verify_pending(self) -> list[VerificationResult]:
        """Verify every proposed artifact and return the results in order."""
        return [self.verify(a) for a in self.pending()]

    def _run_checks(self, artifact: SkillArtifact) -> list[VerificationCheck]:
        """Run every applicable verification check against ``artifact``."""
        return [
            self._check_schema(artifact),
            self._check_tools_exist(artifact),
            self._check_no_conflict(artifact),
            self._check_history(artifact),
        ]

    def _check_schema(self, artifact: SkillArtifact) -> VerificationCheck:
        """Check the artifact parses into a usable skill shape."""
        problems: list[str] = []
        if not artifact.name or not _NAME_RE.match(artifact.name):
            problems.append("invalid or unsafe skill name")
        if not artifact.description.strip():
            problems.append("missing description")
        if not artifact.body.strip():
            problems.append("empty body")
        elif "# " not in artifact.body:
            problems.append("body has no markdown heading")
        if not artifact.tool_names:
            problems.append("no tool names recorded")
        elif not all(isinstance(t, str) and t.strip() for t in artifact.tool_names):
            problems.append("invalid tool name entries")
        return VerificationCheck("schema", not problems, "; ".join(problems) or "ok")

    def _check_tools_exist(self, artifact: SkillArtifact) -> VerificationCheck:
        """Check every referenced tool exists in the live tool registry."""
        available = self._available_tool_names()
        if available is None:
            return VerificationCheck("tools_exist", False, "tool registry unavailable")
        missing = sorted({t for t in artifact.tool_names if t not in available})
        if missing:
            return VerificationCheck(
                "tools_exist", False, f"unknown tools: {', '.join(missing)}"
            )
        return VerificationCheck(
            "tools_exist", True, f"{len(set(artifact.tool_names))} tool(s) verified"
        )

    def _check_no_conflict(self, artifact: SkillArtifact) -> VerificationCheck:
        """Check the artifact neither collides with nor duplicates a skill."""
        registry = self._skill_registry()
        if registry is not None:
            existing = registry.lookup(artifact.name)
            if existing is not None:
                from encre.skills.types import SkillSource

                if existing.source != SkillSource.BUNDLED:
                    return VerificationCheck(
                        "no_conflict",
                        False,
                        f"name collides with existing {existing.source.value} skill",
                    )
        signature = frozenset(artifact.tool_names)
        if signature:
            for other in self._artifacts:
                if other is artifact or other.name == artifact.name:
                    continue
                if other.status == SkillStatus.ACTIVE and frozenset(other.tool_names) == signature:
                    return VerificationCheck(
                        "no_conflict",
                        False,
                        f"duplicates active skill '{other.name}' (same tool signature)",
                    )
        return VerificationCheck("no_conflict", True, "no conflict")

    def _check_history(self, artifact: SkillArtifact) -> VerificationCheck:
        """Check referenced tools have not historically failed too often."""
        stats = self._failure_rates()
        if not stats:
            return VerificationCheck(
                "history", True, "no failure-history source; check skipped"
            )
        offenders: list[str] = []
        for tool in sorted(set(artifact.tool_names)):
            rate = float(stats.get(tool, 0.0) or 0.0)
            if rate > self._max_failure_rate:
                offenders.append(f"{tool}={rate:.2f}")
        if offenders:
            return VerificationCheck(
                "history",
                False,
                f"failure rate above {self._max_failure_rate:.2f}: {', '.join(offenders)}",
            )
        return VerificationCheck("history", True, "failure rates within threshold")

    # ------------------------------------------------------------------
    # Promotion / rollback
    # ------------------------------------------------------------------

    def promote(self, artifact: SkillArtifact) -> bool:
        """Activate a verified artifact (register it and persist its markdown).

        Args:
            artifact: The artifact to activate.

        Returns:
            ``True`` when the artifact became active, ``False`` when it is
            disabled (and therefore cannot be promoted).
        """
        if artifact.status == SkillStatus.DISABLED:
            return False

        artifact.status = SkillStatus.ACTIVE
        self._register_skill(artifact)
        self._write_active_md(artifact)
        with self._lock:
            self._persist_locked()
        self._publish(
            "skill_promoted",
            artifact,
            {"verified_at": artifact.verification.verified_at if artifact.verification else 0.0},
        )
        logger.info("Promoted skill %s v%d to active", artifact.name, artifact.version)

        # Reserved procedural-memory hook: callbacks are isolated so a memory
        # writer can never break promotion.
        for callback in list(self._on_promoted):
            try:
                callback(artifact)
            except Exception:
                logger.warning("on_promoted callback failed for %s", artifact.name, exc_info=True)
        return True

    def disable(self, key: str) -> bool:
        """One-click rollback: deactivate an artifact and unregister it.

        Args:
            key: Rollback id or skill name.

        Returns:
            ``True`` when an artifact was disabled, ``False`` when the key is
            unknown or the artifact was already disabled.
        """
        artifact = self.get(key)
        if artifact is None or artifact.status == SkillStatus.DISABLED:
            return False
        artifact.status = SkillStatus.DISABLED
        self._unregister_skill(artifact)
        self._remove_active_md(artifact)
        with self._lock:
            self._persist_locked()
        self._publish("skill_disabled", artifact, {"reason": "manual rollback"})
        logger.info("Disabled skill %s (rollback_id=%s)", artifact.name, artifact.rollback_id)
        return True

    def rollback(self, key: str) -> bool:
        """Alias for :meth:`disable`; provided for a clearer call site."""
        return self.disable(key)

    def on_promoted(self, callback: Callable[[SkillArtifact], None]) -> None:
        """Register a callback fired after every successful promotion.

        This is the reserved seam for procedural memory (Task 4): the callback
        receives the active :class:`SkillArtifact` and may persist it.  The
        equivalent decoupled signal is the ``skill_promoted`` bus event.
        """
        self._on_promoted.append(callback)

    # ------------------------------------------------------------------
    # Legacy migration
    # ------------------------------------------------------------------

    def migrate_legacy(self) -> int:
        """Mark legacy auto-generated skills as proposed.

        Files written by the pre-verification generator have no ``status`` in
        their frontmatter and were loaded (and injected) unconditionally.  This
        rewrites each such file with ``status: proposed``, records it in the
        proposed store so the next verification round can promote it, and
        removes it from the in-memory skill registry if it was already loaded.

        Idempotent: files already carrying a ``status`` are left untouched.

        Returns:
            The number of files migrated.
        """
        directory = self._skills_dir
        if not directory.is_dir():
            return 0

        from encre.skills.registry import parse_yaml_frontmatter

        migrated = 0
        for path in sorted(directory.glob("*.md")):
            # Tolerant read: legacy plaintext files come back as-is, files the
            # verifier wrote encrypted are decrypted transparently.
            content = read_text(path)
            if not content:
                continue
            metadata, body = parse_yaml_frontmatter(content)
            name = str(metadata.get("name", "")).strip()
            if not name:
                continue
            status = str(metadata.get("status", "")).strip().lower()
            if status:
                # Already carries an explicit lifecycle status (including
                # artifacts promoted by the new flow).
                continue
            if self.get(name) is not None:
                continue

            artifact = SkillArtifact(
                name=name,
                description=str(metadata.get("description", "")).strip(),
                body=body.strip(),
                tool_names=_extract_tools_from_body(body),
                when_to_use=str(metadata.get("when_to_use", "")).strip(),
                aliases=_parse_aliases(metadata.get("aliases")),
                source="auto_generated:migrated",
                trigger="legacy_migration",
                reason="legacy auto_generated skill migrated to proposed",
                version=1,
                rollback_id=self._new_rollback_id(name, 1),
                created_at=time.time(),
                status=SkillStatus.PROPOSED,
            )
            with self._lock:
                self._artifacts.append(artifact)
            # Rewrite the frontmatter in place so the registry loader skips it.
            # Encrypted at rest, like every other runtime-generated artifact.
            try:
                write_text(path, self._render_md(artifact, SkillStatus.PROPOSED))
            except Exception:
                logger.warning("Failed to rewrite legacy skill %s", path, exc_info=True)
                continue

            registry = self._skill_registry()
            if registry is not None:
                with contextlib.suppress(Exception):
                    registry.unregister(name)
            migrated += 1

        if migrated:
            with self._lock:
                self._persist_locked()
            logger.info("Migrated %d legacy auto-generated skill(s) to proposed", migrated)
        return migrated

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _default_skills_dir() -> Path:
        """Return ``<data dir>/skills/auto_generated`` without creating it."""
        from encre.config import get_data_dir

        return get_data_dir("skills", "auto_generated", ensure=False)

    def _store_path(self) -> Path:
        """Return the path of the durable proposed-artifact store."""
        return self._skills_dir / "proposed.json"

    def _load_store(self) -> None:
        """Load previously proposed artifacts from disk, tolerating corruption.

        The store is encrypted at rest (see :meth:`_persist_locked`); the
        tolerant read also keeps legacy plaintext stores readable so an old
        build's data migrates transparently on the next persist.
        """
        path = self._store_path()
        if not path.is_file():
            return
        data = read_json(path)
        if data is None:
            logger.warning("Could not read proposed skill store %s", path)
            return
        records = data.get("artifacts", []) if isinstance(data, dict) else []
        for item in records:
            if not isinstance(item, dict):
                continue
            try:
                self._artifacts.append(SkillArtifact.from_dict(item))
            except (TypeError, ValueError):
                logger.warning("Skipping malformed artifact record in %s", path)

    def _persist_locked(self) -> None:
        """Write the proposed store atomically, encrypted at rest.

        The store carries model-produced skill bodies and metadata, so it is
        persisted as AES-256-GCM ciphertext through :mod:`encre.secure_io`.
        Caller holds ``_lock``.
        """
        path = self._store_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"artifacts": [a.to_dict() for a in self._artifacts]}
            # write_json encrypts before the atomic replace: an encryption
            # failure raises before anything touches the disk, so no plaintext
            # copy can land on storage.
            write_json(path, payload)
        except Exception:
            logger.warning("Failed to persist proposed skill store %s", path, exc_info=True)

    def _find_duplicate(self, name: str, body: str) -> SkillArtifact | None:
        """Return an existing artifact with identical name and body, if any."""
        for artifact in self._artifacts:
            if artifact.name == name and artifact.body.strip() == body.strip():
                if artifact.status in (SkillStatus.PROPOSED, SkillStatus.ACTIVE):
                    return artifact
        return None

    def _next_version(self, name: str) -> int:
        """Return the next version number for ``name``."""
        versions = [a.version for a in self._artifacts if a.name == name]
        return (max(versions) + 1) if versions else 1

    @staticmethod
    def _new_rollback_id(name: str, version: int) -> str:
        """Mint a unique, stable rollback handle for an artifact."""
        return f"{name}@v{version}-{secrets.token_hex(4)}"

    def _available_tool_names(self) -> set[str] | None:
        """Return the set of valid tool names, or ``None`` when unavailable."""
        if self._tool_name_provider is not None:
            try:
                return {str(t) for t in self._tool_name_provider()}
            except Exception:
                logger.warning("tool_name_provider failed", exc_info=True)
                return None
        registry = getattr(self._agent, "tool_registry", None)
        if registry is None:
            return None
        try:
            return set(registry.list_tools().keys())
        except Exception:
            logger.warning("Could not enumerate tool registry", exc_info=True)
            return None

    def _failure_rates(self) -> dict[str, float]:
        """Return the historical failure-rate map (empty when unavailable)."""
        if self._failure_stats is None:
            return {}
        try:
            return dict(self._failure_stats())
        except Exception:
            logger.warning("failure_stats provider failed", exc_info=True)
            return {}

    def _skill_registry(self) -> Any:
        """Return the agent's skill registry, if any."""
        return getattr(self._agent, "skill_registry", None)

    def _get_bus(self) -> Any:
        """Return (creating if needed) the event backbone to publish on."""
        if self._bus is not None:
            return self._bus
        for attr in ("event_bus", "events_bus", "bus"):
            candidate = getattr(self._agent, attr, None)
            if candidate is not None and hasattr(candidate, "publish"):
                self._bus = candidate
                return candidate
        from encre.events.ledger import EventSourcedBus

        self._bus = EventSourcedBus(name="skills")
        return self._bus

    def _publish(
        self,
        kind: str,
        artifact: SkillArtifact,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Publish a lifecycle event for ``artifact`` on the event backbone."""
        bus = self._get_bus()
        if bus is None:
            return
        payload: dict[str, Any] = {
            "name": artifact.name,
            "version": artifact.version,
            "rollback_id": artifact.rollback_id,
            "status": artifact.status.value,
            "source": artifact.source,
            "run_id": artifact.run_id,
            "trigger": artifact.trigger,
            "tool_names": list(artifact.tool_names),
        }
        if extra:
            payload.update(extra)
        try:
            bus.publish(kind, payload=payload, refs=[artifact.rollback_id])
        except Exception:
            logger.warning("Failed to publish %s for %s", kind, artifact.name, exc_info=True)

    def _build_bundled_skill(self, artifact: SkillArtifact) -> Any:
        """Build a registered skill definition from an active artifact."""
        from encre.skills.types import BundledSkillDefinition, SkillContext, SkillSource

        body = artifact.body

        async def get_prompt(
            args: str | None = None,
            ctx: dict[str, Any] | None = None,
        ) -> str:
            resolved = body
            if args is not None:
                resolved = resolved.replace("{{args}}", args)
                resolved = resolved.replace("{{arguments}}", args)
                resolved = resolved.replace("{{user_input}}", args)
            if ctx is not None:
                for key, value in ctx.items():
                    resolved = resolved.replace(f"{{{{{key}}}}}", str(value))
            return resolved

        return BundledSkillDefinition(
            name=artifact.name,
            description=artifact.description,
            get_prompt_for_command=get_prompt,
            aliases=list(artifact.aliases),
            when_to_use=artifact.when_to_use,
            context=SkillContext.INLINE,
            source=SkillSource.BUNDLED,
            body=body,
        )

    def _register_skill(self, artifact: SkillArtifact) -> None:
        """Register an active artifact with the agent's skill registry."""
        registry = self._skill_registry()
        if registry is None:
            return
        # Replace a previous auto-generated version of the same name: the
        # registry refuses same-priority overwrites, so drop the old one first.
        from encre.skills.types import SkillSource

        existing = registry.lookup(artifact.name)
        if existing is not None and existing.source == SkillSource.BUNDLED:
            with contextlib.suppress(Exception):
                registry.unregister(artifact.name)
        registry.register(self._build_bundled_skill(artifact))

    def _unregister_skill(self, artifact: SkillArtifact) -> None:
        """Remove an artifact's skill from the registry, if registered."""
        registry = self._skill_registry()
        if registry is None:
            return
        with contextlib.suppress(Exception):
            registry.unregister(artifact.name)

    def _render_md(self, artifact: SkillArtifact, status: SkillStatus) -> str:
        """Render an artifact as a YAML-frontmatter markdown document."""
        lines = [
            "---",
            f"name: {artifact.name}",
            f"description: {_yaml_scalar(artifact.description)}",
            f"source: {_yaml_scalar(artifact.source)}",
            f"status: {status.value}",
            f"version: {artifact.version}",
            f"rollback_id: {_yaml_scalar(artifact.rollback_id)}",
        ]
        if artifact.trigger:
            lines.append(f"trigger: {_yaml_scalar(artifact.trigger)}")
        if artifact.run_id:
            lines.append(f"run_id: {_yaml_scalar(artifact.run_id)}")
        lines.append(f"created_at: {artifact.created_at}")
        if artifact.verification is not None:
            lines.append(f"verified_at: {artifact.verification.verified_at}")
            lines.append(f"verified: {str(artifact.verification.passed).lower()}")
        if artifact.aliases:
            lines.append(f"aliases: {json.dumps(artifact.aliases, ensure_ascii=False)}")
        if artifact.when_to_use:
            lines.append(f"when_to_use: {_yaml_scalar(artifact.when_to_use)}")
        lines.extend(["---", "", artifact.body])
        return "\n".join(lines)

    def _active_md_path(self, artifact: SkillArtifact) -> Path:
        """Return the markdown path for an active artifact."""
        return self._skills_dir / f"{artifact.name}.md"

    def _write_active_md(self, artifact: SkillArtifact) -> None:
        """Write the active markdown, replacing any disabled copy.

        The markdown carries the model-generated skill body, so it is stored
        encrypted at rest; the skill registry loader decrypts transparently
        when it re-registers the skill on the next startup.
        """
        try:
            self._skills_dir.mkdir(parents=True, exist_ok=True)
            disabled = self._skills_dir / f"{artifact.name}.md.disabled"
            if disabled.exists():
                with contextlib.suppress(OSError):
                    disabled.unlink()
            write_text(
                self._active_md_path(artifact),
                self._render_md(artifact, SkillStatus.ACTIVE),
            )
        except Exception:
            logger.warning("Failed to write active skill markdown for %s", artifact.name, exc_info=True)

    def _remove_active_md(self, artifact: SkillArtifact) -> None:
        """Rename an active markdown to ``.disabled`` so the loader ignores it."""
        path = self._active_md_path(artifact)
        if not path.is_file():
            return
        try:
            path.replace(path.with_suffix(".md.disabled"))
        except OSError:
            logger.warning("Failed to disable skill markdown %s", path, exc_info=True)
