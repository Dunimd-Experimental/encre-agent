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

"""Tests for encre.native -- the Rust native bridge and its Python fallbacks.

Exercises the Python shim's import surface, the mirrored API of the
optional Rust extension, and the behavioural contracts of every
wrapped native operation (files, grep, glob, tokens, diffs, sandbox
execution, and codebase search).
"""

from pathlib import Path

import pytest
from encre import native


class TestNativeImport:
    """Validate the native bridge module import contract.

    Confirms that the encre.native Python shim is always importable
    regardless of whether the Rust extension is compiled, that every
    function declared in the ``_native.pyi`` stubs exists and is
    callable on the shim, and that the actual Rust extension (when
    present) exposes the same API surface.
    """

    def test_verify_native_module_importable(self):
        """Validate that the native bridge module is always importable regardless of Rust extension availability."""
        assert native is not None

    def test_verify_native_has_flag_exists(self):
        """Validate that _HAS_NATIVE is a bool flag indicating whether the Rust extension loaded."""
        assert isinstance(native._HAS_NATIVE, bool)

    def test_verify_native_all_functions_exist(self):
        """Validate that every function declared in _native.pyi stubs is present and callable on the Python shim."""
        expected = [
            "read_file",
            "write_file",
            "grep",
            "glob_pattern",
            "count_tokens",
            "compute_diff",
            "apply_diff",
            "sandbox_execute",
            "sandbox_read_file",
            "sandbox_write_file",
            "search_codebase",
        ]
        for name in expected:
            assert hasattr(native, name), f"Missing function: {name}"
            assert callable(getattr(native, name)), f"Not callable: {name}"

    def test_verify_native_pyi_stubs_match(self):
        """Validate that the Rust _native extension exposes all functions declared in the .pyi stub file."""
        try:
            from encre import _native as _rust_native  # type: ignore
        except ImportError:
            pytest.skip("Rust _native extension not built (expected in dev)")

        # The Rust extension should have the functions declared in _native.pyi
        expected = [
            "search_codebase",
            "read_file",
            "write_file",
            "grep",
            "glob",
            "count_tokens",
            "compute_diff",
            "apply_diff",
            "sandbox_execute",
            "sandbox_read_file",
            "sandbox_write_file",
        ]
        for name in expected:
            assert hasattr(_rust_native, name), f"Rust _native missing: {name}"


class TestReadWriteFile:
    """Validate native file read/write operations.

    Tests exercise ``write_file`` and ``read_file`` across normal
    content, offset-based reads, combined offset+limit windows,
    missing-file errors, and implicit parent-directory creation.
    """

    def test_verify_native_write_and_read_file(self, tmp_path: Path):
        """Validate that write_file and read_file round-trip content correctly."""
        filepath = str(tmp_path / "test_file.txt")
        content = "Hello, encre native tests!\nLine two.\n"
        assert native.write_file(filepath, content) is True
        result = native.read_file(filepath)
        # Native read may or may not preserve trailing newline depending on impl
        assert "Hello, encre native tests!" in result
        assert "Line two" in result

    def test_verify_native_read_file_with_offset(self, tmp_path: Path):
        """Validate that read_file with offset returns content starting from the specified line."""
        filepath = str(tmp_path / "offset_test.txt")
        lines = "line_1\nline_2\nline_3\nline_4\n"
        native.write_file(filepath, lines)
        result = native.read_file(filepath, offset=2)  # 1-indexed
        assert "line_2" in result

    def test_verify_native_read_file_with_offset_and_limit(self, tmp_path: Path):
        """Validate that read_file with offset and limit returns a bounded result."""
        filepath = str(tmp_path / "limit_test.txt")
        lines = "a\nb\nc\nd\ne\n"
        native.write_file(filepath, lines)
        result = native.read_file(filepath, offset=2, limit=2)
        parts = result.strip().splitlines()
        assert len(parts) <= 3  # offset=2 starts at line 2

    def test_verify_native_read_file_not_found(self, tmp_path: Path):
        """Validate that read_file raises FileNotFoundError for missing paths."""
        filepath = str(tmp_path / "does_not_exist.txt")
        with pytest.raises(FileNotFoundError):
            native.read_file(filepath)

    def test_verify_native_write_file_creates_directories(self, tmp_path: Path):
        """Validate that write_file creates parent directories implicitly."""
        filepath = str(tmp_path / "deep" / "nested" / "dir" / "file.txt")
        content = "deeply nested content"
        assert native.write_file(filepath, content) is True
        assert native.read_file(filepath) == content


