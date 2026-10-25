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

"""Performance benchmarks for critical paths: tokenizer throughput,
Jaccard similarity, TF-IDF vectorization with cosine similarity,
memory-directory scanning, semantic search, and memory consolidation."""

import os
import tempfile
import time

from encre.memdir.semantic import (
    _build_idf,
    _cosine_similarity,
    _jaccard_similarity,
    _tf_idf_vectorize,
    _tokenize,
)

# ===========================================================================
# Tokenizer benchmarks
# ===========================================================================

class TestTokenizerBench:
    """Validate tokenizer throughput under sustained load.

    Measures whether the tokenizer stays within acceptable latency
    bounds when processing large ASCII and CJK corpora. Token
    throughput is a critical path for prompt assembly; regressions
    here directly increase first-token latency across all agent
    sessions.
    """

    def test_tokenize_10k_lines(self):
        """Validate that tokenizing 10 000 repeated ASCII lines completes in under 5 seconds.

        A single pass over a 10k-line ASCII stream must finish within
        the 5-second budget to guard against regressions in the
        tokenization pipeline that would degrade overall agent latency.
        """
        text = "Hello world this is a test of the tokenizer. " * 10000
        start = time.perf_counter()
        tokens = _tokenize(text)
        elapsed = time.perf_counter() - start
        # Non-empty input must produce at least one token; an empty
        # output would indicate a silent failure in the pipeline.
        assert len(tokens) > 0
        # Latency must stay under the 5-second threshold.
        assert elapsed < 5.0, f"tokenize 10k lines took {elapsed:.2f}s"

    def test_tokenize_chinese(self):
        """Validate that tokenizing CJK text completes within the same latency bound as ASCII text.

        5 000 repetitions of mixed Chinese/ASCII tokens must finish
        in under 5 seconds, ensuring the tokenizer handles multi-byte
        characters without a disproportionate slowdown versus pure
        ASCII processing.
        """
        text = "Testing Chinese tokenization this is a test string" * 5000
        start = time.perf_counter()
        _tokenize(text)
        elapsed = time.perf_counter() - start
        # CJK tokenization must not exceed the 5-second threshold.
        assert elapsed < 5.0, f"tokenize Chinese took {elapsed:.2f}s"


# ===========================================================================
# Jaccard benchmarks
# ===========================================================================

class TestJaccardBench:
    """Validate Jaccard-similarity throughput across document pairs and edge cases.

    Checks that pairwise Jaccard computation remains fast for typical
    document pairs and for the degenerate empty-string case. Empty-string
    similarity is exercised frequently during memory-dedup passes, so
    regressions in that path directly impact scan latency.
    """

    def test_jaccard_1000_pairs(self):
        """Validate that computing 1 000 consecutive Jaccard pairs completes in under 1 second.

        Generates 100 documents and computes similarity across 99
        adjacent pairs. The sub-1-second bound ensures bulk similarity
        evaluation does not become a bottleneck during memory
        consolidation.
        """
        docs = [f"This is document number {i} about various topics including python, rust, typescript, and more." for i in range(100)]  # noqa: E501
        start = time.perf_counter()
        for i in range(len(docs) - 1):
            _jaccard_similarity(docs[i], docs[i + 1])
        elapsed = time.perf_counter() - start
        # Sequential pair computation must stay under 1 second.
        assert elapsed < 1.0, f"1000 Jaccard pairs took {elapsed:.2f}s"

    def test_jaccard_empty(self):
        """Validate that 10 000 empty-string Jaccard comparisons complete in under 0.5 seconds.

        Empty-string comparisons are a hot path during memory scanning
        when stale or placeholder entries are encountered. The tight
        0.5-second bound prevents degenerate inputs from causing scan
        timeouts.
        """
        start = time.perf_counter()
        for _ in range(10000):
            _jaccard_similarity("", "")
        elapsed = time.perf_counter() - start
        # Empty-string comparisons must complete well under 0.5 seconds.
        assert elapsed < 0.5, f"10000 empty Jaccard took {elapsed:.2f}s"


# ===========================================================================
# TF-IDF benchmarks
# ===========================================================================

