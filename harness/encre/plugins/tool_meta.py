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

"""Authoritative tool UI metadata: built-in catalog + plugins, one list out.

The desktop renderer renders every tool call from this list only.  Nothing
about tool names lives in the frontend anymore; the renderer switches on the
``card`` and ``body`` fields and falls back to a neutral strip for anything
unknown.

Precedence, lowest to highest:

1. ``encre.tools.tool_ui.BUILTIN_TOOL_UI`` — the shipped catalog covering
   every tool the client knows about.
2. A tool instance's own ``ui=`` attribute — declared next to the
   implementation in each builtin module (``tools/builtin/*.py``), so the
   presentation travels with the code.
3. Plugin contributions (``get_tool_meta()`` / ``contributes.tools_meta``) —
   a plugin always wins over the catalog and over the builtin declaration.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from encre.logging_config import get_logger
from encre.plugins.guard import absorb
from encre.tools.tool_ui import all_ui_entries

logger = get_logger("encre.plugins.tool_meta")

# Fields a contribution may set.  Unknown keys are ignored rather than
# forwarded, so a typo cannot invent a renderer field.
_ALLOWED_KEYS = frozenset(
    {"icon", "label", "card", "body", "hidden", "prefix", "result", "keys", "terminal_labels", "hint",
     "extends", "hide", "props"}
)

# Strip-header actions a contribution may ask to hide.  Unknown actions are
# dropped so a typo cannot silently change a card it does not know about.
_HIDE_ACTIONS = frozenset({"stop"})

# JSON-safe scalar types allowed inside a ``props`` value.
_PROPS_SCALARS = (str, int, float, bool)

SOURCE_CATALOG = "builtin-catalog"
SOURCE_TOOL = "builtin-tool"

_builtin_tools_cache: list[Any] | None = None


def _builtin_tools() -> list[Any]:
    """Every registered builtin tool instance, cached per process.

    The server's call sites used to invoke ``collect_tool_meta(include_builtin
    =True)`` without passing ``tools``, so step 2 of the precedence never ran:
    each builtin's own ``ui=`` (icon, label, card, ``hidden``) was dropped and
    the renderer fell back to a neutral strip for any tool missing from the
    static catalog -- which is how a tool declaring ``hidden=True`` still got
    a row in the transcript.
    """
    global _builtin_tools_cache
    if _builtin_tools_cache is None:
        try:
            from encre.tools.builtin import register_builtin_tools
            from encre.tools.registry import ToolRegistry

            registry = ToolRegistry()
            register_builtin_tools(registry)
            _builtin_tools_cache = registry.all()
        except Exception:
            logger.warning("[tool_meta] builtin tool instances unavailable", exc_info=True)
            _builtin_tools_cache = []
    return _builtin_tools_cache


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge ``override`` onto ``base``, recursing one level for dicts."""
    out = dict(base)
    for key, value in override.items():
        if not isinstance(value, dict) or not isinstance(out.get(key), dict):
            out[key] = value
            continue
        merged = dict(out[key])
        merged.update(value)
        out[key] = merged
    return out


def _sanitize(meta: Any) -> dict[str, Any]:
    """Keep only known, JSON-serialisable keys."""
    if not isinstance(meta, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in meta.items():
        if key not in _ALLOWED_KEYS:
            continue
        if key == "keys":
            if isinstance(value, list | tuple):
                out[key] = [str(v) for v in value]
            continue
        if key == "terminal_labels":
            if isinstance(value, dict):
                out[key] = {str(k): str(v) for k, v in value.items()}
            continue
        if key == "extends":
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()
            continue
        if key == "hide":
            if isinstance(value, list | tuple):
                actions = [str(v) for v in value if str(v) in _HIDE_ACTIONS]
                if actions:
                    out[key] = actions
            continue
        if key == "props":
            if isinstance(value, dict):
                props = {
                    str(k): v for k, v in value.items()
                    if isinstance(v, _PROPS_SCALARS)
                }
                if props:
                    out[key] = props
            continue
        if value is None:
            continue
        out[key] = value
    return out


def collect_tool_meta(
    tools: Iterable[Any] = (),
    plugin_entries: Iterable[Any] = (),
    include_builtin: bool = True,
) -> list[dict[str, Any]]:
    """Merge every tool UI declaration into one ``[{name, meta, source}]`` list.

    Args:
        tools: Tool instances (``EncreTool``).  A ``ui`` dict attribute wins
            over the catalog entry for that name.
        plugin_entries: Plugin contributions, either ``{"name", "meta"}`` dicts
            or objects with a ``.name`` and ``.get_tool_meta()``.  Later
            entries win over earlier ones.
        include_builtin: Set False to emit only tool-instance and plugin UI.
    """
    merged: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}

    # No explicit instances means "the core tools as registered" -- otherwise
    # every caller that forgot ``tools=`` silently ships catalog-only data.
    tool_list = list(tools)
    if include_builtin and not tool_list:
        tool_list = _builtin_tools()

    if include_builtin:
        for entry in all_ui_entries():
            name = str(entry.get("name", ""))
            if not name:
                continue
            merged[name] = _sanitize(entry.get("meta"))
            sources[name] = SOURCE_CATALOG

    for tool in tool_list:
        name = getattr(tool, "name", "")
        meta = _sanitize(getattr(tool, "ui", None))
        # An instance that declares nothing must not overwrite the catalog
        # (whose prefix entries, e.g. ``memory_``, are what hide those tools);
        # shipping an empty meta would turn them back into visible strips.
        if not name or not meta:
            continue
        name = str(name)
        merged[name] = _deep_merge(merged.get(name, {}), meta)
        sources[name] = SOURCE_TOOL

    for entry in plugin_entries:
        plugin_name = ""
        if isinstance(entry, dict):
            plugin_name = str(entry.get("source", ""))
            meta_entries = [entry]
        else:
            plugin_name = ""
            manifest = getattr(entry, "manifest", None)
            if manifest is not None:
                plugin_name = str(getattr(manifest, "name", ""))
            try:
                meta_entries = absorb(
                    entry.get_tool_meta,
                    default=(),
                    context=f"plugin {plugin_name!r} get_tool_meta",
                )
                if not isinstance(meta_entries, list):
                    meta_entries = list(meta_entries)
            except Exception:
                meta_entries = []
        for item in meta_entries:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            meta = _sanitize(item.get("meta"))
            if not name or not meta:
                continue
            merged[name] = _deep_merge(merged.get(name, {}), meta)
            sources[name] = plugin_name or "plugin"

    return [
        {"name": name, "meta": merged[name], "source": sources.get(name, SOURCE_CATALOG)}
        for name in sorted(merged)
    ]


def tool_meta_by_name(
    tools: Iterable[Any] = (),
    plugin_entries: Iterable[Any] = (),
    include_builtin: bool = True,
) -> dict[str, dict[str, Any]]:
    """Same merge as :func:`collect_tool_meta`, keyed by tool name."""
    return {entry["name"]: entry["meta"] for entry in collect_tool_meta(tools, plugin_entries, include_builtin)}
