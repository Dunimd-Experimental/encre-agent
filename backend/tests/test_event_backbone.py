#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

from __future__ import annotations

"""Tests for the unified event backbone.

Covers the migration of every previously separate event mechanism onto the
single append-only :class:`EventSourcedBus`:

* the phase-hook ``emit`` semantics carried over from the retired phase bus,
* :class:`EventStream`'s unchanged public surface while being projected over
  the backbone,
* :class:`StateManager` change mirroring onto the backbone (with a graceful
  fallback when no bus is attached),
* session-lifecycle events flowing through the backbone.
"""

import asyncio

import pytest

from encre.compact.loop_state.manager import StateManager
from encre.compact.loop_state.state import LoopState
from encre.compact.loop_state.transition import TurnTransition
from encre.events import (
    PHASE_PRE_MODEL_REQUEST,
    EventSourcedBus,
    EventStream,
    SessionStarted,
    TurnStarted,
)


class _FakeSession:
    """Minimal session double: metadata + id, optionally a backbone."""

    def __init__(self, session_id: str = "sess-1") -> None:
        """Create a session with an id and an empty metadata dict."""
        self.id = session_id
        self.metadata: dict = {}


class TestPhaseHookSemantics:
    """``EventSourcedBus.emit`` preserves the retired phase-bus contract."""

    async def test_emit_returns_last_non_none_in_registration_order(self):
        """emit must invoke handlers in registration order and return the last non-None result."""
        bus = EventSourcedBus("phases")
        calls: list[str] = []

        async def first(**kw):
            """Record its invocation and return None, so it is skipped by emit."""
            calls.append("first")
            return None

        async def second(**kw):
            """Record its invocation and return a result that a later handler overwrites."""
            calls.append("second")
            return {"v": 1}

        async def third(**kw):
            """Record its invocation and return the result emit should report."""
            calls.append("third")
            return {"v": 2}

        bus.on(PHASE_PRE_MODEL_REQUEST, first)
        bus.on(PHASE_PRE_MODEL_REQUEST, second)
        bus.on(PHASE_PRE_MODEL_REQUEST, third)

        result = await bus.emit(PHASE_PRE_MODEL_REQUEST, messages=[], tools=[])
        assert result == {"v": 2}
        assert calls == ["first", "second", "third"]

    async def test_emit_isolates_raising_handler(self):
        """A handler that raises must not prevent later handlers from running."""
        bus = EventSourcedBus("phases")

        async def boom(**kw):
            """Raise to simulate a broken phase hook."""
            raise ValueError("bad")

        async def after(**kw):
            """Return the result that must still be visible past the failure."""
            return "ok"

        bus.on(PHASE_PRE_MODEL_REQUEST, boom)
        bus.on(PHASE_PRE_MODEL_REQUEST, after)
        assert await bus.emit(PHASE_PRE_MODEL_REQUEST, messages=[]) == "ok"

    async def test_emit_records_phase_on_backbone(self):
        """emit must append a phase event to the backbone with the phase as its kind."""
        bus = EventSourcedBus("phases")

        async def handler(**kw):
            """No-op phase hook, present only so emit has something to dispatch to."""
            return None

        bus.on(PHASE_PRE_MODEL_REQUEST, handler)
        kinds: list[str] = []
        bus.subscribe("*", lambda ev: kinds.append(ev.kind))
        await bus.emit(PHASE_PRE_MODEL_REQUEST, messages=["m"])
        assert kinds == [f"phase:{PHASE_PRE_MODEL_REQUEST}"]


class TestEventStreamPublicSurface:
    """The stream API is unchanged, whether or not a backbone is attached."""

    async def test_standalone_publish_fans_out_to_subscriber(self):
        """publish on a backbone-less stream must reach its subscriber and be reported as sent."""
        stream: EventStream = EventStream("standalone")
        sub = stream.subscribe()
        assert stream.publish({"n": 1}) is True
        assert await asyncio.wait_for(sub.__anext__(), timeout=1) == {"n": 1}
        assert stream.subscriber_count == 1

    async def test_full_queue_drops_without_blocking(self):
        """A full bounded queue must drop the newest event without blocking or raising."""
        stream: EventStream = EventStream("bounded", buffer_size=1)
        sub = stream.subscribe()
        assert stream.publish("first") is True
        # Queue is full: the newest event is dropped for this subscriber but
        # publishing must not block or raise.
        assert stream.publish("second") is True
        assert stream.publish("third") is True
        assert await asyncio.wait_for(sub.__anext__(), timeout=1) == "first"
        await stream.close()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(sub.__anext__(), timeout=1)

    async def test_close_seals_and_refuses_publish(self):
        """close must mark the stream sealed, reject further publishes, and end the iterator."""
        stream: EventStream = EventStream("closing")
        sub = stream.subscribe()
        await stream.close()
        assert stream.closed is True
        assert stream.publish("late") is False
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(sub.__anext__(), timeout=1)


