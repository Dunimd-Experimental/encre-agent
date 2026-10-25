from __future__ import annotations

"""Generate an evolution diff report in HTML format (Chinese)."""

import sqlite3
import json
import os
import sys
import argparse
from datetime import datetime

for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")

from _workspace import resolve_workspace_root

DEFAULT_DB_PATH = os.path.join(resolve_workspace_root(), "evolution-data/evolution.db")

# Badge: (css_class, chinese_label)
LAYER_BADGE = {
    "identity": ("badge-identity", "identity"),
    "context": ("badge-context", "context"),
    "protocol": ("badge-protocol", "protocol"),
    "capability": ("badge-capability", "capability"),
    "runtime": ("badge-runtime", "runtime"),
}

STATUS_CN = {
    "proposed": "proposed",
    "approved": "approved",
    "applied": "applied",
    "verified": "verified",
    "rejected": "rejected",
    "pending": "pending",
    "observed": "observed",
    "regression": "regression",
    "partial": "partial",
}
STATUS_COLOR = {
    "proposed": "var(--muted)",
    "approved": "var(--accent)",
    "applied": "var(--amber)",
    "verified": "var(--green)",
    "rejected": "var(--red)",
    "pending": "var(--amber)",
    "observed": "var(--accent)",
    "regression": "var(--red)",
    "partial": "var(--amber)",
}

CSS = """\
:root {
  --bg: #fafafa; --surface: #ffffff; --border: #e5e7eb;
  --text: #1f2937; --muted: #6b7280; --accent: #2563eb;
  --accent-light: #eff6ff;
  --red: #dc2626; --red-light: #fef2f2; --red-border: #fecaca;
  --green: #16a34a; --green-light: #f0fdf4; --green-border: #bbf7d0;
  --amber: #d97706; --amber-light: #fffbeb; --amber-border: #fde68a;
  --purple: #7c3aed; --purple-light: #faf5ff; --purple-border: #ddd6fe;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.75;
  max-width: 760px; margin: 0 auto; padding: 48px 24px 80px;
  font-size: 17px;
}
a { color: var(--accent); text-decoration: none; }
hr { border: none; border-top: 1px solid var(--border); margin: 36px 0; }
h1 { font-size: 26px; font-weight: 700; margin-bottom: 4px; }
h2 { font-size: 20px; font-weight: 700; margin-bottom: 12px; letter-spacing: -0.01em; }
h3 { font-size: 17px; font-weight: 600; margin-bottom: 6px; }
p { margin-bottom: 12px; font-size: 16px; }
ul, ol { font-size: 16px; padding-left: 20px; margin-bottom: 12px; }
li { margin-bottom: 6px; }
code { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 14px; background: #f3f4f6; padding: 2px 5px; border-radius: 3px; }
.meta { color: var(--muted); font-size: 15px; margin-bottom: 28px; }
.meta span { margin-right: 16px; }
table { width: 100%; border-collapse: collapse; font-size: 16px; margin-bottom: 16px; }
th { text-align: left; font-weight: 600; padding: 10px; border-bottom: 2px solid var(--text); font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
td { padding: 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
tr:last-child td { border-bottom: none; }
.badge { display: inline-block; font-size: 13px; font-weight: 600; padding: 2px 8px; border-radius: 3px; }
.badge-identity { background: var(--purple-light); color: var(--purple); border: 1px solid var(--purple-border); }
.badge-protocol { background: var(--accent-light); color: var(--accent); border: 1px solid #bfdbfe; }
.badge-capability { background: var(--green-light); color: var(--green); border: 1px solid var(--green-border); }
.badge-context { background: var(--amber-light); color: var(--amber); border: 1px solid var(--amber-border); }
.badge-runtime { background: #f3f4f6; color: var(--muted); border: 1px solid var(--border); }
.quote { font-size: 16px; color: var(--muted); font-style: italic; padding: 8px 16px; border-left: 3px solid var(--border); margin: 8px 0 12px; }
.quote.negative { border-left-color: var(--red); color: var(--red); }
.evo-card { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; margin-bottom: 24px; overflow: hidden; }
.evo-header { padding: 16px 18px 12px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: baseline; }
.evo-header h3 { margin: 0; font-size: 17px; }
.evo-section { padding: 14px 18px; border-bottom: 1px solid #f3f4f6; }
.evo-section:last-child { border-bottom: none; }
.evo-label { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 6px; }
.before-after { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.ba-box { padding: 12px 14px; border-radius: 4px; font-size: 15px; }
.ba-before { background: var(--red-light); border: 1px solid var(--red-border); }
.ba-after { background: var(--green-light); border: 1px solid var(--green-border); }
.ba-label { font-size: 13px; font-weight: 600; margin-bottom: 4px; }
.ba-before .ba-label { color: var(--red); }
.ba-after .ba-label { color: var(--green); }
.verify-box { background: #f8f9fa; border-radius: 4px; padding: 12px 14px; font-size: 15px; }
.verify-box label { display: block; margin-bottom: 4px; cursor: pointer; }
.verify-box input[type="checkbox"] { margin-right: 8px; }
.summary-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px; }
.summary-card { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 16px; text-align: center; }
.summary-card .num { font-size: 28px; font-weight: 700; }
.summary-card .label { font-size: 14px; color: var(--muted); }
details { margin-bottom: 12px; }
summary { cursor: pointer; font-weight: 600; font-size: 15px; color: var(--muted); }
.hint { font-size: 13px; color: var(--muted); font-weight: 400; margin-top: 2px; }
.summary-card .hint { margin-top: 4px; }
"""


