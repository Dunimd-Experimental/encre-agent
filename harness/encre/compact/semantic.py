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

"""Semantic, pattern-aware context compaction utilities.

This module complements the model-driven compaction in :mod:`encre.compact.engine`
with cheaper, deterministic heuristics:

* :class:`SemanticToolOutputCompactor` recognises the shape of common tool
  outputs (grep, glob, bash, file_read, web_fetch, task lists) and replaces
  them with concise, structured summaries instead of blunt truncation.
* :class:`ContextPartitioner` splits a flat message list into ``hot`` (most
  recent, kept verbatim), ``warm`` (lightly trimmed), ``cold`` (summarised)
  and ``reference`` tiers so information density can be managed per tier.
"""


import re
from dataclasses import dataclass, field
from typing import Any

from encre.utils.tokens import estimate_tokens

# Patterns for extracting key info from tool outputs
_RE_FILE_LIST = re.compile(r'^(\S+)\s*$', re.MULTILINE)
_RE_GREP_MATCH = re.compile(r'^([^:]+):(\d+):(.*)$', re.MULTILINE)
_RE_ERROR_LINE = re.compile(r'.*(?:error|Error|ERROR|exception|Exception|Traceback|FAILED|failed).*')
_RE_PATH_LINE = re.compile(r'[/\\][\w./\\-]+')
_RE_NUMBER = re.compile(r'\b\d+\b')

# Tool-specific summarization hints
_TOOL_OUTPUT_HINTS: dict[str, str] = {
    "grep": "group matches by file, show count per file + first 3 matches",
    "glob": "list files grouped by directory, show count per directory",
    "bash": "extract errors, exit code, and key output lines",
    "file_read": "preserve function signatures and docstrings only",
    "web_fetch": "extract title, key facts, and links",
    "web_search": "extract result titles and URLs only",
    "task": "preserve task IDs, subjects, and statuses",
}


@dataclass
class ContextTier:
    """A single tier in a partitioned context."""
    name: str
    messages: list[dict[str, Any]]
    max_tokens: int = 0

    def token_count(self) -> int:
        """Total token count of all messages in this tier."""
        return sum(_estimate_message_tokens(m) for m in self.messages)


@dataclass
class ContextPartition:
    """Tiered context with hot/warm/cold/reference separation."""
    system: list[dict[str, Any]] = field(default_factory=list)
    hot: list[dict[str, Any]] = field(default_factory=list)      # Last 3 turns, always full
    warm: list[dict[str, Any]] = field(default_factory=list)     # Turns 3-10, lightly compressed
    cold: list[dict[str, Any]] = field(default_factory=list)     # Older, heavily compressed
    reference: list[dict[str, Any]] = field(default_factory=list) # On-demand, indexed

    def to_messages(self) -> list[dict[str, Any]]:
        """Flatten the partition back into a single message list.

        Reference material (on-demand, indexed) is appended last so that
        hot context stays closest to the model's attention window.
        """
        return self.system + self.cold + self.warm + self.hot + self.reference

    def total_tokens(self) -> int:
        """Total token count across every tier in the partition."""
        return sum(
            _estimate_message_tokens(m)
            for tier in [self.system, self.hot, self.warm, self.cold, self.reference]
            for m in tier
        )


