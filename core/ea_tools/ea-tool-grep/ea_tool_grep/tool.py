#!/usr/bin/env python3

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

"""High-fidelity grep that uses ripgrep when available, with a Rust-native
fallback covering the same flag set. The flags mirror the ripgrep CLI so the
model can rely on familiar semantics regardless of which backend runs."""

import asyncio
import fnmatch
import os
import re
import shutil
from typing import Any

from encre import native as _native
from encre.tools._encoding import decode_bytes
from encre.tools.base import build_tool
from encre.tools.tool_ui import RESULT_MATCH_COUNT, ui_dict

_TYPE_GLOBS: dict[str, list[str]] = {
    "py": ["*.py"],
    "python": ["*.py"],
    "js": ["*.js", "*.jsx", "*.mjs", "*.cjs"],
    "ts": ["*.ts", "*.tsx", "*.mts", "*.cts"],
    "json": ["*.json"],
    "md": ["*.md", "*.markdown"],
    "css": ["*.css"],
    "html": ["*.html", "*.htm"],
    "yaml": ["*.yml", "*.yaml"],
    "rust": ["*.rs"],
    "go": ["*.go"],
    "java": ["*.java"],
    "c": ["*.c", "*.h"],
    "cpp": ["*.cc", "*.cpp", "*.cxx", "*.hpp", "*.hh"],
    "sql": ["*.sql"],
}


async def _grep_execute(**kwargs: Any) -> str:
    """Search files for a regex pattern. Uses ripgrep when available, falls back to Python."""
    pattern = str(kwargs.get("pattern", ""))
    path = str(kwargs.get("path") or ".")
    glob_filter = str(kwargs.get("glob") or "")
    type_filter = str(kwargs.get("type") or "")
    output_mode = str(kwargs.get("output_mode") or "content")
    case_insensitive = bool(kwargs.get("-i") or kwargs.get("case_insensitive"))
    show_numbers = bool(kwargs.get("-n", True))
    context_after = int(kwargs.get("-A") or kwargs.get("after_context") or 0)
    context_before = int(kwargs.get("-B") or kwargs.get("before_context") or 0)
    context = kwargs.get("-C") or kwargs.get("context")
    if context is not None:
        context_after = context_before = int(context)
    head_limit_raw = kwargs.get("head_limit")
    head_limit = int(head_limit_raw) if head_limit_raw not in (None, "") else None
    offset_raw = kwargs.get("offset")
    offset = int(offset_raw) if offset_raw not in (None, "") else 0
    multiline = bool(kwargs.get("multiline"))
    fixed_strings = bool(kwargs.get("-F") or kwargs.get("fixed_strings"))
    word_regexp = bool(kwargs.get("-w") or kwargs.get("word_regexp"))

    if not pattern:
        return "Error: pattern is required"

    rg = shutil.which("rg")
    if rg and not multiline:
        try:
            return await _run_rg(
                rg, pattern, path, output_mode,
                case_insensitive, show_numbers,
                context_after, context_before,
                glob_filter, type_filter, head_limit,
                offset, fixed_strings, word_regexp,
            )
        except Exception:
            pass

    return _run_python(
        pattern, path, output_mode,
        case_insensitive, show_numbers,
        context_after, context_before,
        glob_filter, type_filter, head_limit,
        multiline, offset, fixed_strings, word_regexp,
    )


# ------------------------------------------------------------------
# ripgrep backend
# ------------------------------------------------------------------

