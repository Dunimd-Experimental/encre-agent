#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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

"""EA grep Plugin (system-default), targeting the v0.5+ plugin system.

Declarative-first: the manifest carries every v0.5+ surface the host
mirrors at activation time (engine compatibility range, core activation
tier, lucide icon, engine-side danger levels), while ``get_tools`` /
``get_skills`` keep the code side of the dual track.
"""
from pathlib import Path

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource

_PKG_DIR = Path(__file__).resolve().parent
_SKILL_PATH = _PKG_DIR / "skills" / "tool-grep.md"


def _load_skill_md(path: Path) -> dict:
    """Parse a SKILL.md into {name, description, body, hidden, context}.

    Frontmatter is a light ``key: value`` block delimited by ``---`` fences;
    everything after the closing fence is the prompt body.
    """
    text = path.read_text(encoding="utf-8-sig")
    meta: dict = {"name": path.parent.name, "description": "", "hidden": False, "context": "inline"}
    body = text
    if text.lstrip().startswith("---"):
        lines = text.splitlines()
        try:
            end = lines.index("---", 1)
        except ValueError:
            end = -1
        if end > 0:
            for line in lines[1:end]:
                key, sep, value = line.partition(":")
                if not sep:
                    continue
                meta[key.strip()] = value.strip()
            body = "\n".join(lines[end + 1:]).lstrip("\n")
    meta["hidden"] = str(meta.get("hidden", "")).lower() in ("true", "1", "yes")
    meta["body"] = body
    return meta


class _GrepPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-tool-grep",
        display_name="Grep",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: ripgrep-powered content search.",
        icon="text-search",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        repository="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "search", "dev"],
        activation="core",
        activation_events=[],
        permissions=[],
        provides_tools=["grep"],
        provides_skills=["tool-grep"],
        # Read-only search tool: declared safe so the Rust permission engine
        # classifies it without hardcoding tool names on the host side.
        danger_levels={"grep": "safe"},
    )

    def get_tools(self) -> list:
        from ea_tool_grep.tool import grep_tool

        return [grep_tool]

    def get_skills(self) -> list:
        """Expose the bundled tool-grep usage skill (hidden, inline context)."""
        from encre.skills.types import BundledSkillDefinition, SkillContext, SkillSource

        if not _SKILL_PATH.is_file():
            return []
        meta = _load_skill_md(_SKILL_PATH)

        async def _render_prompt(
            _args=None,
            _ctx=None,
            _body: str = meta.get("body", ""),
        ) -> str:
            return _body

        try:
            context = SkillContext(str(meta.get("context", "inline")))
        except ValueError:
            context = SkillContext.INLINE
        skill = BundledSkillDefinition(
            name=str(meta.get("name", "tool-grep")),
            description=str(meta.get("description", "")),
            get_prompt_for_command=_render_prompt,
            context=context,
            source=SkillSource.BUNDLED,
            file_path=str(_SKILL_PATH),
            body=meta.get("body", ""),
            hidden=bool(meta.get("hidden")),
        )
        return [skill]


def create_plugin() -> EncrePlugin:
    return _GrepPlugin()
