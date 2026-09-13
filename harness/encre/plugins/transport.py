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

"""Distributed plugin transport layer.

Plugins can run in three execution contexts, selected by the manifest's
``transport`` field:

``local`` (default)
    In-process — the plugin's Python module is imported and its
    ``create_plugin()`` factory called directly.  Zero IPC overhead,
    backward compatible with every existing plugin.

``process``
    Subprocess sandbox — the plugin runs in a child Python process,
    communicating via line-delimited JSON-RPC over stdio.  Provides
    process isolation (crash containment, resource limits, no GIL
    contention with the host), at the cost of ~50ms startup and serialised
    per-call IPC.

``remote``
    Network plugin — the plugin is hosted at an HTTP endpoint
    (``endpoint`` manifest field).  The host sends JSON POST requests;
    the remote server executes tools and returns results.  Enables true
    distributed deployment: plugins can live on separate machines, be
    scaled independently, and be shared across multiple agent instances.

Tool calls to non-local plugins are transparently proxied: the
registry wraps every remote/process tool in a :class:`RemoteToolProxy`
that has the same ``name`` / ``description`` / ``input_schema`` /
``execute()`` interface as a regular :class:`~encre.tools.base.EncreTool`,
so the agent loop is unaware of transport details.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger
from encre.plugins.audit import log_tool_call

logger = get_logger("encre.plugins.transport")

#: Path to the subprocess worker script (relative to this module).
_WORKER_SCRIPT = Path(__file__).parent / "_worker.py"

#: Substrings that mark an environment variable as sensitive — such vars
#: are stripped from the child process environment to prevent credential
#: leakage (gap S3).
_ENV_SENSITIVE = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY")

#: Polling interval (seconds) for the resource watchdog thread.
_WATCHDOG_INTERVAL = 0.5


def _filter_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a child-process environment with secrets filtered out.

    Every env var whose upper-cased name contains a sensitive substring
    (TOKEN, KEY, SECRET, PASSWORD, CREDENTIAL, API_KEY) is **removed**.
    All other vars pass through unchanged so system libraries continue to
    work.  *extra* entries are always applied last (and never filtered).
    """
    env: dict[str, str] = {}
    for key, val in os.environ.items():
        upper = key.upper()
        if any(s in upper for s in _ENV_SENSITIVE):
            logger.debug(f"Filtered sensitive env var '{key}' from subprocess")
            continue
        env[key] = val
    if extra:
        env.update(extra)
    return env


@dataclass
class ToolMetadata:
    """Serialisable description of a tool (no execute reference)."""
    name: str
    description: str
    input_schema: dict[str, Any]
    category: str = "general"
    intents: list[str] = field(default_factory=lambda: ["general"])


@dataclass
class CallResult:
    """Result of a remote tool execution."""
    ok: bool
    data: Any = None
    error: str = ""


