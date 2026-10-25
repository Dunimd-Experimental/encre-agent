#!/usr/bin/env python3

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

The graph node kinds (``working`` / ``fact`` / ``preference`` / ``decision`` /
``lesson`` / ``entity`` / ``episode``) all share one :class:`MemoryEntry`
schema, so retrieval (Task 5) and the skill-promotion bridge (Task 7.4) speak
a single language.  Nodes carry a ``confidence`` (belief strength, moved by
outcome feedback) and a ``superseded_by`` tombstone pointer (contradiction
arbitration never deletes history, it supersedes it); typed edges
(:class:`LinkRelation`: causes / contradicts / refines / supersedes /
supports / mentions / co_occurs) make the store a genuine cross-linked
memory graph rather than a pile of records.

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
from typing import Any, ClassVar

from encre.logging_config import get_logger
from encre.memdir.judgment import MemoryCandidate
from encre.memdir.semantic import MemoryConsolidator, WorkingMemory
from encre.paths import get_data_dir
from encre.secure_io import (
    append_jsonl,
    read_json,
    read_jsonl,
    read_text,
    write_json,
    write_text,
)

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
#: Recurrence is evidence: every independent repeat sighting confirms the
#: node's trustworthiness a little (capped, see ``MemoryLayerStore._merge``).
CONFIDENCE_BUMP = 0.05

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
    """The graph node kinds, on one shared schema.

    ``working`` is the transient reasoning scratchpad; the others are the
    durable graph nodes.  The string values match the ``suggested_type``
    produced by the significance judge, and legacy names from earlier store
    revisions (``episodic`` / ``semantic`` / ``procedural`` /
    ``autobiographical``) are canonicalised on load -- one taxonomy, never two.

    ``commitment`` carries a deadline in its metadata (``deadline_ts``) and is
    the node kind the resident watcher wakes on (see
    :func:`iter_due_commitments`).
    """

    WORKING = "working"
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    LESSON = "lesson"
    ENTITY = "entity"
    EPISODE = "episode"
    COMMITMENT = "commitment"

    #: Legacy kind -> canonical kind.  Applied on every load and write so
    #: the migration is a property of the schema, not a one-off script.
    #: The map itself lives at module level (``_MEMORY_TYPE_LEGACY``) because
    #: enum class attributes must not be resolved through the enum metaclass.

    @classmethod
    def canonical(cls, value: Any) -> MemoryType:
        """Canonicalise a type value (legacy names remapped, unknown kept)."""
        if isinstance(value, cls):
            return value
        text = str(value).strip().lower()
        try:
            return cls(text)
        except ValueError:
            return _MEMORY_TYPE_LEGACY.get(text, cls.EPISODE)

    @classmethod
    def from_suggestion(cls, value: Any) -> MemoryType:
        """Map a judge ``suggested_type`` (or any string) to a memory type.

        Unknown values fall back to ``EPISODE`` (a lived event), never raise:
        the judge is the gate, this is only a classification hint.
        """
        return cls.canonical(value)


class LinkRelation(str, Enum):
    """Typed edges between graph nodes (used by associative recall)."""

    CAUSES = "causes"
    CONTRADICTS = "contradicts"
    REFINES = "refines"
    SUPERSEDES = "supersedes"
    SUPPORTS = "supports"
    MENTIONS = "mentions"
    CO_OCCURS = "co_occurs"

    #: Legacy relation -> canonical relation.  Normalised on every load and
    #: write so old edges read as their new semantic counterparts.
    #: The map itself lives at module level (``_LINK_RELATION_LEGACY``).

    @classmethod
    def canonical(cls, value: Any) -> LinkRelation | None:
        """Canonicalise a relation value; ``None`` when it is not one."""
        if isinstance(value, cls):
            return value
        text = str(value).strip().lower()
        try:
            return cls(text)
        except ValueError:
            return _LINK_RELATION_LEGACY.get(text)


#: Module-level legacy maps: enum class bodies must not carry plain dicts
#: because the enum metaclass resolves class attributes before instance
#: attribute lookup, which breaks ``cls._LEGACY``-style access on members.
_MEMORY_TYPE_LEGACY: dict[str, MemoryType] = {
    "episodic": MemoryType.EPISODE,
    "semantic": MemoryType.FACT,
    "procedural": MemoryType.LESSON,
    "autobiographical": MemoryType.EPISODE,
}

