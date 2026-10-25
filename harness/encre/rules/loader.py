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

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from encre.config import get_data_dir

from encre.logging_config import get_logger


@dataclass
class RuleFile:
    """A single loaded rule document with its source label and priority."""
    source: str
    content: str
    priority: int = 0


_GLOBAL_RULES_DIR = "rules"
# Name of the subdirectory (under the Encre data dir) that holds global rules.

_PROJECT_RULE_PATTERNS: list[tuple[str, int, str]] = [
    (".encre/rules.md", 100, "encre"),
    (".cursorrules", 90, "cursor"),
    (".windsurfrules", 85, "windsurf"),
    (".clinerules", 80, "cline"),
    ("CLAUDE.md", 75, "claude"),
    (".github/copilot-instructions.md", 60, "copilot"),
]

# AGENTS.md is a Codex-standard instruction file.  It is loaded through the
# Codex compatibility layer so we also get global AGENTS, override files,
# fallback filenames, and the layered directory chain used by Codex CLI.
_CODEX_RULE_PRIORITY = 65

# Map a project-rule source label back to the filename it came from, so
# per-workspace disabled context files (CLAUDE.md / AGENTS.md) can be matched.
_PROJECT_SOURCE_TO_FILE = {
    name: os.path.basename(rel_path)
    for rel_path, _priority, name in _PROJECT_RULE_PATTERNS
}


