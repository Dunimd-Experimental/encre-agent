#!/usr/bin/env python3

# Copyright (c) 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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


"""Session replay player.

Reads a session's encrypted telemetry JSONL log, decrypts each line, and
exposes a cursor-based interface for scrubbing through the recorded event
stream (``step_forward`` / ``step_backward`` / ``jump_to_turn``).  Used by
the observability layer to reconstruct what an agent did during a past
session -- for debugging, audit, and cost/token review.

The JSONL is written by :class:`encre.telemetry.EncreTelemetry`: one event
per line, each line encrypted via ``encre.crypto.encrypt`` (with a plaintext
fallback when encryption fails).  Events are timestamped; the player sorts
by timestamp on load so out-of-order writes (e.g. from concurrent tool
prefetch) still produce a linear timeline.
"""


import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from encre.crypto import decrypt
from encre.logging_config import get_logger

logger = get_logger("encre.replay")


@dataclass
class ReplayEvent:
    """A single decrypted telemetry event for replay."""

    index: int
    event_type: str
    data: dict[str, Any]
    timestamp: float = 0.0
    turn_number: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "event_type": self.event_type,
            "data": self.data,
            "timestamp": self.timestamp,
            "turn_number": self.turn_number,
        }


class ReplayPlayer:
    """Cursor-based player over a session's encrypted telemetry log."""

    def __init__(self, session_id: str, telemetry_dir: str | None = None) -> None:
        self.session_id = session_id
        self._telemetry_dir = telemetry_dir or self._default_telemetry_dir()
        self.events: list[ReplayEvent] = []
        self._cursor = -1
        self._load()

    @staticmethod
    def _default_telemetry_dir() -> str:
        # Telemetry belongs with the rest of the session data.  The old
        # ``~/.encre/telemetry`` split meant replay read a directory that
        # backups never contained, and that ``purge_session`` never cleaned.
        try:
            from encre.paths import get_data_dir

            return str(get_data_dir("telemetry", ensure=False))
        except Exception:  # pragma: no cover - defensive
            return os.path.expanduser(
                os.path.join("~", ".dunimd", "encre", "telemetry")
            )

    def _jsonl_path(self) -> str:
        return os.path.join(self._telemetry_dir, f"{self.session_id}.jsonl")

    def _load(self) -> None:
        path = self._jsonl_path()
        if not os.path.exists(path):
            logger.warning("No telemetry log found at %s", path)
            return
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError as exc:
            logger.error("Failed to read telemetry log %s: %s", path, exc)
            return
        events = []
        for idx, line in enumerate(lines):
            if not line.strip():
                continue
            data = self._parse_line(line)
            if data is not None:
                events.append(self._build_event(idx, data))
        # Stable sort by timestamp preserves write order for equal timestamps.
        events.sort(key=lambda e: (e.timestamp, e.index))
        self.events = events
        self._cursor = -1

    @staticmethod
    def _parse_line(line: str) -> dict[str, Any] | None:
        try:
            try:
                text = decrypt(line)
            except Exception:
                text = line
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    def _build_event(index: int, data: dict[str, Any]) -> ReplayEvent:
        evt_type = str(data.get("type") or data.get("event") or "unknown")
        ts = float(data.get("timestamp") or data.get("ts") or 0.0)
        turn = int(data.get("turn") or data.get("turn_number") or 0)
        # tool_call / retry events aren't turn-scoped in the log; leave 0.
        if evt_type != "turn":
            turn = 0
        return ReplayEvent(
            index=index, event_type=evt_type, data=data, timestamp=ts, turn_number=turn
        )

    # -------------------------------------------------------------------------
    # navigation
    # -------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.events)

    @property
    def cursor(self) -> int:
        return self._cursor

    def current(self) -> ReplayEvent | None:
        if 0 <= self._cursor < len(self.events):
            return self.events[self._cursor]
        return None

    def step_forward(self) -> ReplayEvent | None:
        if self._cursor + 1 >= len(self.events):
            # Already at or past the last event: clamp to last, signal end.
            if self.events:
                self._cursor = len(self.events) - 1
            return None
        self._cursor += 1
        return self.events[self._cursor]

    def step_backward(self) -> ReplayEvent | None:
        if self._cursor <= 0:
            return None
        self._cursor -= 1
        return self.events[self._cursor]

    def jump_to_index(self, index: int) -> ReplayEvent | None:
        if not self.events or index < 0 or index >= len(self.events):
            return None
        self._cursor = index
        return self.events[self._cursor]

    def jump_to_turn(self, turn_number: int) -> ReplayEvent | None:
        if turn_number <= 0:
            self._cursor = -1
            return None
        for ev in self.events:
            if ev.event_type == "turn" and ev.turn_number == turn_number:
                self._cursor = ev.index if 0 <= ev.index < len(self.events) else self.events.index(ev)
                return self.events[self._cursor]
        return None

    def reset(self) -> None:
        self._cursor = -1

    # -------------------------------------------------------------------------
    # views
    # -------------------------------------------------------------------------

    def event_stream(self) -> list[ReplayEvent]:
        return list(self.events)

    def turn_boundaries(self) -> list[int]:
        """Return the event indexes where a new turn begins."""
        boundaries = []
        for i, ev in enumerate(self.events):
            if ev.event_type == "turn":
                boundaries.append(i)
        return boundaries

    def summary(self) -> dict[str, Any]:
        turn_events = [ev for ev in self.events if ev.event_type == "turn"]
        return {
            "session_id": self.session_id,
            "total_events": len(self.events),
            "turns": len(turn_events),
            "first_timestamp": self.events[0].timestamp if self.events else None,
            "last_timestamp": self.events[-1].timestamp if self.events else None,
        }


