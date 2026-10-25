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

"""Sub-agent bundles: external agent CLIs exposed as first-class tools (P1.4).

A :class:`SubAgentBundle` wraps an EXTERNAL agent CLI (``claude -p ...``,
``codex exec ...``, ...) behind the ordinary tool contract
(:meth:`~encre.tools.base.build_tool`: ``name`` / ``description`` /
``input_schema`` / ``execute``) so the loop's regular tool pipeline can invoke
it like any built-in tool.  The parent loop is blocked on the tool call while
the external agent runs, so **the tool result IS the completion wakeup** --
there is deliberately no background-task / polling system here: the
tool-call model already sequences execution, and
:meth:`~encre.events.ledger.EventSourcedBus.publish` fans ``subagent_started``
/ ``subagent_finished`` out to observers (UI, telemetry) synchronously the
moment each happens.

Execution policy (all of it deliberate):

* **argv only, never a shell.**  The command is a ``list[str]`` template and
  is spawned with :func:`asyncio.create_subprocess_exec`.  The prompt is
  substituted into the argv element holding ``{prompt}`` (or appended as the
  final argument when the template has no placeholder) -- as ONE argv element.
  Shell metacharacters in the prompt are inert data; prompt content cannot
  spawn additional commands.
* **Minimal environment.**  The child never inherits the parent's full
  environment.  Only a platform-specific base set (PATH, HOME/USERPROFILE,
  TEMP/TMP, SYSTEMROOT/COMSPEC on Windows -- without which Windows Python
  cannot even start) plus the caller's ``env_allowlist`` is forwarded.  API
  keys and other secrets in the parent environment are therefore invisible to
  the external CLI.
* **Hard timeout with tree kill.**  On expiry the whole process group is
  killed (POSIX: ``killpg``; Windows: ``taskkill /F /T``) -- no orphaned
  agent processes survive the tool result.
* **stderr only on failure.**  Success returns the CLI's stdout (truncated to
  a sane cap with a marker); failures surface the exit code and stderr.
* **Binary guard.**  The bundle refuses to run (clear ``Error`` result, no
  cryptic traceback) when the command binary does not exist on PATH.
* **Permission gated twice.**  The tool goes through the normal executor
  path, so the Rust permission engine gates it like any tool; additionally
  the binary guard lives inside the bundle itself.
"""


import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

from encre.logging_config import get_logger

logger = get_logger(__name__)

__all__ = [
    "SubAgentBundle",
    "register_bundle",
    "register_subagent_bundle",
]

#: Default hard timeout for one external-agent run (seconds).
DEFAULT_TIMEOUT_S: float = 600.0

#: Default cap on the tool-result size; longer output is truncated with a
#: marker so the conversation budget is not consumed by a chatty CLI.
DEFAULT_MAX_OUTPUT_CHARS: int = 20_000

#: Placeholder token replaced with the prompt inside the command template.
PROMPT_PLACEHOLDER: str = "{prompt}"

#: Result marker appended when the captured output was truncated.
_TRUNCATION_MARKER = "\n... [output truncated by subagent bundle]"

# ---------------------------------------------------------------------------
# Minimal child environment
# ---------------------------------------------------------------------------
# The parent's environment routinely holds API keys / tokens / credentials.
# An external agent CLI is untrusted code from the parent's perspective, so it
# gets only the variables it needs to function:
#
# * Windows: ``SYSTEMROOT`` is mandatory for Windows Python to initialise
#   (crypto + encodings); ``COMSPEC``/``PATHEXT`` are what ``cmd /c`` style
#   CLIs and executable resolution rely on; ``USERPROFILE``/``APPDATA``/
#   ``LOCALAPPDATA``/``TEMP`` are where agent CLIs look for their config and
#   caches.
# * POSIX: ``HOME`` (config dirs), ``PATH``, ``TMPDIR``, ``LANG`` (UTF-8 IO).
_BASE_ENV_KEYS_WINDOWS: tuple[str, ...] = (
    "APPDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATH",
    "PATHEXT", "PROGRAMDATA", "PROGRAMFILES", "SYSTEMDRIVE", "SYSTEMROOT",
    "TEMP", "TMP", "USERDOMAIN", "USERNAME", "USERPROFILE", "WINDIR",
)
_BASE_ENV_KEYS_POSIX: tuple[str, ...] = ("HOME", "LANG", "PATH", "TMPDIR")

