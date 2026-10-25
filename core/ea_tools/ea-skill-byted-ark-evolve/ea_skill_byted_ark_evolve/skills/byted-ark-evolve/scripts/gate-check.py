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

"""Check whether evolution analysis should be triggered (Gate)."""

import sqlite3
import json
import os
import sys
import argparse
from datetime import datetime, timedelta, timezone


def _utcnow_naive():
    """UTC now as a naive datetime (replacement for deprecated _utcnow_naive())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Windows UTF-8 fix
for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")

from _workspace import resolve_workspace_root

DEFAULT_DB_PATH = os.path.join(resolve_workspace_root(), "evolution-data/evolution.db")

# Gate thresholds
CORRECTION_NEGATIVE_THRESHOLD = 5
HIGH_SEVERITY_THRESHOLD = 3
SAME_LAYER_THRESHOLD = 3
DAYS_SINCE_LAST_EVOLUTION = 7
COOLDOWN_HOURS = 24


def get_db(db_path=None):
    path = db_path or DEFAULT_DB_PATH
    if not os.path.exists(path):
        print(json.dumps({"trigger": False, "reason": "DB not found"}))
        sys.exit(0)
    return sqlite3.connect(path)


def check_gate(db_path=None):
    conn = get_db(db_path)
    cur = conn.cursor()

    # Check cooldown: last evolution run
    cur.execute("SELECT MAX(created_at) FROM evolution_runs")
    row = cur.fetchone()
    last_run = row[0] if row and row[0] else None

    if last_run:
        last_run_dt = datetime.fromisoformat(last_run)
        if _utcnow_naive() - last_run_dt < timedelta(hours=COOLDOWN_HOURS):
            conn.close()
            print(
                json.dumps(
                    {
                        "trigger": False,
                        "reason": f"Cooldown: last evolution was {last_run}, wait 24h",
                        "last_run": last_run,
                    }
                )
            )
            return

    # Count unprocessed signals
    cur.execute("""
        SELECT type, layer, severity, COUNT(*) as cnt
        FROM signals WHERE processed = 0
        GROUP BY type, layer, severity
    """)
    rows = cur.fetchall()

    total_correction_negative = 0
    total_high = 0
    layer_counts = {}
    total_unprocessed = 0

    for stype, layer, severity, cnt in rows:
        total_unprocessed += cnt
        if stype in ("correction", "negative", "clarification"):
            total_correction_negative += cnt
        if severity == "high":
            total_high += cnt
        if layer:
            layer_counts[layer] = layer_counts.get(layer, 0) + cnt

    max_layer = max(layer_counts.values()) if layer_counts else 0
    max_layer_name = max(layer_counts, key=layer_counts.get) if layer_counts else None

    # Check triggers
    triggers = []

    if total_correction_negative >= CORRECTION_NEGATIVE_THRESHOLD:
        triggers.append(
            f"correction+negative+clarification >= {CORRECTION_NEGATIVE_THRESHOLD} ({total_correction_negative})"
        )

    if total_high >= HIGH_SEVERITY_THRESHOLD:
        triggers.append(f"high severity >= {HIGH_SEVERITY_THRESHOLD} ({total_high})")

    if max_layer >= SAME_LAYER_THRESHOLD:
        triggers.append(
            f"same layer ({max_layer_name}) >= {SAME_LAYER_THRESHOLD} ({max_layer})"
        )

    if last_run and total_unprocessed > 0:
        last_run_dt = datetime.fromisoformat(last_run)
        if _utcnow_naive() - last_run_dt >= timedelta(days=DAYS_SINCE_LAST_EVOLUTION):
            triggers.append(
                f">= {DAYS_SINCE_LAST_EVOLUTION} days since last evolution with pending signals"
            )
    elif not last_run and total_unprocessed > 0:
        triggers.append("No previous evolution run and signals exist")

    should_trigger = len(triggers) > 0

    # Saturation check
    cur.execute("""
        SELECT mutations_proposed FROM evolution_runs
        ORDER BY created_at DESC LIMIT 3
    """)
    recent_runs = [r[0] for r in cur.fetchall()]
    saturated = len(recent_runs) >= 3 and all(m <= 1 for m in recent_runs)

    conn.close()

    result = {
        "trigger": should_trigger,
        "reasons": triggers,
        "stats": {
            "total_unprocessed": total_unprocessed,
            "correction_negative": total_correction_negative,
            "high_severity": total_high,
            "layer_counts": layer_counts,
        },
        "last_run": last_run,
        "saturated": saturated,
    }

    if saturated:
        result["saturation_warning"] = (
            "Last 3 runs produced <= 1 mutation each. Consider reducing frequency."
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Check whether evolution analysis should be triggered"
    )
    parser.add_argument(
        "db_path_positional", nargs="?", default=None, help="Legacy positional DB path"
    )
    parser.add_argument("--db", default=None, help="Path to evolution.db")
    args = parser.parse_args()
    check_gate(args.db or args.db_path_positional)