def get_db(db_path=None):
    path = db_path or DEFAULT_DB_PATH
    if not os.path.exists(path):
        print(f"Error: DB not found at {path}. Run db-init.py first.", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def esc(text):
    """Escape HTML."""
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def badge(layer):
    if not layer or layer not in LAYER_BADGE:
        return ""
    cls, label = LAYER_BADGE[layer]
    return f'<span class="badge {cls}">{label}</span>'


def status_html(status):
    color = STATUS_COLOR.get(status, "var(--muted)")
    label = STATUS_CN.get(status, status)
    return f'<span style="color:{color};font-weight:600">{esc(label)}</span>'


def generate_report(
    db_path=None,
    run_id=None,
    sessions_count=0,
    sessions_cost=0.0,
    evolution_cost=0.0,
    saturation_note=None,
):
    conn = get_db(db_path)
    cur = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")

    # --- Fetch data ---

    # Unprocessed signals
    cur.execute("SELECT * FROM signals WHERE processed = 0 ORDER BY created_at")
    signals = [dict(r) for r in cur.fetchall()]

    # Evolution mutations (proposed by evolution analysis)
    cur.execute("""
        SELECT * FROM mutations
        WHERE status IN ('proposed', 'approved', 'applied', 'verified')
        AND (source = 'evolution' OR source IS NULL)
        ORDER BY created_at DESC LIMIT 20
    """)
    mutations = [dict(r) for r in cur.fetchall()]

    # User-direct and snapshot-diff mutations (tracked changes)
    cur.execute("""
        SELECT * FROM mutations
        WHERE source IN ('user-direct', 'snapshot-diff')
        ORDER BY created_at DESC LIMIT 20
    """)
    tracked_changes = [dict(r) for r in cur.fetchall()]

    # Pending proposals
    cur.execute("""
        SELECT proposal_group_id, proposal_summary, proposal_status, MIN(created_at) as created_at, COUNT(*) as mutation_count
        FROM mutations
        WHERE proposal_status IN ('pending', 'presented')
        GROUP BY proposal_group_id, proposal_summary, proposal_status
        ORDER BY MIN(created_at) ASC
    """)
    pending_proposals = [dict(r) for r in cur.fetchall()]

    # Trajectory stats
    cur.execute("SELECT type, COUNT(*) as cnt FROM trajectories GROUP BY type")
    traj_stats = {r["type"]: r["cnt"] for r in cur.fetchall()}

    # Signal aggregation
    total_signals = len(signals)
    signal_types = {}
    signal_layers = {}
    for s in signals:
        signal_types[s["type"]] = signal_types.get(s["type"], 0) + 1
        if s["layer"]:
            signal_layers[s["layer"]] = signal_layers.get(s["layer"], 0) + 1

    # Previous runs for saturation
    cur.execute(
        "SELECT mutations_proposed FROM evolution_runs ORDER BY created_at DESC LIMIT 3"
    )
    recent_runs = [r["mutations_proposed"] for r in cur.fetchall()]
    is_saturated = len(recent_runs) >= 3 and all((m or 0) <= 1 for m in recent_runs)

    if not saturation_note:
        if is_saturated:
            saturation_note = "\u8d8b\u4e8e\u9971\u548c\u3002\u8fde\u7eed 3 \u6b21\u8fdb\u5316\u4ea7\u51fa \u22641 \u4e2a\u65b9\u6848\uff0c\u5efa\u8bae\u964d\u4f4e\u9891\u7387\u6216\u5173\u6ce8\u65b0\u65b9\u5411\u3002"
        else:
            saturation_note = "\u672a\u9971\u548c\u3002"  # 未饱和。
    if not run_id:
        run_id = f"evo_{datetime.now().strftime('%Y%m%d')}_{len(mutations):03d}"

    # Cost
    roi = (evolution_cost / sessions_cost) if sessions_cost > 0 else 0

    # Pareto
    pareto_pass = sum(
        1
        for m in mutations
        if m.get("pareto_check") and "PASS" in str(m["pareto_check"])
    )
    pareto_label = (
        "\u5168\u90e8"
        if pareto_pass == len(mutations) and mutations
        else f"{pareto_pass}/{len(mutations)}"
    )

    # --- Build HTML ---
    h = []

    # Head
    h.append(f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>\u8fdb\u5316 Diff \u62a5\u544a \u2014 {today}</title>
<style>{CSS}</style>
</head>
<body>

<h1>Agent \u8fdb\u5316 Diff \u62a5\u544a</h1>
<div class="meta">
  <span>{today}</span>
  <span>{run_id}</span>
  <span>{sessions_count} \u4e2a session / {total_signals} \u6761\u4fe1\u53f7 / {len(mutations)} \u4e2a\u8fdb\u5316\u65b9\u6848</span>
</div>
""")

    # Summary grid
    tracked_count = len(tracked_changes)
    h.append(f"""
<div class="summary-grid">
  <div class="summary-card"><div class="num">{total_signals}</div><div class="label">\u5f85\u5904\u7406\u4fe1\u53f7</div><div class="hint">\u7528\u6237\u53cd\u9988\u4e2d\u8bc6\u522b\u51fa\u7684\u6539\u8fdb\u7ebf\u7d22</div></div>
  <div class="summary-card"><div class="num">{len(mutations)}</div><div class="label">\u8fdb\u5316\u65b9\u6848</div><div class="hint">\u57fa\u4e8e\u4fe1\u53f7\u8bbe\u8ba1\u7684\u5177\u4f53\u6539\u52a8</div></div>
  <div class="summary-card"><div class="num" style="color:var(--green)">{pareto_label}</div><div class="label">\u5e15\u7d2f\u6258\u901a\u8fc7</div><div class="hint">\u786e\u8ba4\u6539\u52a8\u4e0d\u4f1a\u8ba9\u5176\u4ed6\u65b9\u9762\u53d8\u5dee</div></div>
  <div class="summary-card"><div class="num">{tracked_count}</div><div class="label">\u7528\u6237\u53d8\u66f4</div><div class="hint">\u7528\u6237\u76f4\u63a5\u6307\u4ee4\u4fee\u6539\u7684\u6587\u4ef6</div></div>
</div>
""")

    # ── Overview table ──
    if mutations:
        h.append("<h2>\u603b\u89c8</h2>\n<table>\n")
        h.append(
            "  <thead><tr><th>\u7f16\u53f7</th><th>\u5f52\u56e0\u5c42</th><th>\u65b9\u6848\u63cf\u8ff0</th><th>\u76ee\u6807\u6587\u4ef6</th><th>\u72b6\u6001</th></tr></thead>\n  <tbody>\n"
        )
        for i, m in enumerate(mutations, 1):
            h.append(f"""    <tr>
      <td>M{i}</td>
      <td>{badge(m.get("layer"))}</td>
      <td>{esc(m.get("description", ""))}</td>
      <td><code>{esc(m.get("target_file", ""))}</code></td>
      <td>{status_html(m.get("status", "proposed"))}</td>
    </tr>\n""")
        h.append("  </tbody>\n</table>\n")

    h.append("<hr>\n")

    # ── Mutation detail cards ──
    for i, m in enumerate(mutations, 1):
        verified_border = (
            ' style="border-color: var(--green-border);"'
            if m["status"] == "verified"
            else ""
        )
        h.append(f"""
<div class="evo-card"{verified_border}>
  <div class="evo-header">
    <h3>M{i}: {esc(m.get("description", ""))}</h3>
    {badge(m.get("layer"))}
  </div>
""")

        # Trigger signals
        sig_ids = []
        if m.get("signal_ids"):
            try:
                sig_ids = json.loads(m["signal_ids"])
            except (json.JSONDecodeError, TypeError):
                pass

        if sig_ids:
            placeholders = ",".join("?" * len(sig_ids))
            cur.execute(
                f"SELECT raw_text, severity, type FROM signals WHERE id IN ({placeholders})",
                sig_ids,
            )
            related = cur.fetchall()
            h.append('  <div class="evo-section">\n')
            h.append(
                f'    <div class="evo-label">\u89e6\u53d1\u4fe1\u53f7\uff08{len(sig_ids)} \u6761\uff09</div>\n'
            )
            for rs in related:
                qcls = "quote negative" if rs["severity"] == "high" else "quote"
                h.append(
                    f'    <div class="{qcls}">&ldquo;{esc(rs["raw_text"])}&rdquo;</div>\n'
                )
            h.append("  </div>\n")

        # Layer attribution reason
        if m.get("layer_reason"):
            h.append('  <div class="evo-section">\n')
            h.append(
                '    <div class="evo-label">\u5c42\u5f52\u56e0\u7406\u7531</div>\n'
            )
            h.append(f"    <p>{esc(m['layer_reason'])}</p>\n")
            h.append("  </div>\n")

        # Before / After
        if m.get("before_text") or m.get("after_text"):
            target = esc(m.get("target_file", ""))
            h.append(f"""  <div class="evo-section">
    <div class="evo-label">\u53d8\u66f4\u524d / \u53d8\u66f4\u540e &mdash; <code>{target}</code></div>
    <div class="before-after">
""")
            h.append(
                f'      <div class="ba-box ba-before"><div class="ba-label">\u53d8\u66f4\u524d</div>{esc(m.get("before_text", "N/A"))}</div>\n'
            )
            h.append(
                f'      <div class="ba-box ba-after"><div class="ba-label">\u53d8\u66f4\u540e</div>{esc(m.get("after_text", "N/A"))}</div>\n'
            )
            h.append("    </div>\n  </div>\n")

        # Pareto check
        if m.get("pareto_check"):
            h.append('  <div class="evo-section">\n')
            h.append(
                '    <div class="evo-label">\u5e15\u7d2f\u6258\u68c0\u67e5</div>\n'
            )
            h.append(f"    <p>{esc(m['pareto_check'])}</p>\n")
            h.append("  </div>\n")

        # Verification checklist
        criteria = []
        if m.get("verification_criteria"):
            try:
                criteria = json.loads(m["verification_criteria"])
            except (json.JSONDecodeError, TypeError):
                criteria = [m["verification_criteria"]]

        if criteria:
            h.append('  <div class="evo-section">\n')
            h.append('    <div class="evo-label">\u9a8c\u8bc1\u6e05\u5355</div>\n')
            h.append('    <div class="verify-box">\n')
            for c in criteria:
                checked = " checked" if m["status"] == "verified" else ""
                h.append(
                    f'      <label><input type="checkbox"{checked}> {esc(c)}</label>\n'
                )
            h.append("    </div>\n")
            h.append("  </div>\n")

        # Verified banner
        if m["status"] == "verified":
            applied = m.get("applied_at", "")
            h.append('  <div class="evo-section">\n')
            h.append(
                '    <p style="color:var(--green);font-weight:600">\u5df2\u9a8c\u8bc1'
            )
            if applied:
                h.append(f" &mdash; {esc(applied)}")
            h.append("</p>\n  </div>\n")

        h.append("</div>\n")

    # ── Pending proposals ──
    if pending_proposals:
        h.append("<hr>\n<h2>待处理提案队列</h2>\n")
        h.append(
            '<p style="font-size:15px;color:var(--muted);margin-bottom:16px">这些 proposal 尚未被用户最终确认，因此会在后续 session_start 中继续可见，直到 accepted / rejected / stale / superseded。</p>\n'
        )
        h.append("<table>\n")
        h.append(
            "  <thead><tr><th>Proposal</th><th>状态</th><th>变更数</th><th>摘要</th><th>生成时间</th></tr></thead>\n  <tbody>\n"
        )
        for pp in pending_proposals:
            h.append(
                f'    <tr><td><code>{esc(pp.get("proposal_group_id", ""))}</code></td><td>{status_html(pp.get("proposal_status", "pending"))}</td><td>{pp.get("mutation_count", 0)}</td><td>{esc(pp.get("proposal_summary", ""))}</td><td style="font-size:14px;color:var(--muted)">{esc(pp.get("created_at", ""))}</td></tr>\n'
            )
        h.append("  </tbody>\n</table>\n")
    # ── Signal distribution by layer ──
    if signal_layers:
        h.append("<hr>\n<h2>\u4fe1\u53f7\u5206\u5e03\uff08\u6309\u5c42\uff09</h2>\n")
        h.append(
            '<p class="hint" style="margin-bottom:10px">'
            "\u8eab\u4efd\uff1aAgent \u7684\u4ef7\u503c\u89c2\u3001\u539f\u5219"
            " &middot; \u4e0a\u4e0b\u6587\uff1a\u7528\u6237\u504f\u597d\u3001\u8bb0\u5fc6"
            " &middot; \u534f\u8bae\uff1a\u884c\u4e3a\u89c4\u5219\u3001\u51b3\u7b56\u6a21\u5f0f"
            " &middot; \u80fd\u529b\uff1a\u6280\u80fd\u3001\u65b9\u6cd5\u8bba"
            " &middot; \u8fd0\u884c\u65f6\uff1a\u6a21\u578b\u914d\u7f6e</p>\n"
        )
        h.append("<table>\n")
        h.append(
            "  <thead><tr><th>\u5c42\u7ea7</th><th>\u6570\u91cf</th></tr></thead>\n  <tbody>\n"
        )
        for layer, cnt in sorted(signal_layers.items(), key=lambda x: -x[1]):
            h.append(f"    <tr><td>{badge(layer)}</td><td>{cnt}</td></tr>\n")
        h.append("  </tbody>\n</table>\n")

    # ── Signal type breakdown ──
    if signal_types:
        type_cn = {
            "correction": "\u7ea0\u6b63",
            "negative": "\u8d1f\u9762",
            "positive": "\u6b63\u9762",
            "suggestion": "\u5efa\u8bae",
        }
        type_hint = {
            "correction": "\u7528\u6237\u7ea0\u6b63\u4e86 Agent \u7684\u9519\u8bef\u884c\u4e3a",
            "negative": "\u7528\u6237\u8868\u8fbe\u4e0d\u6ee1",
            "positive": "\u7528\u6237\u8868\u8fbe\u8ba4\u53ef",
            "suggestion": "\u7528\u6237\u63d0\u51fa\u6539\u8fdb\u5efa\u8bae",
        }
        h.append("<h2>\u4fe1\u53f7\u7c7b\u578b</h2>\n<table>\n")
        h.append(
            "  <thead><tr><th>\u7c7b\u578b</th><th>\u6570\u91cf</th><th></th></tr></thead>\n  <tbody>\n"
        )
        for stype, cnt in sorted(signal_types.items(), key=lambda x: -x[1]):
            hint = type_hint.get(stype, "")
            h.append(
                f"    <tr><td>{type_cn.get(stype, stype)}</td><td>{cnt}</td>"
                f'<td class="hint">{hint}</td></tr>\n'
            )
        h.append("  </tbody>\n</table>\n")

    # ── High severity signals detail ──
    high_signals = [s for s in signals if s["severity"] == "high"]
    if high_signals:
        h.append("<h2>\u9ad8\u4e25\u91cd\u5ea6\u4fe1\u53f7</h2>\n")
        for s in high_signals:
            h.append(
                f'<div class="quote negative">&ldquo;{esc(s["raw_text"])}&rdquo;</div>\n'
            )
            if s.get("context"):
                h.append(
                    f'<p style="font-size:14px;color:var(--muted);margin-top:-4px">{esc(s["context"])}</p>\n'
                )

    # ── Tracked user changes ──
    if tracked_changes:
        source_cn = {
            "user-direct": "Hook \u5b9e\u65f6\u6355\u83b7",
            "snapshot-diff": "\u5feb\u7167\u5bf9\u6bd4\u53d1\u73b0",
        }
        h.append("<hr>\n<h2>\u5df2\u8ffd\u8e2a\u7684\u7528\u6237\u53d8\u66f4</h2>\n")
        h.append(
            f'<p style="font-size:15px;color:var(--muted);margin-bottom:16px">'
            f"\u4ee5\u4e0b {len(tracked_changes)} \u9879\u53d8\u66f4\u7531\u7528\u6237\u76f4\u63a5\u6307\u4ee4\u4ea7\u751f\uff0c"
            f"\u5df2\u81ea\u52a8\u8bb0\u5f55\u4ee5\u4fbf\u8fdb\u5316\u5206\u6790\u65f6\u611f\u77e5\u3002</p>\n"
        )
        h.append("<table>\n")
        h.append(
            "  <thead><tr><th>\u6587\u4ef6</th><th>\u63cf\u8ff0</th><th>\u6765\u6e90</th><th>\u65f6\u95f4</th></tr></thead>\n  <tbody>\n"
        )
        for tc in tracked_changes:
            src_label = source_cn.get(tc.get("source", ""), tc.get("source", ""))
            h.append("    <tr>\n")
            h.append(f"      <td><code>{esc(tc.get('target_file', ''))}</code></td>\n")
            h.append(f"      <td>{esc(tc.get('description', ''))}</td>\n")
            h.append(f"      <td>{esc(src_label)}</td>\n")
            h.append(
                f'      <td style="font-size:14px;color:var(--muted)">{esc(tc.get("applied_at", ""))}</td>\n'
            )
            h.append("    </tr>\n")
        h.append("  </tbody>\n</table>\n")

        # Show before/after for user-direct changes that have diff data
        changes_with_diff = [
            tc
            for tc in tracked_changes
            if tc.get("before_text") or tc.get("after_text")
        ]
        if changes_with_diff:
            h.append('<details style="margin-top:12px">\n')
            h.append(
                "<summary>\u53d8\u66f4\u8be6\u60c5\uff08\u70b9\u51fb\u5c55\u5f00\uff09</summary>\n"
            )
            for tc in changes_with_diff:
                h.append('<div class="evo-card" style="margin-top:12px">\n')
                h.append(
                    f'  <div class="evo-header"><h3>{esc(tc.get("target_file", ""))}</h3></div>\n'
                )
                h.append('  <div class="evo-section">\n')
                h.append('    <div class="before-after">\n')
                h.append(
                    f'      <div class="ba-box ba-before"><div class="ba-label">\u53d8\u66f4\u524d</div>{esc(tc.get("before_text", "N/A"))}</div>\n'
                )
                h.append(
                    f'      <div class="ba-box ba-after"><div class="ba-label">\u53d8\u66f4\u540e</div>{esc(tc.get("after_text", "N/A"))}</div>\n'
                )
                h.append("    </div>\n  </div>\n")
                h.append("</div>\n")
            h.append("</details>\n")

    # ── Trajectory stats ──
    golden = traj_stats.get("golden", 0)
    corrections = traj_stats.get("correction", 0)
    candidates = traj_stats.get("candidate", 0)
    if golden or corrections or candidates:
        h.append("<hr>\n<h2>\u8f68\u8ff9\u5e93</h2>\n<table>\n")
        h.append(
            "  <thead><tr><th>\u7c7b\u578b</th><th>\u6570\u91cf</th></tr></thead>\n  <tbody>\n"
        )
        if golden:
            h.append(
                f"    <tr><td>\u6b63\u786e\u8f68\u8ff9 (Golden)</td><td>{golden}</td></tr>\n"
            )
        if corrections:
            h.append(
                f"    <tr><td>\u7ea0\u6b63\u5bf9 (Correction)</td><td>{corrections}</td></tr>\n"
            )
        if candidates:
            h.append(
                f"    <tr><td>\u5019\u9009 (Candidate)</td><td>{candidates}</td></tr>\n"
            )
        h.append("  </tbody>\n</table>\n")

    # ── Cost tracking ──
    h.append("<hr>\n<h2>\u6210\u672c\u8ffd\u8e2a</h2>\n<table>\n")
    h.append(
        "  <thead><tr><th>\u9879\u76ee</th><th>\u6570\u503c</th></tr></thead>\n  <tbody>\n"
    )
    h.append(
        f"    <tr><td>\u88ab\u5206\u6790 session</td><td>{sessions_count} \u4e2a\uff0c\u7ea6 ${sessions_cost:.1f}</td></tr>\n"
    )
    h.append(
        f"    <tr><td>\u672c\u6b21\u8fdb\u5316\u5206\u6790</td><td>\u7ea6 ${evolution_cost:.1f}</td></tr>\n"
    )
    roi_display = f"{roi:.0%}" if sessions_cost > 0 else "N/A"
    roi_color = "var(--green)" if roi <= 0.25 else "var(--red)"
    h.append(
        f'    <tr><td>ROI \u6bd4\u7387 <span class="hint">\u8fdb\u5316\u6210\u672c\u5360 session \u6210\u672c\u7684\u6bd4\u4f8b\uff0c\u8d8a\u4f4e\u8d8a\u597d</span></td>'
        f'<td><span style="color:{roi_color};font-weight:600">{roi_display}</span>\uff08\u76ee\u6807 &le;25%\uff09</td></tr>\n'
    )
    h.append("  </tbody>\n</table>\n")

    # ── Saturation assessment ──
    h.append("<h2>\u9971\u548c\u8bc4\u4f30</h2>\n")
    h.append(
        '<p class="hint" style="margin-bottom:8px">\u8fde\u7eed\u591a\u6b21\u8fdb\u5316\u4ea7\u51fa\u6781\u5c11\u65b0\u65b9\u6848\u65f6\uff0c\u8bf4\u660e\u5f53\u524d\u65b9\u5411\u5df2\u5145\u5206\u4f18\u5316\uff0c\u53ef\u4ee5\u964d\u4f4e\u8fdb\u5316\u9891\u7387\u3002</p>\n'
    )
    if is_saturated:
        h.append(
            f'<p><strong style="color:var(--amber)">\u8d8b\u4e8e\u9971\u548c\u3002</strong>{esc(saturation_note)}</p>\n'
        )
    else:
        # More detail
        new_count = sum(1 for m in mutations if m.get("mutation_type") == "add")
        modify_count = sum(1 for m in mutations if m.get("mutation_type") == "modify")
        h.append(
            f"<p><strong>\u672a\u9971\u548c\u3002</strong>{len(mutations)} \u4e2a\u65b9\u6848\u4e2d {new_count} \u4e2a\u65b0\u589e\u3001{modify_count} \u4e2a\u4fee\u6539\u3002"
        )
        if saturation_note and saturation_note != "\u672a\u9971\u548c\u3002":
            h.append(f" {esc(saturation_note)}")
        h.append("</p>\n")

    # ── Footer ──
    h.append(f"""
<hr>
<p style="color:var(--muted);font-size:14px;text-align:center;">
  \u7531 Evolution Skill v0.1.0 \u751f\u6210 &mdash; <code>{run_id}</code>
</p>

</body>
</html>
""")

    # --- Write ---
    report_dir = os.path.join(os.path.dirname(db_path or DEFAULT_DB_PATH), "reports")
    os.makedirs(report_dir, exist_ok=True)

    filename = f"evolution-{datetime.now().strftime('%Y-%m-%d')}.html"
    report_path = os.path.join(report_dir, filename)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("".join(h))

    # Record run
    cur.execute(
        """
        INSERT INTO evolution_runs
            (run_id, signals_analyzed, mutations_proposed, sessions_analyzed,
             sessions_cost_usd, evolution_cost_usd, report_path, saturation_note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            run_id,
            total_signals,
            len(mutations),
            sessions_count,
            sessions_cost,
            evolution_cost,
            report_path,
            saturation_note,
        ),
    )
    conn.commit()
    conn.close()

    print(
        json.dumps(
            {
                "status": "ok",
                "report_path": report_path,
                "run_id": run_id,
                "signals_analyzed": total_signals,
                "mutations_count": len(mutations),
                "roi": roi_display,
            },
            ensure_ascii=False,
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description="Generate evolution diff report (Chinese)"
    )
    parser.add_argument("--db", default=None, help="Path to evolution.db")
    parser.add_argument("--run-id", default=None, help="Custom run ID")
    parser.add_argument(
        "--sessions", type=int, default=0, help="Number of sessions analyzed"
    )
    parser.add_argument(
        "--sessions-cost",
        type=float,
        default=0.0,
        help="Total cost of analyzed sessions (USD)",
    )
    parser.add_argument(
        "--evolution-cost",
        type=float,
        default=0.0,
        help="Cost of this evolution analysis (USD)",
    )
    parser.add_argument(
        "--saturation-note", default=None, help="Custom saturation assessment note"
    )
    args = parser.parse_args()
    generate_report(
        args.db,
        args.run_id,
        args.sessions,
        args.sessions_cost,
        args.evolution_cost,
        args.saturation_note,
    )


if __name__ == "__main__":
    main()
