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

"""Record an evolution signal to SQLite."""

import sqlite3
import argparse
import os
import sys
import json
from datetime import datetime

# Windows UTF-8 fix
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
    return sqlite3.connect(path)


def record_signal(args):
    conn = get_db(args.db)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO signals (session_id, type, layer, severity, raw_text, context)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
        (
            args.session or f"session_{datetime.now().strftime('%Y%m%d_%H%M')}",
            args.type,
            args.layer,
            args.severity,
            args.text,
            args.context,
        ),
    )
    conn.commit()
    signal_id = cur.lastrowid
    conn.close()
    print(
        json.dumps(
            {
                "status": "ok",
                "signal_id": signal_id,
                "type": args.type,
                "layer": args.layer,
                "severity": args.severity,
            },
            ensure_ascii=False,
        )
    )


def main():
    parser = argparse.ArgumentParser(description="Record an evolution signal")
    parser.add_argument(
        "--type",
        required=True,
        choices=[
            "correction",
            "negative",
            "positive",
            "suggestion",
            "preference",
            "clarification",
        ],
        help="Signal type",
    )
    parser.add_argument(
        "--layer",
        default=None,
        choices=["identity", "context", "protocol", "capability", "runtime"],
        help="Target layer",
    )
    parser.add_argument(
        "--severity",
        default="medium",
        choices=["low", "medium", "high"],
        help="Signal severity",
    )
    parser.add_argument("--text", required=True, help="User's original feedback text")
    parser.add_argument(
        "--context", default=None, help="What was happening when this signal occurred"
    )
    parser.add_argument("--session", default=None, help="Session ID")
    parser.add_argument("--db", default=None, help="Path to evolution.db")
    args = parser.parse_args()
    record_signal(args)


if __name__ == "__main__":
    main()
