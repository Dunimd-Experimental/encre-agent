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

"""Untrusted-source classification and result wrapping for tool outputs.

Batch C4 convergence: the former ``ToolPipeline`` / streaming-executor
implementation in this module (and ``encre.tools.streaming_executor``) was
retired in favour of the live loop phases (``encre.loop.phases.tools`` /
``encre.loop.phases.execute``).  This module now only houses the
prompt-injection defense helpers the live code imports:

* :func:`_is_untrusted_source` / :func:`_wrap_untrusted_result` -- used by the
  tool preparation / execution phases to delimit external content.
* :func:`set_plugin_tool_names` -- called by the agent's plugin injection path.
"""

from __future__ import annotations

from typing import Any

# --- Prompt-injection defense ---------------------------------------------
# Wraps tool results from high-risk sources in a delimiter so the model
# can distinguish external content from its own reasoning.
# Mirrors Hermes' <untrusted_tool_result> wrapping strategy.

_UNTRUSTED_TOOL_RESULT_TOOLS: frozenset[str] = frozenset({
    "web_search", "web_fetch", "browser", "computer",
    "read", "pdf", "spreadsheet",
    "document", "media", "media_api", "rest_client",
    "mcp",  # generic MCP proxy (mcp__*-prefixed tools covered by prefix)
})

# Tool-name prefixes whose output comes from external code paths (MCP servers,
# plugin-provided tools) and therefore must always be treated as untrusted,
# regardless of the specific tool name.
_UNTRUSTED_TOOL_RESULT_PREFIXES: tuple[str, ...] = ("mcp__", "plugin__")

#: Names of plugin-provided tools registered into the tool registry.  Plugin
#: tools execute third-party code and their output is untrusted by default,
#: even when they register under plain (unprefixed) names.  Kept as a
#: module-level set because the untrusted check receives only a tool name.
#: The single writer is :func:`set_plugin_tool_names` (called by the agent
#: whenever plugin tools are (re-)injected, e.g. at load or hot reload).
_PLUGIN_TOOL_NAMES: frozenset[str] = frozenset()


def set_plugin_tool_names(names: Any) -> None:
    """Replace the set of plugin-provided tool names treated as untrusted.

    Called by the agent's plugin injection path with the full current set,
    so adding and removing plugins (hot reload) stays consistent.
    """
    global _PLUGIN_TOOL_NAMES
    _PLUGIN_TOOL_NAMES = frozenset(n for n in names if n)


def _is_untrusted_source(tool_name: str) -> bool:
    """Whether a tool's output originates from an external, untrusted source."""
    return (
        tool_name in _UNTRUSTED_TOOL_RESULT_TOOLS
        or tool_name in _PLUGIN_TOOL_NAMES
        or tool_name.startswith(_UNTRUSTED_TOOL_RESULT_PREFIXES)
    )

_UNTRUSTED_DELIMITER_OPEN = "<untrusted_tool_result>"
_UNTRUSTED_DELIMITER_CLOSE = "</untrusted_tool_result>"


def _wrap_untrusted_result(tool_name: str, content: str) -> str:
    """Wrap tool output in untrusted-result delimiters, defanging embedded tokens.

    High-risk tools (web, MCP, browser, etc.) produce content from external
    sources that may contain prompt-injection payloads.  Wrapping them in a
    structured delimiter lets the model distinguish them from system / user
    instructions.  Embedded delimiter tokens are defanged to prevent nested
    delimiter attacks.
    """
    from encre.errors import _NEUTRALIZE_DELIMITERS
    safe = _NEUTRALIZE_DELIMITERS(content)
    return (
        f"{_UNTRUSTED_DELIMITER_OPEN} source=\"{tool_name}\"\n"
        f"{safe}\n"
        f"{_UNTRUSTED_DELIMITER_CLOSE}"
    )


# --- Tool argument JSON Schema validation ---------------------------------
#
# Runtime subset validator (pure Python, no third-party dependency):
# required fields, type checks (object/array/string/number/integer/
# boolean/null), enum constraints and recursive properties/items
# traversal.  The live loop phases call :func:`validate_tool_args` before
# executing a tool so malformed model-emitted arguments are rejected with
# a structured error -- no LLM repair, no retry.

#: Depth cap so a pathological (self-referential) schema cannot recurse
#: forever.  Real tool schemas nest at most a handful of levels.
_MAX_SCHEMA_DEPTH = 16
#: Maximum number of validation problems reported in one error message.
_MAX_SCHEMA_ERRORS = 5


def _schema_type_ok(value: Any, expected: str) -> bool:
    """JSON-Schema type check for one expected type name."""
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True  # unknown type annotations are ignored


def _schema_node_errors(
    schema: dict[str, Any], value: Any, path: str, depth: int,
) -> list[str]:
    """Collect validation errors for *value* against one schema node."""
    if depth > _MAX_SCHEMA_DEPTH:
        return []
    errors: list[str] = []
    expected = schema.get("type")
    if isinstance(expected, str):
        if not _schema_type_ok(value, expected):
            errors.append(
                f"{path or 'arguments'}: expected type '{expected}', "
                f"got '{type(value).__name__}'"
            )
            return errors  # type mismatch: deeper checks are meaningless
    elif isinstance(expected, list) and not any(
        isinstance(t, str) and _schema_type_ok(value, t) for t in expected
    ):
        errors.append(
            f"{path or 'arguments'}: expected one of {expected}, "
            f"got '{type(value).__name__}'"
        )
        return errors
    enum = schema.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        errors.append(f"{path or 'arguments'}: value {value!r} not in enum {enum}")
    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    errors.append(f"missing required field '{key}'")
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, sub in properties.items():
                if key in value and isinstance(sub, dict):
                    child_path = f"{path}.{key}" if path else str(key)
                    errors.extend(
                        _schema_node_errors(sub, value[key], child_path, depth + 1)
                    )
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                errors.extend(
                    _schema_node_errors(items, item, f"{path}[{index}]", depth + 1)
                )
    return errors


def validate_tool_args(schema: Any, args: Any) -> str | None:
    """Validate tool call arguments against the registry's JSON Schema.

    Implements the JSON-Schema subset Encre tools actually use: required
    fields, type checks, enum constraints and recursive properties/items
    traversal.  A missing or non-dict schema skips validation so legacy
    builtin tool definitions keep working unchanged.

    Args:
        schema: The tool's ``input_schema`` (any junk tolerated).
        args: The parsed model-emitted argument dict.

    Returns:
        ``None`` when the arguments are valid (or the schema is absent);
        otherwise a human-readable error naming the failing fields.
    """
    if not isinstance(schema, dict) or not schema:
        return None
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    errors = _schema_node_errors(schema, args, "", 0)
    if not errors:
        return None
    shown = "; ".join(errors[:_MAX_SCHEMA_ERRORS])
    hidden = len(errors) - _MAX_SCHEMA_ERRORS
    if hidden > 0:
        shown += f"; (+{hidden} more)"
    return shown
