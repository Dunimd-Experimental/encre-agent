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

"""Diff / file & directory comparison tool.

Produces unified diffs between files or directories, with optional
statistics and patch views to help the model review changes.

Supports four comparison actions:

* ``text``: Diff two text strings directly.
* ``file``: Diff two files on disk (auto-detects binary vs. text).
* ``directory``: Diff two directory trees, reporting files that are
  only-in-left, only-in-right, differing, or identical.
* ``statistics``: Return change counts and similarity ratio without
  producing the full diff output.

Text and file comparisons support four output formats:

* ``unified``: Standard unified diff format (default).
* ``context``: Context diff format.
* ``json``: Structured JSON with per-change details.
* ``html``: HTML table view of the diff.

Binary files are compared using SHA-256 hash equality rather than
line-based diffing, since binary data has no meaningful line structure.
"""

import difflib
import json
import os
from pathlib import Path
from typing import Any

from encre.tools.base import build_tool


async def _diff_execute(**kwargs: Any) -> str:
    """Execute the diff comparison and return the result.

    This is the internal executor function bound to the ``diff``
    tool's ``execute`` field. It dispatches to the appropriate
    comparison mode based on the ``action`` parameter.

    Args:
        **kwargs: Arbitrary keyword arguments passed through by the
            tool dispatcher. Recognised keys:

            * ``action`` (str): One of ``"text"``, ``"file"``,
              ``"directory"``, or ``"statistics"``.
            * ``text1`` / ``text2`` (str): Text content for text
              comparisons.
            * ``file1`` / ``file2`` (str): File or directory paths
              for file/directory comparisons.
            * ``context_lines`` (int): Context lines around changes
              (default 3).
            * ``ignore_case`` (bool): Case-insensitive comparison.
            * ``ignore_whitespace`` (bool): Ignore whitespace
              differences.
            * ``output_format`` (str): One of ``"unified"``,
              ``"context"``, ``"json"``, or ``"html"``.

    Returns:
        A string containing the diff output in the requested format,
        or an error message if validation fails.
    """
    action = kwargs.get("action", "")
    text1 = kwargs.get("text1", "")
    text2 = kwargs.get("text2", "")
    file1 = kwargs.get("file1", "")
    file2 = kwargs.get("file2", "")
    context_lines = kwargs.get("context_lines", 3)
    ignore_case = kwargs.get("ignore_case", False)
    ignore_whitespace = kwargs.get("ignore_whitespace", False)
    output_format = kwargs.get("output_format", "unified")

    if action == "text":
        if not text1 or not text2:
            return "Missing required fields: text1 and text2"

        # Preserve line endings so the diff output accurately reflects
        # the original text structure.
        lines1 = text1.splitlines(keepends=True)
        lines2 = text2.splitlines(keepends=True)

        # Case-insensitive comparison lowercases both inputs before
        # diffing; the original case is not preserved in the output.
        if ignore_case:
            lines1 = [line.lower() for line in lines1]
            lines2 = [line.lower() for line in lines2]

        return _format_diff(lines1, lines2, "text1", "text2", context_lines, output_format, ignore_whitespace)

    elif action == "file":
        if not file1 or not file2:
            return "Missing required fields: file1 and file2"
        if not os.path.exists(file1):
            return f"File not found: {file1}"
        if not os.path.exists(file2):
            return f"File not found: {file2}"

        try:
            content1 = Path(file1).read_text(encoding="utf-8")
            content2 = Path(file2).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Binary files cannot be diffed line-by-line; fall back to
            # SHA-256 hash comparison.
            return _binary_diff(file1, file2)
        except OSError as e:
            return f"File error: {e}"

        lines1 = content1.splitlines(keepends=True)
        lines2 = content2.splitlines(keepends=True)

        if ignore_case:
            lines1 = [line.lower() for line in lines1]
            lines2 = [line.lower() for line in lines2]

        name1 = os.path.basename(file1)
        name2 = os.path.basename(file2)
        return _format_diff(lines1, lines2, name1, name2, context_lines, output_format, ignore_whitespace)

    elif action == "directory":
        if not file1 or not file2:
            return "Missing required fields: file1 and file2 (directory paths)"
        if not os.path.isdir(file1):
            return f"Directory not found: {file1}"
        if not os.path.isdir(file2):
            return f"Directory not found: {file2}"

        return _dir_diff(file1, file2)

    elif action == "statistics":
        if not text1 and not file1:
            return "Missing required field: text1 or file1"
        if not text2 and not file2:
            return "Missing required field: text2 or file2"

        # Resolve the source of the two texts: use file content if
        # both file paths are provided, otherwise fall back to text
        # inputs.
        try:
            if file1 and file2:
                t1 = Path(file1).read_text(encoding="utf-8")
                t2 = Path(file2).read_text(encoding="utf-8")
            else:
                t1, t2 = text1, text2
        except OSError as e:
            return f"File error: {e}"

        lines1 = t1.splitlines()
        lines2 = t2.splitlines()

        # SequenceMatcher computes the optimal alignment between the
        # two sequences, which we use to count insertions, deletions,
        # and replacements.
        matcher = difflib.SequenceMatcher(
            None,
            [line.lower() if ignore_case else line for line in lines1],
            [line.lower() if ignore_case else line for line in lines2],
        )
        ratio = matcher.ratio()

        added = 0
        removed = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "insert":
                added += j2 - j1
            elif tag == "delete":
                removed += i2 - i1
            elif tag == "replace":
                removed += i2 - i1
                added += j2 - j1

        return json.dumps({
            "file1": file1 or "text1",
            "file2": file2 or "text2",
            "lines_in_file1": len(lines1),
            "lines_in_file2": len(lines2),
            "added_lines": added,
            "removed_lines": removed,
            "changed_lines": added + removed,
            "similarity_ratio": round(ratio, 4),
            "similarity_percent": f"{ratio * 100:.1f}%",
        }, ensure_ascii=False, indent=2)

    return f"Unknown action: {action}. Supported: text, file, directory, statistics"