async def _run_rg(
    rg: str,
    pattern: str,
    path: str,
    output_mode: str,
    case_insensitive: bool,
    show_numbers: bool,
    context_after: int,
    context_before: int,
    glob_filter: str,
    type_filter: str,
    head_limit: int | None,
    offset: int,
    fixed_strings: bool,
    word_regexp: bool,
) -> str:
    """Search using ripgrep (rg) CLI with the given flags and output mode."""
    args: list[str] = [rg, "--color=never"]
    if case_insensitive:
        args.append("-i")
    if fixed_strings:
        args.append("-F")
    if word_regexp:
        args.append("-w")
    if output_mode == "files_with_matches":
        args.append("-l")
    elif output_mode == "count":
        args.append("-c")
    else:
        if show_numbers:
            args.append("-n")
        if context_after:
            args.extend(["-A", str(context_after)])
        if context_before:
            args.extend(["-B", str(context_before)])
    if glob_filter:
        args.extend(["-g", glob_filter])
    if type_filter and type_filter in _TYPE_GLOBS:
        # Use --type-add to ensure exact alias coverage even when rg
        # doesn't ship that alias.
        globs = ",".join(_TYPE_GLOBS[type_filter])
        args.extend(["--type-add", f"encre:{globs}", "--type", "encre"])
    args.extend(["--", pattern, path])

    from encre.capabilities.process import hidden_subprocess_kwargs
    kwargs = hidden_subprocess_kwargs()
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=30
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return "Error: grep timed out after 30 seconds"
    if proc.returncode not in (0, 1):  # 1 = no matches in rg
        err = decode_bytes(stderr).strip()
        if err:
            raise RuntimeError(f"ripgrep failed: {err}")
    text = decode_bytes(stdout)
    if offset > 0 or (head_limit is not None and head_limit > 0):
        lines = text.splitlines()
        start = max(0, offset)
        window = lines[start:start + head_limit] if head_limit and head_limit > 0 else lines[start:]
        if not window:
            return "(no matches)"
        dropped = len(lines) - start - len(window)
        if dropped > 0:
            window.append(f"... ({dropped} more line(s) truncated)")
        text = "\n".join(window)
    return text if text else "(no matches)"


# ------------------------------------------------------------------
# Python backend
# ------------------------------------------------------------------

def _run_python(
    pattern: str,
    path: str,
    output_mode: str,
    case_insensitive: bool,
    show_numbers: bool,
    context_after: int,
    context_before: int,
    glob_filter: str,
    type_filter: str,
    head_limit: int | None,
    multiline: bool,
    offset: int,
    fixed_strings: bool,
    word_regexp: bool,
) -> str:
    """Fallback grep using the Rust native engine — returns context directly."""
    # -F: match the literal pattern; -w: require word boundaries. Both are
    # expressed as regex transforms so the Rust backend needs no new flags.
    if fixed_strings:
        pattern = re.escape(pattern)
    if word_regexp:
        pattern = rf"\b(?:{pattern})\b"
    rust_glob = glob_filter or None

    try:
        if output_mode in ("files_with_matches", "count"):
            raw = _native.grep(pattern, path, case_insensitive, rust_glob, multiline, None, context_before, context_after)
        else:
            raw = _native.grep(pattern, path, case_insensitive, rust_glob, multiline, head_limit, context_before, context_after)
    except Exception as exc:
        return f"Error: grep failed: {exc}"

    if not raw:
        return "(no matches)"

    if type_filter and type_filter in _TYPE_GLOBS:
        type_globs = _TYPE_GLOBS[type_filter]
        raw = [r for r in raw if any(
            fnmatch.fnmatch(os.path.basename(r["file_path"]), g) for g in type_globs
        )]

    if not raw:
        return "(no matches)"

    if output_mode == "files_with_matches":
        return _fmt_files_with_matches(raw, head_limit, offset)
    if output_mode == "count":
        return _fmt_count(raw, head_limit, offset)
    return _fmt_content(raw, show_numbers, head_limit, offset)


def _fmt_files_with_matches(
    results: list[dict],
    head_limit: int | None,
    offset: int,
) -> str:
    seen: set[str] = set()
    files: list[str] = []
    for r in results:
        fp = r["file_path"]
        if fp not in seen:
            seen.add(fp)
            files.append(fp)
    start = max(0, offset)
    window = files[start:start + head_limit] if head_limit and head_limit > 0 else files[start:]
    return "\n".join(sorted(window)) if window else "(no matches)"


def _fmt_count(
    results: list[dict],
    head_limit: int | None,
    offset: int,
) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r["file_path"]] = counts.get(r["file_path"], 0) + 1
    ordered = sorted(counts)
    start = max(0, offset)
    window = ordered[start:start + head_limit] if head_limit and head_limit > 0 else ordered[start:]
    return "\n".join(f"{fp}:{counts[fp]}" for fp in window) if window else "(no matches)"


