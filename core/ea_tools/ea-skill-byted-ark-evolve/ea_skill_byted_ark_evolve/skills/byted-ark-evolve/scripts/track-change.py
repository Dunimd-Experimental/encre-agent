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
Track workspace file changes as user-direct mutations.

Called by the PostToolUse hook when Edit/Write targets a file
under the OpenClaw workspace. Records the change in evolution.db
so the evolution system is aware of user-directed modifications.

Usage (standalone):
  python track-change.py --file <path> --description "what changed"

Usage (from hook - reads stdin JSON):
  python track-change.py --from-hook
"""

import sqlite3
import argparse
import os
import sys
import json

# Windows UTF-8 fix
for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")

from _workspace import resolve_workspace_root

_WS = resolve_workspace_root()
DEFAULT_DB_PATH = os.path.join(_WS, "evolution-data/evolution.db")
WORKSPACE_ROOT = _WS


def get_db(db_path=None):
    path = db_path or DEFAULT_DB_PATH
    if not os.path.exists(path):
        print(f"Error: DB not found at {path}. Run db-init.py first.", file=sys.stderr)
        sys.exit(1)
    return sqlite3.connect(path)


def get_relative_path(abs_path):
    """Convert absolute path to workspace-relative path (forward slashes)."""
    try:
        rel = os.path.relpath(abs_path, WORKSPACE_ROOT)
        return rel.replace("\\", "/")  # Normalize for Windows consistency
    except ValueError:
        return abs_path


def is_in_workspace(file_path):
    """Check if a file path is under the OpenClaw workspace."""
    try:
        abs_path = os.path.abspath(os.path.expanduser(file_path))
        abs_workspace = os.path.abspath(WORKSPACE_ROOT)
        return abs_path.startswith(abs_workspace)
    except (ValueError, TypeError):
        return False


def is_evolution_internal(file_path):
    """Check if a file is in evolution-data/ (internal, don't track)."""
    rel = get_relative_path(file_path)
    return rel.startswith("evolution-data")


def record_user_direct(
    file_path,
    description=None,
    session_id=None,
    before_text=None,
    after_text=None,
    db_path=None,
):
    """Record a user-direct mutation."""
    conn = get_db(db_path)
    cur = conn.cursor()

    rel_path = get_relative_path(file_path)

    # Deduplicate: skip if same file was recorded in same session
    if session_id:
        cur.execute(
            """
            SELECT COUNT(*) FROM mutations
            WHERE target_file = ? AND session_id = ? AND source = 'user-direct'
        """,
            (rel_path, session_id),
        )
        if cur.fetchone()[0] > 0:
            conn.close()
            return None  # Already recorded

    cur.execute(
        """
        INSERT INTO mutations
        (target_file, mutation_type, description, before_text, after_text,
         source, status, session_id, applied_at)
        VALUES (?, 'modify', ?, ?, ?, 'user-direct', 'applied', ?, datetime('now'))
    """,
        (
            rel_path,
            description or f"User-directed change to {rel_path}",
            before_text,
            after_text,
            session_id,
        ),
    )
    conn.commit()
    mutation_id = cur.lastrowid
    conn.close()
    return mutation_id


def record_snapshot_diff(file_path, description=None, db_path=None):
    """Record a change detected by snapshot diff (unknown origin)."""
    conn = get_db(db_path)
    cur = conn.cursor()

    rel_path = get_relative_path(file_path)

    cur.execute(
        """
        INSERT INTO mutations
        (target_file, mutation_type, description,
         source, status, applied_at)
        VALUES (?, 'modify', ?, 'snapshot-diff', 'applied', datetime('now'))
    """,
        (rel_path, description or f"Change detected by snapshot diff: {rel_path}"),
    )
    conn.commit()
    mutation_id = cur.lastrowid
    conn.close()
    return mutation_id


def handle_hook_input():
    """Process PostToolUse hook stdin JSON."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        data = json.loads(raw)
    except (json.JSONDecodeError, IOError):
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    if tool_name not in ("Edit", "Write"):
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    if not file_path or not is_in_workspace(file_path):
        sys.exit(0)

    if is_evolution_internal(file_path):
        sys.exit(0)

    session_id = data.get("session_id", "")[:12]

    # For Edit, we have old_string/new_string; for Write, we have content
    if tool_name == "Edit":
        before = tool_input.get("old_string", "")
        after = tool_input.get("new_string", "")
        desc = f"Edit: {get_relative_path(file_path)}"
    else:
        before = None
        after = tool_input.get("content", "")[:500]  # Truncate large writes
        desc = f"Write: {get_relative_path(file_path)}"

    # In hook mode, silently skip if DB doesn't exist yet
    db_path = DEFAULT_DB_PATH
    if not os.path.exists(db_path):
        sys.exit(0)

    mutation_id = record_user_direct(
        file_path=file_path,
        description=desc,
        session_id=session_id,
        before_text=before,
        after_text=after,
        db_path=db_path,
    )

    if mutation_id:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": (
                            f"[Evolution] Tracked workspace change: {get_relative_path(file_path)} "
                            f"(mutation #{mutation_id}, source: user-direct)"
                        ),
                    }
                },
                ensure_ascii=False,
            )
        )


def main():
    parser = argparse.ArgumentParser(description="Track workspace file changes")
    parser.add_argument(
        "--from-hook",
        action="store_true",
        help="Read PostToolUse hook input from stdin",
    )
    parser.add_argument("--file", default=None, help="File path (standalone mode)")
    parser.add_argument("--description", default=None, help="Description of the change")
    parser.add_argument("--session", default=None, help="Session ID")
    parser.add_argument("--db", default=None, help="Path to evolution.db")
    args = parser.parse_args()

    if args.from_hook:
        handle_hook_input()
    elif args.file:
        if not is_in_workspace(args.file):
            print(f"File not in workspace: {args.file}", file=sys.stderr)
            sys.exit(1)
        mid = record_user_direct(
            file_path=args.file,
            description=args.description,
            session_id=args.session,
            db_path=args.db,
        )
        if mid:
            print(json.dumps({"status": "ok", "mutation_id": mid}))
        else:
            print(json.dumps({"status": "skipped", "reason": "duplicate"}))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
