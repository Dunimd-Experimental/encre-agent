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

"""EventEnvelope: versioning, JSON safety, and forward compatibility."""

from __future__ import annotations

import json

import pytest

from encre.contracts.events import EventEnvelope, new_envelope
from encre.contracts.version import EVENT_SCHEMA_VERSION


def test_defaults_and_ids() -> None:
    env = EventEnvelope(kind="turn_started")
    assert env.seq == 0
    assert env.turn == 0
    assert env.trace_id == ""
    assert env.schema_version == EVENT_SCHEMA_VERSION
    assert env.event_id and len(env.event_id) == 16
    assert EventEnvelope(kind="x").event_id != env.event_id


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": ""},
        {"kind": "x", "seq": -1},
        {"kind": "x", "schema_version": 0},
        {"kind": "x", "payload": []},
    ],
)
def test_validation_rejects_bad_records(kwargs: dict) -> None:
    with pytest.raises((ValueError, TypeError)):
        EventEnvelope(**kwargs)


def test_roundtrip_preserves_envelope_fields() -> None:
    env = new_envelope(
        "tool_call",
        payload={"tool": "file_read", "path": "a.txt"},
        session_id="s1",
        turn=3,
        trace_id="t-1",
        refs=["call-1"],
    )
    env.seq = 7
    again = EventEnvelope.from_json(env.to_json())
    assert again.to_dict() == env.to_dict()


def test_unknown_keys_are_preserved_not_dropped() -> None:
    record = {
        "kind": "future_event",
        "seq": 1,
        "payload": {"known": 1},
        "brand_new_field": {"nested": True},
    }
    env = EventEnvelope.from_dict(record)
    assert env.payload["known"] == 1
    assert env.payload["_extra"]["brand_new_field"] == {"nested": True}
    # and it survives another trip
    assert EventEnvelope.from_json(env.to_json()).payload["_extra"]


def test_legacy_records_default_to_schema_one() -> None:
    env = EventEnvelope.from_dict({"kind": "old", "seq": 2})
    assert env.schema_version == 1
    assert env.turn == 0
    assert env.trace_id == ""


def test_payload_is_json_native() -> None:
    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    env = EventEnvelope(kind="x", payload={"obj": Opaque(), "t": (1, 2)})
    json.dumps(env.to_dict())
    assert env.payload["obj"] == "<opaque>"
    assert env.payload["t"] == [1, 2]


def test_refs_are_stringified() -> None:
    env = EventEnvelope(kind="x", refs=[1, "a"])  # type: ignore[list-item]
    assert env.refs == ["1", "a"]
