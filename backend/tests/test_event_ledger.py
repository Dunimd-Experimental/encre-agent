#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
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
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

from __future__ import annotations

"""Tests for the append-only event ledger (:mod:`encre.events.ledger`).

Covers the public surface of :class:`EventSourcedBus` and
:class:`LedgerEvent`: monotonic sequencing, encrypted persistence + replay,
replay idempotency, observer failure isolation, the phase-hook-compatible
``emit`` semantics, read-model projection, serialisation fallback, and the
closed-bus contract.
"""

import asyncio
from pathlib import Path

from encre.events.ledger import EventSourcedBus, LedgerEvent
from encre.secure_io import read_jsonl


def _publish(
    bus: EventSourcedBus,
    kind: str,
    session_id: str = "sess-1",
    payload: dict | None = None,
    refs: list[str] | None = None,
) -> LedgerEvent:
    """Publish an event with a default payload and assert it was accepted."""
    event = bus.publish(
        kind,
        session_id=session_id,
        payload=payload if payload is not None else {"seed": kind},
        refs=refs,
    )
    assert event is not None
    return event


class TestSequencingAndIdentity:
    """seq is monotonic and event ids are unique."""

    def test_seq_increments_from_one(self):
        bus = EventSourcedBus("no-persist")  # in-memory, no ledger file
        e1 = _publish(bus, "turn_started")
        e2 = _publish(bus, "state_changed")
        e3 = _publish(bus, "phase:pre_model_request", session_id="")
        assert [e1.seq, e2.seq, e3.seq] == [1, 2, 3]
        assert isinstance(e1, LedgerEvent)
        assert e1.kind == "turn_started"
        assert e3.session_id == ""

    def test_event_ids_are_unique(self):
        bus = EventSourcedBus("no-persist")
        ids = {_publish(bus, f"kind-{i}").event_id for i in range(20)}
        assert len(ids) == 20
        assert all(ids)


class TestPersistenceAndReplay:
    """Encrypted on-disk persistence round-trips and replay is idempotent."""

    def test_replay_round_trip_after_fresh_bus(self, tmp_path: Path):
        path = tmp_path / "ledger" / "events.jsonl"
        bus = EventSourcedBus("persist", ledger_path=path)
        e1 = _publish(bus, "turn_started", session_id="session-A")
        e2 = _publish(bus, "phase:pre_model_request", session_id="session-A")
        e3 = _publish(bus, "state_changed", session_id="session-B")
        bus.flush()

        # A brand-new bus over the same ledger must read the records back from
        # disk (through decryption), proving this is not an in-memory illusion.
        fresh = EventSourcedBus("persist", ledger_path=path)
        replayed = fresh.replay("session-A")
        assert [ev.seq for ev in replayed] == [1, 2]
        assert [ev.kind for ev in replayed] == ["turn_started", "phase:pre_model_request"]
        assert replayed[0].event_id == e1.event_id
        assert replayed[1].event_id == e2.event_id
        assert replayed[1].payload == {"seed": "phase:pre_model_request"}
        assert fresh.replay("session-B")[0].event_id == e3.event_id

    def test_ledger_is_encrypted_at_rest(self, tmp_path: Path):
        path = tmp_path / "ledger" / "events.jsonl"
        bus = EventSourcedBus("persist", ledger_path=path)
        _publish(bus, "secret_kind", session_id="s", payload={"token": "plaintext-marker"})
        bus.flush()
        raw = path.read_text(encoding="utf-8")
        assert "secret_kind" not in raw
        assert "plaintext-marker" not in raw

    def test_replay_is_idempotent(self, tmp_path: Path):
        path = tmp_path / "ledger" / "events.jsonl"
        bus = EventSourcedBus("persist", ledger_path=path)
        for i in range(5):
            _publish(bus, f"kind-{i}", session_id="s")
        bus.flush()

        fresh = EventSourcedBus("persist", ledger_path=path)
        first = fresh.replay("s")
        second = fresh.replay("s")
        assert first == second
        assert [ev.seq for ev in first] == [1, 2, 3, 4, 5]
        # Replay must never publish a new event or touch in-memory state.
        assert fresh.count == 0

    def test_flush_batch_buffers_until_flush(self, tmp_path: Path):
        path = tmp_path / "ledger" / "events.jsonl"
        bus = EventSourcedBus("batch", ledger_path=path, flush_batch=100)
        _publish(bus, "a", session_id="s")
        _publish(bus, "b", session_id="s")
        assert read_jsonl(path) == []  # still buffered
        bus.flush()
        assert [rec["kind"] for rec in read_jsonl(path)] == ["a", "b"]
        fresh = EventSourcedBus("batch", ledger_path=path)
        assert [ev.kind for ev in fresh.replay("s")] == ["a", "b"]

    async def test_aflush_writes_without_blocking(self, tmp_path: Path):
        path = tmp_path / "ledger" / "events.jsonl"
        bus = EventSourcedBus("aflush", ledger_path=path, flush_batch=100)
        _publish(bus, "a", session_id="s")
        await bus.aflush()
        assert [rec["kind"] for rec in read_jsonl(path)] == ["a"]

    def test_persist_false_never_writes(self, tmp_path: Path):
        path = tmp_path / "ledger" / "events.jsonl"
        bus = EventSourcedBus("nopersist", ledger_path=path, persist=False)
        _publish(bus, "a", session_id="s")
        bus.flush()
        assert not path.exists()
        assert EventSourcedBus("x", ledger_path=path).replay("s") == []

    def test_no_ledger_path_is_pure_memory(self):
        bus = EventSourcedBus("mem")
        _publish(bus, "turn_started", session_id="s")
        bus.flush()  # no-op
        assert bus.replay("s") == []


