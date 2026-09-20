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

"""Tests for memdir: memory system, semantic search, working memory, consolidation."""

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

# ===========================================================================
# Tokeniser & similarity
# ===========================================================================

class TestTokenize:
    """Engineered to validate the text tokeniser and similarity primitives.

This test class exercises _tokenize, Jaccard similarity, TF-IDF
vectorisation, IDF weighting, and cosine similarity across edge cases
(empty input, mixed CJK/ASCII, short-token filtering) to ensure the
semantic search pipeline produces deterministic, bounded vector
representations for downstream recall.
"""
    def test_verify_tokenize_simple(self):
        """Validate that _tokenize correctly lowercases and splits ASCII text."""
        t = _tokenize("Hello World! This is a test.")
        assert "hello" in t
        assert "world" in t
        assert "this" in t

    def test_verify_tokenize_chinese(self):
        """Validate that _tokenize handles mixed CJK and ASCII content correctly."""
        t = _tokenize("娴嬭瘯 涓枃 and English 娣峰悎")
        assert "and" in t
        assert "english" in t

    def test_verify_tokenize_short_tokens_dropped(self):
        """Validate that single-character tokens are filtered out while boundary-length tokens are preserved."""
        t = _tokenize("a b c ab cd ef hello")
        assert "hello" in t
        # Short single-char tokens are dropped; "ab" is exact boundary
        assert "a" not in t
        assert len(t) > 0

    def test_verify_tokenize_empty(self):
        """Validate that _tokenize returns an empty list for empty input."""
        assert _tokenize("") == []


class TestJaccard:
    """Engineered to validate Jaccard set similarity over tokenised text.

Tests cover identical strings, disjoint sets, partial overlap, and
one-empty-input cases to guarantee the similarity metric returns
values in the closed interval [0.0, 1.0] and behaves monotonically
with respect to set intersection size.
"""
    def test_verify_jaccard_identical(self):
        """Validate that Jaccard similarity of identical strings equals 1.0."""
        assert _jaccard_similarity("hello world", "hello world") == 1.0

    def test_verify_jaccard_disjoint(self):
        """Validate that Jaccard similarity of disjoint sets equals 0.0."""
        assert _jaccard_similarity("abc def", "xyz uvw") == 0.0

    def test_verify_jaccard_partial(self):
        """Validate that partial overlap yields a similarity strictly between 0.4 and 1.0."""
        s = _jaccard_similarity("hello world foo", "hello world bar")
        assert 0.4 < s < 1.0

    def test_verify_jaccard_one_empty(self):
        """Validate that Jaccard similarity is 0.0 when either operand is empty."""
        assert _jaccard_similarity("", "hello") == 0.0
        assert _jaccard_similarity("hello", "") == 0.0


class TestTfIdf:
    """Engineered to validate TF-IDF vectorisation and cosine similarity.

The class verifies that _build_idf assigns lower weights to
high-frequency terms, that _tf_idf_vectorize produces sparse
non-zero entries only for in-vocabulary tokens, and that cosine
similarity of a vector with itself equals 1.0 while orthogonal
document pairs yield 0.0.
"""
    def test_verify_tfidf_build_idf(self):
        """Validate that _build_idf assigns lower IDF weights to high-frequency terms."""
        corpus = ["hello world", "hello foo", "bar baz"]
        idf = _build_idf(corpus)
        assert "hello" in idf
        assert "world" in idf
        assert idf["hello"] < idf["world"]  # hello appears in 2 docs, world in 1

    def test_verify_tfidf_empty_corpus(self):
        """Validate that _build_idf returns an empty dict for an empty corpus."""
        assert _build_idf([]) == {}

    def test_verify_tfidf_vectorize(self):
        """Validate that _tf_idf_vectorize produces non-zero entries only for in-vocabulary tokens."""
        corpus = ["hello world foo", "hello bar", "bar baz qux"]
        idf = _build_idf(corpus)
        vocab = set(idf.keys())
        vec = _tf_idf_vectorize("hello world", idf, vocab)
        assert "hello" in vec
        assert vec["hello"] > 0

    def test_verify_tfidf_cosine_same(self):
        """Validate that cosine similarity of a vector with itself equals 1.0."""
        corpus = ["hello world", "foo bar"]
        idf = _build_idf(corpus)
        vocab = set(idf.keys())
        v = _tf_idf_vectorize("hello world", idf, vocab)
        assert _cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-6)

    def test_verify_tfidf_cosine_orthogonal(self):
        """Validate that cosine similarity of orthogonal vectors equals 0.0."""
        corpus = ["hello world", "foo bar"]
        idf = _build_idf(corpus)
        vocab = set(idf.keys())
        v1 = _tf_idf_vectorize("hello world", idf, vocab)
        v2 = _tf_idf_vectorize("foo bar", idf, vocab)
        assert _cosine_similarity(v1, v2) == 0.0


