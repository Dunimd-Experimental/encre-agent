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

from __future__ import annotations

"""Tests for encre.improve.verification -- the skill verification gate.

These tests lock in the core guarantee of the verification gate: a generated
skill is *proposed* first and is never injected into the main prompt until it
passes verification.  A failing artifact is rejected (or stays proposed) with
recorded reasons and does not change regular behaviour.
"""

import time

import pytest

from encre.events.ledger import EventSourcedBus
from encre.improve.learning.engine import LearningEngine
from encre.improve.learning.skill_generator import SkillGenerator
from encre.improve.verification import (
    SkillArtifact,
    SkillStatus,
    SkillVerifier,
)
from encre.loop_skills import SkillManager
from encre.skills.registry import EncreSkillRegistry
from encre.skills.types import BundledSkillDefinition, SkillSource
from encre.tools.base import build_tool
from encre.tools.registry import ToolRegistry


async def _noop(**kwargs) -> str:
    """Minimal async tool body used to satisfy the tool registry contract."""
    return ""


def _make_tool(name: str):
    """Build a real tool object for the given name."""
    return build_tool(
        name=name,
        description=f"{name} tool",
        input_schema={"type": "object", "properties": {}},
        execute=_noop,
    )


class FakeAgent:
    """Minimal agent exposing the two registries the verifier consults."""

    def __init__(self, tool_names: tuple[str, ...] = ()) -> None:
        self.tool_registry = ToolRegistry()
        for name in tool_names:
            self.tool_registry.register(_make_tool(name))
        self.skill_registry = EncreSkillRegistry()


def _skill_def(
    name: str = "auto-file-read-abc12345",
    tools: tuple[str, ...] = ("file_read",),
    *,
    description: str = "Automatically generated skill for reading files.",
    body: str | None = None,
) -> dict:
    """Build a well-formed skill definition like SkillGenerator produces."""
    if body is None:
        tool_lines = "\n".join(f"- `{t}` (used 3 times)" for t in tools)
        body = (
            f"# {name}\n\n{description}\n\n## When to Use\n\nsource files\n\n"
            f"## Tools\n\n{tool_lines}\n\n## Steps\n\n- Do the thing\n"
        )
    return {
        "name": name,
        "description": description,
        "aliases": [name],
        "source": "auto_generated",
        "when_to_use": "source files, logs, configuration",
        "body": body,
        "generated_at": time.time(),
        "tool_names": list(tools),
        "reason": "Repeated tool-use pattern detected",
    }


def _catalogue(agent: FakeAgent) -> str:
    """Render the skill catalogue exactly as the main prompt builder would."""
    return SkillManager(agent.skill_registry).render_skill_catalogue()


class TestProposalGate:
    """A proposed artifact must never be injected into the main prompt."""

    def test_verify_proposed_artifact_is_not_registered(self, tmp_path):
        agent = FakeAgent(("file_read",))
        verifier = SkillVerifier(agent, skills_dir=tmp_path)

        artifact = verifier.propose(_skill_def())

        assert artifact.status == SkillStatus.PROPOSED
        assert agent.skill_registry.lookup(artifact.name) is None
        assert artifact.name not in {s.name for s in agent.skill_registry.list_all()}
        assert artifact.name not in _catalogue(agent)
        assert verifier.pending() == [artifact]

    async def test_generator_register_writes_to_proposed_not_registry(self, tmp_path):
        agent = FakeAgent(("file_read", "glob"))
        verifier = SkillVerifier(agent, skills_dir=tmp_path)
        generator = SkillGenerator(agent, verifier=verifier)

        skill_def = generator.generate(["file_read", "glob"], "read the config")
        artifact = await generator.register(skill_def, run_id="run-42")

        assert artifact is not None
        assert artifact.status == SkillStatus.PROPOSED
        assert agent.skill_registry.lookup(artifact.name) is None
        assert artifact.name not in _catalogue(agent)