class TestObserverIsolation:
    """A raising observer never blocks other observers or the publisher."""

    def test_sync_observer_raise_is_isolated(self):
        bus = EventSourcedBus("iso")
        seen: list[str] = []

        def boom(_event: LedgerEvent) -> None:
            raise RuntimeError("observer exploded")

        def collector(event: LedgerEvent) -> None:
            seen.append(event.kind)

        bus.subscribe("target", boom)
        bus.subscribe("target", collector)
        event = _publish(bus, "target", session_id="s")  # must not raise
        assert seen == ["target"]
        assert event is not None

    def test_wildcard_subscriber_receives_everything(self):
        bus = EventSourcedBus("iso")
        kinds: list[str] = []
        bus.subscribe("*", lambda ev: kinds.append(ev.kind))
        _publish(bus, "a")
        _publish(bus, "b")
        assert kinds == ["a", "b"]

    async def test_async_observer_raise_is_isolated(self):
        bus = EventSourcedBus("iso")
        received: list[int] = []

        async def boom(_event: LedgerEvent) -> None:
            raise RuntimeError("async boom")

        async def collector(event: LedgerEvent) -> None:
            received.append(event.seq)

        bus.subscribe("target", boom)
        bus.subscribe("target", collector)
        _publish(bus, "target", session_id="s")  # must not raise
        await asyncio.sleep(0)  # let the scheduled tasks run
        assert received == [1]


class TestEmitPhaseSemantics:
    """emit keeps phase-hook behaviour: last non-None result, isolated failures."""

    async def test_emit_returns_last_non_none(self):
        bus = EventSourcedBus("phases")

        async def none_handler(**kw):
            return None

        async def first(**kw):
            return {"a": 1}

        async def second(**kw):
            return {"b": 2}

        bus.on("pre_model_request", none_handler)
        bus.on("pre_model_request", first)
        bus.on("pre_model_request", second)
        result = await bus.emit("pre_model_request", messages=[], tools=[])
        assert result == {"b": 2}

    async def test_emit_isolates_raising_handler(self):
        bus = EventSourcedBus("phases")
        calls: list[str] = []

        async def boom(**kw):
            calls.append("boom")
            raise ValueError("bad handler")

        async def after(**kw):
            calls.append("after")
            return "ok"

        bus.on("pre_model_request", boom)
        bus.on("pre_model_request", after)
        result = await bus.emit("pre_model_request", messages=[])
        assert result == "ok"
        assert calls == ["boom", "after"]

    async def test_emit_records_phase_on_ledger(self, tmp_path: Path):
        path = tmp_path / "ledger" / "events.jsonl"
        bus = EventSourcedBus("phases", ledger_path=path)

        async def handler(**kw):
            return None

        bus.on("pre_model_request", handler)
        await bus.emit("pre_model_request", messages=["m"])
        bus.flush()
        fresh = EventSourcedBus("phases", ledger_path=path)
        assert [ev.kind for ev in fresh.replay("")] == ["phase:pre_model_request"]

    async def test_has_listeners(self):
        bus = EventSourcedBus("phases")

        async def h(**kw):
            return None

        assert bus.has_listeners("tool_call") is False
        bus.on("tool_call", h)
        assert bus.has_listeners("tool_call") is True
        bus.off("tool_call", h)
        assert bus.has_listeners("tool_call") is False


