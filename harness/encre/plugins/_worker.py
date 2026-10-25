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

import asyncio
import contextlib
import inspect
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any


def _log_stderr(msg: str) -> None:
    sys.stderr.write(f"[ea-worker] {msg}\n")
    sys.stderr.flush()


# Original stdout, reserved exclusively for the JSON-RPC protocol.  Set by
# :func:`_redirect_stdout` before any plugin code runs.
_PROTOCOL_OUT: Any = None


def _redirect_stdout() -> None:
    """Keep stdout reserved for the JSON-RPC protocol stream.

    Any ``print()`` or stdout logging from plugin code (or its libraries)
    would corrupt the RPC stream: the parent either fails to parse the
    polluted line or, worse, desynchronises from the real responses so
    every subsequent call fails — surfacing as an "abnormal disconnect"
    during execution.  ``sys.stdout`` is redirected to stderr and the
    original stream is kept for protocol replies only.
    """
    global _PROTOCOL_OUT
    _PROTOCOL_OUT = sys.stdout
    sys.stdout = sys.stderr


def _apply_posix_rlimits() -> None:
    """Self-apply resource limits handed over by the host (POSIX only).

    ``ENCRE_PLUGIN_MEMORY_BYTES`` caps the address space (``RLIMIT_AS``) so
    a runaway plugin fails its own allocations instead of exhausting the
    host.  The byte value is computed by the Rust sandbox policy.  Must run
    before any plugin code is imported.  Best-effort: an unsupported or
    malformed limit is logged and skipped, never fatal.
    """
    if sys.platform == "win32":
        return
    memory_bytes = os.environ.get("ENCRE_PLUGIN_MEMORY_BYTES", "")
    if not memory_bytes:
        return
    try:
        import resource

        limit = int(memory_bytes)
        if limit > 0:
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception as exc:
        _log_stderr(f"rlimit application skipped: {exc}")


def _load_plugin() -> Any:
    target = os.environ.get("ENCRE_PLUGIN_FACTORY", "")
    if not target:
        raise RuntimeError("ENCRE_PLUGIN_FACTORY env var not set")
    module_path, _, attr = target.partition(":")
    # Defence in depth: the factory target is a dotted import path, never a
    # filesystem path or a package-relative trick — reject anything that
    # importlib would resolve outside normal module naming.
    if not module_path or not all(part.isidentifier() for part in module_path.split(".")):
        raise RuntimeError(f"invalid ENCRE_PLUGIN_FACTORY target: {target!r}")
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
        "aliases": list(getattr(skill, "aliases", []) or []),
        "when_to_use": getattr(skill, "when_to_use", ""),
        "argument_hint": getattr(skill, "argument_hint", ""),
        "allowed_tools": getattr(skill, "allowed_tools", None),
        "model": getattr(skill, "model", None),
        "disable_model_invocation": bool(
            getattr(skill, "disable_model_invocation", False)
        ),
        "user_invocable": bool(getattr(skill, "user_invocable", True)),
        "context": str(getattr(getattr(skill, "context", ""), "value", getattr(skill, "context", ""))),
        "source": str(getattr(getattr(skill, "source", ""), "value", getattr(skill, "source", ""))),
        "file_path": getattr(skill, "file_path", None),
        "body": getattr(skill, "body", "") or getattr(skill, "data", ""),
        "hidden": bool(getattr(skill, "hidden", False)),
        "auto_activate": bool(getattr(skill, "auto_activate", False)),
        "has_prompt": callable(getattr(skill, "get_prompt_for_command", None)),
    }


def _serialise_hook(event_type: str, handler: Any) -> list[Any]:
    # Preserve a resolvable reference so the parent can build a proxy that
    # calls back into this subprocess (``execute_hook``), rather than
    # dropping the handler entirely.
    ref = f"{getattr(handler, '__module__', '')}:{getattr(handler, '__qualname__', type(handler).__name__)}"
    return [event_type, {"handler_ref": ref}]


class _WorkerContext:
    """Minimal ``on_activate`` context for subprocess plugins.

    The rich :class:`~encre.plugins.context.PluginContext` is unavailable
    across the process boundary, so this collector captures tool
    registrations and exposes them alongside the plugin's own ``get_tools``
    output.  Surfaces that cannot cross the boundary are no-op collectors
    that return an undo closure, so plugin code written against the full
    API does not crash when running sandboxed.
    """

    def __init__(self) -> None:
        self._tools: list[Any] = []
        # Controlled resource API (set in _build_worker_context when the
        # plugin name is known).  None for legacy workers.
        self.resources: Any = None
        # Per-plugin persistent state (same file the host uses).
        self.state: Any = None
        # Surfaces whose registrations cannot cross the process boundary.
        # Activation fails closed when a plugin actually uses one instead of
        # silently accepting a no-op registration.
        self._unsupported: list[str] = []

    def _collector(self, bucket: list[Any] | None = None, unsupported: str = ""):
        target = bucket if bucket is not None else []

        class _Api:
            def register(self, item: Any) -> Any:
                if unsupported:
                    if unsupported not in owner._unsupported:
                        owner._unsupported.append(unsupported)
                    return lambda: None
                target.append(item)

                def _undo() -> None:
                    with contextlib.suppress(ValueError):
                        target.remove(item)

                return _undo

            def __getattr__(self, _name: str) -> Any:  # pragma: no cover - passthrough
                def _noop(*_a: Any, **_kw: Any) -> Any:
                    return lambda: None

                return _noop

        owner = self
        return _Api()

    def _init_surfaces(self) -> None:
        self.tools = self._collector(self._tools)
        for surface in (
            "backends", "loop", "events", "permissions", "commands",
            "sub_agents", "templates", "context_providers", "ui_providers",
            "gateway_adapters", "memory_stores", "ws_handlers",
            "http_routes", "lifecycle",
        ):
            setattr(self, surface, self._collector(unsupported=surface))

    @property
    def collected_tools(self) -> list[Any]:
        return list(self._tools)


