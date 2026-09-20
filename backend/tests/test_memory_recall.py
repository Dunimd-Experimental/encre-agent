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

"""Tests for associative recall and forgetting (Task 5).

The engine under test is the real :class:`RecallEngine` on the real
:class:`MemoryLayerStore`: the real local :class:`SemanticMemorySearch`, the
real encrypted :mod:`encre.secure_io` persistence, and -- when the compiled
extension is present -- the real native embedding backend.  No mock store,
searcher or embedder is used.

Covered invariants:

1. spreading activation recalls linked memories (causal / dependency /
   same_goal / reference) and the result is traceable back to its seed;
2. hop and activation boundaries hold, and a linkless store degrades to pure
   similarity search;
3. freshness + importance decay drives ``decay_score`` down and archives a
   stale, low-importance memory;
4. archived memories are invisible to default recall but readable through the
   audit surface, and their records are **never physically deleted**;
5. local retrieval is the default and remains available when native is
   missing or unconfigured (offline usable);
6. a recall hit records an access, which promotes a buffered candidate.
"""

import time

import pytest

import encre.native as native_backend
from encre.memdir.judgment import MemoryCandidate
from encre.memdir.layers import (
    LinkRelation,
    MemoryEntry,
    MemoryLayerStore,
    MemoryType,
)
from encre.memdir.recall import EmbeddingBackend, RecallEngine, RecallHit
from encre.secure_io import read_jsonl

#: An entry that is never retrieved or referenced, created this far in the past.
_LONG_AGO = time.time() - 400 * 24 * 3600.0


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A layer store rooted in a temp tree, with the data dir isolated."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    return MemoryLayerStore(root=tmp_path / "memory")


def _entry(
    store: MemoryLayerStore,
    content: str,
    *,
    memory_type: str = MemoryType.SEMANTIC.value,
    importance: float = 0.7,
    created_at: float | None = None,
) -> MemoryEntry:
    """Persist a fully-formed long-term memory and return it."""
    entry = MemoryEntry(
        content=content,
        type=memory_type,
        importance=importance,
        source="test",
    )
    if created_at is not None:
        entry.created_at = created_at
        entry.decay.last_access = created_at
    store.store(entry)
    return entry


# ----------------------------------------------------------------------
# 1. Spreading activation over explicit links (traceable)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "relation",
    [
        LinkRelation.CAUSAL,
        LinkRelation.DEPENDENCY,
        LinkRelation.SAME_GOAL,
        LinkRelation.REFERENCE,
    ],
)
def test_spreading_activation_recalls_linked_memories(store, relation):
    """A seed pulls in the memory it is linked to, with provenance."""
    seed = _entry(store, "alpha deploy pipeline orchestrator", importance=0.9)
    linked = _entry(store, "zeta unrelated maintenance mechanism", importance=0.8)
    store.link(seed.id, linked.id, relation)

    engine = RecallEngine(store, record_access=False)
    hits = engine.recall("alpha deploy pipeline orchestrator")
    by_id = {hit.entry_id: hit for hit in hits}

    # The seed is a pure similarity hit.
    assert seed.id in by_id
    assert by_id[seed.id].is_seed
    assert by_id[seed.id].hops == 0
    assert by_id[seed.id].via_links == []

    # The linked memory is recalled through the link, not by similarity.
    assert linked.id in by_id
    spread = by_id[linked.id]
    assert not spread.is_seed
    assert spread.hops == 1
    assert spread.origin_id == seed.id
    assert spread.via_links[0]["relation"] == relation.value
    assert spread.via_links[0]["source_id"] == seed.id
    assert spread.via_links[0]["target_id"] == linked.id
    assert spread.score > 0.0


def test_spread_hit_is_traceable_through_multiple_hops(store):
    """A two-hop hit carries the full link path back to its seed."""
    seed = _entry(store, "alpha deploy pipeline orchestrator", importance=0.9)
    middle = _entry(store, "beta caching subsystem internal", importance=0.8)
    far = _entry(store, "gamma telemetry collector endpoint", importance=0.8)
    store.link(seed.id, middle.id, LinkRelation.CAUSAL)
    store.link(middle.id, far.id, LinkRelation.DEPENDENCY)

    engine = RecallEngine(store, record_access=False, max_hops=3)
    hits = engine.recall("alpha deploy pipeline orchestrator")
    far_hit = next(hit for hit in hits if hit.entry_id == far.id)

    assert far_hit.hops == 2
    assert far_hit.origin_id == seed.id
    assert [edge["relation"] for edge in far_hit.via_links] == ["causal", "dependency"]
    assert far_hit.score < 0.45  # decayed below the one-hop activation


# ----------------------------------------------------------------------
# 2. Boundaries: max hops, min activation, no links
# ----------------------------------------------------------------------


