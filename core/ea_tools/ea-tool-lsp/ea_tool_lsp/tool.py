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

"""LSP (Language Server Protocol) tool for the Encre tool system.

This module exposes a single :data:`lsp_tool` that wraps the process-
wide LSP manager (see :func:`_ensure_manager`) to give the agent
symbol-accurate code intelligence: go to definition, find references,
hover info, diagnostics, and document symbols.

Every query goes through the language server that the manager has
started for the workspace, so results are semantically correct
(they know the AST) rather than text-matching. That is also why the
tool is not concurrency-safe: multiple concurrent queries against
the same server can interleave results, so the tool declares
``is_concurrency_safe=lambda _: False``.
"""
import asyncio
from typing import Any

from encre.tools.base import build_tool

from encre.tools.runtime import get_lsp_manager as _get_manager


def _ensure_manager() -> Any:
    """Return the process-wide LSP manager, bootstrapping one on demand.

    The runtime slot is empty by default (the host app may or may not
    have installed a manager); the tool must therefore create the
    real ``EncreLSPManager`` itself so LSP works in any agent loop.
    The import of :class:`EncreLSPManager` is deferred to call time
    so merely importing this module does not pull in the LSP manager
    package (which in turn pulls in JSON-RPC plumbing and language
    server process management).

    Returns:
        A live :class:`EncreLSPManager` instance. The same instance
        is reused across calls so the underlying language servers
        stay connected between tool invocations.
    """
    manager = _get_manager()
    if manager is None:
        from encre.capabilities.search.lsp.manager import EncreLSPManager
        from encre.tools.runtime import set_lsp_manager

        manager = EncreLSPManager()
        set_lsp_manager(manager)
    return manager


def _format_symbols(symbols: list[dict[str, Any]], indent: int = 0) -> str:
    """Format a tree of LSP document symbols as an indented text outline.

    Each symbol is rendered on one line as ``<Kind>: <name>
    (<line>:<column>)`` when a range is available, or without the
    position when the server only returned the symbol name. Child
    symbols are rendered recursively with an extra indentation level
    so the outline shows the nesting of classes inside modules, etc.

    Args:
        symbols: Flat or nested list of symbol dicts as returned by
            the LSP ``documentSymbol`` request. Each dict may
            include ``name``, ``kind``, ``range``, and ``children``.
        indent: Indentation depth used for this level. The recursive
            call passes ``indent + 1``.

    Returns:
        The rendered outline, suitable for inclusion in a tool
        response.
    """
    lines: list[str] = []
    prefix = "  " * indent
    for sym in symbols:
        name = sym.get("name", "?")
        kind = sym.get("kind", 0)
        kind_name = _symbol_kind_name(kind)
        if "range" in sym and isinstance(sym["range"], dict):
            start = sym["range"].get("start", {})
            lines.append(
                f"{prefix}{kind_name}: {name} "
                f"({start.get('line', 0)}:{start.get('character', 0)})"
            )
        else:
            lines.append(f"{prefix}{kind_name}: {name}")

        children = sym.get("children", [])
        if isinstance(children, list) and children:
            lines.append(_format_symbols(children, indent + 1))
    return "\n".join(lines)


def _symbol_kind_name(kind: int) -> str:
    """Map an LSP ``SymbolKind`` integer to a short human-readable name.

    The mapping follows the LSP specification (https://microsoft.github.io/
    language-server-protocol/specifications/lsp-3.17/specification/#symbolKind).
    Unknown kinds fall through to ``Kind(<int>)`` so a future LSP
    version that adds new symbol kinds does not crash the renderer.
    """
    names: dict[int, str] = {
        1: "File",
        2: "Module",
        3: "Namespace",
        4: "Package",
        5: "Class",
        6: "Method",
        7: "Property",
        8: "Field",
        9: "Constructor",
        10: "Enum",
        11: "Interface",
        12: "Function",
        13: "Variable",
        14: "Constant",
        15: "String",
        16: "Number",
        17: "Boolean",
        18: "Array",
        19: "Object",
        20: "Key",
        21: "Null",
        22: "EnumMember",
        23: "Struct",
        24: "Event",
        25: "Operator",
        26: "TypeParameter",
    }
    return names.get(kind, f"Kind({kind})")


