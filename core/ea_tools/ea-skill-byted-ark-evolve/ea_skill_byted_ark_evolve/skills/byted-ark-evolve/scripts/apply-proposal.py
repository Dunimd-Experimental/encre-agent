from __future__ import annotations

"""Apply an accepted proposal group, commit snapshot, and write back result."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from _workspace import resolve_workspace_root

_WS = resolve_workspace_root()
DEFAULT_DB_PATH = os.path.join(_WS, "evolution-data/evolution.db")
WORKSPACE_ROOT = _WS
SNAPSHOT_SCRIPT = str(Path(__file__).with_name("snapshot.py"))
DASHBOARD_SCRIPT = str(Path(__file__).with_name("dashboard-render.py"))

for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")


def conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def ensure_git_repo(workspace_root):
    git_dir = Path(workspace_root) / ".git"
    if git_dir.exists():
        return True
    subprocess.run(
        ["git", "init"], cwd=workspace_root, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "config", "user.email", "byted-ark-evolve@local"],
        cwd=workspace_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Evolution Skill"],
        cwd=workspace_root,
        check=True,
    )
    return True


def group_rows(c, group_id):
    cur = c.cursor()
    cur.execute(
        "SELECT * FROM mutations WHERE proposal_group_id = ? ORDER BY id", (group_id,)
    )
    return [dict(r) for r in cur.fetchall()]


def apply_mutation(workspace_root, m):
    target = Path(workspace_root) / m["target_file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    before = target.read_text(encoding="utf-8") if target.exists() else ""
    mutation_type = m.get("mutation_type")
    if mutation_type == "add":
        new_text = before
        if before and not before.endswith("\n"):
            new_text += "\n"
        new_text += m.get("after_text") or ""
    elif mutation_type == "modify":
        before_text = m.get("before_text") or ""
        after_text = m.get("after_text") or ""
        if before_text and before_text in before:
            new_text = before.replace(before_text, after_text, 1)
        else:
            raise RuntimeError(f"before_text not found in {m['target_file']}")
    elif mutation_type == "remove":
        before_text = m.get("before_text") or ""
        if before_text and before_text in before:
            new_text = before.replace(before_text, "", 1)
        else:
            raise RuntimeError(f"before_text not found in {m['target_file']}")
    else:
        raise RuntimeError(f"unknown mutation_type: {mutation_type}")
    target.write_text(new_text, encoding="utf-8")
    return str(target)


def check_positive_conflict(c, target_file):
    """检查待修改的文件是否有被 positive 信号验证过的行为。"""
    cur = c.cursor()
    cur.execute(
        """
        SELECT id, description FROM mutations
        WHERE target_file = ? AND status = 'verified' AND source = 'positive-anchor'
    """,
        (target_file,),
    )
    conflicts = [dict(r) for r in cur.fetchall()]
    if conflicts:
        return {
            "has_conflict": True,
            "verified_behaviors": conflicts,
            "warning": f"target {target_file} has {len(conflicts)} verified positive behavior(s), modification may cause regression",
        }
    return {"has_conflict": False}


def commit_group(workspace_root, group_id, files):
    ensure_git_repo(workspace_root)
    subprocess.run(["git", "add", "--", *files], cwd=workspace_root, check=True)
    msg = f"evolution: apply proposal {group_id}"
    subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=workspace_root,
        check=True,
        capture_output=True,
        text=True,
    )
    res = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


def save_snapshot(db_path):
    subprocess.run(
        [sys.executable, SNAPSHOT_SCRIPT, "save", "--db", db_path],
        check=False,
        capture_output=True,
        text=True,
    )


def render_dashboard(db_path, data_dir):
    try:
        subprocess.run(
            [
                sys.executable,
                DASHBOARD_SCRIPT,
                "--db",
                str(db_path),
                "--data-dir",
                str(data_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        print(f"[warn] dashboard render failed: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Apply accepted proposal group")
    parser.add_argument("--group", required=True)
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--workspace-root", default=WORKSPACE_ROOT)
    args = parser.parse_args()

    c = conn(args.db)
    cur = c.cursor()
    rows = group_rows(c, args.group)
    if not rows:
        raise SystemExit("proposal group not found")
    if any(
        r.get("proposal_status") not in ("accepted", "presented", "pending")
        for r in rows
    ):
        raise SystemExit("proposal group has invalid state for apply")

    applied_ids = []
    failed = []
    changed_files = []
    positive_warnings = []
    for row in rows:
        try:
            conflict = check_positive_conflict(c, row["target_file"])
            if conflict.get("has_conflict"):
                positive_warnings.append(
                    {
                        "mutation_id": row["id"],
                        "target_file": row["target_file"],
                        "warning": conflict["warning"],
                    }
                )
            path = apply_mutation(args.workspace_root, row)
            changed_files.append(os.path.relpath(path, args.workspace_root))
            cur.execute(
                "UPDATE mutations SET status='applied', proposal_status='accepted', applied_at=datetime('now'), decision_at=COALESCE(decision_at, datetime('now')) WHERE id = ?",
                (row["id"],),
            )
            applied_ids.append(row["id"])
        except Exception as exc:
            failed.append({"id": row["id"], "error": str(exc)})
    commit_hash = None
    result_status = "failed"
    if applied_ids:
        commit_hash = commit_group(args.workspace_root, args.group, changed_files)
        save_snapshot(args.db)
        result_status = "partial" if failed else "applied"
    review_id = rows[0].get("review_id")
    if review_id:
        payload = {
            "proposal_group_id": args.group,
            "result_status": result_status,
            "commit_hash": commit_hash,
            "applied_mutation_ids": applied_ids,
            "failed_mutation_ids": [x["id"] for x in failed],
            "changed_files": changed_files,
        }
        cur.execute(
            "UPDATE reviews SET pending_summary_json = ?, worker_finished_at = datetime('now') WHERE review_id = ?",
            (json.dumps(payload, ensure_ascii=False), review_id),
        )
    c.commit()
    c.close()
    # Always render dashboard after applying
    data_dir = str(Path(args.db).parent)
    render_dashboard(args.db, data_dir)
    print(
        json.dumps(
            {
                "status": result_status,
                "commit_hash": commit_hash,
                "applied_mutation_ids": applied_ids,
                "failed": failed,
                "changed_files": changed_files,
                "positive_warnings": positive_warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
