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

"""Tool discovery -- a pure information index over the active tool set.

Architecture
============

Registration IS activation: the model-facing tools array contains EVERY
registered tool (harness builtins + all activated plugins + connected MCP
servers) on every turn.  There is no deferred zone and no unlock flow.

The discovery index exists only to *inform*: the ``manage`` tool's discover
action searches it so the model can find the right tool name and parameter
card among hundreds of schemas without reading them all.  The index is
built lazily from the global ToolRegistry on first search.  It refreshes
when the registry changes (e.g. MCP servers connect).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from encre.tools.registry import tool_to_anthropic_format, tool_to_openai_format

if TYPE_CHECKING:
    from encre.tools.registry import ToolRegistry


# ------------------------------------------------------------------------------
# Keyed by tool name. Each entry contributes searchable text and a coarse
# category to help the model judge relevance.

_TOOL_HINTS: dict[str, dict[str, Any]] = {
    # File I/O -- base (also indexed for queries that mention them by name)
    "file_read": {
        "category": "filesystem",
        "triggers": ["read", "open file", "cat", "view", "load file", "look at", "inspect"],
    },
    "file_write": {
        "category": "filesystem",
        "triggers": ["write", "create file", "save", "new file", "overwrite"],
    },
    "file_edit": {
        "category": "filesystem",
        "triggers": ["edit", "modify", "change line", "patch", "replace text", "rewrite"],
    },
    "apply_patch": {
        "category": "filesystem",
        "triggers": ["apply patch", "diff", "unified diff", "multi-file edit", "git patch"],
    },
    "grep": {
        "category": "search",
        "triggers": ["search", "find text", "regex", "ripgrep", "rg", "match pattern"],
    },
    "glob": {
        "category": "search",
        "triggers": ["find files", "glob", "by pattern", "list files", "*.py", "**/*.ts"],
    },
    # Shell
    "bash": {
        "category": "shell",
        "triggers": ["shell", "command", "execute", "run script", "terminal"],
    },
    "bash_output": {
        "category": "shell",
        "triggers": ["background", "background shell output", "poll process", "shell tail"],
    },
    "bash_kill": {
        "category": "shell",
        "triggers": ["kill process", "terminate shell", "stop bash"],
    },
    "bash_list": {
        "category": "shell",
        "triggers": ["list shells", "background processes", "running shells"],
    },
    "process_list": {
        "category": "shell",
        "triggers": ["system processes", "running apps", "tasklist", "ps"],
    },
    "process_kill": {
        "category": "shell",
        "triggers": ["kill app", "terminate process", "force quit", "end task"],
    },
    # Web / network
    "web_fetch": {
        "category": "web",
        "triggers": ["fetch url", "download", "http get", "scrape page", "read web page"],
    },
    "web_search": {
        "category": "web",
        "triggers": ["search web", "google", "duckduckgo", "internet search", "lookup online"],
    },
    "rest_client": {
        "category": "web",
        "triggers": ["rest api", "http request", "api call", "curl", "post json"],
    },
    # Containers / deploy / data infra
    "docker": {
        "category": "infra",
        "triggers": ["docker", "container", "image", "compose"],
    },
    "deploy": {
        "category": "infra",
        "triggers": ["deploy", "release", "publish", "ship"],
    },
    "database": {
        "category": "data",
        "triggers": ["database", "sql", "query", "postgres", "mysql", "sqlite"],
    },
    # Code intelligence
    "lsp": {
        "category": "code_intel",
        "triggers": ["lsp", "language server", "hover", "go to definition", "diagnostics", "rename symbol"],
    },
    "git": {
        "category": "code_intel",
        "triggers": ["git", "commit", "diff", "branch", "log", "blame", "status"],
    },
    # Browser / GUI
    "browser": {
        "category": "gui",
        "triggers": ["browser", "chromium", "click button", "navigate url", "screenshot page", "cdp"],
    },
    "computer": {
        "category": "gui",
        "triggers": ["desktop", "windows", "ui automation", "click ui", "type", "screenshot", "screen", "mouse", "keyboard", "observe"],
    },
    # Notebook / docs
    "notebook": {
        "category": "data",
        "triggers": ["jupyter", "ipynb", "cells", "kernel", "execute notebook"],
    },
    "pdf": {
        "category": "docs",
        "triggers": ["pdf", "extract text", "pdf pages"],
    },
    "spreadsheet": {
        "category": "data",
        "triggers": ["excel", "xlsx", "csv", "sheet", "rows", "columns"],
    },
    "image": {
        "category": "media",
        "triggers": ["image", "resize", "crop", "ocr", "convert image"],
    },
    # Memory (shared, both modes)
    "memory_create": {
        "category": "memory",
        "triggers": ["remember", "save note", "memory create"],
    },
    "memory_read": {
        "category": "memory",
        "triggers": ["recall", "load memory", "memory read"],
    },
    "memory_update": {
        "category": "memory",
        "triggers": ["update memory", "edit note"],
    },
    "memory_delete": {
        "category": "memory",
        "triggers": ["forget", "delete memory"],
    },
    "memory_search": {
        "category": "memory",
        "triggers": ["search memory", "find note", "memory semantic"],
    },
    "memory_profile": {
        "category": "memory",
        "triggers": [
            "user profile",
            "user preferences",
            "about the user",
            "expertise",
            "communication style",
            "persona",
            "profile query",
            "profile update",
        ],
    },
    # Task / scheduling
    "task": {"category": "task", "triggers": ["task", "schedule task", "background task", "list tasks", "task details", "stop task", "task output", "task result", "update task"]},
    "cron": {"category": "task", "triggers": ["cron", "schedule", "recurring", "every minute", "every hour", "list cron", "delete cron", "remove schedule"]},
    # Sub-agent
    "agent": {
        "category": "delegation",
        "triggers": ["sub agent", "delegate", "spawn agent", "team", "parallel work"],
    },
    # Self
    "manage": {
        "category": "meta",
        "triggers": [
            "find tool",
            "search tools",
            "discover tool",
            "install tool",
            "register tool",
            "create tool",
            "new tool",
            "god tool",
            "manage",
            "install agent",
            "register agent",
            "install skill",
            "register skill",
            "install mcp",
            "connect mcp",
            "mcp server",
        ],
    },
    "question": {
        "category": "communication",
        "triggers": ["ask user", "question", "clarify", "confirm intent", "gather context"],
    },
    "todo": {
        "category": "task",
        "triggers": ["todo", "checklist", "track progress"],
    },
    "info": {
        "category": "communication",
        "triggers": [
            "card",
            "info card",
            "widget",
            "flight",
            "airplane",
            "plane",
            "boarding pass",
            "flight status",
            "train",
            "railway",
            "high speed rail",
            "bullet train",
            "CRH",
            "ship",
            "cruise",
            "ferry",
            "boat",
            "vessel",
            "sailing",
            "itinerary",
            "travel card",
            "trip card",
        ],
    },
    # Lint / format
    "lint_format": {
        "category": "code_intel",
        "triggers": [
            "lint",
            "format",
            "linter",
            "linting",
            "code format",
            "ruff",
            "pylint",
            "black",
            "isort",
            "format code",
            "fix lint",
            "autoformat",
            "check style",
        ],
    },
    # Test runner
    "test_run": {
        "category": "code_intel",
        "triggers": [
            "test",
            "pytest",
            "vitest",
            "jest",
            "cargo test",
            "run test",
            "test suite",
            "unit test",
            "coverage",
        ],
    },
    # Archive / compression
    "archive": {
        "category": "filesystem",
        "triggers": ["archive", "compress", "zip", "tar", "extract archive", "unzip"],
    },
    # Chart / visualization
    "chart": {
        "category": "data",
        "triggers": ["chart", "graph", "plot", "visualization", "bar chart", "pie chart", "line chart"],
    },
    # Diagram / flowchart
    "diagram": {
        "category": "data",
        "triggers": ["diagram", "flowchart", "sequence diagram", "uml", "mermaid"],
    },
    # Diff / compare
    "diff": {
        "category": "code_intel",
        "triggers": ["diff", "compare files", "file comparison", "side by side"],
    },
    # Email
    "email": {
        "category": "communication",
        "triggers": ["email", "send mail", "outlook", "gmail", "compose email"],
    },
    # Notifications
    "notify": {
        "category": "communication",
        "triggers": ["notification", "notify", "alert", "push", "desktop notification"],
    },
    # Environment variables
    "env_manager": {
        "category": "infra",
        "triggers": ["environment variable", "env", ".env", "set env", "get env"],
    },
    # Cloud storage
    "cloud_storage": {
        "category": "infra",
        "triggers": ["cloud storage", "s3", "oss", "upload file", "download file", "object storage"],
    },
    # GitHub
    "github": {
        "category": "code_intel",
        "triggers": ["github", "pr", "pull request", "issue", "repository", "repo", "github api"],
    },
    # SSH
    "ssh": {
        "category": "infra",
        "triggers": ["ssh", "remote", "server", "connect ssh", "remote shell"],
    },
    # Image generation / editing
    "generate_image": {
        "category": "media",
        "triggers": ["generate image", "create image", "draw", "dalle", "image generation", "text to image"],
    },
    "edit_image": {
        "category": "media",
        "triggers": ["edit image", "modify image", "inpaint", "image edit"],
    },
    "image_variation": {
        "category": "media",
        "triggers": ["image variation", "similar image", "variant"],
    },
    # Audio / media API
    "transcribe_audio": {
        "category": "media",
        "triggers": ["transcribe", "speech to text", "whisper", "audio transcription"],
    },
    "translate_audio": {
        "category": "media",
        "triggers": ["translate audio", "audio translation", "speech translate"],
    },
    "create_embeddings": {
        "category": "data",
        "triggers": ["embedding", "vectorize", "text embedding", "semantic search setup"],
    },
    "create_moderation": {
        "category": "data",
        "triggers": ["moderate", "content moderation", "toxicity", "safety check"],
    },
    # Platform API
    "file_api": {
        "category": "meta",
        "triggers": ["platform file", "file api", "upload file api", "platform storage"],
    },
    "batch_api": {
        "category": "meta",
        "triggers": ["batch api", "batch processing", "bulk api"],
    },
    "fine_tuning_api": {
        "category": "meta",
        "triggers": ["fine tune", "fine tuning", "model training", "custom model"],
    },
    # Utility tools
    "hash_crypto": {
        "category": "data",
        "triggers": ["hash", "md5", "sha", "encrypt", "decrypt", "crypto"],
    },
    "json_tool": {
        "category": "data",
        "triggers": ["json", "json query", "json path", "json schema", "transform json"],
    },
    "qr_code": {
        "category": "media",
        "triggers": ["qr code", "qrcode", "scan qr", "generate qr"],
    },
    "translation": {
        "category": "communication",
        "triggers": ["translate", "translation", "language", "i18n", "localization"],
    },
    # Workflow / multi-step orchestration
    "workflow": {
        "category": "delegation",
        "triggers": [
            "workflow",
            "multi step",
            "dag",
            "orchestration",
            "sub agent",
            "parallel task",
            "complex goal",
        ],
    },
}


# -- Hardcoded core tool set ---------------------------------------------------
# The harness-builtin tools (file/shell/web primitives + manage + memory).
# Informational only -- everything in the registry is active anyway; this
# constant names the subset that ships with the harness itself.

BASE_TOOLS: frozenset[str] = frozenset(
    {
        "file_read",
        "file_write",
        "file_edit",
        "bash",
        "web_search",
        "manage",
        "memory_create",
        "memory_read",
        "memory_update",
        "memory_search",
        "memory_link",
        "memory_status",
        "memory_profile",
    }
)


# -- Search index --------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _tokenize(text: str) -> list[str]:
    """Split text into lowercase alphanumeric tokens for BM25 indexing."""
    return [t.lower() for t in _TOKEN_RE.findall(text)]


@dataclass
class _IndexEntry:
    """Internal index entry for one tool in the BM25 search index."""

    name: str
    category: str
    description: str
    triggers: list[str]
    tokens: Counter[str]
    length: int


class ToolDiscovery:
    """Pure information index over the active tool set.

    Registration IS activation: every registered tool is already active and
    visible to the model on every turn.  A single ToolDiscovery instance is
    shared across sessions (the index is pure data derived from the global
    ToolRegistry); it exists only so the ``manage`` tool's discover action
    can *inform* the model WHAT exists (name, category, parameter card) --
    it never gates execution or visibility.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        """Initialize the discovery index backed by a ToolRegistry.

        Args:
            registry: The global tool registry to index.
        """
        self.registry = registry
        self._entries: list[_IndexEntry] = []
        self._avgdl: float = 1.0
        self._df: Counter[str] = Counter()
        self._signature: int = -1  # changes when registry changes; triggers rebuild
        self._payload_cache: dict[tuple[int, str, str], list[dict[str, Any]]] = {}

    # -- Index lifecycle -------------------------------------------------------

    def _registry_signature(self) -> int:
        """Compute a hash of the current registry contents for cache invalidation."""
        tools = self.registry.list_tools()
        return hash(tuple(sorted(tools.keys())))

    def _ensure_built(self) -> None:
        """Rebuild the search index if the registry has changed."""
        sig = self._registry_signature()
        if sig == self._signature and self._entries:
            return
        self._build()
        self._signature = sig
        self._payload_cache.clear()

    def _build(self) -> None:
        """Build the BM25 index from all tools in the registry."""
        entries: list[_IndexEntry] = []
        df: Counter[str] = Counter()
        for name, tool in self.registry.list_tools().items():
            hint = _TOOL_HINTS.get(name, {})
            tool_category = getattr(tool, "category", "general") or "general"
            # Centralised hints take precedence over the class default so we don't
            # have to annotate every tool file; an explicit non-default class value
            # still wins.
            category = hint.get("category") if tool_category == "general" else tool_category
            if not category:
                category = "general"
            triggers = list(getattr(tool, "triggers", []) or []) + list(hint.get("triggers", []))
            desc = (getattr(tool, "description", "") or "").strip()
            corpus = " ".join([name, name.replace("_", " "), category, desc, " ".join(triggers)])
            toks = _tokenize(corpus)
            tok_counter = Counter(toks)
            entries.append(
                _IndexEntry(
                    name=name,
                    category=category,
                    description=desc,
                    triggers=triggers,
                    tokens=tok_counter,
                    length=max(len(toks), 1),
                )
            )
            for term in tok_counter:
                df[term] += 1
        self._entries = entries
        self._df = df
        if entries:
            self._avgdl = sum(e.length for e in entries) / len(entries)

    # -- BM25 scoring ----------------------------------------------------------

    def _score(self, query_tokens: list[str], entry: _IndexEntry, k1: float = 1.5, b: float = 0.75) -> float:
        """Compute BM25 relevance score for an entry against query tokens.

        Args:
            query_tokens: Tokenized query terms.
            entry: The index entry to score.
            k1: BM25 k1 parameter (term saturation).
            b: BM25 b parameter (length normalization).

        Returns:
            A float relevance score (higher = more relevant).
        """
        if not query_tokens or not self._entries:
            return 0.0
        n = len(self._entries)
        score = 0.0
        for term in query_tokens:
            tf = entry.tokens.get(term, 0)
            if tf == 0:
                continue
            df_t = self._df.get(term, 0)
            idf = math.log(1.0 + (n - df_t + 0.5) / (df_t + 0.5))
            norm = (1.0 - b) + b * (entry.length / max(self._avgdl, 1.0))
            score += idf * ((tf * (k1 + 1.0)) / (tf + k1 * norm))
        return score

    # -- Public search API -----------------------------------------------------

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        category: str | None = None,
        exclude: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search indexed tools by BM25 relevance to a natural-language query.

        Args:
            query: Natural-language description of the desired capability.
            top_k: Maximum results to return (default 5).
            category: Optional category filter.
            exclude: Optional set of tool names to exclude.

        Returns:
            A list of discovery cards, each with name, category, description,
            parameters, and score.
        """
        self._ensure_built()
        qtokens = _tokenize(query)
        if not qtokens:
            return []
        exclude = set(exclude or set())
        # No automatic exclusions: every registered tool is active, so any
        # match (builtin, plugin, or MCP) is directly callable by the model.
        scored: list[tuple[float, _IndexEntry]] = []
        for entry in self._entries:
            if entry.name in exclude:
                continue
            if category and entry.category != category:
                continue
            s = self._score(qtokens, entry)
            if s > 0:
                scored.append((s, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[dict[str, Any]] = []
        for score, entry in scored[:top_k]:
            tool = self.registry.get(entry.name)
            if tool is None:
                continue
            card = {
                "name": entry.name,
                "category": entry.category,
                "description": entry.description,
                "parameters": getattr(tool, "input_schema", {}),
                "score": round(score, 3),
            }
            results.append(card)
        return results

    def list_by_category(self) -> dict[str, list[str]]:
        """List all indexed tools grouped by category."""
        self._ensure_built()
        out: dict[str, list[str]] = {}
        for entry in self._entries:
            out.setdefault(entry.category, []).append(entry.name)
        for cat in out:
            out[cat].sort()
        return out

    def get_active_tool_names(self, session_id: str) -> list[str]:
        """Tool names the model should see this turn.

        Registration IS activation: the payload contains every registered
        tool -- harness builtins, all activated plugins, and connected MCP
        servers.  There is no per-session unlock state.

        Args:
            session_id: Kept for call-site compatibility; unused.
        """
        del session_id  # no per-session state: the active set is global
        self._ensure_built()
        return sorted(self.registry.list_tools().keys())

    def get_active_tools_payload(
        self,
        session_id: str,
        fmt: str = "openai",
        profile: Any = None,
    ) -> list[dict[str, Any]]:
        """Materialize the active tool list as backend-format dicts.

        ``profile`` (a resolved :class:`~encre.backends.model_profiles.ModelProfile`)
        optionally restricts which tools are *visible* to the model -- weak
        models see a small core surface instead of the whole registry.
        Visibility only: filtered-out tools remain registered and executable.
        ``None`` keeps the historical full payload.
        """
        self._ensure_built()
        profile_key = profile.cache_key if profile is not None else None
        cache_key = (self._signature, session_id, fmt, profile_key)
        cached = self._payload_cache.get(cache_key)
        if cached is not None:
            return cached

        names = self.get_active_tool_names(session_id)
        if profile is not None:
            names = profile.filter_tool_names(names)
        out: list[dict[str, Any]] = []
        for name in names:
            tool = self.registry.get(name)
            if tool is None:
                continue
            if fmt == "anthropic":
                out.append(tool_to_anthropic_format(tool))
            else:
                out.append(tool_to_openai_format(tool))
        self._payload_cache[cache_key] = out
        return out


__all__ = ["BASE_TOOLS", "ToolDiscovery"]
