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

import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, ClassVar

from encre.memdir.judgment import _fingerprint

"""Local, dependency-free semantic memory retrieval and consolidation.

Retrieval is intentionally modelled with classical TF-IDF weighting plus a
Jaccard-similarity fallback instead of downloading an embedding model. The
higher-level prompt construction (see :mod:`encre.memdir.system`) may still
ask the active session backend to rerank results, but no internal model is
initialised here. This module also provides a lightweight :class:`WorkingMemory`
scratchpad and a :class:`MemoryConsolidator` that detects duplicate,
conflicting, and stale memory files.
"""


def _tokenize(text: str) -> list[str]:
    """Split text into normalised tokens for similarity scoring.

    Lowercases the input and extracts runs of at least two alphanumeric or
    underscore characters. CJK characters (Unicode range U+4E00-U+9FFF) are
    split into overlapping bigrams so that both English and Chinese memory
    content contribute to the vocabulary.

    Args:
        text: Raw text to tokenize.

    Returns:
        List of lowercased token strings.
    """
    text = text.lower()
    tokens: list[str] = []

    # Alphanumeric tokens (words separated by spaces/punctuation)
    tokens.extend(re.findall(r"[A-Za-z0-9_]{2,}", text))

    # CJK bigrams: split each run of CJK characters into overlapping pairs
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) >= 2:
            for i in range(len(run) - 1):
                tokens.append(run[i:i + 2])
        elif len(run) == 1:
            tokens.append(run)

    return tokens


def _build_idf(corpus: list[str]) -> dict[str, float]:
    """Compute smoothed inverse-document-frequency weights for a corpus.

    Uses the smoothed formula ``log((n+1)/(df+1)) + 1`` so terms that
    appear in every document still retain a small positive weight.

    Args:
        corpus: List of document strings.

    Returns:
        Mapping from term to its IDF weight. Empty for an empty corpus.
    """
    n = len(corpus)
    if n == 0:
        return {}
    df: dict[str, int] = {}
    for doc in corpus:
        seen: set[str] = set()
        # Count document frequency: each term counts at most once per doc
        for token in _tokenize(doc):
            if token not in seen:
                df[token] = df.get(token, 0) + 1
                seen.add(token)
    # Smoothed IDF keeps a small positive weight for ubiquitous terms
    return {term: math.log((n + 1) / (count + 1)) + 1.0 for term, count in df.items()}


def _tf_idf_vectorize(doc: str, idf: dict[str, float], vocabulary: set[str]) -> dict[str, float]:
    """Convert a document into a TF-IDF term vector restricted to a vocabulary.

    Args:
        doc: Document text.
        idf: Pre-computed IDF weights from :func:`_build_idf`.
        vocabulary: Set of terms to keep; others are ignored.

    Returns:
        Sparse mapping from term to its TF-IDF weight.
    """
    tokens = _tokenize(doc)
    if not tokens:
        return {}
    tf = Counter(tokens)
    total = len(tokens)
    out: dict[str, float] = {}
    for term, count in tf.items():
        if term in vocabulary:
            out[term] = (count / total) * idf.get(term, 1.0)
    return out


