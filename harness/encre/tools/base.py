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

"""
Tool definitions for encre.

All tools are created via the ``build_tool()`` factory, which produces an
``EncreTool`` subclass instance using ``type()``.  The factory handles
schema minification, concurrency safety predicates, and format conversion
(OpenAI / Anthropic) automatically.

Usage::

    EncreGlobTool = build_tool(
        name="glob",
        description="List files matching a glob pattern.",
        input_schema={...},
        execute=_glob_execute,
        intents=["general", "coding"],
        is_concurrency_safe=lambda _: True,
    )
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar

# --- Defaults for optional fields --------------------------------------------

_TOOL_DEFAULTS = {
    "intents": ["general"],
    "category": "general",
    "triggers": [],
    "always_available": False,
    "max_result_size_chars": 100_000,
    "semantic_type": "general",
    "cost_level": "medium",
    "retryability": "auto",
    "safe_fallback": "",
    "source": "user",
}


# --- EncreTool (legacy base -- NOT abstract) ----------------------------------

class EncreTool(ABC):
    """Base class for tool definitions.

    Legacy API kept for backward compatibility.  New tools should use the
    ``build_tool()`` factory instead.

    Subclasses override ``name``, ``description``, ``input_schema``, and
    ``execute()``.  All other fields have sensible defaults.
    """

    name: str = ""
    description: str = ""
    input_schema: ClassVar[dict[str, Any]] = {}
    intents: ClassVar[list[str]] = ["general"]
    category: str = "general"
    triggers: ClassVar[list[str]] = []
    always_available: bool = False
    max_result_size_chars: int = 100_000
    semantic_type: str = "general"
    cost_level: str = "medium"
    retryability: str = "auto"
    safe_fallback: str = ""
    source: str = "user"
    ui: dict[str, Any] | None = None

    _minified_schema: ClassVar[dict[str, Any]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.input_schema:
            cls._minified_schema = _minify_schema(cls.input_schema)

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        ...

    def is_concurrency_safe(self, _input_data: dict[str, Any]) -> bool:
        """Whether this tool may run in parallel with other concurrency-safe tools.

        Args:
            _input_data: The tool call arguments dict.

        Returns:
            True if the tool can execute concurrently, False if it requires
            exclusive sequential execution.
        """
        return False

    def is_readonly(self, _input_data: dict[str, Any]) -> bool:
        """Whether this tool performs only read operations.

        Args:
            _input_data: The tool call arguments dict.

        Returns:
            True if the tool is read-only, False if it may write.
        """
        return False

    def is_destructive(self, _input_data: dict[str, Any]) -> bool:
        """Whether this tool performs destructive operations.

        Args:
            _input_data: The tool call arguments dict.

        Returns:
            True if the tool may destroy data, False otherwise.
        """
        return False

    # --- Optional hooks (Tool Protocol) -----------------------------------------
    # These are optional lifecycle hooks that tools may override to
    # participate in the richer permission / scheduling / validation
    # pipeline.  Default no-ops maintain backward compatibility.

    def validate_input(self, _args: dict[str, Any]) -> str | None:
        """Validate tool arguments before execution.
        Returns None if valid, or an error message string if invalid."""
        return None

    def backfill_observable_input(self, args: dict[str, Any]) -> dict[str, Any]:
        """Mutate *args* in place to fill in observable context before
        the model sees the tool result.  Default no-op."""
        return args

    def should_defer(self, _context: dict[str, Any]) -> bool:
        """Return True if the tool should be deferred (e.g. high resource
        cost, requires user confirmation outside permission check)."""
        return False

    def get_effective_path(self, args: dict[str, Any]) -> str | None:
        """Return the filesystem path this tool operates on, if any.
        Used for path-aware concurrency serialization."""
        _ = args
        return None

    def to_openai_format(self) -> dict[str, Any]:
        """Convert this tool definition to the OpenAI tool format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._minified_schema or _minify_schema(self.input_schema),
            },
        }

    def to_anthropic_format(self) -> dict[str, Any]:
        """Convert this tool definition to the Anthropic tool format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self._minified_schema or _minify_schema(self.input_schema),
        }

    def discovery_card(self) -> dict[str, Any]:
        """Return a discovery card for this tool, used by the discovery system."""
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "parameters": self.input_schema,
            "semantic_type": self.semantic_type,
            "cost_level": self.cost_level,
            "retryability": self.retryability,
            "safe_fallback": self.safe_fallback,
        }


# --- build_tool -- the recommended way ----------------------------------------

def build_tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    execute: Callable[..., Any],
    intents: list[str] | None = None,
    category: str | None = None,
    triggers: list[str] | None = None,
    always_available: bool | None = None,
    max_result_size_chars: int | None = None,
    semantic_type: str | None = None,
    cost_level: str | None = None,
    retryability: str | None = None,
    safe_fallback: str | None = None,
    source: str | None = None,
    ui: dict[str, Any] | None = None,
    is_concurrency_safe: Callable[[dict[str, Any]], bool] | None = None,
    is_readonly: bool | Callable[[dict[str, Any]], bool] | None = None,
    is_destructive: bool | Callable[[dict[str, Any]], bool] | None = None,
    to_openai_format: Callable[..., dict[str, Any]] | None = None,
    to_anthropic_format: Callable[..., dict[str, Any]] | None = None,
    validate_input: Callable[[dict[str, Any]], str | None] | None = None,
    backfill_observable_input: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    should_defer: Callable[[dict[str, Any]], bool] | None = None,
    get_effective_path: Callable[[dict[str, Any]], str | None] | None = None,
) -> EncreTool:
    """Create a tool object without subclassing ``EncreTool``.

    Parameters
    ----------
    name
        Canonical tool name (must be unique across the registry).
    description
        Prompt description shown to the model.
    input_schema
        JSON Schema dict describing the expected parameters.
    execute
        Async callable implementing the tool logic.
        Signature: ``async def execute(**kwargs) -> str``
    intents
        Intent tags for intent-based tool filtering (default ``["general"]``).
    category
        Category label for the discovery system (default ``"general"``).
    triggers
        Natural-language trigger phrases (default ``[]``).
    always_available
        If True, the tool is always visible without discovery (default ``False``).
    max_result_size_chars
        Maximum character count for tool results (default ``100_000``).
    is_concurrency_safe
        Predicate that receives the input dict and returns True when the
        tool may run in parallel with other concurrency-safe tools.

    Returns
    -------
    A ``EncreTool`` instance (legacy compat).  The returned object also
    supports the ``EncreGlobTool()`` call pattern -- calling it returns
    ``self`` -- so existing ``registry.register(EncreGlobTool())`` call
    sites continue to work unchanged.
    """
    if not name or not callable(execute):
        raise ValueError("build_tool: 'name' and 'execute' callable are required")

    minified = _minify_schema(input_schema)

    # Build a tool instance from attributes, mimicking a EncreTool subclass.
    async def _execute(_self, **kwargs: Any) -> str:
        return await execute(**kwargs)

    def _concurrency_check(_self, input_data: dict[str, Any]) -> bool:
        if is_concurrency_safe is None:
            return False
        return is_concurrency_safe(input_data)

    def _readonly_check(_self, input_data: dict[str, Any]) -> bool:
        if is_readonly is None:
            return False
        if isinstance(is_readonly, bool):
            return is_readonly
        return is_readonly(input_data)

    def _destructive_check(_self, input_data: dict[str, Any]) -> bool:
        if is_destructive is None:
            return False
        if isinstance(is_destructive, bool):
            return is_destructive
        return is_destructive(input_data)

    def _openai_format(_self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": minified,
            },
        }

    def _anthropic_format(_self) -> dict[str, Any]:
        return {
            "name": name,
            "description": description,
            "input_schema": minified,
        }

    def _discovery(_self) -> dict[str, Any]:
        return {
            "name": name,
            "category": category or "general",
            "description": description,
            "parameters": input_schema,
        }

    attrs = {
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "_minified_schema": minified,
        "intents": intents if intents is not None else _TOOL_DEFAULTS["intents"],
        "category": category if category is not None else _TOOL_DEFAULTS["category"],
        "triggers": triggers if triggers is not None else _TOOL_DEFAULTS["triggers"],
        "always_available": always_available if always_available is not None else _TOOL_DEFAULTS["always_available"],
        "max_result_size_chars": max_result_size_chars if max_result_size_chars is not None else _TOOL_DEFAULTS["max_result_size_chars"],
        "semantic_type": semantic_type if semantic_type is not None else _TOOL_DEFAULTS["semantic_type"],
        "cost_level": cost_level if cost_level is not None else _TOOL_DEFAULTS["cost_level"],
        "retryability": retryability if retryability is not None else _TOOL_DEFAULTS["retryability"],
        "safe_fallback": safe_fallback if safe_fallback is not None else _TOOL_DEFAULTS["safe_fallback"],
        "source": source if source is not None else _TOOL_DEFAULTS["source"],
        "ui": ui,
        "execute": _execute,
        "is_concurrency_safe": _concurrency_check,
        "is_readonly": _readonly_check,
        "is_destructive": _destructive_check,
        "validate_input": validate_input or (lambda _self, _args: None),
        "backfill_observable_input": backfill_observable_input or (lambda _self, args: args),
        "should_defer": should_defer or (lambda _self, _context: False),
        "get_effective_path": get_effective_path or (lambda _self, _args: None),
        "to_openai_format": to_openai_format if to_openai_format is not None else _openai_format,
        "to_anthropic_format": to_anthropic_format if to_anthropic_format is not None else _anthropic_format,
        "discovery_card": _discovery,
        "__call__": lambda self: self,
    }
    tool_cls = type(name.title().replace("_", "") + "Tool", (EncreTool,), attrs)
    return tool_cls()


# --- Schema compression helper --------------------------------------------------
# Parameter names that are self-explanatory -- their descriptions can be
# omitted from API tool schemas to reduce prompt token usage.
_SELF_EXPLANATORY_PARAMS: frozenset[str] = frozenset({
    "url", "text", "code", "selector", "key", "name", "path", "pattern",
    "port", "host", "timeout", "query", "value", "label", "title",
    "filename", "file_path", "content", "data", "source", "target",
    "x", "y", "x2", "y2", "width", "height", "depth", "count",
    "cwd", "command", "line", "lines", "start", "end", "index",
    "offset", "limit", "glob", "multiline", "output", "result",
    "old_str", "new_str", "replace_all", "max_pages", "max_items",
    "directory", "dir", "token", "tokens", "enabled", "disabled",
    "cell_id", "cell_type", "kernel_name", "node_id", "parent_id",
    "session_id", "task_id", "job_id", "shell_id", "pid",
})


def _minify_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove verbose descriptions from self-explanatory parameters."""
    if "properties" not in schema:
        return schema
    out_props: dict[str, Any] = {}
    for pname, pinfo in schema["properties"].items():
        info = dict(pinfo)
        # Remove description if param name is self-explanatory and
        # the description is longer than ~5 words (just rephrases the name)
        if "description" in info and pname in _SELF_EXPLANATORY_PARAMS:
            del info["description"]
        elif pname == "action" and "description" in info:
            # Keep action descriptions short
            info["description"] = "Action to perform"
        out_props[pname] = info
    result = dict(schema)
    result["properties"] = out_props
    return result