class TestVerification:
    """Verification decides promotion vs rejection using quantifiable evidence."""

    def test_verify_good_skill_promotes_and_is_injected(self, tmp_path):
        agent = FakeAgent(("file_read", "glob"))
        verifier = SkillVerifier(agent, skills_dir=tmp_path)
        artifact = verifier.propose(_skill_def(tools=("file_read", "glob")))

        result = verifier.verify(artifact)

        assert result.passed is True
        assert artifact.status == SkillStatus.ACTIVE
        assert agent.skill_registry.lookup(artifact.name) is not None
        assert artifact.name in _catalogue(agent)
        assert verifier.pending() == []
        assert verifier.active() == [artifact]
        # The active markdown is written with status: active so it survives a restart.
        md = (tmp_path / f"{artifact.name}.md").read_text(encoding="utf-8")
        assert "status: active" in md

    def test_verify_skill_referencing_unknown_tool_is_rejected(self, tmp_path):
        agent = FakeAgent(("file_read",))
        verifier = SkillVerifier(agent, skills_dir=tmp_path)
        artifact = verifier.propose(_skill_def(tools=("file_read", "ghost_tool")))

        result = verifier.verify(artifact)

        assert result.passed is False
        assert artifact.status == SkillStatus.REJECTED
        assert any("ghost_tool" in reason for reason in result.record.reasons)
        # Bad skill is neither registered nor injected, and remains auditable.
        assert agent.skill_registry.lookup(artifact.name) is None
        assert artifact.name not in _catalogue(agent)
        assert artifact in verifier.all_artifacts()

    def test_verify_duplicate_of_active_tool_signature_is_rejected(self, tmp_path):
        agent = FakeAgent(("file_read", "glob"))
        verifier = SkillVerifier(agent, skills_dir=tmp_path)
        first = verifier.propose(_skill_def(name="auto-a-11111111", tools=("file_read", "glob")))
        verifier.verify(first)
        assert first.status == SkillStatus.ACTIVE

        second = verifier.propose(_skill_def(name="auto-b-22222222", tools=("file_read", "glob")))
        result = verifier.verify(second)

        assert result.passed is False
        assert second.status == SkillStatus.REJECTED
        assert any("duplicates active skill" in r for r in result.record.reasons)
        assert agent.skill_registry.lookup(second.name) is None

    def test_verify_name_colliding_with_non_auto_skill_is_rejected(self, tmp_path):
        agent = FakeAgent(("file_read",))

        async def _prompt(args=None, ctx=None):
            return "body"

        agent.skill_registry.register(
            BundledSkillDefinition(
                name="auto-file-read-abc12345",
                description="A project-owned skill with the same name.",
                get_prompt_for_command=_prompt,
                source=SkillSource.PROJECT,
            )
        )
        verifier = SkillVerifier(agent, skills_dir=tmp_path)
        artifact = verifier.propose(_skill_def(name="auto-file-read-abc12345"))

        result = verifier.verify(artifact)

        assert result.passed is False
        assert artifact.status == SkillStatus.REJECTED
        assert any("collides" in r for r in result.record.reasons)

    def test_verify_history_check_rejects_high_failure_tool(self, tmp_path):
        agent = FakeAgent(("file_read",))
        verifier = SkillVerifier(
            agent,
            skills_dir=tmp_path,
            failure_stats=lambda: {"file_read": 0.9},
        )
        artifact = verifier.propose(_skill_def(tools=("file_read",)))

        result = verifier.verify(artifact)

        assert result.passed is False
        assert any("failure rate" in r for r in result.record.reasons)

    def test_artifact_carries_version_source_and_rollback_id(self, tmp_path):
        agent = FakeAgent(("file_read",))
        verifier = SkillVerifier(agent, skills_dir=tmp_path)

        v1 = verifier.propose(_skill_def(), run_id="run-1", trigger="repeated_tool_pattern")
        assert v1.version == 1
        assert v1.source == "auto_generated"
        assert v1.run_id == "run-1"
        assert v1.trigger == "repeated_tool_pattern"
        assert v1.rollback_id
        assert v1.created_at > 0

        v2 = verifier.propose(
            _skill_def(body="# auto-file-read-abc12345\n\nDifferent body.\n"),
            run_id="run-2",
        )
        assert v2.version == 2
        assert v2.rollback_id != v1.rollback_id

    def test_identical_proposal_is_deduplicated(self, tmp_path):
        agent = FakeAgent(("file_read",))
        verifier = SkillVerifier(agent, skills_dir=tmp_path)

        first = verifier.propose(_skill_def())
        second = verifier.propose(_skill_def())

        assert first is second
        assert len(verifier.all_artifacts()) == 1

    def test_verification_record_is_persisted_with_reasons(self, tmp_path):
        agent = FakeAgent(("file_read",))
        verifier = SkillVerifier(agent, skills_dir=tmp_path)
        artifact = verifier.propose(_skill_def(tools=("ghost_tool",)))
        verifier.verify(artifact)

        reloaded = SkillVerifier(agent, skills_dir=tmp_path).get(artifact.name)
        assert reloaded is not None
        assert reloaded.status == SkillStatus.REJECTED
        assert reloaded.verification is not None
        assert reloaded.verification.passed is False
        assert reloaded.verification.reasons