def _cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """Return the cosine similarity between two sparse term vectors.

    Args:
        a: First term-weight vector.
        b: Second term-weight vector.

    Returns:
        Cosine similarity in ``[0, 1]``; ``0.0`` if either vector is empty
        or has zero magnitude.
    """
    if not a or not b:
        return 0.0
    keys = set(a) & set(b)
    dot = sum(a[k] * b[k] for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _jaccard_similarity(a: str, b: str) -> float:
    """Return the Jaccard similarity of the token sets of two texts.

    Useful as a fallback when TF-IDF vectors are too sparse to overlap.

    Args:
        a: First text.
        b: Second text.

    Returns:
        Jaccard coefficient in ``[0, 1]``; ``0.0`` if either side is empty.
    """
    sa = set(_tokenize(a))
    sb = set(_tokenize(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


@dataclass
class SearchResult:
    """A single memory retrieval hit returned by :class:`SemanticMemorySearch`."""
    file_name: str
    score: float
    snippet: str = ""
    memory_type: str = ""
    description: str = ""


class SemanticMemorySearch:
    """Pure local memory retrieval.

    This module intentionally does not initialize or download any separate
    embedding model. Retrieval is based on tf-idf with Jaccard fallback.
    Higher-level LLM reranking, if needed, should use the current session
    backend chosen by the user rather than an internal model.
    """

    def __init__(self, memory_dir: str) -> None:
        """Create a searcher bound to a memory directory on disk."""
        self._memory_dir = memory_dir
        self._corpus: dict[str, str] = {}
        self._idf: dict[str, float] = {}
        self._vocabulary: set[str] = set()
        self._dirty = True

    def index(self, files: dict[str, str]) -> None:
        """Build the in-memory corpus, IDF weights, and vocabulary.

        Args:
            files: Mapping of filename to file content to index.
        """
        self._corpus = dict(files)
        self._idf = _build_idf(list(files.values()))
        self._vocabulary = set(self._idf)
        self._dirty = False

    def search(self, query: str, top_k: int = 5, min_score: float = 0.05) -> list[SearchResult]:
        """Search memory files for the given query.

        First ranks files by cosine similarity of TF-IDF vectors; if no
        file clears ``min_score``, falls back to Jaccard token overlap.

        Args:
            query: Free-text query string.
            top_k: Maximum number of results to return.
            min_score: Minimum similarity threshold for inclusion.

        Returns:
            Up to ``top_k`` :class:`SearchResult` objects, highest first.
        """
        if self._dirty:
            # Lazy (re)index when files on disk may have changed
            self._rebuild_from_disk()
        if not self._corpus:
            return []

        results: list[SearchResult] = []
        q_vec = _tf_idf_vectorize(query, self._idf, self._vocabulary)
        if q_vec:
            for name, text in self._corpus.items():
                d_vec = _tf_idf_vectorize(text, self._idf, self._vocabulary)
                score = _cosine_similarity(q_vec, d_vec)
                if score >= min_score:
                    results.append(SearchResult(file_name=name, score=score, snippet=text[:200]))

        if not results:
            # Fall back to token-overlap similarity when TF-IDF finds nothing
            for name, text in self._corpus.items():
                score = _jaccard_similarity(query, text)
                if score >= min_score:
                    results.append(SearchResult(file_name=name, score=score, snippet=text[:200]))

        results.sort(key=lambda r: r.score, reverse=True)
        # Return only the top_k highest-scoring results
        return results[:top_k]

    def search_relevant(self, query: str, top_k: int = 5) -> list[SearchResult]:
        """Search using a stricter relevance threshold (0.10)."""
        return self.search(query, top_k=top_k, min_score=0.10)

    def _rebuild_from_disk(self) -> None:
        """Scan the memory directory and (re)index every ``.md`` file.

        Skips ``MEMORY.md``, dotfiles, underscore-prefixed internals, and
        non-markdown files, then delegates to :meth:`index`.
        Memory files are encrypted at rest; they are decrypted before indexing.
        """
        from encre.crypto import decrypt

        files: dict[str, str] = {}
        try:
            with os.scandir(self._memory_dir) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if entry.name == "MEMORY.md" or entry.name.startswith(".") or not entry.name.endswith(".md"):
                        continue
                    try:
                        with open(entry.path, encoding="utf-8") as f:
                            raw = f.read().strip()
                    except (OSError, UnicodeDecodeError):
                        continue
                    if not raw:
                        continue
                    # Legacy plaintext (starts with YAML frontmatter)
                    if raw.startswith("---"):
                        files[entry.name] = raw
                        continue
                    try:
                        files[entry.name] = decrypt(raw)
                    except Exception:
                        files[entry.name] = raw  # plaintext fallback (legacy / test)
        except OSError:
            pass
        self.index(files)


@dataclass
class WorkingMemory:
    """A lightweight scratchpad tracking the agent's active reasoning state.

    Holds the current goal, subgoals, hypotheses (with confirm/reject
    outcomes), findings, open questions, and a free-form scratchpad. It can
    be serialised to/from a plain dict for persistence between turns.
    """
    current_goal: str = ""
    subgoals: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    scratchpad: list[str] = field(default_factory=list)
    _created_at: float = field(default_factory=time.time)

    def set_goal(self, goal: str) -> None:
        """Replace the current goal with a new one."""
        self.current_goal = goal

    def add_subgoal(self, subgoal: str) -> None:
        """Record a subgoal, avoiding duplicates."""
        if subgoal not in self.subgoals:
            self.subgoals.append(subgoal)

    def complete_subgoal(self, subgoal: str) -> None:
        """Remove a subgoal once it has been accomplished."""
        if subgoal in self.subgoals:
            self.subgoals.remove(subgoal)

    def add_hypothesis(self, hypothesis: str) -> None:
        """Register a working hypothesis (deduplicated)."""
        if hypothesis not in self.hypotheses:
            self.hypotheses.append(hypothesis)

    def confirm_hypothesis(self, hypothesis: str) -> None:
        """Mark a hypothesis confirmed, moving it to findings."""
        if hypothesis in self.hypotheses:
            self.hypotheses.remove(hypothesis)
        if hypothesis not in self.findings:
            self.findings.append(f"[CONFIRMED] {hypothesis}")

    def reject_hypothesis(self, hypothesis: str) -> None:
        """Mark a hypothesis rejected, recording it in findings."""
        if hypothesis in self.hypotheses:
            self.hypotheses.remove(hypothesis)
        self.findings.append(f"[REJECTED] {hypothesis}")

    def add_finding(self, finding: str) -> None:
        """Append a finding (deduplicated)."""
        if finding not in self.findings:
            self.findings.append(finding)

    def add_question(self, question: str) -> None:
        """Record an open question (deduplicated)."""
        if question not in self.open_questions:
            self.open_questions.append(question)

    def resolve_question(self, question: str, answer: str = "") -> None:
        """Resolve an open question, recording it as a finding."""
        if question in self.open_questions:
            self.open_questions.remove(question)
        entry = f"Q: {question}"
        if answer:
            entry += f" -> {answer}"
        self.findings.append(entry)

    def note(self, text: str) -> None:
        """Append a free-form note to the scratchpad."""
        self.scratchpad.append(text)

    def summarize(self) -> str:
        """Render the working memory as a readable Markdown-ish summary.

        Returns:
            A multi-line summary, or ``"(empty working memory)"`` if empty.
        """
        parts: list[str] = []
        if self.current_goal:
            parts.append(f"Goal: {self.current_goal}")
        if self.subgoals:
            parts.append("Subgoals:")
            parts.extend(f"  - {sg}" for sg in self.subgoals)
        if self.hypotheses:
            parts.append("Hypotheses:")
            parts.extend(f"  - {h}" for h in self.hypotheses)
        if self.findings:
            parts.append("Findings:")
            parts.extend(f"  - {f}" for f in self.findings[-10:])
        if self.open_questions:
            parts.append("Open questions:")
            parts.extend(f"  - {q}" for q in self.open_questions)
        if self.scratchpad:
            parts.append("Scratchpad:")
            parts.extend(f"  - {s}" for s in self.scratchpad[-5:])
        return "\n".join(parts) if parts else "(empty working memory)"

    def to_dict(self) -> dict[str, Any]:
        """Serialise the working memory to a plain dict for persistence."""
        return {
            "current_goal": self.current_goal,
            "subgoals": list(self.subgoals),
            "hypotheses": list(self.hypotheses),
            "findings": list(self.findings),
            "open_questions": list(self.open_questions),
            "scratchpad": list(self.scratchpad),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkingMemory":
        """Reconstruct a :class:`WorkingMemory` from a dict (see :meth:`to_dict`)."""
        wm = cls()
        wm.current_goal = d.get("current_goal", "")
        wm.subgoals = d.get("subgoals", [])
        wm.hypotheses = d.get("hypotheses", [])
        wm.findings = d.get("findings", [])
        wm.open_questions = d.get("open_questions", [])
        wm.scratchpad = d.get("scratchpad", [])
        return wm


@dataclass
class ConsolidationAction:
    """A proposed change to the memory set produced by the consolidator."""
    action: str
    file_a: str
    file_b: str = ""
    reason: str = ""
    merged_content: str = ""


class MemoryConsolidator:
    """Detect duplicate, conflicting, and stale memory files and propose fixes.

    The consolidator scans pairs of memory files for high textual overlap
    (duplicates), opposing claims (conflicts), and dangling file:line
    references (staleness), returning :class:`ConsolidationAction` objects
    that an operator or higher-level routine can apply or review.
    """
    SIMILARITY_THRESHOLD = 0.75
    CONFLICT_PATTERNS: ClassVar[list[tuple[str, str]]] = [
        (r"\b(?:do not|don't|never|should not|must not|avoid|forbidden|prohibited)\b",
         r"\b(?:do|always|should|must|use|prefer|recommended|allowed)\b"),
        (r"\b(?:remove|delete|drop|discard|abandon)\b",
         r"\b(?:keep|retain|preserve|maintain|add)\b"),
    ]

    def __init__(self, memory_dir: str) -> None:
        """Create a consolidator bound to a memory directory."""
        self._memory_dir = memory_dir

    def find_duplicates(self, files: dict[str, str]) -> list[ConsolidationAction]:
        """Find near-duplicate memory pairs and propose merging them.

        Any pair whose Jaccard similarity meets ``SIMILARITY_THRESHOLD``
        yields a ``merge`` action whose ``merged_content`` pre-computes the
        union of unique lines.

        Args:
            files: Mapping of filename to content.

        Returns:
            List of ``merge`` :class:`ConsolidationAction` objects.
        """
        actions: list[ConsolidationAction] = []
        names = list(files.keys())
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                score = _jaccard_similarity(files[names[i]], files[names[j]])
                if score >= self.SIMILARITY_THRESHOLD:
                    actions.append(ConsolidationAction(
                        action="merge",
                        file_a=names[i],
                        file_b=names[j],
                        reason=f"Jaccard similarity {score:.2f} >= {self.SIMILARITY_THRESHOLD}",
                        merged_content=self._merge_pair(files[names[i]], files[names[j]]),
                    ))
        return actions

    # ---- entry-level duplication (layered memory) ------------------------

    @staticmethod
    def entry_similarity(a: Any, b: Any) -> float:
        """Similarity of two memory entries in ``[0.0, 1.0]``.

        Identical normalised content short-circuits to ``1.0`` so that entries
        whose tokens are too short for Jaccard still deduplicate; otherwise the
        Jaccard token similarity is returned.  This is the *same* similarity
        core the file-level path uses, exposed for entry-based callers.

        Args:
            a: First entry (any object exposing a ``content`` attribute).
            b: Second entry (any object exposing a ``content`` attribute).

        Returns:
            Similarity in ``[0.0, 1.0]``.
        """
        text_a = str(getattr(a, "content", "") or "")
        text_b = str(getattr(b, "content", "") or "")
        if not text_a or not text_b:
            return 0.0
        if _fingerprint(text_a) == _fingerprint(text_b):
            return 1.0
        return _jaccard_similarity(text_a, text_b)

    def find_entry_match(self, entry: Any, pool: list[Any]) -> Any | None:
        """Return the first entry in ``pool`` that duplicates ``entry``.

        Entry-level counterpart of :meth:`find_duplicates`, reusing exactly the
        same similarity core and :attr:`SIMILARITY_THRESHOLD`.

        Args:
            entry: The candidate entry.
            pool: Existing entries to compare against.

        Returns:
            The first duplicate found, or ``None``.
        """
        for other in pool:
            if self.entry_similarity(entry, other) >= self.SIMILARITY_THRESHOLD:
                return other
        return None

    def find_entry_duplicates(self, entries: list[Any]) -> list[ConsolidationAction]:
        """Find near-duplicate entry pairs and propose merging them.

        Args:
            entries: Entries (any objects exposing ``id`` and ``content``).

        Returns:
            List of ``merge`` :class:`ConsolidationAction` objects whose
            ``file_a`` / ``file_b`` carry the entry ids.
        """
        actions: list[ConsolidationAction] = []
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                score = self.entry_similarity(entries[i], entries[j])
                if score >= self.SIMILARITY_THRESHOLD:
                    actions.append(ConsolidationAction(
                        action="merge",
                        file_a=str(getattr(entries[i], "id", i)),
                        file_b=str(getattr(entries[j], "id", j)),
                        reason=(
                            f"entry similarity {score:.2f} >= "
                            f"{self.SIMILARITY_THRESHOLD}"
                        ),
                        merged_content=self._merge_pair(
                            str(getattr(entries[i], "content", "")),
                            str(getattr(entries[j], "content", "")),
                        ),
                    ))
        return actions

    def find_conflicts(self, files: dict[str, str]) -> list[ConsolidationAction]:
        """Detect pairs of memories that make opposing claims.

        Only considers pairs with moderate textual overlap (Jaccard >= 0.30)
        and flags those whose phrasing matches :attr:`CONFLICT_PATTERNS`.

        Args:
            files: Mapping of filename to content.

        Returns:
            List of ``flag_conflict`` actions.
        """
        actions: list[ConsolidationAction] = []
        names = list(files.keys())
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a = files[names[i]]
                b = files[names[j]]
                if _jaccard_similarity(a, b) < 0.30:
                    continue
                if self._has_opposing_claims(a, b):
                    actions.append(ConsolidationAction(
                        action="flag_conflict",
                        file_a=names[i],
                        file_b=names[j],
                        reason="Opposing claims detected",
                    ))
        return actions

    def find_stale(self, files: dict[str, str], age_days: dict[str, int], stale_threshold_days: int = 30) -> list[ConsolidationAction]:
        """Flag old memories whose file:line references no longer resolve.

        Args:
            files: Mapping of filename to content.
            age_days: Mapping of filename to age in days.
            stale_threshold_days: Minimum age before references are checked.

        Returns:
            List of ``mark_stale`` actions listing missing referenced paths.
        """
        actions: list[ConsolidationAction] = []
        for name, text in files.items():
            days = age_days.get(name, 0)
            if days < stale_threshold_days:
                continue
            refs = re.findall(r"`([\w./-]+\.[\w]+):\d+`|`([\w./-]+\.[\w]+)`", text)
            missing: list[str] = []
            for ref_tuple in refs:
                path = ref_tuple[0] or ref_tuple[1]
                if path and not self._path_exists(path):
                    missing.append(path)
            if missing:
                actions.append(ConsolidationAction(
                    action="mark_stale",
                    file_a=name,
                    reason=f"References missing files: {', '.join(missing[:3])}",
                ))
        return actions

    def consolidate(self, files: dict[str, str], age_days: dict[str, int] | None = None) -> list[ConsolidationAction]:
        """Run all checks and return prioritised consolidation actions.

        Combines duplicates, conflicts, and (optionally) staleness results,
        then sorts them by action priority (merge < conflict < stale).

        Args:
            files: Mapping of filename to content.
            age_days: Optional filename-to-age mapping enabling staleness.

        Returns:
            A prioritised list of :class:`ConsolidationAction` objects.
        """
        actions = self.find_duplicates(files) + self.find_conflicts(files)
        if age_days:
            actions.extend(self.find_stale(files, age_days))
        priority = {"merge": 0, "flag_conflict": 1, "mark_stale": 2}
        actions.sort(key=lambda a: priority.get(a.action, 99))
        # Stale/conflict issues surface after duplicates for review order
        return actions

    @staticmethod
    def _merge_pair(text_a: str, text_b: str) -> str:
        """Merge two duplicate texts, keeping lines unique to the shorter one.

        The longer document is treated as primary; lines from the other that
        are not already present and longer than ten characters are appended
        under a "Merged from duplicate" heading.
        """
        primary = text_a if len(text_a) >= len(text_b) else text_b
        secondary = text_b if primary == text_a else text_a
        # Use the longer document as the base to minimise information loss
        pri_lines = set(primary.strip().split("\n"))
        unique = [line for line in secondary.strip().split("\n") if line not in pri_lines and len(line.strip()) > 10]
        if unique:
            primary += "\n\n## Merged from duplicate\n" + "\n".join(unique[:20])
        return primary

    def _has_opposing_claims(self, text_a: str, text_b: str) -> bool:
        """Return True if the two texts match any conflicting claim pattern."""
        for pos_pat, neg_pat in self.CONFLICT_PATTERNS:
            a_pos = bool(re.search(pos_pat, text_a, re.IGNORECASE))
            a_neg = bool(re.search(neg_pat, text_a, re.IGNORECASE))
            b_pos = bool(re.search(pos_pat, text_b, re.IGNORECASE))
            b_neg = bool(re.search(neg_pat, text_b, re.IGNORECASE))
            if (a_pos and b_neg) or (a_neg and b_pos):
                return True
        return False

    def _path_exists(self, rel_path: str) -> bool:
        """Check whether a memory-referenced path resolves from the CWD."""
        return os.path.exists(os.path.join(os.getcwd(), rel_path))

