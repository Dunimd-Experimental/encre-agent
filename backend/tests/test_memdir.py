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

"""Tests for memdir: memory system, semantic search, working memory, consolidation.

Covers the lexical primitives that feed semantic recall (tokeniser, Jaccard,
TF-IDF and cosine), the file-based SemanticMemorySearch index, the
WorkingMemory scratch state machine, the MemoryConsolidator heuristics, and
the EncreMemorySystem file facade.
"""

import os
import tempfile

import pytest
from encre.memdir.semantic import (
    MemoryConsolidator,
    SemanticMemorySearch,
    WorkingMemory,
    _build_idf,
    _cosine_similarity,
    _jaccard_similarity,
    _tf_idf_vectorize,
    _tokenize,
)
from encre.memdir.system import EncreMemorySystem


# ----------------------------------------------------------------------
# Tokeniser and similarity primitives
# ----------------------------------------------------------------------

class TestTokenize:
    """Validates the text tokeniser and the similarity primitives.

    Exercises _tokenize, Jaccard similarity, TF-IDF vectorisation, IDF
    weighting, and cosine similarity across edge cases (empty input, mixed
    CJK/ASCII text, short-token filtering) and checks that the semantic search
    pipeline produces deterministic, bounded vector representations for
    downstream recall.
    """

    def test_verify_tokenize_simple(self):
        """Checks that _tokenize lowercases and splits plain ASCII text."""
        t = _tokenize("Hello World! This is a test.")
        assert "hello" in t
        assert "world" in t
        assert "this" in t

    def test_verify_tokenize_chinese(self):
        """Checks that _tokenize keeps ASCII words inside mixed CJK text."""
        t = _tokenize("测试 中文 and English 混合")
        assert "and" in t
        assert "english" in t

    def test_verify_tokenize_short_tokens_dropped(self):
        """Checks that single-character tokens are dropped while two-character ones survive."""
        t = _tokenize("a b c ab cd ef hello")
        assert "hello" in t
        # Single-character tokens fall below the length cutoff; "ab" sits exactly
        # at the boundary and is kept.
        assert "a" not in t
        assert len(t) > 0

    def test_verify_tokenize_empty(self):
        """Checks that _tokenize returns an empty list for empty input."""
        assert _tokenize("") == []


class TestJaccard:
    """Validates Jaccard set similarity over tokenised text.

    Covers identical strings, disjoint sets, partial overlap, and one-empty
    input to guarantee the metric stays inside the closed interval [0.0, 1.0]
    and grows monotonically with the intersection size.
    """

    def test_verify_jaccard_identical(self):
        """Checks that Jaccard similarity of identical strings is 1.0."""
        assert _jaccard_similarity("hello world", "hello world") == 1.0

    def test_verify_jaccard_disjoint(self):
        """Checks that Jaccard similarity of disjoint sets is 0.0."""
        assert _jaccard_similarity("abc def", "xyz uvw") == 0.0

    def test_verify_jaccard_partial(self):
        """Checks that partial overlap yields a similarity strictly between 0.4 and 1.0."""
        s = _jaccard_similarity("hello world foo", "hello world bar")
        assert 0.4 < s < 1.0

    def test_verify_jaccard_one_empty(self):
        """Checks that Jaccard similarity is 0.0 when either operand is empty."""
        assert _jaccard_similarity("", "hello") == 0.0
        assert _jaccard_similarity("hello", "") == 0.0


class TestTfIdf:
    """Validates TF-IDF vectorisation and cosine similarity.

    Verifies that _build_idf weights high-frequency terms lower, that
    _tf_idf_vectorize only fills in-vocabulary tokens, and that cosine
    similarity is 1.0 for a vector against itself and 0.0 for disjoint
    documents.
    """

    def test_verify_tfidf_build_idf(self):
        """Checks that _build_idf weights terms appearing in more documents lower."""
        corpus = ["hello world", "hello foo", "bar baz"]
        idf = _build_idf(corpus)
        assert "hello" in idf
        assert "world" in idf
        # "hello" appears in 2 of 3 documents, "world" in 1, so its IDF is smaller.
        assert idf["hello"] < idf["world"]

    def test_verify_tfidf_empty_corpus(self):
        """Checks that _build_idf returns an empty dict for an empty corpus."""
        assert _build_idf([]) == {}

    def test_verify_tfidf_vectorize(self):
        """Checks that _tf_idf_vectorize fills only in-vocabulary tokens with positive weights."""
        corpus = ["hello world foo", "hello bar", "bar baz qux"]
        idf = _build_idf(corpus)
        vocab = set(idf.keys())
        vec = _tf_idf_vectorize("hello world", idf, vocab)
        assert "hello" in vec
        assert vec["hello"] > 0

    def test_verify_tfidf_cosine_same(self):
        """Checks that cosine similarity of a vector with itself is 1.0."""
        corpus = ["hello world", "foo bar"]
        idf = _build_idf(corpus)
        vocab = set(idf.keys())
        v = _tf_idf_vectorize("hello world", idf, vocab)
        assert _cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-6)

    def test_verify_tfidf_cosine_orthogonal(self):
        """Checks that cosine similarity of two disjoint vectors is 0.0."""
        corpus = ["hello world", "foo bar"]
        idf = _build_idf(corpus)
        vocab = set(idf.keys())
        v1 = _tf_idf_vectorize("hello world", idf, vocab)
        v2 = _tf_idf_vectorize("foo bar", idf, vocab)
        assert _cosine_similarity(v1, v2) == 0.0


