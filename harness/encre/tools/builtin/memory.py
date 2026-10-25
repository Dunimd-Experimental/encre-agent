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

"""Builtin memory tools -- native surface of the rewritten memory graph.

These tools speak the graph store's own primitives, nothing else:

- ``MemoryLayerStore.intake`` -- the judged-candidate write path; the only
  shape in which an experience may enter the graph (buffered first, promoted
  on repetition or when linked).
- ``MemoryLayerStore.store`` / ``supersede`` / ``link`` / ``record_access`` --
  the direct write surface (arbitration, typed edges, decay feedback).
- ``RecallEngine.recall`` -- spreading-activation retrieval over the graph.

There is deliberately no file-layer bridge here: the human-editable memory
directory (``MEMORY.md`` entrypoint plus ingested ``.md`` notes) is the
user's channel and is wired into the graph by the loop's ingestion pass, not
by these tools.  Node references are entry ids; ``md:<file>`` sources of
ingested user notes resolve too.

Store binding: the production loop wires ONE ``MemoryLayerStore`` +
``RecallEngine`` per run (``LoopWiring._wire_memory_layers``), attaches the
store to the event backbone (the significance judge feeds it, the curator
evolves it every turn) and exposes both on the session.  These tools resolve
that session-bound store through the active loop and only fall back to
process-level singletons when no loop is active (headless use) -- a
tool-private store would split the memory graph in two.

Hardcoded builtin: previously shipped as the ``ea-tool-memory`` EA plugin;
inlined into the harness so the memory system prompt's mandated tools are
always present regardless of plugin state.
"""

from __future__ import annotations

from typing import Any

from encre.tools.base import build_tool


def _get_memory_dir() -> str:
    """Get the human-editable memory directory (ingestion target)."""
    from encre.config import get_data_dir

    return str(get_data_dir() / "memory")


def _resolve_session() -> Any:
    """Resolve the active loop's session (``None`` when headless)."""
    try:
        from encre.tools.runtime import _resolve_loop
    except ImportError:
        return None
    loop = _resolve_loop()
    if loop is None:
        return None
    return getattr(loop, "session", None)


# Process-level fallback singletons, used only when no loop (and therefore no
# session-bound store) is active.  Keyed by memory directory.
_LAYER_STORES: dict[str, Any] = {}
_RECALL_ENGINES: dict[str, Any] = {}


def _layer_store(mem_dir: str):
    """Return the active session's ``MemoryLayerStore`` (fallback: singleton).

    The session-bound store is the one the loop's curator evolves and the
    significance judge feeds -- using anything else would fork the memory
    graph, so it always wins when present.
    """
    store = getattr(_resolve_session(), "memory_layers", None)
    if store is not None:
        return store
    store = _LAYER_STORES.get(mem_dir)
    if store is None:
        from encre.memdir.layers import MemoryLayerStore

        store = MemoryLayerStore()
        store.ingest_memory_dir(mem_dir)
        _LAYER_STORES[mem_dir] = store
    return store


def _recall_engine(mem_dir: str):
    """Return the active session's ``RecallEngine`` (fallback: singleton)."""
    engine = getattr(_resolve_session(), "recall_engine", None)
    if engine is not None:
        return engine
    engine = _RECALL_ENGINES.get(mem_dir)
    if engine is None:
        from encre.memdir.recall import RecallEngine

        # record_access=True: a model-initiated search is a genuine usage
        # signal that feeds the decay/consolidation feedback loop.
        engine = RecallEngine(_layer_store(mem_dir), record_access=True)
        _RECALL_ENGINES[mem_dir] = engine
    return engine


def _memory_types() -> list[str]:
    """Canonical graph node types (the working scratchpad is not writable)."""
    from encre.memdir.layers import MemoryType

    return [t.value for t in MemoryType if t.value != "working"]


def _link_relations() -> list[str]:
    """Canonical typed-edge relations."""
    from encre.memdir.layers import LinkRelation

    return [r.value for r in LinkRelation]