async def _lsp_execute(**kwargs: Any) -> str:
    """Execute an LSP operation against the workspace's language server.

    Dispatches a set of LSP operations by name. All operations except
    ``initialize`` and ``shutdown`` are guarded by a 30-second
    :func:`asyncio.wait_for` timeout so a wedged language server
    cannot hang the tool loop indefinitely. All errors are returned
    as string messages rather than raised, because the tool executor
    surfaces strings directly to the LLM.

    Args:
        **kwargs: Tool arguments. ``operation`` (required, one of
            ``go_to_definition``, ``find_references``, ``hover``,
            ``diagnostics``, ``document_symbols``, ``initialize``,
            ``shutdown``). ``file_path``, ``line``, ``character``
            are required for the query operations. ``workspace`` is
            required for ``initialize``.

    Returns:
        A human-readable string describing the operation's result, or
        an ``Error:``-prefixed string on failure.
    """
    try:
        operation = kwargs.get("operation", "")
        file_path = kwargs.get("file_path", "")
        line = kwargs.get("line", 0)
        character = kwargs.get("character", 0)
        workspace = kwargs.get("workspace", "")

        if operation == "initialize":
            if not workspace:
                return "Error: workspace is required for initialization"
            await _ensure_manager().initialize_for_workspace(workspace)
            return "LSP servers initialized"

        if operation == "shutdown":
            await _ensure_manager().shutdown()
            # Clear the runtime slot so a later call re-creates a
            # fresh manager rather than reusing a dead one.
            from encre.tools.runtime import set_lsp_manager
            set_lsp_manager(None)
            return "LSP servers shut down"

        manager = _ensure_manager()

        if operation == "diagnostics":
            if not file_path:
                return "Error: file_path is required"
            diagnostics = await asyncio.wait_for(manager.get_diagnostics(file_path), timeout=30)
            if not diagnostics:
                return "No diagnostics found"
            lines: list[str] = []
            for d in diagnostics:
                lines.append(
                    f"[{d.severity}] {d.message} "
                    f"at {d.range.start.line}:{d.range.start.character}"
                )
            return "\n".join(lines)

        if operation == "go_to_definition":
            if not file_path:
                return "Error: file_path is required"
            locations = await asyncio.wait_for(manager.go_to_definition(file_path, line, character), timeout=30)
            if not locations:
                return "No definition found"
            lines = []
            for loc in locations:
                lines.append(
                    f"{loc.uri} "
                    f"({loc.range.start.line}:{loc.range.start.character})"
                )
            return "\n".join(lines)

        if operation == "find_references":
            if not file_path:
                return "Error: file_path is required"
            locations = await asyncio.wait_for(manager.find_references(file_path, line, character), timeout=30)
            if not locations:
                return "No references found"
            lines = []
            for loc in locations:
                lines.append(
                    f"{loc.uri} "
                    f"({loc.range.start.line}:{loc.range.start.character})"
                )
            return "\n".join(lines)

        if operation == "hover":
            if not file_path:
                return "Error: file_path is required"
            hover_result = await asyncio.wait_for(manager.hover(file_path, line, character), timeout=30)
            if hover_result is None:
                return "No hover information available"
            return hover_result.contents

        if operation == "document_symbols":
            if not file_path:
                return "Error: file_path is required"
            symbols = await asyncio.wait_for(manager.document_symbols(file_path), timeout=30)
            if not symbols:
                return "No symbols found"
            return _format_symbols(symbols)

        return f"Unknown operation: {operation}"
    except asyncio.TimeoutError:
        return "Error: LSP operation timed out after 30 seconds"
    except Exception as exc:
        return f"Error: LSP operation failed: {exc}"


lsp_tool = build_tool(
    name="lsp",
    description=(
        "Query the LSP language server for code intelligence: go to definition, "
        "find references, hover info, diagnostics, and document symbols. Use "
        "this instead of grep when you need symbol-accurate results (e.g. jump "
        "to a definition, list all references of a symbol) rather than raw "
        "text matches. Requires the LSP server for the file's language to be "
        "initialized for the workspace. "
        "TIP: Call operation='initialize' with the workspace path once before "
        "querying files in that workspace. "
        "TIP: line/character are 0-based; check the editor's coordinate system. "
        "AVOID: Using lsp for plain text search -- grep is faster and needs "
        "no language server."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "go_to_definition",
                    "find_references",
                    "hover",
                    "diagnostics",
                    "document_symbols",
                    "initialize",
                    "shutdown",
                ],
                "description": "LSP operation to perform (required). 'initialize' and 'shutdown' manage the server lifecycle; the rest query a file at a cursor position.",
            },
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file to query (required for all operations except 'initialize'/'shutdown').",
            },
            "line": {
                "type": "integer",
                "description": "0-based line number of the cursor position (optional, used by go_to_definition/find_references/hover).",
            },
            "character": {
                "type": "integer",
                "description": "0-based character (column) offset of the cursor position (optional, used by go_to_definition/find_references/hover).",
            },
            "workspace": {
                "type": "string",
                "description": "Absolute path of the workspace root directory (required for 'initialize' only).",
            },
        },
        "required": ["operation"],
    },
    execute=_lsp_execute,
    intents=["coding"],
    category="code_intel",
    semantic_type="search",
    is_concurrency_safe=lambda _: False,
    is_readonly=True,
)
