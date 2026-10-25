#!/usr/bin/env python3
# ruff: noqa: E402
# Copyright 2026 Beijing Volcano Engine Technology Co., Ltd.
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

from __future__ import annotations

"""
Migration: Add source and session_id columns to mutations table.

Safe to run multiple times - uses IF NOT EXISTS logic.

Usage:
  python db-migrate-source.py [path-to-evolution.db]
"""

import sqlite3
import os
import sys

for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")

from _workspace import resolve_workspace_root

DEFAULT_DB_PATH = os.path.join(resolve_workspace_root(), "evolution-data/evolution.db")


def migrate(db_path):
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Check existing columns
    cur.execute("PRAGMA table_info(mutations)")
    columns = {row[1] for row in cur.fetchall()}

    added = []

    if "source" not in columns:
        cur.execute("""
            ALTER TABLE mutations
            ADD COLUMN source TEXT DEFAULT 'evolution'
            CHECK(source IN ('evolution','user-direct','snapshot-diff'))
        """)
        added.append("source")

    if "session_id" not in columns:
        cur.execute("ALTER TABLE mutations ADD COLUMN session_id TEXT")
        added.append("session_id")

    conn.commit()
    conn.close()

    if added:
        print(f"Migration complete. Added columns: {', '.join(added)}")
    else:
        print("No migration needed - columns already exist.")


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    migrate(db_path)