def read_global_rule_text(path: str) -> str:
    """Read a global rule file, decrypting it at rest.

    Global rules live beside ``soul/`` and ``memory/`` and are encrypted with
    the same AES-256-GCM master key.  Rule files written before that migration
    are still plain Markdown, so a failed decrypt falls back to the raw text.

    Args:
        path: Path of the rule file to read.

    Returns:
        The rule text, or ``""`` when the file cannot be read.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip()
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return ""
    if not raw:
        return ""
    try:
        from encre.crypto import decrypt
        return decrypt(raw).strip()
    except Exception:
        # Legacy plaintext rule, or a master key we cannot reach: use as-is.
        return raw


def _rule_file_name(rule: "RuleFile") -> str:
    """Return the filename a rule was loaded from (used for exclusion)."""
    if rule.source.startswith("codex:"):
        return rule.source[len("codex:"):]
    return _PROJECT_SOURCE_TO_FILE.get(rule.source, rule.source)


class RulesLoader:
    """Loads and caches project, global, and Codex rule files."""

    def __init__(self) -> None:
        """Initialise an empty signature-based cache."""
        self._cache: dict[str, tuple[tuple[Any, ...], list[RuleFile]]] = {}

    def clear_cache(self) -> None:
        """Drop all cached rule results."""
        self._cache.clear()

    def _project_signature(self, workspace_path: str) -> tuple[Any, ...]:
        """Build a cache signature from project rule file mtimes/sizes."""
        if not workspace_path or not os.path.isdir(workspace_path):
            return ()
        sig: list[Any] = []
        # Record mtime/size for each candidate file so changes invalidate cache.
        for rel_path, priority, name in _PROJECT_RULE_PATTERNS:
            full_path = os.path.join(workspace_path, rel_path)
            try:
                st = os.stat(full_path)
                sig.append((name, rel_path, st.st_mtime_ns, st.st_size, priority))
            except OSError:
                sig.append((name, rel_path, None, None, priority))
        return tuple(sig)

    def _global_signature(self) -> tuple[Any, ...]:
        """Build a cache signature from global rule markdown file mtimes/sizes."""
        rules_dir = get_data_dir() / _GLOBAL_RULES_DIR
        if not rules_dir.is_dir():
            return ()
        sig: list[Any] = []
        try:
            for entry in sorted(rules_dir.iterdir(), key=lambda p:
                p.name):
                if entry.suffix.lower() != ".md" or not entry.is_file():
                    continue
                try:
                    st = entry.stat()
                    sig.append((entry.name, st.st_mtime_ns, st.st_size))
                except OSError:
                    sig.append((entry.name, None, None))
        except Exception:
            return ()
        return tuple(sig)

    def load_project_rules(
        self,
        workspace_path: str,
        disabled_files: set[str] | None = None,
    ) -> list[RuleFile]:
        """Load workspace rule files, sorted by descending priority (cached)."""
        cache_key = f"project:{workspace_path}"
        if not workspace_path or not os.path.isdir(workspace_path):
            return []
        signature = self._project_signature(workspace_path)
        cached = self._cache.get(cache_key)
        if cached is not None and cached[0] == signature:
            rules = list(cached[1])
        else:
            rules: list[RuleFile] = []
            # Read every candidate file; absent files simply yield no rule.
            for rel_path, priority, name in _PROJECT_RULE_PATTERNS:
                full_path = os.path.join(workspace_path, rel_path)
                content = self._read_file(full_path)
                if content:
                    rules.append(RuleFile(source=name, content=content, priority=priority))

            # Higher-priority rules come first so they appear earlier in the prompt.
            rules.sort(key=lambda r: -r.priority)
            self._cache[cache_key] = (signature, rules)

        # Drop per-workspace-disabled context files (e.g. CLAUDE.md).
        if disabled_files:
            rules = [r for r in rules if _rule_file_name(r) not in disabled_files]
        return rules

        # Higher-priority rules come first so they appear earlier in the prompt.
        rules.sort(key=lambda r: -r.priority)
        self._cache[cache_key] = (signature, rules)
        return rules

    def load_global_rules(self) -> list[RuleFile]:
        """Load global rule files from the data dir (cached); priority 50."""
        cache_key = "global"
        rules_dir = get_data_dir() / _GLOBAL_RULES_DIR
        signature = self._global_signature()
        cached = self._cache.get(cache_key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        if not rules_dir.is_dir():
            self._cache[cache_key] = (signature, [])
            return []

        rules: list[RuleFile] = []
        # Collect every top-level *.md file in the global rules directory.
        try:
            for entry in sorted(rules_dir.iterdir()):
                if entry.suffix.lower() == ".md" and entry.is_file():
                    content = read_global_rule_text(str(entry))
                    if content:
                        name = entry.stem
                        rules.append(RuleFile(source=f"global:{name}", content=content, priority=50))
        except Exception:
            logger.debug("exception suppressed", exc_info=True)

        self._cache[cache_key] = (signature, rules)
        return rules

    def load_codex_rules(
        self,
        workspace_path: str,
        disabled_files: set[str] | None = None,
    ) -> list[RuleFile]:
        """Load Codex-style instructions for *workspace_path*.

        This covers ``~/.codex/AGENTS.md``, the layered ``AGENTS.md`` chain,
        ``developer_instructions``, and ``model_instructions_file`` from
        ``.codex/config.toml``.  The result is intentionally not cached here;
        :class:`RulesLoader` keeps the simple file-based cache for the fixed
        project-rule patterns and recomputes Codex rules on each prompt build.
        """
        try:
            from encre.codex_compat import build_codex_context
        except Exception as e:  # pragma: no cover - tomllib may be missing
            logger = get_logger(__name__)
            logger.debug("Codex compatibility layer unavailable: %s", e)
            return []

        ctx = build_codex_context(workspace_path)
        ws_norm = os.path.normcase(os.path.abspath(workspace_path)) if workspace_path else None
        rules: list[RuleFile] = []
        for path, content in ctx.instructions:
            label = f"codex:{Path(path).name}"
            # Only exclude context files that live inside this workspace; a
            # global ~/.codex/AGENTS.md outside the workspace keeps applying.
            if disabled_files and Path(path).name in disabled_files:
                if ws_norm and os.path.normcase(os.path.abspath(path)).startswith(ws_norm):
                    continue
            rules.append(RuleFile(source=label, content=content, priority=_CODEX_RULE_PRIORITY))
        return rules

    def build_rules_prompt(self, workspace_path: str, enable_project: bool = True, enable_global: bool = True, disabled_files: set[str] | None = None) -> str:
        """Assemble all enabled rules into one prompt string.

        Global rules are emitted first, then project rules, then Codex
        instructions, with each block separated by a horizontal divider.

        Args:
            workspace_path: Workspace root used to find project rules.
            enable_project: Whether to include project/Codex rules.
            enable_global: Whether to include global rules.
            disabled_files: Optional set of filenames (e.g. ``"CLAUDE.md"``,
                ``"AGENTS.md"``) whose contents must be excluded entirely.

        Returns:
            The concatenated rules prompt, or ``""`` when no rules exist.
        """
        blocks: list[str] = []
        # Assemble each enabled rule group into its own labelled block.

        if enable_global:
            global_rules = self.load_global_rules()
            for rule in global_rules:
                label = rule.source.replace("global:", "")
                blocks.append(f"[Global Rule: {label}]\n{rule.content}")

        if enable_project and workspace_path and os.path.isdir(workspace_path):
            project_rules = self.load_project_rules(workspace_path, disabled_files=disabled_files)
            for rule in project_rules:
                label = rule.source
                blocks.append(f"[Project Rule: {label}]\n{rule.content}")
            # Layer Codex instructions after other project rules so the
            # closest/most-specific Codex guidance wins.
            codex_rules = self.load_codex_rules(workspace_path, disabled_files=disabled_files)
            for rule in codex_rules:
                label = rule.source
                blocks.append(f"[Project Rule: {label}]\n{rule.content}")

        # Nothing to include: return an empty prompt.
        if not blocks:
            return ""

        # Separate rule blocks with a visual divider for the model.
        joined = "\n\n---\n\n".join(blocks)
        return joined

    def _read_file(self, path: str) -> str:
        """Read and strip a project rule file, returning "" on any I/O error.

        Project rules live inside user workspaces (``CLAUDE.md``, ``.cursorrules``,
        ``.encre/rules.md``, …) and are plain Markdown by contract — unlike the
        global rules under the data dir, which are encrypted (see
        :func:`read_global_rule_text`).
        """
        # Swallow common I/O errors so a missing/unreadable rule is simply empty.
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
            return ""