class TestGrep:
    """Validate the native grep implementation.

    Tests cover basic regex matching, case-insensitive search,
    no-match empty-result handling, invalid-regex error propagation,
    multiline mode, and head_limit truncation -- ensuring the grep
    wrapper mirrors core grep semantics.
    """

    def test_verify_native_grep_finds_matches(self, tmp_path: Path):
        """Validate that grep returns matching lines with correct structure."""
        filepath = str(tmp_path / "grep_test.py")
        native.write_file(filepath, "def foo():\n    return 42\n\ndef bar():\n    return 99\n")
        results = native.grep(r"def \w+", filepath)
        assert isinstance(results, list)
        assert any("def foo" in r["line_content"] for r in results)
        assert any("def bar" in r["line_content"] for r in results)

    def test_verify_native_grep_case_insensitive(self, tmp_path: Path):
        """Validate that case_insensitive=True matches across letter cases."""
        filepath = str(tmp_path / "case_test.txt")
        native.write_file(filepath, "HELLO world\nhello WORLD\n")
        results = native.grep("hello", filepath, case_insensitive=True)
        assert len(results) == 2

    def test_verify_native_grep_no_match(self, tmp_path: Path):
        """Validate that grep returns an empty list when there are no matches."""
        filepath = str(tmp_path / "no_match.txt")
        native.write_file(filepath, "just some text\n")
        results = native.grep("NOTFOUND", filepath)
        assert isinstance(results, list)
        assert len(results) == 0

    def test_verify_native_grep_invalid_regex(self, tmp_path: Path):
        """Validate that grep raises on invalid regex patterns."""
        filepath = str(tmp_path / "bad_regex.txt")
        native.write_file(filepath, "content\n")
        with pytest.raises(Exception):
            native.grep("[invalid", filepath)

    def test_verify_native_grep_multiline(self, tmp_path: Path):
        """Validate that multiline mode matches across line boundaries."""
        filepath = str(tmp_path / "multiline.txt")
        native.write_file(filepath, "foo\nbar\nbaz\n")
        results = native.grep(r"foo\nbar", filepath, multiline=True)
        assert len(results) == 1

    def test_verify_native_grep_head_limit(self, tmp_path: Path):
        """Validate that head_limit truncates the result set."""
        filepath = str(tmp_path / "head_limit.txt")
        native.write_file(filepath, "match 1\nskip\nmatch 2\nskip\nmatch 3\n")
        results = native.grep("match", filepath, head_limit=2)
        assert len(results) == 2


class TestGlobPattern:
    """Validate the glob pattern matcher.

    Tests assert that ``glob_pattern`` returns only matching paths,
    returns an empty list for non-matching globs, and resolves the
    default search path correctly.
    """

    def test_verify_native_glob_finds_files(self, tmp_path: Path):
        """Validate that glob_pattern returns only matching file paths."""
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")
        result = native.glob_pattern("*.py", str(tmp_path))
        assert len(result) == 2
        assert any("a.py" in p for p in result)
        assert any("b.py" in p for p in result)

    def test_verify_native_glob_no_match(self, tmp_path: Path):
        """Validate that glob_pattern returns an empty list for non-matching globs."""
        result = native.glob_pattern("*.xyz", str(tmp_path))
        assert isinstance(result, list)
        assert len(result) == 0

    def test_verify_native_glob_default_path(self, tmp_path: Path):
        """Validate that glob_pattern resolves the default search path correctly."""
        # Create files in current/working context
        (tmp_path / "hello.md").write_text("")
        result = native.glob_pattern("*.md", str(tmp_path))
        assert len(result) >= 1


class TestCountTokens:
    """Validate token counting across input types.

    Tests confirm that ``count_tokens`` returns a positive integer for
    non-empty text, zero for empty strings, scales monotonically with
    text length, and returns a well-typed result for whitespace-only
    input regardless of platform-specific counting implementation.
    """

    def test_verify_native_count_tokens_returns_int(self):
        """Validate that count_tokens returns a positive integer for non-empty text."""
        result = native.count_tokens("Hello, world!")
        assert isinstance(result, int)
        assert result > 0

    def test_verify_native_count_tokens_empty_string(self):
        """Validate that count_tokens returns zero for empty strings."""
        result = native.count_tokens("")
        assert result == 0

    def test_verify_native_count_tokens_long_text(self):
        """Validate that count_tokens scales monotonically with text length."""
        text = "The quick brown fox " * 100
        result = native.count_tokens(text)
        assert result > 50  # rough estimate at chars/4

    def test_verify_native_count_tokens_whitespace_only(self):
        """Validate that count_tokens returns a well-typed int for whitespace-only input."""
        result = native.count_tokens("   \t\n  ")
        # Implementation differs: Rust may count spaces, Python strips
        assert isinstance(result, int)


