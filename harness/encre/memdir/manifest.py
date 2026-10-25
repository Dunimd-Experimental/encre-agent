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

"""Render the Markdown manifest that summarises all memory files.

The manifest is a Markdown table shown to the model so it can discover
which memories exist, their age, description, type, and tags before
deciding to read a specific ``.md`` file.
"""


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from encre.memdir.system import MemoryHeader


def format_memory_manifest(memories: list[MemoryHeader]) -> str:
    """Build a Markdown table describing each memory header.

    Renders a pipe-delimited table with index, filename, age, description,
    type, and tags. Long descriptions and tag lists are truncated with an
    ellipsis so the manifest stays compact inside the model prompt.

    Args:
        memories: Sequence of :class:`MemoryHeader` objects to render.

    Returns:
        A Markdown table string, or ``""`` when no memories are provided.
    """
    if not memories:
        return ""

    lines: list[str] = []
    lines.append("# Memory Manifest")
    lines.append("")
    lines.append("| # | File | Age | Description | Type | Tags |")
    lines.append("|---|---|---|---|---|---|")

    for i, m in enumerate(memories, 1):
        desc = m.description or "-"
        # Keep descriptions compact so the table stays readable in prompts
        if len(desc) > 60:
            desc = desc[:57] + "..."
        mtype = m.memory_type or "-"
        tags = ", ".join(m.tags) if m.tags else "-"
        # Likewise cap the rendered tag list
        if len(tags) > 40:
            tags = tags[:37] + "..."

        lines.append(
            f"| {i} | {m.filename} | {m.age_text} | {desc} | {mtype} | {tags} |"
        )

    lines.append("")
    return "\n".join(lines)