_IS_WINDOWS = sys.platform == "win32"


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill ``proc`` and (best effort) its whole process tree.

    POSIX: the child was started with ``start_new_session=True`` so it leads
    its own process group -- ``killpg`` takes down spawned grandchildren.
    Windows: ``taskkill /F /T /PID`` walks the tree; the direct ``kill()``
    afterwards is a belt-and-braces fallback.  Every failure is swallowed:
    a kill racing an already-dead process must not raise.
    """
    if _IS_WINDOWS:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10, check=False,
            )
        with contextlib.suppress(Exception):
            proc.kill()
        return
    with contextlib.suppress(Exception):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()


class SubAgentBundle:
    """Turns an external agent CLI into a normal tool in the ToolRegistry.

    Args:
        name: Tool name (must be unique in the target registry).
        command: argv template, e.g. ``["claude", "-p", "{prompt}"]``.  Every
            element containing ``{prompt}`` is substituted with the prompt
            (kept as a single argv element -- never re-parsed by a shell).
            When no element contains the placeholder, the prompt is appended
            as the final argument.
        description: Prompt-facing tool description.
        timeout_s: Hard wall-clock timeout for one run (default 600s).  On
            expiry the process tree is killed and an ``Error`` result with the
            elapsed time is returned.
        cwd: Working directory for the child.  Default: the session workspace
            (``session.metadata["workspace"]``) when a session is attached and
            carries one, otherwise the parent's current working directory.
        env_allowlist: Extra environment variable names (beyond the minimal
            base set) passed through to the child.
        session: Optional session object.  The event bus is read lazily from
            ``session.event_bus`` at execute time, so a session wired later
            (loop construction attaches the bus after the fact) still works.
            When ``None`` nothing is published.
        bus: Explicit :class:`~encre.events.ledger.EventSourcedBus` override;
            takes precedence over ``session.event_bus`` (used by embedders and
            tests without a session).
        max_output_chars: Cap on the captured stdout returned as the tool
            result; longer output is truncated with a marker.
    """

    def __init__(
        self,
        name: str,
        command: list[str],
        description: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        cwd: str = "",
        env_allowlist: list[str] | None = None,
        session: Any = None,
        bus: Any = None,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> None:
        if not name or not str(name).strip():
            raise ValueError("SubAgentBundle: 'name' must be a non-empty string")
        if not command or not all(isinstance(part, str) for part in command):
            raise ValueError(
                "SubAgentBundle: 'command' must be a non-empty list[str] (argv template)"
            )
        self.name = str(name).strip()
        self.command = [str(part) for part in command]
        self.description = str(description or "").strip() or (
            f"Delegate a task to the '{self.name}' external agent and return its final answer."
        )
        self.timeout_s = float(timeout_s) if timeout_s and float(timeout_s) > 0 else DEFAULT_TIMEOUT_S
        self.cwd = str(cwd or "")
        self.env_allowlist = [str(k) for k in (env_allowlist or [])]
        self.session = session
        self.bus = bus
        self.max_output_chars = int(max_output_chars)

    # ------------------------------------------------------------------
    # Tool construction
    # ------------------------------------------------------------------

    def _input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The complete task/prompt handed to the external agent.",
                },
                "task_description": {
                    "type": "string",
                    "description": "Optional short human-readable label for this delegation.",
                },
            },
            "required": ["prompt"],
        }

    def to_tool(self) -> Any:
        """Build the real :class:`~encre.tools.base.EncreTool` for this bundle."""
        from encre.tools.base import build_tool

        bundle = self

        async def _execute(
            prompt: str,
            task_description: str = "",
            _session_id: str = "",
            **_extra: Any,
        ) -> str:
            _ = task_description  # accepted for schema completeness; label only
            return await bundle.run(prompt, _session_id=_session_id)

        return build_tool(
            name=self.name,
            description=self.description,
            input_schema=self._input_schema(),
            execute=_execute,
            category="subagent",
            max_result_size_chars=self.max_output_chars,
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _event_bus(self) -> Any:
        """Return the bus to publish on, or ``None`` (publish only if present)."""
        if self.bus is not None:
            return self.bus
        session = self.session
        if session is None:
            return None
        with contextlib.suppress(Exception):
            bus = getattr(session, "event_bus", None)
            if bus is not None:
                return bus
        return None

    def _publish(self, kind: str, payload: dict[str, Any], session_id: str) -> None:
        bus = self._event_bus()
        if bus is None:
            return
        try:
            bus.publish(kind, session_id=session_id, payload=payload)
        except Exception:
            logger.warning("[subagent_bundle] failed to publish %s", kind, exc_info=True)

    def _build_argv(self, prompt: str) -> list[str]:
        """Substitute the prompt into the template, as raw argv elements only."""
        substituted = [
            part.replace(PROMPT_PLACEHOLDER, prompt)
            if PROMPT_PLACEHOLDER in part else part
            for part in self.command
        ]
        if not any(PROMPT_PLACEHOLDER in part for part in self.command):
            # Template without a placeholder: append the prompt as the final
            # argument (still one argv element -- no shell involved).
            substituted.append(prompt)
        return substituted

    def _build_env(self) -> dict[str, str]:
        """Minimal child environment: base set + allowlist, nothing else."""
        base_keys = _BASE_ENV_KEYS_WINDOWS if _IS_WINDOWS else _BASE_ENV_KEYS_POSIX
        wanted = list(base_keys) + [k for k in self.env_allowlist if k not in base_keys]
        return {
            key: os.environ[key]
            for key in wanted
            if key in os.environ
        }

    def _resolve_cwd(self) -> str:
        """Explicit ``cwd`` > session workspace > the parent's cwd."""
        if self.cwd:
            return self.cwd
        with contextlib.suppress(Exception):
            ws = str((getattr(self.session, "metadata", None) or {}).get("workspace", "") or "")
            if ws:
                return ws
        return os.getcwd()

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_output_chars:
            return text
        return text[: self.max_output_chars].rstrip() + _TRUNCATION_MARKER

    async def run(self, prompt: str, _session_id: str = "") -> str:
        """Run the external agent for ``prompt`` and return the tool result.

        Returns the CLI's stdout on success; on any failure returns an
        ``Error: ...`` string (the executor convention) carrying the exit code
        and stderr.  Publishes ``subagent_started`` / ``subagent_finished``
        on the event bus when one is available.
        """
        prompt = "" if prompt is None else str(prompt)
        argv = self._build_argv(prompt)

        # Binary guard: refuse with a clear message instead of a cryptic
        # FileNotFoundError traceback (or a shell 'not recognized' hop).
        binary = shutil.which(argv[0])
        if binary is None:
            return self._finish_failure(
                _session_id, pid=0, started_at=time.monotonic(),
                message=(
                    f"Error: Sub-agent binary not found: '{argv[0]}'. "
                    f"Install it or adjust the bundle command."
                ),
                exit_code=None, timed_out=False, started=False,
            )

        env = self._build_env()
        cwd = self._resolve_cwd()
        popen_kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "stdin": asyncio.subprocess.DEVNULL,
            "cwd": cwd,
            "env": env,
        }
        if _IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        started_at = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(*argv, **popen_kwargs)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return self._finish_failure(
                _session_id, pid=0, started_at=started_at,
                message=f"Error: Failed to launch sub-agent '{self.name}' ({argv[0]}): {exc}",
                exit_code=None, timed_out=False, started=False,
            )

        self._publish(
            "subagent_started",
            {"name": self.name, "pid": proc.pid, "command": argv[0]},
            _session_id,
        )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            timed_out = True
            _kill_process_tree(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=10)
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            self._publish(
                "subagent_finished",
                {
                    "name": self.name, "pid": proc.pid,
                    "duration_ms": elapsed_ms, "exit_code": None,
                    "ok": False, "timed_out": True,
                },
                _session_id,
            )
            logger.warning(
                "[subagent_bundle] %s timed out after %.1fs pid=%s (process tree killed)",
                self.name, self.timeout_s, proc.pid,
            )
            return (
                f"Error: Sub-agent '{self.name}' timed out after {self.timeout_s:g}s "
                f"and was killed (pid={proc.pid})."
            )
        except asyncio.CancelledError:
            # The parent loop was cancelled -- take the child tree with us.
            _kill_process_tree(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=10)
            raise

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = proc.returncode
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        ok = exit_code == 0

        self._publish(
            "subagent_finished",
            {
                "name": self.name, "pid": proc.pid,
                "duration_ms": elapsed_ms, "exit_code": exit_code,
                "ok": ok, "timed_out": False,
            },
            _session_id,
        )

        if ok:
            return self._truncate(stdout.strip())

        # Failure: surface the exit code and stderr (stdout kept for context).
        detail = stderr.strip() or stdout.strip() or "(no output)"
        return (
            f"Error: Sub-agent '{self.name}' failed with exit code {exit_code}.\n"
            f"--- stderr ---\n{self._truncate(detail)}"
        )

    def _finish_failure(
        self,
        session_id: str,
        *,
        pid: int,
        started_at: float,
        message: str,
        exit_code: int | None,
        timed_out: bool,
        started: bool,
    ) -> str:
        """Publish ``subagent_finished`` for a pre/post-spawn failure and return ``message``."""
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        self._publish(
            "subagent_finished",
            {
                "name": self.name, "pid": pid,
                "duration_ms": elapsed_ms, "exit_code": exit_code,
                "ok": False, "timed_out": timed_out,
            },
            session_id,
        )
        # ``started`` is False when the guard/launch failed before a process
        # existed -- no ``subagent_started`` is published for those runs.
        _ = started
        logger.warning("[subagent_bundle] %s failed: %s", self.name, message)
        return message