def _build_worker_context(plugin_name: str = "") -> _WorkerContext:
    ctx = _WorkerContext()
    ctx._init_surfaces()
    if plugin_name:
        # Controlled resource access, arbitrated by THIS process's engine
        # instance (grants deployed in the activate RPC).  Reuses the exact
        # same class as the host-side PluginContext — no second
        # implementation to diverge.
        from encre.plugins.context import _ResourceApi
        from encre.plugins.state import PluginStateStore

        ctx.resources = _ResourceApi(plugin_name)
        # Same file the host would use, so state survives worker respawn and
        # is identical whether the plugin runs local or process-transported.
        state_file = os.environ.get("ENCRE_PLUGIN_STATE_PATH", "")
        ctx.state = PluginStateStore(plugin_name, path=Path(state_file) if state_file else None)
    return ctx


def _send_response(req_id: int, result: Any = None, error: dict[str, Any] | None = None) -> None:
    resp: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    out = _PROTOCOL_OUT if _PROTOCOL_OUT is not None else sys.stderr
    out.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
    out.flush()


async def _handle_method(
    plugin: Any,
    method: str,
    params: dict[str, Any],
    state: dict[str, Any],
) -> Any:
    if method == "ping":
        return True

    if method == "activate":
        plugin_name = str(params.get("plugin", "") or "")
        # Deploy the manifest resource grants into this process's own
        # permission-engine instance BEFORE the plugin can run any code, so
        # the controlled ``resources`` API arbitrates identically to a
        # local plugin.  Fail-closed: an unusable native core aborts
        # activation.
        grants = params.get("resources")
        if plugin_name and isinstance(grants, dict):
            from encre import native as _native

            _native.permission_set_plugin_resources(
                plugin_name, json.dumps(grants, ensure_ascii=False)
            )
        context = _build_worker_context(plugin_name)
        plugin.on_activate(context)
        state["context"] = context
        state["activated"] = True
        return {"ok": True, "unsupported": list(context._unsupported)}

    if method == "deactivate":
        plugin.on_deactivate()
        state["activated"] = False
        return True

    if method == "get_tools":
        tools = list(plugin.get_tools())
        context = state.get("context")
        if context is not None:
            known = {getattr(t, "name", None) for t in tools}
            for tool in context.collected_tools:
                if getattr(tool, "name", None) not in known:
                    tools.append(tool)
        return [_serialise_tool(t) for t in tools]

    if method == "get_skills":
        return [_serialise_skill(s) for s in plugin.get_skills()]

    if method == "get_hooks":
        return [_serialise_hook(et, h) for et, h in plugin.get_hooks()]

    if method == "get_skill_prompt":
        name = params.get("name", "")
        args = params.get("args")
        ctx = params.get("context") or {}
        for skill in plugin.get_skills():
            if getattr(skill, "name", None) == name:
                fn = getattr(skill, "get_prompt_for_command", None)
                if not callable(fn):
                    raise RuntimeError(f"Skill '{name}' has no prompt renderer")
                result = fn(args, ctx)
                if inspect.isawaitable(result):
                    result = await result
                return result
        raise RuntimeError(f"Skill '{name}' not found in subprocess plugin")

    if method == "execute_hook":
        event_type = params.get("event_type", "")
        args = params.get("args", [])
        kwargs = params.get("kwargs", {})
        for et, handler in plugin.get_hooks():
            if et == event_type:
                result = handler(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return result
        raise RuntimeError(f"Hook '{event_type}' not found in subprocess plugin")

    if method == "execute_tool":
        name = params.get("name", "")
        args = params.get("args", {})
        for tool in plugin.get_tools():
            if getattr(tool, "name", None) == name:
                result = tool.execute(**args)
                # Tool ``execute`` callables are async; drive the coroutine
                # to completion, otherwise the awaiting object itself gets
                # serialised as a "<coroutine object ...>" string.
                if inspect.isawaitable(result):
                    result = await result
                return result
        raise RuntimeError(f"Tool '{name}' not found in subprocess plugin")

    raise RuntimeError(f"Unknown method: {method}")


async def main() -> None:
    # Sandbox first: the plugin must never run with unrestricted resources.
    _apply_posix_rlimits()
    # Redirect sys.stdout to stderr so plugin print()/logging can never
    # corrupt the JSON-RPC protocol stream (see _redirect_stdout).
    _redirect_stdout()
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
        req_id: Any = 0
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
            req_id = req.get("id", 0)
            method = req.get("method", "")
            params = req.get("params", {})
            result = await _handle_method(
                plugin, method, params if isinstance(params, dict) else {}, state
            )
            _send_response(req_id, result=result)
        except BaseException as exc:
            # BaseException, not just Exception: a plugin raising
            # SystemExit / KeyboardInterrupt / anything unusual must not
            # tear the worker down — the parent would see an abrupt
            # process disconnect mid-execution instead of a clean error
            # response.  There is no external canceller in the child, so
            # CancelledError is converted too.
            tb = traceback.format_exc()
            _log_stderr(tb)
            _send_response(req_id, error={"message": str(exc), "code": -32603})

    if state.get("activated"):
        with contextlib.suppress(Exception):
            plugin.on_deactivate()


if __name__ == "__main__":
    asyncio.run(main())
