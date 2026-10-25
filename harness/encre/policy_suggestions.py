#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

"""Data flywheel: aggregate approval signals into policy suggestions.

Pure counting/ratio aggregation (no LLM) over persisted permission
decision records, producing evidence-backed suggestions for loosening or
tightening the permission policy.

Signal sources (merged; a session found in both is counted once, with the
telemetry record winning because it carries the richer ``matched_rule``):

* ``<data-root>/telemetry/<session>.jsonl`` -- encrypted JSONL written by
  :class:`encre.telemetry.EncreTelemetry`, read through
  :func:`encre.crypto.decrypt` exactly like the in-process readers
  (plaintext lines are tolerated for robustness).
* ``<data-root>/sessions/<id>/events.jsonl`` -- the plain loop event
  stream (``permission_decision`` / ``tool_executed`` /
  ``undo_executed`` events from :class:`encre.loop.state.StateEventLog`).

Pattern derivation (mirrors the rule-engine semantics ``tool(primary)``;
a pattern without parentheses matches the primary argument alone):

* A telemetry ``matched_rule`` shaped ``rules[N] tool:pattern`` contributes
  the rule pattern verbatim -- it already carries the winning rule's
  pattern text.
* Otherwise the tool's primary argument, when observable via the
  correlated ``tool_executed`` event's ``path`` field (the most recent
  unbound ``permission_decision`` for the same tool), is collapsed to a
  same-directory glob so operations in one directory aggregate into a
  single bucket: ``file_write(/ws/src/a.py)`` -> pattern ``/ws/src/*``.
* Everything else (classifier/default decisions, declined asks) aggregates
  under the whole-tool pattern ``*``.

Aggregation rules (thresholds are module constants below):

* Allowlist suggestion: one (tool, pattern) bucket with at least
  ``ALLOW_MIN_COUNT`` allow decisions and zero incident sessions among its
  contributors.  A session is an incident for a bucket when it shows an
  ``undo_executed`` event, a failed execution of the same tool, or a
  ``network_blocked`` record.
* Tightening suggestion: at least ``TIGHTEN_MIN_DECISIONS`` ask+deny
  decisions and an incident ratio of at least
  ``TIGHTEN_MIN_INCIDENT_RATIO``; proposes ``deny`` when denies already
  dominate asks, otherwise keep ``ask``.

Output: ``<data-root>/improve/allowlist_suggestions.json`` written
atomically (temp file + ``os.replace``).  CLI: ``python -m
encre.policy_suggestions [--data-root D] [--json]``; the aggregation
function is also importable for schedulers.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from encre.crypto import decrypt
from encre.logging_config import get_logger
from encre.policy_version import policy_versions_stamped

logger = get_logger(__name__)

#: Minimum allow decisions for one (tool, pattern) before an allowlist
#: suggestion is emitted.
ALLOW_MIN_COUNT = 20
#: Minimum ask+deny decisions for one (tool, pattern) before a tightening
#: suggestion is considered.
TIGHTEN_MIN_DECISIONS = 10
#: Minimum incident ratio (incident sessions / contributing sessions) for
#: a tightening suggestion.
TIGHTEN_MIN_INCIDENT_RATIO = 0.30

#: Telemetry ``matched_rule`` prefix marking a network-policy block.
_NETWORK_BLOCKED_PREFIX = "network_blocked:"

__all__ = [
    "aggregate_approval_signals",
    "main",
    "write_suggestions",
]


def _default_data_root() -> Path:
    """Return the configured data directory (lazy import avoids cycles)."""
    from encre.config import get_data_dir
    return get_data_dir()


def _payload_dict(event: dict[str, Any]) -> dict[str, Any]:
    """Return an event's payload as a dict regardless of stored shape.

    :class:`encre.loop.state.StateEventLog` bounds payloads to a JSON string;
    other co-located writers of the same file may store the dict directly.
    """
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _pattern_from_matched_rule(matched_rule: str) -> str | None:
    """Extract the rule pattern from a ``rules[N] tool:pattern`` description.

    Returns ``None`` when the description is not a rule hit (classifier,
    default-effect or network-block records), so the caller falls back to
    the whole-tool pattern.
    """
    text = (matched_rule or "").strip()
    if not text.startswith("rules["):
        return None
    _index, sep, rest = text.partition(" ")
    if not sep:
        return None
    _tool, colon, pattern = rest.partition(":")
    if not colon:
        return None
    return pattern.strip() or "*"


def _collapse_path_pattern(primary: str) -> str:
    """Collapse a path-like primary argument to a same-directory glob.

    ``/ws/src/a.py`` -> ``/ws/src/*`` so every operation touching the same
    directory aggregates into one (tool, pattern) bucket.  Arguments
    without a path separator are kept verbatim (they are not paths).
    """
    text = (primary or "").strip()
    if not text:
        return "*"
    norm = text.replace("\\", "/")
    if "/" not in norm:
        return text
    parent = norm.rsplit("/", 1)[0]
    return f"{parent or '/'}/*"


def _new_session_signals() -> dict[str, Any]:
    """Create the per-session signal accumulator."""
    return {
        # {"tool": str, "effect": str, "pattern": str} in event order.
        "decisions": [],
        # Tools with at least one failed execution (attribution signal).
        "failures": set(),
        "undo": False,
        "network_blocked": False,
    }


def _collect_telemetry(
    root: Path,
    sessions: dict[str, dict[str, Any]],
) -> set[str]:
    """Fold permission/tool signals from encrypted telemetry JSONL files.

    Args:
        root: Data root holding the ``telemetry/`` directory.
        sessions: Per-session accumulator dict, updated in place.

    Returns:
        The set of session ids seen, so event-stream collection can skip
        those sessions (dedupe across the two sources).
    """
    telemetry_dir = root / "telemetry"
    if not telemetry_dir.is_dir():
        return set()
    seen: set[str] = set()
    for path in sorted(telemetry_dir.glob("*.jsonl")):
        try:
            lines = path.read_text("utf-8").splitlines()
        except Exception:
            continue
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(decrypt(raw))
            except Exception:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
            if not isinstance(data, dict):
                continue
            session_id = str(data.get("session_id") or path.stem)
            seen.add(session_id)
            sig = sessions.setdefault(session_id, _new_session_signals())
            event = str(data.get("event", ""))
            if event == "permission_decision":
                matched = str(data.get("matched_rule") or "")
                if matched.startswith(_NETWORK_BLOCKED_PREFIX):
                    sig["network_blocked"] = True
                sig["decisions"].append({
                    "tool": str(data.get("tool_name") or ""),
                    "effect": str(data.get("effect") or ""),
                    "pattern": _pattern_from_matched_rule(matched) or "*",
                })
            elif event == "tool_call":
                if not data.get("success", True):
                    sig["failures"].add(str(data.get("tool_name") or ""))
    return seen


def _collect_events(
    root: Path,
    sessions: dict[str, dict[str, Any]],
    skip: set[str],
) -> None:
    """Fold loop-event-stream signals from ``sessions/*/events.jsonl``.

    Sessions already covered by telemetry (``skip``) are ignored so a
    session present in both sources is not double counted.

    Args:
        root: Data root holding the ``sessions/`` directory.
        sessions: Per-session accumulator dict, updated in place.
        skip: Session ids already collected from telemetry.
    """
    sessions_dir = root / "sessions"
    if not sessions_dir.is_dir():
        return
    for session_dir in sorted(sessions_dir.iterdir()):
        if not session_dir.is_dir() or session_dir.name in skip:
            continue
        path = session_dir / "events.jsonl"
        if not path.is_file():
            continue
        try:
            lines = path.read_text("utf-8", errors="replace").splitlines()
        except Exception:
            continue
        sig = sessions.setdefault(session_dir.name, _new_session_signals())
        # tool -> most recent unbound permission_decision, refined with the
        # primary argument of the next tool_executed event for that tool.
        pending: dict[str, dict[str, Any]] = {}
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except Exception:
                continue  # corrupt crash tail; skip damaged lines
            if not isinstance(event, dict):
                continue
            etype = str(event.get("type", ""))
            payload = _payload_dict(event)
            if etype == "permission_decision":
                decision = {
                    "tool": str(payload.get("tool") or ""),
                    "effect": str(payload.get("effect") or ""),
                    "pattern": "*",
                }
                sig["decisions"].append(decision)
                pending[decision["tool"]] = decision
            elif etype == "tool_executed":
                tool = str(payload.get("tool") or "")
                if payload.get("success") is False:
                    sig["failures"].add(tool)
                decision = pending.pop(tool, None)
                if decision is not None and decision["pattern"] == "*":
                    primary = str(payload.get("path") or payload.get("action") or "")
                    decision["pattern"] = _collapse_path_pattern(primary)
            elif etype == "undo_executed":
                sig["undo"] = True


def _aggregate(
    sessions: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fold per-session signals into per-(tool, pattern) suggestions.

    Returns:
        ``(suggestions, tightenings)`` sorted by evidence strength.
    """
    buckets: dict[tuple[str, str], dict[str, int]] = {}
    contributors: dict[tuple[str, str], set[str]] = defaultdict(set)
    for session_id, sig in sessions.items():
        for decision in sig["decisions"]:
            key = (decision["tool"], decision["pattern"])
            bucket = buckets.setdefault(key, {
                "allow": 0, "ask": 0, "deny": 0,
                "incident_sessions": 0, "session_count": 0,
            })
            effect = decision["effect"]
            if effect in ("allow", "ask", "deny"):
                bucket[effect] += 1
            contributors[key].add(session_id)

    # Incident pass: a session is an incident for a bucket when it
    # contributed decisions to that bucket AND shows undo usage, a failed
    # execution of the same tool, or a network block.
    for (tool, pattern), bucket in buckets.items():
        incident = sum(
            1 for sid in contributors[(tool, pattern)]
            if sessions[sid]["undo"]
            or tool in sessions[sid]["failures"]
            or sessions[sid]["network_blocked"]
        )
        bucket["incident_sessions"] = incident
        bucket["session_count"] = len(contributors[(tool, pattern)])

    suggestions: list[dict[str, Any]] = []
    tightenings: list[dict[str, Any]] = []
    for (tool, pattern), bucket in buckets.items():
        evidence: dict[str, Any] = {
            "allow_count": bucket["allow"],
            "ask_count": bucket["ask"],
            "deny_count": bucket["deny"],
            "incident_count": bucket["incident_sessions"],
            "session_count": bucket["session_count"],
        }
        if bucket["allow"] >= ALLOW_MIN_COUNT and bucket["incident_sessions"] == 0:
            suggestions.append({
                "tool": tool,
                "pattern": pattern,
                "effect": "allow",
                "evidence": evidence,
            })
        gated = bucket["ask"] + bucket["deny"]
        ratio = (
            bucket["incident_sessions"] / bucket["session_count"]
            if bucket["session_count"] else 0.0
        )
        if gated >= TIGHTEN_MIN_DECISIONS and ratio >= TIGHTEN_MIN_INCIDENT_RATIO:
            tightenings.append({
                "tool": tool,
                "pattern": pattern,
                "effect": "deny" if bucket["deny"] >= bucket["ask"] else "ask",
                "evidence": {**evidence, "incident_ratio": round(ratio, 4)},
            })
    suggestions.sort(
        key=lambda s: (-s["evidence"]["allow_count"], s["tool"], s["pattern"])
    )
    tightenings.sort(
        key=lambda s: (-s["evidence"]["incident_ratio"], s["tool"], s["pattern"])
    )
    return suggestions, tightenings