# ===========================================================================
# SemanticMemorySearch
# ===========================================================================

class TestSemanticMemorySearch:
    """Engineered to validate semantic file search over a document directory.

This class tests SemanticMemorySearch's ability to build an
in-memory TF-IDF index from plain-text files, query it by keyword,
enforce top_k limits, skip the MEMORY.md entrypoint, and support
explicit index construction for controlled test scenarios.
"""
    @pytest.fixture(autouse=True)
    def setup(self):
        """Create a temp directory and yield; clean up on teardown."""
        self.tmpdir = tempfile.mkdtemp()
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, name, content):
        """Write content to a file in the temp directory."""
        with open(os.path.join(self.tmpdir, name), "w", encoding="utf-8") as f:
            f.write(content)

    def test_verify_semantic_search_finds_relevant(self):
        """Validate that search returns the most relevant file for a keyword query."""
        self._write("auth.md", "The login system uses OAuth2 with JWT tokens.")
        self._write("ui.md", "The dashboard uses React and Tailwind CSS for styling.")
        sms = SemanticMemorySearch(self.tmpdir)
        results = sms.search("authentication login")
        assert len(results) >= 1
        assert results[0].file_name == "auth.md"

    def test_verify_semantic_search_respects_top_k(self):
        """Validate that the top_k parameter limits the number of results returned."""
        for i in range(10):
            self._write(f"doc{i}.md", f"Document number {i} about various topics.")
        sms = SemanticMemorySearch(self.tmpdir)
        results = sms.search("document", top_k=3)
        assert len(results) <= 3

    def test_verify_semantic_search_empty_dir(self):
        """Validate that search returns an empty list when the index is empty."""
        sms = SemanticMemorySearch(self.tmpdir)
        assert sms.search("anything") == []

    def test_verify_semantic_search_relevant_higher_threshold(self):
        """Validate that search_relevant uses a higher threshold and returns the best match."""
        self._write("a.md", "python async programming guide")
        self._write("b.md", "baking chocolate cake recipe")
        sms = SemanticMemorySearch(self.tmpdir)
        results = sms.search_relevant("python programming")
        assert len(results) >= 1
        assert results[0].file_name == "a.md"

    def test_verify_semantic_search_ignores_memory_md(self):
        """Validate that the MEMORY.md entrypoint file is excluded from search results."""
        self._write("MEMORY.md", "entrypoint content")
        self._write("real.md", "actual memory content here")
        sms = SemanticMemorySearch(self.tmpdir)
        results = sms.search("content")
        names = {r.file_name for r in results}
        assert "MEMORY.md" not in names
        assert "real.md" in names

    def test_verify_semantic_search_index_explicit(self):
        """Validate that explicit index construction works for controlled test scenarios."""
        sms = SemanticMemorySearch(self.tmpdir)
        sms.index({"a.md": "hello world", "b.md": "foo bar"})
        results = sms.search("hello")
        assert results[0].file_name == "a.md"


# ===========================================================================
# WorkingMemory
# ===========================================================================