def _resolve_entry(store: Any, ref: str) -> Any:
    """Resolve a model-facing reference into a graph node.

    Node ids are the primary handle (every write result echoes one).  Sources
    of ingested user notes (``md:<filename>`` or the bare filename) resolve
    as well, since those notes are part of the graph after ingestion.
    """
    ref = str(ref or "").strip()
    if not ref:
        return None
    direct = store.get(ref)
    if direct is None:
        # Tool-created nodes may still sit in the short-term buffer; the
        # long-term-only ``get`` never sees them.
        direct = next((e for e in store.buffer() if e.id == ref), None)
    if direct is not None:
        return direct
    source = ref if ref.startswith("md:") else f"md:{ref}"
    for pool in (store.long_term(), store.buffer()):
        for entry in pool:
            if entry.source == source or entry.source == f"md:{ref}.md":
                return entry
    return None


def _entry_label(entry: Any) -> str:
    """Human-facing label for a graph node (source file when ingested)."""
    source = str(entry.source or "")
    if source.startswith("md:"):
        return source[3:]
    return entry.id


def _entry_placement(store: Any, entry_id: str, before_long: set[str], before_buf: set[str]) -> str:
    """Describe where ``intake`` left a node, relative to pre-call pools."""
    if entry_id in before_long:
        return "merged into the existing long-term node"
    if entry_id in before_buf:
        return "promoted to long-term (repeated observation)"
    if any(e.id == entry_id for e in store.long_term()):
        return "promoted to long-term"
    return "held in the short-term buffer (promotes on repetition or when linked)"


