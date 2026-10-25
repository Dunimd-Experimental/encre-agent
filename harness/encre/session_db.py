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

import contextlib
import json
import logging
import pathlib
import sqlite3
import threading
from typing import Any

from encre.crypto import decrypt, encrypt

logger = logging.getLogger("encre.session_db")

_DB_FILENAME = "session.db"


def _encode(items: list[dict[str, Any]]) -> list[str]:
    return [encrypt(json.dumps(i, ensure_ascii=False)) for i in items]


class SessionDB:
    """Per-session SQLite store for summary-panel data.

    Holds ``plan_items``, ``artifacts`` and ``references`` in a single
    sqlite database file inside the session directory (``session.db``).
    Because these tables are written by ``EncreSession.save_to_dir()`` --
    never rewritten from compacted messages -- they survive context
    compaction and are restored verbatim on load, exactly like the meta
    file but with a dedicated, unique-per-session database file.

    Values are stored as encrypted JSON strings (AES-256-GCM via
    ``encre.crypto``), matching the rest of the session storage.
    Thread-safe: one connection guarded by a lock.
    """

    def __init__(self, db_path: str | pathlib.Path | None = None, session_dir: str | pathlib.Path | None = None) -> None:
        if db_path is None:
            if session_dir is None:
                raise ValueError("SessionDB requires either db_path or session_dir")
            db_path = pathlib.Path(session_dir) / _DB_FILENAME
        self._path = pathlib.Path(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._open()

    # ── connection lifecycle ─────────────────────────────────────────────

    def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS plan_items (
                ord INTEGER PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                path TEXT PRIMARY KEY,
                ord INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS references_ (
                ord INTEGER PRIMARY KEY,
                data TEXT NOT NULL
            );
            """
        )
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    self._conn.close()
                self._conn = None

    # ── public API ───────────────────────────────────────────────────────

    def save(self, plan_items: list[dict[str, Any]], artifacts: list[dict[str, Any]], references: list[dict[str, Any]]) -> None:
        """Replace all stored summary-panel data.

        ``artifacts`` is keyed by the artifact's ``path`` so the table stays
        unique; ordering is preserved via the ``ord`` column.
        """
        encoded_plan = _encode(plan_items)
        encoded_arts = _encode(artifacts)
        encoded_refs = _encode(references)
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            with conn:
                conn.execute("DELETE FROM plan_items")
                conn.execute("DELETE FROM artifacts")
                conn.execute("DELETE FROM references_")
                conn.executemany(
                    "INSERT INTO plan_items (ord, data) VALUES (?, ?)",
                    [(i, d) for i, d in enumerate(encoded_plan)],
                )
                conn.executemany(
                    "INSERT INTO artifacts (path, ord, data) VALUES (?, ?, ?)",
                    [
                        (str(a.get("path", i)), i, encoded_arts[i])
                        for i, a in enumerate(artifacts)
                    ],
                )
                conn.executemany(
                    "INSERT INTO references_ (ord, data) VALUES (?, ?)",
                    [(i, d) for i, d in enumerate(encoded_refs)],
                )

    def load(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Return ``(plan_items, artifacts, references)`` stored in the DB."""
        plan_items: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        references: list[dict[str, Any]] = []
        with self._lock:
            conn = self._conn
            if conn is None:
                return plan_items, artifacts, references
            try:
                plan_items = self._decode(conn.execute(
                    "SELECT data FROM plan_items ORDER BY ord"
                ).fetchall())
                artifacts = self._decode(conn.execute(
                    "SELECT data FROM artifacts ORDER BY ord"
                ).fetchall())
                references = self._decode(conn.execute(
                    "SELECT data FROM references_ ORDER BY ord"
                ).fetchall())
            except Exception as e:
                logger.warning("[SessionDB] load failed for %s: %s", self._path, e)
                return [], [], []
        return plan_items, artifacts, references

    def has_data(self) -> bool:
        with self._lock:
            conn = self._conn
            if conn is None:
                return False
            try:
                for table in ("plan_items", "artifacts", "references_"):
                    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    if row and row[0] > 0:
                        return True
            except Exception as e:
                logger.warning("[SessionDB] has_data failed for %s: %s", self._path, e)
                return False
        return False

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _decode(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for (data,) in rows:
            try:
                result.append(json.loads(decrypt(data)))
            except Exception:
                with contextlib.suppress(Exception):
                    result.append(json.loads(data))
        return result
