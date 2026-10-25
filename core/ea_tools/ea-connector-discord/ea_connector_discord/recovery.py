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
Durable SQLite ledger backing Discord reconnect recovery.

Encre's Discord gateway (:class:`ea_connector_discord.adapter.DiscordAdapter`)
cannot know, at the moment it reconnects, which inbound Discord messages it
failed to answer while it was offline.  Discord does not replay missed
``MESSAGE_CREATE`` events.  This module supplies the persistent bookkeeping that
lets the adapter close that gap: a small profile-scoped SQLite database that
records every Discord message the gateway has observed, the lifecycle state of
its handling, and the per-channel scan cursor used to resume an interrupted
recovery sweep.

Data flow.  All state mutation happens inside
:meth:`DiscordRecoveryStore.call`, which opens a short-lived connection, runs the
caller's operation, commits, and closes.  The adapter wraps those calls in
``asyncio.to_thread`` (see
:meth:`ea_connector_discord.adapter.DiscordAdapter._with_discord_recovery_db_async`)
because the blocking driver must never run on the asyncio event loop that also
drives Discord's WebSocket reader.  Reads and writes both go through the same
lock, so a recovery scan racing a live message dispatch cannot interleave inside
a transaction.

Tables (see :meth:`DiscordRecoveryStore._initialize`):

* ``discord_messages`` -- one row per observed Discord message id, carrying the
  channel/thread/partition ids, the handling state machine
  (``discovered`` -> ``queued`` -> ``processing`` -> ``processed`` /
  ``responded`` / ``failed`` / ``cancelled``), and the bookkeeping needed to
  distinguish a genuinely completed reply from a bare emoji acknowledgement.
* ``discord_recovery_scans`` -- one row per recovery sweep, for operator audit
  of what a reconnect reconciliation covered and whether it finished.
* ``discord_recovery_cursors`` -- the last message id committed per channel, so
  a resumed scan starts where the previous one stopped instead of re-reading the
  entire window.