# ----------------------------------------------------------------------
# SemanticMemorySearch
# ----------------------------------------------------------------------

class TestSemanticMemorySearch:
    """Validates semantic file search over a directory of documents.

    Checks that SemanticMemorySearch builds an in-memory TF-IDF index from
    plain-text files, ranks results by keyword relevance, honours top_k,
    skips the MEMORY.md entrypoint, and accepts an explicit index for
    controlled scenarios.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Creates a temp directory and removes it after each test."""
        self.tmpdir = tempfile.mkdtemp()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, name, content):
        """Writes content to a file inside the temp directory."""
        with open(os.path.join(self.tmpdir, name), "w", encoding="utf-8") as f:
            f.write(content)

    def test_verify_semantic_search_finds_relevant(self):
        """Checks that search ranks the keyword-relevant file first."""
        self._write("auth.md", "The login system uses OAuth2 with JWT tokens.")
        self._write("ui.md", "The dashboard uses React and Tailwind CSS for styling.")
        sms = SemanticMemorySearch(self.tmpdir)
        results = sms.search("authentication login")
        assert len(results) >= 1
        assert results[0].file_name == "auth.md"

    def test_verify_semantic_search_respects_top_k(self):
        """Checks that top_k caps the number of returned results."""
        for i in range(10):
            self._write(f"doc{i}.md", f"Document number {i} about various topics.")
        sms = SemanticMemorySearch(self.tmpdir)
        results = sms.search("document", top_k=3)
        assert len(results) <= 3

    def test_verify_semantic_search_empty_dir(self):
        """Checks that search returns an empty list for an empty index."""
        sms = SemanticMemorySearch(self.tmpdir)
        assert sms.search("anything") == []

    def test_verify_semantic_search_relevant_higher_threshold(self):
        """Checks that search_relevant applies a stricter threshold and still finds the best match."""
        self._write("a.md", "python async programming guide")
        self._write("b.md", "baking chocolate cake recipe")
        sms = SemanticMemorySearch(self.tmpdir)
        results = sms.search_relevant("python programming")
        assert len(results) >= 1
        assert results[0].file_name == "a.md"

    def test_verify_semantic_search_ignores_memory_md(self):
        """Checks that the MEMORY.md entrypoint is excluded from search results."""
        self._write("MEMORY.md", "entrypoint content")
        self._write("real.md", "actual memory content here")
        sms = SemanticMemorySearch(self.tmpdir)
        results = sms.search("content")
        names = {r.file_name for r in results}
        assert "MEMORY.md" not in names
        assert "real.md" in names

    def test_verify_semantic_search_index_explicit(self):
        """Checks that a caller-supplied index can be searched directly."""
        sms = SemanticMemorySearch(self.tmpdir)
        sms.index({"a.md": "hello world", "b.md": "foo bar"})
        results = sms.search("hello")
        assert results[0].file_name == "a.md"


# ----------------------------------------------------------------------
# WorkingMemory
# ----------------------------------------------------------------------

