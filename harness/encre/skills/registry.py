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

"""Skill registry.

Loads, stores, and resolves :class:`BundledSkillDefinition` entries.  Skills
may be registered programmatically or discovered from ``SKILL.md`` files on
disk (with YAML/legacy frontmatter).  Name and alias lookups honour a
source-priority scheme so that higher-precedence definitions win on conflict.
"""

import os
import re
from typing import Any

import yaml

from encre.skills.types import (
    _PRIORITY_ORDER,
    BundledSkillDefinition,
    SkillContext,
    SkillSource,
)

_EXTENSIONS_HEADER_PATTERN = re.compile(
    r"\.\w+$",
    re.IGNORECASE,
)

_FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n",
    re.MULTILINE | re.DOTALL,
)

_ALIASES_SEPARATOR = re.compile(r",\s*")


class EncreSkillRegistry:
    """In-memory registry of skills keyed by name and alias."""

    def __init__(self) -> None:
        """Initialize empty name and alias maps."""
        self._skills: dict[str, BundledSkillDefinition] = {}
        self._aliases: dict[str, str] = {}

    def register(self, skill: BundledSkillDefinition) -> None:
        """Register a skill, resolving name/alias conflicts by source priority."""
        existing = self._skills.get(skill.name)
        if existing is not None:
            new_priority = _PRIORITY_ORDER.get(skill.source, 3)
            old_priority = _PRIORITY_ORDER.get(existing.source, 3)
            if new_priority >= old_priority:
                return
        self._skills[skill.name] = skill
        for alias in skill.aliases:
            existing_alias = self._aliases.get(alias)
            if existing_alias is not None:
                existing_skill = self._skills.get(existing_alias)
                if existing_skill is not None:
                    new_priority = _PRIORITY_ORDER.get(skill.source, 3)
                    old_priority = _PRIORITY_ORDER.get(existing_skill.source, 3)
                    if new_priority >= old_priority:
                        continue
            self._aliases[alias] = skill.name

    def unregister(self, name: str) -> BundledSkillDefinition | None:
        """Remove a skill by name, cleaning up its aliases.

        Used by the verification gate to roll back an auto-generated skill and
        by legacy migration to stop injecting unverified artifacts.

        Args:
            name: The exact skill name to remove.

        Returns:
            The removed definition, or ``None`` when no such skill exists.
        """
        skill = self._skills.pop(name, None)
        if skill is None:
            return None
        for alias in list(skill.aliases):
            if self._aliases.get(alias) == name:
                self._aliases.pop(alias, None)
        return skill

    def lookup(self, name: str) -> BundledSkillDefinition | None:
        """Resolve a skill by exact name or by alias."""
        skill = self._skills.get(name)
        if skill is not None:
            return skill
        resolved = self._aliases.get(name)
        if resolved is not None:
            return self._skills.get(resolved)
        return None

    async def activate(
        self,
        name: str,
        args: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Activate a skill by name and return its rendered prompt.

        Looks the skill up, calls its ``get_prompt_for_command`` coroutine with
        the provided args/context, and returns an error string on failure.
        """
        skill = self.lookup(name)
        if skill is None:
            return f"Error: skill '{name}' not found."
        ctx = context or {}
        try:
            prompt = await skill.get_prompt_for_command(args, ctx)
            return prompt
        except Exception as e:
            return f"Error activating skill '{name}': {e}"

    async def activate_for_paths(self, file_paths: list[str]) -> list[str]:
        """Return names of skills whose ``when_to_use`` matches given file extensions."""
        prompts: list[str] = []
        seen: set[str] = set()
        for file_path in file_paths:
            match = _EXTENSIONS_HEADER_PATTERN.search(file_path)
            if match is None:
                continue
            ext = match.group(0).lower()
            for skill in self._skills.values():
                if skill.auto_activate and skill.when_to_use and ext in skill.when_to_use.lower() and skill.name not in seen:
                    seen.add(skill.name)
                    prompts.append(skill.name)
        return prompts

    def load_from_dir(
        self,
        skills_dir: str,
        source: SkillSource = SkillSource.PROJECT,
    ) -> None:
        """Walk a directory and load every ``SKILL.md`` (or frontmatter) file.

        Hidden directories are skipped.  Each discovered file is parsed and
        registered with the supplied :class:`SkillSource`.
        """
        if not os.path.isdir(skills_dir):
            return
        for root, dirs, files in os.walk(skills_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for filename in files:
                if filename.upper() == "SKILL.MD":
                    filepath = os.path.join(root, filename)
                    self._load_skill_md(filepath, source)
            for filename in files:
                if not filename.endswith(".md") or filename.upper() == "SKILL.MD" or filename == "MEMORY.md":
                    continue
                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, encoding="utf-8") as f:
                        first_chunk = f.read(512)
                    if first_chunk.startswith("---") and "name: " in first_chunk.split("---")[1] if "---" in first_chunk else False:
                        self._load_skill_md(filepath, source)
                except (OSError, UnicodeDecodeError):
                    continue

    def _load_skill_md(self, filepath: str, source: SkillSource) -> None:
        """Parse a SKILL.md file's frontmatter and register it as a skill."""
        try:
            with open(filepath, encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            return

        # Parse YAML frontmatter -- supports both standards-compliant YAML
        # and our legacy key: value format (via YAML)
        metadata, body = _parse_yaml_frontmatter(content)
        name = metadata.get("name", "").strip()
        if not name:
            return

        # Lifecycle gate: auto-generated skills carry an explicit ``status``
        # and only ``active`` ones may be loaded.  Anything else (proposed,
        # rejected, disabled) is kept on disk for auditing but must never be
        # injected into the prompt.  Skills without a status are unchanged.
        status = str(metadata.get("status", "")).strip().lower()
        if status and status != "active":
            return

        # Validate name per spec: lowercase, hyphens, no underscores
        _name_validated = name.lower().replace("_", "-")

        description = str(metadata.get("description", "")).strip()
        license_val = str(metadata.get("license", "")).strip()
        compatibility = str(metadata.get("compatibility", "")).strip()
        when_to_use = str(metadata.get("when_to_use", "")).strip()
        argument_hint = str(metadata.get("argument_hint", "")).strip()
        model = metadata.get("model")
        disable_model = _parse_bool(str(metadata.get("disable_model_invocation", "false")))
        user_invocable = _parse_bool(str(metadata.get("user_invocable", "true")))
        hidden = _parse_bool(str(metadata.get("hidden", "false")))
        auto_activate = _parse_bool(str(metadata.get("auto_activate", "false")))
        context_raw = str(metadata.get("context", "inline")).strip().lower()
        context_enum = SkillContext(context_raw) if context_raw in ("inline", "fork") else SkillContext.INLINE

        # metadata map -- extract any unrecognized keys as extra metadata
        extra_meta: dict[str, str] = {}
        meta_val = metadata.get("metadata")
        if isinstance(meta_val, dict):
            extra_meta = {str(k): str(v) for k, v in meta_val.items()}
        # Also flatten top-level extra fields into metadata
        _known = {"name", "description", "license", "compatibility", "aliases",
                   "allowed-tools", "allowed_tools", "metadata", "model", "context",
                   "when_to_use", "argument_hint", "disable_model_invocation",
                   "user_invocable", "hidden", "auto_activate", "version"}
        for k, v in metadata.items():
            if k not in _known and v is not None:
                extra_meta[str(k)] = str(v)

        # allowed-tools -- support both standard (space-delimited) and legacy (comma)
        allowed_tools_raw = str(metadata.get("allowed-tools") or metadata.get("allowed_tools") or "")
        allowed_tools: list[str] | None = None
        if allowed_tools_raw.strip():
            # Split by spaces, handle entries like "Bash(git:*)" from spec
            tools_list: list[str] = []
            for part in allowed_tools_raw.split():
                part = part.strip()
                if part:
                    tools_list.append(part)
            # Also support comma format
            if len(tools_list) == 1 and "," in tools_list[0]:
                tools_list = [t.strip() for t in tools_list[0].split(",") if t.strip()]
            if tools_list:
                allowed_tools = tools_list

        # aliases -- support YAML list, comma string, or space-delimited
        aliases_raw = metadata.get("aliases")
        aliases: list[str] = []
        if isinstance(aliases_raw, list):
            aliases = [str(a).strip() for a in aliases_raw if a]
        elif isinstance(aliases_raw, str) and aliases_raw.strip():
            aliases = [a.strip() for a in _ALIASES_SEPARATOR.split(aliases_raw) if a.strip()]

        async def get_prompt(
            args: str | None = None,
            ctx: dict[str, Any] | None = None,
        ) -> str:
            resolved = body
            if args is not None:
                resolved = resolved.replace("{{args}}", args)
                resolved = resolved.replace("{{arguments}}", args)
                resolved = resolved.replace("{{user_input}}", args)
            if ctx is not None:
                for key, value in ctx.items():
                    resolved = resolved.replace(f"{{{{{key}}}}}", str(value))
            return resolved

        skill = BundledSkillDefinition(
            name=_name_validated,
            description=description,
            get_prompt_for_command=get_prompt,
            aliases=aliases,
            when_to_use=when_to_use,
            argument_hint=argument_hint,
            allowed_tools=allowed_tools,
            model=str(model) if model else None,
            disable_model_invocation=disable_model,
            user_invocable=user_invocable,
            hidden=hidden,
            auto_activate=auto_activate,
            context=context_enum,
            source=source,
            file_path=filepath,
            body=body,
            license=license_val,
            compatibility=compatibility,
            metadata=extra_meta,
        )
        self.register(skill)

    def list_all(self) -> list[BundledSkillDefinition]:
        """Return all registered skills."""
        return list(self._skills.values())


def _parse_yaml_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Split YAML/legacy frontmatter from a SKILL.md body.

    Returns a ``(metadata, body)`` tuple.  When no frontmatter is present the
    metadata is empty and the whole content is returned as the body.
    """
    match = _FRONTMATTER_PATTERN.match(content)
    if match is None:
        return {}, content
    frontmatter_text = match.group(1)
    body = content[match.end():]
    try:
        metadata = yaml.safe_load(frontmatter_text) or {}
    except Exception:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return metadata, body


# Public alias reused by sibling modules (e.g. ``encre.commands.registry``)
# so the same frontmatter contract is honoured across skills and commands.
parse_yaml_frontmatter = _parse_yaml_frontmatter


def _parse_bool(value: str) -> bool:
    """Parse a truthy string ("true"/"1"/"yes"/"on") into a boolean."""
    return value.strip().lower() in ("true", "1", "yes", "on")