class TestEventStreamBackboneProjection:
    """With a bus attached, events really flow through the backbone."""

    async def test_publish_records_on_backbone_and_fans_out(self):
        """publish must append to the backbone under the stream's own kind and fan out the payload."""
        bus = EventSourcedBus("backbone")
        stream: EventStream = EventStream("agent", bus=bus, session_id="s1")
        sub = stream.subscribe()

        seen: list = []
        bus.subscribe("agent_event", lambda ev: seen.append(ev))

        assert stream.publish({"kind": "delta"}) is True
        assert bus.count == 1
        assert [ev.kind for ev in seen] == ["agent_event"]
        assert await asyncio.wait_for(sub.__anext__(), timeout=1) == {"kind": "delta"}
        assert seen[0].session_id == "s1"

    async def test_lifecycle_event_uses_lifecycle_kind(self):
        """Session lifecycle events must be recorded with a lifecycle-prefixed kind."""
        bus = EventSourcedBus("backbone")
        stream: EventStream = EventStream("agent", bus=bus)
        sub = stream.subscribe()

        kinds: list[str] = []
        bus.subscribe("*", lambda ev: kinds.append(ev.kind))

        stream.publish(SessionStarted(session_id="s2"))
        assert kinds == ["lifecycle:SessionStarted"]
        received = await asyncio.wait_for(sub.__anext__(), timeout=1)
        assert isinstance(received, SessionStarted)
        assert received.session_id == "s2"

    async def test_close_detaches_from_backbone(self):
        """close must stop recording to the backbone and end the subscriber's iteration."""
        bus = EventSourcedBus("backbone")
        stream: EventStream = EventStream("agent", bus=bus)
        sub = stream.subscribe()
        await stream.close()
        assert stream.publish(TurnStarted(turn=1)) is False
        assert bus.count == 0
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(sub.__anext__(), timeout=1)


class TestStateManagerBackbone:
    """StateManager mirrors changes onto the backbone; works without one."""

    async def test_change_publishes_state_changed(self):
        """Setting state must emit a state_changed event and keep the local change log."""
        session = _FakeSession("sess-42")
        session.event_bus = EventSourcedBus("state")
        mgr = StateManager(session)

        received: list[dict] = []
        session.event_bus.subscribe("state_changed", lambda ev: received.append(ev.payload))

        mgr.task_stage = "plan"
        assert session.event_bus.count == 1
        assert received[0]["key"] == "task_stage"
        assert received[0]["new"] == "plan"
        assert received[0]["seq"] == 1
        # Local change log is preserved.
        assert mgr.change_log()[0]["new"] == "plan"

    async def test_no_bus_degrades_to_local_only(self):
        """Without an attached bus, state changes must be recorded locally without raising."""
        session = _FakeSession("sess-43")
        mgr = StateManager(session)
        mgr.task_stage = "review"  # must not raise
        assert mgr.change_log()[-1]["new"] == "review"


class TestLoopStateTransitionsBackbone:
    """Per-run turn transitions are mirrored onto the backbone."""

    async def test_transition_publishes_state_transition(self):
        """recording a transition must emit a state_transition event with the transition payload."""
        bus = EventSourcedBus("loop")
        state = LoopState.create(event_bus=bus, session_id="sess-9")

        received: list[dict] = []
        bus.subscribe("state_transition", lambda ev: received.append(ev.payload))

        state.transitions.record(TurnTransition.NEXT_TURN, turn=3, detail="tool_use")
        assert bus.count == 1
        assert received[0]["transition"] == "next_turn"
        assert received[0]["turn"] == 3
        assert state.transitions.last is TurnTransition.NEXT_TURN

    async def test_transition_without_bus_is_local_only(self):
        """Transitions must still be recorded locally when no bus is supplied."""
        state = LoopState.create()
        state.transitions.record(TurnTransition.TEXT_ONLY, turn=1)
        assert len(state.transitions) == 1
