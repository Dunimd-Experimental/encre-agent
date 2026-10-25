from __future__ import annotations

"""
Render evolution report HTML from JSON data.

The Agent fills a JSON file (pure data, no logic).
This script handles all conditional rendering.

Usage:
  python report-render.py report-data.json
  python report-render.py report-data.json --output report.html
  cat report-data.json | python report-render.py -

JSON schema: see references/report-schema-example.json
"""

import json
import os
import sys
from datetime import datetime

for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")


LAYER_CN = {
    "identity": "韬唤",
    "context": "上下文",
    "protocol": "鍗忚",
    "capability": "能力",
    "runtime": "运行时",
}
LAYER_CSS = {
    "identity": ("badge-identity", "#faf5ff", "#7c3aed", "#ddd6fe"),
    "context": ("badge-context", "#fffbeb", "#d97706", "#fde68a"),
    "protocol": ("badge-protocol", "#eff6ff", "#2563eb", "#bfdbfe"),
    "capability": ("badge-capability", "#f0fdf4", "#16a34a", "#bbf7d0"),
    "runtime": ("badge-runtime", "#f3f4f6", "#6b7280", "#e5e7eb"),
}
STATUS_CN = {
    "applied": "已生效",
    "approved": "已批准",
    "proposed": "待审核",
    "rejected": "已拒绝",
    "verified": "已验证",
    "pending": "待验证",
    "observed": "已观测",
    "regression": "已回退",
    "partial": "部分生效",
}
STATUS_COLOR = {
    "applied": "#16a34a",
    "approved": "#2563eb",
    "proposed": "#6b7280",
    "rejected": "#dc2626",
    "verified": "#16a34a",
    "pending": "#d97706",
    "observed": "#2563eb",
    "regression": "#dc2626",
    "partial": "#d97706",
}
SIGNAL_TYPE_CN = {
    "correction": "纠正",
    "negative": "负面",
    "positive": "正面",
    "suggestion": "建议",
}


