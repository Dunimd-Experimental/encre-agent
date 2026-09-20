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

"""native 热路径性能基线（Task 10.4）。

本模块为 ``harness/encre/native.py`` 导出的热路径函数建立**真实测量**的性能基线：
在**相同输入**下，用 ``time.perf_counter`` 分别测量 native 与纯 Python 回退的耗时
（多轮取最小值以降低调度噪声），并断言 native 不慢于回退（宽容容差）。native
不可用时（``_HAS_NATIVE`` 为假）用 ``skipif`` 跳过对比，但回退侧的功能正确性断言
仍会运行。

**热路径 -> 纯 Python 回退对照表**

============================  ==========================  ==================================================
native 函数                    用途                        纯 Python 回退位置
============================  ==========================  ==================================================
``count_tokens``             估算文本 token 数            ``harness/encre/utils/tokens.py::estimate_tokens``
                                                          （native -> tiktoken -> ``len//4`` 三级回退）
``compute_diff``             生成行级 diff                无 in-tree 回退（Rust-only）；测试内用 ``difflib``
``apply_diff``               应用行级 diff                无 in-tree 回退；测试内实现同格式应用器
``grep``                     正则搜索文件                 无 in-tree 回退（Rust-only）；测试内用 ``re``
``text_similarity``          空白分词 Jaccard             无 in-tree 回退；测试内用 set 交/并集
``cosine_similarity``        向量余弦相似度               无 in-tree 回退；测试内用 dot/norm
``simd_find_all``            子串全部出现位置             无 in-tree 回退；测试内用 ``str.find`` 循环
``simd_contains``            子串存在性                   无 in-tree 回退；测试内用 ``in``
``simd_memmem``              字节级 memmem                 无 in-tree 回退；测试内用 ``bytes.find``
``read_file``                读文件（支持 offset/limit）   无 in-tree 回退；测试内用 ``Path.read_text``
``write_file``               原子写（temp+fsync+rename）   无 in-tree 回退；测试内用 Python 原子写
============================  ==========================  ==================================================

说明：``memdir/semantic.py::_cosine_similarity`` 作用于词频 dict，与 native 的
向量 ``cosine_similarity`` 不是同一函数，故不作为其回退。

**诚实界定**：本模块只给出本机（GNU 工具链编译的 release 产物）在确定性负载下的
真实耗时数字，**不**声称任何真实 LLM 任务收益。若某函数 native 反而更慢，本模块
**不掩盖**：要么如实断言其真实劣化（见 :class:`TestObservedNativeRegressions`），
要么在报告中给出真实数字。
"""

import math
import os
import re
import time
from pathlib import Path

import pytest

from encre import native
from encre.utils.tokens import estimate_tokens

#: native 是否真正加载（本机已编译，通常为 True）。
_HAS_NATIVE: bool = bool(getattr(native, "_HAS_NATIVE", False))

#: 性能断言默认容差：允许 native 在极端情况下略慢（本机 release 产物 + 计时噪声）。
_DEFAULT_TOLERANCE = 8.0

#: 默认测量轮数（取最小值以削弱调度/GC 噪声）。
_DEFAULT_REPEATS = 7

#: 性能测试统一跳过条件：native 不可用时无法做 native/回退对比。
_requires_native = pytest.mark.skipif(
    not _HAS_NATIVE, reason="encre._native 未编译，跳过 native/回退性能对比",
)


# ---------------------------------------------------------------------------
# 计时与断言工具
# ---------------------------------------------------------------------------


def _best_of(fn, *, repeats: int = _DEFAULT_REPEATS) -> float:
    """返回 ``fn`` 多次调用的**最小**单次耗时（秒），降低噪声。"""
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def _compare(label: str, native_fn, py_fn, *, tolerance: float, repeats: int) -> tuple[float, float]:
    """测量并打印 native vs 回退耗时；断言 native 不慢于回退超过 ``tolerance`` 倍。"""
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
# 纯 Python 回退（等价语义的 stdlib 参考实现，供对比使用）
# ---------------------------------------------------------------------------


def _py_apply_diff(content: str, diff: str) -> str:
    """应用 native 行级 diff 格式（``' '``/``'-'``/``'+'`` 前缀，行含换行）。"""
    out: list[str] = []
    for raw in diff.splitlines(keepends=True):
        if not raw:
            continue
        tag, text = raw[0], raw[1:]
        if tag == "+":
            out.append(text)
        elif tag == " ":
            out.append(text)
        # "-" 表示删除该行，不输出。
    return "".join(out)