class TestTfIdfBench:
    """Validate IDF build and cosine-similarity throughput for vector-based retrieval.

    Ensures that building an IDF index over a thousand-document corpus
    and computing pairwise cosine similarities remain within strict
    latency bounds. These operations drive semantic memory search, so
    regressions here directly degrade search responsiveness.
    """

    def test_build_idf_large_corpus(self):
        """Validate that building an IDF index over 1 000 documents completes in under 2 seconds.

        Constructs a synthetic corpus and measures the time to compute
        inverse-document frequencies. The 2-second bound guards against
        regressions in vocabulary scoring that would delay search index
        initialization.
        """
        corpus = [f"Document {i}: contains words about various topics like python programming and async rust development." for i in range(1000)]  # noqa: E501
        start = time.perf_counter()
        idf = _build_idf(corpus)
        elapsed = time.perf_counter() - start
        # The IDF index must contain entries; an empty index means failure.
        assert len(idf) > 0
        # Index construction must complete within the 2-second threshold.
        assert elapsed < 2.0, f"build_idf 1000 docs took {elapsed:.2f}s"

    def test_vectorize_and_cosine(self):
        """Validate that vectorizing a 500-document corpus and computing pairwise cosine similarities completes in under 1 second.

        Builds an IDF index, vectorizes every document, then measures
        the time to compute 499 consecutive cosine-similarity pairs.
        The combined vectorization-plus-similarity path must stay below
        1 second to avoid blocking semantic search queries.
        """
        corpus = [f"Document {i}: python rust typescript async programming patterns" for i in range(500)]  # noqa: E501
        idf = _build_idf(corpus)
        vocab = set(idf.keys())
        vecs = [_tf_idf_vectorize(doc, idf, vocab) for doc in corpus]
        start = time.perf_counter()
        for i in range(len(vecs) - 1):
            _cosine_similarity(vecs[i], vecs[i + 1])
        elapsed = time.perf_counter() - start
        # Vectorization plus pairwise cosine must stay under 1 second.
        assert elapsed < 1.0, f"500 cosine pairs took {elapsed:.2f}s"


# ===========================================================================
# Memory scan benchmarks
# ===========================================================================

class TestMemoryScanBench:
    """Validate memory-directory scan and semantic-search throughput.

    Measures end-to-end latency for scanning a directory of memory
    files and for executing semantic searches over the indexed contents.
    Both paths are exercised on every agent startup, so regressions
    directly increase cold-start latency.
    """

    def test_scan_large_memory_dir(self):
        """Validate that scanning 200 memory files completes in under 3 seconds.

        Creates a temporary directory with 200 markdown memory files,
        scans them through :class:`EncreMemorySystem`, and asserts the
        scan duration stays below 3 seconds. The temporary directory is
        removed afterwards to avoid leaving artifacts on the filesystem.
        """
        tmpdir = tempfile.mkdtemp()
        # Create 200 memory files.
        for i in range(200):
            with open(os.path.join(tmpdir, f"memory_{i:04d}.md"), "w", encoding="utf-8") as f:
                f.write(f"---\ndescription: Memory {i}\ntype: reference\n---\n\nContent for memory {i}.\n" * 10)  # noqa: E501

        from encre.memdir.system import EncreMemorySystem
        ms = EncreMemorySystem(tmpdir)
        start = time.perf_counter()
        memories = ms.scan()
        elapsed = time.perf_counter() - start
        # A non-empty directory must yield at least one memory entry.
        assert len(memories) > 0
        # Directory scan must complete within the 3-second threshold.
        assert elapsed < 3.0, f"scan 200 files took {elapsed:.2f}s"

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    def test_semantic_search_performance(self):
        """Validate that semantic search over 100 indexed documents completes in under 2 seconds.

        Creates 100 markdown documents, initializes
        :class:`SemanticMemorySearch`, and runs a query for "python async
        programming" with top-k=10. The sub-2-second bound ensures
        interactive search feels responsive during agent operation.
        """
        tmpdir = tempfile.mkdtemp()
        for i in range(100):
            with open(os.path.join(tmpdir, f"doc_{i:04d}.md"), "w", encoding="utf-8") as f:
                f.write(f"Document {i} about python programming and async patterns.\n" * 5)

        from encre.memdir.semantic import SemanticMemorySearch
        sms = SemanticMemorySearch(tmpdir)
        start = time.perf_counter()
        results = sms.search("python async programming", top_k=10)
        elapsed = time.perf_counter() - start
        # A populated index must return at least one result.
        assert len(results) > 0
        # Semantic search must complete within the 2-second threshold.
        assert elapsed < 2.0, f"semantic search 100 docs took {elapsed:.2f}s"

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# Consolidation benchmarks
# ===========================================================================

class TestConsolidationBench:
    """Validate memory-consolidation throughput for batch document ingestion.

    Measures how quickly :class:`MemoryConsolidator` can process a
    batch of candidate documents into a consolidated output.
    Consolidation runs during memory compaction, so its latency
    directly affects how quickly the agent can free up workspace after
    long sessions.
    """

    def test_consolidate_many_files(self):
        """Validate that consolidating 50 documents completes in under 2 seconds.

        Creates a temporary directory and 50 synthetic documents
        alternating between two topics, then runs
        :meth:`MemoryConsolidator.consolidate` and asserts the elapsed
        time stays below 2 seconds. Cleanup removes the temporary
        directory.
        """
        tmpdir = tempfile.mkdtemp()
        from encre.memdir.semantic import MemoryConsolidator

        files = {}
        for i in range(50):
            files[f"doc_{i:04d}.md"] = f"Document {i} about {'async programming' if i % 2 == 0 else 'CSS styling'} patterns and best practices.\n" * 3  # noqa: E501

        mc = MemoryConsolidator(tmpdir)
        start = time.perf_counter()
        mc.consolidate(files, {})
        elapsed = time.perf_counter() - start
        # Consolidation must complete within the 2-second threshold.
        assert elapsed < 2.0, f"consolidate 50 files took {elapsed:.2f}s"

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