def esc(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def badge(layer):
    cn = LAYER_CN.get(layer, layer or "")
    _, bg, fg, border = LAYER_CSS.get(layer, ("", "#f3f4f6", "#6b7280", "#e5e7eb"))
    return f'<span style="display:inline-block;font-size:11px;font-weight:600;padding:1px 6px;border-radius:3px;background:{bg};color:{fg};border:1px solid {border}">{esc(cn)}</span>'


def render(data):
    d = data
    date = d.get("date", datetime.now().strftime("%Y-%m-%d"))
    stats = d.get("stats", {})
    signals = stats.get("signals", 0)
    changes_count = stats.get("changes", 0)
    rejected = stats.get("rejected", 0)
    summary = d.get("summary", "")
    changes = d.get("changes", [])
    signals_by_layer = d.get("signals_by_layer", {})
    high_signals = d.get("high_severity_signals", [])
    all_signals = d.get("all_signals", [])
    traj = d.get("trajectory", {})
    sat = d.get("saturation", {})
    cost = d.get("cost", {})
    user_direct = d.get("user_direct_changes", 0)
    next_steps = d.get("next_steps", [])

    h = []

    # === HEAD ===
    h.append(f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>进化报告 - {date}</title>
<style>
:root {{
  --bg:#fafafa;--surface:#fff;--border:#e5e7eb;--text:#1f2937;--muted:#6b7280;
  --accent:#2563eb;--green:#16a34a;--red:#dc2626;--amber:#d97706;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text);line-height:1.75;max-width:720px;margin:0 auto;padding:40px 24px 80px;font-size:16px}}
h1{{font-size:24px;font-weight:700;margin-bottom:4px}}
h2{{font-size:18px;font-weight:700;margin:24px 0 10px}}
p{{margin-bottom:10px}}
hr{{border:none;border-top:1px solid var(--border);margin:28px 0}}
code{{font-family:'SF Mono',monospace;font-size:13px;background:#f3f4f6;padding:1px 4px;border-radius:2px}}
details{{margin-bottom:10px}}
summary{{cursor:pointer;font-weight:600;font-size:14px;color:var(--muted)}}
table{{width:100%;border-collapse:collapse;font-size:14px;margin-bottom:10px}}
th{{text-align:left;font-weight:600;padding:6px 8px;border-bottom:2px solid var(--text);font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}}
td{{padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:top}}
.meta{{color:var(--muted);font-size:14px;margin-bottom:24px}}
.stat-row{{display:flex;gap:10px;margin-bottom:20px}}
.stat-pill{{display:flex;align-items:baseline;gap:6px;padding:8px 14px;border-radius:6px;background:var(--surface);border:1px solid var(--border)}}
.stat-pill .num{{font-size:20px;font-weight:700}}
.stat-pill .label{{font-size:12px;color:var(--muted)}}
.change-item{{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:14px 16px;margin-bottom:10px}}
.change-item .file-tag{{font-size:12px;font-weight:600;color:var(--accent);margin-bottom:2px}}
.change-item .desc{{font-size:15px;font-weight:600;margin-bottom:4px}}
.change-item .reason{{font-size:13px;color:var(--muted)}}
.anchor{{text-align:center;font-size:12px;color:var(--muted);padding:8px 0;margin:16px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border)}}
.ba-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}}
.ba-box{{padding:8px 10px;border-radius:4px;font-size:13px}}
.ba-before{{background:#fef2f2;border:1px solid #fecaca}}
.ba-after{{background:#f0fdf4;border:1px solid #bbf7d0}}
.ba-label{{font-size:11px;font-weight:600;margin-bottom:2px}}
.action-box{{background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;padding:14px 16px}}
.action-box .action-title{{font-size:13px;font-weight:600;color:var(--accent);margin-bottom:6px}}
.action-box li{{font-size:13px;margin-bottom:4px}}
.bar-row{{display:flex;align-items:center;gap:8px;margin-bottom:5px}}
.bar-label{{font-size:13px;width:50px;text-align:right}}
.bar{{height:18px;border-radius:3px;min-width:4px}}
.bar-count{{font-size:13px;color:var(--muted)}}
.info-pair{{display:flex;gap:12px;margin-top:16px}}
.info-card{{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:10px 14px;font-size:13px}}
.info-card .title{{font-weight:600;margin-bottom:4px}}
</style>
</head>
<body>
""")

    # === GLOSSARY ===
    h.append("""
<details style="margin-bottom:16px;background:#f8f9fa;border:1px solid var(--border);border-radius:6px;padding:10px 14px">
  <summary style="font-size:13px;cursor:pointer">📖 快速查（首次阅读建议展开）</summary>
  <table style="margin-top:8px;font-size:12px">
    <tbody>
      <tr><td style="font-weight:600;width:80px">反馈</td><td>你日常纠正、建议中识别出的改进线索</td></tr>
      <tr><td style="font-weight:600">变更</td><td>基于反馈设计的具体文件改动</td></tr>
      <tr><td style="font-weight:600">协议层</td><td>Agent 的行为规则和决策流程</td></tr>
      <tr><td style="font-weight:600">能力层</td><td>Agent 的技能和方法论</td></tr>
      <tr><td style="font-weight:600">帕累托检查</td><td>确保改动不会让其他方面变差</td></tr>
      <tr><td style="font-weight:600">饱和</td><td>连续多次进化产出极少新方案，当前方向已充分优化</td></tr>
      <tr><td style="font-weight:600">轨迹</td><td>Golden = 做对了的记录；Correction = 做错后修正的记录</td></tr>
    </tbody>
  </table>
</details>
""")

    # === HERO ===
    h.append(f'<h1>进化报告</h1>\n<div class="meta">{esc(date)}</div>\n')
    if summary:
        h.append(
            f'<p style="font-size:15px;line-height:1.6;margin-bottom:16px">{esc(summary)}</p>\n'
        )

    # Stats: 3 pills (if rejected > 0, show rejected; otherwise show 0 in green)
    h.append('<div class="stat-row">\n')
    h.append(
        f'  <div class="stat-pill"><span class="num">{signals}</span><span class="label">条反馈</span></div>\n'
    )
    h.append(
        f'  <div class="stat-pill"><span class="num">{changes_count}</span><span class="label">条变更</span></div>\n'
    )
    if rejected > 0:
        h.append(
            f'  <div class="stat-pill"><span class="num" style="color:var(--red)">{rejected}</span><span class="label">条拒绝</span></div>\n'
        )
    else:
        h.append(
            '  <div class="stat-pill"><span class="num" style="color:var(--green)">0</span><span class="label">条拒绝</span></div>\n'
        )
    h.append("</div>\n")

    # === CHANGES ===
    if changes:
        h.append("<h2>变更清单</h2>\n")
        for c in changes:
            status = c.get("status", "applied")
            dot_color = STATUS_COLOR.get(status, "#16a34a")
            h.append('<div class="change-item">\n')
            h.append(
                f'  <div class="file-tag"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{dot_color};margin-right:4px;vertical-align:middle"></span>{esc(c.get("file", ""))}</div>\n'
            )
            h.append(f'  <div class="desc">{esc(c.get("description", ""))}</div>\n')
            if c.get("reason"):
                h.append(f'  <div class="reason">原因：{esc(c["reason"])}</div>\n')
            # Folded diff
            before = c.get("before")
            after = c.get("after")
            if before or after:
                h.append(
                    '  <details style="margin-top:6px"><summary>查看具体变更</summary>\n'
                )
                h.append('    <div class="ba-grid">\n')
                h.append(
                    f'      <div class="ba-box ba-before"><div class="ba-label" style="color:var(--red)">Before</div>{esc(before or "(none)")}</div>\n'
                )
                h.append(
                    f'      <div class="ba-box ba-after"><div class="ba-label" style="color:var(--green)">After</div>{esc(after or "(none)")}</div>\n'
                )
                h.append("    </div>\n  </details>\n")
            h.append("</div>\n")

        # Rejected changes (only if any)
        rejected_changes = [c for c in changes if c.get("status") == "rejected"]
        if rejected_changes:
            h.append('<h2 style="color:var(--red)">被拒绝的变更</h2>\n')
            for c in rejected_changes:
                h.append('<div class="change-item" style="border-color:#fecaca">\n')
                h.append(
                    f'  <div class="file-tag" style="color:var(--red)">{esc(c.get("file", ""))}</div>\n'
                )
                h.append(f'  <div class="desc">{esc(c.get("description", ""))}</div>\n')
                if c.get("reject_reason"):
                    h.append(
                        f'  <div class="reason">拒绝原因：{esc(c["reject_reason"])}</div>\n'
                    )
                h.append("</div>\n")

        h.append(
            f'<div class="anchor">{len(changes)} changes written' + (f" ({rejected} rejected)" if rejected > 0 else "") + '</div>\n'
        )
    else:
        h.append(
            '<h2>变更清单</h2>\n<p style="color:var(--muted)">本次分析未产生变更方案。</p>\n'
        )

    # === SIGNAL SOURCE ===
    if signals_by_layer:
        h.append("<h2>鍙嶉鏉ユ簮</h2>\n")
        max_count = max(signals_by_layer.values()) if signals_by_layer else 1
        bar_colors = {
            "protocol": "#2563eb",
            "capability": "#16a34a",
            "identity": "#7c3aed",
            "context": "#d97706",
            "runtime": "#6b7280",
        }
        for layer, cnt in sorted(signals_by_layer.items(), key=lambda x: -x[1]):
            w = max(int(cnt / max_count * 160), 8)
            color = bar_colors.get(layer, "#6b7280")
            h.append(
                f'<div class="bar-row"><div class="bar-label">{badge(layer)}</div><div class="bar" style="width:{w}px;background:{color}"></div><div class="bar-count">{cnt}</div></div>\n'
            )

    # High severity (only if any)
    if high_signals:
        h.append(
            '<p style="font-size:13px;color:var(--muted);margin-top:10px;margin-bottom:4px">高优先级反馈：</p>\n'
        )
        for sig in high_signals:
            h.append(
                f'<div style="font-size:13px;color:var(--red);font-style:italic;padding:6px 12px;border-left:3px solid var(--red);margin-bottom:6px">&ldquo;{esc(sig.get("text", ""))}&rdquo;</div>\n'
            )

    # All signals (folded)
    if all_signals:
        h.append(
            f'<details style="margin-top:8px"><summary>查看全部 {len(all_signals)} 条反馈</summary>\n'
        )
        h.append(
            '<table style="margin-top:8px"><thead><tr><th>鍙嶉</th><th>类型</th></tr></thead><tbody>\n'
        )
        for sig in all_signals:
            stype = SIGNAL_TYPE_CN.get(sig.get("type", ""), sig.get("type", ""))
            h.append(
                f"<tr><td>{esc(sig.get('text', ''))}</td><td>{esc(stype)}</td></tr>\n"
            )
        h.append("</tbody></table></details>\n")

    if signals_by_layer or high_signals or all_signals:
        h.append('<div class="anchor">以上是本次分析的反馈来源</div>\n')

    # === NEXT STEPS ===
    if next_steps:
        h.append(
            '<h2>下一步</h2>\n<div class="action-box">\n<div class="action-title">验证计划</div>\n<ul style="padding-left:18px">\n'
        )
        for step in next_steps:
            h.append(f"  <li>{esc(step)}</li>\n")
        h.append("</ul>\n</div>\n")

    # === TRAJECTORY + SATURATION ===
    golden = traj.get("golden", 0)
    correction = traj.get("correction", 0)
    has_traj = golden > 0 or correction > 0
    has_sat = "is_saturated" in sat

    if has_traj or has_sat:
        h.append('<div class="info-pair">\n')
        if has_traj:
            h.append(
                f'<div class="info-card"><div class="title">轨迹库</div>{golden} golden / {correction} correction</div>\n'
            )
        if has_sat:
            if sat.get("is_saturated"):
                sat_text = '<span style="color:var(--amber)">趋于饱和</span>'
            else:
                added = sat.get("added", 0)
                modified = sat.get("modified", 0)
                sat_text = f'<span style="color:var(--green)">未饱和</span> · {added} 新增 / {modified} 修改'
            h.append(
                f'<div class="info-card"><div class="title">饱和评估</div>{sat_text}</div>\n'
            )
        h.append("</div>\n")

    # Cost (compact line)
    analysis_cost = cost.get("analysis_cost", 0)
    roi = cost.get("roi_percent", 0)
    cost_parts = []
    if analysis_cost > 0:
        cost_parts.append(f"分析成本 ~${analysis_cost:.1f}")
    if roi > 0:
        cost_parts.append(f"ROI {roi}%")
    if user_direct > 0:
        cost_parts.append(f"User-direct changes: {user_direct}")
    if cost_parts:
        h.append(
            f'<div style="margin-top:10px;font-size:12px;color:var(--muted);text-align:right">{" · ".join(cost_parts)}</div>\n'
        )

    # === FOOTER ===
    h.append(f"""
<hr>
<p style="color:var(--muted);font-size:12px;text-align:center">
  Evolution Skill v0.1.2 - {esc(date)}
</p>
</body>
</html>
""")
    return "".join(h)


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: python report-render.py <json-file> [--output <html-file>]",
            file=sys.stderr,
        )
        sys.exit(1)

    json_path = sys.argv[1]
    if json_path == "-":
        data = json.load(sys.stdin)
    else:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

    html = render(data)

    output_path = None
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_path = sys.argv[idx + 1]

    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(json.dumps({"status": "ok", "path": output_path}, ensure_ascii=False))
    else:
        print(html)


if __name__ == "__main__":
    main()