def test_max_hops_boundary_stops_spreading(store):
    """Nothing beyond ``max_hops`` is recalled."""
    a = _entry(store, "alpha deploy pipeline orchestrator", importance=0.9)
    b = _entry(store, "beta caching subsystem internal", importance=0.8)
    c = _entry(store, "gamma telemetry collector endpoint", importance=0.8)
    d = _entry(store, "delta retention compaction schedule", importance=0.8)
    store.link(a.id, b.id, LinkRelation.CAUSAL)
    store.link(b.id, c.id, LinkRelation.CAUSAL)
    store.link(c.id, d.id, LinkRelation.CAUSAL)

    engine = RecallEngine(store, record_access=False, max_hops=2)
    hops = {hit.entry_id: hit.hops for hit in engine.recall("alpha deploy pipeline orchestrator")}

    assert hops[b.id] == 1
    assert hops[c.id] == 2
    assert d.id not in hops


def test_min_activation_boundary_blocks_weak_links(store):
    """A link too weak to clear the activation floor is not traversed."""
    seed = _entry(store, "alpha deploy pipeline orchestrator", importance=0.9)
    weak = _entry(store, "zeta unrelated maintenance mechanism", importance=0.8)
    store.link(seed.id, weak.id, LinkRelation.REFERENCE, weight=0.05)

    engine = RecallEngine(store, record_access=False, min_activation=0.05)
    hits = engine.recall("alpha deploy pipeline orchestrator")

    assert {hit.entry_id for hit in hits} == {seed.id}


def test_no_links_degrades_to_pure_similarity(store):
    """Without links recall returns only similarity seeds, at hop 0."""
    match = _entry(store, "the deploy pipeline uses blue environment", importance=0.9)
    _entry(store, "an entirely different observation about tea", importance=0.5)

    engine = RecallEngine(store, record_access=False)
    hits = engine.recall("deploy pipeline blue environment")

    assert hits
    assert hits[0].entry_id == match.id
    assert all(hit.is_seed and hit.hops == 0 and hit.via_links == [] for hit in hits)


def test_blank_query_returns_nothing(store):
    """A blank query never recalls anything."""
    _entry(store, "the deploy pipeline uses blue environment")
    assert RecallEngine(store, record_access=False).recall("   ") == []


# ----------------------------------------------------------------------
# 3. Decay: freshness + importance -> archived
# ----------------------------------------------------------------------


def test_decay_archives_stale_low_importance_memory(store):
    """A long-unreferenced, low-importance memory decays and is archived."""
    fresh = _entry(store, "fresh important memory", importance=0.9)
    stale = _entry(
        store, "stale low value memory", importance=0.2, created_at=_LONG_AGO,
    )

    engine = RecallEngine(store, record_access=False)
    summary = engine.forget()

    assert summary["archived"] == [stale.id]
    assert stale.decay.archived is True
    assert stale.decay.decay_score < 0.05
    assert fresh.decay.archived is False
    assert fresh.decay.decay_score > 0.5
    assert [entry.id for entry in engine.archived()] == [stale.id]


def test_recently_accessed_memory_survives_decay(store):
    """Recording an access refreshes freshness so the memory is not archived."""
    stale = _entry(
        store, "stale but recently used memory", importance=0.2, created_at=_LONG_AGO,
    )
    store.record_access(stale.id)

    store.apply_decay()

    assert stale.decay.archived is False
    assert store.archived() == []


# ----------------------------------------------------------------------
# 4. Archived memories: invisible by default, auditable, never deleted
# ----------------------------------------------------------------------


def test_archived_memory_invisible_but_auditable_and_persisted(store):
    """Archiving hides a memory from recall without deleting any record."""
    stale = _entry(
        store,
        "obsolete deployment convention",
        importance=0.1,
        created_at=_LONG_AGO,
    )
    layer_file = store.layers_dir / "semantic.jsonl"
    records_before = len(read_jsonl(layer_file))
    assert records_before >= 1

    summary = store.apply_decay()
    assert stale.id in summary["archived"]

    engine = RecallEngine(store, record_access=False)

    # Default recall: archived memory is invisible.
    assert engine.recall("obsolete deployment convention") == []

    # Audit surface: the memory is still readable, and include_archived
    # brings it back into recall explicitly.
    assert [entry.id for entry in engine.archived()] == [stale.id]
    revived = engine.recall(
        "obsolete deployment convention", include_archived=True,
    )
    assert stale.id in {hit.entry_id for hit in revived}

    # No physical deletion: the JSONL only ever grew, and the record remains.
    records_after = read_jsonl(layer_file)
    assert len(records_after) >= records_before
    assert any(record.get("id") == stale.id for record in records_after)

    # Archival survives a reload (it is persisted, not in-memory only).
    store.reload()
    assert [entry.id for entry in store.archived()] == [stale.id]


# ----------------------------------------------------------------------
# 5. Local default; native optional; silent fallback
# ----------------------------------------------------------------------