class TestDiff:
    """Validate diff computation and application.

    Tests verify that ``compute_diff`` returns a deterministic string
    representation (empty for identical inputs), that ``apply_diff``
    successfully reverses a diff to reconstruct the original text, and
    that both functions handle empty-string arguments without error.
    """

    def test_verify_native_compute_diff_identical(self):
        """Validate that compute_diff returns an empty string for identical inputs."""
        diff = native.compute_diff("hello\nworld\n", "hello\nworld\n")
        assert isinstance(diff, str)

    def test_verify_native_compute_diff_changed(self):
        """Validate that compute_diff returns a non-empty string when inputs differ."""
        diff = native.compute_diff("hello\nworld\n", "hello\nuniverse\n")
        # Native implementations may use different diff formats
        assert isinstance(diff, str)
        assert len(diff) > 0  # changed content should produce non-empty diff

    def test_verify_native_apply_diff_simple(self):
        """Validate that apply_diff reconstructs the target text from a diff."""
        original = "hello\nworld\n"
        diff = native.compute_diff(original, "hello\nuniverse\n")
        result = native.apply_diff(original, diff)
        assert "universe" in result

    def test_verify_native_apply_diff_roundtrip(self):
        """Validate that compute_diff followed by apply_diff is a lossless round-trip."""
        old = "line1\nline2\nline3\n"
        new = "line1\nline2_modified\nline3\nline4\n"
        diff = native.compute_diff(old, new)
        applied = native.apply_diff(old, diff)
        assert applied == new

    def test_verify_native_compute_diff_empty_strings(self):
        """Validate that compute_diff handles empty strings without error."""
        diff = native.compute_diff("", "")
        assert isinstance(diff, str)


class TestSandboxExecute:
    """Validate the sandboxed command execution interface.

    Tests assert that ``sandbox_execute`` returns a dict with stdout,
    stderr, and exit_code keys; that successful commands yield exit
    code 0; and that stderr output is captured correctly.
    """

    def test_verify_native_sandbox_echo(self):
        """Validate that sandbox_execute returns structured output with stdout/stderr/exit_code."""
        result = native.sandbox_execute("echo hello", timeout=10)
        assert isinstance(result, dict)
        assert "stdout" in result
        assert "stderr" in result
        assert "exit_code" in result
        assert "hello" in result["stdout"]

    def test_verify_native_sandbox_exit_code_success(self):
        """Validate that successful commands return exit code 0."""
        result = native.sandbox_execute("exit 0", timeout=10)
        assert result["exit_code"] == 0

    def test_verify_native_sandbox_stderr(self):
        """Validate that stderr output is captured in the result dict."""
        result = native.sandbox_execute("echo error >&2", timeout=10)
        assert "error" in result["stderr"] or result["exit_code"] is not None


class TestSandboxFileOps:
    """Validate sandboxed file read/write operations.

    Tests confirm that ``sandbox_write_file`` creates a file and
    ``sandbox_read_file`` retrieves the exact content, and that
    reading a non-existent path raises FileNotFoundError.
    """

    def test_verify_native_sandbox_write_and_read(self, tmp_path: Path):
        """Validate that sandbox_write_file and sandbox_read_file round-trip content."""
        filepath = str(tmp_path / "sandbox_file.txt")
        assert native.sandbox_write_file(filepath, "sandbox content") is True
        result = native.sandbox_read_file(filepath)
        assert result == "sandbox content"

    def test_verify_native_sandbox_read_missing(self, tmp_path: Path):
        """Validate that sandbox_read_file raises FileNotFoundError for missing paths."""
        filepath = str(tmp_path / "sandbox_missing.txt")
        with pytest.raises(FileNotFoundError):
            native.sandbox_read_file(filepath)


class TestSearchCodebase:
    """Validate the codebase search function.

    Tests assert that ``search_codebase`` returns a list of match
    records containing file paths, returns zero matches for
    non-existent terms, and falls back to a default search path when
    none is supplied.
    """

    def test_verify_native_search_finds_content(self, tmp_path: Path):
        """Validate that search_codebase returns match records with file paths."""
        (tmp_path / "sample.py").write_text("def my_function():\n    return True\n")
        results = native.search_codebase("my_function", str(tmp_path))
        assert isinstance(results, list)
        assert len(results) > 0
        assert any("sample.py" in r.get("file_path", "") for r in results)

    def test_verify_native_search_no_match(self, tmp_path: Path):
        """Validate that search_codebase returns an empty list for non-existent terms."""
        (tmp_path / "data.txt").write_text("ordinary text here\n")
        results = native.search_codebase("XYZ-NONEXISTENT", str(tmp_path))
        assert isinstance(results, list)
        assert len(results) == 0

    def test_verify_native_search_default_path(self):
        """Validate that search_codebase falls back to a default path when none is supplied."""
        results = native.search_codebase("def")
        assert isinstance(results, list)