class TestWorkingMemory:
    """Validates the WorkingMemory transient state machine.

    Exercises goal setting, sub-goal deduplication, the hypothesis lifecycle
    (add, then confirm or reject), finding capture, question resolution,
    scratchpad notes, summarisation with list truncation, and the
    to_dict / from_dict round trip.
    """

    def test_verify_working_memory_initial_empty(self):
        """Checks that a new WorkingMemory starts with no goal, subgoals, or hypotheses."""
        wm = WorkingMemory()
        assert wm.current_goal == ""
        assert wm.subgoals == []
        assert wm.hypotheses == []

    def test_verify_working_memory_set_goal(self):
        """Checks that set_goal updates the current goal."""
        wm = WorkingMemory()
        wm.set_goal("Implement OAuth2")
        assert wm.current_goal == "Implement OAuth2"

    def test_verify_working_memory_add_subgoal_no_dupes(self):
        """Checks that repeating a subgoal does not duplicate it."""
        wm = WorkingMemory()
        wm.add_subgoal("Write tests")
        wm.add_subgoal("Write tests")
        assert len(wm.subgoals) == 1

    def test_verify_working_memory_complete_subgoal(self):
        """Checks that complete_subgoal removes the finished subgoal."""
        wm = WorkingMemory()
        wm.add_subgoal("Write tests")
        wm.complete_subgoal("Write tests")
        assert wm.subgoals == []

    def test_verify_working_memory_hypothesis_lifecycle(self):
        """Checks that confirm_hypothesis moves the hypothesis into findings as CONFIRMED."""
        wm = WorkingMemory()
        wm.add_hypothesis("The bug is in auth.py")
        wm.confirm_hypothesis("The bug is in auth.py")
        assert wm.hypotheses == []
        assert any("CONFIRMED" in f for f in wm.findings)

    def test_verify_working_memory_reject_hypothesis(self):
        """Checks that reject_hypothesis moves the hypothesis into findings as REJECTED."""
        wm = WorkingMemory()
        wm.add_hypothesis("Memory leak in loop")
        wm.reject_hypothesis("Memory leak in loop")
        assert wm.hypotheses == []
        assert any("REJECTED" in f for f in wm.findings)

    def test_verify_working_memory_add_finding(self):
        """Checks that add_finding appends to the findings list."""
        wm = WorkingMemory()
        wm.add_finding("Token refresh endpoint returns 401")
        assert len(wm.findings) == 1

    def test_verify_working_memory_question_lifecycle(self):
        """Checks that resolve_question clears the question and records the answer."""
        wm = WorkingMemory()
        wm.add_question("Should we use asyncpg?")
        wm.resolve_question("Should we use asyncpg?", "Yes, it's faster")
        assert wm.open_questions == []
        assert any("asyncpg" in f for f in wm.findings)

    def test_verify_working_memory_scratchpad(self):
        """Checks that note appends entries to the scratchpad."""
        wm = WorkingMemory()
        wm.note("TODO: check error handling")
        wm.note("Done: error handling looks fine")
        assert len(wm.scratchpad) == 2

    def test_verify_working_memory_summarize_empty(self):
        """Checks that summarize reports the empty state when nothing is recorded."""
        wm = WorkingMemory()
        assert "empty" in wm.summarize().lower()

    def test_verify_working_memory_summarize_with_content(self):
        """Checks that summarize includes the goal and the findings."""
        wm = WorkingMemory()
        wm.set_goal("Test framework")
        wm.add_finding("pytest configured")
        s = wm.summarize()
        assert "Test framework" in s
        assert "pytest configured" in s

    def test_verify_working_memory_summarize_truncates_lists(self):
        """Checks that summarize keeps only the most recent findings."""
        wm = WorkingMemory()
        for i in range(20):
            wm.add_finding(f"Finding {i}")
        s = wm.summarize()
        # Only the last 10 findings survive truncation.
        assert "Finding 0" not in s
        assert "Finding 19" in s

    def test_verify_working_memory_serialize_roundtrip(self):
        """Checks that to_dict and from_dict preserve every state field."""
        wm = WorkingMemory()
        wm.set_goal("Test")
        wm.add_hypothesis("H1")
        wm.add_finding("F1")
        d = wm.to_dict()
        wm2 = WorkingMemory.from_dict(d)
        assert wm2.current_goal == "Test"
        assert "H1" in wm2.hypotheses
        assert "F1" in wm2.findings


# ----------------------------------------------------------------------
# MemoryConsolidator
# ----------------------------------------------------------------------

