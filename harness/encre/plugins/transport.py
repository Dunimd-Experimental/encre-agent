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
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from encre.logging_config import get_logger
from encre.plugins.audit import log_tool_call

logger = get_logger("encre.plugins.transport")

#: Path to the subprocess worker script (relative to this module).
_WORKER_SCRIPT = Path(__file__).parent / "_worker.py"

#: Polling interval (seconds) for the resource watchdog thread.
_WATCHDOG_INTERVAL = 0.5
# CPU-limit enforcement grace: freshly spawned workers legitimately spike
# above their CPU rate while importing plugin dependencies (single-shot
# imports parallelised by the interpreter).  Killing during this window
# produced "connects then instantly dies" failures for CPU-capped plugins.
_CPU_GRACE_S = 10.0
# Sustained-over-limit tolerance before the watchdog kills the child: a
# single 0.5 s poll above the rate is normal scheduling noise, persistent
# overshoot is a runaway plugin.
_CPU_OVER_LIMIT_POLLS = 3

# ---------------------------------------------------------------------------
# OS-level sandbox — policy resolved in Rust, applied per platform:
#   Windows  — kill-on-close Job Object (memory cap + CPU rate control)
#   POSIX    — own session (group kill) + worker-side RLIMIT_AS
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectExtendedLimitInformation = 9
    _JobObjectCpuRateControlInformation = 15
    _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x00000001
    _JOB_OBJECT_CPU_RATE_CONTROL_HARD_LIMIT = 0x00000004
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("CpuRate", wintypes.DWORD),
            ("ControlFlags", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE,
    ]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _windows_job_assign(
    pid: int, memory_bytes: int, cpu_rate: int
) -> int | None:
    """Create a kill-on-close Job object and assign *pid* to it.

    Returns the Job handle — the caller MUST keep it open for the lifetime
    of the child (closing it terminates the whole job) — or ``None`` when
    the OS sandbox is unavailable, in which case the software watchdog
    remains the only enforcement layer.

    ``memory_bytes`` caps the child's private committed memory (allocations
    fail beyond it; 0 = no cap).  ``cpu_rate`` is the hard CPU share as a
    fraction of total CPU in 1..=10000 (0 = no cap).  Both values come from
    the Rust sandbox policy (:func:`plugin_sandbox_resolve_limits`).
    """
    if not _IS_WINDOWS:
        return None
    process = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not process:
        return None
    try:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        assigned = False
        try:
            limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if memory_bytes > 0:
                limits.ProcessMemoryLimit = memory_bytes
                flags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            limits.BasicLimitInformation.LimitFlags = flags
            if not _kernel32.SetInformationJobObject(
                job, _JobObjectExtendedLimitInformation,
                ctypes.byref(limits), ctypes.sizeof(limits),
            ):
                return None
            if 1 <= cpu_rate <= 10000:
                control = _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(
                    CpuRate=cpu_rate,
                    ControlFlags=(_JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
                                  | _JOB_OBJECT_CPU_RATE_CONTROL_HARD_LIMIT),
                )
                # Best-effort: CPU rate control needs Windows 8+.
                _kernel32.SetInformationJobObject(
                    job, _JobObjectCpuRateControlInformation,
                    ctypes.byref(control), ctypes.sizeof(control),
                )
            assigned = bool(_kernel32.AssignProcessToJobObject(job, process))
            return job if assigned else None
        finally:
            if not assigned:
                _kernel32.CloseHandle(job)
    finally:
        _kernel32.CloseHandle(process)