class TestProjection:
    """project folds replayed events into the documented read-model shape."""

    def test_project_counts(self, tmp_path: Path):
        path = tmp_path / "ledger" / "events.jsonl"
        bus = EventSourcedBus("proj", ledger_path=path)
        _publish(bus, "turn_started", session_id="sess-A")
        _publish(bus, "turn_started", session_id="sess-A")
        _publish(bus, "state_changed", session_id="sess-A")
        _publish(bus, "tool_call", session_id="sess-A", refs=["call_1"])
        _publish(bus, "tool_call", session_id="sess-A", refs=["call_2"])
        # A different session must be excluded from the projection.
        _publish(bus, "turn_started", session_id="sess-B")
        bus.flush()

        proj = EventSourcedBus("proj", ledger_path=path).project("sess-A")
        assert proj["session_id"] == "sess-A"
        assert proj["events"] == 5
        assert proj["last_seq"] == 5
        assert proj["kinds"] == {
            "turn_started": 2,
            "state_changed": 1,
            "tool_call": 2,
        }
        assert proj["turns"] == 2
        assert sorted(proj["tool_calls"]) == ["call_1", "call_2"]

    def test_project_empty_session(self, tmp_path: Path):
        path = tmp_path / "ledger" / "events.jsonl"
        EventSourcedBus("proj", ledger_path=path).flush()
        proj = EventSourcedBus("proj", ledger_path=path).project("ghost")
        assert proj == {
            "session_id": "ghost",
            "events": 0,
            "last_seq": 0,
            "kinds": {},
            "turns": 0,
            "tool_calls": [],
        }


class TestSerialisationFallback:
    """Non-JSON payload values degrade safely without raising."""

    def test_unsafe_payload_degrades_without_raising(self, tmp_path: Path):
        path = tmp_path / "ledger" / "events.jsonl"
        bus = EventSourcedBus("fallback", ledger_path=path)
        bus.publish("opaque", session_id="s", payload={"marker": object(), "ok": 1})
        bus.flush()

        events = EventSourcedBus("fallback", ledger_path=path).replay("s")
        assert len(events) == 1
        payload = events[0].payload
        assert payload["ok"] == 1
        # The non-serialisable value became a repr string.
        assert isinstance(payload["marker"], str)
        assert "object at 0x" in payload["marker"]

    def test_nested_unsafe_value_degrades(self, tmp_path: Path):
        path = tmp_path / "ledger" / "events.jsonl"
        bus = EventSourcedBus("fallback", ledger_path=path)
        bus.publish("nested", session_id="s", payload={"l": [{1, 2, 3}, "x"]})
        bus.flush()
        events = EventSourcedBus("fallback", ledger_path=path).replay("s")
        assert isinstance(events[0].payload["l"][0], str)
        assert events[0].payload["l"][1] == "x"


class TestClosedBus:
    """close() refuses new events; replay/projection keep working."""

    def test_close_then_publish_returns_none(self, tmp_path: Path):
        path = tmp_path / "ledger" / "events.jsonl"
        bus = EventSourcedBus("close", ledger_path=path)
        _publish(bus, "turn_started", session_id="s")
        bus.close()
        assert bus.closed is True
        assert bus.publish("turn_started", session_id="s") is None
        assert bus.count == 1
        assert len(read_jsonl(path)) == 1

    async def test_close_blocks_emit(self):
        bus = EventSourcedBus("close")

        async def h(**kw):
            raise AssertionError("handler must not run on a closed bus")

        bus.on("p", h)
        bus.close()
        result = await bus.emit("p")
        assert result is None
        assert bus.count == 0

    def test_close_forces_flush(self, tmp_path: Path):
        path = tmp_path / "ledger" / "events.jsonl"
        bus = EventSourcedBus("close", ledger_path=path, flush_batch=100)
        _publish(bus, "turn_started", session_id="s")
        assert read_jsonl(path) == []
        bus.close()
        assert len(read_jsonl(path)) == 1
