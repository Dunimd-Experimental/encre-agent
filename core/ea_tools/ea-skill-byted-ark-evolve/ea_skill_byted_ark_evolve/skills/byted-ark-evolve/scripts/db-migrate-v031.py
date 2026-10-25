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

"""Database migration for v0.3.1: expand signal types + add positive-anchor source."""

import sqlite3
import os
import sys
import argparse

for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")

from _workspace import resolve_workspace_root

DEFAULT_DB_PATH = os.path.join(resolve_workspace_root(), "evolution-data/evolution.db")

MIGRATION_SQL = """
-- v0.3.1: Expand signal types to include preference and clarification
CREATE TABLE IF NOT EXISTS signals_v029 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    type TEXT NOT NULL CHECK(type IN ('correction','negative','positive','suggestion','preference','clarification')),
    layer TEXT CHECK(layer IN ('identity','context','protocol','capability','runtime')),
    severity TEXT DEFAULT 'medium' CHECK(severity IN ('low','medium','high')),
    raw_text TEXT NOT NULL,
    context TEXT,
    processed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

INSERT INTO signals_v029 (id, session_id, type, layer, severity, raw_text, context, processed, created_at)
SELECT id, session_id, type, layer, severity, raw_text, context, processed, created_at
FROM signals;

DROP TABLE signals;
ALTER TABLE signals_v029 RENAME TO signals;

CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(type);
CREATE INDEX IF NOT EXISTS idx_signals_processed ON signals(processed);
CREATE INDEX IF NOT EXISTS idx_signals_layer ON signals(layer);

-- v0.3.1: Expand mutations source to include positive-anchor
CREATE TABLE IF NOT EXISTS mutations_v029 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT,
    proposal_group_id TEXT,
    proposal_summary TEXT,
    proposal_status TEXT DEFAULT 'pending' CHECK(proposal_status IN ('pending','presented','accepted','rejected','stale','superseded')),
    presented_at TEXT,
    decision_at TEXT,
    expires_at TEXT,
    target_file TEXT NOT NULL,
    mutation_type TEXT CHECK(mutation_type IN ('add','modify','remove')),
    layer TEXT CHECK(layer IN ('identity','context','protocol','capability','runtime')),
    description TEXT,
    before_text TEXT,
    after_text TEXT,
    signal_ids TEXT,
    source TEXT DEFAULT 'evolution' CHECK(source IN ('evolution','user-direct','snapshot-diff','positive-anchor')),
    status TEXT DEFAULT 'proposed' CHECK(status IN ('proposed','approved','applied','verified','rejected')),
    pareto_check TEXT,
    verification_criteria TEXT,
    layer_reason TEXT,
    gene_id TEXT,
    gene_reason TEXT,
    session_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    applied_at TEXT
);

INSERT INTO mutations_v029 SELECT * FROM mutations;
DROP TABLE mutations;
ALTER TABLE mutations_v029 RENAME TO mutations;

CREATE INDEX IF NOT EXISTS idx_mutations_status ON mutations(status);
CREATE INDEX IF NOT EXISTS idx_mutations_proposal_status ON mutations(proposal_status);
CREATE INDEX IF NOT EXISTS idx_mutations_proposal_group ON mutations(proposal_group_id);

-- Record migration
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO schema_migrations (version) VALUES ('v0.3.1');
"""


def migrate(db_path=None):
    path = db_path or DEFAULT_DB_PATH
    if not os.path.exists(path):
        print(f"DB not found: {path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT version FROM schema_migrations WHERE version = 'v0.3.1'")
        if cur.fetchone():
            print("Migration v0.3.1 already applied.")
            conn.close()
            return
    except sqlite3.OperationalError:
        pass

    conn.executescript(MIGRATION_SQL)
    conn.close()
    print(f"Migration v0.3.1 applied to {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate evolution DB to v0.3.1")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    migrate(args.db)
