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

"""
Command-line entry point for ``python -m encre.iclaw``.

This module is the shell around :func:`encre.iclaw.run_iclaw`. It exists
because the Encre Agent Desktop does not embed the daemon in its own
process; the Electron main process spawns it as a separate OS process and
needs a well-defined handshake to know when to attach.

The handshake is textual rather than a socket probe: once the WebSocket
listener is bound, :func:`encre.iclaw.run_iclaw` writes ``ICLAW_READY``
followed by the bind address to stdout. The desktop parses that single line
to discover the port -- which matters because the port can be overridden by
an operator and, when ``--port`` is left at its default, the caller still
wants one authoritative value rather than re-reading config.

Everything here is argument parsing plus lifecycle plumbing. The daemon
itself -- PID file, log file, scheduler, the self-improvement add-ons --
lives in :mod:`encre.iclaw`. Keep the flag surface in this file and the
behaviour there, otherwise the two drift and ``--help`` lies.

Position in Encre: the gateway mode (:mod:`encre.gateway`) bridges third-party
IM platforms, while the iClaw daemon runs the same Rust agent core as a
long-lived local WebSocket service that the desktop UI and headless drivers
attach to. This module is the only place that mode is started by hand.

CLI:
    --host                     Bind address (default: 127.0.0.1)
    --port                     WebSocket port (default: 18791)
    --max-concurrent           Max concurrent sessions (default: 20)
    --scheduler-poll-interval  Scheduler polling interval in seconds (default: 30)
    --consolidation-interval   Memory consolidation seconds (default: 3600, 0=disabled)
    --compact                  Enable context compaction (default: true)
    --no-compact               Disable context compaction
    --evolution                Enable evolution learning (default: true)
    --no-evolution             Disable evolution learning
    --reflex                   Enable reflex loop (default: true)
    --no-reflex                Disable reflex loop
    --metacognition            Enable metacognition (default: true)
    --no-metacognition         Disable metacognition
    --feedback                 Enable feedback learning (default: true)
    --no-feedback              Disable feedback learning
    --swarm                    Enable swarm / multi-agent (default: true)
    --no-swarm                 Disable swarm / multi-agent
    --hooks                    Enable hook system (default: true)
    --no-hooks                 Disable hook system
    --log-level                Logging level (default: INFO)
    --stop                     Stop a running daemon via PID file
    --status                   Check if daemon is running (prints "running"/"stopped")
"""

import argparse
import asyncio
import contextlib
import logging

from encre.agent import EncreAgent
from encre.config import EncreConfig
from encre.iclaw import _data_dir, _log_path, is_running, run_iclaw, stop_daemon