# ---------------------------------------------------------------------------
# Loop events.jsonl replay (StateEventLog stream, distinct from the encrypted
# telemetry log played back by ``ReplayPlayer`` above)
# ---------------------------------------------------------------------------

#: Event types that carry a runtime state transition (mirrors
#: ``encre.loop.state._STATE_EVENT_TYPES``).
_STATE_EVENT_TYPES = frozenset({"state", "state_recovered"})


@dataclass
class ReplayResult:
    """Reconstructed session view over one ``events.jsonl`` stream."""

    session_id: str
    final_state: str = "idle"
    state_timeline: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results_index: dict[str, Any] = field(default_factory=dict)
    messages_summary: list[dict[str, Any]] = field(default_factory=list)
    permission_decisions: list[dict[str, Any]] = field(default_factory=list)
    undo_events: list[dict[str, Any]] = field(default_factory=list)
    ended: dict[str, Any] | None = None
    integrity: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "final_state": self.final_state,
            "state_timeline": self.state_timeline,
            "tool_calls": self.tool_calls,
            "tool_results_index": self.tool_results_index,
            "messages_summary": self.messages_summary,
            "permission_decisions": self.permission_decisions,
            "undo_events": self.undo_events,
            "ended": self.ended,
            "integrity": self.integrity,
        }


def _events_path(session_id: str, data_root: str | Path | None) -> Path:
    """Resolve ``<data_root>/sessions/<session_id>/events.jsonl``.

    Mirrors the ``LoopStateMachine`` wiring convention
    (``Path("data", "sessions", session_id)``): the default data root is the
    ``data/`` tree relative to the working directory.
    """
    root = Path(data_root) if data_root else Path("data")
    return root / "sessions" / session_id / "events.jsonl"


