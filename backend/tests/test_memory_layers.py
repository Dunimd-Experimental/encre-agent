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

"""Tests for the layered memory store and its consolidation rules.

The store under test is the real :class:`MemoryLayerStore`, backed by the real
entry-level :class:`MemoryConsolidator`, real encrypted
:mod:`encre.secure_io` persistence, the real append-only
:class:`EventSourcedBus`, and the real :class:`SignificanceJudge`.  No mock
store, consolidator or bus is substituted.

The suite pins down that all memory kinds round-trip losslessly through
serialisation, that a repeated pattern consolidates to exactly one long-term
memory, that unreferenced one-off candidates are evicted by capacity or TTL
while re-referenced ones are promoted, that ``attach(bus)`` intakes
``memory_candidate`` events idempotently and swallows malformed payloads, that
layer files are ciphertext at rest and reload identically, and that
below-threshold content never produces a memory at all.
"""

import json
import time

import pytest

from encre.events.ledger import EventSourcedBus
from encre.memdir.judgment import Experience, MemoryCandidate, SignificanceJudge
from encre.memdir.layers import (
    DecayState,
    LinkRelation,
    MemoryEntry,
    MemoryLayerStore,
    MemoryLink,
    MemoryType,
)
from encre.memdir.semantic import WorkingMemory
from encre.secure_io import is_encrypted

#: A one-off, task-irrelevant intermediate output (a directory listing).  The
#: judge must never turn it into a candidate.
_ONE_OFF_LISTING = "\n".join([
    "src/main.py",
    "src/utils.py",
    "src/config.json",
    "README.md",
    "package.json",
    "tests/test_a.py",
])

#: A user correction: high significance, which should become a candidate.
_USER_CORRECTION = "我们项目的端口固定用 8080，不要再用 3000 了"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Layer store rooted in a temp tree, with the data directory isolated."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    return MemoryLayerStore(root=tmp_path / "memory")


def _candidate(
    content: str,
    *,
    significance: float = 0.7,
    suggested_type: str = "fact",
    session_id: str = "sess-1",
    refs: list[str] | None = None,
) -> MemoryCandidate:
    """Build a judged candidate in the exact shape the judge publishes."""
    return MemoryCandidate(
        content=content,
        significance=significance,
        reasons=["repeated_pattern (w=0.75, s=1.00)"],
        source_refs=list(refs or []),
        suggested_type=suggested_type,
        session_id=session_id,
    )


# ----------------------------------------------------------------------
# 1. Memory kinds + lossless entry serialisation
# ----------------------------------------------------------------------


def test_memory_kinds_exist():
    """The enum carries exactly the node kinds defined by the spec."""
    assert {m.value for m in MemoryType} == {
        "working", "fact", "preference", "decision", "lesson", "entity", "episode",
    }


def test_all_layers_write_and_read_back(store):
    """Each long-term kind persists and reloads through a single schema."""
    written: dict[str, MemoryEntry] = {}
    for memory_type in (
        MemoryType.FACT,
        MemoryType.PREFERENCE,
        MemoryType.DECISION,
        MemoryType.LESSON,
        MemoryType.ENTITY,
        MemoryType.EPISODE,
    ):
        entry = MemoryEntry(
            content=f"durable memory of kind {memory_type.value}",
            type=memory_type.value,
            importance=0.6,
            source="test",
        )
        store.store(entry)
        written[memory_type.value] = entry

    working = WorkingMemory()
    working.set_goal("ship the unified memory layers")
    working.add_finding("working memory is reused, not redefined")
    store.save_working(working)

    store.reload()

    for memory_type in (
        MemoryType.FACT,
        MemoryType.PREFERENCE,
        MemoryType.DECISION,
        MemoryType.LESSON,
        MemoryType.ENTITY,
        MemoryType.EPISODE,
    ):
        entries = store.by_type(memory_type)
        assert [e.id for e in entries] == [written[memory_type.value].id]
        assert entries[0].content == written[memory_type.value].content
        assert entries[0].memory_type is memory_type

    assert len(store.long_term()) == 6
    restored_working = store.load_working()
    assert restored_working.current_goal == "ship the unified memory layers"
    assert "reused, not redefined" in " ".join(restored_working.findings)