class TestRollback:
    """One-click disable/rollback must take an active skill out of service."""

    def test_disable_by_rollback_id_removes_from_prompt(self, tmp_path):
        agent = FakeAgent(("file_read",))
        verifier = SkillVerifier(agent, skills_dir=tmp_path)
        artifact = verifier.propose(_skill_def())
        verifier.verify(artifact)
        assert agent.skill_registry.lookup(artifact.name) is not None

        assert verifier.disable(artifact.rollback_id) is True

        assert artifact.status == SkillStatus.DISABLED
        assert agent.skill_registry.lookup(artifact.name) is None
        assert artifact.name not in _catalogue(agent)
        assert (tmp_path / f"{artifact.name}.md").exists() is False
        assert (tmp_path / f"{artifact.name}.md.disabled").exists() is True

    def test_disable_by_name_and_unknown_key(self, tmp_path):
        agent = FakeAgent(("file_read",))
        verifier = SkillVerifier(agent, skills_dir=tmp_path)
        artifact = verifier.propose(_skill_def())
        verifier.verify(artifact)

        assert verifier.disable(artifact.name) is True
        assert verifier.disable("does-not-exist") is False
        assert verifier.disable(artifact.name) is False  # already disabled

    def test_rollback_alias_disables(self, tmp_path):
        agent = FakeAgent(("file_read",))
        verifier = SkillVerifier(agent, skills_dir=tmp_path)
        artifact = verifier.propose(_skill_def())
        verifier.verify(artifact)

        assert verifier.rollback(artifact.name) is True
        assert artifact.status == SkillStatus.DISABLED


class TestEvents:
    """State changes are published on the event backbone."""

    def test_promotion_publishes_proposed_verified_promoted(self, tmp_path):
        agent = FakeAgent(("file_read",))
        bus = EventSourcedBus(name="test-skills")
        seen: list[tuple[str, dict]] = []
        bus.subscribe("*", lambda e: seen.append((e.kind, e.payload)))
        verifier = SkillVerifier(agent, bus=bus, skills_dir=tmp_path)

        artifact = verifier.propose(_skill_def())
        verifier.verify(artifact)

        kinds = [k for k, _ in seen]
        assert "skill_proposed" in kinds
        assert "skill_verified" in kinds
        assert "skill_promoted" in kinds
        promoted = next(p for k, p in seen if k == "skill_promoted")
        assert promoted["name"] == artifact.name
        assert promoted["rollback_id"] == artifact.rollback_id
        assert promoted["version"] == 1
        assert promoted["status"] == "active"

    def test_rejection_publishes_verified_and_rejected(self, tmp_path):
        agent = FakeAgent(("file_read",))
        bus = EventSourcedBus(name="test-skills")
        seen: list[tuple[str, dict]] = []
        bus.subscribe("*", lambda e: seen.append((e.kind, e.payload)))
        verifier = SkillVerifier(agent, bus=bus, skills_dir=tmp_path)

        artifact = verifier.propose(_skill_def(tools=("ghost_tool",)))
        verifier.verify(artifact)

        kinds = [k for k, _ in seen]
        assert "skill_rejected" in kinds
        verified = next(p for k, p in seen if k == "skill_verified")
        assert verified["passed"] is False
        rejected = next(p for k, p in seen if k == "skill_rejected")
        assert rejected["status"] == "rejected"
        assert rejected["reasons"]

    def test_disable_publishes_disabled_event(self, tmp_path):
        agent = FakeAgent(("file_read",))
        bus = EventSourcedBus(name="test-skills")
        seen: list[str] = []
        bus.subscribe("skill_disabled", lambda e: seen.append(e.payload["name"]))
        verifier = SkillVerifier(agent, bus=bus, skills_dir=tmp_path)
        artifact = verifier.propose(_skill_def())
        verifier.verify(artifact)

        verifier.disable(artifact.name)

        assert seen == [artifact.name]


LEGACY_MD = """---
name: auto-bash-deadbeef
description: Automatically generated skill for task involving bash.
source: auto_generated
aliases: auto-bash-deadbeef
when_to_use: source files, logs, configuration
---

# auto-bash-deadbeef

Legacy skill written before the verification gate existed.

## Tools

- `bash` (used 4 times)
- `file_read` (used 2 times)

## Steps

- Execute command using `bash` (4 times)
"""


