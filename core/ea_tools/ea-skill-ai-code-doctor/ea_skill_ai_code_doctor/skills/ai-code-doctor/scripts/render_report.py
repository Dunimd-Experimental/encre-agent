#!/usr/bin/env python3
from __future__ import annotations

"""Render the HTML audit report from structured JSON with safe escaping."""

import html
import json
import re
import sys
from pathlib import Path


TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "assets" / "report_template.html"
TAG_DETAILS = {
    "smell": ("smell", "① 异味"),
    "perf": ("perf", "② 性能"),
}
FINDING_FIELDS = (
    ("location", "位置"),
    ("problem", "问题"),
    ("impact", "影响"),
    ("fix", "方案"),
    ("gain", "收益"),
)


def _text(value, field):
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field)
    return html.escape(value, quote=True)


def _text_list(data, field, allow_empty=True):
    values = data.get(field)
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("%s must be a list of strings" % field)
    if not allow_empty and not values:
        raise ValueError("%s must not be empty" % field)
    return values


def _render_findings(findings):
    if not isinstance(findings, list):
        raise ValueError("findings must be a list")
    rendered = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ValueError("findings[%d] must be an object" % index)
        tag = finding.get("tag")
        if tag not in TAG_DETAILS:
            raise ValueError("findings[%d].tag must be smell or perf" % index)
        tag_class, tag_label = TAG_DETAILS[tag]
        fields = []
        for key, label in FINDING_FIELDS:
            value = _text(finding.get(key), "findings[%d].%s" % (index, key))
            fields.append(
                '    <div class="field"><span class="field-label">%s：</span>%s</div>'
                % (label, value)
            )
        rendered.append(
            "\n".join([
                '<div class="finding">',
                '  <div class="finding-head">',
                '    <span class="tag %s">%s</span>' % (tag_class, tag_label),
                '    <span class="sev">严重度：%s</span>'
                % _text(finding.get("severity"), "findings[%d].severity" % index),
                "  </div>",
                "  <h3>%s</h3>" % _text(finding.get("title"), "findings[%d].title" % index),
                *fields,
                "</div>",
            ])
        )
    return "\n".join(rendered)


def _render_list(values, empty_text):
    if not values:
        values = [empty_text]
    return "\n".join("<li>%s</li>" % html.escape(value, quote=True) for value in values)


def render_report(data, template):
    if not isinstance(data, dict):
        raise ValueError("report data must be an object")
    score = data.get("health_score")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError("health_score must be an integer from 0 to 100")
    prerequisites = _text_list(data, "prerequisites", allow_empty=False)
    tradeoffs = _text_list(data, "tradeoffs")
    findings = data.get("findings")
    findings_html = _render_findings(findings)
    score_class = "low" if score < 60 else "mid" if score < 80 else ""
    replacements = {
        "FILE_NAME": _text(data.get("file_name"), "file_name"),
        "HEALTH_SCORE": str(score),
        "SCORE_CLASS": score_class,
        "VERDICT": _text(data.get("verdict"), "verdict"),
        "AUDIT_DATE": _text(data.get("audit_date"), "audit_date"),
        "MODE": _text(data.get("mode"), "mode"),
        "FINDING_COUNT": str(len(findings)),
        "FINDINGS_HTML": findings_html,
        "PREREQUISITES_HTML": _render_list(prerequisites, ""),
        "OPTIMIZED_CODE": _text(data.get("optimized_code"), "optimized_code"),
        "DIFF_BEFORE": _text(data.get("diff_before"), "diff_before"),
        "DIFF_AFTER": _text(data.get("diff_after"), "diff_after"),
        "TRADEOFFS_HTML": _render_list(tradeoffs, "无"),
    }
    rendered = template
    for key, value in replacements.items():
        placeholder = "{{%s}}" % key
        if placeholder not in rendered:
            raise ValueError("template is missing %s" % placeholder)
        rendered = rendered.replace(placeholder, value)
    remaining = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", rendered)))
    if remaining:
        raise ValueError("unfilled template placeholders: %s" % ", ".join(remaining))
    return rendered


def main(argv):
    if len(argv) != 3:
        print(json.dumps({"error": "usage: render_report.py <report.json> <output.html>"}))
        return 2
    try:
        data = json.loads(Path(argv[1]).read_text(encoding="utf-8-sig"))
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        rendered = render_report(data, template)
        Path(argv[2]).write_text(rendered, encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": "%s: %s" % (type(error).__name__, error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"output": argv[2], "finding_count": len(data["findings"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