def _new_candidate(
    content: str,
    significance: float,
    node_type: str,
    reason: str,
    keywords: list[str] | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> Any:
    """Build a judged candidate shaped exactly like the judge's output."""
    from encre.memdir.judgment import MemoryCandidate

    session = _resolve_session()
    metadata: dict[str, Any] = {"origin": "agent_tool", "full_content": content}
    # Tag the origin turn so session rollback can purge this node later.
    # ``active_turn`` is the 1-based numbering the end-of-turn judge uses and
    # the rollback boundary compares against: tagging the raw ``turn_count``
    # (still the previous turn's count mid-turn) put these nodes one index
    # early, so the turn being rolled back kept its memory.
    tool_turn = getattr(session, "active_turn", None)
    if isinstance(tool_turn, int) and not isinstance(tool_turn, bool):
        metadata["turn"] = tool_turn
    if keywords:
        # Model-assisted tokenization channel: the curator forwards these to
        # refresh_lexicon, which admits each only after corpus validation.
        metadata["model_keywords"] = keywords
    if extra_metadata:
        metadata.update(extra_metadata)
    return MemoryCandidate(
        content=content,
        significance=significance,
        reasons=[reason] if reason else [],
        suggested_type=node_type,
        session_id=str(getattr(session, "id", "") or ""),
        metadata=metadata,
    )


def _parse_deadline_ts(raw: Any) -> float | None:
    """Parse an ISO 8601 deadline into epoch seconds; ``None`` when invalid.

    Naive datetimes are interpreted in the local timezone; a trailing ``Z``
    is normalised for pre-3.11 ``datetime.fromisoformat``.
    """
    from datetime import datetime

    text = str(raw or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.timestamp()


async def _memory_create_execute(**kwargs: Any) -> str:
    """Memory create execute -- intake a candidate into the memory graph.

    Args:
        kwargs: Description of the kwargs parameter.
    """
    content = str(kwargs.get("content", "") or "").strip()
    if not content:
        return "Error: content is required."
    node_type = str(kwargs.get("type", "") or "episode").strip().lower()
    if node_type not in _memory_types():
        return f"Unknown type '{node_type}'. Valid types: {', '.join(_memory_types())}."
    try:
        significance = float(kwargs.get("significance", 0.5))
    except (TypeError, ValueError):
        significance = 0.5
    significance = max(0.0, min(1.0, significance))
    reason = str(kwargs.get("reason", "") or "").strip()
    # Commitment nodes carry a deadline: the resident watcher wakes on it.
    deadline_raw = str(kwargs.get("deadline", "") or "").strip()
    deadline_ts: float | None = None
    if deadline_raw:
        deadline_ts = _parse_deadline_ts(deadline_raw)
        if deadline_ts is None:
            return "Error: deadline must be an ISO 8601 timestamp (e.g. 2026-10-08T18:00:00)."
    if node_type == "commitment" and deadline_ts is None:
        return "Error: type 'commitment' requires a deadline (ISO 8601 timestamp)."
    # Accept keywords as a list OR a comma-separated string; hard-cap the
    # count so a runaway model cannot flood the lexicon.
    raw_keywords = kwargs.get("keywords")
    keywords: list[str] = []
    if isinstance(raw_keywords, str):
        keywords = [k.strip() for k in raw_keywords.split(",") if k.strip()]
    elif isinstance(raw_keywords, list | tuple):
        keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]
    keywords = keywords[:8]

    extra_metadata: dict[str, Any] = {}
    if deadline_ts is not None:
        extra_metadata["deadline_ts"] = deadline_ts
        extra_metadata["commitment_status"] = "active"
        extra_metadata["notify_stage"] = 0

    mem_dir = _get_memory_dir()
    try:
        store = _layer_store(mem_dir)
        before_long = {e.id for e in store.long_term()}
        before_buf = {e.id for e in store.buffer()}
        entry = store.intake(_new_candidate(content, significance, node_type, reason, keywords, extra_metadata))
        placement = _entry_placement(store, entry.id, before_long, before_buf)
        suffix = f", deadline: {deadline_raw}" if deadline_ts is not None else ""
        return f"Memory node '{entry.id}' (type: {entry.type}, importance: {float(entry.importance):.2f}{suffix}) {placement}."
    except Exception as e:
        return f"Error creating memory: {e}"


async def _memory_read_execute(**kwargs: Any) -> str:
    """Memory read execute -- read one graph node with its full detail.

    Args:
        kwargs: Description of the kwargs parameter.
    """
    ref = str(kwargs.get("ref", "") or "").strip()
    if not ref:
        return "Error: ref is required."

    mem_dir = _get_memory_dir()
    try:
        store = _layer_store(mem_dir)
        entry = _resolve_entry(store, ref)
        if entry is None:
            return f"Memory node '{ref}' not found in the graph. Use memory_search to locate it."
    except Exception as e:
        return f"Error reading memory: {e}"

    try:
        detail = store.load_detail(entry)
    except Exception:
        detail = ""
    body = (detail or str(entry.content or "")).strip()

    status = "live"
    if entry.superseded_by:
        winner = store.get(entry.superseded_by)
        status = f"superseded by {_entry_label(winner) if winner else entry.superseded_by}"

    link_parts: list[str] = []
    for edge in entry.links:
        target = store.get(edge.target_id)
        label = _entry_label(target) if target is not None else edge.target_id
        link_parts.append(f"{edge.relation} -> {label}")

    lines = [
        f"Memory node {_entry_label(entry)} ({entry.id})",
        f"- type: {entry.type} | importance: {float(entry.importance):.2f} | confidence: {float(entry.confidence):.2f} | status: {status}",
        f"- accesses: {int(getattr(entry.decay, 'access_count', 0))}",
    ]
    if link_parts:
        lines.append(f"- links: {'; '.join(link_parts)}")
    lines.append("")
    lines.append(body if body else "(empty node)")
    return "\n".join(lines)


async def _memory_update_execute(**kwargs: Any) -> str:
    """Memory update execute -- replace a node via supersede arbitration.

    The graph is append-only: a correction intakes a fresh candidate and
    tombstones the loser, keeping the audit trail traversable.

    Args:
        kwargs: Description of the kwargs parameter.
    """
    ref = str(kwargs.get("ref", "") or "").strip()
    content = str(kwargs.get("content", "") or "").strip()
    if not ref:
        return "Error: ref is required."

    mem_dir = _get_memory_dir()
    try:
        store = _layer_store(mem_dir)
        old = _resolve_entry(store, ref)
        if old is None:
            return f"Memory node '{ref}' not found in the graph. Use memory_search to locate it."

        # Lifecycle transitions (commitments): a status flip is metadata, not
        # a content correction -- no supersede, the node keeps its identity.
        status_update = str(kwargs.get("commitment_status", "") or "").strip().lower()
        if status_update:
            from encre.memdir.layers import (
                COMMITMENT_STATUS_DONE,
                COMMITMENT_STATUS_EXPIRED,
                COMMITMENT_STATUS_KEY,
            )

            if status_update not in (COMMITMENT_STATUS_DONE, COMMITMENT_STATUS_EXPIRED):
                return "Error: commitment_status must be 'done' or 'expired'."
            if old.memory_type.value != "commitment":
                return "Error: commitment_status applies only to commitment nodes."
            store.update_metadata(old.id, {COMMITMENT_STATUS_KEY: status_update})
            return f"Commitment '{old.id}' marked {status_update}."

        if not content:
            return "Error: content is required."

        node_type = str(kwargs.get("type", "") or old.type).strip().lower()
        if node_type not in _memory_types():
            return f"Unknown type '{node_type}'. Valid types: {', '.join(_memory_types())}."
        try:
            significance = float(kwargs.get("significance", old.importance))
        except (TypeError, ValueError):
            significance = float(old.importance)
        significance = max(0.0, min(1.0, significance))
        reason = str(kwargs.get("reason", "") or "").strip()

        candidate = _new_candidate(content, significance, node_type, reason)
        candidate.metadata["supersedes"] = old.id
        before_long = {e.id for e in store.long_term()}
        before_buf = {e.id for e in store.buffer()}
        new_entry = store.intake(candidate)

        if new_entry.id == old.id:
            # The candidate merged back into the same node (duplicate
            # detection); superseding itself would corrupt the lineage.
            placement = _entry_placement(store, old.id, before_long, before_buf)
            return f"Content merged into the existing node '{old.id}' ({placement}); nothing to supersede."

        if not store.supersede(old.id, new_entry.id):
            return f"Error: node '{old.id}' could not be superseded."
        return (
            f"Node '{old.id}' was superseded by '{new_entry.id}' "
            f"(type: {new_entry.type}, importance: {float(new_entry.importance):.2f}). "
            f"The old node is tombstoned: kept for audit, hidden from recall."
        )
    except Exception as e:
        return f"Error updating memory: {e}"


async def _memory_search_execute(**kwargs: Any) -> str:
    """Memory search execute -- spreading-activation recall over the graph.

    Args:
        kwargs: Description of the kwargs parameter.
    """
    query = kwargs.get("query", "")
    top_k = kwargs.get("top_k", 5)
    type_filter = str(kwargs.get("type", "") or "").strip().lower()
    include_archived = bool(kwargs.get("include_archived", False))

    canonical_type = ""
    if type_filter:
        if type_filter not in _memory_types():
            return f"Unknown type '{type_filter}'. Valid types: {', '.join(_memory_types())}."
        canonical_type = type_filter

    mem_dir = _get_memory_dir()
    try:
        from encre.memdir.age import memory_freshness_text

        store = _layer_store(mem_dir)
        # Idempotent re-ingest: picks up externally edited user notes before
        # recalling (unchanged files are skipped by content hash, and recall
        # reads the store's live entries, so no reload is needed).
        store.ingest_memory_dir(mem_dir)
        hits = _recall_engine(mem_dir).recall(
            str(query),
            top_k=max(1, int(top_k)),
            record_access=True,
            include_archived=include_archived,
        )
        if canonical_type:
            hits = [h for h in hits if str(h.entry.type) == canonical_type]
    except Exception as e:
        return f"Error searching memories: {e}"

    if not hits:
        return f"No memories found matching '{query}'."

    lines: list[str] = [f"Memory search results for '{query}':", ""]
    for i, hit in enumerate(hits, 1):
        entry = hit.entry
        freshness = memory_freshness_text(float(entry.created_at) * 1000.0)
        spread = f", spread via {len(hit.via_links)} link(s) from a direct match" if hit.hops > 0 else ""
        lines.append(
            f"{i}. {_entry_label(entry)} [{entry.id}] (type: {entry.type}, "
            f"score: {hit.score:.2f}, confidence: {float(entry.confidence):.2f}, "
            f"{freshness}{spread})"
        )
        content = (hit.detail_text or entry.content or "").strip()
        if content:
            lines.append(f"   {content[:200]}")
        lines.append("")
    return "\n".join(lines)


async def _memory_link_execute(**kwargs: Any) -> str:
    """Memory link execute -- wire a typed edge between two graph nodes.

    Args:
        kwargs: Description of the kwargs parameter.
    """
    source = kwargs.get("source", "")
    target = kwargs.get("target", "")
    weight = kwargs.get("weight", 1.0)

    from encre.memdir.layers import LinkRelation

    raw_relation = str(kwargs.get("relation", "")).strip().lower()
    try:
        relation = LinkRelation(raw_relation).value
    except ValueError:
        return f"Unknown relation '{raw_relation}'. Valid relations: {', '.join(_link_relations())}."
    try:
        weight = max(0.0, min(2.0, float(weight)))
    except (TypeError, ValueError):
        weight = 1.0

    mem_dir = _get_memory_dir()
    try:
        store = _layer_store(mem_dir)
        src = _resolve_entry(store, source)
        if src is None:
            return f"Source memory '{source}' not found in the graph. Use memory_search to locate it."
        tgt = _resolve_entry(store, target)
        if tgt is None:
            return f"Target memory '{target}' not found in the graph. Use memory_search to locate it."

        # Supersede is arbitration, not a plain edge: the loser is tombstoned
        # (kept for audit, hidden from recall) and the winner gains the
        # supersedes edge, so lineage stays traversable.
        if relation == "supersedes":
            if not store.supersede(tgt.id, src.id):
                return f"Error: target memory '{target}' could not be superseded."
            return f"'{_entry_label(tgt)}' was superseded by '{_entry_label(src)}' (tombstoned, hidden from recall, kept for audit)."

        store.link(src.id, tgt.id, relation, weight=weight)
        return f"Linked '{_entry_label(src)}' --[{relation}]--> '{_entry_label(tgt)}' (weight: {weight:.2f})."
    except Exception as e:
        return f"Error linking memories: {e}"


async def _memory_status_execute(**kwargs: Any) -> str:
    """Memory status execute -- introspect the memory graph.

    Args:
        kwargs: Description of the kwargs parameter.
    """
    del kwargs  # no parameters; kept for a uniform execute signature
    mem_dir = _get_memory_dir()
    try:
        store = _layer_store(mem_dir)
        long_term = store.long_term()
        buffered = store.buffer()
        archived = store.archived()
    except Exception as e:
        return f"Error reading memory status: {e}"

    by_type: dict[str, int] = {}
    for entry in long_term:
        by_type[str(entry.type)] = by_type.get(str(entry.type), 0) + 1
    total_links = sum(len(entry.links) for entry in long_term)
    superseded = sum(1 for entry in long_term if entry.superseded_by)
    confidences = [float(entry.confidence) for entry in long_term if entry.confidence]
    mean_conf = sum(confidences) / len(confidences) if confidences else 0.0

    lines = [
        "Memory graph status:",
        f"- Long-term nodes: {len(long_term)} (by type: " + (", ".join(f"{k}={v}" for k, v in sorted(by_type.items())) or "none") + ")",
        f"- Buffered candidates (not yet promoted): {len(buffered)}",
        f"- Archived (decayed, hidden from recall): {len(archived)}",
        f"- Superseded (tombstoned, audit only): {superseded}",
        f"- Typed edges: {total_links}; mean confidence: {mean_conf:.2f}",
    ]
    return "\n".join(lines)


async def _memory_profile_execute(**kwargs: Any) -> str:
    """Memory profile execute.

    Args:
        kwargs: Description of the kwargs parameter.
    """
    field = kwargs.get("field", "")
    value = kwargs.get("value")
    confidence = float(kwargs.get("confidence", 0.7))

    from encre.config import get_data_dir
    from encre.profile.system import EncreProfileSystem

    mem_dir = str(get_data_dir() / "memory")
    ps = EncreProfileSystem(mem_dir)
    ps.load()

    if value is not None:
        if not field:
            return "Error: field is required when updating."
        valid_fields = {
            "expertise_level",
            "domain",
            "formality",
            "detail_preference",
            "tone",
            "response_style",
            "testing_preference",
            "learning_style",
            "error_tolerance",
            "os",
            "editor",
            "name",
            "language_preference",
            "timezone",
            "preferred_languages",
            "preferred_frameworks",
            "skill_levels",
            "common_goals",
        }
        if field not in valid_fields:
            return f"Unknown field '{field}'. Valid fields: {', '.join(sorted(valid_fields))}"
        try:
            ps.update_field(field, value, confidence=confidence)
            return f"Profile field '{field}' updated to '{value}' (confidence: {confidence:.2f})."
        except Exception as e:
            return f"Error updating profile: {e}"

    # Query mode
    data = ps.get_data()
    if field:
        if field not in data or not data[field]:
            return f"Profile field '{field}' is not set."
        conf = data.get("confidence", {}).get(field, 0)
        val = data[field]
        if isinstance(val, list):
            val = ", ".join(val)
        elif isinstance(val, dict):
            val = ", ".join(f"{k}: {v}" for k, v in val.items())
        return f"{field}: {val} (confidence: {conf:.2f})"

    # Full profile dump
    lines: list[str] = ["## User Profile", ""]
    sections = {
        "Basic": ["name", "language_preference", "timezone", "expertise_level", "domain"],
        "Communication": ["formality", "detail_preference", "tone", "response_style"],
        "Technical": ["preferred_languages", "preferred_frameworks", "skill_levels", "os", "editor"],
        "Behavioral": ["testing_preference", "learning_style", "error_tolerance", "common_goals"],
    }
    has_any = False
    for section_name, fields in sections.items():
        section_parts: list[str] = []
        for f in fields:
            val = data.get(f)
            if val and val != "" and val != [] and val != {}:
                conf = data.get("confidence", {}).get(f, 0)
                display = val
                if isinstance(val, list):
                    display = ", ".join(val)
                elif isinstance(val, dict):
                    display = ", ".join(f"{k}: {v}" for k, v in val.items())
                section_parts.append(f"  - {f}: {display} (conf: {conf:.2f})")
                has_any = True
        if section_parts:
            lines.append(f"### {section_name}")
            lines.extend(section_parts)
            lines.append("")
    if not has_any:
        return "User profile is empty. It builds over time as interactions accumulate."
    return "\n".join(lines)


memory_create_tool = build_tool(
    name="memory_create",
    description=(
        "Intake a new memory node into the persistent memory graph. The node "
        "lands in the short-term buffer and is promoted to long-term by "
        "repetition or when referenced by memory_link; duplicates merge into "
        "the existing node instead of duplicating. "
        "Use this to record durable facts, user preferences, decisions, "
        "lessons learned, or episodes that future sessions should recall. "
        "Use type=commitment when the user commits to doing something by a "
        "deadline or asks to be reminded -- record WHAT was promised and pass "
        "`deadline` (ISO 8601); the resident watcher wakes on it "
        "automatically. "
        "Do NOT use this for ephemeral scratch data (use todo/task tools) or "
        "to correct an existing memory (use memory_update -- the graph is "
        "append-only, corrections supersede). "
        "Tips: search first (memory_search) to avoid duplicates; `type` "
        "classifies the node for richer recall (fact/preference/decision/"
        "lesson/entity/episode/commitment); `significance` (0.0-1.0) drives "
        "promotion and decay resistance; `reason` records why this is worth "
        "remembering; `keywords` lists the key terms (full names, fixed "
        "phrases) so the tokenizer keeps them whole. "
        "Pitfalls: buffered nodes are recallable but weaker -- link or repeat "
        "them to make them durable; node ids are the primary handle -- use "
        "the id echoed in the result for memory_read/memory_link."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The memory statement, written as a refined standalone sentence.",
            },
            "type": {
                "type": "string",
                "enum": ["fact", "preference", "decision", "lesson", "entity", "episode", "commitment"],
                "description": (
                    "Graph node classification; defaults to episode. Use "
                    "commitment for promises/deadlines the user should be "
                    "woken about (requires `deadline`)."
                ),
            },
            "deadline": {
                "type": "string",
                "description": (
                    "ISO 8601 deadline (e.g. 2026-10-08T18:00:00, or with "
                    "timezone 2026-10-08T18:00:00+08:00). Required for "
                    "type=commitment."
                ),
            },
            "significance": {
                "type": "number",
                "description": "How much this matters, 0.0-1.0; drives promotion and decay resistance; defaults to 0.5.",
            },
            "reason": {
                "type": "string",
                "description": "Why this memory is worth keeping (stored as provenance).",
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Key terms this memory is about -- names, subjects, fixed "
                    "phrases (e.g. a person's full name that segmentation might "
                    "shred). Each is admitted to the statistical tokenizer only "
                    "after corpus validation; at most 8 are kept."
                ),
            },
        },
        "required": ["content"],
    },
    execute=_memory_create_execute,
    intents=["general", "coding"],
    category="memory",
    semantic_type="write",
    is_destructive=True,
)

