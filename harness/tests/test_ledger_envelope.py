# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
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
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

"""The in-process ledger and the wire envelope must stay in lockstep."""

from __future__ import annotations

from pathlib import Path

from encre.contracts.events import EventEnvelope
from encre.contracts.version import EVENT_SCHEMA_VERSION
from encre.events.ledger import EventSourcedBus, LedgerEvent


def test_ledger_event_carries_versioned_fields() -> None:
    event = LedgerEvent(seq=1, ts=123.0, kind="turn_started", session_id="s")
    assert event.turn == 0
    assert event.trace_id == ""
    assert event.schema_version == EVENT_SCHEMA_VERSION
    record = event.to_dict()
    assert record["schema_version"] == EVENT_SCHEMA_VERSION
    assert "trace_id" in record and "turn" in record


def test_envelope_and_ledger_roundtrip_both_ways() -> None:
    event = LedgerEvent(
        seq=9,
        ts=1.5,
        kind="tool_call",
        session_id="s1",
        payload={"tool": "echo"},
        refs=["r1"],
        trace_id="trace-9",
        turn=4,
    )
    envelope = EventEnvelope.from_ledger(event)
    assert envelope.trace_id == "trace-9"
    assert envelope.turn == 4
    assert envelope.schema_version == EVENT_SCHEMA_VERSION

    back = LedgerEvent.from_envelope(envelope)
    assert back.to_dict() == event.to_dict()


def test_legacy_ledger_json_defaults_to_schema_one() -> None:
    legacy = {
        "seq": 3,
        "ts": 10.0,
        "kind": "old_event",
        "session_id": "s",
        "payload": {},
        "refs": [],
        "event_id": "abc",
    }
    event = LedgerEvent.from_dict(legacy)
    assert event.schema_version == 1
    assert event.turn == 0


def test_bus_publish_replay_and_project(tmp_path: Path) -> None:
    bus = EventSourcedBus("test", ledger_path=tmp_path / "events.jsonl")
    first = bus.publish("turn_started", session_id="s1", payload={"turn": 1})
    second = bus.publish("tool_call", session_id="s1", payload={"tool": "echo"})
    assert first is not None and second is not None
    assert first.seq < second.seq

    replayed = bus.replay("s1")
    assert [e.kind for e in replayed] == ["turn_started", "tool_call"]
    assert all(e.schema_version == EVENT_SCHEMA_VERSION for e in replayed)

    projected = bus.project("s1")
    assert projected  # projection is a non-empty dict
    bus.close()

    # A fresh bus over the same file sees the same history.
    reopened = EventSourcedBus("test2", ledger_path=tmp_path / "events.jsonl")
    assert [e.kind for e in reopened.replay("s1")] == ["turn_started", "tool_call"]
    reopened.close()


def test_bus_events_project_onto_envelopes(tmp_path: Path) -> None:
    bus = EventSourcedBus("test", ledger_path=tmp_path / "events.jsonl")
    bus.publish("turn_started", session_id="s1", payload={"turn": 1})
    event = bus.replay("s1")[0]
    envelope = EventEnvelope.from_ledger(event)
    assert envelope.kind == "turn_started"
    assert envelope.session_id == "s1"
    assert envelope.payload["turn"] == 1
    bus.close()