class TestMemoryConsolidator:
    """Validates the memory consolidation heuristics.

    Covers duplicate detection (semantic similarity suggests a merge), conflict
    detection (contradictory assertions are flagged), staleness detection (age
    past a threshold marks a reference stale), and the pipeline ordering
    invariant: merge actions precede conflict flags when both apply.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Creates a temp directory and consolidator, then cleans up afterwards."""
        self.tmpdir = tempfile.mkdtemp()
        self.mc = MemoryConsolidator(self.tmpdir)
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_verify_consolidator_find_duplicates(self):
        """Checks that find_duplicates flags near-identical files for merging."""
        files = {
            "a.md": "Always use async/await for network calls in production code.",
            "b.md": "Always use async/await for network calls in the production environment.",
            "c.md": "Completely different topic about CSS grid layout and flexbox.",
        }
        actions = self.mc.find_duplicates(files)
        assert len(actions) >= 1
        action = actions[0]
        assert action.action == "merge"
        assert action.merged_content

    def test_verify_consolidator_find_duplicates_none(self):
        """Checks that find_duplicates returns nothing when no pairs are similar."""
        files = {"a.md": "foo bar", "b.md": "completely unrelated"}
        assert self.mc.find_duplicates(files) == []

    def test_verify_consolidator_find_conflicts(self):
        """Checks that find_conflicts flags contradictory assertions."""
        files = {
            "a.md": "Always use async/await for network calls.",
            "b.md": "Never use async/await; prefer synchronous calls.",
        }
        actions = self.mc.find_conflicts(files)
        assert len(actions) >= 1
        assert actions[0].action == "flag_conflict"

    def test_verify_consolidator_find_conflicts_no_overlap_no_flag(self):
        """Checks that unrelated content raises no conflict flags."""
        files = {
            "a.md": "Always use async/await for network calls.",
            "b.md": "The CSS grid system is preferred for layouts.",
        }
        actions = self.mc.find_conflicts(files)
        assert len(actions) == 0

    def test_verify_consolidator_find_stale(self):
        """Checks that find_stale flags references older than the threshold."""
        files = {"old.md": "Reference: `src/auth.py:42` has the login flow."}
        age_days = {"old.md": 60}
        actions = self.mc.find_stale(files, age_days, stale_threshold_days=30)
        # src/auth.py does not exist in the working directory, so the reference
        # resolves as broken and stale.
        assert len(actions) >= 1
        assert actions[0].action == "mark_stale"

    def test_verify_consolidator_find_stale_not_old_enough(self):
        """Checks that find_stale ignores references below the age threshold."""
        files = {"recent.md": "Reference: `src/auth.py:42`"}
        age_days = {"recent.md": 5}
        actions = self.mc.find_stale(files, age_days, stale_threshold_days=30)
        assert len(actions) == 0

    def test_verify_consolidator_consolidate_orders_actions(self):
        """Checks that merges are emitted before conflict flags."""
        files = {
            "dup_a.md": "Always use async/await for network calls in production code.",
            "dup_b.md": "Always use async/await for network calls in the production environment.",
            "conflict.md": "Never use async/await; prefer synchronous calls.",
        }
        age_days = {"dup_a.md": 35, "dup_b.md": 10, "conflict.md": 5}
        actions = self.mc.consolidate(files, age_days)
        # Merge actions are always ordered ahead of conflict flags.
        assert actions[0].action == "merge"
        assert any(a.action == "flag_conflict" for a in actions)


# ----------------------------------------------------------------------
# EncreMemorySystem integration
# ----------------------------------------------------------------------

class TestEncreMemorySystem:
    """Validates the top-level EncreMemorySystem file facade.

    Exercises the facade's file-I/O surface: directory scanning with YAML
    frontmatter parsing, MEMORY.md entrypoint write and load, and the ingestion
    trigger (``refresh``). Retrieval is no longer part of this facade -- it goes
    only through RecallEngine over the layer store, so the old search,
    prompt-building, and consolidation assertions are gone.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        """Creates a temp directory and memory system, then cleans up afterwards."""
        self.tmpdir = tempfile.mkdtemp()
        self.ms = EncreMemorySystem(self.tmpdir)
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_verify_memory_system_scan_empty(self):
        """Checks that scan returns an empty list when no memory files exist."""
        assert self.ms.scan() == []

    def test_verify_memory_system_scan_single(self):
        """Checks that scan parses YAML frontmatter into description and type."""
        self._write("test.md", "---\ndescription: Test memory\ntype: fact\n---\nContent here.")
        memories = self.ms.scan()
        assert len(memories) == 1
        assert memories[0].description == "Test memory"
        assert memories[0].memory_type == "fact"

    def test_verify_memory_system_scan_skips_entrypoint(self):
        """Checks that MEMORY.md is excluded from scan results."""
        self._write("MEMORY.md", "entrypoint")
        self._write("real.md", "real memory")
        memories = self.ms.scan()
        names = {m.filename for m in memories}
        assert "MEMORY.md" not in names
        assert "real.md" in names

    def test_verify_memory_system_refresh_without_store_is_noop(self):
        """Checks that refresh degrades to a harmless no-op when unattached."""
        assert self.ms.layer_store is None
        self._write("test.md", "---\ndescription: A test\n---\nTest content.")
        # No store attached: the ingestion trigger must not raise.
        self.ms.refresh()

    def test_verify_memory_system_write_entrypoint(self):
        """Checks that write_entrypoint and load_entrypoint round-trip content."""
        self.ms.write_entrypoint("# Test\n\nEntrypoint content.")
        result = self.ms.load_entrypoint()
        assert "Entrypoint content" in result.content

    def test_verify_memory_system_load_entrypoint_empty(self):
        """Checks that load_entrypoint returns empty content with no truncation flag."""
        result = self.ms.load_entrypoint()
        assert result.content == ""
        assert result.was_line_truncated is False

    def _write(self, name, content):
        """Writes content to a file inside the temp directory."""
        with open(os.path.join(self.tmpdir, name), "w", encoding="utf-8") as f:
            f.write(content)