def main() -> None:
    """Parse CLI arguments and run (or signal) the iClaw daemon.

    Three mutually exclusive modes are handled in order:

    - ``--stop``: terminate the daemon that owns the PID file. Runs before
      logging is configured so a control-plane invocation leaves no log
      noise behind.
    - ``--status``: report ``running`` or ``stopped`` and exit. The desktop
      polls this to decide whether to spawn a fresh daemon on startup.
    - default: boot the daemon for good.

    The paired ``--flag`` / ``--no-flag`` pairs are the reason every boolean
    here is declared twice with a shared ``dest``: argparse sets the
    default to the enabled state and lets the ``--no-*`` form override it,
    so an operator can disable any single self-improvement subsystem without
    disabling the whole group.

    Note:
        ``--compact`` and ``--hooks`` are parsed and forwarded to
        :func:`encre.iclaw.run_iclaw` but that function does not currently
        pass them on to the profile. They exist so the flag surface is stable
        and an operator setting them sees no argparse error.

    Args:
        None. Arguments come from ``sys.argv`` via argparse.

    Returns:
        ``None``, always. Control-plane modes return early; the daemon path
        blocks inside :func:`asyncio.run` for the lifetime of the process.

    Raises:
        SystemExit: From argparse on ``--help``, ``--stop`` on an invalid
            host port, or an unknown flag. Callers (the desktop spawner)
            treat a non-zero exit as a failed daemon start.
    """
    # Configure the argument parser with all daemon tunables.
    parser = argparse.ArgumentParser(
        description="iClaw -- Encre background daemon",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--max-concurrent", type=int, default=20)
    parser.add_argument("--scheduler-poll-interval", type=float, default=30.0,
                        help="Scheduler polling interval in seconds")
    parser.add_argument("--consolidation-interval", type=int, default=3600)

    parser.add_argument("--compact", action="store_true", default=True,
                        dest="compact", help="Enable context compaction")
    parser.add_argument("--no-compact", action="store_false",
                        dest="compact", help="Disable context compaction")
    parser.add_argument("--evolution", action="store_true", default=True,
                        dest="evolution", help="Enable evolution learning")
    parser.add_argument("--no-evolution", action="store_false",
                        dest="evolution", help="Disable evolution learning")
    parser.add_argument("--reflex", action="store_true", default=True,
                        dest="reflex", help="Enable reflex loop")
    parser.add_argument("--no-reflex", action="store_false",
                        dest="reflex", help="Disable reflex loop")
    parser.add_argument("--metacognition", action="store_true", default=True,
                        dest="metacognition", help="Enable metacognition")
    parser.add_argument("--no-metacognition", action="store_false",
                        dest="metacognition", help="Disable metacognition")
    parser.add_argument("--feedback", action="store_true", default=True,
                        dest="feedback", help="Enable feedback learning")
    parser.add_argument("--no-feedback", action="store_false",
                        dest="feedback", help="Disable feedback learning")
    parser.add_argument("--swarm", action="store_true", default=True,
                        dest="swarm", help="Enable swarm / multi-agent")
    parser.add_argument("--no-swarm", action="store_false",
                        dest="swarm", help="Disable swarm / multi-agent")
    parser.add_argument("--hooks", action="store_true", default=True,
                        dest="hooks", help="Enable hook system")
    parser.add_argument("--no-hooks", action="store_false",
                        dest="hooks", help="Disable hook system")

    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--stop", action="store_true",
                        help="Stop a running daemon")
    parser.add_argument("--status", action="store_true",
                        help="Check if daemon is running")

    args = parser.parse_args()

    # --stop / --status act on an already-running daemon via the PID file, so
    # they must short-circuit before the data dir, logging, and agent
    # construction below -- those belong to a fresh boot, not a control-plane
    # call.
    if args.stop:
        if is_running():
            stop_daemon()
            print("iClaw daemon stopped", flush=True)
        else:
            print("iClaw daemon is not running", flush=True)
        return

    if args.status:
        if is_running():
            print("running", flush=True)
        else:
            print("stopped", flush=True)
        return

    # The data directory is created before logging is configured because the
    # log file itself lives inside it; basicConfig with a filename target
    # fails silently if the directory does not exist yet.
    _data_dir().mkdir(parents=True, exist_ok=True)

    # Log to a file rather than stdout. stdout is reserved for the
    # ICLAW_READY handshake that the desktop process parses line by line, so
    # any log record written there would corrupt that protocol.
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        filename=str(_log_path()),
    )

    config = EncreConfig()
    # The agent is built here, before asyncio.run, so a construction error
    # fails synchronously with a normal traceback instead of surfacing inside
    # the event loop after the ICLAW_READY line was already emitted.
    agent = EncreAgent(config=config)

    # KeyboardInterrupt is swallowed so Ctrl-C tears the daemon down through
    # the normal shutdown path (which clears the PID file) rather than
    # leaving a stale PID behind.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_iclaw(
            agent=agent,
            host=args.host,
            port=args.port,
            max_concurrent=args.max_concurrent,
            consolidation_interval=args.consolidation_interval,
            scheduler_poll_interval=args.scheduler_poll_interval,
            enable_compact=args.compact,
            enable_evolution=args.evolution,
            enable_reflex=args.reflex,
            enable_metacognition=args.metacognition,
            enable_feedback=args.feedback,
            enable_swarm=args.swarm,
            enable_hooks=args.hooks,
        ))


if __name__ == "__main__":
    main()