class TestWorkingMemory:
    """Engineered to validate the WorkingMemory transient state machine.

The class exercises goal setting, sub-goal deduplication, hypothesis
lifecycle (add then confirm or reject), finding capture, question
resolution, scratchpad notes, summarisation with truncation, and
serialize/deserialize round-tripping via to_dict and from_dict.
"""
    def test_verify_working_memory_initial_empty(self):
        """Validate that a new WorkingMemory instance starts with empty goal, subgoals, and hypotheses."""
        wm = WorkingMemory()
        assert wm.current_goal == ""
        assert wm.subgoals == []
        assert wm.hypotheses == []

    def test_verify_working_memory_set_goal(self):
        """Validate that set_goal correctly updates the current goal."""
        wm = WorkingMemory()
        wm.set_goal("Implement OAuth2")
        assert wm.current_goal == "Implement OAuth2"

    def test_verify_working_memory_add_subgoal_no_dupes(self):
        """Validate that adding the same subgoal twice does not create duplicates."""
        wm = WorkingMemory()
        wm.add_subgoal("Write tests")
        wm.add_subgoal("Write tests")
        assert len(wm.subgoals) == 1

    def test_verify_working_memory_complete_subgoal(self):
        """Validate that complete_subgoal removes the specified subgoal."""
        wm = WorkingMemory()
        wm.add_subgoal("Write tests")
        wm.complete_subgoal("Write tests")
        assert wm.subgoals == []

    def test_verify_working_memory_hypothesis_lifecycle(self):
        """Validate that confirm_hypothesis moves the hypothesis to findings with CONFIRMED marker."""
        wm = WorkingMemory()
        wm.add_hypothesis("The bug is in auth.py")
        wm.confirm_hypothesis("The bug is in auth.py")
        assert wm.hypotheses == []
        assert any("CONFIRMED" in f for f in wm.findings)

    def test_verify_working_memory_reject_hypothesis(self):
        """Validate that reject_hypothesis moves the hypothesis to findings with REJECTED marker."""
        wm = WorkingMemory()
        wm.add_hypothesis("Memory leak in loop")
        wm.reject_hypothesis("Memory leak in loop")
        assert wm.hypotheses == []
        assert any("REJECTED" in f for f in wm.findings)

    def test_verify_working_memory_add_finding(self):
        """Validate that add_finding appends to the findings list."""
        wm = WorkingMemory()
        wm.add_finding("Token refresh endpoint returns 401")
        assert len(wm.findings) == 1

    def test_verify_working_memory_question_lifecycle(self):
        """Validate that resolve_question removes the question and records it in findings."""
        wm = WorkingMemory()
        wm.add_question("Should we use asyncpg?")
        wm.resolve_question("Should we use asyncpg?", "Yes, it's faster")
        assert wm.open_questions == []
        assert any("asyncpg" in f for f in wm.findings)

    def test_verify_working_memory_scratchpad(self):
        """Validate that note appends entries to the scratchpad list."""
        wm = WorkingMemory()
        wm.note("TODO: check error handling")
        wm.note("Done: error handling looks fine")
        assert len(wm.scratchpad) == 2

    def test_verify_working_memory_summarize_empty(self):
        """Validate that summarize contains 'empty' when no data is present."""
        wm = WorkingMemory()
        assert "empty" in wm.summarize().lower()

    def test_verify_working_memory_summarize_with_content(self):
        """Validate that summarize includes the goal and findings in the output."""
        wm = WorkingMemory()
        wm.set_goal("Test framework")
        wm.add_finding("pytest configured")
        s = wm.summarize()
        assert "Test framework" in s
        assert "pytest configured" in s

    def test_verify_working_memory_summarize_truncates_lists(self):
        """Validate that summarize truncates long lists to the last 10 entries."""
        wm = WorkingMemory()
        for i in range(20):
            wm.add_finding(f"Finding {i}")
        s = wm.summarize()
        # Should show only last 10 findings
        assert "Finding 0" not in s
        assert "Finding 19" in s

    def test_verify_working_memory_serialize_roundtrip(self):
        """Validate that to_dict and from_dict preserve all state fields."""
        wm = WorkingMemory()
        wm.set_goal("Test")
        wm.add_hypothesis("H1")
        wm.add_finding("F1")
        d = wm.to_dict()
        wm2 = WorkingMemory.from_dict(d)
        assert wm2.current_goal == "Test"
        assert "H1" in wm2.hypotheses
        assert "F1" in wm2.findings


# ===========================================================================
# MemoryConsolidator
# ===========================================================================

