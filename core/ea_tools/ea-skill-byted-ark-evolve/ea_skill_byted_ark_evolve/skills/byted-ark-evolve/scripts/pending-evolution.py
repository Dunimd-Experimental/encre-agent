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

"""Manage pending evolution proposals and export user-facing summaries."""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def _utcnow_naive():
    """UTC now as a naive datetime (replacement for deprecated _utcnow_naive())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


from _workspace import resolve_workspace_root

_WS = resolve_workspace_root()
DEFAULT_DB_PATH = os.path.join(_WS, "evolution-data/evolution.db")
DEFAULT_DATA_DIR = Path(_WS) / "evolution-data"

for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")


def get_conn(db_path=None):
    path = db_path or DEFAULT_DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def grouped_pending(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT proposal_group_id, proposal_summary, proposal_status,
               MIN(created_at) AS created_at,
               MAX(presented_at) AS presented_at,
               COUNT(*) AS mutation_count,
               GROUP_CONCAT(DISTINCT gene_id) AS gene_ids
        FROM mutations
        WHERE proposal_status IN ('pending','presented')
        GROUP BY proposal_group_id, proposal_summary, proposal_status
        ORDER BY MIN(created_at) ASC
    """)
    groups = []
    for row in cur.fetchall():
        proposal_group_id = row["proposal_group_id"]
        cur.execute(
            "SELECT id, target_file, description, gene_id, gene_reason, layer, status, proposal_status FROM mutations WHERE proposal_group_id = ? ORDER BY id",
            (proposal_group_id,),
        )
        muts = [dict(r) for r in cur.fetchall()]
        groups.append(
            {
                "proposal_group_id": proposal_group_id,
                "proposal_summary": row["proposal_summary"],
                "proposal_status": row["proposal_status"],
                "created_at": row["created_at"],
                "presented_at": row["presented_at"],
                "mutation_count": row["mutation_count"],
                "gene_ids": [x for x in str(row["gene_ids"] or "").split(",") if x],
                "mutations": muts,
            }
        )
    return groups


def export_files(conn, out_dir=DEFAULT_DATA_DIR):
    out_dir.mkdir(parents=True, exist_ok=True)
    pending = grouped_pending(conn)
    now = _utcnow_naive().isoformat() + "Z"
    pending_payload = {"generated_at": now, "pending_proposals": pending}
    digest_payload = {
        "generated_at": now,
        "pending_count": len(pending),
        "summary": [
            {
                "proposal_group_id": p["proposal_group_id"],
                "proposal_summary": p["proposal_summary"],
                "proposal_status": p["proposal_status"],
                "mutation_count": p["mutation_count"],
            }
            for p in pending
        ],
    }
    (out_dir / "pending-evolution.json").write_text(
        json.dumps(pending_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "daily-digest.json").write_text(
        json.dumps(digest_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "pending_path": str(out_dir / "pending-evolution.json"),
        "digest_path": str(out_dir / "daily-digest.json"),
        "pending_count": len(pending),
    }


def show(conn):
    print(
        json.dumps(
            {"pending_proposals": grouped_pending(conn)}, ensure_ascii=False, indent=2
        )
    )


def update_group(
    conn, group_id, proposal_status, decision_at=False, mark_presented=False
):
    cur = conn.cursor()
    parts = ["proposal_status = ?"]
    params = [proposal_status]
    if decision_at:
        parts.append("decision_at = datetime('now')")
    if mark_presented:
        parts.append("presented_at = datetime('now')")
    params.append(group_id)
    cur.execute(
        f"UPDATE mutations SET {', '.join(parts)} WHERE proposal_group_id = ?", params
    )
    conn.commit()
    return cur.rowcount


def mark_stale(conn, older_than_hours=72):
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE mutations
        SET proposal_status = 'stale', decision_at = datetime('now')
        WHERE proposal_status IN ('pending','presented')
          AND datetime(created_at) < datetime('now', ?)
    """,
        (f"-{int(older_than_hours)} hours",),
    )
    conn.commit()
    return cur.rowcount


def main():
    parser = argparse.ArgumentParser(description="Manage pending evolution proposals")
    parser.add_argument("--db", default=None)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("export")
    sub.add_parser("show")
    p = sub.add_parser("present")
    p.add_argument("--group", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--group", required=True)
    d.add_argument(
        "--decision", choices=["accepted", "rejected", "superseded"], required=True
    )
    s = sub.add_parser("stale")
    s.add_argument("--older-than-hours", type=int, default=72)
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    conn = get_conn(args.db)
    if args.command == "export":
        print(json.dumps(export_files(conn), ensure_ascii=False, indent=2))
    elif args.command == "show":
        show(conn)
    elif args.command == "present":
        print(
            json.dumps(
                {
                    "updated": update_group(
                        conn, args.group, "presented", mark_presented=True
                    )
                },
                ensure_ascii=False,
            )
        )
    elif args.command == "decide":
        print(
            json.dumps(
                {
                    "updated": update_group(
                        conn, args.group, args.decision, decision_at=True
                    )
                },
                ensure_ascii=False,
            )
        )
    elif args.command == "stale":
        print(
            json.dumps(
                {"updated": mark_stale(conn, args.older_than_hours)}, ensure_ascii=False
            )
        )
    conn.close()


if __name__ == "__main__":
    main()