class SemanticToolOutputCompactor:
    """Intelligently summarize tool outputs instead of blunt truncation.

    Recognizes common output patterns and extracts the essential information:
    - grep/glob -> summarize by file, keep first N matches
    - bash -> extract errors, exit codes, key output lines
    - file_read -> keep function signatures, strip bodies
    - web_fetch -> extract title, key facts, links
    """

    MAX_TOOL_OUTPUT_CHARS = 8000

    def compact_tool_output(self, tool_name: str, output: str) -> str:
        """Summarise a tool output, dispatching by tool name.

        Outputs shorter than :attr:`MAX_TOOL_OUTPUT_CHARS` are returned
        unchanged.  Larger outputs are routed to a tool-specific handler
        that extracts only the essential information (errors, file paths,
        signatures, links, ...).  Falls back to head/tail truncation.
        """
        if len(output) <= self.MAX_TOOL_OUTPUT_CHARS:
            return output

        if tool_name in ("grep",):
            return self._compact_grep(output)
        if tool_name in ("glob",):
            return self._compact_glob(output)
        if tool_name in ("bash",):
            return self._compact_bash(output)
        if tool_name in ("file_read", "file_read_chunk"):
            return self._compact_file_read(output)
        if tool_name in ("web_fetch",):
            return self._compact_web_fetch(output)
        if tool_name in ("task",):
            return self._compact_task_output(output)

        # Default: keep first and last chunks
        return self._compact_default(output)

    def _compact_grep(self, output: str) -> str:
        """Group grep matches by file and keep only the first few per file."""
        matches_by_file: dict[str, list[str]] = {}
        for match in _RE_GREP_MATCH.finditer(output):
            filepath = match.group(1)
            line = match.group(3).strip()[:120]
            matches_by_file.setdefault(filepath, []).append(line)

        if not matches_by_file:
            return output[:self.MAX_TOOL_OUTPUT_CHARS]

        lines: list[str] = []
        total_matches = sum(len(v) for v in matches_by_file.values())
        lines.append(f"[grep: {total_matches} matches in {len(matches_by_file)} files]")
        for filepath, matches in sorted(matches_by_file.items()):
            lines.append(f"\n{filepath} ({len(matches)} matches):")
            for m in matches[:
                3]:
                lines.append(f"  - {m}")
            if len(matches) > 3:
                lines.append(f"  ... and {len(matches) - 3} more")
        return "\n".join(lines)[:self.MAX_TOOL_OUTPUT_CHARS]

    def _compact_glob(self, output: str) -> str:
        """Group discovered files by directory for a compact listing."""
        files: list[str] = []
        for match in _RE_FILE_LIST.finditer(output):
            f = match.group(1).strip()
            if f:
                files.append(f)

        if not files:
            return output[:self.MAX_TOOL_OUTPUT_CHARS]

        by_dir: dict[str, list[str]] = {}
        for f in files:
            import os
            d = os.path.dirname(f) or "."
            by_dir.setdefault(d, []).append(os.path.basename(f))

        lines: list[str] = [f"[glob: {len(files)} files in {len(by_dir)} directories]"]
        for d, names in sorted(by_dir.items()):
            lines.append(f"\n{d}/ ({len(names)} files):")
            lines.append(f"  {', '.join(names[:10])}")
            if len(names) > 10:
                lines.append(f"  ... and {len(names) - 10} more")
        return "\n".join(lines)[:self.MAX_TOOL_OUTPUT_CHARS]

    def _compact_bash(self, output: str) -> str:
        """Extract error lines, referenced paths and trailing output."""
        error_lines = _RE_ERROR_LINE.findall(output)
        path_lines = list(set(_RE_PATH_LINE.findall(output)))

        parts: list[str] = []
        if error_lines:
            parts.append(f"[bash: {len(error_lines)} error lines]")
            parts.append("Errors:")
            for line in error_lines[:
                5]:
                parts.append(f"  {line.strip()[:200]}")
        else:
            parts.append("[bash: no errors]")

        if path_lines:
            parts.append(f"Files referenced: {', '.join(path_lines[:20])}")

        # Keep last 500 chars for context
        if len(output) > 500:
            parts.append(f"\nLast output:\n{output[-500:]}")

        return "\n".join(parts)[:self.MAX_TOOL_OUTPUT_CHARS]

    def _compact_file_read(self, output: str) -> str:
        """Keep only function/class signatures, imports and comments."""
        func_sigs: list[str] = []
        for line in output.split("\n"):
            stripped = line.strip()
            if re.match(r'^\s*(def |class |async def |@|import |from |# |//|/\*\*|export |function |const |let |var )', line):
                func_sigs.append(stripped[:120])
        if func_sigs:
            return "[File signatures]\n" + "\n".join(func_sigs[:50])
        return output[:self.MAX_TOOL_OUTPUT_CHARS]

    def _compact_web_fetch(self, output: str) -> str:
        """Extract the page title and a handful of unique links."""
        title_match = re.search(r'<title[^>]*>([^<]+)</title>', output, re.IGNORECASE)
        links = re.findall(r'https?://[^\s<>"]+', output)

        parts: list[str] = []
        if title_match:
            parts.append(f"Title: {title_match.group(1)}")
        if links:
            unique_links = list(set(links))[:10]
            parts.append(f"Links ({len(links)} total, showing {len(unique_links)}):")
            for link in unique_links:
                parts.append(f"  {link[:120]}")
        if not parts:
            return output[:self.MAX_TOOL_OUTPUT_CHARS]
        return "\n".join(parts)[:self.MAX_TOOL_OUTPUT_CHARS]

    def _compact_task_output(self, output: str) -> str:
        """Summarise task-list / task-get JSON entries."""
        matches = list(re.finditer(r'\{[^}]+\}', output))
        if matches:
            return f"[{len(matches)} task entries]\n" + "\n".join(
                m.group(0)[:200] for m in matches[:10]
            )
        return output[:self.MAX_TOOL_OUTPUT_CHARS]

    def _compact_default(self, output: str) -> str:
        """Fallback: keep the head and tail, omit the middle."""
        half = self.MAX_TOOL_OUTPUT_CHARS // 2
        return output[:half] + f"\n\n... [{len(output) - self.MAX_TOOL_OUTPUT_CHARS} chars omitted]\n\n" + output[-half:]