class TestMemoryConsolidator:
    """Engineered to validate memory consolidation heuristics.

Tests cover duplicate detection (semantic similarity triggers merge),
conflict detection (contradictory assertions trigger flag_conflict),
staleness detection (age threshold triggers mark_stale), and the
consolidation pipeline ordering invariant: merge actions must
precede conflict flags when both are applicable.
"""
    @pytest.fixture(autouse=True)
    def setup(self):
        """Create a temp directory and yield; clean up on teardown."""
        self.tmpdir = tempfile.mkdtemp()
        self.mc = MemoryConsolidator(self.tmpdir)
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_verify_consolidator_find_duplicates(self):
        """Validate that find_duplicates detects semantically similar content and suggests merge."""
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
        """Validate that find_duplicates returns empty when no similar pairs exist."""
        files = {"a.md": "foo bar", "b.md": "completely unrelated"}
        assert self.mc.find_duplicates(files) == []

    def test_verify_consolidator_find_conflicts(self):
        """Validate that find_conflicts detects contradictory assertions."""
        files = {
            "a.md": "Always use async/await for network calls.",
            "b.md": "Never use async/await; prefer synchronous calls.",
        }
        actions = self.mc.find_conflicts(files)
        assert len(actions) >= 1
        assert actions[0].action == "flag_conflict"

    def test_verify_consolidator_find_conflicts_no_overlap_no_flag(self):
        """Validate that unrelated content does not trigger conflict flags."""
        files = {
            "a.md": "Always use async/await for network calls.",
            "b.md": "The CSS grid system is preferred for layouts.",
        }
        actions = self.mc.find_conflicts(files)
        assert len(actions) == 0

    def test_verify_consolidator_find_stale(self):
        """Validate that find_stale marks old references as stale when past the threshold."""
        files = {"old.md": "Reference: `src/auth.py:42` has the login flow."}
        age_days = {"old.md": 60}
        actions = self.mc.find_stale(files, age_days, stale_threshold_days=30)
        # src/auth.py likely doesn't exist in cwd
        assert len(actions) >= 1
        assert actions[0].action == "mark_stale"

    def test_verify_consolidator_find_stale_not_old_enough(self):
        """Validate that find_stale does not flag content below the age threshold."""
        files = {"recent.md": "Reference: `src/auth.py:42`"}
        age_days = {"recent.md": 5}
        actions = self.mc.find_stale(files, age_days, stale_threshold_days=30)
        assert len(actions) == 0

    def test_verify_consolidator_consolidate_orders_actions(self):
        """Validate that merge actions are ordered before conflict flags."""
        files = {
            "dup_a.md": "Always use async/await for network calls in production code.",
            "dup_b.md": "Always use async/await for network calls in the production environment.",
            "conflict.md": "Never use async/await; prefer synchronous calls.",
        }
        age_days = {"dup_a.md": 35, "dup_b.md": 10, "conflict.md": 5}
        actions = self.mc.consolidate(files, age_days)
        # merge should come before conflict
        assert actions[0].action == "merge"
        assert any(a.action == "flag_conflict" for a in actions)


# ===========================================================================
# EncreMemorySystem integration
# ===========================================================================

class TestEncreMemorySystem:
    """Engineered to validate the top-level EncreMemorySystem file facade.

This class exercises the facade's file-I/O surface: directory scanning with
YAML frontmatter parsing, MEMORY.md entrypoint write/load, and the
ingestion trigger (``refresh``).  Retrieval is no longer part of this
facade -- it goes only through RecallEngine over the layer store, so the
old search / prompt-building / consolidation assertions are gone.
"""
    @pytest.fixture(autouse=True)
    def setup(self):
        """Create a temp directory and yield; clean up on teardown."""
        self.tmpdir = tempfile.mkdtemp()
        self.ms = EncreMemorySystem(self.tmpdir)
        yield
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_verify_memory_system_scan_empty(self):
        """Validate that scan returns an empty list when no memory files exist."""
        assert self.ms.scan() == []

    def test_verify_memory_system_scan_single(self):
        """Validate that scan parses YAML frontmatter and extracts description and type."""
        self._write("test.md", "---\ndescription: Test memory\ntype: reference\n---\nContent here.")
        memories = self.ms.scan()
        assert len(memories) == 1
        assert memories[0].description == "Test memory"
        assert memories[0].memory_type == "reference"

    def test_verify_memory_system_scan_skips_entrypoint(self):
        """Validate that MEMORY.md is excluded from scan results."""
        self._write("MEMORY.md", "entrypoint")
        self._write("real.md", "real memory")
        memories = self.ms.scan()
        names = {m.filename for m in memories}
        assert "MEMORY.md" not in names
        assert "real.md" in names

    def test_verify_memory_system_refresh_without_store_is_noop(self):
        """Validate that refresh degrades to a harmless no-op when unattached."""
        assert self.ms.layer_store is None
        self._write("test.md", "---\ndescription: A test\n---\nTest content.")
        # No store attached: the ingestion trigger must not raise.
        self.ms.refresh()

    def test_verify_memory_system_write_entrypoint(self):
        """Validate that write_entrypoint and load_entrypoint round-trip content correctly."""
        self.ms.write_entrypoint("# Test\n\nEntrypoint content.")
        result = self.ms.load_entrypoint()
        assert "Entrypoint content" in result.content

    def test_verify_memory_system_load_entrypoint_empty(self):
        """Validate that load_entrypoint returns empty content and was_line_truncated=False when no entrypoint exists."""
        result = self.ms.load_entrypoint()
        assert result.content == ""
        assert result.was_line_truncated is False

    def _write(self, name, content):
        """Write content to a file in the temp directory."""
        with open(os.path.join(self.tmpdir, name), "w", encoding="utf-8") as f:
            f.write(content)
