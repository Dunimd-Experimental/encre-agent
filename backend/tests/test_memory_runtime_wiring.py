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
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

from __future__ import annotations

"""Runtime wiring of the layered memory subsystem (Tasks 4.5 / 7.4).

These tests exercise the **real** wiring built by
:class:`~encre.loop.wiring._LoopWiringMixin`: the real
:class:`~encre.events.ledger.EventSourcedBus` backbone, the real
:class:`~encre.memdir.layers.MemoryLayerStore` (encrypted
:mod:`encre.secure_io` file I/O), the real
:class:`~encre.memdir.recall.RecallEngine`, and the real
:class:`~encre.improve.verification.SkillVerifier`.  No mock store, bus or
recall engine is used.

Covered invariants:

1. a ``memory_candidate`` published on the loop's backbone is intaken by
   ``loop.memory_layers`` and is encrypted at rest;
2. the same memory is retrievable through ``loop.recall_engine.recall``;
3. re-wiring never subscribes the same backbone twice (single intake);
4. a promoted skill becomes one ``procedural`` memory entry carrying the
   skill name, description, version and rollback id;
5. a rejected skill is never written to procedural memory;
6. re-promoting the same skill does not create a duplicate entry.

The data root is redirected to ``tmp_path`` via ``ENCRE_DATA_DIR`` so the real
user data directory is never touched.
"""

from unittest.mock import MagicMock, patch

import pytest

from encre.config import EncreConfig
from encre.improve.learning.engine import LearningEngine
from encre.improve.verification import SkillStatus, SkillVerifier
from encre.loop import EncreLoop
from encre.memdir.judgment import MemoryCandidate
from encre.memdir.layers import MemoryLayerStore, MemoryType
from encre.secure_io import is_encrypted
from encre.session import EncreSession
from encre.skills.registry import EncreSkillRegistry
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


class _FakeAgent:
    """Minimal agent exposing the loop plus the registries the verifier reads."""

    def __init__(self, loop: EncreLoop, tool_names: tuple[str, ...]) -> None:
        self.loop = loop
        self.tool_registry = ToolRegistry()
        for name in tool_names:
            self.tool_registry.register(_make_tool(name))
        self.skill_registry = EncreSkillRegistry()