class ContextPartitioner:
    """Splits a flat message list into hot/warm/cold/reference tiers.

    Hot:   last 3 turns -- always kept in full
    Warm:  turns 3-10 -- lightly compressed (trim tool outputs)
    Cold:  turns 10+ -- heavily compressed (one-line summaries)
    Reference: system messages, inserted at top
    """

    def __init__(
        self,
        hot_turns: int = 3,
        warm_turns: int = 7,
        compact_cold: bool = True,
    ) -> None:
        """Configure the partitioner's tier boundaries.

        ``hot_turns`` / ``warm_turns`` define how many recent assistant
        turns stay at full / light compression; ``compact_cold`` toggles
        whether older turns are summarised.
        """
        self.hot_turns = hot_turns
        self.warm_turns = warm_turns
        self.compact_cold = compact_cold
        self._tool_compactor = SemanticToolOutputCompactor()

    def partition(self, messages: list[dict[str, Any]]) -> ContextPartition:
        """Partition a flat message list into hot/warm/cold/reference tiers."""
        result = ContextPartition()

        # Separate system from rest
        non_system: list[dict[str, Any]] = []
        for m in messages:
            if m.get("role") == "system":
                result.system.append(m)
            else:
                non_system.append(m)

        if not non_system:
            return result

        # Count turns (assistant messages)
        turn_indices: list[int] = []
        for i, m in enumerate(non_system):
            if m.get("role") == "assistant":
                turn_indices.append(i)

        if not turn_indices:
            result.hot = non_system
            return result

        total_turns = len(turn_indices)
        hot_boundary = turn_indices[max(0, total_turns - self.hot_turns)]
        warm_boundary = turn_indices[max(0, total_turns - self.hot_turns - self.warm_turns)]

        for i, m in enumerate(non_system):
            if i >= hot_boundary:
                result.hot.append(m)
            elif i >= warm_boundary:
                result.warm.append(m)
            else:
                if self.compact_cold:
                    result.cold.append(self._summarize_message(m))
                else:
                    result.cold.append(m)

        return result

    def _summarize_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Lightly compress a single message for the cold tier."""
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role in ("tool",):
            tool_name = msg.get("name", "")
            if isinstance(content, str) and len(content) > 500:
                compacted = self._tool_compactor.compact_tool_output(tool_name, content)
                return {**msg, "content": compacted}

        if isinstance(content, str) and len(content) > 2000:
            return {**msg, "content": content[:500] + " [summarized]"}

        return msg


def _estimate_message_tokens(msg: dict[str, Any]) -> int:
    """Estimate the token count of a single message (text or multimodal)."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                total += estimate_tokens(str(block.get("text", block.get("input", ""))))
        return total
    return 0
