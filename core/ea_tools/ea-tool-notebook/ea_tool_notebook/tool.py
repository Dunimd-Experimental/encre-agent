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

"""Notebook (Jupyter) tool for the Encre tool system.

This module exposes a single :data:`notebook_tool` that wraps a
persistent :class:`EncreNotebookSession`. Unlike one-shot bash
Python, the session's kernel stays alive between cells so variables
defined in one cell remain available in later cells -- this is what
makes the tool suitable for iterative data exploration, long-running
computation, and visualization workflows where re-executing the
setup code on every call would be wasteful.

The session is created lazily on first use (:func:`_get_session`) so
the module can be imported without booting a Jupyter kernel. The
``reset`` action replaces the session in place; other actions go
through the same singleton until the tool is reset.
"""
import json
from typing import Any

from encre.notebook.session import EncreNotebookSession
from encre.tools.base import build_tool

_session: EncreNotebookSession | None = None


def _get_session() -> EncreNotebookSession:
    """Return the process-wide notebook session, creating one if needed.

    The import of :class:`EncreNotebookSession` is deferred to
    :func:`_get_session` time rather than module load so merely
    importing this tool does not boot a Jupyter kernel. The kernel
    is only created on the first tool invocation that actually needs
    it.
    """
    global _session
    if _session is None:
        _session = EncreNotebookSession()
    return _session


async def _notebook_execute(**kwargs: Any) -> str:
    """Execute a notebook action against the persistent session.

    Dispatches on the ``action`` key to the matching session method.
    All actions return a JSON string (either ``{"ok": true, ...}`` or
    ``{"ok": false, "error": ...}``) rather than a plain string, so
    the agent can programmatically inspect the outcome instead of
    parsing free-form text.

    The ``execute_cell`` and ``execute_all`` actions wrap the
    session call in a try/except that adds a hint about
    ``ipykernel`` being required, because a missing kernel is the
    single most common failure mode for first-time users and the
    raw exception message is not always clear about that.

    Args:
        **kwargs: Tool arguments. ``action`` (required, one of
            ``create_cell``, ``edit_cell``, ``execute_cell``,
            ``execute_all``, ``get_output``, ``get_state``,
            ``delete_cell``, ``reset``). Other keys depend on the
            action: ``code``, ``cell_id``, ``cell_type``, ``timeout``,
            ``kernel_name``.

    Returns:
        A JSON string describing the action's outcome.
    """
    action = kwargs.get("action", "")
    session = _get_session()

    if action == "create_cell":
        code = kwargs.get("code", "")
        cell_type = kwargs.get("cell_type", "code")
        cell_id = session.create_cell(code, cell_type)
        return json.dumps({"ok": True, "cell_id": cell_id})

    elif action == "edit_cell":
        cell_id = kwargs.get("cell_id", "")
        code = kwargs.get("code", "")
        ok = session.edit_cell(cell_id, code)
        return json.dumps({"ok": ok, "cell_id": cell_id})

    elif action == "execute_cell":
        cell_id = kwargs.get("cell_id", "")
        timeout = kwargs.get("timeout", 60)
        try:
            result = await session.execute_cell(cell_id, timeout)
        except Exception as e:
            return json.dumps(
                {"ok": False, "error": f"Cell execution failed: {e}. Ensure a Jupyter kernel is installed (pip install ipykernel)."},
                ensure_ascii=False,
            )
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    elif action == "execute_all":
        timeout = kwargs.get("timeout", 300)
        try:
            results = await session.execute_all(timeout)
        except Exception as e:
            return json.dumps(
                {"ok": False, "error": f"Execution failed: {e}. Ensure a Jupyter kernel is installed (pip install ipykernel)."},
                ensure_ascii=False,
            )
        return json.dumps({"ok": True, "results": results}, ensure_ascii=False)

    elif action == "get_output":
        cell_id = kwargs.get("cell_id", "")
        output = session.get_output(cell_id)
        error = session.get_error(cell_id)
        return json.dumps({"ok": True, "output": output, "error": error}, ensure_ascii=False)

    elif action == "get_state":
        state = session.get_state()
        return json.dumps(state, ensure_ascii=False)

    elif action == "delete_cell":
        cell_id = kwargs.get("cell_id", "")
        ok = session.delete_cell(cell_id)
        return json.dumps({"ok": ok})

    elif action == "reset":
        global _session
        # Close the previous session before installing a new one so
        # the old kernel's stdout/stderr pipes are not left dangling.
        if _session is not None:
            _session.close()
        kernel_name = kwargs.get("kernel_name", "python3")
        _session = EncreNotebookSession(kernel_name)
        return json.dumps({"ok": True, "message": "Notebook session reset"})

    else:
        return json.dumps({"ok": False, "error": f"Unknown action: {action}"})


notebook_tool = build_tool(
    name="notebook",
    description=(
        "Manage an interactive Jupyter-style notebook session with a "
        "persistent kernel. Supports creating/editing cells, executing code "
        "or markdown, and inspecting outputs and session state. Use this for "
        "iterative data exploration, visualization, or long-running "
        "computations where kernel state must persist across cells -- unlike "
        "one-shot bash python, variables defined in one cell remain "
        "available in later cells. "
        "TIP: Call action='get_state' to inspect existing cells and kernel "
        "status before issuing further actions. "
        "AVOID: Running very long computations with execute_all without a "
        "timeout -- a single slow cell can block the whole run."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "create_cell",
                    "edit_cell",
                    "execute_cell",
                    "execute_all",
                    "get_output",
                    "get_state",
                    "delete_cell",
                    "reset",
                ],
                "description": "Notebook action to perform (required). 'reset' restarts the kernel and clears all cells.",
            },
            "code": {
                "type": "string",
                "description": "Source code (or markdown) for the cell (required for create_cell and edit_cell).",
            },
            "cell_id": {
                "type": "string",
                "description": "Target cell id (required for edit_cell, execute_cell, get_output, delete_cell). Obtainable from create_cell result or get_state.",
            },
            "cell_type": {
                "type": "string",
                "enum": ["code", "markdown"],
                "description": "Cell type for create_cell (optional, default 'code').",
            },
            "timeout": {
                "type": "integer",
                "description": "Execution timeout in seconds (optional, default 60 for execute_cell, 300 for execute_all).",
            },
            "kernel_name": {
                "type": "string",
                "description": "Kernel name for the reset action (optional, default 'python3').",
            },
        },
        "required": ["action"],
    },
    execute=_notebook_execute,
    intents=["data"],
    category="data",
    semantic_type="exec",
    is_destructive=lambda args: args.get("action", "") in ("execute", "delete_cell", "delete", "reset"),
)
