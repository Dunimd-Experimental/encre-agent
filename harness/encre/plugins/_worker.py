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

"""Subprocess worker for :class:`~encre.plugins.transport.ProcessTransport`.

Implements the JSON-RPC protocol over stdio:

    Request  (stdin):  {"jsonrpc":"2.0","id":1,"method":"activate","params":{...}}
    Response (stdout): {"jsonrpc":"2.0","id":1,"result":true}

Supported methods:
    ping         — health check, returns True
    activate     — call plugin.on_activate(context_snapshot)
    deactivate   — call plugin.on_deactivate()
    get_tools    — return serialisable tool metadata list
    get_skills   — return serialisable skill metadata list
    get_hooks    — return (event_type, handler_info) pairs
    execute_tool — execute a tool by name with kwargs

The worker reads the plugin factory target from the
``ENCRE_PLUGIN_FACTORY`` environment variable (``module:attr`` format).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import traceback
from typing import Any


def _log_stderr(msg: str) -> None:
    sys.stderr.write(f"[ea-worker] {msg}\n")
    sys.stderr.flush()


def _load_plugin() -> Any:
    target = os.environ.get("ENCRE_PLUGIN_FACTORY", "")
    if not target:
        raise RuntimeError("ENCRE_PLUGIN_FACTORY env var not set")
    module_path, _, attr = target.partition(":")
    import importlib
    module = importlib.import_module(module_path)
    factory = getattr(module, attr) if attr else module
    if callable(factory):
        return factory()
    return factory


def _serialise_tool(tool: Any) -> dict[str, Any]:
    return {
        "name": getattr(tool, "name", ""),
        "description": getattr(tool, "description", ""),
        "input_schema": getattr(tool, "input_schema", {}),
        "category": getattr(tool, "category", "general"),
        "intents": getattr(tool, "intents", ["general"]),
    }


def _serialise_skill(skill: Any) -> dict[str, Any]:
    if isinstance(skill, dict):
        return skill
    return {
        "name": getattr(skill, "name", ""),
        "description": getattr(skill, "description", ""),
        "data": str(skill),
    }


def _serialise_hook(event_type: str, handler: Any) -> list[Any]:
    return [event_type, {"handler_type": type(handler).__name__}]


def _send_response(req_id: int, result: Any = None, error: dict[str, Any] | None = None) -> None:
    resp: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    sys.stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _handle_method(
    plugin: Any,
    method: str,
    params: dict[str, Any],
    state: dict[str, Any],
) -> Any:
    if method == "ping":
        return True

    if method == "activate":
        plugin.on_activate(None)
        state["activated"] = True
        return True

    if method == "deactivate":
        plugin.on_deactivate()
        state["activated"] = False
        return True

    if method == "get_tools":
        return [_serialise_tool(t) for t in plugin.get_tools()]

    if method == "get_skills":
        return [_serialise_skill(s) for s in plugin.get_skills()]

    if method == "get_hooks":
        return [_serialise_hook(et, h) for et, h in plugin.get_hooks()]

    if method == "execute_tool":
        name = params.get("name", "")
        args = params.get("args", {})
        for tool in plugin.get_tools():
            if getattr(tool, "name", None) == name:
                return tool.execute(**args)
        raise RuntimeError(f"Tool '{name}' not found in subprocess plugin")

    raise RuntimeError(f"Unknown method: {method}")


def main() -> None:
    try:
        plugin = _load_plugin()
    except Exception:
        _log_stderr(f"Failed to load plugin:\n{traceback.format_exc()}")
        sys.exit(1)

    state: dict[str, Any] = {"activated": False}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            req_id = req.get("id", 0)
            method = req.get("method", "")
            params = req.get("params", {})
            result = _handle_method(plugin, method, params, state)
            _send_response(req_id, result=result)
        except Exception as exc:
            tb = traceback.format_exc()
            _log_stderr(tb)
            req_id = req.get("id", 0) if "req" in dir() else 0
            _send_response(req_id, error={"message": str(exc), "code": -32603})

    if state.get("activated"):
        with contextlib.suppress(Exception):
            plugin.on_deactivate()


if __name__ == "__main__":
    main()
