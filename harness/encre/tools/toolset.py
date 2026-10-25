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

"""Declarative ToolSet system — group tools into named scenario sets.

This module defines **only** tool groupings.  The actual tool selection
logic lives in ``encre.tools.discovery.ToolDiscovery`` — this module
just provides the declaration format and resolver that discovery uses.

Usage
-----

    from encre.tools.toolset import TOOLSETS, resolve_toolset

    # Resolve "coding" → {"file_read", "file_write", "bash", ...}
    tools = resolve_toolset("coding")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ------------------------------- ToolSet definition -------------------------------


@dataclass(frozen=True)
class ToolSet:
    """A named group of tools that should be available together.

    Attributes
    ----------
    name
        Canonical name (e.g. ``"coding"``, ``"research"``).
    description
        Human-readable description of this set.
    tools
        Concrete tool names included directly.
    includes
        Names of other tool sets to inherit.  Resolved recursively.
    """

    name: str
    description: str = ""
    tools: frozenset[str] = field(default_factory=frozenset)
    includes: frozenset[str] = field(default_factory=frozenset)

    def resolve(self, known: dict[str, ToolSet]) -> frozenset[str]:
        """Resolve this set and all transitively included sets into a flat set
        of tool names.  Circular references are detected and broken."""
        seen: set[str] = set()
        result: set[str] = set()

        def _walk(name: str) -> None:
            if name in seen:
                return
            seen.add(name)
            s = known.get(name)
            if s is None:
                return
            result.update(s.tools)
            for inc in s.includes:
                _walk(inc)

        _walk(self.name)
        return frozenset(result)


# ------------------------------- Default tool sets ------------------------------
#
# Convention:
#   - "default":   always-available primitives (the new BASE_TOOLS)
#   - "file":      file I/O + search (shared by almost every scenario)
#   - "coding":    full development tool set (build, test, debug)
#   - "research":  web research & data extraction
#   - "devops":    infra & deployment tools
#   - "data":      data analysis & visualization
#   - "agent":     sub-agent & delegation tools
#   - "all":       everything (rarely used)

_TOOLSETS_DEF: dict[str, dict[str, Any]] = {
    "default": {
        "description": "Always-available primitives — file, shell, search, manage, memory",
        # Synced with discovery.BASE_TOOLS (the harness hardcoded set).
        # With register-as-activate this list is informational: the
        # model-facing payload always contains every registered tool.
        "tools": [
            "file_read", "file_write", "file_edit",
            "bash",
            "web_search",
            "manage",
            "memory_create", "memory_read", "memory_update",
            "memory_search", "memory_link", "memory_status",
            "memory_profile",
        ],
        "includes": [],
    },
    "file": {
        "description": "File I/O and search tools",
        "tools": [
            "file_read", "file_write", "file_edit", "apply_patch",
            "grep", "glob",
            "archive", "diff",
        ],
        "includes": [],
    },
    "coding": {
        "description": "Full development tool set — build, test, debug, LSP, git",
        "tools": [
            "bash",
            "bash_output", "bash_kill", "bash_list",
            "process_list", "process_kill",
            "lsp", "git",
            "lint_format", "test_run",
            "task", "cron",
        ],
        "includes": ["file", "agent"],
    },
    "research": {
        "description": "Web research, data extraction and synthesis",
        "tools": [
            "web_search", "web_fetch",
            "spreadsheet", "pdf", "chart", "diagram",
            "notebook",
        ],
        "includes": ["file", "data"],
    },
    "devops": {
        "description": "Infrastructure, deployment and operations",
        "tools": [
            "bash",
            "bash_output", "bash_kill", "bash_list",
            "deploy", "docker", "database", "ssh",
            "cloud_storage", "env_manager",
        ],
        "includes": ["file"],
    },
    "data": {
        "description": "Data analysis, transformation and visualization",
        "tools": [
            "chart", "diagram", "json_tool", "hash_crypto",
            "translation", "qr_code",
        ],
        "includes": ["file"],
    },
    "agent": {
        "description": "Sub-agent, delegation and orchestration tools",
        "tools": [
            "agent", "workflow",
        ],
        "includes": [],
    },
    "media": {
        "description": "Image, audio and video generation / editing",
        "tools": [
            "image", "generate_image", "edit_image", "image_variation",
            "transcribe_audio", "translate_audio",
            "create_embeddings", "create_moderation",
        ],
        "includes": ["file"],
    },
    "communication": {
        "description": "Email, notification and communication tools",
        "tools": [
            "email", "notify",
        ],
        "includes": [],
    },
    "all": {
        "description": "All available tools (use sparingly — large context cost)",
        "tools": [],
        "includes": [
            "default", "file", "coding", "research", "devops",
            "data", "agent", "media", "communication",
        ],
    },
}


# --------------------------- Build the ToolSet registry -------------------------

def _build_toolsets() -> dict[str, ToolSet]:
    registry: dict[str, ToolSet] = {}
    for name, raw in _TOOLSETS_DEF.items():
        registry[name] = ToolSet(
            name=name,
            description=raw.get("description", ""),
            tools=frozenset(raw.get("tools", [])),
            includes=frozenset(raw.get("includes", [])),
        )
    return registry


TOOLSETS: dict[str, ToolSet] = _build_toolsets()


# ------------------------------ Resolution helpers ------------------------------


def resolve_toolset(name: str) -> frozenset[str]:
    """Resolve a tool set by name into a flat set of tool names.

    Includes all transitively included sets.  Falls back to ``default`` if
    *name* is not found.  A ``+``-joined name (e.g. ``"default+coding"``)
    is resolved as the union of each component, letting callers compose
    base + intent-matched sets without inventing new registry entries.

    Examples
    --------
    >>> resolve_toolset("coding")
    frozenset({"file_read", "file_write", "bash", "grep", "agent", ...})
    >>> resolve_toolset("default+coding")
    frozenset({"file_read", "file_write", "bash", ..., "lsp", "git", ...})
    """
    if "+" in name:
        result: set[str] = set()
        for part in name.split("+"):
            part = part.strip()
            if not part:
                continue
            ts = TOOLSETS.get(part)
            if ts is not None:
                result.update(ts.resolve(TOOLSETS))
            elif part == "default":
                result.update(TOOLSETS["default"].resolve(TOOLSETS))
        if result:
            return frozenset(result)
    ts = TOOLSETS.get(name)
    if ts is None:
        ts = TOOLSETS.get("default")
        if ts is None:
            return frozenset()
    return ts.resolve(TOOLSETS)


def list_available_sets() -> list[str]:
    """Return the names of all registered tool sets, sorted."""
    return sorted(TOOLSETS.keys())


__all__ = [
    "TOOLSETS",
    "ToolSet",
    "list_available_sets",
    "resolve_toolset",
]