memory_read_tool = build_tool(
    name="memory_read",
    description=(
        "Read one memory-graph node in full: metadata (type, importance, "
        "confidence, status, access count, links) plus its verbatim detail "
        "layer. "
        "Use this when you already have a node id (echoed by every memory "
        "tool) and want the complete node. "
        "Do NOT use this to discover memories by meaning (use memory_search) "
        "or to inspect the user profile (use memory_profile). "
        "Tips: notes the user edited by hand can be referenced as filenames "
        "too -- they enter the graph through ingestion. "
        "Pitfalls: a node whose status says superseded is kept only for "
        "audit; follow the status pointer to the live replacement."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "Node id, or a filename of an ingested user note (e.g. 'topic.md').",
            },
        },
        "required": ["ref"],
    },
    execute=_memory_read_execute,
    intents=["general", "coding", "research"],
    category="memory",
    semantic_type="read",
    is_concurrency_safe=lambda _: True,
    is_readonly=True,
)

memory_update_tool = build_tool(
    name="memory_update",
    description=(
        "Replace an existing memory node with corrected content via "
        "supersede arbitration: a fresh node is intaken and the old one is "
        "tombstoned -- kept for audit, hidden from recall. The graph never "
        "leaves two opposing beliefs alive. "
        "Use this to correct, refresh, or contradict an existing memory. "
        "For commitment nodes, pass only `ref` + `commitment_status` to "
        "close one out (done/expired) without touching its content. "
        "Do NOT use this to add a new memory (use memory_create) or to "
        "record a relation between two existing nodes (use memory_link). "
        "Tips: omit `type`/`significance` to inherit them from the old node; "
        "`reason` documents why the correction was needed. "
        "Pitfalls: supersession is permanent -- the loser never returns to "
        "normal recall; if the new content matches an existing node it "
        "merges there instead of superseding."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "Node id (or ingested-note filename) of the memory to replace.",
            },
            "content": {
                "type": "string",
                "description": "The corrected memory statement.",
            },
            "commitment_status": {
                "type": "string",
                "enum": ["done", "expired"],
                "description": (
                    "Close a commitment node (mark finished or expired) by id without superseding it. When set, `content` is not required."
                ),
            },
            "type": {
                "type": "string",
                "enum": ["fact", "preference", "decision", "lesson", "entity", "episode"],
                "description": "Node type for the replacement; defaults to the old node's type.",
            },
            "significance": {
                "type": "number",
                "description": "Importance of the replacement, 0.0-1.0; defaults to the old node's importance.",
            },
            "reason": {
                "type": "string",
                "description": "Why this correction was needed (stored as provenance).",
            },
        },
        "required": ["ref"],
    },
    execute=_memory_update_execute,
    intents=["general", "coding"],
    category="memory",
    semantic_type="write",
    is_destructive=True,
)