def test_memory_entry_serialisation_round_trip():
    """MemoryEntry survives a JSON round trip with every field intact."""
    entry = MemoryEntry(
        content="the deploy pipeline is reproducible",
        type=MemoryType.LESSON.value,
        importance=0.83,
        created_at=1700000000.5,
        links=[
            MemoryLink(
                target_id="abc123",
                relation=LinkRelation.SUPPORTS.value,
                weight=0.75,
                created_at=1700000001.0,
            ),
        ],
        decay=DecayState(
            last_access=1700000002.0, access_count=3, archived=True, decay_score=0.42,
        ),
        version=4,
        source="significance_judgment",
        source_refs=["evt-1", "evt-2"],
        metadata={"session_id": "sess-rt", "nested": {"a": 1}},
    )

    restored = MemoryEntry.from_dict(json.loads(json.dumps(entry.to_dict())))

    assert restored == entry
    assert restored.to_dict() == entry.to_dict()
    assert restored.links[0].target_id == "abc123"
    assert restored.decay.archived is True


# ----------------------------------------------------------------------
# 2. Repeated pattern -> exactly one long-term memory
# ----------------------------------------------------------------------


def test_repeated_candidate_consolidates_to_single_long_term(store):
    """A pattern seen twice yields one merged long-term memory, not two."""
    text = "deploy always uses the blue environment"
    first = store.intake(_candidate(text, suggested_type="lesson"))
    original_importance = first.importance

    # First sighting is buffered only; no long-term memory exists yet.
    assert [e.id for e in store.buffer()] == [first.id]
    assert store.long_term() == []

    second = store.intake(_candidate(text, suggested_type="lesson"))
    assert second.id == first.id

    long_term = store.long_term()
    assert len(long_term) == 1
    assert long_term[0].id == first.id
    assert store.buffer() == []
    # The repeat raised importance and accumulated the reference count.
    assert long_term[0].decay.access_count == 2
    assert long_term[0].importance > original_importance

    # A third sighting merges into that same long-term memory.
    third = store.intake(_candidate(text, suggested_type="lesson"))
    assert third.id == first.id
    assert len(store.long_term()) == 1
    assert store.long_term()[0].decay.access_count == 3
    assert store.long_term()[0].version >= 3

    # Still one logical record after a reload: no duplicate revision survives.
    store.reload()
    assert len(store.long_term()) == 1
    assert store.long_term()[0].content == text


def test_high_similarity_candidate_merges_instead_of_duplicating(store):
    """Near-identical content is deduplicated by the consolidator's similarity."""
    base = "the authentication service issues jwt tokens per request"
    variant = "the authentication service issues jwt tokens per request now"

    kept = store.intake(_candidate(base))
    merged = store.intake(_candidate(variant))

    assert merged.id == kept.id
    assert len(store.long_term()) == 1
    assert store.long_term()[0].content == base


# ----------------------------------------------------------------------
# 3. One-off, low-value candidate is evicted
# ----------------------------------------------------------------------


def test_one_off_candidates_are_evicted_by_capacity(tmp_path, monkeypatch):
    """Unreferenced candidates overflow the buffer capacity and are dropped."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    store = MemoryLayerStore(root=tmp_path / "memory", buffer_capacity=2)

    for marker in ("alpha", "beta", "gamma"):
        store.intake(_candidate(
            f"transient observation {marker} marker", significance=0.55,
        ))
    assert len(store.buffer()) == 3

    summary = store.consolidate()

    assert summary["promoted"] == []
    assert summary["long_term"] == 0
    assert len(summary["evicted"]) == 1
    assert len(store.buffer()) == 2
    assert store.long_term() == []

    store.reload()
    assert store.long_term() == []
    assert len(store.buffer()) == 2


def test_one_off_candidate_expires_by_ttl(tmp_path, monkeypatch):
    """A candidate older than the buffer TTL is evicted on consolidation."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    store = MemoryLayerStore(root=tmp_path / "memory", buffer_ttl=0.0)

    store.intake(_candidate("a transient observation that never repeats"))
    summary = store.consolidate(now=time.time() + 10.0)

    assert summary["promoted"] == []
    assert len(summary["evicted"]) == 1
    assert store.buffer() == []
    assert store.long_term() == []


# ----------------------------------------------------------------------
# 4. Re-referenced candidate is promoted
# ----------------------------------------------------------------------


def test_retrieval_hit_promotes_buffered_candidate(store):
    """A buffered candidate hit by retrieval is promoted to long-term memory."""
    entry = store.intake(_candidate("the cache layer uses redis"))
    assert store.long_term() == []

    accessed = store.record_access(entry.id)

    assert accessed is not None
    assert accessed.decay.access_count == 2
    assert store.buffer() == []
    assert [e.id for e in store.long_term()] == [entry.id]
    assert store.get(entry.id) is accessed


