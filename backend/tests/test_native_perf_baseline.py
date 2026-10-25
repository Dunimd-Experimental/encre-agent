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

"""Performance baseline for the native hot path (Task 10.4).

This module establishes a **measured** performance baseline for the hot-path
functions exported by ``harness/encre/native.py``: for **identical inputs**,
``time.perf_counter`` measures the native implementation against a pure-Python
fallback (the minimum over several rounds is taken to suppress scheduling
noise), and the native path is asserted not to be slower than the fallback
beyond a generous tolerance. When native is unavailable (``_HAS_NATIVE`` is
false) the comparison is skipped via ``skipif``, but the functional
correctness assertions on the fallback side still run.

**Hot path -> pure-Python fallback cross-reference**

============================  ==========================  ==================================================
native function               purpose                       pure-Python fallback location
============================  ==========================  ==================================================
``count_tokens``              estimate text token count     ``harness/encre/utils/tokens.py::estimate_tokens``
                                                          (native -> tiktoken -> ``len//4`` three-level fallback)
``compute_diff``              produce a line-level diff     no in-tree fallback (Rust only); ``difflib`` used in the test
``apply_diff``                apply a line-level diff       no in-tree fallback; a same-format applier lives in the test
``grep``                      regex-search a file           no in-tree fallback (Rust only); ``re`` used in the test
``text_similarity``           whitespace-token Jaccard      no in-tree fallback; set intersection/union in the test
``cosine_similarity``         vector cosine similarity      no in-tree fallback; dot/norm in the test
``simd_find_all``             all substring occurrences     no in-tree fallback; a ``str.find`` loop in the test
``simd_contains``             substring presence            no in-tree fallback; ``in`` in the test
``simd_memmem``               byte-level memmem             no in-tree fallback; ``bytes.find`` in the test
``read_file``                 read a file (offset/limit)    no in-tree fallback; ``Path.read_text`` in the test
``write_file``                atomic write (temp+fsync+rename) no in-tree fallback; a Python atomic write in the test
============================  ==========================  ==================================================

Note: ``memdir/semantic.py::_cosine_similarity`` operates on a word-frequency
dict and is not the same function as the native vector ``cosine_similarity``,
so it is not treated as a fallback for it.

**Honest scope**: this module only reports real wall-clock numbers for the
local machine (release build from the GNU toolchain) under deterministic loads,
and makes **no** claim about real-world LLM task gains. If a function turns out
to be slower in native, this module does **not** hide it: either it asserts the
real regression honestly (see :class:`TestObservedNativeRegressions`), or it
reports the real numbers.
"""

import math
import os
import re
import time
from pathlib import Path

import pytest

from encre import native
from encre.utils.tokens import estimate_tokens

#: Whether native is actually loaded (compiled on this machine, usually True).
_HAS_NATIVE: bool = bool(getattr(native, "_HAS_NATIVE", False))

#: Default tolerance for performance assertions: allows native to be slightly
#: slower in extreme cases (local release build plus timing noise).
_DEFAULT_TOLERANCE = 8.0

#: Default measurement rounds (the minimum is taken to reduce scheduling/GC noise).
_DEFAULT_REPEATS = 7

#: Shared skip condition for performance tests: the native/fallback comparison
#: is impossible when native is unavailable.
_requires_native = pytest.mark.skipif(
    not _HAS_NATIVE, reason="encre._native 未编译，跳过 native/回退性能对比",
)


# ---------------------------------------------------------------------------
# Timing and assertion utilities
# ---------------------------------------------------------------------------