def _prepare_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the child-process environment via the Rust sandbox policy.

    Sensitive variables (TOKEN/KEY/SECRET/PASSWORD/CREDENTIAL) never cross
    the plugin boundary — the filtering itself lives in the native core so
    every platform shares one implementation.
    """
    try:
        from encre import native as _native

        return dict(_native.plugin_sandbox_prepare_env(dict(extra or {})))
    except Exception as exc:
        # The native core is the authoritative filter; if it is somehow
        # unavailable, fail closed — spawn no child with a dirty environment.
        raise RuntimeError(f"plugin_sandbox_prepare_env unavailable: {exc}") from exc


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


def _skill_from_meta(meta: dict[str, Any], transport: PluginTransport) -> Any:
    """Rebuild a ``BundledSkillDefinition`` from a plugin-process metadata dict.

    The prompt renderer is a proxy that calls back into the plugin process
    (``get_skill_prompt`` RPC), so skills registered by non-local plugins
    behave like local ones instead of being stringified and dropped.
    """
    from encre.skills.types import BundledSkillDefinition, SkillContext, SkillSource

    name = meta.get("name", "")

    async def _get_prompt(args: Any = None, ctx: Any = None) -> str:
        return transport.call_rpc(
            "get_skill_prompt", {"name": name, "args": args, "context": ctx}
        )

    def _enum(enum_cls: Any, value: Any, default: Any) -> Any:
        try:
            return enum_cls(value)
        except Exception:
            return default

    return BundledSkillDefinition(
        name=name,
        description=meta.get("description", ""),
        get_prompt_for_command=_get_prompt,
        aliases=list(meta.get("aliases") or []),
        when_to_use=meta.get("when_to_use", ""),
        argument_hint=meta.get("argument_hint", ""),
        allowed_tools=meta.get("allowed_tools"),
        model=meta.get("model"),
        disable_model_invocation=bool(meta.get("disable_model_invocation", False)),
        user_invocable=bool(meta.get("user_invocable", True)),
        context=_enum(SkillContext, meta.get("context"), SkillContext.INLINE),
        source=_enum(SkillSource, meta.get("source"), SkillSource.BUNDLED),
        file_path=meta.get("file_path") or "",
        body=meta.get("body", ""),
        hidden=bool(meta.get("hidden", False)),
        auto_activate=bool(meta.get("auto_activate", False)),
    )


def _hook_from_meta(
    event_type: str, meta: dict[str, Any], transport: PluginTransport
) -> Any:
    """Build a proxy callable for a hook handler living in a plugin process."""
    handler_ref = meta.get("handler_ref", "")

    def _handler(*args: Any, **kwargs: Any) -> Any:
        return transport.call_rpc(
            "execute_hook",
            {"event_type": event_type, "args": list(args), "kwargs": kwargs},
        )

    _handler.__name__ = f"plugin_hook_{event_type}"
    _handler.__qualname__ = handler_ref or _handler.__name__
    return _handler


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

    def call_rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Invoke a method on the plugin (used by skill/hook proxies)."""
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


class _LineReader:
    """Line reader over a Rust ``SandboxSession`` byte pipe."""

    def __init__(self, session: Any, stream: str) -> None:
        self._session = session
        self._stream = stream
        self._queue: Queue[str | None] = Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _drain(self) -> bytes:
        if self._stream == "stdout":
            return self._session.drain_stdout(65536, 100)
        return self._session.drain_stderr(65536, 100)

    def _pump(self) -> None:
        pending = b""
        while not self._stop.is_set():
            try:
                chunk = self._drain()
            except Exception:
                break
            if chunk:
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    self._queue.put(line.decode("utf-8", "replace") + "\n")
                continue
            try:
                running = self._session.is_running()
            except Exception:
                running = False
            if not running:
                break
        if pending:
            self._queue.put(pending.decode("utf-8", "replace") + "\n")
        self._queue.put(None)

    def readline(self) -> str:
        try:
            value = self._queue.get()
        except Empty:
            return ""
        return value or ""

    def close(self) -> None:
        self._stop.set()


class _NativeSandboxProcess:
    """Popen-compatible handle over a Rust ``SandboxSession``.

    All spawn-time confinement (Landlock plugin profile / macOS sandbox /
    Windows Job Object), environment filtering and resource limits are owned
    by Rust.  Python only writes/reads the protocol lines.
    """

    def __init__(self, session: Any, isolation: str) -> None:
        self._session = session
        self.isolation = isolation
        self._returncode: int | None = None
        self._stdout = _LineReader(session, "stdout")
        self._stderr = _LineReader(session, "stderr")
        self.stdin = _NativeStdin(session)

    @property
    def pid(self) -> int:
        return int(self._session.pid())

    @property
    def stdout(self) -> _LineReader:
        return self._stdout

    @property
    def stderr(self) -> _LineReader:
        return self._stderr

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        try:
            code = self._session.wait(0)
        except Exception:
            return self._returncode
        if code is not None:
            self._returncode = int(code)
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        ms = 0 if timeout is None else max(0, int(timeout * 1000))
        code = self._session.wait(ms)
        if code is None:
            raise subprocess.TimeoutExpired(cmd="plugin-worker", timeout=timeout)
        self._returncode = int(code)
        return self._returncode

    def terminate(self) -> None:
        with contextlib.suppress(Exception):
            self._session.kill()
        self._returncode = -1

    def kill(self) -> None:
        self.terminate()

    def close(self) -> None:
        self.stdin.close()