@pytest.fixture
def loop(tmp_path, monkeypatch) -> EncreLoop:
    """A real loop whose data root is isolated to ``tmp_path``."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    config = EncreConfig(
        model="gpt-5.6",
        backend_type="openai",
        permission_mode="default",
        max_turns=10,
        max_tokens=4096,
        log_level="ERROR",
        enable_prompt_caching=False,
    )
    session = EncreSession(config)
    with patch("encre.loop.create_backend") as mock_create_backend:
        mock_backend = MagicMock()
        mock_backend.supports_tool_calling.return_value = True
        mock_backend.supports_prompt_caching.return_value = False
        mock_backend.context_window_size.return_value = 128000
        mock_create_backend.return_value = mock_backend
        return EncreLoop(config=config, session=session)


def _candidate(content: str) -> MemoryCandidate:
    """Build a judged candidate (the shape the significance judge publishes)."""
    return MemoryCandidate(
        content=content,
        significance=0.7,
        reasons=["user_correction (w=0.85, s=1.00)"],
        source_refs=["msg-1"],
        suggested_type="semantic",
        session_id="sess-wiring",
    )


def _skill_def(
    name: str = "auto-mem-bridge-11111111",
    tools: tuple[str, ...] = ("file_read",),
    *,
    description: str = "Automatically generated skill for reading files.",
) -> dict:
    """Build a well-formed skill definition like SkillGenerator produces."""
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
        "tool_names": list(tools),
        "reason": "Repeated tool-use pattern detected",
    }


def _bound_verifier(loop: EncreLoop, tools: tuple[str, ...] = ("file_read", "glob")) -> SkillVerifier:
    """A real verifier bound to ``loop`` through the production engine path."""
    engine = LearningEngine(_FakeAgent(loop, tools))
    return engine._get_verifier()


# ----------------------------------------------------------------------
# 4.5 -- candidates land in the store, are encrypted, and are recallable
# ----------------------------------------------------------------------


def test_memory_candidate_becomes_persistent_and_recallable(loop, tmp_path):
    """A backbone candidate really lands in memory_layers and is recallable."""
    assert isinstance(loop.memory_layers, MemoryLayerStore)
    assert loop.recall_engine is not None
    # The store and recall engine are reachable from the session too.
    assert loop.session.memory_layers is loop.memory_layers
    assert loop.session.recall_engine is loop.recall_engine
    # The data root is the isolated temp tree, never the real user data dir.
    assert loop.memory_layers.root == tmp_path / "data" / "memory"

    candidate = _candidate("our project always uses port 8080 for the gateway")
    loop.event_bus.publish(
        "memory_candidate",
        session_id=candidate.session_id,
        payload=candidate.to_dict(),
        refs=candidate.source_refs,
    )

    visible = loop.memory_layers.long_term() + loop.memory_layers.buffer()
    assert [entry.content for entry in visible] == [candidate.content]

    # Encrypted at rest: ciphertext on disk, plaintext never visible.
    raw = (loop.memory_layers.layers_dir / "_short_term.json").read_text(encoding="utf-8")
    assert is_encrypted(raw)
    assert candidate.content not in raw

    # Associative recall finds it (and records the access, promoting it).
    hits = loop.recall_engine.recall("gateway port 8080")
    assert [hit.entry.content for hit in hits] == [candidate.content]
    assert [entry.content for entry in loop.memory_layers.long_term()] == [candidate.content]


def test_memory_wiring_is_idempotent(loop):
    """Re-wiring does not subscribe the same backbone twice (single intake)."""
    store = loop.memory_layers
    loop._wire_memory_layers()  # must be a no-op
    assert loop.memory_layers is store

    candidate = _candidate("the deploy pipeline always runs the blue environment")
    loop.event_bus.publish(
        "memory_candidate",
        session_id="sess-wiring",
        payload=candidate.to_dict(),
    )
    assert len(store.buffer()) == 1
    assert len(store.long_term()) == 0


# ----------------------------------------------------------------------
# 7.4 -- promoted skills become procedural memory
# ----------------------------------------------------------------------


def test_promoted_skill_is_written_to_procedural_memory(loop):
    """A verified, promoted skill lands in procedural memory with provenance."""
    verifier = _bound_verifier(loop)
    artifact = verifier.propose(_skill_def())

    result = verifier.verify(artifact)
    assert result.passed is True
    assert artifact.status is SkillStatus.ACTIVE

    entries = loop.memory_layers.by_type(MemoryType.PROCEDURAL)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.metadata["skill_name"] == artifact.name
    assert artifact.description in entry.content
    assert entry.source == f"skill:{artifact.name}@v{artifact.version}"
    assert entry.source_refs == [artifact.rollback_id]
    assert entry.version == artifact.version

    # The procedural layer file is ciphertext at rest.
    raw = (loop.memory_layers.layers_dir / "procedural.jsonl").read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert lines
    assert all(is_encrypted(line) for line in lines)
    assert artifact.name not in raw


def test_rejected_skill_is_not_written_to_procedural_memory(loop):
    """A skill that fails verification never reaches procedural memory."""
    verifier = _bound_verifier(loop)
    artifact = verifier.propose(
        _skill_def(name="auto-mem-reject-22222222", tools=("file_read", "ghost_tool")),
    )

    result = verifier.verify(artifact)
    assert result.passed is False
    assert artifact.status is SkillStatus.REJECTED
    assert loop.memory_layers.by_type(MemoryType.PROCEDURAL) == []


def test_repeated_promotion_does_not_duplicate_procedural_memory(loop):
    """Re-promoting the same skill keeps exactly one procedural entry."""
    verifier = _bound_verifier(loop)
    artifact = verifier.propose(_skill_def(name="auto-mem-dup-33333333"))
    verifier.verify(artifact)
    assert len(loop.memory_layers.by_type(MemoryType.PROCEDURAL)) == 1

    # Promoting the very same artifact again must not mint a second memory.
    assert verifier.promote(artifact) is True
    assert len(loop.memory_layers.by_type(MemoryType.PROCEDURAL)) == 1

    # A second, independent binding is idempotent as well.
    loop.attach_skill_verifier(verifier)
    assert verifier.promote(artifact) is True
    assert len(loop.memory_layers.by_type(MemoryType.PROCEDURAL)) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-header"]))
