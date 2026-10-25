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
Scan OpenClaw conversation history for evolution signal extraction.

Subcommands:
  list                   List all sessions with date, message count, tokens
  estimate --days N      Estimate token cost for scanning last N days
  extract --days N       Extract user-assistant dialogue pairs for signal analysis

Sessions location: ~/.{runtime}/agents/main/sessions/  (auto-detected: arkclaw / openclaw / etc.)
Format: JSONL (one JSON object per line), sessions.json as index.

Usage:
  python scan-history.py list
  python scan-history.py estimate --days 7
  python scan-history.py estimate --days 30
  python scan-history.py extract --days 7
  python scan-history.py extract --session <session-id>
"""

import json
import os
import sys
import argparse
from datetime import datetime, timedelta, timezone

for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")

from _workspace import resolve_runtime_home

SESSIONS_DIR = os.path.join(resolve_runtime_home(), "agents/main/sessions")
SESSIONS_INDEX = os.path.join(SESSIONS_DIR, "sessions.json")


def load_sessions_index():
    """Load sessions.json metadata index."""
    if not os.path.exists(SESSIONS_INDEX):
        return {}
    with open(SESSIONS_INDEX, "r", encoding="utf-8") as f:
        return json.load(f)


def find_all_session_files():
    """Find all .jsonl session files (active + archived/reset)."""
    files = []
    if not os.path.isdir(SESSIONS_DIR):
        return files

    for name in os.listdir(SESSIONS_DIR):
        full = os.path.join(SESSIONS_DIR, name)
        if not os.path.isfile(full):
            continue
        # Active: <uuid>.jsonl
        # Archived: <uuid>.jsonl.reset.<timestamp>
        if name.endswith(".jsonl") or ".jsonl.reset." in name:
            files.append(full)
    return sorted(files, key=lambda f: os.path.getmtime(f), reverse=True)


def parse_session_file(path):
    """Parse a JSONL session file, return list of message dicts."""
    messages = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                messages.append(obj)
            except json.JSONDecodeError:
                continue
    return messages


def get_session_info(path):
    """Get summary info for a session file."""
    messages = parse_session_file(path)
    if not messages:
        return None

    name = os.path.basename(path)
    # Extract session ID from filename
    session_id = name.split(".jsonl")[0]
    is_archived = ".reset." in name

    # Find date range from timestamps
    timestamps = []
    total_tokens = 0
    user_msgs = 0
    assistant_msgs = 0

    for msg in messages:
        ts = msg.get("timestamp")
        if ts:
            timestamps.append(ts)

        m = msg.get("message", {})
        role = m.get("role", "")
        if role == "user":
            user_msgs += 1
        elif role == "assistant":
            assistant_msgs += 1

        usage = m.get("usage", {})
        total_tokens += usage.get("totalTokens", 0)

    first_ts = min(timestamps) if timestamps else None
    last_ts = max(timestamps) if timestamps else None

    return {
        "session_id": session_id,
        "path": path,
        "is_archived": is_archived,
        "message_count": len(messages),
        "user_messages": user_msgs,
        "assistant_messages": assistant_msgs,
        "total_tokens": total_tokens,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
    }


def filter_by_days(sessions_info, days):
    """Filter sessions that have activity within the last N days."""
    if days is None:
        return sessions_info
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.isoformat()

    filtered = []
    for info in sessions_info:
        last = info.get("last_timestamp")
        if last and last >= cutoff_str:
            filtered.append(info)
    return filtered


def extract_dialogue(path):
    """Extract user-assistant dialogue pairs from a session file.

    Returns a list of dialogue turns, each with:
    - user_text: what the user said
    - assistant_text: what the assistant responded (text only, no tool calls)
    - timestamp: when this exchange happened
    """
    messages = parse_session_file(path)
    dialogues = []

    for msg in messages:
        m = msg.get("message", {})
        role = m.get("role", "")
        content_blocks = m.get("content", [])
        ts = msg.get("timestamp", "")

        if role == "user":
            # Extract text from user message
            texts = []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif isinstance(block, str):
                    texts.append(block)
            if texts:
                dialogues.append(
                    {
                        "role": "user",
                        "text": "\n".join(texts),
                        "timestamp": ts,
                    }
                )

        elif role == "assistant":
            # Extract only text blocks (skip thinking, toolCall)
            texts = []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text", "")
                    if t:
                        texts.append(t)
            if texts:
                dialogues.append(
                    {
                        "role": "assistant",
                        "text": "\n".join(texts),
                        "timestamp": ts,
                    }
                )

    return dialogues


def estimate_scan_tokens(dialogues):
    """Rough estimate of tokens needed to analyze dialogues.

    Heuristic: ~1.5 tokens per CJK char, ~0.75 tokens per ASCII word.
    Plus overhead for signal analysis prompt (~500 tokens per conversation).
    """
    total_chars = sum(len(d["text"]) for d in dialogues)
    # Rough: 1 token per 2 chars for mixed CJK/ASCII
    content_tokens = total_chars // 2
    # Analysis overhead: ~500 tokens per conversation for the signal extraction prompt
    overhead = 500
    return content_tokens + overhead


def cmd_detect(args):
    """Detect signal candidates from conversation history using pattern matching."""
    # Import pattern matcher from detect-signal.py (same directory)
    import importlib.util

    detect_path = os.path.join(os.path.dirname(__file__), "detect-signal.py")
    if not os.path.exists(detect_path):
        # Fallback: try detect_signal.py (underscore)
        detect_path = os.path.join(os.path.dirname(__file__), "detect_signal.py")
    if os.path.exists(detect_path):
        spec = importlib.util.spec_from_file_location("detect_signal", detect_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _detect_signals = mod.detect_signals
    else:
        print(json.dumps({"status": "error", "message": "detect-signal.py not found"}))
        sys.exit(1)

    if args.session:
        files = find_all_session_files()
        matched = [f for f in files if args.session in os.path.basename(f)]
        target_files = matched
    else:
        files = find_all_session_files()
        all_info = [get_session_info(f) for f in files]
        all_info = [i for i in all_info if i]
        filtered = filter_by_days(all_info, args.days)
        target_files = [i["path"] for i in filtered]

    all_candidates = []
    session_summaries = []

    for path in target_files:
        info = get_session_info(path)
        dialogues = extract_dialogue(path)
        session_candidates = []

        for turn in dialogues:
            if turn["role"] != "user":
                continue
            candidates = _detect_signals(turn["text"])
            if not candidates:
                continue
            if args.min_confidence == "high":
                candidates = [c for c in candidates if c["confidence"] == "high"]
            if candidates:
                session_candidates.append(
                    {
                        "user_text": turn["text"][:200],
                        "timestamp": turn["timestamp"],
                        "candidates": candidates,
                    }
                )

        if session_candidates:
            session_summaries.append(
                {
                    "session_id": info["session_id"]
                    if info
                    else os.path.basename(path),
                    "candidate_count": len(session_candidates),
                    "candidates": session_candidates,
                }
            )
            all_candidates.extend(session_candidates)

    type_counts = {}
    for sc in all_candidates:
        for cand in sc["candidates"]:
            t = cand["type"]
            type_counts[t] = type_counts.get(t, 0) + 1

    print(
        json.dumps(
            {
                "status": "ok",
                "total_candidates": len(all_candidates),
                "by_type": type_counts,
                "sessions_scanned": len(target_files),
                "sessions_with_signals": len(session_summaries),
                "sessions": session_summaries,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_list(args):
    """List all sessions."""
    files = find_all_session_files()
    if not files:
        print(
            json.dumps(
                {"status": "ok", "sessions": [], "message": "未找到对话记录"},
                ensure_ascii=False,
            )
        )
        return

    sessions = []
    for f in files:
        info = get_session_info(f)
        if info:
            sessions.append(info)

    # Sort by last timestamp desc
    sessions.sort(key=lambda s: s.get("last_timestamp", ""), reverse=True)

    print(
        json.dumps(
            {
                "status": "ok",
                "total_sessions": len(sessions),
                "sessions": sessions,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


def cmd_estimate(args):
    """Estimate token cost for scanning."""
    files = find_all_session_files()
    all_info = [get_session_info(f) for f in files]
    all_info = [i for i in all_info if i]

    filtered = filter_by_days(all_info, args.days)

    total_dialogues = []
    session_details = []
    for info in filtered:
        dialogues = extract_dialogue(info["path"])
        est = estimate_scan_tokens(dialogues)
        user_turns = sum(1 for d in dialogues if d["role"] == "user")
        total_dialogues.extend(dialogues)
        session_details.append(
            {
                "session_id": info["session_id"],
                "is_archived": info["is_archived"],
                "user_turns": user_turns,
                "estimated_tokens": est,
                "date_range": f"{info.get('first_timestamp', '?')[:10]} ~ {info.get('last_timestamp', '?')[:10]}",
            }
        )

    total_est = sum(s["estimated_tokens"] for s in session_details)
    total_user_turns = sum(s["user_turns"] for s in session_details)

    # Rough cost estimate (GPT-4 class: ~$0.03/1K input tokens)
    cost_low = total_est * 0.005 / 1000  # cheap model
    cost_high = total_est * 0.03 / 1000  # expensive model

    print(
        json.dumps(
            {
                "status": "ok",
                "days": args.days,
                "sessions_count": len(filtered),
                "total_user_turns": total_user_turns,
                "estimated_tokens": total_est,
                "cost_estimate": {
                    "low": f"${cost_low:.2f}",
                    "high": f"${cost_high:.2f}",
                    "note": "取决于使用的模型",
                },
                "sessions": session_details,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_extract(args):
    """Extract dialogue for signal analysis."""
    if args.session:
        # Find specific session
        files = find_all_session_files()
        matched = [f for f in files if args.session in os.path.basename(f)]
        if not matched:
            print(
                json.dumps(
                    {"status": "error", "message": f"未找到 session: {args.session}"},
                    ensure_ascii=False,
                )
            )
            sys.exit(1)
        target_files = matched
    else:
        files = find_all_session_files()
        all_info = [get_session_info(f) for f in files]
        all_info = [i for i in all_info if i]
        filtered = filter_by_days(all_info, args.days)
        target_files = [i["path"] for i in filtered]

    results = []
    for path in target_files:
        info = get_session_info(path)
        dialogues = extract_dialogue(path)
        if not dialogues:
            continue
        results.append(
            {
                "session_id": info["session_id"] if info else os.path.basename(path),
                "is_archived": info["is_archived"] if info else False,
                "date_range": f"{info.get('first_timestamp', '?')[:10]} ~ {info.get('last_timestamp', '?')[:10]}"
                if info
                else "?",
                "dialogue": dialogues,
            }
        )

    print(
        json.dumps(
            {
                "status": "ok",
                "sessions_extracted": len(results),
                "sessions": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description="Scan OpenClaw conversation history")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List all sessions")

    est = sub.add_parser("estimate", help="Estimate scan cost")
    est.add_argument(
        "--days", type=int, default=7, help="Scan last N days (default: 7)"
    )

    ext = sub.add_parser("extract", help="Extract dialogues for signal analysis")
    ext.add_argument(
        "--days", type=int, default=7, help="Extract last N days (default: 7)"
    )
    ext.add_argument(
        "--session", type=str, default=None, help="Extract specific session by ID"
    )

    det = sub.add_parser("detect", help="Detect signal candidates from history")
    det.add_argument(
        "--days", type=int, default=7, help="Scan last N days (default: 7)"
    )
    det.add_argument(
        "--session", type=str, default=None, help="Detect from specific session"
    )
    det.add_argument(
        "--min-confidence",
        choices=["high", "low"],
        default="low",
        help="Minimum confidence to include (default: low)",
    )

    args = parser.parse_args()
    if args.command == "list":
        cmd_list(args)
    elif args.command == "estimate":
        cmd_estimate(args)
    elif args.command == "extract":
        cmd_extract(args)
    elif args.command == "detect":
        cmd_detect(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