def _format_diff(
    lines1: list[str],
    lines2: list[str],
    name1: str,
    name2: str,
    context: int,
    output_format: str,
    ignore_ws: bool,
) -> str:
    """Format the diff between two line sequences in the requested output format.

    This helper dispatches to the appropriate ``difflib`` function
    based on the ``output_format`` parameter. When ``ignore_ws`` is
    true, leading and trailing whitespace is stripped from each line
    before diffing so that indentation differences do not appear as
    changes.

    Args:
        lines1: Lines of the first text (after case normalisation if
            requested).
        lines2: Lines of the second text.
        name1: Display name for the first input (used in diff headers).
        name2: Display name for the second input.
        context: Number of context lines to include around each change.
        output_format: One of ``"unified"``, ``"context"``, ``"json"``,
            or ``"html"``.
        ignore_ws: When true, strip whitespace before diffing.

    Returns:
        The formatted diff output string, or an error message if the
        format is unsupported.
    """
    if ignore_ws:
        # Strip all whitespace; re-append a newline to preserve line
        # boundaries for the diff algorithms.
        lines1 = [line.strip() + "\n" for line in lines1]
        lines2 = [line.strip() + "\n" for line in lines2]

    if output_format == "unified":
        diff = difflib.unified_diff(lines1, lines2, fromfile=name1, tofile=name2, n=context)
        result = "".join(diff)
        if not result:
            return "Files are identical"
        return result

    elif output_format == "context":
        diff = difflib.context_diff(lines1, lines2, fromfile=name1, tofile=name2, n=context)
        result = "".join(diff)
        if not result:
            return "Files are identical"
        return result

    elif output_format == "json":
        # Build a structured JSON representation of each change,
        # including line numbers and content for both the original and
        # modified sides.
        matcher = difflib.SequenceMatcher(None, lines1, lines2)
        changes = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            change = {
                "type": tag,
            }
            if tag in ("delete", "replace"):
                change["original"] = {
                    "start_line": i1 + 1,
                    "end_line": i2,
                    "content": "".join(lines1[i1:i2]),
                }
            if tag in ("insert", "replace"):
                change["modified"] = {
                    "start_line": j1 + 1,
                    "end_line": j2,
                    "content": "".join(lines2[j1:j2]),
                }
            changes.append(change)
        return json.dumps({
            "file1": name1,
            "file2": name2,
            "changes": changes,
            "total_changes": len(changes),
        }, ensure_ascii=False, indent=2)

    elif output_format == "html":
        from difflib import HtmlDiff
        diff = HtmlDiff(tabsize=4, wrapcolumn=80)
        result = diff.make_table(lines1, lines2, name1, name2, context=True, numlines=context)
        return f"<html><body>{result}</body></html>"

    return f"Unsupported output format: {output_format}"


