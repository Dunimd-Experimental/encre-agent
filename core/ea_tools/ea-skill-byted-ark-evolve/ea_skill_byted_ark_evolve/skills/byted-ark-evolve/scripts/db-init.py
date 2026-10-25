#!/usr/bin/env python3
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

"""Initialize the evolution SQLite database."""

import sqlite3
import os
import sys
import argparse

from _workspace import resolve_workspace_root

DEFAULT_DB_DIR = os.path.join(resolve_workspace_root(), "evolution-data")
# Windows UTF-8 fix
for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")

DEFAULT_DB_PATH = os.path.join(DEFAULT_DB_DIR, "evolution.db")


def get_db_path():
    parser = argparse.ArgumentParser(description="Initialize evolution SQLite database")
    parser.add_argument(
        "db_path_positional", nargs="?", default=None, help="Legacy positional DB path"
    )
    parser.add_argument("--db", default=None, help="Path to evolution.db")
    args = parser.parse_args()
    return args.db or args.db_path_positional or DEFAULT_DB_PATH


def init_db(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    base_dir = os.path.dirname(db_path)
    os.makedirs(os.path.join(base_dir, "trajectories", "golden"), exist_ok=True)
    os.makedirs(os.path.join(base_dir, "trajectories", "corrections"), exist_ok=True)
    os.makedirs(os.path.join(base_dir, "reports"), exist_ok=True)
    os.makedirs(os.path.join(base_dir, "tmp"), exist_ok=True)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
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

        CREATE TABLE IF NOT EXISTS trajectories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK(type IN ('golden','correction','candidate')),
            task_type TEXT,
            tags TEXT,
            key_steps TEXT,
            error_approach TEXT,
            correct_approach TEXT,
            root_cause TEXT,
            file_path TEXT,
            session_id TEXT,
            verified INTEGER DEFAULT 0,
            verify_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'prepared' CHECK(status IN ('prepared','running','completed','failed')),
            run_reason TEXT,
            daily_digest_json TEXT,
            pending_summary_json TEXT,
            worker_started_at TEXT,
            worker_finished_at TEXT,
            worker_error TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS mutations (
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

        CREATE TABLE IF NOT EXISTS gene_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id TEXT,
            signal_id INTEGER,
            gene_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            judgment_type TEXT DEFAULT 'root_cause_fit' CHECK(judgment_type IN ('root_cause_fit','surface_relief','fallback_only')),
            confidence REAL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS evolution_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE NOT NULL,
            signals_analyzed INTEGER DEFAULT 0,
            mutations_proposed INTEGER DEFAULT 0,
            mutations_applied INTEGER DEFAULT 0,
            sessions_analyzed INTEGER DEFAULT 0,
            sessions_cost_usd REAL DEFAULT 0,
            evolution_cost_usd REAL DEFAULT 0,
            report_path TEXT,
            saturation_note TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(type);
        CREATE INDEX IF NOT EXISTS idx_signals_processed ON signals(processed);
        CREATE INDEX IF NOT EXISTS idx_signals_layer ON signals(layer);
        CREATE INDEX IF NOT EXISTS idx_trajectories_type ON trajectories(type);
        CREATE INDEX IF NOT EXISTS idx_mutations_status ON mutations(status);
        CREATE INDEX IF NOT EXISTS idx_mutations_proposal_status ON mutations(proposal_status);
        CREATE INDEX IF NOT EXISTS idx_mutations_proposal_group ON mutations(proposal_group_id);
        CREATE INDEX IF NOT EXISTS idx_gene_matches_signal ON gene_matches(signal_id);
        CREATE INDEX IF NOT EXISTS idx_gene_matches_gene ON gene_matches(gene_id);
    """)

    conn.commit()
    conn.close()
    print(f"Database initialized: {db_path}")


if __name__ == "__main__":
    init_db(get_db_path())
