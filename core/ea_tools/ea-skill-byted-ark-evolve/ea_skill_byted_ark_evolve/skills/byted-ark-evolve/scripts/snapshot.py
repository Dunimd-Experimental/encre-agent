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
Workspace file snapshot - hash-based change detection.

Creates a JSON snapshot of all workspace files (path → SHA256 hash).
Used as fallback (Plan B) to detect changes that hooks missed.

Usage:
  python snapshot.py save              # Save current snapshot
  python snapshot.py diff              # Diff current vs saved snapshot
  python snapshot.py diff --record     # Diff and record changes to DB
"""

import hashlib
import json
import os
import sys
import sqlite3
from datetime import datetime

# Windows UTF-8 fix
for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")

from _workspace import resolve_workspace_root

WORKSPACE_ROOT = resolve_workspace_root()
EVOLUTION_DATA = os.path.join(WORKSPACE_ROOT, "evolution-data")
SNAPSHOT_PATH = os.path.join(EVOLUTION_DATA, "snapshot.json")
DEFAULT_DB_PATH = os.path.join(EVOLUTION_DATA, "evolution.db")

# Directories to skip when scanning
SKIP_DIRS = {".git", "node_modules", "__pycache__", "evolution-data", ".venv"}

# File extensions to track
TRACK_EXTENSIONS = {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".py",
    ".js",
    ".ts",
    ".sh",
    ".html",
    ".css",
}


def hash_file(path):
    """SHA256 hash of file contents."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (IOError, PermissionError):
        return None


def scan_workspace():
    """Scan workspace and return {relative_path: sha256_hash}."""
    files = {}
    for root, dirs, filenames in os.walk(WORKSPACE_ROOT):
        # Skip excluded directories
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in TRACK_EXTENSIONS:
                continue

            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, WORKSPACE_ROOT)
            # Normalize path separators
            rel_path = rel_path.replace("\\", "/")
            file_hash = hash_file(abs_path)
            if file_hash:
                files[rel_path] = file_hash

    return files


def save_snapshot():
    """Save current workspace state to snapshot.json."""
    os.makedirs(EVOLUTION_DATA, exist_ok=True)
    files = scan_workspace()
    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "file_count": len(files),
        "files": files,
    }
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    print(f"Snapshot saved: {len(files)} files")
    return snapshot


def load_snapshot():
    """Load saved snapshot. Returns None if doesn't exist."""
    if not os.path.exists(SNAPSHOT_PATH):
        return None
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def diff_snapshot(record=False, db_path=None):
    """Compare current workspace against saved snapshot.

    Returns dict with added/modified/removed files.
    If record=True, writes snapshot-diff mutations to DB.
    """
    saved = load_snapshot()
    if not saved:
        print("No snapshot found. Run 'snapshot.py save' first.")
        return None

    current = scan_workspace()
    saved_files = saved.get("files", {})

    added = [f for f in current if f not in saved_files]
    removed = [f for f in saved_files if f not in current]
    modified = [f for f in current if f in saved_files and current[f] != saved_files[f]]

    result = {
        "snapshot_time": saved.get("timestamp"),
        "diff_time": datetime.now().isoformat(),
        "added": added,
        "modified": modified,
        "removed": removed,
        "total_changes": len(added) + len(modified) + len(removed),
    }

    if result["total_changes"] == 0:
        print("No changes detected since last snapshot.")
        return result

    print(f"Changes since {saved.get('timestamp')}:")
    if added:
        print(f"  Added ({len(added)}):")
        for f in added:
            print(f"    + {f}")
    if modified:
        print(f"  Modified ({len(modified)}):")
        for f in modified:
            print(f"    ~ {f}")
    if removed:
        print(f"  Removed ({len(removed)}):")
        for f in removed:
            print(f"    - {f}")

    if record:
        record_diff_mutations(added, modified, removed, db_path)

    return result


def record_diff_mutations(added, modified, removed, db_path=None):
    """Record snapshot-diff changes as mutations in DB."""
    db = db_path or DEFAULT_DB_PATH
    if not os.path.exists(db):
        print(f"DB not found: {db}. Skipping mutation recording.", file=sys.stderr)
        return

    # Check which files already have user-direct mutations (avoid duplicates)
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    recorded = 0

    for file_path in added + modified:
        # Skip if already tracked as user-direct in recent history
        cur.execute(
            """
            SELECT COUNT(*) FROM mutations
            WHERE target_file = ? AND source = 'user-direct'
            AND created_at > datetime('now', '-7 days')
        """,
            (file_path,),
        )
        if cur.fetchone()[0] > 0:
            continue  # Already tracked by hook

        mutation_type = "add" if file_path in added else "modify"
        cur.execute(
            """
            INSERT INTO mutations
            (target_file, mutation_type, description,
             source, status, applied_at)
            VALUES (?, ?, ?, 'snapshot-diff', 'applied', datetime('now'))
        """,
            (
                file_path,
                mutation_type,
                f"快照检测到变更: {file_path} ({mutation_type})",
            ),
        )
        recorded += 1

    for file_path in removed:
        cur.execute(
            """
            INSERT INTO mutations
            (target_file, mutation_type, description,
             source, status, applied_at)
            VALUES (?, 'remove', ?, 'snapshot-diff', 'applied', datetime('now'))
        """,
            (file_path, f"文件已移除: {file_path}"),
        )
        recorded += 1

    conn.commit()
    conn.close()
    print(f"Recorded {recorded} snapshot-diff mutations to DB.")


def main():
    if len(sys.argv) < 2:
        print("Usage: snapshot.py save|diff [--record] [--db <path>]")
        sys.exit(1)

    command = sys.argv[1]
    record = "--record" in sys.argv
    db_path = None
    if "--db" in sys.argv:
        idx = sys.argv.index("--db")
        if idx + 1 < len(sys.argv):
            db_path = sys.argv[idx + 1]

    if command == "save":
        save_snapshot()
    elif command == "diff":
        diff_snapshot(record=record, db_path=db_path)
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
