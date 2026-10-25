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
Trajectory → Skill candidate detection.

Analyzes golden and correction trajectories to find patterns
that can be extracted into standalone skills.

Usage:
  python trajectory-skill-check.py [--db <path>]

Output: JSON with skill candidates (if any).
"""

import sqlite3
import json
import os
import sys

# Windows UTF-8 fix
for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")

from _workspace import resolve_workspace_root

DEFAULT_DB_PATH = os.path.join(resolve_workspace_root(), "evolution-data/evolution.db")

GOLDEN_THRESHOLD = 3  # ≥3 golden trajectories of same task_type → skill candidate
CORRECTION_THRESHOLD = 2  # ≥2 corrections with same root_cause → guardrail candidate


def get_db(db_path=None):
    path = db_path or DEFAULT_DB_PATH
    if not os.path.exists(path):
        print(f"Error: DB not found at {path}.", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def check_golden_candidates(conn):
    """Find task_types with enough golden trajectories to extract a skill."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT task_type, COUNT(*) as cnt
        FROM trajectories
        WHERE type = 'golden' AND task_type IS NOT NULL AND task_type != ''
        GROUP BY task_type
        HAVING cnt >= ?
        ORDER BY cnt DESC
    """,
        (GOLDEN_THRESHOLD,),
    )

    candidates = []
    for row in cur.fetchall():
        task_type = row["task_type"]
        cnt = row["cnt"]

        # Fetch the trajectories for detail
        cur.execute(
            """
            SELECT key_steps, tags
            FROM trajectories
            WHERE type = 'golden' AND task_type = ?
            ORDER BY created_at DESC
        """,
            (task_type,),
        )
        trajectories = [dict(r) for r in cur.fetchall()]

        # Extract common steps
        all_steps = []
        all_tags = set()
        for t in trajectories:
            if t.get("key_steps"):
                try:
                    steps = json.loads(t["key_steps"])
                    all_steps.append(steps)
                except (json.JSONDecodeError, TypeError):
                    all_steps.append([t["key_steps"]])
            if t.get("tags"):
                try:
                    tags = json.loads(t["tags"])
                    all_tags.update(tags)
                except (json.JSONDecodeError, TypeError):
                    all_tags.add(t["tags"])

        candidates.append(
            {
                "type": "golden_skill",
                "task_type": task_type,
                "trajectory_count": cnt,
                "tags": list(all_tags),
                "sample_steps": all_steps[:3],  # Show up to 3 examples
                "recommendation": f"Task type '{task_type}' has {cnt} successful trajectories. "
                f"Consider extracting into a standalone skill.",
            }
        )

    return candidates


def check_correction_candidates(conn):
    """Find repeated root_causes that should become guardrails."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT root_cause, COUNT(*) as cnt
        FROM trajectories
        WHERE type = 'correction' AND root_cause IS NOT NULL AND root_cause != ''
        GROUP BY root_cause
        HAVING cnt >= ?
        ORDER BY cnt DESC
    """,
        (CORRECTION_THRESHOLD,),
    )

    candidates = []
    for row in cur.fetchall():
        root_cause = row["root_cause"]
        cnt = row["cnt"]

        # Fetch correction details
        cur.execute(
            """
            SELECT error_approach, correct_approach, task_type
            FROM trajectories
            WHERE type = 'correction' AND root_cause = ?
            ORDER BY created_at DESC
        """,
            (root_cause,),
        )
        corrections = [dict(r) for r in cur.fetchall()]

        # Check related task types
        related_types = list(
            set(c["task_type"] for c in corrections if c.get("task_type"))
        )

        candidates.append(
            {
                "type": "correction_guardrail",
                "root_cause": root_cause,
                "occurrence_count": cnt,
                "related_task_types": related_types,
                "error_pattern": corrections[0].get("error_approach", ""),
                "correct_pattern": corrections[0].get("correct_approach", ""),
                "recommendation": f"Root cause '{root_cause}' appeared {cnt} times. "
                f"Consider adding guardrail to prevent recurrence.",
            }
        )

    return candidates


def main():
    db_path = None
    if "--db" in sys.argv:
        idx = sys.argv.index("--db")
        if idx + 1 < len(sys.argv):
            db_path = sys.argv[idx + 1]

    conn = get_db(db_path)

    golden = check_golden_candidates(conn)
    correction = check_correction_candidates(conn)
    conn.close()

    result = {
        "golden_skill_candidates": golden,
        "correction_guardrail_candidates": correction,
        "total_candidates": len(golden) + len(correction),
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result["total_candidates"] == 0:
        print("\nNo skill candidates detected.", file=sys.stderr)
    else:
        print(f"\n{result['total_candidates']} candidate(s) found.", file=sys.stderr)


if __name__ == "__main__":
    main()