def _binary_diff(file1: str, file2: str) -> str:
    """Compare two binary files using SHA-256 hash equality.

    Binary files do not have a meaningful line structure, so instead of
    line-based diffing this function computes the SHA-256 hash and size
    of each file and reports whether they are identical.

    Files are read in 64 KB chunks to avoid loading large files entirely
    into memory at once.

    Args:
        file1: Path to the first file.
        file2: Path to the second file.

    Returns:
        A JSON string containing the hash comparison result, or an
        error message if a file cannot be read.
    """
    try:
        import hashlib
        h1 = hashlib.sha256()
        h2 = hashlib.sha256()
        size1 = 0
        size2 = 0

        # Stream each file in 64 KB chunks to keep memory usage flat
        # regardless of file size.
        with open(file1, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h1.update(chunk)
                size1 += len(chunk)

        with open(file2, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h2.update(chunk)
                size2 += len(chunk)

        return json.dumps({
            "file1": os.path.basename(file1),
            "file2": os.path.basename(file2),
            "type": "binary",
            "size1": size1,
            "size2": size2,
            "same_size": size1 == size2,
            "same_hash": h1.hexdigest() == h2.hexdigest(),
            "hash1": h1.hexdigest(),
            "hash2": h2.hexdigest(),
        }, ensure_ascii=False, indent=2)
    except OSError as e:
        return f"File error: {e}"


def _dir_diff(dir1: str, dir2: str) -> str:
    """Recursively compare two directory trees.

    Uses ``filecmp.dircmp`` to build a comparison tree, then walks it
    recursively to produce a JSON report of files that exist only in
    the left directory, only in the right directory, differ between
    the two, or are identical.

    The recursion is performed via a nested helper function ``_walk``
    that processes each subdirectory comparison object.

    Args:
        dir1: Path to the first directory.
        dir2: Path to the second directory.

    Returns:
        A JSON string containing the recursive directory comparison
        result.
    """
    import filecmp

    comparison = filecmp.dircmp(dir1, dir2)

    def _walk(cmp: filecmp.dircmp, path: str = "") -> dict:
        """Recursively process a dircmp object into a flat dict.

        This inner function avoids passing the current path as a
        closure variable, keeping the recursive traversal explicit.
        File paths are constructed relative to the root of the
        comparison, using forward-slash separators.

        Args:
            cmp: The ``dircmp`` object for the current directory
                level.
            path: The relative path prefix for files at this level
                (empty string for the root).

        Returns:
            A dictionary with keys ``only_in_left``,
            ``only_in_right``, ``differing_files``,
            ``identical_files``, and ``subdirs``.
        """
        result = {
            "only_in_left": [f"{path}/{f}" if path else f for f in cmp.left_only],
            "only_in_right": [f"{path}/{f}" if path else f for f in cmp.right_only],
            "differing_files": [f"{path}/{f}" if path else f for f in cmp.diff_files],
            "identical_files": [f"{path}/{f}" if path else f for f in cmp.same_files],
            "subdirs": {},
        }
        for subdir, sub_cmp in cmp.subdirs.items():
            sub_path = f"{path}/{subdir}" if path else subdir
            result["subdirs"][subdir] = _walk(sub_cmp, sub_path)
        return result

    return json.dumps(_walk(comparison), ensure_ascii=False, indent=2)


diff_tool = build_tool(
    name="diff",
    description=(
        "Compare text, files, or directories and return a structured diff. "
        "Supports unified/context/JSON/HTML output formats for text and file "
        "comparisons, SHA-256 binary diff for non-text files, directory tree "
        "diff (files only-in-left/right/differing/identical), and statistics "
        "(added/removed lines, similarity ratio). Use this instead of bash "
        "`diff` for richer structured output and for binary/directory modes. "
        "TIP: Use action='statistics' when you only need a similarity ratio "
        "and change counts, not the full diff. "
        "AVOID: Comparing huge directories with action='directory' when you "
        "only care about a few files -- scope the paths first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["text", "file", "directory", "statistics"],
                "description": "Comparison action (required). 'text' diffs two text strings; 'file' diffs two files; 'directory' diffs two directory trees; 'statistics' returns change counts and similarity ratio.",
            },
            "text1": {"type": "string", "description": "First text content (required for action='text' or 'statistics' without file inputs)."},
            "text2": {"type": "string", "description": "Second text content (required for action='text' or 'statistics' without file inputs)."},
            "file1": {"type": "string", "description": "Path to the first file or directory (required for action='file'/'directory'; optional for 'statistics')."},
            "file2": {"type": "string", "description": "Path to the second file or directory (required for action='file'/'directory'; optional for 'statistics')."},
            "context_lines": {"type": "integer", "description": "Number of context lines to show around each change (optional, default 3)."},
            "ignore_case": {"type": "boolean", "description": "Case-insensitive comparison (optional, default false)."},
            "ignore_whitespace": {"type": "boolean", "description": "Ignore whitespace differences when diffing (optional, default false)."},
            "output_format": {
                "type": "string",
                "enum": ["unified", "context", "json", "html"],
                "description": "Output format for text/file actions (optional, default 'unified').",
            },
        },
        "required": ["action"],
    },
    execute=_diff_execute,
    intents=["general", "coding"],
    category="code_intel",
    semantic_type="read",
    cost_level="low",
    retryability="auto",
    is_concurrency_safe=lambda _: True,
    is_readonly=lambda _: True,
)
