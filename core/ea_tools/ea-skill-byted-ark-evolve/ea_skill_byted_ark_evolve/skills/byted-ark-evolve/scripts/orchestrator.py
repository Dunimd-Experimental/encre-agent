from __future__ import annotations

"""Minimal end-to-end orchestrator for timer review and session-start surfacing."""

import argparse
import json
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _utcnow_naive():
    """UTC now as a naive datetime (replacement for deprecated datetime.utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


from _workspace import resolve_workspace_root

DEFAULT_WS = Path(resolve_workspace_root())
DEFAULT_DATA_DIR = DEFAULT_WS / "evolution-data"
DEFAULT_DB = DEFAULT_DATA_DIR / "evolution.db"
DEFAULT_PENDING_SCRIPT = Path(__file__).with_name("pending-evolution.py")
DEFAULT_DASHBOARD_SCRIPT = Path(__file__).with_name("dashboard-render.py")

for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")


def conn(db_path):
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    return c


def ensure_db(db_path):
    if db_path.exists():
        return
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("db-init.py")),
            "--db",
            str(db_path),
        ],
        check=True,
    )


GENE_LIBRARY_PATH = (
    Path(__file__).resolve().parent.parent / "references" / "gene-library.json"
)


def load_gene_library():
    if not GENE_LIBRARY_PATH.exists():
        return []
    return json.loads(GENE_LIBRARY_PATH.read_text(encoding="utf-8"))


def select_signals(c, limit=10, include_positive=False):
    cur = c.cursor()
    if include_positive:
        cur.execute(
            """SELECT * FROM signals WHERE processed = 0
            AND type IN ('correction','negative','suggestion','preference','clarification','positive')
            ORDER BY
                CASE type WHEN 'positive' THEN 1 ELSE 0 END,
                CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                created_at ASC
            LIMIT ?""",
            (limit,),
        )
    else:
        cur.execute(
            """SELECT * FROM signals WHERE processed = 0
            AND type IN ('correction','negative','suggestion','preference','clarification')
            ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                created_at ASC
            LIMIT ?""",
            (limit,),
        )
    return [dict(r) for r in cur.fetchall()]


def create_review(c, reason):
    review_id = (
        f"review-{_utcnow_naive().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    )
    cur = c.cursor()
    cur.execute(
        "INSERT INTO reviews (review_id, status, run_reason, worker_started_at) VALUES (?, 'running', ?, datetime('now'))",
        (review_id, reason),
    )
    c.commit()
    return review_id


def write_workset(data_dir, review_id, signals, genes):
    tmp = data_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / f"workset-{review_id}.json"
    payload = {
        "review_id": review_id,
        "generated_at": _utcnow_naive().isoformat() + "Z",
        "signals": signals,
        "genes": genes,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_mock_worker(data_dir, review_id, signals, genes):
    tmp = data_dir / "tmp"
    selected = []
    proposals = []
    if signals:
        gene = genes[0] if genes else None
        group_id = f"prop-{review_id}"
        summary = "根据近期反馈新增执行前检查与help优先规则"
        for sig in signals[:2]:
            selected.append(
                {
                    "signal_id": sig["id"],
                    "gene_id": (gene or {}).get("id", "fallback-no-gene"),
                    "reason": "该方案可减少重复错误，优先约束执行前检查和帮助优先。",
                    "judgment_type": "root_cause_fit",
                    "confidence": 0.78,
                }
            )
        proposals.append(
            {
                "proposal_group_id": group_id,
                "proposal_summary": summary,
                "mutations": [
                    {
                        "target_file": "AGENTS.md",
                        "mutation_type": "add",
                        "layer": "protocol",
                        "description": "新建执行前检查与 help 优先规则",
                        "before_text": None,
                        "after_text": "## 执行前检查\n- 参数报错先看 --help\n- 执行 python 前先检查 .venv\n",
                        "signal_ids": [s["id"] for s in signals[:2]],
                        "status": "proposed",
                        "gene_id": (gene or {}).get("id", "fallback-no-gene"),
                        "gene_reason": "如果提前应用，可减少当前错误再次发生",
                    }
                ],
            }
        )
    cand_path = tmp / f"gene-candidates-{review_id}.json"
    prop_path = tmp / f"proposals-{review_id}.json"
    cand_path.write_text(
        json.dumps({"selected_genes": selected}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    prop_path.write_text(
        json.dumps({"proposals": proposals}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return cand_path, prop_path


def run_agent_worker(data_dir, review_id, workset_path):
    tmp = data_dir / "tmp"
    cand = tmp / f"gene-candidates-{review_id}.json"
    prop = tmp / f"proposals-{review_id}.json"
    message = f"""Read the JSON workset at: {workset_path}