def _fmt_content(
    results: list[dict],
    show_numbers: bool,
    head_limit: int | None,
    offset: int,
) -> str:
    from collections import defaultdict
    by_file: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_file[r["file_path"]].append(r)

    out_lines: list[str] = []
    for file_path in sorted(by_file):
        matches = by_file[file_path]

        for m in matches:
            if m["line_number"] == 0:
                # Multiline — show entire matched snippet
                out_lines.append(f"{file_path}:{m['line_content']}")
            else:
                # Context before
                for ln, lc in m.get("context_before", []):
                    out_lines.append(f"{file_path}-{ln}-{lc}" if show_numbers else f"{file_path}-{lc}")
                # The matched line
                ln = m["line_number"]
                lc = m["line_content"]
                out_lines.append(f"{file_path}:{ln}:{lc}" if show_numbers else f"{file_path}:{lc}")
                # Context after
                for ln, lc in m.get("context_after", []):
                    out_lines.append(f"{file_path}-{ln}-{lc}" if show_numbers else f"{file_path}-{lc}")

    start = max(0, offset)
    window = out_lines[start:]
    if head_limit is not None and head_limit > 0:
        window = window[:head_limit]
    if not window:
        return "(no matches)"
    dropped = len(out_lines) - start - len(window)
    if dropped > 0:
        window.append(f"... ({dropped} more line(s) truncated)")
    return "\n".join(window)


grep_tool = build_tool(
    name="grep",
    description=(
        "Search files for a regex pattern. Wraps ripgrep when available, with "
        "a Rust-native fallback covering the same flag set. Use this instead "
        "of bash `grep`/`rg` -- it returns structured output, supports "
        "context lines (-A/-B/-C), line numbers, multiline patterns, file-type "
        "filtering, glob filters, case-insensitive matching, literal (-F) and "
        "whole-word (-w) matching, head_limit/offset paging, and three output "
        "modes (content / files_with_matches / count). "
        "TIP: Use output_mode='files_with_matches' to get just the file list "
        "when you only need to know where matches live. "
        "TIP: Scope with 'path' and 'type'/'glob' to keep results focused and "
        "fast. "
        "AVOID: Very broad regexes across the whole workspace without a type "
        "filter -- they can produce huge outputs. "
        "For persistent memory search, use memory_search. For web search, use web_search."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression pattern to search for (required).",
            },
            "path": {
                "type": "string",
                "description": (
                    "File or directory to search (optional, default: current "
                    "directory). Use an absolute path for reproducible results."
                ),
            },
            "glob": {
                "type": "string",
                "description": "Glob pattern to filter files (optional), e.g. \"*.py\", \"**/*.ts\".",
            },
            "type": {
                "type": "string",
                "description": (
                    "File type alias (optional): py, python, js, ts, json, md, "
                    "css, html, yaml, rust, go, java, c, cpp, sql. Filters "
                    "files like ripgrep --type does."
                ),
            },
            "-i": {
                "type": "boolean",
                "description": "Case-insensitive search (optional, default false).",
            },
            "-n": {
                "type": "boolean",
                "description": "Include line numbers (optional, default true in content mode).",
            },
            "-F": {
                "type": "boolean",
                "description": "Treat the pattern as a literal string, no regex interpretation (optional, default false).",
            },
            "-w": {
                "type": "boolean",
                "description": "Match only whole words (optional, default false).",
            },
            "-A": {
                "type": "integer",
                "description": "Lines of context to show after each match (optional).",
            },
            "-B": {
                "type": "integer",
                "description": "Lines of context to show before each match (optional).",
            },
            "-C": {
                "type": "integer",
                "description": "Lines of context to show before and after each match (optional). Overrides -A/-B.",
            },
            "multiline": {
                "type": "boolean",
                "description": "Enable multiline mode: '.' matches newlines and patterns can span lines (optional, default false).",
            },
            "head_limit": {
                "type": "integer",
                "description": "Cap the number of output lines, files, or counts (optional).",
            },
            "offset": {
                "type": "integer",
                "description": (
                    "Skip the first N output entries before applying "
                    "head_limit, for paging through large result sets "
                    "(optional, default 0)."
                ),
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": (
                    "Output mode (optional, default: content). 'content' "
                    "shows matched lines with context; 'files_with_matches' "
                    "lists only file paths; 'count' shows per-file match counts."
                ),
            },
        },
        "required": ["pattern"],
    },
    execute=_grep_execute,
    ui=ui_dict(
        icon="search", label="Grep",
        result=RESULT_MATCH_COUNT,
        keys=("query", "pattern", "glob", "path"),
    ),
    intents=["general", "coding", "data"],
    category="search",
    triggers=["search", "find", "grep", "rg", "ripgrep", "search code", "code search"],
    semantic_type="search",
    cost_level="low",
    retryability="auto",
    safe_fallback="Narrow the path, add a file type filter, or refine the regex before retrying the search.",
    is_concurrency_safe=lambda _: True,
    is_readonly=True,
)