def test_local_is_default_and_offline_usable(store):
    """Without any embedding configuration recall runs on the local backend."""
    _entry(store, "the deploy pipeline uses blue environment", importance=0.9)

    engine = RecallEngine(store, record_access=False)
    assert engine.backend == EmbeddingBackend.LOCAL.value

    hits = engine.recall("deploy pipeline blue environment")
    assert hits
    assert all(hit.backend == EmbeddingBackend.LOCAL.value for hit in hits)


def test_native_unavailable_falls_back_to_local(store, monkeypatch):
    """When native is missing, an explicitly native request degrades silently."""
    _entry(store, "the deploy pipeline uses blue environment", importance=0.9)
    monkeypatch.setattr(native_backend, "_HAS_NATIVE", False)

    engine = RecallEngine(
        store,
        backend=EmbeddingBackend.NATIVE.value,
        embedding_fn=lambda _text: [1.0, 0.0],
        embedding_index={"anything": [1.0, 0.0]},
        record_access=False,
    )

    assert engine.backend == EmbeddingBackend.LOCAL.value
    assert engine.recall("deploy pipeline blue environment")


def test_native_without_embeddings_falls_back_to_local(store):
    """Native is enabled only when an embedder and an index are supplied."""
    _entry(store, "the deploy pipeline uses blue environment", importance=0.9)

    engine = RecallEngine(store, backend="auto", record_access=False)

    assert engine.backend == EmbeddingBackend.LOCAL.value
    assert engine.recall("deploy pipeline blue environment")


def test_explicit_native_backend_scores_with_native(store):
    """With embeddings configured, native cosine similarity drives the seeds."""
    if not native_backend._HAS_NATIVE:
        pytest.skip("native extension is not built")

    target = _entry(store, "target memory about caching", importance=0.9)
    other = _entry(store, "unrelated memory about tea", importance=0.9)

    def embed(text: str) -> list[float]:
        return [1.0, 0.0] if "cach" in text else [0.0, 1.0]

    engine = RecallEngine(
        store,
        backend=EmbeddingBackend.NATIVE.value,
        embedding_fn=embed,
        embedding_index={target.id: [1.0, 0.0], other.id: [0.0, 1.0]},
        seed_min_score=0.5,
        record_access=False,
    )

    assert engine.backend == EmbeddingBackend.NATIVE.value
    hits = engine.recall("anything about caching")
    assert hits
    assert hits[0].entry_id == target.id
    assert hits[0].backend == EmbeddingBackend.NATIVE.value


def test_invalid_backend_is_rejected(store):
    """An unknown backend name is a caller error, not a silent fallback."""
    with pytest.raises(ValueError):
        RecallEngine(store, backend="quantum")


# ----------------------------------------------------------------------
# 6. Recall feeds the Task 4 promotion trigger
# ----------------------------------------------------------------------


def test_recall_hit_records_access_and_promotes_buffered(store):
    """Recalling a buffered candidate records an access and promotes it."""
    candidate = MemoryCandidate(
        content="the cache layer uses redis",
        significance=0.7,
        reasons=["repeated_pattern"],
        suggested_type=MemoryType.SEMANTIC.value,
        session_id="sess-1",
    )
    buffered = store.intake(candidate)
    assert store.long_term() == []
    assert [entry.id for entry in store.buffer()] == [buffered.id]

    engine = RecallEngine(store)
    hits = engine.recall("the cache layer uses redis")

    assert buffered.id in {hit.entry_id for hit in hits}
    assert store.buffer() == []
    promoted = store.get(buffered.id)
    assert promoted is not None
    assert promoted.decay.access_count >= 2


def test_recall_records_access_on_long_term_memory(store):
    """A long-term hit bumps its access bookkeeping."""
    entry = _entry(store, "the deploy pipeline uses blue environment", importance=0.9)
    assert entry.decay.access_count == 0

    RecallEngine(store).recall("deploy pipeline blue environment")

    assert store.get(entry.id).decay.access_count == 1


def test_recall_hit_serialises_for_auditing(store):
    """RecallHit exposes a JSON-friendly view for Task 10 validation."""
    seed = _entry(store, "alpha deploy pipeline orchestrator", importance=0.9)
    linked = _entry(store, "zeta unrelated maintenance mechanism", importance=0.8)
    store.link(seed.id, linked.id, LinkRelation.CAUSAL)

    hit = next(
        h for h in RecallEngine(store, record_access=False).recall(
            "alpha deploy pipeline orchestrator"
        )
        if h.entry_id == linked.id
    )
    payload = hit.to_dict()

    assert isinstance(hit, RecallHit)
    assert payload["entry_id"] == linked.id
    assert payload["hops"] == 1
    assert payload["is_seed"] is False
    assert payload["origin_id"] == seed.id
    assert payload["via_links"][0]["relation"] == "causal"