memory_search_tool = build_tool(
    name="memory_search",
    description=(
        "Search the persistent memory graph and return the most relevant "
        "nodes for the query. Recall is associative: direct matches seed the "
        "search, then activation spreads along the graph's typed links "
        "(causes/contradicts/refines/supersedes/supports/mentions/co_occurs), "
        "so recalling one memory pulls in the cluster it belongs to. "
        "Use this when you need to recall a memory by meaning rather than by "
        "node id, e.g. 'what does the user prefer for testing?' -- and "
        "always before creating a new memory to avoid duplicates. "
        "Do NOT use this when you already know the node id (use memory_read) "
        "or for structured user profile fields (use memory_profile). "
        "Tips: write the query as a natural-language question for best "
        "recall; raise `top_k` to broaden the result set; narrow with `type` "
        "(fact/preference/decision/lesson/entity/episode) or pass "
        "`include_archived` to reach decayed nodes; hits are annotated with "
        "freshness, node confidence, and the link path when they were "
        "reached by spreading rather than a direct match. "
        "Pitfalls: every search records access for the hits, strengthening "
        "their retention against decay -- do not spam queries; very generic "
        "queries can surface many similar memories, narrow the wording or "
        "reduce `top_k` to focus results."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language query used to find relevant memories.",
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum number of results to return; defaults to 5.",
            },
            "type": {
                "type": "string",
                "enum": ["fact", "preference", "decision", "lesson", "entity", "episode"],
                "description": "Restrict results to one graph node type; omitted means all types.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include decayed/archived nodes (hidden from recall by default); defaults to false.",
            },
        },
        "required": ["query"],
    },
    execute=_memory_search_execute,
    intents=["general", "coding", "research"],
    category="memory",
    semantic_type="search",
    is_concurrency_safe=lambda _: True,
    is_readonly=True,
)