def test_link_reference_promotes_buffered_candidate(store):
    """A candidate referenced by an explicit link is promoted to long-term."""
    source = store.store(MemoryEntry(
        content="project goal: keep deployments reproducible",
        type=MemoryType.EPISODE.value,
        importance=0.9,
    ))
    target = store.intake(_candidate("the release checklist has seven steps"))

    linked = store.link(source.id, target.id, LinkRelation.CO_OCCURS)

    assert linked is source
    assert source.links[0].target_id == target.id
    assert source.links[0].relation == "co_occurs"
    assert store.buffer() == []
    assert store.get(target.id) is not None


# ----------------------------------------------------------------------
# 5. attach(bus): auto intake, idempotent, failure-isolated
# ----------------------------------------------------------------------


def test_attach_intakes_candidates_and_is_idempotent(store):
    """One subscription per bus: a candidate is intaken exactly once."""
    bus = EventSourcedBus("mem-layers-attach")
    store.attach(bus)
    store.attach(bus)  # a second attach must be a no-op

    candidate = _candidate("our project always uses port 8080", refs=["msg-7"])
    bus.publish(
        "memory_candidate",
        session_id="sess-1",
        payload=candidate.to_dict(),
        refs=candidate.source_refs,
    )

    assert len(store.buffer()) == 1

    bus.publish(
        "memory_candidate",
        session_id="sess-1",
        payload=candidate.to_dict(),
        refs=candidate.source_refs,
    )

    long_term = store.long_term()
    assert len(long_term) == 1
    assert store.buffer() == []
    # access_count would reach 4 with a duplicated subscription; one gives 2.
    assert long_term[0].decay.access_count == 2


def test_attach_survives_malformed_and_empty_events(store):
    """A malformed payload never raises into the backbone and never writes."""
    bus = EventSourcedBus("mem-layers-bad-payload")
    store.attach(bus)

    bus.publish("memory_candidate", session_id="s", payload={"not_a_candidate": True})
    bus.publish("memory_candidate", session_id="s", payload={})
    bus.publish("memory_candidate", session_id="s", payload={"content": "   "})

    assert store.buffer() == []
    assert store.long_term() == []
    assert bus.count == 3


# ----------------------------------------------------------------------
# 6. Encrypted at rest + reload consistency
# ----------------------------------------------------------------------


def test_layers_are_encrypted_at_rest_and_survive_reload(store):
    """Every layer file is ciphertext on disk and reloads to the same content."""
    text = "the staging database is reset every night"
    store.intake(_candidate(text))
    store.intake(_candidate(text))  # the repeat promotes it -> long-term file written

    working = WorkingMemory()
    working.set_goal("verify encryption at rest")
    store.save_working(working)

    store.intake(_candidate("an unrelated buffered observation"))
    assert store.buffer()  # the buffer file is written too

    long_term_raw = (store.layers_dir / "fact.jsonl").read_text(encoding="utf-8")
    long_term_lines = [line for line in long_term_raw.splitlines() if line.strip()]
    assert long_term_lines
    assert all(is_encrypted(line) for line in long_term_lines)
    assert text not in long_term_raw

    working_raw = (store.layers_dir / "working.json").read_text(encoding="utf-8")
    assert is_encrypted(working_raw)
    assert "verify encryption at rest" not in working_raw

    buffer_raw = (store.layers_dir / "_short_term.json").read_text(encoding="utf-8")
    assert is_encrypted(buffer_raw)

    store.reload()

    assert [e.content for e in store.long_term()] == [text]
    assert store.load_working().current_goal == "verify encryption at rest"
    assert [e.content for e in store.buffer()] == ["an unrelated buffered observation"]


# ----------------------------------------------------------------------
# 7. Below-threshold content produces no memory at all
# ----------------------------------------------------------------------


def test_low_significance_content_produces_no_memory(store):
    """A one-off listing never becomes a candidate, so nothing is written."""
    bus = EventSourcedBus("mem-layers-low-sig")
    judge = SignificanceJudge()
    judge.attach(bus)
    store.attach(bus)

    # Direct path: the judge refuses the one-off output outright.
    assert judge.judge(Experience(
        kind="tool_result",
        content=_ONE_OFF_LISTING,
        session_id="sess-1",
        metadata={"tool_name": "file_list", "is_error": False},
    )) is None

    bus.publish(
        "tool_result",
        session_id="sess-1",
        payload={"content": _ONE_OFF_LISTING, "tool_name": "file_list"},
    )

    assert store.buffer() == []
    assert store.long_term() == []

    # Control: a genuine correction does flow through the same pipeline.
    bus.publish(
        "user_message",
        session_id="sess-1",
        payload={"content": _USER_CORRECTION},
    )
    assert len(store.buffer()) == 1
    assert store.long_term() == []
