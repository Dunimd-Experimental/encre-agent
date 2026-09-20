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

"""Layered memory: five kinds of memory on one unified entry schema.

Core principle (project-wide, non-negotiable): **context != memory**.

This module is the storage half of that principle.  The significance judge
(:mod:`encre.memdir.judgment`) decides *whether* something is meaningful and
emits a :class:`~encre.memdir.judgment.MemoryCandidate`.  This module decides
*how long* it lives:

* A candidate first lands in the **short-term buffer** -- it is a proposal,
  not yet a memory.
* A candidate that *recurs* (same content / high similarity) or is *re-used*
  (referenced by a link, hit by retrieval) is deduplicated and **promoted to
  long-term memory**.
* A one-off, unreferenced candidate is **evicted** when the buffer overflows
  or its time-to-live expires.  Nothing is written to long-term memory.

The five kinds (``working`` / ``episodic`` / ``semantic`` / ``procedural`` /
``autobiographical``) all share one :class:`MemoryEntry` schema, so retrieval
(Task 5) and the procedural-memory bridge (Task 7.4) speak a single language.

Working memory is **not** re-implemented here: the canonical
:class:`~encre.memdir.semantic.WorkingMemory` scratchpad is the working layer's
view, and :meth:`MemoryLayerStore.save_working` /
:meth:`MemoryLayerStore.load_working` persist it through the shared entry
schema.  There is exactly one working-memory implementation in the tree.

Forgetting (Task 5.3) is owned here too, for exactly the same reason: there is
one :meth:`MemoryLayerStore._decay_score`, used by merging, promotion, access
recording and the decay sweep alike.  :meth:`MemoryLayerStore.apply_decay`
recomputes every long-term entry's freshness and marks stale ones
``archived`` -- invisible to recall, still auditable, and **never physically
deleted** (the JSONL files are append-only revision logs).

Human-editable ``.md`` memory files are **ingested into this store** rather
than being searched by a second store: :meth:`MemoryLayerStore.ingest_memory_dir`
reads the memory directory through the existing file facade (reusing its
entrypoint-skip rules and its front-matter parser) and turns every memory file
into one :class:`MemoryEntry`, so retrieval goes only through
:class:`~encre.memdir.recall.RecallEngine`.

Everything is written through :mod:`encre.secure_io`, so every layer file is
AES-256-GCM ciphertext at rest, and the store is fully offline-capable.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger
from encre.memdir.judgment import MemoryCandidate
from encre.memdir.semantic import MemoryConsolidator, WorkingMemory
from encre.paths import get_data_dir
from encre.secure_io import append_jsonl, read_json, read_jsonl, read_text, write_json

logger = get_logger(__name__)

__all__ = [
    "DecayState",
    "LinkRelation",
    "MemoryEntry",
    "MemoryLayerStore",
    "MemoryLink",
    "MemoryType",
]

#: Default short-term buffer capacity (entries).
DEFAULT_BUFFER_CAPACITY = 64

#: Default short-term buffer time-to-live in seconds (entries not repeated or
#: referenced within this window are evicted).
DEFAULT_BUFFER_TTL_SECONDS = 3600.0

#: Half-life used by :meth:`MemoryLayerStore._decay_score` (7 days).
DECAY_HALF_LIFE_SECONDS = 7 * 24 * 3600.0

#: Importance added when a memory is re-observed or re-referenced.
IMPORTANCE_BUMP = 0.1

#: Access count at which a buffered entry is consolidated into long-term memory.
PROMOTION_ACCESS_THRESHOLD = 2

#: Decay score below which a long-term memory is archived (forgetting).
#: Archiving is *invisible but auditable*: the entry disappears from recall but
#: its records stay on disk and remain reachable through the audit surface.
ARCHIVE_DECAY_THRESHOLD = 0.05

#: Importance given to an ingested human-curated ``.md`` memory file.  Such a
#: file was deliberately written and kept by the user, so it ranks slightly
#: above the ``0.5`` a judged candidate gets by default; it still decays like
#: every other memory (there is one forgetting implementation, not two).
INGESTED_MEMORY_IMPORTANCE = 0.6

#: Metadata key holding the sha256 of an ingested file's stored content.  It is
#: the idempotency signal of :meth:`MemoryLayerStore.ingest_memory_dir`.
INGEST_CONTENT_HASH_KEY = "content_hash"

#: A front-matter block delimited by ``---`` lines -- the same shape the memory
#: file facade parses.  Used here only to *strip* the block from the body; the
#: parsing itself stays in the facade (one parser, never two).
_FRONTMATTER_BLOCK_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)


class MemoryType(str, Enum):
    """The five kinds of memory, on one shared schema.

    ``working`` is the transient reasoning scratchpad; the other four are the
    durable kinds.  The string values match the ``suggested_type`` produced by
    the significance judge.
    """

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    AUTOBIOGRAPHICAL = "autobiographical"

    @classmethod
    def from_suggestion(cls, value: Any) -> "MemoryType":
        """Map a judge ``suggested_type`` (or any string) to a memory type.

        Unknown values fall back to ``EPISODIC`` (a lived event), never raise:
        the judge is the gate, this is only a classification hint.
        """
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.EPISODIC


class LinkRelation(str, Enum):
    """Explicit relations between memories (used by associative recall)."""

    REFERENCE = "reference"
    DEPENDENCY = "dependency"
    CAUSAL = "causal"
    SAME_GOAL = "same_goal"


def _as_float(value: Any, default: float) -> float:
    """Coerce ``value`` to float, returning ``default`` when absent/invalid."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    """Coerce ``value`` to int, returning ``default`` when absent/invalid."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class DecayState:
    """Freshness/usage bookkeeping that drives forgetting (Task 5).

    Args:
        last_access: Wall-clock time of the most recent access or reference.
        access_count: How many times the memory has been observed/referenced;
            the promotion and forgetting heuristics both read it.
        archived: Whether the memory has been archived (invisible to recall
            but still auditable).  Physical deletion is never performed.
        decay_score: Cached ``importance * freshness`` score.
    """

    last_access: float = field(default_factory=time.time)
    access_count: int = 0
    archived: bool = False
    decay_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        return {
            "last_access": self.last_access,
            "access_count": self.access_count,
            "archived": self.archived,
            "decay_score": self.decay_score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DecayState":
        """Build a :class:`DecayState` from a dict (see :meth:`to_dict`)."""
        data = data or {}
        return cls(
            last_access=_as_float(data.get("last_access"), time.time()),
            access_count=_as_int(data.get("access_count"), 0),
            archived=bool(data.get("archived", False)),
            decay_score=_as_float(data.get("decay_score"), 0.0),
        )


@dataclass
class MemoryLink:
    """An explicit, typed edge from one memory to another.

    Args:
        target_id: Id of the memory this edge points at.
        relation: One of :class:`LinkRelation` (``reference`` / ``dependency``
            / ``causal`` / ``same_goal``); free-form strings are tolerated.
        weight: Edge strength in ``[0.0, 1.0]`` (used by spreading activation).
        created_at: Wall-clock time the edge was created.
    """

    target_id: str
    relation: str = LinkRelation.REFERENCE.value
    weight: float = 1.0
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if isinstance(self.relation, Enum):
            self.relation = str(self.relation.value)
        else:
            self.relation = str(self.relation)
        self.target_id = str(self.target_id)
        self.weight = _as_float(self.weight, 1.0)
        self.created_at = _as_float(self.created_at, time.time())

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        return {
            "target_id": self.target_id,
            "relation": self.relation,
            "weight": self.weight,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryLink":
        """Build a :class:`MemoryLink` from a dict (see :meth:`to_dict`)."""
        return cls(
            target_id=str(data.get("target_id", "")),
            relation=data.get("relation", LinkRelation.REFERENCE.value),
            weight=_as_float(data.get("weight"), 1.0),
            created_at=_as_float(data.get("created_at"), time.time()),
        )


@dataclass
class MemoryEntry:
    """One memory, whatever its layer -- the unified entry schema.

    Args:
        content: The remembered statement.
        type: One of :class:`MemoryType` (stored as its string value).
        id: Stable unique id.
        created_at: Wall-clock time the memory was first seen.
        importance: Significance in ``[0.0, 1.0]``; raised on re-observation.
        links: Explicit typed edges to other memories.
        decay: Freshness/usage state (see :class:`DecayState`).
        version: Monotonic revision counter (bumped on every merge/promotion).
        source: Where the entry came from (``significance_judgment``,
            ``working_memory``, ...).
        source_refs: Provenance references (event ids, tool call ids, ...).
        metadata: Free-form extras (judge reasons, promotion reason, ...).
    """

    content: str
    type: str = MemoryType.EPISODIC.value
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    importance: float = 0.5
    links: list[MemoryLink] = field(default_factory=list)
    decay: DecayState = field(default_factory=DecayState)
    version: int = 1
    source: str = ""
    source_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalise loosely-typed inputs so the entry is always JSON-clean."""
        if isinstance(self.type, MemoryType):
            self.type = self.type.value
        else:
            self.type = str(self.type)
        if isinstance(self.decay, dict):
            self.decay = DecayState.from_dict(self.decay)
        elif not isinstance(self.decay, DecayState):
            self.decay = DecayState()
        normalised: list[MemoryLink] = []
        for link in self.links or []:
            if isinstance(link, MemoryLink):
                normalised.append(link)
            elif isinstance(link, dict):
                normalised.append(MemoryLink.from_dict(link))
        self.links = normalised
        self.id = str(self.id)
        self.created_at = _as_float(self.created_at, time.time())
        self.importance = _as_float(self.importance, 0.5)
        self.version = _as_int(self.version, 1)
        self.source = str(self.source)
        self.source_refs = [str(r) for r in (self.source_refs or [])]
        self.metadata = dict(self.metadata or {})

    @property
    def memory_type(self) -> MemoryType:
        """The entry's layer as a :class:`MemoryType` (falls back to episodic)."""
        return MemoryType.from_suggestion(self.type)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        return {
            "id": self.id,
            "type": self.type,
            "content": self.content,
            "created_at": self.created_at,
            "importance": self.importance,
            "links": [link.to_dict() for link in self.links],
            "decay": self.decay.to_dict(),
            "version": self.version,
            "source": self.source,
            "source_refs": list(self.source_refs),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEntry":
        """Build a :class:`MemoryEntry` from a dict (see :meth:`to_dict`)."""
        return cls(
            content=str(data.get("content", "")),
            type=data.get("type", MemoryType.EPISODIC.value),
            id=str(data.get("id", "") or uuid.uuid4().hex),
            created_at=_as_float(data.get("created_at"), time.time()),
            importance=_as_float(data.get("importance"), 0.5),
            links=list(data.get("links") or []),
            decay=data.get("decay") or DecayState(),
            version=_as_int(data.get("version"), 1),
            source=str(data.get("source", "")),
            source_refs=[str(r) for r in (data.get("source_refs") or [])],
            metadata=dict(data.get("metadata", {}) or {}),
        )


class MemoryLayerStore:
    """Five-layer memory store with a short-term buffer and consolidation.

    Candidates enter through :meth:`intake` (short-term buffer).  They become
    long-term memories only when repeated or referenced (:meth:`link` /
    :meth:`record_access`), or during a :meth:`consolidate` sweep; otherwise
    they are evicted.  All persistence goes through :mod:`encre.secure_io`.

    Args:
        root: Data root; defaults to ``get_data_dir("memory")``.  Layer files
            live under ``<root>/layers/``.
        consolidator: Optional :class:`MemoryConsolidator`; one bound to the
            layers directory is created when omitted.
        buffer_capacity: Maximum short-term buffer size.
        buffer_ttl: Short-term buffer time-to-live in seconds.
    """

    #: Durable layers (``working`` is handled separately as a snapshot).
    LONG_TERM_TYPES = (
        MemoryType.EPISODIC,
        MemoryType.SEMANTIC,
        MemoryType.PROCEDURAL,
        MemoryType.AUTOBIOGRAPHICAL,
    )

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        consolidator: MemoryConsolidator | None = None,
        buffer_capacity: int = DEFAULT_BUFFER_CAPACITY,
        buffer_ttl: float = DEFAULT_BUFFER_TTL_SECONDS,
    ) -> None:
        self._root = Path(root) if root is not None else get_data_dir("memory", ensure=False)
        self._layers_dir = self._root / "layers"
        self._consolidator = consolidator or MemoryConsolidator(str(self._layers_dir))
        self._buffer_capacity = max(1, int(buffer_capacity))
        self._buffer_ttl = float(buffer_ttl)
        self._lock = threading.RLock()
        self._buffer: list[MemoryEntry] = []
        self._long_term: dict[str, list[MemoryEntry]] = {
            memory_type.value: [] for memory_type in self.LONG_TERM_TYPES
        }
        self._working_entry: MemoryEntry | None = None
        self._attached: set[int] = set()
        self.reload()

    # ------------------------------------------------------------------
    # Paths and introspection
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """The data root the store persists under."""
        return self._root

    @property
    def layers_dir(self) -> Path:
        """The directory holding the per-layer files."""
        return self._layers_dir

    @property
    def consolidator(self) -> MemoryConsolidator:
        """The consolidator reused for entry-level deduplication."""
        return self._consolidator

    def _type_path(self, memory_type: Any) -> Path:
        """Path of the JSONL file backing one durable layer."""
        return self._layers_dir / f"{self._coerce_type(memory_type).value}.jsonl"

    @property
    def _buffer_path(self) -> Path:
        return self._layers_dir / "_short_term.json"

    @property
    def _working_path(self) -> Path:
        return self._layers_dir / "working.json"

    @staticmethod
    def _coerce_type(memory_type: Any) -> MemoryType:
        """Coerce a :class:`MemoryType` or its string value to the enum."""
        if isinstance(memory_type, MemoryType):
            return memory_type
        return MemoryType(str(memory_type).strip().lower())

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """(Re)load every layer from disk, replacing the in-memory state.

        Long-term files are append-only revision logs: when the same id appears
        several times the last record wins, so a merge/promotion never leaves a
        stale duplicate visible.
        """
        with self._lock:
            self._long_term = {
                memory_type.value: [] for memory_type in self.LONG_TERM_TYPES
            }
            for memory_type in self.LONG_TERM_TYPES:
                by_id: dict[str, MemoryEntry] = {}
                order: list[str] = []
                for record in read_jsonl(self._type_path(memory_type)):
                    if not isinstance(record, dict):
                        continue
                    try:
                        entry = MemoryEntry.from_dict(record)
                    except Exception:
                        logger.warning("[memdir.layers] skipping malformed entry", exc_info=True)
                        continue
                    if entry.id not in by_id:
                        order.append(entry.id)
                    by_id[entry.id] = entry
                self._long_term[memory_type.value] = [by_id[eid] for eid in order]

            self._buffer = self._read_buffer()
            raw_working = read_json(self._working_path, default=None)
            if isinstance(raw_working, dict):
                try:
                    self._working_entry = MemoryEntry.from_dict(raw_working)
                except Exception:
                    self._working_entry = None
            else:
                self._working_entry = None

    def _read_buffer(self) -> list[MemoryEntry]:
        """Read the short-term buffer back from disk (empty when absent)."""
        raw = read_json(self._buffer_path, default=[])
        entries: list[MemoryEntry] = []
        if isinstance(raw, list):
            for record in raw:
                if not isinstance(record, dict):
                    continue
                try:
                    entries.append(MemoryEntry.from_dict(record))
                except Exception:
                    logger.warning("[memdir.layers] skipping malformed buffer entry", exc_info=True)
        return entries

    def _persist_buffer(self) -> None:
        """Rewrite the short-term buffer file (encrypted, atomic overwrite)."""
        if not self._buffer and not self._buffer_path.exists():
            return
        write_json(self._buffer_path, [entry.to_dict() for entry in self._buffer])

    def _persist_entry(self, entry: MemoryEntry) -> None:
        """Append an entry revision to its layer file (encrypted)."""
        append_jsonl(self._type_path(entry.type), entry.to_dict())

    # ------------------------------------------------------------------
    # Intake: candidates -> short-term buffer
    # ------------------------------------------------------------------

    def intake(self, candidate: MemoryCandidate) -> MemoryEntry:
        """Take a judged candidate into short-term memory.

        A candidate never goes straight to long-term memory.  It is buffered,
        and only *merges* into an existing memory when it duplicates one:

        * duplicates a long-term memory -> merged there (deduplicated);
        * duplicates a buffered candidate -> merged and promoted;
        * otherwise -> appended to the short-term buffer.

        Args:
            candidate: A candidate emitted by the significance judge.

        Returns:
            The entry that now holds the candidate (long-term or buffered).
            A blank candidate yields a detached entry that is **not** persisted
            (the judge never emits one; this only hardens the bus path).
        """
        if not str(candidate.content or "").strip():
            logger.debug("[memdir.layers] ignoring blank candidate")
            return MemoryEntry(content="", source="significance_judgment")

        with self._lock:
            now = time.time()
            entry = self._entry_from_candidate(candidate, now)

            existing = self._find_in_long_term(entry)
            if existing is not None:
                self._merge(existing, entry, now)
                self._persist_entry(existing)
                return existing

            buffered = self._consolidator.find_entry_match(entry, self._buffer)
            if buffered is not None:
                self._merge(buffered, entry, now)
                self._promote(buffered, now, reason="repeated")
                return buffered

            self._buffer.append(entry)
            self._persist_buffer()
            return entry

    def _entry_from_candidate(self, candidate: MemoryCandidate, now: float) -> MemoryEntry:
        """Build a buffered :class:`MemoryEntry` from a judged candidate."""
        created_at = _as_float(candidate.created_at, now)
        return MemoryEntry(
            content=str(candidate.content or ""),
            type=MemoryType.from_suggestion(candidate.suggested_type).value,
            created_at=created_at,
            importance=_as_float(candidate.significance, 0.5),
            source="significance_judgment",
            source_refs=list(candidate.source_refs),
            decay=DecayState(last_access=created_at, access_count=1),
            metadata={
                "reasons": list(candidate.reasons),
                "session_id": candidate.session_id,
            },
        )

    # ------------------------------------------------------------------
    # Consolidation: promote or evict
    # ------------------------------------------------------------------

    def consolidate(self, *, now: float | None = None) -> dict[str, Any]:
        """Run one consolidation cycle over the short-term buffer.

        Referenced/repeated entries are promoted to long-term memory; entries
        that expired (time-to-live) or overflowed the buffer capacity are
        evicted.  Nothing is ever physically deleted from long-term memory.

        This is the *buffer* half of memory maintenance: it only ever touches
        short-term proposals.  Long-term forgetting (freshness decay +
        archival) is the other half and lives in :meth:`apply_decay`, which
        reuses the same :meth:`_decay_score` -- there is exactly one forgetting
        implementation per storage tier, never two.

        Args:
            now: Optional clock override (tests).

        Returns:
            A summary dict with ``promoted`` / ``evicted`` id lists plus the
            resulting ``buffer`` and ``long_term`` counts.
        """
        moment = time.time() if now is None else float(now)
        with self._lock:
            promoted: list[str] = []
            evicted: list[str] = []
            remaining: list[MemoryEntry] = []

            for entry in list(self._buffer):
                if entry.decay.access_count >= PROMOTION_ACCESS_THRESHOLD:
                    self._promote(entry, moment, reason="referenced")
                    promoted.append(entry.id)
                elif (moment - entry.decay.last_access) > self._buffer_ttl:
                    evicted.append(entry.id)
                else:
                    remaining.append(entry)

            if len(remaining) > self._buffer_capacity:
                # Drop the least important first, oldest as the tie-break.
                remaining.sort(key=lambda e: (e.importance, e.decay.last_access))
                overflow = len(remaining) - self._buffer_capacity
                evicted.extend(entry.id for entry in remaining[:overflow])
                remaining = remaining[overflow:]

            self._buffer = remaining
            self._persist_buffer()
            return {
                "promoted": promoted,
                "evicted": evicted,
                "buffer": len(self._buffer),
                "long_term": sum(len(entries) for entries in self._long_term.values()),
            }

    def _merge(self, target: MemoryEntry, incoming: MemoryEntry, now: float) -> None:
        """Fold ``incoming`` into ``target`` (dedupe): bump, keep first time."""
        target.importance = min(
            1.0, max(target.importance, incoming.importance) + IMPORTANCE_BUMP
        )
        target.decay.access_count += 1
        target.decay.last_access = now
        target.decay.decay_score = self._decay_score(target, now)
        target.version += 1
        target.created_at = min(target.created_at, incoming.created_at)
        for ref in incoming.source_refs:
            if ref not in target.source_refs:
                target.source_refs.append(ref)

    def _promote(self, entry: MemoryEntry, now: float, *, reason: str) -> None:
        """Move a buffered entry into its long-term layer."""
        self._buffer = [buffered for buffered in self._buffer if buffered.id != entry.id]
        entry.decay.last_access = now
        entry.decay.decay_score = self._decay_score(entry, now)
        entry.version += 1
        entry.metadata["promoted_by"] = reason
        entry.metadata["promoted_at"] = now
        try:
            layer = self._coerce_type(entry.type)
        except ValueError:
            layer = MemoryType.EPISODIC
            entry.type = layer.value
        self._long_term[layer.value].append(entry)
        self._persist_entry(entry)
        self._persist_buffer()

    def _decay_score(self, entry: MemoryEntry, now: float) -> float:
        """Cached freshness score: ``importance * 0.5 ** (age / half_life)``.

        Age is measured from the last access/reference; an entry that has never
        been accessed falls back to its creation time so freshness is always
        defined.
        """
        reference = entry.decay.last_access or entry.created_at
        age = max(0.0, now - reference)
        freshness = 0.5 ** (age / DECAY_HALF_LIFE_SECONDS)
        return round(float(entry.importance) * freshness, 6)

    # ------------------------------------------------------------------
    # Reference / retrieval signals
    # ------------------------------------------------------------------

    def link(
        self,
        source_id: str,
        target_id: str,
        relation: Any = LinkRelation.REFERENCE.value,
        *,
        weight: float = 1.0,
    ) -> MemoryEntry | None:
        """Add a typed edge ``source -> target``; referencing promotes target.

        Args:
            source_id: Id of the entry the edge starts from (long-term or
                buffered).
            target_id: Id of the referenced entry.
            relation: A :class:`LinkRelation` (or free-form string).
            weight: Edge strength.

        Returns:
            The source entry, or ``None`` when the source does not exist.
        """
        with self._lock:
            source = self._find_by_id(source_id)
            if source is None:
                return None
            relation_value = relation.value if isinstance(relation, Enum) else str(relation)
            if not any(
                edge.target_id == target_id and edge.relation == relation_value
                for edge in source.links
            ):
                source.links.append(MemoryLink(
                    target_id=target_id, relation=relation_value, weight=weight,
                ))
                source.version += 1
                self._persist_source(source)

            target = self._find_by_id(target_id)
            if target is not None and self._is_buffered(target.id):
                # Being referenced is a promotion trigger.
                target.decay.access_count += 1
                self._promote(target, time.time(), reason="linked")
            return source

    def record_access(self, entry_id: str, *, now: float | None = None) -> MemoryEntry | None:
        """Record a retrieval hit on an entry; a buffered hit is promoted.

        Args:
            entry_id: The accessed entry id.
            now: Optional clock override (tests).

        Returns:
            The accessed entry, or ``None`` when it does not exist.
        """
        with self._lock:
            entry = self._find_by_id(entry_id)
            if entry is None:
                return None
            moment = time.time() if now is None else float(now)
            entry.decay.access_count += 1
            entry.decay.last_access = moment
            entry.decay.decay_score = self._decay_score(entry, moment)
            if self._is_buffered(entry_id):
                self._promote(entry, moment, reason="retrieved")
            else:
                entry.version += 1
                self._persist_entry(entry)
            return entry

    def _persist_source(self, entry: MemoryEntry) -> None:
        """Persist an entry back to whichever layer currently holds it."""
        if self._is_buffered(entry.id):
            self._persist_buffer()
        else:
            self._persist_entry(entry)

    # ------------------------------------------------------------------
    # Queries (for retrieval, Task 5)
    # ------------------------------------------------------------------

    def long_term(self) -> list[MemoryEntry]:
        """All long-term memories across the four durable layers."""
        with self._lock:
            return [
                entry
                for memory_type in self.LONG_TERM_TYPES
                for entry in self._long_term[memory_type.value]
            ]

    def by_type(self, memory_type: Any) -> list[MemoryEntry]:
        """Long-term memories of one type (accepts a :class:`MemoryType`)."""
        with self._lock:
            return list(self._long_term.get(self._coerce_type(memory_type).value, []))

    def get(self, entry_id: str) -> MemoryEntry | None:
        """Return a long-term memory by id, or ``None``."""
        with self._lock:
            for entries in self._long_term.values():
                for entry in entries:
                    if entry.id == entry_id:
                        return entry
            return None

    def buffer(self) -> list[MemoryEntry]:
        """A copy of the current short-term buffer."""
        with self._lock:
            return list(self._buffer)

    def _find_in_long_term(self, entry: MemoryEntry) -> MemoryEntry | None:
        """Return a long-term duplicate of ``entry`` across all layers."""
        for memory_type in self.LONG_TERM_TYPES:
            match = self._consolidator.find_entry_match(
                entry, self._long_term[memory_type.value]
            )
            if match is not None:
                return match
        return None

    def _find_by_id(self, entry_id: str) -> MemoryEntry | None:
        """Find an entry by id in the buffer or in long-term memory."""
        for entry in self._buffer:
            if entry.id == entry_id:
                return entry
        return self.get(entry_id)

    def _is_buffered(self, entry_id: str) -> bool:
        """Whether an entry with ``entry_id`` is currently buffered."""
        return any(entry.id == entry_id for entry in self._buffer)

    # ------------------------------------------------------------------
    # Forgetting (archival semantics; no physical deletion, ever)
    # ------------------------------------------------------------------

    def apply_decay(
        self,
        *,
        now: float | None = None,
        threshold: float = ARCHIVE_DECAY_THRESHOLD,
    ) -> dict[str, Any]:
        """Recompute long-term freshness and archive what has faded.

        This is the forgetting half of Task 5, and it deliberately reuses the
        one :meth:`_decay_score` the rest of the store already uses: freshness
        is ``importance * 0.5 ** (age / half_life)``, so a memory that is never
        retrieved or referenced loses score over time while important memories
        fade far more slowly.  Entries whose score drops below ``threshold``
        are marked :attr:`DecayState.archived` -- they vanish from recall but
        remain fully auditable.

        Nothing is ever removed: the updated entries are appended as a new
        revision to their layer file (last record wins on reload), so the audit
        trail and the original records survive.

        Args:
            now: Optional clock override (tests).
            threshold: Decay score below which an entry is archived.

        Returns:
            A summary dict with ``scanned`` / ``archived`` (newly archived ids)
            / ``already_archived`` / ``long_term`` counts.
        """
        moment = time.time() if now is None else float(now)
        with self._lock:
            newly_archived: list[str] = []
            already_archived = 0
            scanned = 0
            for memory_type in self.LONG_TERM_TYPES:
                for entry in self._long_term[memory_type.value]:
                    scanned += 1
                    score = self._decay_score(entry, moment)
                    changed = abs(score - entry.decay.decay_score) > 1e-9
                    entry.decay.decay_score = score
                    if entry.decay.archived:
                        already_archived += 1
                    elif score < threshold:
                        entry.decay.archived = True
                        newly_archived.append(entry.id)
                        changed = True
                    if changed:
                        entry.version += 1
                        self._persist_entry(entry)
            return {
                "scanned": scanned,
                "archived": newly_archived,
                "already_archived": already_archived,
                "long_term": sum(len(entries) for entries in self._long_term.values()),
            }

    def archived(self) -> list[MemoryEntry]:
        """Audit view: every archived long-term memory.

        Archived memories are hidden from recall by default but never deleted,
        so this is the explicit surface that reads them back for inspection.
        """
        with self._lock:
            return [
                entry
                for memory_type in self.LONG_TERM_TYPES
                for entry in self._long_term[memory_type.value]
                if entry.decay.archived
            ]

    # ------------------------------------------------------------------
    # Direct writes and working memory (the working layer's view)
    # ------------------------------------------------------------------

    def store(self, entry: MemoryEntry) -> MemoryEntry:
        """Persist a fully-formed entry into its layer, without filtering.

        This is the direct-write surface for callers that already hold a
        :class:`MemoryEntry` (e.g. the skill verifier promoting a procedure, or
        a working-memory snapshot).  Candidates from the significance judge go
        through :meth:`intake` instead.

        Args:
            entry: The entry to persist.

        Returns:
            The persisted entry.
        """
        with self._lock:
            if entry.type == MemoryType.WORKING.value:
                write_json(self._working_path, entry.to_dict())
                self._working_entry = entry
                return entry
            layer = self._coerce_type(entry.type)
            self._long_term[layer.value].append(entry)
            self._persist_entry(entry)
            return entry

    def save_working(self, working: WorkingMemory) -> MemoryEntry:
        """Persist a :class:`WorkingMemory` snapshot through the shared schema.

        The working layer is *not* a second store: this simply wraps the
        canonical scratchpad in a ``working``-typed :class:`MemoryEntry`.
        """
        with self._lock:
            now = time.time()
            entry = MemoryEntry(
                content=working.summarize(),
                type=MemoryType.WORKING.value,
                created_at=now,
                importance=0.5,
                source="working_memory",
                decay=DecayState(last_access=now, access_count=1),
                metadata={"working": working.to_dict()},
            )
            self._working_entry = entry
            write_json(self._working_path, entry.to_dict())
            return entry

    def load_working(self) -> WorkingMemory:
        """Rebuild the persisted :class:`WorkingMemory` (empty when absent)."""
        with self._lock:
            if self._working_entry is None:
                return WorkingMemory()
            payload = self._working_entry.metadata.get("working")
            if isinstance(payload, dict):
                return WorkingMemory.from_dict(payload)
            return WorkingMemory()

    # ------------------------------------------------------------------
    # ``.md`` memory-file ingestion (the single store's file front door)
    # ------------------------------------------------------------------

    def ingest_memory_dir(self, directory: str | Path | None = None) -> int:
        """Ingest every memory ``.md`` file of ``directory`` as an entry.

        The memory directory stays human-editable: this reads it through the
        existing file facade (:class:`~encre.memdir.system.EncreMemorySystem`),
        so the skip rules (``MEMORY.md`` entrypoint, dotfiles, underscore-
        prefixed internals, non-markdown) and the front-matter parser are the
        ones the facade already owns -- there is no second parser here.  The
        facade's header also supplies the front-matter ``description`` /
        ``type`` / ``tags``, which are mapped onto the unified entry schema.

        Each file becomes one :class:`MemoryEntry` whose ``source`` is
        ``md:<filename>``: filename-only, so the identity is stable across
        machines (the full path lives in ``metadata`` for provenance).  The
        entry's ``id`` is derived from that same source, so re-ingesting after
        a wiped store re-creates the *same* entry rather than a new one.

        Ingestion is idempotent.  An entry whose stored content is unchanged
        (same sha256, kept in ``metadata``) is left completely alone -- no
        record is appended and no counter moves.  A file whose content changed
        appends a **new revision of the same entry**, following the store's
        established append-only, last-record-wins pattern (:meth:`apply_decay`
        / :meth:`_promote`): the live entry is updated in place and re-persisted,
        never duplicated.  The entry keeps the layer it was first classified
        into, exactly as every other revision path in this store does.

        Args:
            directory: Directory to scan; defaults to the store's own root.

        Returns:
            The number of entries created or updated (unchanged files excluded).
        """
        from encre.memdir.system import EncreMemorySystem

        target = Path(directory) if directory is not None else self._root
        if not target.is_dir():
            return 0

        facade = EncreMemorySystem(str(target))
        changed = 0
        with self._lock:
            for header in facade.scan():
                content = self._memory_file_content(header.file_path, header.description)
                if not content:
                    continue
                source = f"md:{header.filename}"
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                metadata = {
                    "description": header.description or "",
                    "tags": list(header.tags),
                    "path": header.file_path,
                    INGEST_CONTENT_HASH_KEY: digest,
                }

                existing = self._find_by_source(source)
                if existing is not None:
                    if (
                        existing.content == content
                        and existing.metadata.get(INGEST_CONTENT_HASH_KEY) == digest
                    ):
                        continue
                    existing.content = content
                    existing.importance = max(
                        existing.importance, INGESTED_MEMORY_IMPORTANCE
                    )
                    existing.metadata.update(metadata)
                    existing.version += 1
                    self._persist_source(existing)
                    changed += 1
                    continue

                self.store(MemoryEntry(
                    id=self._ingest_entry_id(source),
                    content=content,
                    type=MemoryType.from_suggestion(header.memory_type).value,
                    importance=INGESTED_MEMORY_IMPORTANCE,
                    source=source,
                    metadata=metadata,
                ))
                changed += 1
        return changed

    @staticmethod
    def _ingest_entry_id(source: str) -> str:
        """Deterministic entry id for an ingested file, derived from ``source``."""
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _memory_file_content(file_path: str, description: str | None) -> str:
        """Return the retrievable content of one memory file.

        The file is read through :mod:`encre.secure_io`, so an encrypted memory
        file is decrypted transparently while legacy plaintext is kept as-is.
        The front-matter block is stripped (it is metadata, not memory) and the
        human-written ``description`` is prepended: :class:`RecallEngine` scores
        :attr:`MemoryEntry.content` alone, so folding the summary into the
        content is what makes a file findable by its own summary.
        """
        text = read_text(file_path)
        if not text.strip():
            return ""
        body = _FRONTMATTER_BLOCK_RE.sub("", text, count=1).strip()
        summary = str(description or "").strip()
        if summary and summary not in body:
            return f"{summary}\n\n{body}".strip()
        return body

    def _find_by_source(self, source: str) -> MemoryEntry | None:
        """Find an entry by its provenance ``source`` (long-term or buffered)."""
        for entries in self._long_term.values():
            for entry in entries:
                if entry.source == source:
                    return entry
        for entry in self._buffer:
            if entry.source == source:
                return entry
        return None

    # ------------------------------------------------------------------
    # Event backbone integration
    # ------------------------------------------------------------------

    def attach(self, bus: Any) -> None:
        """Subscribe to ``memory_candidate`` events and intake them.

        Idempotent per bus instance, and defensive: a malformed payload or a
        failing intake is logged, never raised into the publisher.

        Args:
            bus: The event backbone (``subscribe(kind, handler)`` semantics).
        """
        if bus is None or id(bus) in self._attached:
            return
        self._attached.add(id(bus))
        try:
            bus.subscribe("memory_candidate", self._on_candidate_event)
        except Exception:
            logger.warning("[memdir.layers] failed to subscribe to bus", exc_info=True)

    def _on_candidate_event(self, event: Any) -> None:
        """Bus observer: convert a ``memory_candidate`` event into an intake."""
        try:
            payload = getattr(event, "payload", None)
            if not isinstance(payload, dict):
                return
            self.intake(MemoryCandidate.from_dict(payload))
        except Exception:
            logger.warning("[memdir.layers] candidate intake failed", exc_info=True)