def _read_events(path: Path, target_seq: int | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse the JSONL event stream, tolerating a damaged crash tail.

    Returns ``(events, integrity)`` where *events* are the parsed dicts in
    file order truncated to ``seq <= target_seq`` (``None`` = no truncation)
    and *integrity* carries line/parse statistics.
    """
    events: list[dict[str, Any]] = []
    total_lines = 0
    corrupted_lines = 0
    last_seq = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                total_lines += 1
                try:
                    event = json.loads(line)
                except Exception:
                    corrupted_lines += 1
                    continue
                if not isinstance(event, dict):
                    corrupted_lines += 1
                    continue
                seq = event.get("seq")
                if isinstance(seq, int) and seq > last_seq:
                    last_seq = seq
                if target_seq is not None and (
                    not isinstance(seq, int) or seq > target_seq
                ):
                    continue
                events.append(event)
    except FileNotFoundError:
        logger.debug("exception suppressed", exc_info=True)
    integrity = {
        "total_lines": total_lines,
        "corrupted_lines": corrupted_lines,
        "last_seq": last_seq,
    }
    return events, integrity


def replay_session(
    session_id: str,
    *,
    data_root: str | Path | None = None,
    target_seq: int | None = None,
) -> ReplayResult:
    """Rebuild a session summary from its ``events.jsonl`` event stream.

    Reads the append-only loop event log written by
    :class:`encre.loop.state.StateEventLog` (state transitions, tool/undo/
    permission/inbox events, ``session_ended`` attribution) and derives a
    structured replay view.

    Args:
        session_id: The session directory name under ``<data_root>/sessions``.
        data_root: Data root holding ``sessions/``; defaults to ``data/``
            (same relative resolution the loop's ``StateEventLog`` uses).
        target_seq: When given, only events with ``seq <= target_seq`` are
            replayed (inclusive truncation); ``None`` replays everything.

    Returns:
        A :class:`ReplayResult` with the final state, state timeline,
        tool-call index, message (steering inbox) summary, permission
        decisions, undo events, terminal attribution and integrity stats.
    """
    path = _events_path(session_id, data_root)
    events, integrity = _read_events(path, target_seq)
    result = ReplayResult(session_id=session_id)
    result.integrity = integrity
    by_tool_seqs: dict[str, list[int]] = {}
    for event in events:
        etype = str(event.get("type", ""))
        seq = event.get("seq")
        ts = event.get("ts", "")
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if etype in _STATE_EVENT_TYPES:
            result.state_timeline.append({
                "seq": seq,
                "ts": ts,
                "from": str(event.get("from", "")),
                "to": str(event.get("to", "")),
            })
            result.final_state = str(event.get("to") or event.get("from") or "") \
                or result.final_state
        elif etype == "tool_executed":
            entry = {
                "seq": seq,
                "ts": ts,
                "tool": str(payload.get("tool", "")),
                "path": str(payload.get("path", "")),
                "action": str(payload.get("action", "")),
                "undo_seq": payload.get("undo_seq"),
                "success": bool(payload.get("success", True)),
            }
            result.tool_calls.append(entry)
            # Result-reference index: prefer an explicit call id when the
            # payload carries one, otherwise list tool_executed sequences
            # grouped by tool name.
            call_id = payload.get("tool_call_id") or payload.get("id")
            if call_id:
                result.tool_results_index[str(call_id)] = {
                    "seq": seq,
                    "ts": ts,
                    "tool": entry["tool"],
                }
            elif entry["tool"]:
                by_tool_seqs.setdefault(entry["tool"], []).append(seq)
        elif etype in ("inbox_enqueue", "inbox_drain"):
            result.messages_summary.append({
                "seq": seq,
                "ts": ts,
                "event": etype,
                "count": payload.get("count", 1),
                "source": str(payload.get("source", "")),
            })
        elif etype == "permission_decision":
            result.permission_decisions.append({
                "seq": seq,
                "ts": ts,
                "tool": str(payload.get("tool", "")),
                "effect": str(payload.get("effect", "")),
                "source": str(payload.get("source", "")),
            })
        elif etype in ("undo_executed", "undo_empty"):
            entry = {"seq": seq, "ts": ts, "event": etype}
            if etype == "undo_executed":
                entry.update({
                    "tool": str(payload.get("tool", "")),
                    "path": str(payload.get("path", "")),
                    "action": str(payload.get("action", "")),
                    "undone": bool(payload.get("undone", False)),
                })
            result.undo_events.append(entry)
        elif etype == "session_ended":
            result.ended = {"seq": seq, "ts": ts, **payload}
    if by_tool_seqs:
        result.tool_results_index["by_tool"] = by_tool_seqs
    if not result.final_state:
        result.final_state = "idle"
    return result


def _print_human(result: ReplayResult) -> None:
    """Print a human-readable replay summary."""
    integrity = result.integrity
    print(f"Session   : {result.session_id}")
    print(f"Events    : integrity={integrity}")
    print(f"Final state: {result.final_state}")
    if result.ended:
        print(f"Ended     : {result.ended.get('reason', '')} -- "
              f"{result.ended.get('detail', '')}")
    print(f"State transitions ({len(result.state_timeline)}):")
    for entry in result.state_timeline:
        print(f"  #{entry['seq']} {entry['ts']} {entry['from']} -> {entry['to']}")
    print(f"Tool executions ({len(result.tool_calls)}):")
    for entry in result.tool_calls:
        print(f"  #{entry['seq']} {entry['ts']} {entry['tool']} "
              f"undo_seq={entry['undo_seq']} success={entry['success']}")
    if result.tool_results_index:
        print(f"Tool results index: {result.tool_results_index}")
    print(f"Messages (steering inbox) ({len(result.messages_summary)}):")
    for entry in result.messages_summary:
        print(f"  #{entry['seq']} {entry['ts']} {entry['event']} "
              f"count={entry['count']} source={entry['source']}")
    print(f"Permission decisions ({len(result.permission_decisions)}):")
    for entry in result.permission_decisions:
        print(f"  #{entry['seq']} {entry['ts']} {entry['tool']} "
              f"effect={entry['effect']} source={entry['source']}")
    print(f"Undo events ({len(result.undo_events)}):")
    for entry in result.undo_events:
        print(f"  #{entry['seq']} {entry['ts']} {entry['event']} "
              f"{entry.get('tool', '')} {entry.get('path', '')}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m encre.replay <session_id> [--seq N]``."""
    parser = argparse.ArgumentParser(
        prog="python -m encre.replay",
        description="Replay a session's loop event stream (events.jsonl).",
    )
    parser.add_argument("session_id", help="Session id under <data-root>/sessions/")
    parser.add_argument(
        "--seq", type=int, default=None,
        help="Truncate replay to events with seq <= N (default: all)",
    )
    parser.add_argument(
        "--data-root", default=None,
        help="Data root holding sessions/ (default: ./data)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)
    result = replay_session(
        args.session_id,
        data_root=args.data_root,
        target_seq=args.seq,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
