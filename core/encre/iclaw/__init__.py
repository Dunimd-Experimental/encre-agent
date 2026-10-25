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

"""iClaw background daemon -- core-server launcher profile.

The former ``IClawEngine`` re-assembled sessions, event routing, the WS
transport, scheduler, hooks, safety, and its own ``PluginRegistry``
bootstrap -- all of which the core server already provides.  That engine
is gone.  The daemon now boots through the core server launcher
(:class:`encre.server.app.EncreServer`) and layers the iClaw
self-improvement add-ons via :class:`encre.iclaw.profile.IclawProfile`.

What remains here is the daemon shell: PID/log lifecycle, the
``ICLAW_READY`` stdout handshake consumed by parent processes, and the
top-level start/wait/stop orchestration.  Background daemon behaviour
(pid file, log file, ``--stop`` / ``--status``) is unchanged.

Position in Encre: iClaw is the autonomous agent mode, the counterpart of
the gateway mode in :mod:`encre.gateway`.  Where the gateway bridges
third-party IM platforms to the Rust agent core, iClaw runs that same core
as a long-lived background process on a local WebSocket and layers the
self-improvement subsystems (evolution, feedback, memory consolidation,
soul learning) on top of it.  Desktop UI and headless drivers reach it
through the same WS endpoint the gateway does not use.

Public API:
    * :func:`run_iclaw` -- async entry point for embedding the daemon.
    * :class:`IClawDaemon` -- object form of the same lifecycle.
    * :func:`is_running`, :func:`stop_daemon` -- control-plane helpers.
    * :class:`IclawProfile`, :class:`DaemonStats` -- re-exported from
      :mod:`encre.iclaw.profile`.
"""

import asyncio
import contextlib
import logging
import os
import signal
import time
from dataclasses import dataclass  # noqa: F401  (re-exported for compat)
from pathlib import Path
from typing import Any

from encre.agent import EncreAgent
from encre.config import EncreConfig, get_data_dir
from encre.iclaw.profile import DaemonStats, IclawProfile, _make_agent_factory
from encre.logging_config import get_logger
from encre.protocol.session_router import EventRouter
from encre.server.app import EncreServer

logger = get_logger("encre.iclaw")

__all__ = [
    "DaemonStats",
    "IClawDaemon",
    "IclawProfile",
    "is_running",
    "run_iclaw",
    "stop_daemon",
]

_PID_FILE = "iclaw.pid"
_LOG_FILE = "iclaw.log"


def _data_dir() -> Path:
    """Return the Encre data directory used for daemon state files."""
    # Centralise all pid/log/state files under the Encre data directory.
    return get_data_dir()


def _pid_path() -> Path:
    """Return the path to the daemon PID file."""
    return _data_dir() / _PID_FILE


def _log_path() -> Path:
    """Return the path to the daemon log file."""
    return _data_dir() / _LOG_FILE


def _write_pid(pid: int) -> None:
    """Persist the running daemon's process id to the PID file."""
    _data_dir().mkdir(parents=True, exist_ok=True)
    _pid_path().write_text(str(pid))


def _clear_pid() -> None:
    """Remove the PID file, ignoring any error (e.g. already gone)."""
    with contextlib.suppress(Exception):
        _pid_path().unlink(missing_ok=True)


def _read_pid() -> int | None:
    """Read the daemon PID from disk, or ``None`` if absent/unreadable."""
    try:
        return int(_pid_path().read_text().strip())
    except Exception:
        return None


def is_running() -> bool:
    """Return ``True`` if a daemon PID file points to a live process."""
    pid = _read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        _clear_pid()
        return False


def stop_daemon() -> bool:
    """Send SIGTERM to the running daemon; return ``True`` if signalled."""
    pid = _read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    _clear_pid()
    return True