# ---------------------------------------------------------------------------
# Registration helpers (consistent with the P1.2 adapter-registration API)
# ---------------------------------------------------------------------------

def register_bundle(registry: Any, bundle: SubAgentBundle) -> Any:
    """Register ``bundle``'s tool on ``registry`` and return the tool.

    The bundle is validated through the registry's own tool contract
    (``name`` / ``description`` / ``input_schema`` / ``execute``), so a bad
    bundle fails loudly at registration time.
    """
    tool = bundle.to_tool()
    registry.register(tool)
    return tool


def register_subagent_bundle(
    tool_registry: Any,
    *,
    name: str,
    command: list[str],
    description: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    cwd: str = "",
    env_allowlist: list[str] | None = None,
    session: Any = None,
    bus: Any = None,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> tuple[SubAgentBundle, Any]:
    """One-call convenience: build a :class:`SubAgentBundle`, register its tool.

    Returns ``(bundle, tool)`` so callers keep a handle to the bundle for
    later tuning (timeout, session wiring) or unregistration via
    ``tool_registry.unregister(name)``.
    """
    bundle = SubAgentBundle(
        name=name,
        command=command,
        description=description,
        timeout_s=timeout_s,
        cwd=cwd,
        env_allowlist=env_allowlist,
        session=session,
        bus=bus,
        max_output_chars=max_output_chars,
    )
    return bundle, register_bundle(tool_registry, bundle)
