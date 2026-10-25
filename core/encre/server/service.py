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

"""Daemon wrapper for the Encre server process.

This module is process plumbing only: file logging, PID-file bookkeeping,
stale-lock detection and signal-driven shutdown.  It carries no agent logic --
:class:`encre.server.app.EncreServer` owns the runtime, and this module only
decides whether, where and under what constraints that runtime runs.

It is selected by the ``--service`` switch in :mod:`encre.server.app`, which is
the path the desktop main process takes when it wants the backend to detach
from the console that launched it (autostart / restart-on-boot / service
managers).  The foreground path skips this module and uses
:meth:`logging.basicConfig` on stderr instead, because there the terminal is
the intended sink.

Public API
    * :func:`run_service` -- programmatic daemon entry point.
    * :func:`main` -- the ``python -m encre.server.service`` CLI.

Layout contract: everything this module owns lives in
:data:`ENCRE_DATA_DIR`, which the Encre Agent Desktop and
:func:`encre.paths.get_data_dir` resolve to the same location, so a PID file
written here is the one other components look for.
"""

import argparse
import logging
import os
import signal
from pathlib import Path

# Override the default data root when the host embeds Encre (containers,
# per-user sandboxes) and does not want state under the user profile.
ENCRE_DATA_DIR = Path(os.environ.get("ENCRE_DATA_DIR", Path.home() / ".dunimd" / "encre"))
PID_FILE = ENCRE_DATA_DIR / "yimd.pid"
LOG_FILE = ENCRE_DATA_DIR / "yimd.log"

logger = logging.getLogger("encre.service")


def _setup_logging(log_level: str) -> None:
    """Point the root logger at the persistent log file.

    A detached daemon has no terminal, so the file handler is the only place
    operator-visible output goes.  Levels are configured on the root logger
    rather than on :data:`logger` so that every sub-logger used by the runtime
    (``encre.server``, ``encre.gateway.*``, ``encre.agent``) honours the same
    threshold instead of falling back to WARNING.

    Args:
        log_level: Case-insensitive level name such as ``"DEBUG"`` or
            ``"WARNING"``; unknown names would raise from ``getattr`` and are
            therefore constrained by the CLI default.

    Raises:
        OSError: If the data directory cannot be created or the log file
            cannot be opened.
    """
    ENCRE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(str(LOG_FILE), mode="a", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper()))
    root.addHandler(file_handler)
    # watchfiles logs a DEBUG record on every 1s poll timeout while idle
    # ("rust notify timeout, continuing"); keep app-level DEBUG without
    # flooding the log file with third-party watcher noise.
    logging.getLogger("watchfiles").setLevel(logging.INFO)
    logger.info("File logging initialized (level=%s, path=%s)", log_level, LOG_FILE)


def _write_pid() -> None:
    """Publish this process's PID so operators and supervisors can find it.

    The file is written after logging is up, so a failed :func:`_setup_logging`
    never leaves a live process masquerading as recorded.  The content is the
    bare PID with no trailing newline: readers parse it with ``int(...strip())``.

    Raises:
        OSError: If the data directory cannot be created or the file cannot
            be written.
    """
    ENCRE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    logger.info("PID file written: %d -> %s", os.getpid(), PID_FILE)


def _remove_pid() -> None:
    """Clear the PID file, tolerating both a missing file and I/O errors.

    Called on both the normal exit path and the signal path, so it must never
    raise: a cleanup that throws would mask the real shutdown reason and, on
    the signal path, would abort the handler.
    """
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
            logger.info("PID file removed: %s", PID_FILE)
    except Exception:
        pass