async def run_iclaw(
    agent: EncreAgent | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 18791,
    max_concurrent: int = 20,
    consolidation_interval: int = 3600,
    scheduler_poll_interval: float = 30.0,
    enable_evolution: bool = True,
    enable_reflex: bool = True,
    enable_metacognition: bool = True,
    enable_feedback: bool = True,
    enable_swarm: bool = True,
    enable_compact: bool = True,
    enable_hooks: bool = True,
) -> None:
    """Run the iClaw daemon through the core server launcher.

    Boots an :class:`EncreServer` (transport + sessions + scheduler),
    attaches the :class:`IclawProfile` add-ons, prints ``ICLAW_READY`` for
    the parent process, then serves until cancelled.

    Args:
        agent: An existing :class:`encre.agent.EncreAgent`.  When ``None`` a
            fresh agent with default :class:`EncreConfig` is created here.
            Passing one in is how a host application shares its own model and
            settings with the daemon.
        host: Bind address for the daemon WebSocket server.
        port: Bind port.  The same value is announced in the ``ICLAW_READY``
            line, so it must be the address a parent process should connect to.
        max_concurrent: Upper bound on concurrent agent runs.  Shared by the
            server and the profile so neither can admit a run the other will
            not service.
        consolidation_interval: Seconds between memory-consolidation passes.
        scheduler_poll_interval: Poll cadence passed through for scheduling.
        enable_evolution: Enable the evolution learner.
        enable_reflex: Enable reflexive self-review.
        enable_metacognition: Enable the metacognition subsystem.
        enable_feedback: Enable the feedback learner.
        enable_swarm: Enable swarm-style task splitting.
        enable_compact: Enable the context compaction engine on the profile.
        enable_hooks: Enable turn-start/turn-end hook emission on the profile.

    Returns:
        None.  The coroutine runs until cancelled, so callers await it inside
        a task they can cancel to shut the daemon down.

    Note:
        The PID file is written before the server starts, so its presence
        means "a daemon is booting or running", not "it is serving".  It is
        cleared in the final ``finally`` block, so a crash anywhere inside the
        coroutine removes it as long as the event loop is still alive.
    """
    _write_pid(os.getpid())
    try:
        if agent is None:
            agent = EncreAgent(config=EncreConfig())

        # Core launcher owns transport, sessions, and the scheduler.
        server = EncreServer(
            host=host,
            port=port,
            config=agent.config,
            max_concurrent=max_concurrent,
        )
        await server.start()

        # iClaw add-ons: self-improvement + autonomy subsystems.
        router = EventRouter(
            session_manager=server.session_manager,
            default_config=agent.config,
        )
        profile = IclawProfile(
            agent,
            router=router,
            scheduler=server.scheduler,
            max_concurrent=max_concurrent,
            consolidation_interval=consolidation_interval,
            enable_evolution=enable_evolution,
            enable_reflex=enable_reflex,
            enable_metacognition=enable_metacognition,
            enable_feedback=enable_feedback,
            enable_swarm=enable_swarm,
            enable_compact=enable_compact,
            enable_hooks=enable_hooks,
        )
        await profile.start()

        # Handshake for parent processes parsing stdout.
        print(f"ICLAW_READY ws://{host}:{port}/ws", flush=True)

        try:
            await asyncio.Event().wait()  # run until cancelled
        except asyncio.CancelledError:
            pass
        finally:
            await profile.stop()
            await server.stop()
    finally:
        _clear_pid()