memory_link_tool = build_tool(
    name="memory_link",
    description=(
        "Wire a typed edge between two memory-graph nodes, by node id (or "
        "ingested-note filename). This shapes the associative recall "
        "surface: links are the paths spreading activation travels, so a "
        "linked cluster is recalled together. `supersedes` is special -- it "
        "arbitrates contradictions: the target is tombstoned (kept for "
        "audit, hidden from recall) and the source becomes the live "
        "replacement. "
        "Use this to record causal chains (causes), supporting evidence "
        "(supports), refinements (refines), corrections (supersedes), or "
        "plain associations (mentions) -- and to promote buffered nodes by "
        "referencing them. "
        "Do NOT use it to duplicate structural links the system already "
        "maintains (co_occurs wiring) or to link memories that do not exist "
        "yet -- create them first. "
        "Tips: node ids are echoed by every memory tool; `weight` (0.0-2.0) "
        "scales edge strength for recall spreading; linking a buffered node "
        "promotes it to long-term. "
        "Pitfalls: supersedes permanently hides the loser from normal "
        "recall; edges are not deduplicated across relations -- linking the "
        "same pair twice with different relations creates two edges."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Node id (or ingested-note filename) the edge starts from.",
            },
            "relation": {
                "type": "string",
                "enum": ["causes", "contradicts", "refines", "supersedes", "supports", "mentions", "co_occurs"],
                "description": "Edge relation; 'supersedes' tombstones the target (contradiction arbitration).",
            },
            "target": {
                "type": "string",
                "description": "Node id (or ingested-note filename) the edge points to.",
            },
            "weight": {
                "type": "number",
                "description": "Edge strength for recall spreading, 0.0-2.0; defaults to 1.0.",
            },
        },
        "required": ["source", "relation", "target"],
    },
    execute=_memory_link_execute,
    intents=["general", "coding"],
    category="memory",
    semantic_type="write",
    is_destructive=True,
)

