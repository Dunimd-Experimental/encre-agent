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

"""SQLite durability helpers.

Response-store and session databases want WAL mode: it gives concurrent
readers alongside a writer and is the fastest of SQLite's journal modes.
WAL, however, relies on shared-memory mapping and file locking that network
filesystems (NFS, SMB) and some FUSE mounts do not provide.  On those mounts
``PRAGMA journal_mode=WAL`` silently stays on the previous mode or raises.

:func:`apply_wal_with_fallback` attempts WAL, verifies the mode actually
changed, and otherwise walks a list of progressively safer journal modes so the
database keeps working -- just not with WAL's concurrency -- instead of
crashing on startup.
"""

import sqlite3
from typing import Any

try:
    from encre.logging_config import get_logger

    logger = get_logger(__name__)
except Exception:  # pragma: no cover - logging is best-effort
    import logging

    logger = logging.getLogger(__name__)

__all__ = ["apply_wal_with_fallback", "apply_durability_pragmas"]

# Tried in order when WAL is unavailable.  TRUNCATE/PERSIST keep the rollback
# journal on disk (durable across crashes); DELETE is SQLite's default; MEMORY
# is the last resort for read-mostly or temporary stores.
_WAL_FALLBACKS = ("TRUNCATE", "PERSIST", "DELETE", "MEMORY")

_BUSY_TIMEOUT_MS = 5000


def _try_journal_mode(conn: sqlite3.Connection, mode: str) -> str | None:
    """Set the journal mode and return the mode SQLite reports afterwards.

    Returns ``None`` when the pragma could not be executed at all (e.g. a
    read-only database).
    """
    try:
        cursor = conn.execute(f"PRAGMA journal_mode={mode}")
        row = cursor.fetchone()
        return str(row[0]).lower() if row and row[0] is not None else None
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return None
    finally:
        try:
            conn.commit()
        except Exception:
            pass


def _safe_pragma(conn: sqlite3.Connection, statement: str) -> None:
    """Execute a pragma, swallowing the errors a locked/read-only db can raise."""
    try:
        conn.execute(statement)
        conn.commit()
    except Exception:
        pass


def apply_wal_with_fallback(conn: sqlite3.Connection | None, db_label: str = "") -> str:
    """Enable WAL on *conn*, falling back to safer modes if unsupported.

    Args:
        conn: An open :class:`sqlite3.Connection`, or ``None``.
        db_label: A human label for logs (e.g. ``"response_store.db"``).

    Returns:
        The journal mode actually applied, lowercased (``"wal"``,
        ``"truncate"``, ...), ``"unknown"`` when *conn* is ``None``, or
        ``"default"`` when even the fallback pragmas failed.
    """
    label = db_label or "sqlite"

    if conn is None:
        return "unknown"

    if _try_journal_mode(conn, "WAL") == "wal":
        _safe_pragma(conn, "PRAGMA synchronous=NORMAL")
        apply_durability_pragmas(conn)
        logger.debug("Enabled WAL journal mode on %s", label)
        return "wal"

    for fallback in _WAL_FALLBACKS:
        if _try_journal_mode(conn, fallback) is not None:
            # Rollback-journal modes need FULL to be crash-safe.
            _safe_pragma(conn, "PRAGMA synchronous=FULL")
            apply_durability_pragmas(conn)
            logger.warning(
                "WAL unavailable on %s (likely a network/FUSE filesystem); "
                "using %s journal mode instead",
                label,
                fallback,
            )
            return fallback.lower()

    _safe_pragma(conn, "PRAGMA synchronous=FULL")
    apply_durability_pragmas(conn)
    logger.warning("Could not set any journal mode on %s; using defaults", label)
    return "default"


def apply_durability_pragmas(conn: sqlite3.Connection | None) -> None:
    """Apply connection-level pragmas shared by all journal modes.

    Sets a busy timeout so a briefly locked database waits instead of raising
    ``database is locked`` immediately.
    """
    if conn is None:
        return
    _safe_pragma(conn, f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