def _best_of(fn, *, repeats: int = _DEFAULT_REPEATS) -> float:
    """Return the **minimum** single-call duration (seconds) of ``fn`` over many calls.

    Taking the best of several runs suppresses scheduler and GC noise so the
    reported figure approximates the steady-state cost of the operation.
    """
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def _compare(label: str, native_fn, py_fn, *, tolerance: float, repeats: int) -> tuple[float, float]:
    """Measure and print native vs fallback duration; assert native is not slower than ``tolerance``x.

    Both sides are measured over the same number of rounds and with the same
    inputs. The elapsed figures are printed for the report and the assertion
    only guards against native exceeding the generous tolerance.
    """
    native_t = _best_of(native_fn, repeats=repeats)
    py_t = _best_of(py_fn, repeats=repeats)
    ratio = native_t / max(py_t, 1e-9)
    print(
        f"[perf] {label}: native={native_t * 1e6:.1f}us python={py_t * 1e6:.1f}us "
        f"ratio={ratio:.2f}x tolerance={tolerance:.1f}x"
    )
    assert native_t <= py_t * tolerance, (
        f"{label}: native {native_t * 1e6:.1f}us 慢于回退 {py_t * 1e6:.1f}us "
        f"超过 {tolerance:.1f}x（真实倍率 {ratio:.2f}x）"
    )
    return native_t, py_t


# ---------------------------------------------------------------------------
# Pure-Python fallbacks (equivalent-semantics stdlib reference implementations)
# ---------------------------------------------------------------------------


def _py_apply_diff(content: str, diff: str) -> str:
    """Apply the native line-level diff format (``' '``/``'-'``/``'+'`` prefixes).

    Each line carries its own trailing newline, matching the format produced by
    the native ``compute_diff`` function.
    """
    out: list[str] = []
    for raw in diff.splitlines(keepends=True):
        if not raw:
            continue
        tag, text = raw[0], raw[1:]
        if tag == "+":
            out.append(text)
        elif tag == " ":
            out.append(text)
        # "-" marks a deleted line and contributes nothing to the output.
    return "".join(out)


def _py_grep(pattern: str, path: str) -> list[int]:
    """Regex-search a file line by line and return hit line numbers (1-indexed)."""
    rx = re.compile(pattern)
    hits: list[int] = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if rx.search(line):
                hits.append(number)
    return hits


def _py_text_similarity(a: str, b: str) -> float:
    """Whitespace-token Jaccard similarity between two texts."""
    left, right = set(a.split()), set(b.split())
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _py_cosine_similarity(a: list[float], b: list[float]) -> float:
    """Vector cosine similarity computed with pure Python dot/norm."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _py_find_all(haystack: str, needle: str) -> list[int]:
    """Find every occurrence position of ``needle`` using a ``str.find`` loop."""
    out: list[int] = []
    index = haystack.find(needle)
    while index != -1:
        out.append(index)
        index = haystack.find(needle, index + 1)
    return out


def _py_atomic_write(path: str, content: str) -> None:
    """Python atomic write: temp -> flush + fsync -> os.replace (native-equivalent)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Fallback-side functional correctness (must run even without native)
# ---------------------------------------------------------------------------


