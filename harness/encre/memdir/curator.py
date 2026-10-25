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

"""Curator: the self-evolution half of the memory graph.

Core principle (project-wide, non-negotiable): **context != memory**, and
memory is not a dead archive either.  The significance judge decides *what*
enters the graph (:mod:`encre.memdir.judgment` -> :mod:`encre.memdir.layers`);
the curator decides *what the graph should believe afterwards*.  The two
together form the cross network: memory records experience, evolution
re-weights and re-links it.

Concretely, the curator runs one :meth:`curate` pass per session turn end and:

* **Outcome feedback** -- candidates that fired ``user_correction`` weaken the
  existing nodes they correct (and gain a ``contradicts`` edge onto them);
  ``error_or_surprise`` weakens the nodes implicated in the failure;
  ``key_decision`` / ``goal_met`` strengthen the nodes that supported the
  decision.  Confidence thereby tracks evidence instead of staying frozen.
* **Co-occurrence wiring** -- the session's distinct candidate nodes are
  linked pairwise with ``co_occurs`` edges, because things that happened in
  one episode belong to one associative neighbourhood.
* **Skill candidacy** -- ``lesson`` nodes whose confidence and usage prove
  them (``confidence >= threshold``, ``access_count >= min_uses``) are
  stamped as skill candidates and announced on the backbone as
  ``skill_candidate`` events, where the existing verification stack owns the
  actual promotion.  The curator never promotes by itself: lessons earn the
  *right to be verified*, not a bypass of the gate.

The curator is fully offline and rule-based (no LLM, no network), subscribes
to the same event backbone as the judge, and never raises into a publisher.
"""

from __future__ import annotations

import math
import threading
import time
from collections import Counter
from typing import Any

from encre.logging_config import get_logger
from encre.memdir import judgment as _judgment
from encre.memdir.judgment import MemoryCandidate, _tokenize, refresh_lexicon
from encre.memdir.layers import LinkRelation, MemoryEntry, MemoryLayerStore, MemoryType

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_REINFORCE_DELTA",
    "DEFAULT_SKILL_CONFIDENCE",
    "DEFAULT_SKILL_MIN_USES",
    "DEFAULT_WEAKEN_DELTA",
    "MemoryCurator",
]

#: Confidence bump for nodes that supported a positive outcome.
DEFAULT_REINFORCE_DELTA = 0.10

#: Confidence cut for nodes implicated in a correction or a failure.
DEFAULT_WEAKEN_DELTA = 0.15

#: Lesson confidence at which a node becomes a skill candidate.
DEFAULT_SKILL_CONFIDENCE = 0.80

#: Lesson accesses required before a node becomes a skill candidate.
DEFAULT_SKILL_MIN_USES = 3

#: Bus event kind carrying lesson nodes that earned skill verification.
SKILL_CANDIDATE_EVENT = "skill_candidate"

#: Bus event kind that triggers a curation pass for a session.
CURATE_TRIGGER_EVENT = "phase:turn_end"

#: Signals whose candidates *support* existing beliefs.
_REINFORCE_SIGNALS = ("key_decision", "goal_met")

#: Signals whose candidates *oppose* existing beliefs.
_WEAKEN_SIGNALS = ("user_correction", "error_or_surprise")

#: Minimum recall score for an existing node to count as a real match when
#: applying outcome feedback.  Below this the hit is coincidental vocabulary,
#: and acting on it would weaken unrelated nodes.
_MATCH_MIN_SCORE = 0.12

#: Fraction of an existing node's tokens a candidate must restate before the
#: candidate may weaken it without a direct similarity seed.  Cosine shrinks
#: when a rich correction restates a short belief ("美式咖啡不要了, 改喝燕麦
#: 拿铁" vs "只喝美式咖啡": shared core 3/8 = 0.375), so raw similarity alone
#: misses real negations; innocent same-subject pairs stay far below
#: (typically < 0.15).  Pure set arithmetic, no lexicons.
_COVER_MIN = 0.3

#: Statistical entity hubs: a token recurring in at least this many corpus
#: documents is a tracked subject (person, pet, tool, ...).
_ENTITY_MIN_DF = 2

#: At most this many hub tokens are promoted per curation pass, ranked by
#: ``df * idf`` so discriminative recurring tokens beat common vocabulary.
_ENTITY_TOP_TOKENS = 8