Public API: :class:`DiscordRecoveryStore`.
"""

import datetime as dt
import logging
import os
import sqlite3
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from encre.config import get_data_dir

logger = logging.getLogger(__name__)

# SQLite database file name, stored inside the ``gateway/`` subdirectory of the
# configured Encre data directory.  The file is per-profile: profiles are
# isolated by the data directory that :func:`encre.config.get_data_dir` returns,
# so two concurrent gateway profiles never share one ledger.
_DB_FILENAME = "discord_message_recovery.db"

# Rows older than this are dropped during schema initialisation.  The retention
# window bounds database growth without affecting any recovery sweep, which is
# always limited to a much shorter lookback configured on the adapter.
_RETENTION_DAYS = 30


class DiscordRecoveryStore:
    """Thread-safe SQLite ledger of Discord message handling state.

    A single store instance is created per
    :class:`ea_connector_discord.adapter.DiscordAdapter` and shared
    across every asyncio task in that gateway process.  The class is not
    itself async: it exposes one synchronous entry point (:meth:`call`) that
    callers hop off the event loop into, and everything inside it is guarded by
    a single threading lock.

    Invariants maintained:

    * The database file and its schema exist before any caller-supplied
      operation runs; creation is deferred to the first successful call so that
      constructing the store never performs I/O.
    * All read and write operations on one store instance are serialised.
      This is what makes it legal for a recovery scan to read a row while a
      live dispatch writes to the same row.
    * A corrupt, locked, or unreadable database never raises.  Every failure
      path logs a warning and returns the caller's ``default``, which lets the
      adapter degrade to "no durable history" rather than crashing the gateway.

    Attributes:
        _lock: Serialises every database access on this instance.
        _initialized: Guards one-time schema creation and pruning.
        _encre_home: Root data directory used to derive the database path.
    """

    def __init__(self, encre_home: Path | None = None) -> None:
        """Construct the store without touching the filesystem.

        The ``encre_home`` parameter exists for compatibility with callers that
        pass an explicit root; the resolved location always comes from the
        active Encre profile, so the store stays correctly scoped to the
        running gateway profile.

        Args:
            encre_home: Optional data root override; retained for API
                compatibility and deliberately not used to relocate the
                database.

        Returns:
            None.
        """
        self._lock = threading.Lock()
        self._initialized = False
        # Resolve the data directory from the active profile.  The supplied
        # argument is intentionally ignored so two callers passing different
        # roots still converge on the one database the adapter reads from.
        self._encre_home = Path(get_data_dir())

    def path(self) -> Path:
        """Return the absolute database file path, creating its parent first.

        Creating the ``gateway`` directory here rather than at store
        construction time keeps :class:`DiscordRecoveryStore` cheap to
        instantiate and ensures the directory exists exactly when a writer
        needs it.

        Returns:
            Path: Absolute path of ``discord_message_recovery.db`` under the
                profile's ``gateway`` directory.
        """
        directory = self._encre_home / "gateway"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / _DB_FILENAME

    def call(self, fn: Callable[[sqlite3.Connection], Any], default: Any = None) -> Any:
        """Run *fn* inside a locked, short-lived transaction.

        This is the only public mutation/read entry point.  It acquires the
        instance lock, opens a fresh connection with a deliberately tiny timeout,
        performs one-time schema initialisation on the first successful call,
        executes *fn* against the live connection, commits, and closes.

        Failures are swallowed by design: a lost ledger must degrade the
        recovery feature, not the gateway.  Callers must therefore treat the
        return value as possibly ``default`` and never assume the write
        landed.

        Args:
            fn: Operation to run against an open connection.  Receives the
                connection and returns its result; the connection is not
                closed or committed by *fn*.
            default: Value returned when the operation cannot complete,
                including when the store is unavailable.

        Returns:
            Any: The result of *fn* on success, otherwise *default*.
        """
        try:
            with self._lock:
                path = self.path()
                # A short timeout prevents a stuck reader from blocking the
                # gateway indefinitely; the lock already serialises writers.
                conn = sqlite3.connect(path, timeout=0.1)
                try:
                    if not self._initialized:
                        self._initialize(conn)
                        self._initialized = True
                        # Restrict the database file to the owning user only.
                        with suppress(OSError):
                            os.chmod(path, 0o600)
                    result = fn(conn)
                    conn.commit()
                    return result
                finally:
                    conn.close()
        except Exception as exc:
            logger.warning("Discord recovery ledger unavailable: %s", exc)
            return default

    def _initialize(self, conn: sqlite3.Connection) -> None:
        """Create the ledger schema and prune rows past the retention window.

        Runs exactly once per store instance, inside the caller's transaction,
        so the schema and the pruning share the first commit.

        ``PRAGMA journal_mode=WAL`` is enabled to let the recovery scan reader
        and the live-dispatch writer proceed concurrently against the same file
        on SQLite builds that support WAL; if the platform cannot enable it the
        pragma is a no-op and the instance lock still serialises access.

        Args:
            conn: Open SQLite connection; transaction boundaries are owned by
                the caller.

        Returns:
            None.
        """
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_messages (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT,
                thread_id TEXT,
                parent_channel_id TEXT,
                author_id TEXT,
                created_at TEXT,
                status TEXT NOT NULL,
                replied INTEGER NOT NULL DEFAULT 0,
                emoji_ack INTEGER NOT NULL DEFAULT 0,
                outage_response INTEGER NOT NULL DEFAULT 0,
                response_message_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_recovery_scans (
                scan_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                channels TEXT NOT NULL,
                window_seconds REAL NOT NULL,
                limit_count INTEGER NOT NULL,
                scanned INTEGER NOT NULL DEFAULT 0,
                missed INTEGER NOT NULL DEFAULT 0,
                dispatched INTEGER NOT NULL DEFAULT 0,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_recovery_cursors (
                channel_id TEXT PRIMARY KEY,
                last_message_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Compute the oldest timestamp we keep and drop everything older.
        cutoff = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=_RETENTION_DAYS)
        ).isoformat()
        conn.execute("DELETE FROM discord_messages WHERE updated_at < ?", (cutoff,))
        conn.execute(
            "DELETE FROM discord_recovery_scans "
            "WHERE COALESCE(completed_at, started_at) < ?",
            (cutoff,),
        )
        conn.execute(
            "DELETE FROM discord_recovery_cursors WHERE updated_at < ?",
            (cutoff,),
        )
