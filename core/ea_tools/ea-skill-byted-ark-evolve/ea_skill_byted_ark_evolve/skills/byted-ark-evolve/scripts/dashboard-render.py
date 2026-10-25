from __future__ import annotations

"""
Render evolution dashboard from SQLite DB + bundled gene library.

Reads data from DB, injects JSON into the frozen HTML template.
The template handles all rendering via JS - this script never touches HTML/CSS.

Usage:
  python dashboard-render.py --db evolution.db --output dashboard.html
  python dashboard-render.py --db evolution.db --data-dir evolution-data/
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")

TEMPLATE_PATH = Path(__file__).with_name("dashboard-template.html")
from _workspace import resolve_workspace_root

_WS = Path(resolve_workspace_root())
DEFAULT_DB = _WS / "evolution-data/evolution.db"
DEFAULT_DATA_DIR = _WS / "evolution-data"


def conn(db_path):
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    return c


def fmt_date(dt_str):
    """Convert '2026-03-22 10:15:00' to '3/22'."""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return f"{dt.month}/{dt.day}"
    except Exception:
        return dt_str[:10] if len(dt_str) >= 10 else dt_str


def fmt_date_cn(dt_str):
    """Convert to '3月2日 10:15' format."""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return f"{dt.month}月{dt.day}日 {dt.hour:02d}:{dt.minute:02d}"
    except Exception:
        return dt_str


def split_gene_ids(gene_id_str):
    """Split comma-separated gene_id string into individual IDs."""
    if not gene_id_str:
        return []
    return [gid.strip() for gid in gene_id_str.split(",") if gid.strip()]


def week_label(dt_str):
    """Convert date to 'M/D' week label."""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        # Monday of that week
        monday = dt - timedelta(days=dt.weekday())
        return f"{monday.month}/{monday.day}"
    except Exception:
        return ""


def build_overview(c):
    """Build overview tab data from DB."""
    cur = c.cursor()

    # Basic counts
    cur.execute(
        "SELECT COUNT(DISTINCT session_id) FROM signals WHERE session_id IS NOT NULL"
    )
    session_counts = cur.fetchone()[0] or 0

    cur.execute("SELECT COUNT(*) FROM signals")
    signals_total = cur.fetchone()[0] or 0

    cur.execute(
        "SELECT COUNT(*) FROM signals WHERE created_at >= date('now', '-7 days')"
    )
    signals_week = cur.fetchone()[0] or 0

    cur.execute("SELECT COUNT(*) FROM reviews WHERE status = 'completed'")
    evo_runs = cur.fetchone()[0] or 0

    cur.execute("SELECT MAX(created_at) FROM reviews WHERE status = 'completed'")
    last_evo_row = cur.fetchone()
    last_evo = (
        fmt_date_cn(last_evo_row[0]) if last_evo_row and last_evo_row[0] else "日"
    )

    cur.execute("SELECT COUNT(*) FROM mutations WHERE status = 'applied'")
    applied = cur.fetchone()[0] or 0

    cur.execute(
        "SELECT COUNT(*) FROM mutations WHERE status IN ('proposed', 'approved', 'applied', 'verified')"
    )
    total_proposed = cur.fetchone()[0] or 1
    hit_rate = round(applied / total_proposed * 100) if total_proposed > 0 else 0

    cur.execute("SELECT COUNT(DISTINCT gene_id) FROM gene_matches")
    genes_matched = cur.fetchone()[0] or 0

    cur.execute("SELECT COUNT(*) FROM mutations WHERE status = 'verified'")
    verified = cur.fetchone()[0] or 0

    verify_rate = round(verified / applied * 100) if applied > 0 else 0

    cards = [
        {
            "label": "总会话数",
            "value": session_counts,
            "delta": f"+{signals_week} 本周反馈",
            "delta_up": signals_week > 0,
        },
        {
            "label": "收集反馈",
            "value": signals_total,
            "cls": "accent",
            "delta": f"+{signals_week} 鏈懆",
            "delta_up": signals_week > 0,
        },
        {"label": "进化执行", "value": evo_runs, "delta": f"上次 {last_evo}"},
        {
            "label": "已应用改动",
            "value": applied,
            "cls": "accent",
            "delta": f"命中率 {hit_rate}%",
            "delta_up": True,
        },
        {"label": "关联基因", "value": genes_matched},
        {
            "label": "验证通过",
            "value": verified,
            "cls": "accent",
            "delta": f"通过率 {verify_rate}%",
            "delta_up": verify_rate > 0,
        },
    ]

    # Trend: signals and mutations per week (last 8 weeks)
    trend = []
    for i in range(7, -1, -1):
        now_utc = datetime.now(timezone.utc)
        week_start = (now_utc - timedelta(weeks=i)).strftime("%Y-%m-%d")
        week_end = (
            (now_utc - timedelta(weeks=i - 1)).strftime("%Y-%m-%d")
            if i > 0
            else (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")
        )
        cur.execute(
            "SELECT COUNT(*) FROM signals WHERE created_at >= ? AND created_at < ?",
            (week_start, week_end),
        )
        sig_count = cur.fetchone()[0] or 0
        cur.execute(
            "SELECT COUNT(*) FROM mutations WHERE created_at >= ? AND created_at < ?",
            (week_start, week_end),
        )
        mut_count = cur.fetchone()[0] or 0
        dt = datetime.fromisoformat(week_start)
        trend.append(
            {
                "label": f"{dt.month}/{dt.day}",
                "signals": sig_count,
                "mutations": mut_count,
            }
        )

    # Verified items
    cur.execute(
        "SELECT target_file, description FROM mutations WHERE status = 'verified' ORDER BY applied_at DESC LIMIT 20"
    )
    verified_items = [
        {"file": r["target_file"], "desc": r["description"] or ""}
        for r in cur.fetchall()
    ]

    # Pending items
    cur.execute(
        "SELECT target_file, description FROM mutations WHERE status IN ('applied', 'proposed', 'approved') AND status != 'verified' ORDER BY created_at DESC LIMIT 20"
    )
    pending_items = [
        {"file": r["target_file"], "desc": r["description"] or ""}
        for r in cur.fetchall()
    ]

    # Layer distribution
    layers = {"identity": 0, "context": 0, "protocol": 0, "capability": 0, "runtime": 0}
    cur.execute(
        "SELECT layer, COUNT(*) as cnt FROM signals WHERE layer IS NOT NULL GROUP BY layer"
    )
    for r in cur.fetchall():
        if r["layer"] in layers:
            layers[r["layer"]] = r["cnt"]

    return {
        "cards": cards,
        "trend": trend,
        "verified_items": verified_items,
        "pending_items": pending_items,
        "layers": layers,
    }


def build_reviews(c):
    """Build activity tab data - one entry per completed review."""
    cur = c.cursor()
    cur.execute(
        "SELECT * FROM reviews WHERE status = 'completed' ORDER BY created_at DESC LIMIT 20"
    )
    reviews_rows = [dict(r) for r in cur.fetchall()]

    reviews = []
    for rev in reviews_rows:
        review_id = rev["review_id"]
        date_label = fmt_date(rev["created_at"])

        # Get mutations for this review
        cur.execute(
            "SELECT * FROM mutations WHERE review_id = ? ORDER BY id", (review_id,)
        )
        mutations_rows = [dict(r) for r in cur.fetchall()]

        # Get signals linked to these mutations
        signal_ids = set()
        for m in mutations_rows:
            try:
                sids = json.loads(m.get("signal_ids") or "[]")
                signal_ids.update(sids)
            except Exception:
                pass

        signals = []
        if signal_ids:
            placeholders = ",".join("?" for _ in signal_ids)
            cur.execute(
                f"SELECT * FROM signals WHERE id IN ({placeholders}) ORDER BY id",
                list(signal_ids),
            )
            for s in cur.fetchall():
                meta_parts = []
                if s["layer"]:
                    meta_parts.append(s["layer"].capitalize())
                sev_cn = {"high": "High", "medium": "Medium", "low": "Low"}.get(
                    s["severity"], ""
                )
                if sev_cn:
                    meta_parts.append(sev_cn)
                signals.append(
                    {
                        "type": s["type"],
                        "text": s["raw_text"],
                        "meta": " \u00b7 ".join(meta_parts),
                    }
                )

        # Build mutations list
        mutations = []
        applied_count = 0
        rejected_count = 0
        genes_linked = set()
        for m in mutations_rows:
            status = m.get("status", "proposed")
            if status in ("applied", "verified"):
                applied_count += 1
            if m.get("proposal_status") == "rejected" or status == "rejected":
                status = "rejected"
                rejected_count += 1
            if m.get("gene_id"):
                for _gid in split_gene_ids(m["gene_id"]):
                    genes_linked.add(_gid)

            diff_lines = []
            if m.get("after_text"):
                for line in m["after_text"].split("\n"):
                    if line.strip():
                        diff_lines.append(f"+ {line}")
            if m.get("before_text") and m.get("mutation_type") in ("modify", "remove"):
                for line in m["before_text"].split("\n"):
                    if line.strip():
                        diff_lines.insert(0, f"- {line}")

            mutations.append(
                {
                    "target_file": m["target_file"],
                    "status": status,
                    "description": m.get("description") or "",
                    "reason": m.get("gene_reason") or "",
                    "diff_lines": diff_lines,
                    "gene_id": m.get("gene_id") or "",
                    "gene_name": "",  # filled later from gene cache
                }
            )

        # Summary
        total_signals = len(signals)
        total_mutations = len(mutations)
        stats = [
            {"value": total_signals, "label": "反馈处理"},
            {"value": total_mutations, "label": "提出改动"},
            {"value": applied_count, "label": "applied"},
        ]
        if rejected_count:
            stats.append({"value": rejected_count, "label": "rejected"})
        if genes_linked:
            stats.append({"value": len(genes_linked), "label": "关联基因"})

        summary_parts = [
            f"处理了 <strong>{total_signals} 条反馈</strong>，产生了 <strong>{total_mutations} 条改动</strong>"
        ]
        if applied_count and rejected_count:
            summary_parts.append(
                f"Applied {applied_count}, rejected {rejected_count}."
            )
        elif applied_count:
            summary_parts.append("All applied.")
        elif total_mutations:
            summary_parts.append("Pending confirmation.")

        reviews.append(
            {
                "review_id": review_id,
                "date_label": date_label,
                "summary_html": " ".join(summary_parts),
                "stats": stats,
                "signals": signals,
                "mutations": mutations,
            }
        )

    return reviews


def build_genes(c, data_dir):
    """Build genes tab data from gene_matches + bundled gene-library.json."""
    cur = c.cursor()

    # Load static gene library (bundled with the skill release)
    gene_cache = {}
    library_path = (
        Path(__file__).resolve().parent.parent / "references" / "gene-library.json"
    )
    if library_path.exists():
        try:
            raw = json.loads(library_path.read_text(encoding="utf-8"))
            genes_list = raw if isinstance(raw, list) else raw.get("genes", [])
            for g in genes_list:
                gid = g.get("id") or g.get("gene_id") or ""
                gene_cache[gid] = g
        except Exception:
            pass

    # Get hit counts per gene
    cur.execute("""
        SELECT gene_id, COUNT(*) as hits, MAX(created_at) as last_used
        FROM gene_matches
        WHERE gene_id IS NOT NULL AND gene_id != ''
        GROUP BY gene_id
        ORDER BY hits DESC
    """)
    hit_data = {
        r["gene_id"]: {"hits": r["hits"], "last_used": fmt_date(r["last_used"])}
        for r in cur.fetchall()
    }

    # Get mutations per gene
    cur.execute("""
        SELECT gene_id, target_file, description, status, created_at
        FROM mutations
        WHERE gene_id IS NOT NULL AND gene_id != ''
        ORDER BY created_at DESC
    """)
    gene_mutations = {}
    for r in cur.fetchall():
        # gene_id may be comma-separated (e.g. "PG-D1-001,PG-C3-001")
        for gid in split_gene_ids(r["gene_id"]):
            if gid not in gene_mutations:
                gene_mutations[gid] = []
            gene_mutations[gid].append(
                {
                    "file": r["target_file"],
                    "desc": r["description"] or "",
                    "date": fmt_date(r["created_at"]),
                    "status": r["status"] or "proposed",
                }
            )

    # Merge all gene IDs
    all_gene_ids = set(hit_data.keys()) | set(gene_cache.keys())

    items = []
    for gid in sorted(all_gene_ids):
        cache = gene_cache.get(gid, {})
        hits_info = hit_data.get(gid, {"hits": 0, "last_used": ""})
        items.append(
            {
                "id": gid,
                "name": cache.get("name")
                or cache.get("gene_name")
                or cache.get("summary", "").split("(")[0][:10]
                or gid,
                "summary": cache.get("summary")
                or cache.get("action", {}).get("action_description")
                or cache.get("description")
                or "",
                "rule_text": cache.get("rule_text")
                or cache.get("action", {}).get("action_template")
                or cache.get("pattern_key")
                or "",
                "fitness": cache.get("fitness")
                or cache.get("fitnessScore")
                or cache.get("metadata", {}).get("fitness_score")
                or None,
                "layer": cache.get("layer") or "",
                "mutation_space": cache.get("mutation_space")
                or cache.get("mutationSpace")
                or "",
                "gene_status": cache.get("status")
                or ("active" if hits_info["hits"] > 0 else "unused"),
                "hits": hits_info["hits"],
                "last_used": hits_info["last_used"],
                "mutations": gene_mutations.get(gid, []),
            }
        )

    # Sort: hits > 0 first (desc), then hits == 0
    items.sort(key=lambda x: (-x["hits"], x["id"]))

    matched = sum(1 for i in items if i["hits"] > 0)
    mutations_linked = sum(len(i["mutations"]) for i in items)
    total = len(items)

    return {
        "total": total,
        "matched": matched,
        "mutations_linked": mutations_linked,
        "unused": total - matched,
        "items": items,
    }


def enrich_gene_names(reviews, genes_data):
    """Fill gene_name in review mutations from genes data."""
    gene_names = {g["id"]: g["name"] for g in genes_data.get("items", [])}
    for review in reviews:
        for m in review.get("mutations", []):
            if m.get("gene_id") and not m.get("gene_name"):
                # gene_id may be comma-separated
                ids = split_gene_ids(m["gene_id"])
                names = [gene_names.get(gid, gid) for gid in ids]
                m["gene_name"] = ", ".join(n for n in names if n)


def build_dashboard_data(db_path, data_dir):
    """Build complete dashboard JSON."""
    c = conn(db_path)
    try:
        overview = build_overview(c)
        reviews = build_reviews(c)
        genes = build_genes(c, data_dir)
        enrich_gene_names(reviews, genes)
    finally:
        c.close()

    now_utc = datetime.now(timezone.utc)
    return {
        "generated_at": f"{now_utc.month}月{now_utc.day}日 {now_utc.hour:02d}:{now_utc.minute:02d} UTC",
        "overview": overview,
        "reviews": reviews,
        "genes": genes,
    }


def render(db_path, data_dir, output_path, template_path=None):
    """Main render: DB → JSON → inject into template → write HTML."""
    tpl = Path(template_path) if template_path else TEMPLATE_PATH
    if not tpl.exists():
        print(f"ERROR: template not found: {tpl}", file=sys.stderr)
        sys.exit(1)

    template = tpl.read_text(encoding="utf-8")
    data = build_dashboard_data(db_path, data_dir)
    data_json = json.dumps(data, ensure_ascii=False, indent=None)

    html = template.replace("__DASHBOARD_DATA__", data_json, 1)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return str(out)


def main():
    parser = argparse.ArgumentParser(description="Render evolution dashboard from DB")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to evolution.db")
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="Path to evolution-data directory",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output HTML path (default: <data-dir>/dashboard.html)",
    )
    parser.add_argument(
        "--template", default=None, help="Path to dashboard-template.html"
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    data_dir = Path(args.data_dir)
    output = args.output or str(data_dir / "dashboard.html")

    if not db_path.exists():
        print(
            json.dumps(
                {"status": "error", "reason": f"DB not found: {db_path}"},
                ensure_ascii=False,
            )
        )
        sys.exit(1)

    result_path = render(db_path, data_dir, output, args.template)
    print(json.dumps({"status": "ok", "path": result_path}, ensure_ascii=False))


if __name__ == "__main__":
    main()
