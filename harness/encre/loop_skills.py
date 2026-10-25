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

import re
from collections.abc import Iterable
from typing import Any


class SkillManager:
    """Skill activation, collection, and rendering for EncreLoop.

    Manages the lifecycle of tool skills (auto-activated usage guidance),
    document skills (domain guidance scoped by file extension), and the
    dynamic skill catalogue presented to the model.  Composed into
    :class:`EncreLoop` via delegation.
    """

    _SKILL_PATTERN = re.compile(r"^/(\S+)(?:\s+(.*))?", re.DOTALL)

    #: Tool guides are indexed rather than inlined: one exists per tool and the
    #: bodies are long, so a compact index keeps the prompt cheap while still
    #: telling the model the guide exists *before* the first call.
    _TOOL_INDEX_CAP = 40
    _TOOL_INDEX_DESC_CHARS = 160
    #: Per-skill body cap for injected playbooks.  A few shipped guides run to
    #: 70k+ characters; injected whole they would consume the entire
    #: background budget and evict the catalogue (and the guidance itself
    #: would be head/tail-truncated anyway).
    _ACTIVE_SKILL_MAX_CHARS = 16_000

    def __init__(self, skill_registry: Any | None) -> None:
        self._skill_registry = skill_registry
        self._active_tool_skills: dict[str, str] = {}
        self._active_doc_skills: dict[str, str] = {}

    @property
    def active_tool_skills(self) -> dict[str, str]:
        return self._active_tool_skills

    @property
    def active_doc_skills(self) -> dict[str, str]:
        return self._active_doc_skills

    async def activate_skills(self, prompt: str) -> tuple[str, str]:
        if not self._skill_registry:
            return "", prompt
        parts: list[str] = []
        remaining = prompt
        while True:
            m = self._SKILL_PATTERN.match(remaining)
            if not m:
                break
            skill_name = m.group(1)
            args = (m.group(2) or "").strip() or None
            skill = self._skill_registry.lookup(skill_name)
            if skill is None:
                break
            skill_prompt = await self._skill_registry.activate(skill_name, args)
            if not skill_prompt.startswith("Error: "):
                parts.append(skill_prompt)
            end = m.end()
            remaining = remaining[end:].strip()
        if parts:
            return "\n\n".join(parts) + "\n\n---\n\n", remaining
        return "", prompt

    @staticmethod
    def tool_skill_name(tool_name: str) -> str:
        """Map a tool name to its guide name (``read_file`` -> ``tool-read-file``)."""
        return f"tool-{tool_name.replace('_', '-')}"

    async def collect_tool_skill(self, tool_name: str) -> None:
        if not self._skill_registry or not tool_name:
            return
        if tool_name in self._active_tool_skills:
            return
        skill_name = self.tool_skill_name(tool_name)
        skill = self._skill_registry.lookup(skill_name)
        if skill is None:
            return
        try:
            body = await self._skill_registry.activate(skill_name)
        except Exception:
            return
        if not body or body.startswith("Error: "):
            return
        self._active_tool_skills[tool_name] = body

    async def collect_doc_skills(self, args: dict) -> None:
        if not self._skill_registry or not args:
            return
        paths = [str(v) for v in args.values() if isinstance(v, str)]
        if not paths:
            return
        try:
            names = await self._skill_registry.activate_for_paths(paths)
        except Exception:
            return
        for skill_name in names:
            if skill_name in self._active_doc_skills:
                continue
            try:
                body = await self._skill_registry.activate(
                    skill_name, "(referenced this session)"
                )
            except Exception:
                continue
            if not body or body.startswith("Error: "):
                continue
            self._active_doc_skills[skill_name] = body

    def cache_activated_skill(self, skill_name: str, body: str) -> str:
        """Store a skill body activated through the ``skill`` tool.

        Tool guides are keyed by tool name (``tool-read-file`` ->
        ``read_file``) so the collectors treat them as already loaded and the
        index no longer advertises them; everything else is a domain/document
        skill.

        Returns:
            ``"tool"``, ``"doc"``, or ``""`` when nothing was cached.
        """
        name = (skill_name or "").strip().lower()
        if not name or not body:
            return ""
        if name.startswith("tool-"):
            self._active_tool_skills[name[len("tool-"):].replace("-", "_")] = body
            return "tool"
        self._active_doc_skills[name] = body
        return "doc"

    def render_tool_skill_index(self, tool_names: Iterable[str]) -> str:
        """Compact index of usage guides for the tools active this turn.

        Tool guides are excluded from the main skill catalogue (there is one
        per tool and their bodies are long), so without an index the model
        never learns they exist -- and guidance used to arrive only *after* a
        tool had already been used.  This lists every active tool that has a
        guide so the model can activate the exact one before its first call;
        guides that are already loaded drop out and render in full through
        :meth:`render_active_tool_skills`.
        """
        if not self._skill_registry:
            return ""
        entries: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw in tool_names:
            tool_name = str(raw or "").strip()
            if not tool_name or tool_name in seen or tool_name in self._active_tool_skills:
                continue
            seen.add(tool_name)
            skill_name = self.tool_skill_name(tool_name)
            skill = self._skill_registry.lookup(skill_name)
            if skill is None:
                continue
            desc = " ".join(str(getattr(skill, "description", "") or "").split())
            if len(desc) > self._TOOL_INDEX_DESC_CHARS:
                desc = desc[: self._TOOL_INDEX_DESC_CHARS - 3].rstrip() + "..."
            entries.append((skill_name, desc))
        if not entries:
            return ""
        hidden = max(0, len(entries) - self._TOOL_INDEX_CAP)
        parts = [
            "## Tool Usage Guides",
            "",
            "Each tool below ships a detailed guide (exact parameters, best "
            "practices, pitfalls). Before the first call to a tool whose "
            "arguments are not obvious from its schema, load its guide with "
            "the `skill` tool -- for example `skill(name=\"tool-glob\")`. The "
            "guide is injected into your next step; guides already loaded are "
            "expanded under \"Tool Skills (auto-activated)\".",
            "",
        ]
        for skill_name, desc in entries[: self._TOOL_INDEX_CAP]:
            parts.append(f"- `{skill_name}`: {desc}" if desc else f"- `{skill_name}`")
        if hidden:
            parts.extend(
                [
                    "",
                    f"*{hidden} more tool guides are available -- call the "
                    "`skill` tool with `name=\"list\"` to enumerate them.*",
                ]
            )
        return "\n".join(parts).rstrip()

    def render_active_tool_skills(self) -> str:
        if not self._active_tool_skills:
            return ""
        parts = [
            "## Tool Skills (auto-activated)",
            "",
            "Detailed usage guides for tools used this session (loaded "
            "automatically on first use, or on request through the `skill` "
            "tool):",
            "",
        ]
        for tool_name, body in self._active_tool_skills.items():
            parts.append(f"### tool-{tool_name.replace('_', '-')}")
            parts.append("")
            parts.append(body.strip())
            parts.append("")
        return "\n".join(parts).rstrip()

    def render_active_doc_skills(self) -> str:
        if not self._active_doc_skills:
            return ""
        parts = [
            "## Active Skills",
            "",
            "Playbooks activated this session (auto-selected by file type, or "
            "loaded on request through the `skill` tool):",
            "",
        ]
        for skill_name, body in self._active_doc_skills.items():
            text = body.strip()
            if len(text) > self._ACTIVE_SKILL_MAX_CHARS:
                where = ""
                if self._skill_registry is not None:
                    skill = self._skill_registry.lookup(skill_name)
                    path = str(getattr(skill, "file_path", "") or "")
                    if path:
                        where = f" Full text: {path}"
                text = (
                    text[: self._ACTIVE_SKILL_MAX_CHARS].rstrip()
                    + "\n\n...[skill truncated to keep context bounded; "
                    "read the rest from disk if it is needed."
                    + where
                    + "]"
                )
            parts.append(f"### {skill_name}")
            parts.append("")
            parts.append(text)
            parts.append("")
        return "\n".join(parts).rstrip()

    # Catalogue sizing.  The list is the model's only map of the skill library:
    # cutting it in alphabetical order silently makes whole families
    # unreachable (with 224 skills and a cap of 120 the tail -- travel, video,
    # wechat, xlsx, youtube ... -- never appeared at all).  Descriptions are
    # therefore shortened per entry instead, which keeps every skill visible
    # at roughly the same prompt cost as the old truncated list.
    _CATALOGUE_CAP = 260
    _CATALOGUE_DESC_CHARS = 104

    def render_skill_catalogue(self) -> str:
        if not self._skill_registry:
            return ""
        skills = [
            s for s in self._skill_registry.list_all()
            if s.user_invocable and not s.name.startswith("tool-")
        ]
        if not skills:
            return ""
        skills.sort(key=lambda s: s.name)
        shown = skills[: self._CATALOGUE_CAP]
        total = len(skills)
        parts = [
            "Skills are reusable playbooks for whole task types (decks, PDFs, "
            "Redis work, video editing, travel planning ...). Invoke one by "
            "typing `/skill-name <args>` (aliases also work), or call the "
            "`skill` tool with `name` (and optional `args`) when the request "
            "matches a skill's purpose. Descriptions are shortened here -- "
            "activate a skill to read the full playbook.",
            "",
        ]
        for s in shown:
            desc = " ".join(str(getattr(s, "description", "") or "").split())
            if len(desc) > self._CATALOGUE_DESC_CHARS:
                desc = desc[: self._CATALOGUE_DESC_CHARS - 3].rstrip(" ,.;:-") + "..."
            parts.append(f"- `/{s.name}`: {desc}" if desc else f"- `/{s.name}`")
        if total > len(shown):
            parts.append("")
            parts.append(
                f"*{total - len(shown)} more skills are available — call the "
                "`skill` tool with `list` to enumerate them.*"
            )
        return "\n".join(parts).rstrip()