#: Pairwise semantic wiring: two visible nodes whose IDF-weighted cosine
#: similarity reaches this threshold are about the same subject and get a
#: ``co_occurs`` edge, no matter which session produced them.  Unrelated
#: pairs (tool output, system noise) sit below ~0.03; single-token overlaps
#: land around ~0.05-0.1; nodes of one natural subject cluster at ~0.15+.
#: The threshold must sit clearly ABOVE the single-shared-token band --
#: that is exactly what separates "shares a word" from "shares a subject".
#: Pure distributional arithmetic, no lexicons.
_RELATED_MIN_COSINE = 0.15

#: Episode binding: nodes that occurred in one session are linked
#: ``co_occurs`` when their content similarity reaches HALF the cross-corpus
#: threshold -- co-occurrence is itself evidence (shared episode context),
#: so the content gate is relaxed, but a pair with zero lexical overlap is
#: still not an association.
_CO_OCCURS_MIN_COSINE = _RELATED_MIN_COSINE / 2.0

#: Node kinds whose text is free human language.  Entity-hub nomination
#: counts document frequency over these only, so template boilerplate
#: (fixed phrase stock in generated lessons) can never become a hub.
_NATURAL_TYPES = {
    MemoryType.EPISODE,
    MemoryType.FACT,
    MemoryType.PREFERENCE,
    MemoryType.DECISION,
}

#: Hub tokens must keep almost their whole footprint inside natural-language
#: documents.  A real subject ("李晓明") occurs only where it is talked about;
#: conversational generic vocabulary ("今天" / "使用" / "工具") also permeates
#: command echoes and lesson prompts, so more than a third of its documents
#: are system-flavoured.  ``df_all / df_natural`` above this ratio disqualifies
#: the token -- pure document arithmetic, no lexicons.
_HUB_NATURAL_DF_SHARE = 1.5

#: A subject is talked about in SOME documents; corpus furniture ("一个" /
#: "然后") permeates most of them.  A token whose natural-language document
#: frequency exceeds this fraction of ALL natural-language documents is
#: generic vocabulary and can never become a hub, no matter how cohesive its
#: document set looks -- pure document arithmetic, no lexicons.
_HUB_MAX_DF_SHARE = 0.5


def _token_coverage(candidate_text: str, matched_text: str) -> float:
    """Fraction of ``matched_text``'s tokens restated by ``candidate_text``.

    Args:
        candidate_text: The newer statement (correction / failure report).
        matched_text: The existing node it may negate.

    Returns:
        ``|tokens(matched) & tokens(candidate)| / |tokens(matched)|`` in
        ``[0, 1]``; ``0.0`` when the matched node has no tokens.
    """
    old = set(_tokenize(matched_text))
    if not old:
        return 0.0
    new = set(_tokenize(candidate_text))
    return len(old & new) / len(old)