def _check_stale_pid() -> None:
    """Drop a PID file left behind by a daemon that died without cleaning up.

    A killed or crashed daemon never reaches :func:`_remove_pid`, so the file
    is otherwise sticky.  The probe is ``os.kill(pid, 0)``: signal zero performs
    the permission and existence checks without delivering anything, so it is
    the cheapest liveness test available cross-platform on POSIX.

    Two distinct outcomes are worth logging separately:

    * ``ProcessLookupError`` -- the PID is gone, the record is a leftover.
    * ``PermissionError`` / other ``OSError`` -- the PID is alive but owned by
      another user, so this caller must not touch the record at all.
    """
    if not PID_FILE.exists():
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError, OSError):
        logger.warning("Stale PID file found, cleaning up (PID was dead)")
        _remove_pid()
        logger.warning("PID file exists but process is owned by another user")


def _shutdown(signum: int, _frame) -> None:
    """Signal handler that converts a stop request into loop termination.

    ``SIGTERM`` arrives on the main thread between bytecodes; from a handler
    the only sanctioned way to reach the running coroutine is
    :func:`threading.interrupt_main`, which wakes the main thread and raises
    :class:`KeyboardInterrupt` there.  That exception unwinds
    :func:`asyncio.run`, which in turn lets
    :func:`encre.server.app.EncreServer.serve_forever` reach its ``finally``
    block and run the ordered :func:`stop` sequence.

    The PID file is released before the interrupt so a second supervisor
    restart sees a clean slate immediately.

    Args:
        signum: The signal number that fired, logged for post-mortems.
        _frame: Ignored main-thread frame supplied by the interpreter.
    """
    logger.info("Received signal %d, shutting down...", signum)
    _remove_pid()
    # Signal the main event loop to stop by raising KeyboardInterrupt
    import threading
    threading.main_thread().interrupt_main()


def run_service(
    host: str = "localhost",
    port: int = 7110,
    config_file: str | None = None,
    max_concurrent: int = 20,
    log_level: str = "INFO",
) -> None:
    """Run the Encre server as a detached background daemon.

    Startup order matters: logging first (so every later step is observable),
    then stale-PID recovery, then the PID publish, and only then the server
    itself.  Publishing the PID before the server is actually listening would
    make a failed bind look like a running daemon to whatever supervises it.

    :func:`encre.server.app.run_server` is imported inside the function rather
    than at module scope to keep this module importable in environments that
    cannot load the full runtime yet -- the desktop process imports the CLI
    path before deciding which mode to run.

    Args:
        host: Interface to bind.
        port: Port to bind; ``0`` lets the OS choose.
        config_file: Optional explicit ``config.yaml`` path; otherwise
            :class:`encre.config.EncreConfig` auto-discovery applies.
        max_concurrent: Upper bound on simultaneously active agent sessions,
            enforced by the session manager rather than here.
        log_level: Threshold for the file log.

    Raises:
        OSError: From logging or PID-file setup.
        KeyboardInterrupt: Propagates after :func:`_remove_pid` runs in the
            ``finally`` clause, so the PID is never orphaned on interrupt.
    """
    _setup_logging(log_level)
    _check_stale_pid()
    _write_pid()

    logger.info("Background service starting (PID %d, host=%s, port=%d)", os.getpid(), host, port)

    signal.signal(signal.SIGTERM, _shutdown)

    from encre.server.app import run_server

    try:
        run_server(
            host=host,
            port=port,
            config_file=config_file,
            max_concurrent=max_concurrent,
        )
    finally:
        _remove_pid()


def main() -> None:
    """CLI entry point for the daemon mode.

    Arguments mirror the foreground flags in :mod:`encre.server.app` so both
    modes can be driven by the same wrapper script; ``--service`` is what
    selects this module in the first place, so it is not repeated here.

    The parser defaults are deliberately duplicated from
    :func:`run_service` rather than referenced from it, keeping the CLI
    contract readable at the call site and independent of library evolution.
    """
    parser = argparse.ArgumentParser(description="Encre Background Service")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7110)
    parser.add_argument("--config", help="Path to config file")
    parser.add_argument("--max-concurrent", type=int, default=20)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    run_service(
        host=args.host,
        port=args.port,
        config_file=args.config,
        max_concurrent=args.max_concurrent,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
