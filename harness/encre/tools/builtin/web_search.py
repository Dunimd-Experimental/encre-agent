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

from __future__ import annotations

from typing import Any

from encre.search.manager import EncreSearchManager
from encre.tools.base import build_tool
from encre.tools.tool_ui import ui_dict

_manager: EncreSearchManager | None = None


def _get_manager() -> EncreSearchManager:
    global _manager
    if _manager is None:
        _manager = EncreSearchManager()
    return _manager


async def _web_search_execute(**kwargs: Any) -> str:
    query = kwargs.get("query", "")
    if not query:
        return "Error: No search query provided."
    try:
        num = min(max(1, int(kwargs.get("num", 10))), 10)
    except (TypeError, ValueError):
        num = 5
    language = kwargs.get("language", "")
    categories = kwargs.get("categories", "general")

    manager = _get_manager()
    result = await manager.search(query, num=num, language=language,
                                   categories=categories, content=bool(kwargs.get("content", True)))

    error = result.get("_error", "")
    if error and not result.get("results"):
        return f"Error: {error}"

    results = result.get("results", [])
    suggestions = result.get("suggestions", [])
    if not results:
        return f"No results found. Did you mean: {' | '.join(suggestions)}?" if suggestions else "No results found."

    lines = []
    for i, r in enumerate(results[:num], 1):
        title = r.get("title", "").strip()
        url = r.get("url", "")
        content = r.get("content", "").strip()
        entry = f"{i}. [{title}]({url})"
        if content:
            entry += "\n   " + content.replace("\n", "\n   ")
        lines.append(entry)

    output = "\n\n".join(lines)
    if suggestions:
        output += f"\n\nSuggestions: {' | '.join(suggestions[:5])}"
    return output


web_search_tool = build_tool(
    name="web_search",
    description=(
        "Search the web for up-to-date information. Returns title, URL, and "
        "inline page content by default. Zero configuration needed. "
        "Use web_fetch to read a specific URL; use web_search when you need to discover information."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (required). Be specific."},
            "num": {"type": "integer", "description": "Max results (default 10, max 10)."},
            "language": {"type": "string", "description": "BCP-47 language code (e.g. zh-CN, en-US)."},
            "categories": {"type": "string", "description": "'general' (default) or 'news'."},
            "content": {"type": "boolean", "description": "Inline page content (default true)."},
        },
        "required": ["query"],
    },
    execute=_web_search_execute,
    ui=ui_dict(icon="globe", label="Web Search", body="web-results", keys=("query", "q")).as_dict(),
    intents=["general", "research"], category="web",
    triggers=["search web", "internet search", "google", "lookup"],
    semantic_type="network", cost_level="medium", retryability="auto",
    is_concurrency_safe=lambda _: True, is_readonly=True,
)



