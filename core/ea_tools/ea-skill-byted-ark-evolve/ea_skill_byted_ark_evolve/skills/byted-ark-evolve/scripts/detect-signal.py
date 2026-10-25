from __future__ import annotations

"""Detect evolution signal candidates from user messages via Hook.

Can be used as:
1. PostToolUse hook - reads JSON from stdin, auto-records high-confidence signals
2. Library - import detect_signals() for batch detection in scan-history.py
"""

import json
import os
import re
import sys

for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")

from _workspace import resolve_workspace_root

DEFAULT_DB_PATH = os.path.join(resolve_workspace_root(), "evolution-data/evolution.db")

# ── Pattern  ──────────────────────────────────────────────

PATTERNS = {
    "correction": {
        "high_confidence": [
            r"不[要是]这[样个么]",
            r"错了",
            r"不对[，。！\s]",
            r"应该[用是]",
            r"正确的[做方]法",
            r"我要的是.{1,20}不是",
            r"别[再这]",
            r"不是这个意思",
            r"你搞[错混]了",
            r"重[做来]",
        ],
        "low_confidence": [
            r"我刚[才说]的是",
            r"再[试]一[次遍]",
            r"看清楚",
        ],
    },
    "negative": {
        "high_confidence": [
            r"太[长短慢]了",
            r"AI[味感]",
            r"机器[味感]",
            r"套话",
            r"又来了",
            r"跟上次一样",
            r"不[是]我[想]的",
            r"废话",
            r"没用",
        ],
        "low_confidence": [
            r"^嗯",
            r"^算了",
            r"^行吧",
            r"^凑合",
            r"emmm",
            r"无语",
        ],
    },
    "positive": {
        "high_confidence": [
            r"[完太]美了",
            r"就[是这][这样]",
            r"做得[好不]错",
            r"比上次好",
            r"nice|great|perfect",
        ],
        "low_confidence": [],
    },
    "suggestion": {
        "high_confidence": [
            r"以后[可能]行",
            r"下次",
            r"能不能",
            r"要是能{1,30}就好了",
            r"这种情况应该",
            r"建议",
            r"最好[能是]",
            r"希望你",
        ],
        "low_confidence": [
            r"有没有办法",
            r"怎么才能",
        ],
    },
    "preference": {
        "high_confidence": [
            r"我[更比较]喜欢",
            r"我习惯",
            r"我[一般]都会",
            r"我的风格",
            r"[别不][要用]给我",
            r"用{1,10}格式",
            r"[简详][知细][点些]",
        ],
        "low_confidence": [],
    },
    "clarification": {
        "high_confidence": [
            r"我[的这]里[说的指]的是",
            r"[抓其][它]{1,15}[就指]的是",
            r"你[理搞]解错了",
            r"不是.{1,20}而是",
            r"准确[来地]说",
            r"补充一下",
            r"我[再解]释一下",
        ],
        "low_confidence": [],
    },
}


def detect_signals(text):
    """对文本做 pattern 匹配，返回候选信号列表。"""
    candidates = []
    for signal_type, patterns in PATTERNS.items():
        high_matches = []
        low_matches = []
        for pat in patterns.get("high_confidence", []):
            if re.search(pat, text, re.IGNORECASE):
                high_matches.append(pat)
        for pat in patterns.get("low_confidence", []):
            if re.search(pat, text, re.IGNORECASE):
                low_matches.append(pat)
        if high_matches or low_matches:
            confidence = "high" if high_matches else "low"
            candidates.append(
                {
                    "type": signal_type,
                    "confidence": confidence,
                    "matched_patterns": high_matches + low_matches,
                    "high_count": len(high_matches),
                    "low_count": len(low_matches),
                }
            )
    return candidates


def auto_record(db_path, signal_type, raw_text, session_id, context):
    """Write a signal directly to DB. Returns signal_id or None."""
    if not os.path.exists(db_path):
        return None
    import sqlite3

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO signals (session_id, type, severity, raw_text, context)
        VALUES (?, ?, 'medium', ?, ?)
    """,
        (session_id, signal_type, raw_text[:500], context),
    )
    conn.commit()
    signal_id = cur.lastrowid
    conn.close()
    return signal_id


def handle_hook_input():
    """Read user message JSON from hook stdin and run detection."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        data = json.loads(raw)
    except (json.JSONDecodeError, IOError):
        sys.exit(0)

    messages = data.get("messages", [])
    user_text = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                user_text = "\n".join(texts)
            elif isinstance(content, str):
                user_text = content
            break

    if not user_text or len(user_text) < 2:
        sys.exit(0)

    candidates = detect_signals(user_text)
    if not candidates:
        sys.exit(0)

    auto_recorded = []
    needs_confirmation = []
    db_path = DEFAULT_DB_PATH
    session_id = (data.get("session_id") or "")[:12]

    for cand in candidates:
        if cand["confidence"] == "high":
            sid = auto_record(
                db_path,
                cand["type"],
                user_text,
                session_id,
                f"auto-detected by hook, patterns: {cand['matched_patterns']}",
            )
            if sid:
                auto_recorded.append({"signal_id": sid, "type": cand["type"]})
            else:
                needs_confirmation.append(cand)
        else:
            needs_confirmation.append(cand)

    context_parts = []
    if auto_recorded:
        ids_str = ", ".join(f"#{r['signal_id']}({r['type']})" for r in auto_recorded)
        context_parts.append(
            f"[Evolution] Auto-recorded {len(auto_recorded)} signal(s): {ids_str}"
        )
    if needs_confirmation:
        types_str = ", ".join(c["type"] for c in needs_confirmation)
        context_parts.append(
            f"[Evolution] Candidate signal(s) need confirmation: {types_str}. "
            f"Consider recording via signal-record.py if appropriate."
        )

    if context_parts:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": " | ".join(context_parts),
            }
        }
        print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    handle_hook_input()