class TestLegacyMigration:
    """Pre-verification auto_generated skills are marked proposed."""

    def test_migrate_legacy_marks_file_proposed_and_unregisters(self, tmp_path):
        agent = FakeAgent(("bash", "file_read"))
        legacy = tmp_path / "auto-bash-deadbeef.md"
        legacy.write_text(LEGACY_MD, encoding="utf-8")

        # Simulate the pre-fix world: the legacy file was loaded as active.
        agent.skill_registry.load_from_dir(str(tmp_path), source=SkillSource.USER)
        assert agent.skill_registry.lookup("auto-bash-deadbeef") is not None

        verifier = SkillVerifier(agent, skills_dir=tmp_path)
        migrated = verifier.migrate_legacy()

        assert migrated == 1
        content = legacy.read_text(encoding="utf-8")
        assert "status: proposed" in content
        # No longer active and no longer injected.
        assert agent.skill_registry.lookup("auto-bash-deadbeef") is None
        assert "auto-bash-deadbeef" not in _catalogue(agent)
        # Recorded as a pending artifact with tools recovered from the body.
        pending = verifier.pending()
        assert len(pending) == 1
        assert set(pending[0].tool_names) == {"bash", "file_read"}
        assert pending[0].source == "auto_generated:migrated"

        # A fresh registry skips the proposed file entirely.
        fresh = EncreSkillRegistry()
        fresh.load_from_dir(str(tmp_path), source=SkillSource.USER)
        assert "auto-bash-deadbeef" not in {s.name for s in fresh.list_all()}

    def test_migrate_legacy_is_idempotent(self, tmp_path):
        agent = FakeAgent(("bash", "file_read"))
        (tmp_path / "auto-bash-deadbeef.md").write_text(LEGACY_MD, encoding="utf-8")
        verifier = SkillVerifier(agent, skills_dir=tmp_path)

        assert verifier.migrate_legacy() == 1
        assert verifier.migrate_legacy() == 0

    def test_migrate_absent_directory_returns_zero(self, tmp_path):
        agent = FakeAgent()
        verifier = SkillVerifier(agent, skills_dir=tmp_path / "does-not-exist")

        assert verifier.migrate_legacy() == 0


class TestEngineIntegration:
    """The learning engine proposes then verifies a crystallised skill."""

    async def test_engine_promotes_verified_skill(self, tmp_path):
        tools = ("file_read", "glob", "grep", "bash", "file_write")
        agent = FakeAgent(tools)
        verifier = SkillVerifier(agent, skills_dir=tmp_path)
        engine = LearningEngine(agent, verifier=verifier)
        await engine.start()

        await engine._crystallize(list(tools), "do a task", "run-1")

        active = verifier.active()
        assert len(active) == 1
        assert agent.skill_registry.lookup(active[0].name) is not None
        assert active[0].run_id == "run-1"

    async def test_engine_rejects_skill_with_unknown_tool(self, tmp_path):
        tools = ("file_read", "glob", "grep", "bash", "ghost_tool")
        agent = FakeAgent(("file_read", "glob", "grep", "bash"))
        verifier = SkillVerifier(agent, skills_dir=tmp_path)
        engine = LearningEngine(agent, verifier=verifier)
        await engine.start()

        await engine._crystallize(list(tools), "do a task", "run-2")

        assert verifier.active() == []
        rejected = verifier.rejected()
        assert len(rejected) == 1
        assert rejected[0].verification is not None
        assert rejected[0].verification.reasons
        assert agent.skill_registry.lookup(rejected[0].name) is None
        assert rejected[0].name not in _catalogue(agent)


def test_verification_result_and_record_serialisation(tmp_path):
    """Verification records round-trip through the durable proposed store."""
    agent = FakeAgent(("file_read",))
    verifier = SkillVerifier(agent, skills_dir=tmp_path)
    artifact = verifier.propose(_skill_def())
    verifier.verify(artifact)

    payload = artifact.to_dict()
    rebuilt = SkillArtifact.from_dict(payload)

    assert rebuilt.status == SkillStatus.ACTIVE
    assert rebuilt.version == artifact.version
    assert rebuilt.rollback_id == artifact.rollback_id
    assert rebuilt.verification is not None
    assert rebuilt.verification.passed is True
    assert rebuilt.to_skill_def()["tool_names"] == artifact.tool_names


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-header"]))