Task:
1. Analyze the signals and available genes.
2. Write {cand} with JSON shape {{"selected_genes": [{{"signal_id": 1, "gene_id": "...", "reason": "...", "judgment_type": "root_cause_fit|surface_relief|fallback_only", "confidence": 0.8}}]}}
3. Write {prop} with JSON shape {{"proposals": [{{"proposal_group_id": "prop-...", "proposal_summary": "...", "mutations": [{{"target_file": "AGENTS.md", "mutation_type": "add|modify|remove", "layer": "protocol", "description": "...", "before_text": null, "after_text": "...", "signal_ids": [1], "status": "proposed", "gene_id": "...", "gene_reason": "..."}}]}}]}}
4. Do not apply changes to workspace files. Only write these two JSON files.
5. Reply with exactly: DONE
"""
    res = subprocess.run(
        [
            "openclaw",
            "agent",
            "--local",
            "--session-id",
            f"evo-{review_id}",
            "--message",
            message,
            "--json",
            "--thinking",
            "low",
            "--timeout",
            "180",
        ],
        capture_output=True,
        text=True,
    )
    return {
        "returncode": res.returncode,
        "stdout": res.stdout,
        "stderr": res.stderr,
        "cand": cand,
        "prop": prop,
    }


def ingest_outputs(c, data_dir, review_id):
    tmp = data_dir / "tmp"
    cand = json.loads(
        (tmp / f"gene-candidates-{review_id}.json").read_text(encoding="utf-8")
    )
    prop = json.loads((tmp / f"proposals-{review_id}.json").read_text(encoding="utf-8"))
    cur = c.cursor()
    for item in cand.get("selected_genes", []):
        cur.execute(
            "INSERT INTO gene_matches (review_id, signal_id, gene_id, reason, judgment_type, confidence) VALUES (?, ?, ?, ?, ?, ?)",
            (
                review_id,
                item.get("signal_id"),
                item.get("gene_id"),
                item.get("reason"),
                item.get("judgment_type", "root_cause_fit"),
                item.get("confidence"),
            ),
        )
    mutation_count = 0
    for proposal in prop.get("proposals", []):
        group_id = proposal.get("proposal_group_id") or f"prop-{review_id}"
        summary = proposal.get("proposal_summary") or "未命名提案"
        for m in proposal.get("mutations", []):
            cur.execute(
                """
                INSERT INTO mutations (
                    review_id, proposal_group_id, proposal_summary, proposal_status,
                    target_file, mutation_type, layer, description, before_text, after_text,
                    signal_ids, source, status, gene_id, gene_reason, session_id
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, 'evolution', ?, ?, ?, ?)
            """,
                (
                    review_id,
                    group_id,
                    summary,
                    m.get("target_file"),
                    m.get("mutation_type"),
                    m.get("layer"),
                    m.get("description"),
                    m.get("before_text"),
                    m.get("after_text"),
                    json.dumps(m.get("signal_ids", []), ensure_ascii=False),
                    m.get("status", "proposed"),
                    m.get("gene_id"),
                    m.get("gene_reason"),
                    f"evo-{review_id}",
                ),
            )
            mutation_count += 1
            for sid in m.get("signal_ids", []):
                cur.execute("UPDATE signals SET processed = 1 WHERE id = ?", (sid,))
    c.commit()
    return mutation_count


def complete_review(c, review_id, pending_summary):
    cur = c.cursor()
    cur.execute(
        "UPDATE reviews SET status = 'completed', worker_finished_at = datetime('now'), pending_summary_json = ? WHERE review_id = ?",
        (json.dumps(pending_summary, ensure_ascii=False), review_id),
    )
    c.commit()


def fail_review(c, review_id, error_text):
    cur = c.cursor()
    cur.execute(
        "UPDATE reviews SET status = 'failed', worker_finished_at = datetime('now'), worker_error = ? WHERE review_id = ?",
        (str(error_text)[:4000], review_id),
    )
    c.commit()


def export_pending(pending_script, db_path):
    subprocess.run(
        [sys.executable, str(pending_script), "--db", str(db_path), "export"],
        check=True,
        capture_output=True,
        text=True,
    )


def render_dashboard(db_path, data_dir):
    """Regenerate dashboard.html from DB. Non-fatal if it fails."""
    try:
        res = subprocess.run(
            [
                sys.executable,
                str(DEFAULT_DASHBOARD_SCRIPT),
                "--db",
                str(db_path),
                "--data-dir",
                str(data_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return {"status": "ok", "output": res.stdout.strip()}
    except Exception as exc:
        print(f"[warn] dashboard render failed: {exc}", file=sys.stderr)
        return {"status": "failed", "error": str(exc)}


def summarize_session_start(c):
    cur = c.cursor()
    cur.execute("""
        SELECT proposal_group_id, proposal_summary, proposal_status, COUNT(*) as mutation_count, MIN(created_at) as created_at
        FROM mutations
        WHERE proposal_status IN ('pending','presented')
        GROUP BY proposal_group_id, proposal_summary, proposal_status
        ORDER BY MIN(created_at) ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    for row in rows:
        if row["proposal_status"] == "pending":
            cur.execute(
                "UPDATE mutations SET proposal_status='presented', presented_at=datetime('now') WHERE proposal_group_id = ? AND proposal_status='pending'",
                (row["proposal_group_id"],),
            )
            row["proposal_status"] = "presented"
    c.commit()
    if not rows:
        return {"pending_count": 0, "message": "当前没有未处理的进化提案。"}
    lines = [f"当前有 {len(rows)} 组未处理的进化提案："]
    for idx, row in enumerate(rows, 1):
        lines.append(
            f"{idx}. {row['proposal_summary']}（{row['mutation_count']} 条变更，状态：{row['proposal_status']}（"
        )
    lines.append("你可以接受、拒绝，或者让我展开详情。")
    return {"pending_count": len(rows), "message": "\n".join(lines), "items": rows}