_LINK_RELATION_LEGACY: dict[str, LinkRelation] = {
    "causal": LinkRelation.CAUSES,
    "dependency": LinkRelation.SUPPORTS,
    "same_goal": LinkRelation.CO_OCCURS,
    "reference": LinkRelation.MENTIONS,
}


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
    def from_dict(cls, data: dict[str, Any] | None) -> DecayState:
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
        relation: One of :class:`LinkRelation` (``causes`` / ``contradicts``
            / ``refines`` / ``supersedes`` / ``supports`` / ``mentions`` /
            ``co_occurs``); free-form strings are tolerated.
        weight: Edge strength in ``[0.0, 1.0]`` (used by spreading activation).
        created_at: Wall-clock time the edge was created.
    """

    target_id: str
    relation: str = LinkRelation.MENTIONS.value
    weight: float = 1.0
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if isinstance(self.relation, Enum):
            self.relation = str(self.relation.value)
        else:
            self.relation = str(self.relation)
        # Canonicalise legacy relations at the schema boundary: every edge
        # that enters the store -- whatever its source -- reads as its new
        # semantic counterpart, so old and new edges are one graph.
        canonical = LinkRelation.canonical(self.relation)
        self.relation = canonical.value if canonical is not None else self.relation
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
    def from_dict(cls, data: dict[str, Any]) -> MemoryLink:
        """Build a :class:`MemoryLink` from a dict (see :meth:`to_dict`)."""
        return cls(
            target_id=str(data.get("target_id", "")),
            relation=data.get("relation", LinkRelation.MENTIONS.value),
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
        confidence: Belief strength in ``[0.0, 1.0]``; raised by supportive
            outcomes and lowered by failures or contradictions.  Unlike
            ``importance`` it answers "how much do we believe this", not
            "how much does this matter".
        superseded_by: Id of the node that replaced this one (tombstone).
            Non-empty means the node lost a contradiction arbitration: it is
            invisible to recall but never physically deleted.
        links: Explicit typed edges to other memories.
        decay: Freshness/usage state (see :class:`DecayState`).
        version: Monotonic revision counter (bumped on every merge/promotion).
        source: Where the entry came from (``significance_judgment``,
            ``working_memory``, ...).
        source_refs: Provenance references (event ids, tool call ids, ...).
        detail_ref: Pointer into the verbatim detail layer, shaped as
            ``details/<type>.md#<id>``.  The node's ``content`` may be a
            truncated summary used for lexical math; the detail file carries
            the complete statement.  Empty means the node *is* the whole
            memory (legacy entries before the detail layer existed).
        metadata: Free-form extras (judge reasons, promotion reason, ...).
    """

    content: str
    type: str = MemoryType.EPISODE.value
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    importance: float = 0.5
    confidence: float = 0.5
    superseded_by: str = ""
    links: list[MemoryLink] = field(default_factory=list)
    decay: DecayState = field(default_factory=DecayState)
    version: int = 1
    source: str = ""
    source_refs: list[str] = field(default_factory=list)
    detail_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalise loosely-typed inputs so the entry is always JSON-clean."""
        self.type = MemoryType.canonical(self.type).value
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
        self.confidence = min(1.0, max(0.0, _as_float(self.confidence, 0.5)))
        self.superseded_by = str(self.superseded_by or "")
        self.version = _as_int(self.version, 1)
        self.source = str(self.source)
        self.source_refs = [str(r) for r in (self.source_refs or [])]
        self.detail_ref = str(self.detail_ref or "")
        self.metadata = dict(self.metadata or {})

    @property
    def memory_type(self) -> MemoryType:
        """The entry's layer as a :class:`MemoryType` (canonicalised)."""
        return MemoryType.canonical(self.type)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        return {
            "id": self.id,
            "type": self.type,
            "content": self.content,
            "created_at": self.created_at,
            "importance": self.importance,
            "confidence": self.confidence,
            "superseded_by": self.superseded_by,
            "links": [link.to_dict() for link in self.links],
            "decay": self.decay.to_dict(),
            "version": self.version,
            "source": self.source,
            "source_refs": list(self.source_refs),
            "detail_ref": self.detail_ref,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        """Build a :class:`MemoryEntry` from a dict (see :meth:`to_dict`)."""
        return cls(
            content=str(data.get("content", "")),
            type=data.get("type", MemoryType.EPISODE.value),
            id=str(data.get("id", "") or uuid.uuid4().hex),
            created_at=_as_float(data.get("created_at"), time.time()),
            importance=_as_float(data.get("importance"), 0.5),
            confidence=_as_float(data.get("confidence"), 0.5),
            superseded_by=str(data.get("superseded_by", "") or ""),
            links=list(data.get("links") or []),
            decay=data.get("decay") or DecayState(),
            version=_as_int(data.get("version"), 1),
            source=str(data.get("source", "")),
            source_refs=[str(r) for r in (data.get("source_refs") or [])],
            detail_ref=str(data.get("detail_ref", "") or ""),
            metadata=dict(data.get("metadata", {}) or {}),
        )


#: ---------------------------------------------------------------------
#: Commitment nodes (``MemoryType.COMMITMENT``) -- deadline metadata keys
#: and the pure-math notification schedule.  All arithmetic lives here so
#: the resident watcher only consumes decisions, never computes them.
#: ---------------------------------------------------------------------

#: Epoch-seconds deadline.  The only required commitment metadata key.
COMMITMENT_DEADLINE_KEY = "deadline_ts"
#: Lifecycle status: ``active`` until the user (or the agent) closes it out,
#: ``done`` / ``expired`` are terminal.  Repetition is tracked by the stage
#: counter, not by flipping the status back and forth.
COMMITMENT_STATUS_KEY = "commitment_status"
COMMITMENT_STATUS_ACTIVE = "active"
COMMITMENT_STATUS_DONE = "done"
COMMITMENT_STATUS_EXPIRED = "expired"
#: How many notifications have already fired (0 = none yet).
COMMITMENT_STAGE_KEY = "notify_stage"
#: Warn this many seconds before the deadline (notification #1).
COMMITMENT_REMIND_BEFORE_KEY = "remind_before"
#: Re-remind every this many seconds once the deadline has passed.
COMMITMENT_REMIND_REPEAT_KEY = "remind_repeat"
#: Total notification budget (default: one pre-deadline + one at deadline).
COMMITMENT_MAX_REMINDS_KEY = "max_reminds"

COMMITMENT_REMIND_BEFORE_DEFAULT = 86400.0
COMMITMENT_REMIND_REPEAT_DEFAULT = 86400.0
COMMITMENT_MAX_REMINDS_DEFAULT = 2

#: Terminal statuses -- a closed commitment never wakes again.
_TERMINAL_COMMITMENT_STATUSES = frozenset({COMMITMENT_STATUS_DONE, COMMITMENT_STATUS_EXPIRED})


def _commitment_fire_time(entry: MemoryEntry, stage: int) -> float | None:
    """Epoch time of the ``stage``-th notification (1-based); ``None`` if malformed.

    Notification #1 fires ``remind_before`` seconds ahead of the deadline;
    every later notification fires ``remind_repeat`` seconds after the
    previous one, anchored at the deadline.  Pure arithmetic on metadata.
    """
    meta = entry.metadata or {}
    deadline = _as_float(meta.get(COMMITMENT_DEADLINE_KEY), -1.0)
    if deadline <= 0.0:
        return None
    remind_before = max(0.0, _as_float(meta.get(COMMITMENT_REMIND_BEFORE_KEY), COMMITMENT_REMIND_BEFORE_DEFAULT))
    remind_repeat = max(0.0, _as_float(meta.get(COMMITMENT_REMIND_REPEAT_KEY), COMMITMENT_REMIND_REPEAT_DEFAULT))
    if stage <= 1:
        return deadline - remind_before
    return deadline + (stage - 2) * remind_repeat


def iter_due_commitments(
    entries: list[MemoryEntry] | tuple[MemoryEntry, ...],
    now: float,
) -> tuple[list[tuple[MemoryEntry, int]], list[str]]:
    """Split commitment entries into due-for-notification and expired.

    The single source of truth for "does this commitment need a wake right
    now" -- the watcher calls this, it never re-derives the schedule.

    Args:
        entries: Candidate memory entries (any types; non-commitments and
            superseded nodes are skipped).
        now: Current epoch seconds.

    Returns:
        ``(due, expired)`` where ``due`` is a list of ``(entry, next_stage)``
        pairs whose next notification time has arrived, and ``expired`` lists
        the ids of commitments whose notification budget is exhausted and
        whose last fire time has passed (candidates for terminal marking).
    """
    due: list[tuple[MemoryEntry, int]] = []
    expired: list[str] = []
    for entry in entries:
        if entry.memory_type is not MemoryType.COMMITMENT:
            continue
        if entry.superseded_by:
            continue
        meta = entry.metadata or {}
        status = str(meta.get(COMMITMENT_STATUS_KEY) or COMMITMENT_STATUS_ACTIVE)
        if status in _TERMINAL_COMMITMENT_STATUSES:
            continue
        stage = max(0, _as_int(meta.get(COMMITMENT_STAGE_KEY), 0))
        max_reminds = max(1, _as_int(meta.get(COMMITMENT_MAX_REMINDS_KEY), COMMITMENT_MAX_REMINDS_DEFAULT))
        last_fire = _commitment_fire_time(entry, max_reminds)
        if last_fire is None:
            continue
        next_stage = stage + 1
        if next_stage > max_reminds:
            if now >= last_fire:
                expired.append(entry.id)
            continue
        fire = _commitment_fire_time(entry, next_stage)
        if fire is not None and now >= fire:
            due.append((entry, next_stage))
    return due, expired


class MemoryLayerStore:
    """Typed graph memory store with a short-term buffer and consolidation.

    Candidates enter through :meth:`intake` (short-term buffer).  They become
    long-term graph nodes only when repeated or referenced (:meth:`link` /
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

    #: Durable graph node kinds (``working`` is handled separately as a
    #: snapshot).
    LONG_TERM_TYPES = (
        MemoryType.FACT,
        MemoryType.PREFERENCE,
        MemoryType.DECISION,
        MemoryType.LESSON,
        MemoryType.ENTITY,
        MemoryType.EPISODE,
        MemoryType.COMMITMENT,
    )

    #: Legacy per-kind layer files migrated physically on first load.
    LEGACY_TYPE_FILES: ClassVar[dict[str, MemoryType]] = {
        "episodic": MemoryType.EPISODE,
        "semantic": MemoryType.FACT,
        "procedural": MemoryType.LESSON,
        "autobiographical": MemoryType.EPISODE,
    }

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
        # Verbatim detail layer (hippocampal-trace analog): the graph nodes
        # carry summaries for lexical math; this directory carries the full
        # statements as human-editable markdown, one file per type.
        self._details_dir = self._root / "details"
        self._consolidator = consolidator or MemoryConsolidator(str(self._layers_dir))
        self._buffer_capacity = max(1, int(buffer_capacity))
        self._buffer_ttl = float(buffer_ttl)
        self._lock = threading.RLock()
        self._buffer: list[MemoryEntry] = []
        self._long_term: dict[str, list[MemoryEntry]] = {memory_type.value: [] for memory_type in self.LONG_TERM_TYPES}
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

    def _detail_path(self, memory_type: Any) -> Path:
        """Path of the detail markdown file backing one durable layer."""
        return self._details_dir / f"{self._coerce_type(memory_type).value}.md"

    # ------------------------------------------------------------------
    # Verbatim detail layer (markdown, per type, sectioned by entry id)
    # ------------------------------------------------------------------

    @staticmethod
    def _split_sections(document: str) -> dict[str, str]:
        """Split a detail file into ``{anchor: body}`` markdown sections.

        A section starts at a ``## <anchor>`` header and runs to the next
        header.  Content before the first header is ignored, so the files
        stay round-trippable and forward-compatible.
        """
        sections: dict[str, str] = {}
        anchor = ""
        body: list[str] = []
        for line in document.split("\n"):
            if line.startswith("## "):
                if anchor:
                    sections[anchor] = "\n".join(body).strip()
                anchor = line[3:].strip()
                body = []
            elif anchor:
                body.append(line)
        if anchor:
            sections[anchor] = "\n".join(body).strip()
        return sections

    def _sync_detail(self, entry: MemoryEntry) -> None:
        """Persist the entry's verbatim detail text and stamp ``detail_ref``.

        The full statement rides in from the judge via the candidate's
        ``full_content`` metadata when the node summary was truncated;
        otherwise the node content *is* the detail.  The payload is popped
        from the node metadata once materialised, so the graph never stores
        the verbatim text twice.  A re-observation refreshes the section
        body in place -- the append-only ledger keeps the revision history,
        the detail file keeps the current truth.

        Args:
            entry: The entry whose detail should be materialised.  Its
                ``detail_ref`` is stamped on success.
        """
        full = str((entry.metadata or {}).pop("full_content", "") or "")
        if not full.strip():
            full = entry.content
        full = full.strip()
        if not full:
            return
        path = self._detail_path(entry.type)
        document = read_text(path)
        sections = self._split_sections(document)
        sections[entry.id] = full
        rebuilt = "\n\n".join(f"## {anchor}\n\n{body}" for anchor, body in sections.items())
        write_text(path, rebuilt)
        entry.detail_ref = f"details/{path.name}#{entry.id}"

    def load_detail(self, entry: MemoryEntry) -> str:
        """Load the verbatim detail text for ``entry``.

        This is the read half of the two-layer design: the graph decides
        *which* memory is relevant (fast, statistical), this method returns
        *the whole memory* (verbatim, human-readable).

        The return is the longest of the three verbatim sources, so a
        polluted or truncated detail section can never make the caller see
        less than what is stored elsewhere: (1) the entry's detail section,
        (2) the ingested source file for ``md:`` nodes (read through the
        secure-io facade, so encrypted files decrypt transparently), (3) the
        node content itself.  This also repairs legacy nodes whose detail
        section was clobbered by a merge before the monotonic guard existed.

        Args:
            entry: The node whose detail to load.

        Returns:
            The full detail text, or the node content as the fallback.
        """
        anchor = str(entry.id)
        ref = entry.detail_ref or f"details/{entry.memory_type.value}.md"
        file_name = Path(ref.split("#", 1)[0]).name
        document = read_text(self._details_dir / file_name)
        body = ""
        if document:
            body = self._split_sections(document).get(anchor, "") or ""
        source = str(entry.source or "")
        if source.startswith("md:"):
            file_body = self._memory_file_content(str(self._root / source[3:]), None)
            if len(file_body) > len(body):
                body = file_body
        if len(entry.content or "") > len(body):
            body = str(entry.content)
        return body

    @staticmethod
    def _coerce_type(memory_type: Any) -> MemoryType:
        """Coerce a type value (or a legacy name) to the canonical enum."""
        return MemoryType.canonical(memory_type)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """(Re)load every layer from disk, replacing the in-memory state.

        Long-term files are append-only revision logs: when the same id appears
        several times the last record wins, so a merge/promotion never leaves a
        stale duplicate visible.  Legacy per-kind files from before the graph
        taxonomy are migrated physically on first load (:meth:
        `_migrate_legacy_files`), so exactly one set of layer files remains.
        """
        with self._lock:
            self._migrate_legacy_files()
            self._long_term = {memory_type.value: [] for memory_type in self.LONG_TERM_TYPES}
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

    def _migrate_legacy_files(self) -> None:
        """Physically migrate legacy per-kind layer files to the graph kinds.

        Reads each legacy file (``episodic`` / ``semantic`` / ``procedural`` /
        ``autobiographical``), appends every record -- re-typed by the schema
        canonicalisation in :class:`MemoryEntry` -- to its canonical layer
        file, then deletes the legacy file.  One-time, idempotent: after the
        first pass no legacy file exists, so every later load is a no-op.
        """
        for legacy_name, target_type in self.LEGACY_TYPE_FILES.items():
            legacy_path = self._layers_dir / f"{legacy_name}.jsonl"
            if not legacy_path.exists():
                continue
            try:
                records = read_jsonl(legacy_path)
            except Exception:
                logger.warning(
                    "[memdir.layers] failed to read legacy layer file %s",
                    legacy_path.name,
                    exc_info=True,
                )
                continue
            migrated = 0
            for record in records:
                if not isinstance(record, dict):
                    continue
                try:
                    entry = MemoryEntry.from_dict(record)
                except Exception:
                    logger.warning(
                        "[memdir.layers] skipping malformed legacy entry",
                        exc_info=True,
                    )
                    continue
                append_jsonl(self._type_path(target_type), entry.to_dict())
                migrated += 1
            try:
                legacy_path.unlink()
            except OSError:
                logger.warning(
                    "[memdir.layers] failed to remove legacy layer file %s",
                    legacy_path.name,
                    exc_info=True,
                )
            if migrated:
                logger.info(
                    "[memdir.layers] migrated %d entries from %s to %s",
                    migrated,
                    legacy_path.name,
                    target_type.value,
                )

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
                self._hand_over_detail(existing, entry)
                self._merge(existing, entry, now)
                self._sync_detail(existing)
                self._persist_entry(existing)
                return existing

            buffered = self._consolidator.find_entry_match(entry, self._buffer)
            if buffered is not None:
                self._hand_over_detail(buffered, entry)
                self._merge(buffered, entry, now)
                self._sync_detail(buffered)
                self._promote(buffered, now, reason="repeated")
                return buffered

            self._sync_detail(entry)
            self._buffer.append(entry)
            self._persist_buffer()
            return entry

    @staticmethod
    def _hand_over_detail(target: MemoryEntry, incoming: MemoryEntry) -> None:
        """Move an incoming re-observation's full detail onto ``target``.

        Merging keeps the target node (identity, confidence history); only
        the verbatim detail of the newest observation is fresher, so it is
        handed over before the incoming entry is discarded.
        """
        full = str((incoming.metadata or {}).pop("full_content", "") or "")
        if full.strip():
            target.metadata["full_content"] = full

    def _entry_from_candidate(self, candidate: MemoryCandidate, now: float) -> MemoryEntry:
        """Build a buffered :class:`MemoryEntry` from a judged candidate."""
        created_at = _as_float(candidate.created_at, now)
        metadata: dict[str, Any] = {
            "reasons": list(candidate.reasons),
            "session_id": candidate.session_id,
        }
        # The judge ships the verbatim statement alongside the truncated
        # node summary; carry it through so the detail layer receives it.
        full_content = str((candidate.metadata or {}).get("full_content") or "")
        if full_content.strip():
            metadata["full_content"] = full_content
        # Model-assisted tokenization channel: memory_create keywords ride
        # through to entry.metadata, where the curator collects them for
        # refresh_lexicon (which corpus-validates each term).
        raw_keywords = (candidate.metadata or {}).get("model_keywords")
        if isinstance(raw_keywords, list | tuple) and raw_keywords:
            keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]
            if keywords:
                metadata["model_keywords"] = keywords[:8]
        # Origin turn rides through so session rollback can purge this node.
        cand_turn = (candidate.metadata or {}).get("turn")
        if isinstance(cand_turn, int) and not isinstance(cand_turn, bool):
            metadata["turn"] = cand_turn
        return MemoryEntry(
            content=str(candidate.content or ""),
            type=MemoryType.from_suggestion(candidate.suggested_type).value,
            created_at=created_at,
            importance=_as_float(candidate.significance, 0.5),
            source="significance_judgment",
            source_refs=list(candidate.source_refs),
            decay=DecayState(last_access=created_at, access_count=1),
            metadata=metadata,
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
            self.export_markdown()
            return {
                "promoted": promoted,
                "evicted": evicted,
                "buffer": len(self._buffer),
                "long_term": sum(len(entries) for entries in self._long_term.values()),
            }

    def _merge(self, target: MemoryEntry, incoming: MemoryEntry, now: float) -> None:
        """Fold ``incoming`` into ``target`` (dedupe): bump stats, keep first
        timestamp, adopt the longer body.

        Re-observation is evidence, but it can also be newer.  The numeric
        bumps below are monotonic in the sense that access/importance
        only grow; the content, however, needs to move in the *other*
        direction too -- when the judge restates the memory with more
        detail (a fresh ``full_content`` payload, or a longer re-summary),
        the target's summary must follow, otherwise the node's visible
        title and preview are frozen at the state of its first sighting
        forever and the settings panel cannot reflect what the corpus
        actually says now.  Shorter incoming content is ignored so a
        tool-written one-liner merged onto a full-text node never shrinks
        the graph's representation of it.
        """
        target.importance = min(1.0, max(target.importance, incoming.importance) + IMPORTANCE_BUMP)
        target.confidence = min(0.95, target.confidence + CONFIDENCE_BUMP)
        target.decay.access_count += 1
        target.decay.last_access = now
        target.decay.decay_score = self._decay_score(target, now)
        target.version += 1
        target.created_at = min(target.created_at, incoming.created_at)
        for ref in incoming.source_refs:
            if ref not in target.source_refs:
                target.source_refs.append(ref)
        # Adopt the longer body: only a strictly longer observation rewrites
        # the node summary.  Equal length keeps the incumbent (stable titles
        # in the graph); shorter incoming is dropped, so a truncated merge
        # never truncates the target's already-longer text.
        incoming_content = str(incoming.content or "")
        if len(incoming_content) > len(str(target.content or "")):
            target.content = incoming_content
        # Same rule for the human-readable label: adopt the longer
        # description, and also stamp ``full_content`` into the target's
        # metadata so ``_sync_detail`` can materialise the verbatim section
        # on the next write.  Without this the merged node's detail panel
        # would still show only the original first-sighting text.
        incoming_meta = dict(incoming.metadata or {})
        target_meta = dict(target.metadata or {})
        incoming_desc = str(incoming_meta.get("description", "") or "")
        target_desc = str(target_meta.get("description", "") or "")
        if len(incoming_desc) > len(target_desc):
            target_meta["description"] = incoming_desc
        if "full_content" in incoming_meta:
            incoming_full = str(incoming_meta.get("full_content") or "")
            target_full = str(target_meta.get("full_content") or "")
            if len(incoming_full) > len(target_full):
                target_meta["full_content"] = incoming_full
        target.metadata = target_meta

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
            layer = MemoryType.EPISODE
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
        relation: Any = LinkRelation.MENTIONS.value,
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
            if not any(edge.target_id == target_id and edge.relation == relation_value for edge in source.links):
                source.links.append(
                    MemoryLink(
                        target_id=target_id,
                        relation=relation_value,
                        weight=weight,
                    )
                )
                source.version += 1
                self._persist_source(source)

            target = self._find_by_id(target_id)
            if target is not None and self._is_buffered(target.id):
                # Being referenced is a promotion trigger.
                target.decay.access_count += 1
                self._promote(target, time.time(), reason="linked")
            return source

    def unlink(
        self,
        source_id: str,
        target_id: str,
        relation: Any = LinkRelation.MENTIONS.value,
    ) -> bool:
        """Remove one typed edge ``source -> target``.

        Used by the curator's statistical passes when an edge falls below
        the quality threshold that defines the relation (e.g. a ``co_occurs``
        edge whose endpoint contents no longer share a subject).  Structural
        bookkeeping only: content, confidence and lineage are untouched.

        Args:
            source_id: Id of the entry the edge starts from.
            target_id: Id of the entry the edge points to.
            relation: The relation value to remove.

        Returns:
            ``True`` when an edge was found and removed, ``False`` otherwise.
        """
        with self._lock:
            source = self._find_by_id(source_id)
            if source is None:
                return False
            relation_value = relation.value if isinstance(relation, Enum) else str(relation)
            kept = [edge for edge in source.links if not (edge.target_id == target_id and edge.relation == relation_value)]
            if len(kept) == len(source.links):
                return False
            source.links = kept
            source.version += 1
            self._persist_source(source)
            return True

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

    def adjust_confidence(
        self,
        entry_id: str,
        delta: float,
        *,
        reason: str = "",
    ) -> MemoryEntry | None:
        """Move a node's belief strength by ``delta`` (outcome feedback).

        This is the evolution half of the memory graph: outcomes reinforce the
        nodes that participated in a success and weaken the ones implicated in
        a failure, so confidence tracks evidence rather than staying frozen.

        Args:
            entry_id: The node whose confidence moves.
            delta: Signed shift in ``[-1.0, 1.0]``; the result is clamped to
                ``[0.0, 1.0]``.
            reason: Free-form provenance recorded in ``metadata``.

        Returns:
            The updated entry, or ``None`` when it does not exist.
        """
        with self._lock:
            entry = self._find_by_id(entry_id)
            if entry is None:
                return None
            shift = max(-1.0, min(1.0, float(delta)))
            entry.confidence = min(1.0, max(0.0, entry.confidence + shift))
            entry.version += 1
            if reason:
                history = entry.metadata.setdefault("confidence_history", [])
                if isinstance(history, list):
                    history.append(
                        {
                            "delta": round(shift, 6),
                            "confidence": round(entry.confidence, 6),
                            "reason": str(reason),
                            "at": time.time(),
                        }
                    )
            self._persist_source(entry)
            return entry

    def supersede(self, old_id: str, new_id: str) -> bool:
        """Tombstone ``old_id`` in favour of ``new_id`` (contradiction win).

        The loser keeps its records (append-only audit trail) but gains a
        ``superseded_by`` pointer that hides it from recall, and the winner
        gains a ``supersedes`` edge back to it so the lineage stays traversable.

        Hardening: a tombstone is only ever written toward a live successor.
        When ``new_id`` does not resolve to a stored entry (or the successor
        is archived), the tombstone is refused -- hiding the loser without a
        live replacement would orphan the only copy of the content.  A
        winner still sitting in the short-term buffer is promoted to
        long-term first, so a later buffer eviction can never leave the
        tombstone pointing at a vanished node.

        Args:
            old_id: Id of the node that lost the arbitration.
            new_id: Id of the node that replaces it.

        Returns:
            ``True`` when the old node was found and tombstoned.
        """
        with self._lock:
            old = self._find_by_id(old_id)
            if old is None or not str(new_id):
                return False
            new = self._find_by_id(str(new_id))
            if new is None or new.decay.archived:
                return False
            if any(buffered.id == new.id for buffered in self._buffer):
                self._promote(new, time.time(), reason="supersede")
            old.superseded_by = str(new_id)
            old.version += 1
            old.metadata["superseded_at"] = time.time()
            self._persist_source(old)
            self.link(str(new_id), old_id, LinkRelation.SUPERSEDES.value)
            return True

    def _lineage_alive(
        self,
        successor_id: str,
        by_id: dict[str, MemoryEntry],
        seen: frozenset[str],
    ) -> bool:
        """Whether a tombstone's replacement chain still ends at a live node.

        Follows ``superseded_by`` pointers until a terminal node; the chain
        is alive only when it reaches a non-archived entry with no further
        replacement.  A dangling id, an archived endpoint or a cycle makes
        the lineage dead -- the tombstoned entry is then the last surviving
        copy and must become visible again.

        Args:
            successor_id: The ``superseded_by`` pointer to resolve.
            by_id: All persisted entries (long-term + buffer) keyed by id.
            seen: Ids already visited on this chain (cycle guard).

        Returns:
            ``True`` when the chain terminates at a live entry.
        """
        current = by_id.get(successor_id)
        if current is None or current.id in seen:
            return False
        if current.decay.archived:
            return False
        if not current.superseded_by:
            return True
        return self._lineage_alive(
            current.superseded_by, by_id, seen | {current.id}
        )

    def visible_entries(self, include_archived: bool = False) -> list[MemoryEntry]:
        """Long-term + buffer entries that recall and graph views may show.

        This is the single canonical visibility rule for every reader
        (recall engine, curator, settings panel, graph projection).  A
        tombstone hides a node only while its replacement lineage is alive;
        when ``superseded_by`` points at an id that no longer exists, or the
        chain dead-ends in an archived node, the entry is healed back into
        visibility -- an interrupted or partial arbitration must never make
        stored content vanish from retrieval.

        Args:
            include_archived: When ``True``, archived entries are also
                returned (the audit view).

        Returns:
            The visible entries: long-term first, then buffered.
        """
        entries = self.long_term() + self.buffer()
        by_id = {entry.id: entry for entry in entries}
        return [
            entry for entry in entries
            if (include_archived or not entry.decay.archived)
            and (
                not entry.superseded_by
                or not self._lineage_alive(
                    entry.superseded_by, by_id, frozenset({entry.id})
                )
            )
        ]

    def archive_entry(self, entry_id: str, reason: str = "") -> bool:
        """Archive one entry by id (structural retirement, not deletion).

        The record stays in the append-only log; archiving only hides the
        node from recall and graph views.  Used for statistical index nodes
        (entity hubs) whose token no longer exists in the corpus lexicon --
        the index is derived state, so retiring it is bookkeeping, not
        information destruction.

        Args:
            entry_id: Id of the entry to archive.
            reason: Optional audit stamp written into metadata.

        Returns:
            ``True`` when the entry was found and archived.
        """
        with self._lock:
            entry = self._find_by_id(entry_id)
            if entry is None:
                return False
            entry.decay.archived = True
            entry.version += 1
            if reason:
                entry.metadata["archived_reason"] = reason
            self._persist_source(entry)
            return True

    def purge_session_entries(
        self,
        session_id: str,
        min_turn: int,
        *,
        inclusive: bool = False,
        created_after: float | None = None,
    ) -> int:
        """Retire memory nodes produced by turns after ``min_turn``.

        Rollback support (git semantics: a rollback rewinds to an old
        commit, so everything derived after that commit belongs to the
        discarded future): entries belonging to ``session_id`` are retired
        when EITHER their ``turn`` tag is strictly greater than ``min_turn``
        (or equal to it when ``inclusive``), OR -- for legacy nodes created
        before turn tagging existed -- ``created_after`` is given and the
        node was ingested after that timestamp (the target commit's time).
        Tagged entries take precedence: a tagged entry is never judged by
        the timestamp rule.

        Long-term entries are archived (the append-only audit trail is
        preserved); short-term buffer entries are dropped.  Entries with
        neither a turn tag nor a passing timestamp are never touched.

        Args:
            session_id: Session whose nodes are eligible.
            min_turn: Turn boundary; turns after it are purged.
            inclusive: Also purge entries tagged exactly ``min_turn``.
            created_after: Optional ingestion-time cutoff for untagged
                legacy nodes (pass the rollback target commit's timestamp).

        Returns:
            Number of entries retired (archived or buffer-dropped).
        """

        def _matches(entry: MemoryEntry) -> bool:
            meta = entry.metadata or {}
            if str(meta.get("session_id") or "") != str(session_id or ""):
                return False
            turn = meta.get("turn")
            if isinstance(turn, int) and not isinstance(turn, bool):
                return turn > min_turn or (inclusive and turn == min_turn)
            # Legacy node without a turn tag: fall back to the rollback
            # commit's timestamp -- anything ingested after that commit
            # was derived from the discarded turns.
            if created_after is not None:
                created = getattr(entry, "created_at", None)
                if isinstance(created, int | float) and not isinstance(created, bool) and float(created) > float(created_after):
                    return True
            return False

        purged = 0
        with self._lock:
            for entries in self._long_term.values():
                for entry in entries:
                    if entry.decay.archived or not _matches(entry):
                        continue
                    entry.decay.archived = True
                    entry.version += 1
                    entry.metadata["archived_reason"] = "session_rollback"
                    self._persist_entry(entry)
                    purged += 1
            kept = [entry for entry in self._buffer if not _matches(entry)]
            if len(kept) != len(self._buffer):
                purged += len(self._buffer) - len(kept)
                self._buffer = kept
                self._persist_buffer()
        return purged

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
            return [entry for memory_type in self.LONG_TERM_TYPES for entry in self._long_term[memory_type.value]]

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

    def update_metadata(self, entry_id: str, patch: dict[str, Any]) -> MemoryEntry | None:
        """Merge ``patch`` into an entry's metadata and persist the change.

        Long-term updates append to the revision log (last record wins on
        reload); short-term buffer updates rewrite the buffer snapshot. Both
        are safe for out-of-process writers such as the commitment watcher.
        """
        with self._lock:
            entry = self.get(entry_id)
            if entry is not None:
                entry.metadata.update(dict(patch))
                entry.version += 1
                self._persist_entry(entry)
                return entry
            for index, buffered in enumerate(self._buffer):
                if buffered.id == entry_id:
                    buffered.metadata.update(dict(patch))
                    buffered.version += 1
                    self._buffer[index] = buffered
                    self._persist_buffer()
                    return buffered
            return None

    def buffer(self) -> list[MemoryEntry]:
        """A copy of the current short-term buffer."""
        with self._lock:
            return list(self._buffer)

    def _find_in_long_term(self, entry: MemoryEntry) -> MemoryEntry | None:
        """Return a long-term duplicate of ``entry`` across all layers."""
        for memory_type in self.LONG_TERM_TYPES:
            match = self._consolidator.find_entry_match(entry, self._long_term[memory_type.value])
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
            return [entry for memory_type in self.LONG_TERM_TYPES for entry in self._long_term[memory_type.value] if entry.decay.archived]

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
            layer_list = self._long_term[layer.value]
            # An entry may already be resident (e.g. the curator re-writing a
            # stamped skill candidate): update in place, never duplicate.
            for index, resident in enumerate(layer_list):
                if resident.id == entry.id:
                    layer_list[index] = entry
                    break
            else:
                layer_list.append(entry)
            # Uniform detail-layer invariant: every durable node gets a
            # verbatim section (for direct writes the node content *is* the
            # detail), so `load_detail` works across the whole graph.
            self._sync_detail(entry)
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
    # Markdown projection (the graph rendered for humans)
    # ------------------------------------------------------------------

    def export_markdown(self) -> str:
        """Render the visible graph to ``<root>/_graph.md`` (projection view).

        The markdown is a *rendering*, not a second store: the graph stays
        the single source of truth and this file is regenerated from it.  It
        is underscore-prefixed, so the facade's scan rules skip it during
        ingestion -- the projection can never be re-ingested as a memory of
        itself.  Returns the rendered text; failures are logged, never raised.

        Visible nodes only: archived (forgotten) and superseded (arbitrated)
        nodes are excluded, keeping the view equal to what recall can see.
        """
        try:
            sections: list[str] = [
                "# Memory Graph (auto-generated projection)",
                "",
                "Rendered from the memory graph; edits here are NOT persisted.",
                "Authoritative data lives in the encrypted layer files.",
                "",
            ]
            with self._lock:
                # Healed visibility: tombstones hide a node only while its
                # replacement lineage is alive, keeping the projection equal
                # to what recall can see.
                by_id: dict[str, MemoryEntry] = {}
                for layer in self._long_term.values():
                    for indexed in layer:
                        by_id[indexed.id] = indexed
                for buffered in self._buffer:
                    by_id[buffered.id] = buffered
                for memory_type in self.LONG_TERM_TYPES:
                    entries = [
                        entry for entry in self._long_term[memory_type.value]
                        if not entry.decay.archived
                        and (
                            not entry.superseded_by
                            or not self._lineage_alive(
                                entry.superseded_by, by_id, frozenset({entry.id})
                            )
                        )
                    ]
                    if not entries:
                        continue
                    sections.append(f"## {memory_type.value} ({len(entries)})")
                    sections.append("")
                    for entry in entries:
                        sections.append(
                            f"- [conf {entry.confidence:.2f}, uses {entry.decay.access_count}] {' '.join(entry.content.split())[:300]}"
                        )
                        for edge in entry.links:
                            sections.append(f"  - {edge.relation} -> {edge.target_id}")
                    sections.append("")
            text = "\n".join(sections).rstrip() + "\n"
            target = self._root / "_graph.md"
            if text != read_text(target, default=""):
                write_text(target, text)
            return text
        except Exception:
            logger.warning(
                "[memdir.layers] markdown projection failed",
                exc_info=True,
            )
            return ""

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
                    "branch_of": header.branch_of,
                    "branch_index": header.branch_index,
                    INGEST_CONTENT_HASH_KEY: digest,
                }

                existing = self._find_by_source(source)
                if existing is not None:
                    if existing.content == content and existing.metadata.get(INGEST_CONTENT_HASH_KEY) == digest:
                        continue
                    existing.content = content
                    existing.importance = max(existing.importance, INGESTED_MEMORY_IMPORTANCE)
                    existing.metadata.update(metadata)
                    existing.version += 1
                    self._persist_source(existing)
                    changed += 1
                    continue

                self.store(
                    MemoryEntry(
                        id=self._ingest_entry_id(source),
                        content=content,
                        type=MemoryType.from_suggestion(header.memory_type).value,
                        importance=INGESTED_MEMORY_IMPORTANCE,
                        source=source,
                        metadata=metadata,
                    )
                )
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
