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

"""Query evolution data from SQLite for the Agent to read."""

import sqlite3
import json
import os
import sys
import argparse

for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")

from _workspace import resolve_workspace_root

DEFAULT_DB_PATH = os.path.join(resolve_workspace_root(), "evolution-data/evolution.db")


def get_db(db_path=None):
    path = db_path or DEFAULT_DB_PATH
    if not os.path.exists(path):
        print(f"Error: DB not found at {path}. Run db-init.py first.", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def query_dashboard(conn):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM signals WHERE processed = 0")
    unprocessed = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM signals")
    total_signals = cur.fetchone()[0]
    cur.execute(
        "SELECT type, COUNT(*) as cnt FROM signals WHERE processed = 0 GROUP BY type ORDER BY cnt DESC"
    )
    signal_by_type = {r["type"]: r["cnt"] for r in cur.fetchall()}
    cur.execute(
        "SELECT layer, COUNT(*) as cnt FROM signals WHERE processed = 0 AND layer IS NOT NULL GROUP BY layer ORDER BY cnt DESC"
    )
    signal_by_layer = {r["layer"]: r["cnt"] for r in cur.fetchall()}
    cur.execute("SELECT status, COUNT(*) as cnt FROM mutations GROUP BY status")
    mutation_by_status = {r["status"]: r["cnt"] for r in cur.fetchall()}
    cur.execute(
        "SELECT proposal_status, COUNT(*) as cnt FROM mutations GROUP BY proposal_status"
    )
    proposal_by_status = {r["proposal_status"]: r["cnt"] for r in cur.fetchall()}
    cur.execute("SELECT type, COUNT(*) as cnt FROM trajectories GROUP BY type")
    traj_by_type = {r["type"]: r["cnt"] for r in cur.fetchall()}
    cur.execute(
        "SELECT gene_id, COUNT(*) as cnt FROM gene_matches GROUP BY gene_id ORDER BY cnt DESC LIMIT 5"
    )
    top_gene_matches = [
        {"gene_id": r["gene_id"], "count": r["cnt"]} for r in cur.fetchall()
    ]
    cur.execute("SELECT * FROM evolution_runs ORDER BY created_at DESC LIMIT 1")
    row = cur.fetchone()
    last_run = dict(row) if row else None
    print(
        json.dumps(
            {
                "signals": {
                    "total": total_signals,
                    "unprocessed": unprocessed,
                    "by_type": signal_by_type,
                    "by_layer": signal_by_layer,
                },
                "mutations": mutation_by_status,
                "proposal_status": proposal_by_status,
                "trajectories": traj_by_type,
                "gene_matches": top_gene_matches,
                "last_run": last_run,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def query_signals(conn, args):
    cur = conn.cursor()
    conditions, params = [], []
    if args.unprocessed:
        conditions.append("processed = 0")
    if args.layer:
        conditions.append("layer = ?")
        params.append(args.layer)
    if args.type:
        conditions.append("type = ?")
        params.append(args.type)
    if args.severity:
        conditions.append("severity = ?")
        params.append(args.severity)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    limit = f" LIMIT {args.limit}" if args.limit else ""
    cur.execute(f"SELECT * FROM signals{where} ORDER BY created_at DESC{limit}", params)
    print(json.dumps([dict(r) for r in cur.fetchall()], ensure_ascii=False, indent=2))


def query_mutations(conn, args):
    cur = conn.cursor()
    conditions, params = [], []
    if args.status:
        conditions.append("status = ?")
        params.append(args.status)
    if getattr(args, "proposal_status", None):
        conditions.append("proposal_status = ?")
        params.append(args.proposal_status)
    if args.layer:
        conditions.append("layer = ?")
        params.append(args.layer)
    if getattr(args, "source", None):
        conditions.append("source = ?")
        params.append(args.source)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    cur.execute(f"SELECT * FROM mutations{where} ORDER BY created_at DESC", params)
    print(json.dumps([dict(r) for r in cur.fetchall()], ensure_ascii=False, indent=2))


def query_trajectories(conn, args):
    cur = conn.cursor()
    if args.search:
        cur.execute(
            "SELECT * FROM trajectories WHERE task_type LIKE ? OR tags LIKE ? OR key_steps LIKE ? ORDER BY created_at DESC LIMIT 10",
            (f"%{args.search}%", f"%{args.search}%", f"%{args.search}%"),
        )
        print(
            json.dumps([dict(r) for r in cur.fetchall()], ensure_ascii=False, indent=2)
        )
    else:
        cur.execute("SELECT type, COUNT(*) as cnt FROM trajectories GROUP BY type")
        stats = {r["type"]: r["cnt"] for r in cur.fetchall()}
        cur.execute(
            "SELECT type, task_type, created_at, verified FROM trajectories ORDER BY created_at DESC LIMIT 20"
        )
        recent = [dict(r) for r in cur.fetchall()]
        print(
            json.dumps({"stats": stats, "recent": recent}, ensure_ascii=False, indent=2)
        )


def query_gene_matches(conn, args):
    cur = conn.cursor()
    conditions, params = [], []
    if args.gene_id:
        conditions.append("gene_id = ?")
        params.append(args.gene_id)
    if args.signal_id:
        conditions.append("signal_id = ?")
        params.append(args.signal_id)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    limit = f" LIMIT {args.limit}" if args.limit else ""
    cur.execute(
        f"SELECT * FROM gene_matches{where} ORDER BY created_at DESC{limit}", params
    )
    print(json.dumps([dict(r) for r in cur.fetchall()], ensure_ascii=False, indent=2))


def query_pending_proposals(conn, _args):
    cur = conn.cursor()
    cur.execute("""
        SELECT proposal_group_id, proposal_summary, proposal_status, MIN(created_at) as created_at, COUNT(*) as mutation_count
        FROM mutations
        WHERE proposal_status IN ('pending','presented')
        GROUP BY proposal_group_id, proposal_summary, proposal_status
        ORDER BY MIN(created_at) ASC
    """)
    print(json.dumps([dict(r) for r in cur.fetchall()], ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Query evolution data")
    parser.add_argument("--db", default=None, help="Path to evolution.db")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("dashboard", help="Show overall status")
    sig = sub.add_parser("signals", help="List signals")
    sig.add_argument("--unprocessed", action="store_true")
    sig.add_argument(
        "--layer", choices=["identity", "context", "protocol", "capability", "runtime"]
    )
    sig.add_argument(
        "--type",
        choices=[
            "correction",
            "negative",
            "positive",
            "suggestion",
            "preference",
            "clarification",
        ],
    )
    sig.add_argument("--severity", choices=["low", "medium", "high"])
    sig.add_argument("--limit", type=int, default=50)
    mut = sub.add_parser("mutations", help="List mutations")
    mut.add_argument(
        "--status", choices=["proposed", "approved", "applied", "verified", "rejected"]
    )
    mut.add_argument(
        "--proposal-status",
        choices=["pending", "presented", "accepted", "rejected", "stale", "superseded"],
    )
    mut.add_argument(
        "--layer", choices=["identity", "context", "protocol", "capability", "runtime"]
    )
    mut.add_argument(
        "--source",
        choices=["evolution", "user-direct", "snapshot-diff", "positive-anchor"],
    )
    traj = sub.add_parser("trajectories", help="List/search trajectories")
    traj.add_argument("--search", default=None)
    gm = sub.add_parser("gene-matches", help="List recent gene match decisions")
    sub.add_parser("pending-proposals", help="List pending/presented proposal groups")
    gm.add_argument("--gene-id", default=None)
    gm.add_argument("--signal-id", type=int, default=None)
    gm.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    conn = get_db(args.db)
    if args.command == "dashboard":
        query_dashboard(conn)
    elif args.command == "signals":
        query_signals(conn, args)
    elif args.command == "mutations":
        query_mutations(conn, args)
    elif args.command == "trajectories":
        query_trajectories(conn, args)
    elif args.command == "gene-matches":
        query_gene_matches(conn, args)
    elif args.command == "pending-proposals":
        query_pending_proposals(conn, args)
    conn.close()


if __name__ == "__main__":
    main()