class TestFallbackCorrectness:
    """Functional correctness assertions for the fallback path (native-free).

    These tests do not depend on the compiled extension and therefore run in
    every environment, guaranteeing that the reference implementations remain
    trustworthy even when native is absent.
    """

    def test_production_python_token_fallback_runs(self):
        """The production Python fallback ``estimate_tokens`` is always available and sane."""
        assert estimate_tokens("") == 0
        assert estimate_tokens("hello world this is a token estimate") > 0
        assert estimate_tokens("a" * 400) >= 1

    def test_reference_implementations_are_correct(self, tmp_path):
        """The in-test Python reference implementations are correct on hand-verifiable inputs."""
        # apply_diff: " a\\n-b\\n+c\\n" turns "a\\nb" into "a\\nc".
        assert _py_apply_diff("a\nb\n", " a\n-b\n+c\n") == "a\nc\n"
        assert _py_apply_diff("", "+x\n+y\n") == "x\ny\n"

        # grep: returns the matching line numbers.
        path = tmp_path / "sample.txt"
        path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        assert _py_grep("a", str(path)) == [1, 2, 3]

        # Jaccard: {a,b,c} vs {a,b} -> 2/3.
        assert abs(_py_text_similarity("a b c", "a b") - 2.0 / 3.0) < 1e-12

        # Cosine: aligned vectors give 1.0, orthogonal vectors give 0.0.
        assert abs(_py_cosine_similarity([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-12
        assert abs(_py_cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-12

        # find_all / contains / memmem.
        assert _py_find_all("abab", "ab") == [0, 2]
        assert "ell" in "hello"
        assert b"abab".find(b"ab") == 0

    def test_native_shim_flag_is_bool(self):
        """``_HAS_NATIVE`` is always a readable boolean flag."""
        assert isinstance(_HAS_NATIVE, bool)


# ---------------------------------------------------------------------------
# Native and fallback agree on identical inputs
# ---------------------------------------------------------------------------


@_requires_native
class TestNativeEquivalence:
    """Native and the pure-Python fallback produce identical results on equal inputs."""

    def test_count_tokens_is_positive_and_scales(self):
        """Native token counting is positive and grows with input length."""
        text = "The quick brown fox jumps over the lazy dog. " * 2000
        assert native.count_tokens(text) > 0
        assert native.count_tokens(text) > native.count_tokens("short text")

    def test_diff_round_trip_matches_reference(self):
        """Native compute_diff/apply_diff round-trips and matches the Python reference."""
        # The native diff uses a "line includes newline" format, so inputs must
        # end with a newline for a lossless round trip.
        old = "\n".join(f"line {i} content here" for i in range(500)) + "\n"
        new = "\n".join(
            (f"line {i} changed" if i % 20 == 0 else f"line {i} content here")
            for i in range(500)
        ) + "\n"
        diff = native.compute_diff(old, new)
        assert native.apply_diff(old, diff) == new
        assert _py_apply_diff(old, diff) == new

    def test_grep_matches_reference_line_numbers(self, tmp_path):
        """Native grep reports the same matching line numbers as the Python reference."""
        path = tmp_path / "code.py"
        path.write_text(
            "\n".join(
                (f"def func_{i}(): pass" if i % 10 == 0 else f"x = {i}") for i in range(500)
            ),
            encoding="utf-8",
        )
        native_hits = sorted(item["line_number"] for item in native.grep("def func_", str(path)))
        assert native_hits == _py_grep("def func_", str(path))
        assert native_hits

    def test_text_similarity_matches_reference(self):
        """Native text similarity agrees with the Python Jaccard reference."""
        a = " ".join(f"tok{i}" for i in range(400))
        b = " ".join(f"tok{i}" for i in range(200, 600))
        # Native computes in f32, so the tolerance is at f32 precision scale.
        assert abs(native.text_similarity(a, b) - _py_text_similarity(a, b)) < 1e-6

    def test_cosine_similarity_matches_reference(self):
        """Native cosine similarity agrees with the Python reference."""
        a = [float(i % 7) for i in range(256)]
        b = [float((i + 1) % 5) for i in range(256)]
        assert abs(native.cosine_similarity(a, b) - _py_cosine_similarity(a, b)) < 1e-6

    def test_simd_find_all_matches_reference(self):
        """Native SIMD substring search returns the same offsets as str.find."""
        haystack = "abab" * 200
        assert native.simd_find_all(haystack, "ab") == _py_find_all(haystack, "ab")

    def test_simd_contains_and_memmem_match_reference(self):
        """Native SIMD contains and memmem agree with ``in`` and ``bytes.find``."""
        haystack = ("abcdefghij" * 1000) + "NEEDLE"
        assert native.simd_contains(haystack, "NEEDLE") is True
        assert native.simd_contains(haystack, "ABSENT") is False
        assert native.simd_memmem(haystack.encode(), b"NEEDLE") == haystack.encode().find(b"NEEDLE")
        assert native.simd_memmem(haystack.encode(), b"ABSENT") is None

    def test_read_write_file_round_trip_matches_reference(self, tmp_path):
        """Native file I/O round-trips content identical to what pathlib writes."""
        path = str(tmp_path / "round_trip.txt")
        content = "x" * 10000 + "\nline two\n"
        assert native.write_file(path, content) is True
        assert native.read_file(path) == content
        assert Path(path).read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# Hot-path performance baseline (native vs pure-Python fallback)
# ---------------------------------------------------------------------------


@_requires_native
class TestHotPathPerformance:
    """Real wall-clock comparison: assert native is not slower beyond tolerance.

    Every figure is printed for the report so the numbers stay auditable
    instead of being swallowed by a boolean assertion.
    """

    def test_count_tokens_vs_tiktoken(self):
        """Token counting: native vs tiktoken (the equivalent tokenizer fallback)."""
        tiktoken = pytest.importorskip("tiktoken")
        encoding = tiktoken.get_encoding("o200k_base")
        text = "The quick brown fox jumps over the lazy dog. " * 2000
        _compare(
            "count_tokens(text=90KB)",
            lambda: native.count_tokens(text),
            lambda: len(encoding.encode(text)),
            tolerance=_DEFAULT_TOLERANCE,
            repeats=5,
        )

    def test_compute_diff_vs_difflib(self):
        """Diff generation: native vs difflib.unified_diff."""
        import difflib

        old = "\n".join(f"line {i} content here" for i in range(2000))
        new = "\n".join(
            (f"line {i} changed" if i % 20 == 0 else f"line {i} content here")
            for i in range(2000)
        )
        _compare(
            "compute_diff(2000 lines)",
            lambda: native.compute_diff(old, new),
            lambda: "".join(
                difflib.unified_diff(old.splitlines(True), new.splitlines(True))
            ),
            tolerance=_DEFAULT_TOLERANCE,
            repeats=5,
        )

    def test_apply_diff_vs_python(self):
        """Diff application: native vs a pure-Python applier."""
        old = "\n".join(f"line {i} content here" for i in range(2000))
        new = "\n".join(
            (f"line {i} changed" if i % 20 == 0 else f"line {i} content here")
            for i in range(2000)
        )
        diff = native.compute_diff(old, new)
        _compare(
            "apply_diff(2000 lines)",
            lambda: native.apply_diff(old, diff),
            lambda: _py_apply_diff(old, diff),
            tolerance=_DEFAULT_TOLERANCE,
            repeats=5,
        )

    def test_grep_vs_python_re(self, tmp_path):
        """File regex search: native vs line-by-line Python ``re`` scanning."""
        path = tmp_path / "big.txt"
        path.write_text(
            "\n".join(
                (f"def func_{i}(): pass" if i % 10 == 0 else f"x = {i}") for i in range(20000)
            ),
            encoding="utf-8",
        )
        _compare(
            "grep(20000 lines)",
            lambda: native.grep("def func_", str(path)),
            lambda: _py_grep("def func_", str(path)),
            tolerance=_DEFAULT_TOLERANCE,
            repeats=5,
        )

    def test_text_similarity_vs_python(self):
        """Text Jaccard: native vs pure-Python set intersection/union."""
        a = " ".join(f"tok{i}" for i in range(5000))
        b = " ".join(f"tok{i}" for i in range(2500, 7500))
        _compare(
            "text_similarity(5000 tokens)",
            lambda: native.text_similarity(a, b),
            lambda: _py_text_similarity(a, b),
            tolerance=_DEFAULT_TOLERANCE,
            repeats=7,
        )

    def test_cosine_similarity_vs_python(self):
        """Vector cosine: native vs pure-Python dot/norm."""
        a = [float(i % 7) for i in range(4096)]
        b = [float((i + 1) % 5) for i in range(4096)]
        _compare(
            "cosine_similarity(dim=4096)",
            lambda: native.cosine_similarity(a, b),
            lambda: _py_cosine_similarity(a, b),
            tolerance=_DEFAULT_TOLERANCE,
            repeats=7,
        )

    def test_simd_find_all_vs_python(self):
        """All substring positions: native vs a ``str.find`` loop."""
        haystack = "abab" * 50000
        _compare(
            "simd_find_all(200KB, 100k hits)",
            lambda: native.simd_find_all(haystack, "ab"),
            lambda: _py_find_all(haystack, "ab"),
            tolerance=_DEFAULT_TOLERANCE,
            repeats=5,
        )

    def test_read_file_vs_pathlib(self, tmp_path):
        """File read: native vs ``Path.read_text``."""
        path = tmp_path / "read.txt"
        path.write_text("x" * 500000, encoding="utf-8")
        _compare(
            "read_file(500KB)",
            lambda: native.read_file(str(path)),
            lambda: Path(path).read_text(encoding="utf-8"),
            tolerance=_DEFAULT_TOLERANCE,
            repeats=7,
        )

    def test_write_file_vs_python_atomic_write(self, tmp_path):
        """Atomic write: native (temp+fsync+rename) vs an equivalent Python atomic write.

        The comparison must be **semantically equivalent** (including fsync
        persistence): against a plain write without fsync the native path would
        look slower, but that is not a like-for-like comparison.
        """
        path = str(tmp_path / "write.txt")
        content = "x" * 500000
        _compare(
            "write_file(500KB, atomic+fsync)",
            lambda: native.write_file(path, content),
            lambda: _py_atomic_write(path, content),
            tolerance=_DEFAULT_TOLERANCE,
            repeats=5,
        )


# ---------------------------------------------------------------------------
# Observed native regressions (asserted honestly, never hidden)
# ---------------------------------------------------------------------------


@_requires_native
class TestObservedNativeRegressions:
    """Record faithfully the functions where native is slower than stdlib.

    These two ``simd_*`` functions have **no Python caller anywhere in the
    repository** (only the ``native.py`` export), so they do not form a
    production hot path; as a baseline their real ratio must still be recorded
    honestly. Here we only assert an upper bound against catastrophic
    regression, and the report carries the precise numbers.
    """

    def test_simd_contains_is_slower_than_stdlib(self):
        """``simd_contains`` is slower than CPython ``in`` (optimized memchr/two-way)."""
        haystack = ("abcdefghij" * 100000) + "NEEDLE"
        native_t = _best_of(lambda: native.simd_contains(haystack, "NEEDLE"))
        py_t = _best_of(lambda: "NEEDLE" in haystack)
        print(
            f"[perf][regression] simd_contains: native={native_t * 1e6:.1f}us "
            f"python={py_t * 1e6:.1f}us ratio={native_t / max(py_t, 1e-9):.2f}x"
        )
        assert native.simd_contains(haystack, "NEEDLE") is True
        # The upper bound only guards against catastrophic regression; the real
        # ratio is reported (about 5x slower on this machine).
        assert native_t <= py_t * 20.0

    def test_simd_memmem_is_slower_than_stdlib(self):
        """``simd_memmem`` is slower than CPython ``bytes.find`` (about 28x locally)."""
        haystack = (b"abcdefghij" * 100000) + b"NEEDLE"
        native_t = _best_of(lambda: native.simd_memmem(haystack, b"NEEDLE"))
        py_t = _best_of(lambda: haystack.find(b"NEEDLE"))
        print(
            f"[perf][regression] simd_memmem: native={native_t * 1e6:.1f}us "
            f"python={py_t * 1e6:.1f}us ratio={native_t / max(py_t, 1e-9):.2f}x"
        )
        assert native.simd_memmem(haystack, b"NEEDLE") == haystack.find(b"NEEDLE")
        # The upper bound only guards against catastrophic regression; the real
        # ratio is reported (about 28x slower on this machine).
        assert native_t <= py_t * 100.0