class IClawDaemon:
    """Headless background daemon managed by the Encre Agent Desktop.

    Thin lifecycle shell around :func:`run_iclaw`: wraps the core server
    launcher plus the iClaw profile with PID management and start/wait/stop
    orchestration.  Parent processes spawn this as a child process, parse
    the ``ICLAW_READY`` line from stdout, and connect to the WS endpoint.

    Invariant: ``self.profile`` is ``None`` until :meth:`start` completes and
    stays set until :meth:`stop`.  Every read method treats ``None`` as the
    "never started" or "already stopped" state and returns a neutral value
    instead of raising, so the desktop UI can poll health without coordinating
    shutdown.

    Note:
        This class clears the PID file but never writes it.  A caller that
        drives :class:`IClawDaemon` directly (rather than
        :func:`run_iclaw`) is responsible for writing it, otherwise
        :func:`is_running` reports the daemon as down.
    """

    def __init__(
        self,
        agent: EncreAgent | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 18791,
        max_concurrent: int = 20,
        consolidation_interval: int = 3600,
        scheduler_poll_interval: float = 30.0,
        enable_evolution: bool = True,
        enable_reflex: bool = True,
        enable_metacognition: bool = True,
        enable_feedback: bool = True,
        enable_swarm: bool = True,
        enable_compact: bool = True,
        enable_hooks: bool = True,
    ) -> None:
        """Record daemon launch parameters without starting anything.

        Construction is deliberately inert: the core server and profile are only
        built inside :meth:`start`, so a caller may assemble an
        :class:`IClawDaemon` with non-default host, port, or subsystem toggles
        and defer boot. Every argument here is captured verbatim and handed to
        :class:`encre.server.app.EncreServer` or
        :class:`encre.iclaw.profile.IclawProfile` at start time.

        Args:
            agent: Pre-built :class:`encre.agent.EncreAgent` to reuse. When
                omitted, :meth:`start` constructs one from a fresh
                :class:`encre.config.EncreConfig`.
            host: Bind address for the daemon's WebSocket endpoint.
            port: Bind port for the WebSocket endpoint advertised in the
                ``ICLAW_READY`` line.
            max_concurrent: Session concurrency limit passed to the core server.
            consolidation_interval: Period, in seconds, between memory
                consolidation passes on the profile.
            scheduler_poll_interval: Poll cadence, in seconds, for the cron
                scheduler owned by the core server.
            enable_evolution: Enable the evolution learning subsystem.
            enable_reflex: Enable reflex action capture.
            enable_metacognition: Enable metacognitive self-review.
            enable_feedback: Enable feedback learning.
            enable_swarm: Enable swarm delegation.
            enable_compact: Enable context compaction.
            enable_hooks: Enable the hook pipeline.

        Invariant:
            ``self.profile`` is ``None`` on return and ``self.started_at`` is
            ``0.0``; both are set only by :meth:`start`.
        """
        self._agent = agent
        self._host = host
        self._port = port
        self._max_concurrent = max_concurrent
        self._consolidation_interval = consolidation_interval
        self._scheduler_poll_interval = scheduler_poll_interval
        self._enable_evolution = enable_evolution
        self._enable_reflex = enable_reflex
        self._enable_metacognition = enable_metacognition
        self._enable_feedback = enable_feedback
        self._enable_swarm = enable_swarm
        self._enable_compact = enable_compact
        self._enable_hooks = enable_hooks
        self.profile: IclawProfile | None = None
        self.started_at = 0.0

    async def start(self) -> None:
        """Boot the core server launcher with the iClaw profile attached."""
        if self.profile is not None:
            return
        if self._agent is None:
            self._agent = EncreAgent(config=EncreConfig())
        self.started_at = time.time()

        server = EncreServer(
            host=self._host,
            port=self._port,
            config=self._agent.config,
            max_concurrent=self._max_concurrent,
        )
        await server.start()

        router = EventRouter(
            session_manager=server.session_manager,
            default_config=self._agent.config,
        )
        self.profile = IclawProfile(
            self._agent,
            router=router,
            scheduler=server.scheduler,
            max_concurrent=self._max_concurrent,
            consolidation_interval=self._consolidation_interval,
            enable_evolution=self._enable_evolution,
            enable_reflex=self._enable_reflex,
            enable_metacognition=self._enable_metacognition,
            enable_feedback=self._enable_feedback,
            enable_swarm=self._enable_swarm,
            enable_compact=self._enable_compact,
            enable_hooks=self._enable_hooks,
        )
        await self.profile.start()
        print(f"ICLAW_READY ws://{self._host}:{self._port}/ws", flush=True)

    async def wait(self) -> None:
        """Block until the daemon is cancelled."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Stop the profile and clear the PID file."""
        if self.profile is not None:
            await self.profile.stop()
            self.profile = None
        _clear_pid()

    # Stats passthrough

    def get_stats(self) -> dict[str, Any]:
        """Return the profile stats snapshot, or idle zeros."""
        if self.profile is None:
            return {"uptime_seconds": 0.0}
        return self.profile.get_stats()

    def get_health(self) -> dict[str, Any]:
        """Return the profile health summary, or ``stopped``."""
        if self.profile is None:
            return {"status": "stopped", "running": False}
        return self.profile.get_health()

    def list_jobs(self) -> list:
        """Return all currently scheduled jobs."""
        if self.profile is None:
            return []
        return self.profile.list_jobs()