class _NativeStdin:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._closed = False

    def write(self, data: str) -> int:
        if self._closed:
            raise ValueError("stdin closed")
        return int(self._session.write_stdin(data.encode("utf-8")))

    def flush(self) -> None:
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._session.close_stdin()


def _plugin_read_paths(pkg_dir: Path, grants: dict[str, Any] | None) -> list[str]:
    """Read-only roots a plugin worker legitimately needs."""
    import sysconfig

    paths: list[str] = [
        sys.prefix,
        sys.base_prefix,
        os.path.dirname(sys.executable),
        sysconfig.get_path("stdlib") or "",
        sysconfig.get_path("platstdlib") or "",
        str(pkg_dir),
    ]
    paths.extend(p for p in sys.path if p and os.path.isdir(p))
    for pattern in (grants or {}).get("file_read") or []:
        raw = str(pattern)
        candidate = Path(raw.split("*", 1)[0]).parent if "*" in raw else Path(raw)
        if candidate.is_dir():
            paths.append(str(candidate))
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p and p not in seen and os.path.exists(p):
            seen.add(p)
            out.append(p)
    return out


def _plugin_write_paths(plugin_name: str, grants: dict[str, Any] | None) -> list[str]:
    """Read-write roots: only the plugin's own state directory + declared grants."""
    paths: list[str] = []
    if plugin_name:
        from encre.plugins.state import state_path

        state_dir = state_path(plugin_name).parent
        state_dir.mkdir(parents=True, exist_ok=True)
        paths.append(str(state_dir))
    for pattern in (grants or {}).get("file_write") or []:
        raw = str(pattern)
        candidate = Path(raw.split("*", 1)[0]).parent if "*" in raw else Path(raw)
        if candidate.is_dir():
            paths.append(str(candidate))
    return paths


