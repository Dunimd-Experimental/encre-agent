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

"""Associative recall: similarity seeds + spreading activation over links.

Core principle (project-wide, non-negotiable): **context != memory**.

Memory retrieval is not keyword lookup.  A remembered thing is *associated*
with other remembered things, so recalling one should pull in the cluster it
belongs to -- "由一条想起一串".  This module implements exactly that, on top of
the storage tier in :mod:`encre.memdir.layers`:

1. **Seeds.**  The query is matched against visible memories with the existing
   local :class:`~encre.memdir.semantic.SemanticMemorySearch` (TF-IDF cosine
   with a Jaccard fallback).  This reuses the one local retrieval core -- there
   is no parallel scorer here.
2. **Spreading activation.**  Activation then flows along the explicit
   :class:`~encre.memdir.layers.MemoryLink` edges, decaying with every hop and
   weighted by the link relation, so a seed activates what it causally
   implicates, depends on, shares a goal with, or merely references.
3. **Traceability.**  Every :class:`RecallHit` records whether it was a seed
   and, when it was spread to, the exact link path (``via_links``) and the seed
   it originated from (``origin_id``).

Boundaries are explicit and configurable: ``max_hops``, ``min_activation`` and
``limit`` stop activation from flooding the whole graph.  A hit also feeds the
promotion mechanism from Task 4: recall calls
:meth:`~encre.memdir.layers.MemoryLayerStore.record_access` on every returned
non-archived memory, so a buffered candidate that is recalled is promoted to
long-term memory.

Embedding backend
=================
Local TF-IDF + Jaccard is the **default and always-available** path, so the
engine stays fully usable offline.  A native embedding backend (Rust
``cosine_similarity`` / ``search_embedding_index``) can be selected as an
*optional stronger* scorer when the caller supplies an embedding function plus
a vector index.  The backend is chosen explicitly via ``backend="auto" |
"local" | "native"``; ``auto`` prefers native when it is actually usable and
falls back otherwise.  When the strong backend is unavailable, unconfigured,
or fails at call time, the engine **silently** falls back to the local path:
the behaviour is identical, only precision/speed differ, and nothing is ever
raised into the caller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from encre import native as native_backend
from encre.logging_config import get_logger
from encre.memdir.layers import LinkRelation, MemoryEntry, MemoryLayerStore
from encre.memdir.semantic import SemanticMemorySearch

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_RELATION_WEIGHTS",
    "EmbeddingBackend",
    "RecallEngine",
    "RecallHit",
]

#: Propagation weight per link relation, applied on top of the edge's own
#: ``weight``.  The ordering reflects how tightly the association binds two
#: memories: a *causal* link means one thing produced the other, and a
#: *dependency* means one cannot stand without the other, so both spread
#: strongly; a *same_goal* link is a goal cluster (strong but looser); a bare
#: *reference* is the weakest association, a citation without entailment.
DEFAULT_RELATION_WEIGHTS: dict[str, float] = {
    LinkRelation.CAUSAL.value: 0.90,
    LinkRelation.DEPENDENCY.value: 0.85,
    LinkRelation.SAME_GOAL.value: 0.80,
    LinkRelation.REFERENCE.value: 0.60,
}

#: Fallback propagation weight for an unrecognised (free-form) relation.
DEFAULT_UNKNOWN_RELATION_WEIGHT = 0.50

#: Activation multiplier applied per hop: activation halves each hop, so
#: spreading dies out quickly and cannot flood the graph.
DEFAULT_HOP_DECAY = 0.50

#: Maximum number of link hops explored from the seeds.
DEFAULT_MAX_HOPS = 2

#: Minimum activation for an entry to be recalled or to keep spreading.
DEFAULT_MIN_ACTIVATION = 0.05

#: Minimum similarity for a memory to qualify as a seed.
DEFAULT_SEED_MIN_SCORE = 0.05

#: Maximum number of seeds pulled from similarity search.
DEFAULT_SEED_LIMIT = 10

#: Maximum number of hits returned by :meth:`RecallEngine.recall`.
DEFAULT_RESULT_LIMIT = 10


class EmbeddingBackend(str, Enum):
    """Selectable scoring backend for recall seeds."""

    AUTO = "auto"
    LOCAL = "local"
    NATIVE = "native"


@dataclass
class _Activation:
    """Internal activation bookkeeping for one entry."""

    score: float
    hops: int
    path: list[dict[str, Any]]
    origin_id: str


@dataclass
class RecallHit:
    """One recalled memory, with the provenance needed to audit the recall.

    Args:
        entry: The recalled :class:`MemoryEntry`.
        score: Final activation (``[0.0, 1.0]``; seeds are their similarity).
        hops: ``0`` for a similarity seed, otherwise the link distance.
        via_links: The link edges traversed to reach this hit, seed-first.
        origin_id: Id of the seed this hit originated from (its own id when it
            is a seed).
        backend: The scoring backend actually used (``"local"`` / ``"native"``).
    """

    entry: MemoryEntry
    score: float
    hops: int = 0
    via_links: list[dict[str, Any]] = field(default_factory=list)
    origin_id: str = ""
    backend: str = EmbeddingBackend.LOCAL.value

    @property
    def entry_id(self) -> str:
        """Id of the recalled entry."""
        return self.entry.id

    @property
    def is_seed(self) -> bool:
        """Whether this hit came straight from similarity search."""
        return self.hops == 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        return {
            "entry_id": self.entry.id,
            "type": self.entry.type,
            "content": self.entry.content,
            "score": self.score,
            "hops": self.hops,
            "is_seed": self.is_seed,
            "origin_id": self.origin_id,
            "via_links": [dict(link) for link in self.via_links],
            "backend": self.backend,
        }


class RecallEngine:
    """Associative recall over a :class:`MemoryLayerStore`.

    Args:
        store: The layered memory store to recall from.
        backend: ``"auto"`` (default), ``"local"`` or ``"native"``.  ``auto``
            picks native only when it is genuinely usable, otherwise local.
        embedding_fn: Optional ``text -> list[float]`` embedder supplied by the
            caller.  Required for the native backend to activate.
        embedding_index: Optional ``{entry_id: vector}`` index.  When given
            together with ``embedding_fn``, native ``cosine_similarity`` scores
            the seeds.
        embedding_workspace: Optional workspace holding a persisted native
            embedding index; when given, native ``search_embedding_index`` is
            used and each hit's ``file`` field is read as the memory entry id.
        relation_weights: Optional override merged onto
            :data:`DEFAULT_RELATION_WEIGHTS`.
        hop_decay: Activation multiplier per hop.
        max_hops: Maximum link hops explored from the seeds.
        min_activation: Minimum activation to be recalled / keep spreading.
        seed_min_score: Minimum similarity for a seed.
        seed_limit: Maximum number of similarity seeds.
        limit: Maximum number of hits returned.
        record_access: Whether a returned hit records an access (the Task 4
            promotion trigger).  Defaults to ``True``.
    """

    def __init__(
        self,
        store: MemoryLayerStore,
        *,
        backend: str = EmbeddingBackend.AUTO.value,
        embedding_fn: Callable[[str], list[float]] | None = None,
        embedding_index: dict[str, list[float]] | None = None,
        embedding_workspace: str | None = None,
        relation_weights: dict[str, float] | None = None,
        hop_decay: float = DEFAULT_HOP_DECAY,
        max_hops: int = DEFAULT_MAX_HOPS,
        min_activation: float = DEFAULT_MIN_ACTIVATION,
        seed_min_score: float = DEFAULT_SEED_MIN_SCORE,
        seed_limit: int = DEFAULT_SEED_LIMIT,
        limit: int = DEFAULT_RESULT_LIMIT,
        record_access: bool = True,
    ) -> None:
        self._store = store
        self._requested_backend = EmbeddingBackend(str(backend).strip().lower())
        self._embedding_fn = embedding_fn
        self._embedding_index = embedding_index
        self._embedding_workspace = embedding_workspace
        self._relation_weights = dict(DEFAULT_RELATION_WEIGHTS)
        if relation_weights:
            self._relation_weights.update(
                {str(k): float(v) for k, v in relation_weights.items()}
            )
        self._hop_decay = float(hop_decay)
        self._max_hops = max(0, int(max_hops))
        self._min_activation = float(min_activation)
        self._seed_min_score = float(seed_min_score)
        self._seed_limit = max(1, int(seed_limit))
        self._limit = max(1, int(limit))
        self._record_access = bool(record_access)
        #: True only when the compiled Rust extension actually loaded.
        self._native_available = bool(getattr(native_backend, "_HAS_NATIVE", False))
        self._backend = self._resolve_backend()

    # ------------------------------------------------------------------
    # Backend selection
    # ------------------------------------------------------------------

    def _native_usable(self) -> bool:
        """Whether native embedding scoring is genuinely available here.

        Native is enabled only when the extension loaded *and* the caller
        supplied both an embedder and something to score against (an in-memory
        index or a persisted workspace index).  Otherwise the local backend is
        used, unchanged.
        """
        if not self._native_available or self._embedding_fn is None:
            return False
        if self._embedding_workspace:
            return callable(getattr(native_backend, "search_embedding_index", None))
        if self._embedding_index is not None:
            return callable(getattr(native_backend, "cosine_similarity", None))
        return False

    def _resolve_backend(self) -> EmbeddingBackend:
        """Resolve the requested backend to the one actually used."""
        if self._requested_backend is EmbeddingBackend.LOCAL:
            return EmbeddingBackend.LOCAL
        if self._native_usable():
            return EmbeddingBackend.NATIVE
        # AUTO and an unusable NATIVE both degrade silently to local: offline
        # availability is never compromised and nothing is raised.
        return EmbeddingBackend.LOCAL

    @property
    def backend(self) -> str:
        """The scoring backend actually in use (``"local"`` / ``"native"``)."""
        return self._backend.value

    @property
    def store(self) -> MemoryLayerStore:
        """The store this engine recalls from."""
        return self._store

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------

    def recall(
        self,
        query: str,
        *,
        top_k: int | None = None,
        max_hops: int | None = None,
        min_activation: float | None = None,
        include_archived: bool = False,
        record_access: bool | None = None,
    ) -> list[RecallHit]:
        """Recall memories for ``query``, spreading activation along links.

        Args:
            query: Free-text query.
            top_k: Override for the result limit.
            max_hops: Override for the hop limit.
            min_activation: Override for the activation floor.
            include_archived: When ``True``, archived memories are visible again
                (the audit path).  When ``False`` (default) they are invisible
                to recall.
            record_access: Override for the access-recording behaviour.

        Returns:
            Up to ``top_k`` :class:`RecallHit` objects, highest activation
            first.  Empty for a blank query or when nothing matches.
        """
        text = str(query or "")
        if not text.strip():
            return []
        k = self._limit if top_k is None else max(1, int(top_k))
        hops_limit = self._max_hops if max_hops is None else max(0, int(max_hops))
        floor = self._min_activation if min_activation is None else float(min_activation)
        do_record = self._record_access if record_access is None else bool(record_access)

        pool = self._visible_pool(include_archived)
        if not pool:
            return []
        pool_by_id = {entry.id: entry for entry in pool}

        seed_scores = self._seed_scores(text, pool)
        if not seed_scores:
            return []

        activations = self._spread(seed_scores, pool_by_id, hops_limit, floor)

        hits: list[RecallHit] = []
        for entry_id, activation in activations.items():
            if activation.score < floor:
                continue
            entry = pool_by_id.get(entry_id)
            if entry is None:
                continue
            hits.append(RecallHit(
                entry=entry,
                score=round(float(activation.score), 6),
                hops=activation.hops,
                via_links=list(activation.path),
                origin_id=activation.origin_id,
                backend=self._backend.value,
            ))

        hits.sort(key=lambda hit: (-hit.score, hit.hops, hit.entry.id))
        hits = hits[:k]

        if do_record:
            self._record_hits(hits)
        return hits

    def _record_hits(self, hits: list[RecallHit]) -> None:
        """Record an access for every recalled (non-archived) memory.

        This is the Task 4 promotion trigger: a buffered candidate that is
        recalled crosses the access threshold and becomes long-term memory.
        Failures are isolated so recall itself never fails.
        """
        for hit in hits:
            if hit.entry.decay.archived:
                continue
            try:
                self._store.record_access(hit.entry.id)
            except Exception:
                logger.warning(
                    "[memdir.recall] failed to record access entry_id=%s",
                    hit.entry.id, exc_info=True,
                )

    def _visible_pool(self, include_archived: bool) -> list[MemoryEntry]:
        """Long-term plus buffered memories, archived filtered by default."""
        entries = self._store.long_term() + self._store.buffer()
        if include_archived:
            return entries
        return [entry for entry in entries if not entry.decay.archived]

    # ------------------------------------------------------------------
    # Seeds (similarity) -- local by default, native when configured
    # ------------------------------------------------------------------

    def _seed_scores(self, query: str, pool: list[MemoryEntry]) -> dict[str, float]:
        """Score ``query`` against the pool, returning ``{entry_id: score}``."""
        if self._backend is EmbeddingBackend.NATIVE:
            try:
                native_scores = self._native_seed_scores(query, pool)
            except Exception:
                logger.warning(
                    "[memdir.recall] native seed scoring failed; using local",
                    exc_info=True,
                )
                native_scores = None
            if native_scores is not None:
                return native_scores
        return self._local_seed_scores(query, pool)

    def _local_seed_scores(self, query: str, pool: list[MemoryEntry]) -> dict[str, float]:
        """Seed scoring through the existing local TF-IDF/Jaccard searcher."""
        searcher = SemanticMemorySearch(str(self._store.layers_dir))
        searcher.index({entry.id: entry.content for entry in pool})
        results = searcher.search(
            query, top_k=self._seed_limit, min_score=self._seed_min_score,
        )
        return {result.file_name: float(result.score) for result in results}

    def _native_seed_scores(
        self, query: str, pool: list[MemoryEntry]
    ) -> dict[str, float] | None:
        """Seed scoring through the native embedding backend.

        Returns ``None`` when no query vector is available, which lets the
        caller fall back to the local scorer.  A non-``None`` (possibly empty)
        mapping is a genuine native result and is used as-is.
        """
        vector = self._embedding_fn(query) if self._embedding_fn else None
        if not vector:
            return None
        query_vec = [float(value) for value in vector]

        if self._embedding_workspace:
            raw = native_backend.search_embedding_index(
                self._embedding_workspace, json.dumps(query_vec), self._seed_limit,
            )
            hits = json.loads(raw) if isinstance(raw, str) else raw
            valid_ids = {entry.id for entry in pool}
            scores: dict[str, float] = {}
            for hit in hits or []:
                if not isinstance(hit, dict):
                    continue
                entry_id = str(hit.get("file") or hit.get("id") or "")
                if entry_id not in valid_ids:
                    continue
                score = max(0.0, float(hit.get("score", 0.0)))
                if score >= self._seed_min_score:
                    scores[entry_id] = score
            return scores

        index = self._embedding_index or {}
        scores = {}
        for entry in pool:
            stored = index.get(entry.id)
            if not stored:
                continue
            score = max(0.0, float(native_backend.cosine_similarity(
                query_vec, [float(value) for value in stored],
            )))
            if score >= self._seed_min_score:
                scores[entry.id] = score
        return scores

    # ------------------------------------------------------------------
    # Spreading activation
    # ------------------------------------------------------------------

    def _relation_weight(self, relation: Any) -> float:
        """Propagation weight for a link relation (free-form tolerated)."""
        return self._relation_weights.get(str(relation), DEFAULT_UNKNOWN_RELATION_WEIGHT)

    def _adjacency(
        self, pool: list[MemoryEntry]
    ) -> dict[str, list[tuple[str, str, float]]]:
        """Build the association graph from explicit links.

        Edges are made symmetric: if ``A`` references ``B`` then recalling
        ``A`` activates ``B`` and recalling ``B`` activates ``A``, which is how
        associative memory behaves.  Links pointing outside the visible pool
        are ignored.
        """
        valid_ids = {entry.id for entry in pool}
        adjacency: dict[str, list[tuple[str, str, float]]] = {}
        for entry in pool:
            for link in entry.links:
                if link.target_id not in valid_ids:
                    continue
                weight = max(0.0, min(1.0, float(link.weight)))
                adjacency.setdefault(entry.id, []).append(
                    (link.target_id, str(link.relation), weight)
                )
                adjacency.setdefault(link.target_id, []).append(
                    (entry.id, str(link.relation), weight)
                )
        return adjacency

    def _spread(
        self,
        seed_scores: dict[str, float],
        pool_by_id: dict[str, MemoryEntry],
        max_hops: int,
        floor: float,
    ) -> dict[str, _Activation]:
        """Propagate activation from the seeds along the association graph."""
        adjacency = self._adjacency(list(pool_by_id.values()))
        best: dict[str, _Activation] = {}
        for entry_id, score in seed_scores.items():
            if entry_id not in pool_by_id:
                continue
            best[entry_id] = _Activation(float(score), 0, [], entry_id)

        frontier = dict(best)
        for hop in range(1, max_hops + 1):
            next_frontier: dict[str, _Activation] = {}
            for source_id, activation in frontier.items():
                if activation.score < floor:
                    continue
                for target_id, relation, link_weight in adjacency.get(source_id, ()):
                    edge_weight = self._relation_weight(relation) * link_weight
                    contribution = activation.score * edge_weight * self._hop_decay
                    if contribution < floor:
                        continue
                    existing = best.get(target_id)
                    if existing is not None and existing.score >= contribution:
                        continue
                    edge = {
                        "source_id": source_id,
                        "target_id": target_id,
                        "relation": relation,
                        "weight": round(edge_weight, 6),
                        "hop": hop,
                    }
                    spread = _Activation(
                        contribution, hop, activation.path + [edge], activation.origin_id,
                    )
                    best[target_id] = spread
                    next_frontier[target_id] = spread
            frontier = next_frontier
            if not frontier:
                break
        return best

    # ------------------------------------------------------------------
    # Forgetting / audit (delegates to the store's single implementation)
    # ------------------------------------------------------------------

    def forget(
        self, *, now: float | None = None, threshold: float | None = None
    ) -> dict[str, Any]:
        """Run the store's long-term decay sweep (archival, never deletion).

        Thin delegate to :meth:`MemoryLayerStore.apply_decay` so recall callers
        do not need a second forgetting implementation.
        """
        if threshold is None:
            return self._store.apply_decay(now=now)
        return self._store.apply_decay(now=now, threshold=threshold)

    def archived(self) -> list[MemoryEntry]:
        """Audit view: archived memories that default recall cannot see."""
        return self._store.archived()