def main():
    parser = argparse.ArgumentParser(
        description="Timer/session_start orchestrator for evolution skill v0.3.1"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--pending-script", default=str(DEFAULT_PENDING_SCRIPT))
    sub = parser.add_subparsers(dest="command")
    t = sub.add_parser("timer-review")
    t.add_argument("--reason", default="timer")
    t.add_argument("--signal-limit", type=int, default=10)
    t.add_argument("--worker-mode", choices=["agent", "mock"], default="agent")
    t.add_argument(
        "--skip-gate",
        action="store_true",
        default=False,
        help="Skip gate check and cooldown (for manual /evolve)",
    )
    sub.add_parser("session-start")
    ap = sub.add_parser("apply-proposal")
    ap.add_argument("--group", required=True)
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    db_path = Path(args.db)
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    ensure_db(db_path)
    c = conn(db_path)
    try:
        if args.command == "timer-review":
            if not args.skip_gate:
                gate_res = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("gate-check.py")),
                        "--db",
                        str(db_path),
                    ],
                    capture_output=True,
                    text=True,
                )
                try:
                    gate = json.loads(gate_res.stdout)
                except (json.JSONDecodeError, ValueError):
                    gate = {"trigger": False, "reason": "gate-check failed"}
                if not gate.get("trigger"):
                    print(
                        json.dumps(
                            {
                                "status": "noop",
                                "reason": "gate-not-triggered",
                                "gate": gate,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return
            signals = select_signals(c, args.signal_limit, include_positive=True)
            if not signals:
                print(
                    json.dumps(
                        {"status": "noop", "reason": "no-unprocessed-signals"},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return
            genes = load_gene_library()
            review_id = create_review(c, args.reason)
            workset = write_workset(data_dir, review_id, signals, genes)
            if args.worker_mode == "mock":
                run_mock_worker(data_dir, review_id, signals, genes)
            else:
                agent_res = run_agent_worker(data_dir, review_id, workset)
                if (
                    agent_res["returncode"] != 0
                    or not agent_res["cand"].exists()
                    or not agent_res["prop"].exists()
                ):
                    fail_review(
                        c,
                        review_id,
                        agent_res["stderr"]
                        or agent_res["stdout"]
                        or "agent worker failed",
                    )
                    print(
                        json.dumps(
                            {
                                "status": "failed",
                                "review_id": review_id,
                                "agent": agent_res,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    return
            mutation_count = ingest_outputs(c, data_dir, review_id)
            export_pending(Path(args.pending_script), db_path)
            summary = {"mutation_count": mutation_count}
            complete_review(c, review_id, summary)
            render_dashboard(db_path, data_dir)
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "review_id": review_id,
                        "signals": len(signals),
                        "mutations": mutation_count,
                        "worker_mode": args.worker_mode,
                        "workset": str(workset),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "session-start":
            export_pending(Path(args.pending_script), db_path)
            render_dashboard(db_path, data_dir)
            print(json.dumps(summarize_session_start(c), ensure_ascii=False, indent=2))
        elif args.command == "apply-proposal":
            res = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("apply-proposal.py")),
                    "--group",
                    args.group,
                    "--db",
                    str(db_path),
                    "--workspace-root",
                    str(DEFAULT_WS),
                ],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "stderr": res.stderr,
                            "stdout": res.stdout,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                export_pending(Path(args.pending_script), db_path)
                render_dashboard(db_path, data_dir)
                print(res.stdout.strip())
    finally:
        c.close()


if __name__ == "__main__":
    main()