def _spawn_native_worker(
    argv: list[str],
    cwd: str,
    env: dict[str, str],
    pkg_dir: Path,
    plugin_name: str,
    grants: dict[str, Any] | None,
) -> _NativeSandboxProcess | None:
    """Spawn through Rust; ``None`` means this native build has no API."""
    try:
        from encre import native as _native

        cls = getattr(_native, "SandboxSession", None)
        spawn = getattr(cls, "spawn_confined", None) if cls is not None else None
        if not callable(spawn):
            return None
        session, level = spawn(
            argv,
            cwd,
            env,
            None,
            _plugin_read_paths(pkg_dir, grants),
            _plugin_write_paths(plugin_name, grants),
        )
        return _NativeSandboxProcess(session, str(level))
    except RuntimeError:
        raise
    except Exception as exc:
        logger.warning("Native plugin sandbox unavailable: %s", exc)
        return None


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
        resource_grants: dict[str, Any] | None = None,
        plugin_name: str = "",
        require_isolation: bool = False,
    ) -> None:
        self._factory_target = factory_target
        self._pkg_dir = pkg_dir
        self._plugin_name = plugin_name
        self._require_isolation = require_isolation
        self._isolation_level = "none"
        self._limits = resource_limits or {}
        # Engine resource-grant payload (compiled from the manifest by
        # ``PluginManifest.resources_payload``) forwarded to the worker at
        # activate time so the child process runs the same fail-closed
        # arbitration as the host.
        self._resource_grants = resource_grants or {}
        self._proc: Any = None
        self._seq = 0
        self._tool_cache: list[ToolMetadata] | None = None
        self._skill_cache: list[dict[str, Any]] | None = None
        self._hook_cache: list[tuple[str, dict[str, Any]]] | None = None
        # Resource watchdog thread (S1).
        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._watchdog_killed = False
        # Windows Job object handle for the OS-level sandbox.  Kept open
        # for the lifetime of the child: closing it kills the whole job.
        self._job_handle: int | None = None
        # Resolved limits from the Rust sandbox policy, computed lazily
        # (memory_bytes, cpu_rate, cpu_fraction).
        self._resolved: tuple[int, int, float] | None = None
        # Serialises concurrent JSON-RPC calls over the single stdio pipe.
        self._rpc_lock = threading.Lock()
        # Lifecycle bookkeeping for crash recovery: whether the child was
        # activated (so a respawned worker can replay the activate RPC),
        # whether _ensure_process just respawned a fresh child, and when
        # the current child was spawned (CPU-check grace window).
        self._was_activated = False
        self._respawned = False
        self._spawn_time = 0.0

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def _resolve_limits(self) -> tuple[int, int, float]:
        """Resolve manifest limits via the Rust sandbox policy (cached).

        Returns ``(memory_bytes, cpu_rate, cpu_fraction)``.  The native core
        is the single source of truth for what the declared limits mean on
        this platform; fail-closed — a missing native core aborts the spawn
        instead of degrading to unrestricted resources.
        """
        if self._resolved is None:
            memory_mb = self._limits.get("memory_mb")
            cpu = self._limits.get("cpu")
            try:
                from encre import native as _native

                self._resolved = tuple(
                    _native.plugin_sandbox_resolve_limits(
                        float(memory_mb) if memory_mb is not None else None,
                        float(cpu) if cpu is not None else None,
                    )
                )
            except Exception as exc:
                raise RuntimeError(
                    f"plugin_sandbox_resolve_limits unavailable for "
                    f"'{self._plugin_name}': {exc}"
                ) from exc
        return self._resolved

    def _close_job_handle(self) -> None:
        """Close the Job handle — on Windows this terminates the child tree
        (``KILL_ON_JOB_CLOSE``), a stronger guarantee than a plain kill."""
        job, self._job_handle = self._job_handle, None
        if job is not None and _IS_WINDOWS:
            with contextlib.suppress(Exception):
                _kernel32.CloseHandle(job)

    def _kill_process_tree(self) -> None:
        """Terminate the whole child tree on every platform.

        Windows: closing the Job handle fires ``KILL_ON_JOB_CLOSE`` (covers
        grandchildren).  POSIX: the child runs in its own session (see
        ``start_new_session``), so ``killpg`` reaches the entire group.
        """
        proc = self._proc
        if proc is None:
            return
        if isinstance(proc, _NativeSandboxProcess):
            proc.kill()
            return
        if _IS_WINDOWS:
            self._close_job_handle()
        else:
            with contextlib.suppress(Exception):
                os.killpg(proc.pid, signal.SIGKILL)

    def _apply_os_sandbox(self) -> None:
        """Best-effort OS-level sandbox around the freshly spawned child.

        Windows: assign the child to a kill-on-close Job object with the
        Rust-resolved memory cap and CPU rate.  POSIX: the child already
        self-applied ``RLIMIT_AS`` in the worker (value computed by Rust
        and passed through the environment) and runs in its own session
        for group-kill containment.  Failure here only degrades to the
        software watchdog — it never blocks the spawn.
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        if _IS_WINDOWS:
            memory_bytes, cpu_rate, _cpu_fraction = self._resolve_limits()
            job = _windows_job_assign(proc.pid, memory_bytes, cpu_rate)
            if job is None:
                logger.debug(
                    "Job-object sandbox unavailable for plugin '%s'; "
                    "software watchdog only",
                    self._plugin_name,
                )
            self._job_handle = job

    def _ensure_process(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        # A previous child died naturally: drop its stale Job handle.
        self._close_job_handle()
        # S3: the sensitive-variable filter is Rust policy, shared across
        # all platforms.  Failing closed means we never spawn with secrets.
        env = _prepare_env({
            "ENCRE_PLUGIN_FACTORY": self._factory_target,
        })
        if self._plugin_name:
            from encre.plugins.state import state_path

            env["ENCRE_PLUGIN_STATE_PATH"] = str(state_path(self._plugin_name))
        if not _IS_WINDOWS:
            # POSIX sandbox: the worker self-applies RLIMIT_AS before any
            # plugin code runs; the byte cap is resolved by Rust.
            memory_bytes = self._resolve_limits()[0]
            if memory_bytes > 0:
                env["ENCRE_PLUGIN_MEMORY_BYTES"] = str(memory_bytes)
        pkg_dir_str = str(self._pkg_dir)
        pythonpath = env.get("PYTHONPATH", "")
        if pkg_dir_str not in pythonpath:
            env["PYTHONPATH"] = pkg_dir_str + os.pathsep + pythonpath
        try:
            native = _spawn_native_worker(
                [sys.executable, str(_WORKER_SCRIPT)],
                str(self._pkg_dir),
                env,
                self._pkg_dir,
                self._plugin_name,
                self._resource_grants,
            )
            if native is not None:
                if self._require_isolation and native.isolation == "none":
                    native.kill()
                    raise RuntimeError("native sandbox reported no isolation")
                self._proc = native
                self._isolation_level = native.isolation
                logger.info("plugin '%s' worker isolation: %s", self._plugin_name, native.isolation)
            else:
                if self._require_isolation:
                    raise RuntimeError(
                        "strong isolation required but this native build has no SandboxSession.spawn_confined"
                    )
                kwargs: dict[str, Any] = {}
                if not _IS_WINDOWS:
                    kwargs["start_new_session"] = True
                self._proc = subprocess.Popen(
                    [sys.executable, str(_WORKER_SCRIPT)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    text=True,
                    bufsize=1,
                    **kwargs,
                )
                self._isolation_level = "job_object" if _IS_WINDOWS else "none"
            self._respawned = True
            self._spawn_time = time.monotonic()
        except Exception as exc:
            logger.error("Failed to spawn plugin subprocess: %s", exc)
            self._proc = None
            return False
        if self._proc.poll() is None and not isinstance(self._proc, _NativeSandboxProcess):
            self._apply_os_sandbox()
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

    def _rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_override: float | None = None,
    ) -> Any:
        """Send a JSON-RPC request and read the response with a timeout.

        ``timeout_override`` shrinks the wait for lifecycle RPCs (e.g. a
        hung ``deactivate`` must not stall uninstall for the full tool
        timeout — the process is killed right afterwards anyway).
        """
        with self._rpc_lock:
            return self._rpc_locked(method, params, timeout_override)

    def _rpc_locked(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_override: float | None = None,
    ) -> Any:
        """RPC implementation; caller must hold :attr:`_rpc_lock`.

        Re-entrant by design (no re-locking): respawn recovery replays the
        ``activate`` RPC through this method while already inside the lock.
        """
        if not self._ensure_process():
            raise RuntimeError("Plugin subprocess not running")
        if self._respawned and self._was_activated:
            # The previous child died (crash / timeout kill).  A fresh
            # worker starts unactivated, so replay the activation RPC;
            # otherwise every subsequent tool call fails with "Tool not
            # found" after the reconnect.
            self._respawned = False
            with contextlib.suppress(Exception):
                self._rpc_locked("activate", {"context": {}})
        if timeout_override is not None:
            timeout = float(timeout_override)
        else:
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
        consecutive_cpu_over = 0
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
                # CPU check (averaged over the poll interval).  A short
                # grace window after spawn skips the interpreter's import
                # spike, and only *sustained* overshoot kills — single
                # polls above the rate are scheduling noise, and killing
                # on them made CPU-capped plugins die right after connect.
                if cpu_limit_percent is not None:
                    if time.monotonic() - self._spawn_time < _CPU_GRACE_S:
                        continue
                    cpu_pct = proc.cpu_percent(interval=_WATCHDOG_INTERVAL)
                    if cpu_pct > cpu_limit_percent:
                        consecutive_cpu_over += 1
                        if consecutive_cpu_over < _CPU_OVER_LIMIT_POLLS:
                            continue
                        logger.warning(
                            "Plugin '%s' exceeded CPU limit: %.1f%% > %.1f%%; killing",
                            self._plugin_name, cpu_pct, cpu_limit_percent,
                        )
                        self._kill_process(reason="CPU limit exceeded")
                        return
                    consecutive_cpu_over = 0
            except psutil.NoSuchProcess:
                return
            except Exception:
                logger.debug("Watchdog poll error", exc_info=True)

    def _kill_process(self, reason: str = "") -> None:
        """Force-kill the subprocess and mark it as watchdog-killed."""
        self._watchdog_killed = True
        # Tree-kill first (Windows Job close / POSIX group kill), then the
        # plain kill as belt-and-braces for the direct child.
        self._kill_process_tree()
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
        result = self._rpc(
            "activate",
            {
                "context": context_snapshot or {},
                # Deploy the manifest resource grants into the child's own
                # permission-engine instance (every process runs one) and
                # name the plugin so its controlled ``resources`` API
                # arbitrates identically to a local plugin.
                "plugin": self._plugin_name,
                "resources": self._resource_grants,
            },
        )
        if isinstance(result, dict):
            if result.get("ok") is False:
                return False
            unsupported = result.get("unsupported") or []
            if unsupported:
                raise RuntimeError(
                    "plugin used surfaces that cannot cross the process boundary: "
                    + ", ".join(str(s) for s in unsupported)
                )
        if bool(result):
            # Remember activation so a respawned worker (crash / timeout
            # kill recovery) can replay it transparently.
            self._was_activated = True
            self._respawned = False
        return bool(result)

    def deactivate(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            # Short timeout: a hung on_deactivate must not stall uninstall
            # or disable for the full tool timeout — the child is killed
            # right after by close() anyway.
            with contextlib.suppress(Exception):
                self._rpc(
                    "deactivate",
                    timeout_override=min(float(self._limits.get("timeout_s", 30.0)), 5.0),
                )
        self._was_activated = False
        self._respawned = False
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
        raw = self._rpc("get_skills") or []
        self._skill_cache = [_skill_from_meta(m, self) for m in raw]
        return self._skill_cache

    def get_hook_metas(self) -> list[tuple[str, dict[str, Any]]]:
        if self._hook_cache is not None:
            return self._hook_cache
        raw = self._rpc("get_hooks") or []
        self._hook_cache = [
            (h[0], {"handler": _hook_from_meta(h[0], h[1], self)}) for h in raw
        ]
        return self._hook_cache

    def call_rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return self._rpc(method, params)

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
        # Tree-kill on every platform: Windows Job close (KILL_ON_JOB_CLOSE)
        # and POSIX process-group kill both reach grandchildren, which a
        # plain TerminateProcess/kill on the child would leave running.
        self._kill_process_tree()
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
        self._respawned = False


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

    def __init__(
        self,
        endpoint: str,
        plugin_name: str = "",
        auth_token: str = "",
        timeout_s: float = 30.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._plugin_name = plugin_name
        self._auth_token = auth_token or os.environ.get("ENCRE_REMOTE_PLUGIN_TOKEN", "")
        self._timeout_s = timeout_s
        self._tool_cache: list[ToolMetadata] | None = None
        self._activated = False

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    def _post(self, method: str, params: dict[str, Any] | None = None) -> Any:
        import httpx
        url = f"{self._endpoint}/{method}"
        try:
            resp = httpx.post(
                url, json=params or {}, timeout=self._timeout_s, headers=self._headers()
            )
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
        raw = self._post("get_skills") or []
        return [_skill_from_meta(m, self) for m in raw]

    def get_hook_metas(self) -> list[tuple[str, dict[str, Any]]]:
        raw = self._post("get_hooks") or []
        return [(h[0], {"handler": _hook_from_meta(h[0], h[1], self)}) for h in raw]

    def call_rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return self._post(method, params)

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


def create_transport(manifest: Any, plugin: Any = None, pkg_dir: Path | None = None, mode: str | None = None) -> PluginTransport:
    """Select and instantiate the appropriate transport for a plugin.

    *mode* overrides the manifest value (used by the registry's strong-
    isolation default for untrusted tiers).  An untrusted plugin forced to
    ``process`` without a resolvable factory fails instead of silently
    falling back to an in-process execution.
    """
    mode = mode or getattr(manifest, "transport", "local")
    plugin_name = getattr(manifest, "name", "")
    forced = mode != getattr(manifest, "transport", "local")
    if mode == "local" or plugin is None:
        return LocalTransport(plugin)
    if mode == "process":
        # Factory target: manifest.factory > derive from plugin module > endpoint
        factory_target = getattr(manifest, "factory", "")
        if not factory_target and plugin is not None:
            mod_name = getattr(type(plugin), "__module__", "") or getattr(plugin, "__module__", "")
            mod = sys.modules.get(mod_name) if mod_name else None
            if mod is not None and callable(getattr(mod, "create_plugin", None)):
                factory_target = f"{mod_name}:create_plugin"
        if not factory_target:
            factory_target = getattr(manifest, "endpoint", "")
        if not factory_target:
            if forced:
                raise RuntimeError("untrusted plugin requires process transport but declares no factory target")
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
            resource_grants=json.loads(manifest.resources_payload()),
            plugin_name=plugin_name,
            require_isolation=forced,
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