class MemoryCurator:
    """Post-run graph curator over a :class:`MemoryLayerStore`.

    Args:
        store: The memory graph store the curator evolves.
        recall_engine: Optional recall engine used to locate the existing
            nodes a candidate corrects or corroborates.  Without one,
            matching falls back to the store's consolidator (exact/dedup
            match), and every feedback signal that needs similarity becomes
            inactive -- never an error.
        reinforce_delta: Confidence bump applied on positive outcomes.
        weaken_delta: Confidence cut applied on corrections/failures.
        skill_confidence: Lesson confidence threshold for skill candidacy.
        skill_min_uses: Lesson access threshold for skill candidacy.
    """

    def __init__(
        self,
        store: MemoryLayerStore,
        *,
        recall_engine: Any = None,
        reinforce_delta: float = DEFAULT_REINFORCE_DELTA,
        weaken_delta: float = DEFAULT_WEAKEN_DELTA,
        skill_confidence: float = DEFAULT_SKILL_CONFIDENCE,
        skill_min_uses: int = DEFAULT_SKILL_MIN_USES,
    ) -> None:
        self._store = store
        self._recall_engine = recall_engine
        # Outcome feedback matches on direct content similarity plus the
        # entity-hub family expansion: a correction reaches the belief it
        # contradicts through the statistical entity they share (one
        # entity-scoped step), never through free associative spreading.
        self._match_engine: Any = None
        try:
            from encre.memdir.recall import RecallEngine

            self._match_engine = RecallEngine(store, record_access=False, max_hops=0)
        except Exception:
            self._match_engine = None
        self._reinforce_delta = max(0.0, float(reinforce_delta))
        self._weaken_delta = max(0.0, float(weaken_delta))
        self._skill_confidence = float(skill_confidence)
        self._skill_min_uses = max(1, int(skill_min_uses))
        # session_id -> list of observed candidate dicts, in arrival order.
        self._session_candidates: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._attached: set[int] = set()
        self._bus: Any = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def store(self) -> MemoryLayerStore:
        """The store this curator evolves."""
        return self._store

    # ------------------------------------------------------------------
    # Bus integration
    # ------------------------------------------------------------------

    def attach(self, bus: Any) -> None:
        """Observe the backbone: buffer candidates, curate on turn end.

        Idempotent per bus instance; a malformed payload or a failing pass is
        logged, never raised into the publisher.

        Args:
            bus: The event backbone (``subscribe(kind, handler)`` and
                ``publish(kind, session_id=..., payload=...)`` semantics).
        """
        if bus is None or id(bus) in self._attached:
            return
        self._attached.add(id(bus))
        self._bus = bus
        try:
            bus.subscribe("*", self._on_event)
        except Exception:
            logger.warning("[memdir.curator] failed to subscribe to bus", exc_info=True)

    def _on_event(self, event: Any) -> None:
        """Bus observer: collect candidates, curate when a turn ends."""
        try:
            kind = str(getattr(event, "kind", "") or "")
            payload = getattr(event, "payload", None)
            session_id = str(getattr(event, "session_id", "") or "")
            if kind == "memory_candidate" and isinstance(payload, dict):
                self.note_candidate(MemoryCandidate.from_dict(payload))
            elif kind == CURATE_TRIGGER_EVENT and session_id:
                self.curate(session_id)
        except Exception:
            logger.warning("[memdir.curator] event handling failed", exc_info=True)

    def note_candidate(self, candidate: MemoryCandidate) -> None:
        """Record one judged candidate for the session's curation pass."""
        content = str(candidate.content or "").strip()
        if not content:
            return
        with self._lock:
            self._session_candidates.setdefault(candidate.session_id, []).append(candidate.to_dict())

    # ------------------------------------------------------------------
    # Curation pass
    # ------------------------------------------------------------------

    def curate(self, session_id: str) -> dict[str, Any]:
        """Run one curation pass for ``session_id``.

        Applies outcome feedback onto the existing nodes the session's
        candidates corrected or corroborated, wires ``co_occurs`` edges
        between the session's distinct candidate nodes, and announces lesson
        nodes that earned skill candidacy.

        The corpus-wide semantic passes (entity hubs, cross-corpus
        ``related``, weak-edge pruning) run even when the session produced
        no candidates: they keep the graph's link set current with anything
        intake or consolidation added since the last turn.  Skipping them
        here is what leaves visible-but-unlinked nodes stranded in the
        graph -- the corpus grew, but no turn happened to name a new
        candidate, so nothing rewired.

        Args:
            session_id: The session whose buffered candidates to curate.

        Returns:
            A summary dict with the applied change counts (also logged).
        """
        with self._lock:
            candidates = self._session_candidates.pop(session_id, [])

        # Refresh the statistical lexicon before any pass tokenizes: hub
        # nomination, similarity wiring and pruning all share _tokenize, and
        # a stale bigram-only lexicon would shred 3+ char words ("李晓明" ->
        # "李晓"/"晓明") in every one of those passes.  Model-assisted
        # channel: candidates created via memory_create may carry
        # metadata["model_keywords"]; refresh_lexicon admits each only after
        # corpus validation, so a hallucinated term never reaches the
        # tokenizer.
        assisted: set[str] = set()
        for e in self._store.long_term() + self._store.buffer():
            for kw in (getattr(e, "metadata", None) or {}).get("model_keywords") or []:
                assisted.add(str(kw))
        refresh_lexicon(
            (e.content for e in self._store.long_term() + self._store.buffer()),
            assisted=sorted(assisted),
        )

        summary: dict[str, Any] = {
            "session_id": session_id,
            "candidates": len(candidates),
            "reinforced": [],
            "weakened": [],
            "contradictions": [],
            "co_occurs_edges": 0,
            "skill_candidates": [],
        }
        # Entity hubs and cross-corpus related edges are corpus-global, so
        # they run unconditionally; co-occurrence is session-scoped and only
        # runs when this turn actually produced candidate nodes.
        summary["entity_hubs"] = self._wire_entities()
        summary["related_edges"] = self._wire_related()
        # Prune AFTER wiring: fresh edges pass their own gates, so pruning
        # only removes links inherited from an older (looser) regime.
        summary["pruned_edges"] = self._prune_weak_links()
        if candidates:
            session_nodes = self._collect_session_nodes(candidates)
            feedback = self._apply_outcome_feedback(candidates)
            summary["reinforced"] = feedback["reinforced"]
            summary["weakened"] = feedback["weakened"]
            summary["contradictions"] = feedback["contradictions"]
            # Wire the episode's associations only after judgement: otherwise
            # the fresh co_occurs edges would let feedback spread onto
            # innocent neighbours the candidate merely occurred beside.
            summary["co_occurs_edges"] = self._wire_co_occurrence(session_nodes)
            summary["skill_candidates"] = self._announce_skill_candidates()
        try:
            self._store.export_markdown()
        except Exception:
            logger.debug("[memdir.curator] markdown projection failed", exc_info=True)
        logger.info(
            "[memdir.curator] curated session={s} candidates={c} hubs={h} related={r} pruned={p} co_occurs={e} skills={k}",
            s=session_id,
            c=len(candidates),
            h=summary.get("entity_hubs", 0),
            r=summary.get("related_edges", 0),
            p=summary.get("pruned_edges", 0),
            e=summary["co_occurs_edges"],
            k=len(summary["skill_candidates"]),
        )
        return summary

    # ------------------------------------------------------------------
    # Outcome feedback
    # ------------------------------------------------------------------

    def _apply_outcome_feedback(self, candidates: list[dict[str, Any]]) -> dict[str, list[str]]:
        """Move confidence of existing nodes per the session's signals."""
        reinforced: list[str] = []
        weakened: list[str] = []
        contradictions: list[str] = []
        for candidate in candidates:
            signals = candidate.get("metadata", {}).get("signals", {})
            if not isinstance(signals, dict):
                signals = {}
            firing = set(signals)
            if not (firing & (set(_REINFORCE_SIGNALS) | set(_WEAKEN_SIGNALS))):
                continue
            source_node = self._find_candidate_node(candidate)
            matches = self._match_existing(candidate.get("content", ""))
            if not matches:
                continue
            # Weakening and contradicts edges demand direct evidence: either
            # a hops=0 lexical match, or a hub-reached node whose token core
            # the candidate restates (coverage >= _COVER_MIN).  Entity-hub
            # reach alone means "same subject" -- never "contradicts" -- so
            # hub-distant nodes may be reinforced but are never weakened.
            correcting = "user_correction" in firing
            failing = "error_or_surprise" in firing
            supporting = bool(firing & set(_REINFORCE_SIGNALS))
            candidate_content = str(candidate.get("content", "") or "")
            for matched, hops in matches:
                if source_node is not None and matched.id == source_node.id:
                    continue
                covered = hops == 0 or (_token_coverage(candidate_content, matched.content) >= _COVER_MIN)
                if correcting and covered:
                    delta = -self._weaken_delta
                    reason = "corrected by newer evidence"
                    self._store.adjust_confidence(
                        matched.id,
                        delta,
                        reason=f"{reason} (candidate {candidate.get('content', '')[:60]!r})",
                    )
                    weakened.append(matched.id)
                    # A correction is an explicit contradiction: the newer
                    # belief gets a typed edge onto the older one so the
                    # arbitration is visible in the graph, not just implicit
                    # in the score.
                    if source_node is not None:
                        self._store.link(
                            source_node.id,
                            matched.id,
                            LinkRelation.CONTRADICTS.value,
                        )
                        contradictions.append(matched.id)
                elif failing and matched.memory_type is MemoryType.LESSON:
                    # The same lesson proving out again is confirmation,
                    # not blame: the failure evidence strengthens it.
                    self._store.adjust_confidence(
                        matched.id,
                        self._reinforce_delta,
                        reason=(f"recurrence confirms this lesson (candidate {candidate.get('content', '')[:60]!r})"),
                    )
                    reinforced.append(matched.id)
                elif failing and hops == 0:
                    delta = -self._weaken_delta
                    self._store.adjust_confidence(
                        matched.id,
                        delta,
                        reason=(f"implicated in a failure (candidate {candidate.get('content', '')[:60]!r})"),
                    )
                    weakened.append(matched.id)
                elif supporting:
                    self._store.adjust_confidence(
                        matched.id,
                        self._reinforce_delta,
                        reason=f"supported a decision (candidate {candidate.get('content', '')[:60]!r})",
                    )
                    reinforced.append(matched.id)
        return {"reinforced": reinforced, "weakened": weakened, "contradictions": contradictions}

    def _match_existing(self, content: str, top_k: int = 3) -> list[tuple[MemoryEntry, int]]:
        """Existing nodes most similar to ``content``, as ``(entry, hops)``.

        ``hops`` is 0 for a direct lexical match and > 0 when the node was
        reached only through the entity-hub family expansion.
        """
        text = str(content or "").strip()
        if not text:
            return []
        engine = self._match_engine or self._recall_engine
        if engine is not None:
            try:
                hits = engine.recall(text, top_k=top_k, record_access=False)
            except Exception:
                logger.debug("[memdir.curator] recall matching failed", exc_info=True)
                hits = []
            # A recall hit below the floor is noise, not a real match:
            # weakening a weakly-similar node on a hunch would let every
            # correction corrupt unrelated parts of the graph.
            return [(hit.entry, int(hit.hops)) for hit in hits or [] if hit.entry is not None and hit.score >= _MATCH_MIN_SCORE]
        # No engine: fall back to the store's own dedup matcher so exact
        # duplicates are still found (similarity-based feedback then simply
        # stays inactive -- degraded, never broken).
        probe = MemoryEntry(content=text)
        pool = self._store.long_term() + self._store.buffer()
        matched = self._store.consolidator.find_entry_match(probe, pool)
        return [(matched, 0)] if matched is not None else []

    def _find_candidate_node(self, candidate: dict[str, Any]) -> MemoryEntry | None:
        """Locate the graph node a stored candidate produced (if it exists)."""
        refs = [str(r) for r in (candidate.get("source_refs") or [])]
        if refs:
            for entry in self._store.long_term() + self._store.buffer():
                if any(ref in entry.source_refs for ref in refs):
                    return entry
        return None

    # ------------------------------------------------------------------
    # Edge wiring
    # ------------------------------------------------------------------

    def _collect_session_nodes(self, candidates: list[dict[str, Any]]) -> list[MemoryEntry]:
        """Distinct graph nodes the session's candidates produced."""
        nodes: list[MemoryEntry] = []
        seen: set[str] = set()
        for candidate in candidates:
            node = self._find_candidate_node(candidate)
            if node is None or node.id in seen:
                continue
            seen.add(node.id)
            nodes.append(node)
        return nodes

    # ------------------------------------------------------------------
    # IDF-weighted document vectors (shared by every edge pass)
    # ------------------------------------------------------------------

    @staticmethod
    def _idf_index(pool: list[MemoryEntry]):
        """IDF-weighted unit-normalised vectors for every pool entry.

        Returns ``(doc_tokens, idf, vecs)`` where ``doc_tokens`` pairs each
        entry with its token set, ``idf`` maps token -> inverse document
        frequency over the pool, and ``vecs`` pairs each entry with its
        weight vector and L2 norm.  Shared by wiring and pruning so both
        passes score pairs with the exact same arithmetic.
        """
        n_docs = len(pool)
        df: Counter[str] = Counter()
        doc_tokens: list[tuple[MemoryEntry, set[str]]] = []
        for entry in pool:
            tokens = set(_tokenize(entry.content))
            doc_tokens.append((entry, tokens))
            df.update(tokens)
        idf = {t: math.log((n_docs + 1) / (c + 1)) for t, c in df.items()}
        vecs: list[tuple[MemoryEntry, dict[str, float], float]] = []
        for entry, tokens in doc_tokens:
            weights = {t: (idf.get(t, 0.0) + 1e-9) for t in tokens}
            norm = math.sqrt(sum(w * w for w in weights.values()))
            vecs.append((entry, weights, norm))
        return doc_tokens, idf, vecs

    @staticmethod
    def _cosine(wa: dict[str, float], na: float, wb: dict[str, float], nb: float) -> float:
        """IDF-weighted cosine between two prepared vectors."""
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        dot = 0.0
        for t, w in wa.items():
            w2 = wb.get(t)
            if w2 is not None:
                dot += w * w2
        return dot / (na * nb)

    def _wire_co_occurrence(self, session_nodes: list[MemoryEntry]) -> int:
        """Link the session's nodes pairwise with ``co_occurs`` edges.

        Nodes that occurred in one episode are one associative neighbourhood;
        linking them here is what lets a future recall of any one of them
        pull in the rest of the episode (the spreading-activation half of
        "recalling one brings up the cluster").  Co-occurrence is evidence,
        but not proof: a pair still has to share subject matter at
        :data:`_CO_OCCURS_MIN_COSINE` (half the cross-corpus threshold),
        otherwise the edge would tie together unrelated topics that merely
        shared a conversation -- exactly the "related without being related"
        noise on the graph.
        """
        if len(session_nodes) < 2:
            return 0
        _doc_tokens, _idf, vecs = self._idf_index(session_nodes)
        wired = 0
        for i, (source, wa, na) in enumerate(vecs):
            for target, wb, nb in vecs[i + 1 :]:
                if self._cosine(wa, na, wb, nb) < _CO_OCCURS_MIN_COSINE:
                    continue
                try:
                    result = self._store.link(
                        source.id,
                        target.id,
                        LinkRelation.CO_OCCURS.value,
                    )
                except Exception:
                    logger.warning(
                        "[memdir.curator] co_occurs wiring failed",
                        exc_info=True,
                    )
                    continue
                if result is not None:
                    wired += 1
        return wired

    def _wire_related(self) -> int:
        """Cross-corpus semantic edges over the whole visible pool.

        Sessions isolate their ``co_occurs`` wiring to the nodes that
        occurred together, so two beliefs about the same subject produced in
        different sessions never meet.  This pass closes that gap with pure
        distributional math: every visible pair is scored by an IDF-weighted
        cosine over token sets (rare shared tokens dominate, common
        vocabulary cancels out) and pairs at or above
        :data:`_RELATED_MIN_COSINE` are linked ``co_occurs``.  Idempotent:
        pairs already joined in either direction are skipped.

        Returns:
            The number of newly wired edges.
        """
        pool = self._store.visible_entries()
        if len(pool) < 2:
            return 0
        _doc_tokens, _idf, vecs = self._idf_index(pool)
        wired = 0
        for i, (a, wa, na) in enumerate(vecs):
            if na <= 0.0:
                continue
            a_linked = {lk.target_id for lk in a.links}
            for b, wb, nb in vecs[i + 1 :]:
                if nb <= 0.0 or b.id in a_linked or a.id in {lk.target_id for lk in b.links}:
                    continue
                if self._cosine(wa, na, wb, nb) < _RELATED_MIN_COSINE:
                    continue
                try:
                    result = self._store.link(
                        a.id,
                        b.id,
                        LinkRelation.CO_OCCURS.value,
                    )
                except Exception:
                    logger.debug(
                        "[memdir.curator] related wiring failed %s->%s",
                        a.id,
                        b.id,
                        exc_info=True,
                    )
                    continue
                if result is not None:
                    wired += 1
        return wired

    def _prune_weak_links(self) -> int:
        """Drop ``co_occurs`` edges whose endpoints no longer share a subject.

        Statistical wiring quality is defined by thresholds; the corpus
        evolves (content updated, thresholds tightened), so edges wired under
        an older regime would linger forever as noise.  Every visible
        ``co_occurs`` edge is re-scored with the same IDF-weighted cosine and
        removed when it falls below :data:`_CO_OCCURS_MIN_COSINE` -- the
        gate a same-session edge must pass today.  Structural relations
        (``mentions`` hubs, ``supersedes`` lineage, judgement-written edges)
        are never touched here.
        """
        pool = self._store.visible_entries()
        if len(pool) < 2:
            return 0
        _doc_tokens, _idf, vecs = self._idf_index(pool)
        by_id = {entry.id: (weights, norm) for entry, weights, norm in vecs}
        pruned = 0
        for entry, weights, norm in vecs:
            if norm <= 0.0:
                continue
            weak: list[str] = []
            for edge in entry.links:
                if edge.relation != LinkRelation.CO_OCCURS.value:
                    continue
                neighbour = by_id.get(edge.target_id)
                if neighbour is None:
                    continue
                wb, nb = neighbour
                if self._cosine(weights, norm, wb, nb) < _CO_OCCURS_MIN_COSINE:
                    weak.append(edge.target_id)
            for target_id in weak:
                try:
                    if self._store.unlink(
                        entry.id,
                        target_id,
                        LinkRelation.CO_OCCURS.value,
                    ):
                        pruned += 1
                except Exception:
                    logger.debug(
                        "[memdir.curator] weak-link prune failed %s->%s",
                        entry.id,
                        target_id,
                        exc_info=True,
                    )
        return pruned

    # ------------------------------------------------------------------
    # Skill candidacy (lesson -> verified skill, via the existing gate)
    # ------------------------------------------------------------------

    def _wire_entities(self) -> int:
        """Statistical entity hubs over the whole visible corpus.

        Pure distributional math: a token recurring in ``_ENTITY_MIN_DF`` or
        more **natural-language** documents (episode/fact/preference/
        decision) is a tracked subject.  The natural-df gate is what keeps
        boilerplate out: template-generated lessons share a fixed phrase
        stock whose tokens reach high raw df without ever naming a real
        subject, and without the gate they would crowd the top-8 slots and
        wire every lesson into one meaningless blob.  Hubs are ranked by
        ``df * idf`` (recurring but discriminative), materialised as
        ``entity`` nodes, and every visible node containing the token is
        linked to the hub with a ``mentions`` edge.  Recall treats hubs as
        index nodes and pulls in a seed's whole family in one burst.
        """
        pool = self._store.visible_entries()
        if not pool:
            return 0
        n_docs = len(pool)
        n_natural = sum(1 for entry in pool if entry.type in _NATURAL_TYPES)
        lexicon = _judgment._LEXICON_SET
        df: Counter[str] = Counter()
        df_natural: Counter[str] = Counter()
        term_freq: Counter[str] = Counter()
        doc_tokens: list[tuple[MemoryEntry, set[str]]] = []
        for entry in pool:
            tokens = _tokenize(entry.content)
            # Sub-word inheritance: when segmentation consumed a longer
            # lexicon word ("和晓明"), its lexicon-confirmed sub-words
            # ("晓明") still occurred -- count them, otherwise a productive
            # collocation would starve the entity hub of its subject.
            expanded = list(tokens)
            for tok in set(tokens):
                if tok in lexicon and len(tok) >= 3:
                    for start in range(len(tok) - 1):
                        sub = tok[start : start + 2]
                        if sub != tok and sub in lexicon:
                            expanded.append(sub)
            term_freq.update(expanded)
            token_set = set(expanded)
            doc_tokens.append((entry, token_set))
            df.update(token_set)
            if entry.type in _NATURAL_TYPES:
                df_natural.update(token_set)
        # Rank by repetition-aware salience: (avg occurrences per document
        # containing the token) x idf.  A subject is discussed at length --
        # "李晓明" averages ~16 mentions per document -- while conversational
        # vocabulary ("然后" / "看着") passes by roughly once per document,
        # so raw document frequency alone cannot separate them; weighting by
        # in-document repetition does.  idf keeps ubiquitous tokens down.
        ranked = sorted(
            (
                (
                    token,
                    (term_freq[token] / count) * math.log((n_docs + 1) / (count + 1)),
                )
                for token, count in df.items()
                if len(token) >= 2
                and token in lexicon
                and not token.isdigit()
                and count >= _ENTITY_MIN_DF
                and df_natural[token] >= _ENTITY_MIN_DF
                and count <= _HUB_NATURAL_DF_SHARE * df_natural[token]
                and df_natural[token] <= _HUB_MAX_DF_SHARE * n_natural
            ),
            key=lambda item: -item[1],
        )
        # Cohesion gate: ranking by df*idf alone still promotes generic
        # conversational vocabulary ("用户" / "今天" / "工具") whose raw
        # statistics look identical to a real subject's.  The distributional
        # property that actually separates them is whether the documents
        # containing the token form ONE subject or a random sample of the
        # corpus: a true subject's documents share the token's surrounding
        # content, so their pairwise IDF-weighted cosine is high, while a
        # generic token's documents agree on nothing else.  A candidate is
        # promoted only when its document set is cohesive at the same
        # threshold that defines "same subject" for edges
        # (:data:`_RELATED_MIN_COSINE`) -- one definition, no lexicons.
        doc_vecs: list[tuple[dict[str, float], float]] = []
        for _entry, tokens in doc_tokens:
            weights = {t: (math.log((n_docs + 1) / (df[t] + 1)) + 1e-9) for t in tokens}
            norm = math.sqrt(sum(w * w for w in weights.values()))
            doc_vecs.append((weights, norm))

        def _cohesion(token: str) -> float:
            """Mean pairwise cosine of the documents containing ``token``."""
            idxs = [i for i, (_e, tokens) in enumerate(doc_tokens) if token in tokens]
            if len(idxs) < 2:
                return 0.0
            total = 0.0
            pairs = 0
            for i, ai in enumerate(idxs):
                wa, na = doc_vecs[ai]
                if na <= 0.0:
                    continue
                for bi in idxs[i + 1 :]:
                    wb, nb = doc_vecs[bi]
                    if nb <= 0.0:
                        continue
                    dot = sum(w * wb.get(t, 0.0) for t, w in wa.items())
                    total += dot / (na * nb)
                    pairs += 1
            return total / pairs if pairs else 0.0

        hubs = {}
        for token, _score in ranked:
            if len(hubs) >= _ENTITY_TOP_TOKENS:
                break
            if _cohesion(token) < _RELATED_MIN_COSINE:
                continue
            hub = self._hub_for(token)
            if hub is not None:
                hubs[token] = hub
        # Retire every hub outside this pass's top-_ENTITY_TOP_TOKENS
        # promotion.  Hub count is then a stable-state invariant (at most
        # _ENTITY_TOP_TOKENS live hubs at any time) instead of an ever
        # growing graveyard of shredded leftovers ("个男" / "晓天") and
        # single-character strays.  Retiring is safe: the node is archived,
        # not deleted, and _hub_for rebuilds a fresh one if the token is
        # promoted again later.
        for hub in self._store.by_type(MemoryType.ENTITY):
            if hub.decay.archived or hub.superseded_by:
                continue
            token = (hub.metadata or {}).get("hub_token")
            if token and token in hubs:
                continue
            for edge in list(hub.links):
                if edge.relation == LinkRelation.MENTIONS.value:
                    try:
                        self._store.unlink(hub.id, edge.target_id, edge.relation)
                    except Exception:
                        logger.opt(exception=True).debug(
                            "[memdir.curator] hub retire unlink failed hub={}",
                            hub.id,
                        )
            self._store.archive_entry(hub.id, reason="outside_top_hubs")
        wired = 0
        for entry, tokens in doc_tokens:
            if entry.type == MemoryType.ENTITY.value:
                continue
            for token, hub in hubs.items():
                if token not in tokens or entry.id == hub.id:
                    continue
                if any(link.target_id == entry.id for link in hub.links):
                    continue
                try:
                    self._store.link(hub.id, entry.id, LinkRelation.MENTIONS.value)
                except Exception:
                    logger.debug(
                        "[memdir.curator] hub link failed hub=%s entry=%s",
                        hub.id,
                        entry.id,
                        exc_info=True,
                    )
                    continue
                wired += 1
        return wired

    def _hub_for(self, token: str) -> MemoryEntry | None:
        """Get or create the ``entity`` hub node for one recurring token."""
        for entry in self._store.by_type(MemoryType.ENTITY):
            if entry.decay.archived or entry.superseded_by:
                continue
            if (entry.metadata or {}).get("hub_token") == token:
                return entry
        hub = MemoryEntry(
            content=token,
            type=MemoryType.ENTITY.value,
            importance=0.7,
            source="entity_hub",
            metadata={"hub_token": token},
        )
        try:
            self._store.store(hub)
        except Exception:
            logger.debug(
                "[memdir.curator] hub creation failed token=%s",
                token,
                exc_info=True,
            )
            return None
        return hub

    def _announce_skill_candidates(self) -> list[str]:
        """Stamp and announce ``lesson`` nodes that earned verification.

        A lesson qualifies when evolution has proven it: ``confidence`` at or
        above the threshold *and* enough distinct usages.  Qualifying nodes
        are stamped (idempotent) and announced as ``skill_candidate`` events;
        actual promotion stays owned by the verification stack.
        """
        announced: list[str] = []
        now = time.time()
        for entry in self._store.by_type(MemoryType.LESSON):
            if entry.decay.archived or entry.superseded_by:
                continue
            if entry.metadata.get("skill_name"):
                continue  # already a verified skill's node
            if entry.metadata.get("skill_candidate"):
                continue  # already announced
            if entry.confidence < self._skill_confidence or entry.decay.access_count < self._skill_min_uses:
                continue
            entry.metadata["skill_candidate"] = True
            entry.metadata["skill_candidate_at"] = now
            entry.version += 1
            self._store.store(entry)
            announced.append(entry.id)
            self._publish_skill_candidate(entry)
        return announced

    def _publish_skill_candidate(self, entry: MemoryEntry) -> None:
        """Announce one skill candidate on the backbone (never raises)."""
        if self._bus is None:
            return
        try:
            self._bus.publish(
                SKILL_CANDIDATE_EVENT,
                session_id=str(entry.metadata.get("session_id", "") or ""),
                payload={
                    "entry_id": entry.id,
                    "content": entry.content,
                    "confidence": entry.confidence,
                    "access_count": entry.decay.access_count,
                    "source": entry.source,
                },
                refs=list(entry.source_refs),
            )
        except Exception:
            logger.warning(
                "[memdir.curator] failed to publish skill_candidate",
                exc_info=True,
            )