def _py_grep(pattern: str, path: str) -> list[int]:
    """逐行正则搜索，返回命中行号（1-indexed）。"""
    rx = re.compile(pattern)
    hits: list[int] = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if rx.search(line):
                hits.append(number)
    return hits


def _py_text_similarity(a: str, b: str) -> float:
    """空白分词 Jaccard 相似度。"""
    left, right = set(a.split()), set(b.split())
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _py_cosine_similarity(a: list[float], b: list[float]) -> float:
    """向量余弦相似度（纯 Python dot/norm）。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _py_find_all(haystack: str, needle: str) -> list[int]:
    """``str.find`` 循环找出全部出现位置。"""
    out: list[int] = []
    index = haystack.find(needle)
    while index != -1:
        out.append(index)
        index = haystack.find(needle, index + 1)
    return out


def _py_atomic_write(path: str, content: str) -> None:
    """Python 原子写：temp -> flush + fsync -> os.replace（与 native 语义对齐）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 回退侧功能正确性（native 不可用时也必须运行）
# ---------------------------------------------------------------------------


class TestFallbackCorrectness:
    """回退路径的功能正确性断言（不依赖 native，任何环境都运行）。"""

    def test_production_python_token_fallback_runs(self):
        """生产 Python 回退 ``estimate_tokens`` 始终可用且行为合理。"""
        assert estimate_tokens("") == 0
        assert estimate_tokens("hello world this is a token estimate") > 0
        assert estimate_tokens("a" * 400) >= 1

    def test_reference_implementations_are_correct(self, tmp_path):
        """测试内 Python 参考实现在手工可验证的输入上结果正确。"""
        # apply_diff：' a\n-b\n+c\n' 把 a/b 变为 a/c。
        assert _py_apply_diff("a\nb\n", " a\n-b\n+c\n") == "a\nc\n"
        assert _py_apply_diff("", "+x\n+y\n") == "x\ny\n"

        # grep：命中行号。
        path = tmp_path / "sample.txt"
        path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        assert _py_grep("a", str(path)) == [1, 2, 3]

        # Jaccard：{a,b,c} 与 {a,b} -> 2/3。
        assert abs(_py_text_similarity("a b c", "a b") - 2.0 / 3.0) < 1e-12

        # 余弦：同向 1.0，正交 0.0。
        assert abs(_py_cosine_similarity([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-12
        assert abs(_py_cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-12

        # find_all / contains / memmem。
        assert _py_find_all("abab", "ab") == [0, 2]
        assert "ell" in "hello"
        assert b"abab".find(b"ab") == 0

    def test_native_shim_flag_is_bool(self):
        """``_HAS_NATIVE`` 始终是可读的布尔标志。"""
        assert isinstance(_HAS_NATIVE, bool)


# ---------------------------------------------------------------------------
# native 与回退在相同输入下结果一致
# ---------------------------------------------------------------------------


@_requires_native
class TestNativeEquivalence:
    """native 与纯 Python 回退在相同输入下结果一致（功能正确性）。"""

    def test_count_tokens_is_positive_and_scales(self):
        text = "The quick brown fox jumps over the lazy dog. " * 2000
        assert native.count_tokens(text) > 0
        assert native.count_tokens(text) > native.count_tokens("short text")

    def test_diff_round_trip_matches_reference(self):
        # native diff 是"行含换行"格式，输入以换行结尾才能无损往返。
        old = "\n".join(f"line {i} content here" for i in range(500)) + "\n"
        new = "\n".join(
            (f"line {i} changed" if i % 20 == 0 else f"line {i} content here")
            for i in range(500)
        ) + "\n"
        diff = native.compute_diff(old, new)
        assert native.apply_diff(old, diff) == new
        assert _py_apply_diff(old, diff) == new

    def test_grep_matches_reference_line_numbers(self, tmp_path):
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
        a = " ".join(f"tok{i}" for i in range(400))
        b = " ".join(f"tok{i}" for i in range(200, 600))
        # native 以 f32 计算，故用 f32 精度量级的容差。
        assert abs(native.text_similarity(a, b) - _py_text_similarity(a, b)) < 1e-6

    def test_cosine_similarity_matches_reference(self):
        a = [float(i % 7) for i in range(256)]
        b = [float((i + 1) % 5) for i in range(256)]
        assert abs(native.cosine_similarity(a, b) - _py_cosine_similarity(a, b)) < 1e-6

    def test_simd_find_all_matches_reference(self):
        haystack = "abab" * 200
        assert native.simd_find_all(haystack, "ab") == _py_find_all(haystack, "ab")

    def test_simd_contains_and_memmem_match_reference(self):
        haystack = ("abcdefghij" * 1000) + "NEEDLE"
        assert native.simd_contains(haystack, "NEEDLE") is True
        assert native.simd_contains(haystack, "ABSENT") is False
        assert native.simd_memmem(haystack.encode(), b"NEEDLE") == haystack.encode().find(b"NEEDLE")
        assert native.simd_memmem(haystack.encode(), b"ABSENT") is None

    def test_read_write_file_round_trip_matches_reference(self, tmp_path):
        path = str(tmp_path / "round_trip.txt")
        content = "x" * 10000 + "\nline two\n"
        assert native.write_file(path, content) is True
        assert native.read_file(path) == content
        assert Path(path).read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# 热路径性能基线（native vs 纯 Python 回退）
# ---------------------------------------------------------------------------


@_requires_native
class TestHotPathPerformance:
    """真实耗时对比：断言 native 不慢于回退超过宽容容差，并打印真实数字。"""

    def test_count_tokens_vs_tiktoken(self):
        """token 计数：native vs tiktoken（等价的分词器回退）。"""
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
        """diff 生成：native vs difflib.unified_diff。"""
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
        """diff 应用：native vs 纯 Python 应用器。"""
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
        """文件正则搜索：native vs Python ``re`` 逐行扫描。"""
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
        """文本 Jaccard：native vs 纯 Python set 交/并集。"""
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
        """向量余弦：native vs 纯 Python dot/norm。"""
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
        """子串全部位置：native vs ``str.find`` 循环。"""
        haystack = "abab" * 50000
        _compare(
            "simd_find_all(200KB, 100k hits)",
            lambda: native.simd_find_all(haystack, "ab"),
            lambda: _py_find_all(haystack, "ab"),
            tolerance=_DEFAULT_TOLERANCE,
            repeats=5,
        )

    def test_read_file_vs_pathlib(self, tmp_path):
        """读文件：native vs ``Path.read_text``。"""
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
        """原子写：native（temp+fsync+rename）vs 等价 Python 原子写。

        对照必须是**等价语义**（含 fsync 持久化）：与无 fsync 的普通写相比 native
        会显得更慢，但那不是同类比较。
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
# 观测到的 native 劣化（如实断言，不掩盖）
# ---------------------------------------------------------------------------


@_requires_native
class TestObservedNativeRegressions:
    """如实记录 native 反而慢于 stdlib 的函数。

    这两个 ``simd_*`` 函数在整个仓库中**没有任何 Python 调用方**（只有
    ``native.py`` 的导出），因此不构成生产热路径；但作为基线必须如实记录其真实
    倍率。此处只断言"未出现灾难性劣化"的上界，并在报告中给出精确数字。
    """

    def test_simd_contains_is_slower_than_stdlib(self):
        """``simd_contains`` 慢于 CPython ``in``（后者用优化的 memchr/two-way）。"""
        haystack = ("abcdefghij" * 100000) + "NEEDLE"
        native_t = _best_of(lambda: native.simd_contains(haystack, "NEEDLE"))
        py_t = _best_of(lambda: "NEEDLE" in haystack)
        print(
            f"[perf][regression] simd_contains: native={native_t * 1e6:.1f}us "
            f"python={py_t * 1e6:.1f}us ratio={native_t / max(py_t, 1e-9):.2f}x"
        )
        assert native.simd_contains(haystack, "NEEDLE") is True
        # 上界只用于防止灾难性劣化；真实倍率由报告给出（本机约 5x 慢）。
        assert native_t <= py_t * 20.0

    def test_simd_memmem_is_slower_than_stdlib(self):
        """``simd_memmem`` 慢于 CPython ``bytes.find``（本机约 28x）。"""
        haystack = (b"abcdefghij" * 100000) + b"NEEDLE"
        native_t = _best_of(lambda: native.simd_memmem(haystack, b"NEEDLE"))
        py_t = _best_of(lambda: haystack.find(b"NEEDLE"))
        print(
            f"[perf][regression] simd_memmem: native={native_t * 1e6:.1f}us "
            f"python={py_t * 1e6:.1f}us ratio={native_t / max(py_t, 1e-9):.2f}x"
        )
        assert native.simd_memmem(haystack, b"NEEDLE") == haystack.find(b"NEEDLE")
        # 上界只用于防止灾难性劣化；真实倍率由报告给出（本机约 28x 慢）。
        assert native_t <= py_t * 100.0