def aggregate_approval_signals(data_root: Path | None = None) -> dict[str, Any]:
    """Aggregate persisted permission decisions into policy suggestions.

    Args:
        data_root: Data root holding ``telemetry/`` and ``sessions/``;
            defaults to the configured data directory.

    Returns:
        A report dict: ``generated_at``, ``policy_versions`` (same-source
        registry from :mod:`encre.policy_version`), ``suggestions``,
        ``tightenings`` and ``sessions_scanned``.
    """
    root = Path(data_root) if data_root is not None else _default_data_root()
    sessions: dict[str, dict[str, Any]] = {}
    telemetry_ids = _collect_telemetry(root, sessions)
    _collect_events(root, sessions, telemetry_ids)
    suggestions, tightenings = _aggregate(sessions)
    logger.debug(
        "policy_suggestions: scanned %d sessions -> %d suggestions / %d tightenings",
        len(sessions), len(suggestions), len(tightenings),
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "policy_versions": policy_versions_stamped(),
        "suggestions": suggestions,
        "tightenings": tightenings,
        "sessions_scanned": len(sessions),
    }


def write_suggestions(
    report: dict[str, Any],
    data_root: Path | None = None,
) -> Path:
    """Atomically persist *report* to the suggestions file.

    Args:
        report: The dict returned by :func:`aggregate_approval_signals`.
        data_root: Data root; defaults to the configured data directory.

    Returns:
        The written path ``<data-root>/improve/allowlist_suggestions.json``.
    """
    root = Path(data_root) if data_root is not None else _default_data_root()
    out_dir = root / "improve"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "allowlist_suggestions.json"
    tmp_path = out_dir / (out_path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, out_path)
    return out_path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m encre.policy_suggestions [--data-root D] [--json]``."""
    parser = argparse.ArgumentParser(
        prog="python -m encre.policy_suggestions",
        description="Aggregate approval signals into allowlist suggestions.",
    )
    parser.add_argument(
        "--data-root", default=None,
        help="Data root holding telemetry/ and sessions/ "
             "(default: configured data dir)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the full report JSON",
    )
    args = parser.parse_args(argv)
    data_root = Path(args.data_root) if args.data_root else None
    report = aggregate_approval_signals(data_root)
    out_path = write_suggestions(report, data_root)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"suggestions: {len(report['suggestions'])}")
        print(f"tightenings: {len(report['tightenings'])}")
        print(f"sessions scanned: {report['sessions_scanned']}")
        print(f"written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