class PluginTransport:
    """Abstract transport for plugin execution.

    Concrete subclasses implement the five lifecycle methods.
    The registry selects the transport based on the plugin manifest's
    ``transport`` field.
    """

    transport_name: str = "abstract"

    def activate(self, context_snapshot: dict[str, Any] | None = None) -> bool:
        raise NotImplementedError

    def deactivate(self) -> None:
        raise NotImplementedError

    def get_tool_metas(self) -> list[ToolMetadata]:
        raise NotImplementedError

    def get_skill_metas(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_hook_metas(self) -> list[tuple[str, dict[str, Any]]]:
        raise NotImplementedError

    def execute_tool(self, name: str, args: dict[str, Any]) -> CallResult:
        raise NotImplementedError

    def health_check(self) -> bool:
        return True

    def close(self) -> None:
        pass


class LocalTransport(PluginTransport):
    """In-process transport — the plugin runs in the host process.

    This is the zero-overhead default.  The plugin instance is held
    directly; tool calls are Python method invocations with no IPC.
    """

    transport_name = "local"

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._activated = False

    def activate(self, context_snapshot: dict[str, Any] | None = None) -> bool:  # noqa: ARG002
        """In-process plugins receive the live context via on_activate."""
        self._activated = True
        return True

    def deactivate(self) -> None:
        self._activated = False

    def get_tool_metas(self) -> list[ToolMetadata]:
        if not self._activated:
            return []
        tools = self._plugin.get_tools()
        return [
            ToolMetadata(
                name=getattr(t, "name", ""),
                description=getattr(t, "description", ""),
                input_schema=getattr(t, "input_schema", {}),
                category=getattr(t, "category", "general"),
                intents=getattr(t, "intents", ["general"]),
            )
            for t in tools
        ]

    def get_skill_metas(self) -> list[dict[str, Any]]:
        if not self._activated:
            return []
        return [
            {"name": getattr(s, "name", ""), "data": s}
            for s in self._plugin.get_skills()
        ]

    def get_hook_metas(self) -> list[tuple[str, dict[str, Any]]]:
        if not self._activated:
            return []
        return [
            (event_type, {"handler": handler})
            for event_type, handler in self._plugin.get_hooks()
        ]

    def execute_tool(self, name: str, args: dict[str, Any]) -> CallResult:
        t0 = time.monotonic()
        for tool in self._plugin.get_tools():
            if getattr(tool, "name", None) == name:
                try:
                    result = tool.execute(**args)
                    log_tool_call(
                        getattr(self._plugin, "manifest", type("", (), {"name": "local"})).name,
                        name, "local",
                        ok=True, duration_ms=(time.monotonic() - t0) * 1000, args=args,
                    )
                    return CallResult(ok=True, data=result)
                except Exception as exc:
                    log_tool_call(
                        getattr(self._plugin, "manifest", type("", (), {"name": "local"})).name,
                        name, "local",
                        ok=False, error=str(exc),
                        duration_ms=(time.monotonic() - t0) * 1000, args=args,
                    )
                    return CallResult(ok=False, error=str(exc))
        return CallResult(ok=False, error=f"Tool '{name}' not found")

    @property
    def plugin(self) -> Any:
        return self._plugin


class ProcessTransport(PluginTransport):
    """Subprocess sandbox transport — plugin runs in a child process.

    Communication is line-delimited JSON-RPC over the child's
    stdin/stdout.  The child process imports the plugin module, calls
    its factory, and dispatches tool calls on request.

    Benefits:
    - Crash containment: a segfault in a native extension kills the
      child, not the host.
    - Resource limits: CPU/memory caps via OS-level controls.
    - Parallel execution: multiple process plugins can execute tools
      concurrently without GIL contention.

    The child runs :mod:`encre.plugins._worker`, which implements the
    JSON-RPC protocol.
    """

    transport_name = "process"

    def __init__(
        self,
        factory_target: str,
        pkg_dir: Path,
        resource_limits: dict[str, Any] | None = None,
        plugin_name: str = "",
    ) -> None:
        self._factory_target = factory_target
        self._pkg_dir = pkg_dir
        self._plugin_name = plugin_name
        self._limits = resource_limits or {}
        self._proc: subprocess.Popen | None = None
        self._seq = 0
        self._tool_cache: list[ToolMetadata] | None = None
        self._skill_cache: list[dict[str, Any]] | None = None
        self._hook_cache: list[tuple[str, dict[str, Any]]] | None = None
        # Resource watchdog thread (S1).
        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._watchdog_killed = False

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def _ensure_process(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        # S3: filter secrets from the child environment.
        env = _filter_env({
            "ENCRE_PLUGIN_FACTORY": self._factory_target,
        })
        pkg_dir_str = str(self._pkg_dir)
        pythonpath = env.get("PYTHONPATH", "")
        if pkg_dir_str not in pythonpath:
            env["PYTHONPATH"] = pkg_dir_str + os.pathsep + pythonpath
        try:
            self._proc = subprocess.Popen(
                [sys.executable, str(_WORKER_SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            logger.error("Failed to spawn plugin subprocess: %s", exc)
            self._proc = None
            return False
        # S1: start resource watchdog if limits are configured.
        if self._proc.poll() is None and self._limits:
            self._watchdog_stop.clear()
            self._watchdog_killed = False
            self._watchdog = threading.Thread(
                target=self._watchdog_loop,
                name=f"ea-watchdog-{self._plugin_name or self._factory_target}",
                daemon=True,
            )
            self._watchdog.start()
        return self._proc.poll() is None

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC request and read the response with a timeout.

        S2: the blocking ``readline()`` is replaced by a helper-thread read
        with a configurable timeout.  On timeout the subprocess is killed
        to prevent indefinite hangs.
        """
        if not self._ensure_process():
            raise RuntimeError("Plugin subprocess not running")
        timeout = float(self._limits.get("timeout_s", 30.0))
        req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }
        line = json.dumps(req, ensure_ascii=False) + "\n"
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

        # S2: read stdout in a daemon thread so we can enforce a timeout.
        result_holder: dict[str, str] = {}

        def _reader() -> None:
            assert self._proc is not None and self._proc.stdout is not None
            result_holder["line"] = self._proc.stdout.readline()

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        reader.join(timeout=timeout)
        if reader.is_alive():
            # Timed out — kill the subprocess to unblock the reader.
            self._kill_process(reason=f"RPC '{method}' timed out after {timeout}s")
            raise TimeoutError(f"Plugin RPC '{method}' timed out after {timeout}s")

        resp_line = result_holder.get("line", "")
        if not resp_line:
            err = ""
            if self._proc is not None and self._proc.stderr is not None:
                with contextlib.suppress(Exception):
                    err = self._proc.stderr.readline()
            raise RuntimeError(f"Plugin subprocess closed: {err}")
        resp = json.loads(resp_line)
        if "error" in resp:
            raise RuntimeError(str(resp["error"].get("message", resp["error"])))
        return resp.get("result")

    def _watchdog_loop(self) -> None:
        """S1: monitor subprocess memory/CPU and kill on limit exceed.

        Uses ``psutil`` (a harness dependency) to poll RSS and CPU percent.
        On exceed: logs a warning, sets ``_watchdog_killed``, and kills the
        subprocess so the next ``_rpc()`` call fails fast.
        """
        try:
            import psutil
        except ImportError:
            logger.debug("psutil not available; watchdog disabled")
            return
        memory_limit_mb = self._limits.get("memory_mb")
        cpu_limit = self._limits.get("cpu")  # core fraction, e.g. 1.5
        cpu_limit_percent = None
        if cpu_limit is not None:
            cpu_limit_percent = float(cpu_limit) * 100.0
        while not self._watchdog_stop.wait(_WATCHDOG_INTERVAL):
            if self._proc is None or self._proc.poll() is not None:
                return
            try:
                proc = psutil.Process(self._proc.pid)
                # Memory check
                if memory_limit_mb is not None:
                    rss_mb = proc.memory_info().rss / (1024 * 1024)
                    if rss_mb > float(memory_limit_mb):
                        logger.warning(
                            "Plugin '%s' exceeded memory limit: %.1f MB > %s MB; killing",
                            self._plugin_name, rss_mb, memory_limit_mb,
                        )
                        self._kill_process(reason="memory limit exceeded")
                        return
                # CPU check (averaged over the poll interval)
                if cpu_limit_percent is not None:
                    cpu_pct = proc.cpu_percent(interval=_WATCHDOG_INTERVAL)
                    if cpu_pct > cpu_limit_percent:
                        logger.warning(
                            "Plugin '%s' exceeded CPU limit: %.1f%% > %.1f%%; killing",
                            self._plugin_name, cpu_pct, cpu_limit_percent,
                        )
                        self._kill_process(reason="CPU limit exceeded")
                        return
            except psutil.NoSuchProcess:
                return
            except Exception:
                logger.debug("Watchdog poll error", exc_info=True)

    def _kill_process(self, reason: str = "") -> None:
        """Force-kill the subprocess and mark it as watchdog-killed."""
        self._watchdog_killed = True
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.kill()
                self._proc.wait(timeout=3)
            self._proc = None
        if reason:
            logger.warning("Plugin subprocess killed: %s", reason)

    def _stop_watchdog(self) -> None:
        """Signal the watchdog thread to stop and wait for it."""
        self._watchdog_stop.set()
        if self._watchdog is not None:
            self._watchdog.join(timeout=2.0)
            self._watchdog = None

    def activate(self, context_snapshot: dict[str, Any] | None = None) -> bool:
        if not self._ensure_process():
            return False
        result = self._rpc("activate", {"context": context_snapshot or {}})
        return bool(result)

    def deactivate(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            with contextlib.suppress(Exception):
                self._rpc("deactivate")
        self.close()

    def get_tool_metas(self) -> list[ToolMetadata]:
        if self._tool_cache is not None:
            return self._tool_cache
        raw = self._rpc("get_tools")
        self._tool_cache = [
            ToolMetadata(
                name=t["name"],
                description=t["description"],
                input_schema=t["input_schema"],
                category=t.get("category", "general"),
                intents=t.get("intents", ["general"]),
            )
            for t in raw
        ]
        return self._tool_cache

    def get_skill_metas(self) -> list[dict[str, Any]]:
        if self._skill_cache is not None:
            return self._skill_cache
        self._skill_cache = self._rpc("get_skills") or []
        return self._skill_cache

    def get_hook_metas(self) -> list[tuple[str, dict[str, Any]]]:
        if self._hook_cache is not None:
            return self._hook_cache
        raw = self._rpc("get_hooks") or []
        self._hook_cache = [(h[0], h[1]) for h in raw]
        return self._hook_cache

    def execute_tool(self, name: str, args: dict[str, Any]) -> CallResult:
        t0 = time.monotonic()
        try:
            result = self._rpc("execute_tool", {"name": name, "args": args})
            log_tool_call(
                self._plugin_name, name, "process",
                ok=True, duration_ms=(time.monotonic() - t0) * 1000, args=args,
            )
            return CallResult(ok=True, data=result)
        except Exception as exc:
            log_tool_call(
                self._plugin_name, name, "process",
                ok=False, error=str(exc),
                duration_ms=(time.monotonic() - t0) * 1000, args=args,
            )
            return CallResult(ok=False, error=str(exc))

    def health_check(self) -> bool:
        if self._proc is None:
            return False
        if self._proc.poll() is not None:
            return False
        try:
            self._rpc("ping")
            return True
        except Exception:
            return False

    def close(self) -> None:
        self._stop_watchdog()
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.stdin.close()
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                with contextlib.suppress(Exception):
                    self._proc.kill()
            self._proc = None


class RemoteTransport(PluginTransport):
    """Network plugin transport — tools executed at a remote HTTP endpoint.

    The endpoint must implement the Encre Plugin RPC protocol:
    ``POST {endpoint}/{method}`` with JSON body ``{params}``, returning
    a JSON response ``{"result": ...}`` or ``{"error": {...}}``.

    This enables plugins hosted on separate machines, in containers,
    or behind load balancers — the host agent sees them as regular
    tools with transparent network proxying.
    """

    transport_name = "remote"

    def __init__(self, endpoint: str, plugin_name: str = "") -> None:
        self._endpoint = endpoint.rstrip("/")
        self._plugin_name = plugin_name
        self._tool_cache: list[ToolMetadata] | None = None
        self._activated = False

    def _post(self, method: str, params: dict[str, Any] | None = None) -> Any:
        import httpx
        url = f"{self._endpoint}/{method}"
        try:
            resp = httpx.post(url, json=params or {}, timeout=30.0)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(str(data["error"].get("message", data["error"])))
            return data.get("result", data)
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Remote plugin HTTP {exc.response.status_code}: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"Remote plugin call failed: {exc}") from exc

    def activate(self, context_snapshot: dict[str, Any] | None = None) -> bool:
        result = self._post("activate", {"context": context_snapshot or {}})
        self._activated = bool(result)
        return self._activated

    def deactivate(self) -> None:
        with contextlib.suppress(Exception):
            self._post("deactivate")
        self._activated = False

    def get_tool_metas(self) -> list[ToolMetadata]:
        if self._tool_cache is not None:
            return self._tool_cache
        raw = self._post("get_tools") or []
        self._tool_cache = [
            ToolMetadata(
                name=t["name"],
                description=t["description"],
                input_schema=t["input_schema"],
                category=t.get("category", "general"),
                intents=t.get("intents", ["general"]),
            )
            for t in raw
        ]
        return self._tool_cache

    def get_skill_metas(self) -> list[dict[str, Any]]:
        return self._post("get_skills") or []

    def get_hook_metas(self) -> list[tuple[str, dict[str, Any]]]:
        raw = self._post("get_hooks") or []
        return [(h[0], h[1]) for h in raw]

    def execute_tool(self, name: str, args: dict[str, Any]) -> CallResult:
        t0 = time.monotonic()
        try:
            result = self._post("execute_tool", {"name": name, "args": args})
            log_tool_call(
                self._plugin_name, name, "remote",
                ok=True, duration_ms=(time.monotonic() - t0) * 1000, args=args,
            )
            return CallResult(ok=True, data=result)
        except Exception as exc:
            log_tool_call(
                self._plugin_name, name, "remote",
                ok=False, error=str(exc),
                duration_ms=(time.monotonic() - t0) * 1000, args=args,
            )
            return CallResult(ok=False, error=str(exc))

    def health_check(self) -> bool:
        try:
            self._post("ping")
            return True
        except Exception:
            return False


class RemoteToolProxy:
    """Transparent proxy wrapping a remote/process tool.

    Has the same interface as :class:`~encre.tools.base.EncreTool`
    (``name``, ``description``, ``input_schema``, ``execute``) so the
    agent loop and tool registry are unaware of transport details.
    The first call to ``execute()`` is proxied over the transport;
    the result is cached for stateless tools.
    """

    def __init__(
        self,
        meta: ToolMetadata,
        transport: PluginTransport,
    ) -> None:
        self.name = meta.name
        self.description = meta.description
        self.input_schema = meta.input_schema
        self.category = meta.category
        self.intents = meta.intents
        self._transport = transport

    def execute(self, **kwargs: Any) -> Any:
        result = self._transport.execute_tool(self.name, kwargs)
        if not result.ok:
            raise RuntimeError(result.error)
        return result.data

    async def aexecute(self, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.execute, **kwargs)

    def to_openai_format(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def to_anthropic_format(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def create_transport(manifest: Any, plugin: Any = None, pkg_dir: Path | None = None) -> PluginTransport:
    """Select and instantiate the appropriate transport for a plugin.

    Called by the registry during activation.  Falls back to
    :class:`LocalTransport` when the manifest doesn't specify a
    transport mode (backward compatible).
    """
    mode = getattr(manifest, "transport", "local")
    plugin_name = getattr(manifest, "name", "")
    if mode == "local" or plugin is None:
        return LocalTransport(plugin)
    if mode == "process":
        # Factory target: manifest.factory > derive from plugin module > endpoint
        factory_target = getattr(manifest, "factory", "")
        if not factory_target and plugin is not None:
            mod = getattr(type(plugin), "__module__", "") or getattr(plugin, "__module__", "")
            if mod:
                factory_target = f"{mod}:create_plugin" if hasattr(type(plugin), "create_plugin") else mod
        if not factory_target:
            factory_target = getattr(manifest, "endpoint", "")
        if not factory_target:
            logger.warning(
                "Plugin '%s' declares transport=process but no factory target; "
                "falling back to local",
                plugin_name,
            )
            return LocalTransport(plugin)
        if pkg_dir is None:
            pkg_dir = Path(".")
        return ProcessTransport(
            factory_target=factory_target,
            pkg_dir=pkg_dir,
            resource_limits=getattr(manifest, "resource_limits", {}),
            plugin_name=plugin_name,
        )
    if mode == "remote":
        endpoint = getattr(manifest, "endpoint", "")
        if not endpoint:
            logger.warning(
                "Plugin '%s' declares transport=remote but no endpoint; "
                "falling back to local",
                plugin_name,
            )
            return LocalTransport(plugin)
        return RemoteTransport(endpoint, plugin_name=plugin_name)
    logger.warning("Unknown transport '%s' for plugin '%s'; using local",
                   mode, plugin_name)
    return LocalTransport(plugin)
