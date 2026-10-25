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

import contextlib
from collections.abc import Callable
from typing import Any

from encre.tools.base import EncreTool


def _tool_schema(tool: Any) -> dict[str, Any]:
    """Best-effort input schema extraction for third-party tool objects."""
    schema = getattr(tool, "input_schema", None) or getattr(tool, "parameters", None)
    return schema if isinstance(schema, dict) else {"type": "object", "properties": {}}


def tool_to_openai_format(tool: Any) -> dict[str, Any]:
    """OpenAI function format; duck-typed fallback for third-party tools.

    Plugin tools that predate the ``to_openai_format`` protocol (or simply
    declare ``name``/``description``/``input_schema``) must not crash the
    whole payload build -- fall back to a structural conversion instead.
    """
    fmt = getattr(tool, "to_openai_format", None)
    if callable(fmt):
        return fmt()
    return {
        "type": "function",
        "function": {
            "name": getattr(tool, "name", ""),
            "description": getattr(tool, "description", ""),
            "parameters": _tool_schema(tool),
        },
    }


def tool_to_anthropic_format(tool: Any) -> dict[str, Any]:
    """Anthropic tool format; duck-typed fallback for third-party tools."""
    fmt = getattr(tool, "to_anthropic_format", None)
    if callable(fmt):
        return fmt()
    return {
        "name": getattr(tool, "name", ""),
        "description": getattr(tool, "description", ""),
        "input_schema": _tool_schema(tool),
    }


class ToolRegistry:
    """Central registry for tool definitions.

    Manages tool lifecycle: registration, validation, lookup, and format
    conversion (OpenAI / Anthropic).  Supports listeners for dynamic
    tool-set changes and caches converted formats for performance.

    Registration IS activation: every registered tool is executable AND
    visible in the prompt-facing views.  There is no deferred zone --
    plugins activate at startup and their tools join the active set
    immediately.
    """

    def __init__(self) -> None:
        self._tools: dict[str, EncreTool] = {}
        self._listeners: list[Callable[[str, EncreTool], None]] = []
        self._format_cache: dict[str, list[dict[str, Any]]] = {}

    def _validate_tool(self, tool: EncreTool) -> None:
        """Validate that a tool has all required fields before registration."""
        if not tool.name:
            raise ValueError("Tool must have a non-empty 'name'")
        if not tool.description:
            raise ValueError(f"Tool '{tool.name}' must have a non-empty 'description'")
        if not tool.input_schema:
            raise ValueError(f"Tool '{tool.name}' must have an 'input_schema'")
        if not hasattr(tool, "execute") or not callable(tool.execute):
            raise ValueError(f"Tool '{tool.name}' must have a callable 'execute' method")

    def _notify_listeners(self, event: str, tool: EncreTool) -> None:
        """Notify all registered listeners of a tool registration event."""
        for cb in self._listeners:
            with contextlib.suppress(Exception):
                cb(event, tool)

    def _invalidate_cache(self) -> None:
        """Clear all cached format conversions so they are regenerated."""
        self._format_cache.clear()

    def register(self, tool: EncreTool) -> None:
        """Register a tool. Validates requirements and notifies listeners."""
        self._validate_tool(tool)
        self._tools[tool.name] = tool
        self._invalidate_cache()
        self._notify_listeners("register", tool)

    def register_many(self, tools: list[EncreTool]) -> None:
        """Register multiple tools in sequence."""
        for tool in tools:
            self.register(tool)

    def unregister(self, name: str) -> EncreTool | None:
        """Remove a tool by name. Returns the removed tool, or None."""
        tool = self._tools.pop(name, None)
        if tool is not None:
            self._invalidate_cache()
            self._notify_listeners("unregister", tool)
        return tool

    def get(self, name: str) -> EncreTool | None:
        """Look up a tool by name in the execution layer.

        Registration makes a tool callable, period.  Visibility and
        executability are the same thing: the prompt-facing views
        (``get_openai_tools`` etc.) expose exactly the registered set.
        """
        return self._tools.get(name)

    def list_tools(self) -> dict[str, EncreTool]:
        """Return a copy of all registered tools keyed by name."""
        return dict(self._tools)

    def all(self) -> list[EncreTool]:
        """Return a list of all registered tools."""
        return list(self._tools.values())

    @property
    def count(self) -> int:
        """Number of registered tools."""
        return len(self._tools)

    def get_by_category(self, category: str) -> list[EncreTool]:
        """Filter tools by their category field."""
        return [t for t in self._tools.values() if t.category == category]

    def get_by_intents(self, intents: list[str]) -> list[EncreTool]:
        """Filter tools that match at least one of the given intents."""
        return [t for t in self._tools.values() if any(i in getattr(t, "intents", ["general"]) for i in intents)]

    def register_listener(self, callback: Callable[[str, EncreTool], None]) -> None:
        """Register a callback invoked on tool register/unregister events."""
        self._listeners.append(callback)

    def get_openai_tools(self) -> list[dict[str, Any]]:
        """Return all tools in OpenAI function-calling format (cached)."""
        key = "openai"
        if key not in self._format_cache:
            self._format_cache[key] = [tool_to_openai_format(t) for t in self._tools.values()]
        return self._format_cache[key]

    def get_openai_tools_for_intents(self, intents: list[str]) -> list[dict[str, Any]]:
        """Return tools matching given intents in OpenAI format."""
        return [tool_to_openai_format(t) for t in self.get_by_intents(intents)]

    def get_anthropic_tools(self) -> list[dict[str, Any]]:
        """Return all tools in Anthropic tool format (cached)."""
        key = "anthropic"
        if key not in self._format_cache:
            self._format_cache[key] = [tool_to_anthropic_format(t) for t in self._tools.values()]
        return self._format_cache[key]