memory_status_tool = build_tool(
    name="memory_status",
    description=(
        "Introspect the memory graph: node counts by type, buffered "
        "candidates awaiting promotion, archived nodes, tombstoned "
        "supersessions, edge totals, and mean node confidence. "
        "Use this to check memory health before/after large memory work, to "
        "find out whether the buffer is hoarding candidates that should be "
        "linked, or to explain why a memory is not being recalled (it may "
        "be archived or superseded). "
        "Do NOT use it to list individual memories (use memory_search) or to "
        "read one memory's content (use memory_read). "
        "Tips: run it once before bulk memory_link sessions to see what "
        "exists; buffered candidates can be pulled into long-term by "
        "referencing them with memory_link. "
        "Pitfalls: counts reflect the session-bound graph store; externally "
        "edited user notes appear only after the next ingestion pass."
    ),
    input_schema={"type": "object", "properties": {}},
    execute=_memory_status_execute,
    intents=["general", "coding"],
    category="memory",
    semantic_type="read",
    is_concurrency_safe=lambda _: True,
    is_readonly=True,
)

memory_profile_tool = build_tool(
    name="memory_profile",
    description=(
        "Read or update the user profile -- structured observations about the user "
        "(expertise, communication style, preferences, OS, editor, etc.) stored as "
        "_profile.md inside the unified memory system.\n\n"
        "Use this to tailor responses to the user (query) or to record a new "
        "observation (update).\n"
        "Do NOT use this for free-form memories (use memory_create/update) or for "
        "one-off facts that do not belong in the profile.\n"
        "Tips: query with no args to dump the whole profile; pass field+value to "
        "record an observation, optionally with a confidence score.\n"
        "Pitfalls: profile fields are a fixed enumeration -- see the `field` enum "
        "description for the supported set."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "field": {
                "type": "string",
                "description": (
                    "Profile field to query or update. Valid fields: "
                    "expertise_level, domain, formality, detail_preference, "
                    "tone, response_style, testing_preference, learning_style, "
                    "error_tolerance, os, editor, name, language_preference, "
                    "timezone, preferred_languages, preferred_frameworks, "
                    "skill_levels, common_goals"
                ),
            },
            "value": {
                "type": "string",
                "description": (
                    "If set, updates the field with this value. If omitted, queries the field (or all fields if field is also omitted)."
                ),
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in the observation, from 0.0 to 1.0; only used when updating. Defaults to 0.7.",
            },
        },
    },
    execute=_memory_profile_execute,
    intents=["general", "coding", "research"],
    category="memory",
    semantic_type="read",
    is_concurrency_safe=lambda _: True,
    is_readonly=True,
)
