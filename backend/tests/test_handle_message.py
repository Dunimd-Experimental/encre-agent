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

"""Tests for BaseAdapter.handle_message inbound routing (Phase 1).

These tests exercise the canonical inbound entry point: source normalization
when an event arrives without a SessionSource, dispatch to a registered
message handler or the legacy process_with_stream, and the two-level guard that
queues follow-up messages per session key until the active turn drains.

They also verify that messages from different chats run concurrently, that
bypass commands such as /stop still respect session ordering, and that pending
messages are drained in FIFO order once the active session completes.
"""

import asyncio

import pytest

from encre.gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from encre.gateway.session import SessionSource, build_session_key


class _StubAdapter(BasePlatformAdapter):
    """Minimal adapter that only implements the abstract send() method."""

    name = "stub"

    def __init__(self):
        """Run the real base initialization and add a sent-message recorder."""
        super().__init__()
        self.sent: list[tuple[str, str]] = []

    async def connect(self, *, is_reconnect=False) -> bool:
        """Report a successful connection."""
        return True

    async def disconnect(self) -> None:
        """No-op disconnect for the stub adapter."""
        pass

    async def send(self, chat_id, content, *, reply_to=None, metadata=None):
        """Record the sent content and report success."""
        self.sent.append((chat_id, content))
        return SendResult(success=True, message_id="m1")


def _dm_event(text="hi", chat_id="123", user_id="42", platform="stub"):
    """Build a direct-message MessageEvent with a fully populated source."""
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=platform, chat_id=chat_id, chat_type="dm", user_id=user_id),
    )


# ---- source normalization ----


@pytest.mark.asyncio
async def test_verify_handle_message_dispatches_event_even_when_source_is_missing():
    """Validate that handle_message dispatches even when the event has no source.

    Constructs a MessageEvent without a source, dispatches it, and asserts the
    handler received exactly one event, confirming source is optional and
    downstream processors may synthesize it from platform context if needed.
    """
    a = _StubAdapter()
    seen = []

    async def handler(adapter, event):
        """Record every dispatched event in ``seen``."""
        seen.append(event)

    a.set_message_handler(handler)
    # No source on the event.
    event = MessageEvent(text="hi")
    await a.handle_message(event)
    assert len(seen) == 1
    # Without a source the event is dispatched unchanged.
    assert seen[0].text == "hi"


# ---- dispatch ----


@pytest.mark.asyncio
async def test_verify_handle_message_awaits_registered_handler_with_event():
    """Validate that handle_message awaits the handler and passes the event.

    Registers a handler, emits a direct-message event, and asserts the handler
    saw the exact event object, confirming the dispatch contract that the
    handler is the single source of truth for inbound processing.
    """
    a = _StubAdapter()
    seen = []

    async def handler(adapter, event):
        """Record every dispatched event in ``seen``."""
        seen.append(event)

    a.set_message_handler(handler)
    event = _dm_event("hello")
    await a.handle_message(event)
    assert seen == [event]


@pytest.mark.asyncio
async def test_verify_handle_message_logs_warning_and_drops_when_no_handler():
    """Validate that a missing handler is logged and dropped, not raised.

    Emits a message on an adapter with no handler registered and asserts no
    exception escapes, so a misconfigured adapter stays resilient during
    development and testing.
    """
    a = _StubAdapter()
    event = _dm_event("hello", chat_id="9", user_id="u1")
    # Should not raise - just logs warning
    await a.handle_message(event)


# ---- two-level guard ----


@pytest.mark.asyncio
async def test_verify_concurrent_message_on_same_session_is_queued_not_dispatched():
    """Validate that a same-session message is queued while the first is active.

    Runs two tasks against the same chat, asserts the second handler has not
    started while the first is midway, and confirms both complete in order
    after the first releases, so the guard prevents races on per-session state
    such as tool-call invariants.
    """
    a = _StubAdapter()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def handler(adapter, event):
        """Log the start of each event and signal the first one."""
        order.append(f"start:{event.text}")
        first_started.set()
        if event.text == "first":
            await release_first.wait()
        order.append(f"end:{event.text}")

    a.set_message_handler(handler)

    t1 = asyncio.create_task(a.handle_message(_dm_event("first", chat_id="1")))
    await first_started.wait()

    # Second message on the same session_key should queue (same chat_id "1").
    t2 = asyncio.create_task(a.handle_message(_dm_event("second", chat_id="1")))
    await asyncio.sleep(0)  # let t2 run to the guard (queues, returns)
    await t2

    # The second handler has NOT run yet -- it's queued.
    assert order == ["start:first"]

    release_first.set()
    await t1
    # Drain runs the queued message.
    for _ in range(10):
        await asyncio.sleep(0)
        if "end:second" in order:
            break
    assert "start:second" in order
    assert "end:second" in order


@pytest.mark.asyncio
async def test_verify_messages_from_different_chats_run_concurrently():
    """Validate that different chats run concurrently without queueing.

    Runs one task per chat and asserts both handlers have started before either
    releases, confirming the guard is scoped per session key and that separate
    sessions never block each other.
    """
    a = _StubAdapter()
    release = asyncio.Event()
    started: list[str] = []

    async def handler(adapter, event):
        """Record the event text, then stall until the test releases it."""
        started.append(event.text)
        if event.text == "a":
            await release.wait()

    a.set_message_handler(handler)

    ta = asyncio.create_task(a.handle_message(_dm_event("a", chat_id="1")))
    await asyncio.sleep(0)
    # Different chat_id -> different session key -> runs concurrently.
    tb = asyncio.create_task(a.handle_message(_dm_event("b", chat_id="2")))
    await asyncio.sleep(0)
    assert "a" in started
    assert "b" in started  # Both started since different session keys
    release.set()
    await ta
    await tb


@pytest.mark.asyncio
async def test_verify_bypass_command_runs_after_drain_not_immediately():
    """Validate that a bypass command waits for the active session to drain.

    Starts a long-running handler, sends /stop for the same chat, and asserts
    /stop is not processed until the active turn releases and the queue drains.
    Bypass commands still respect session ordering -- they just skip the
    concurrency guard on their own turn.
    """
    a = _StubAdapter()
    release = asyncio.Event()
    first_started = asyncio.Event()
    seen: list[str] = []

    async def handler(adapter, event):
        """Record the event text and pause on the sentinel message."""
        seen.append(event.text)
        if event.text == "running":
            first_started.set()
            await release.wait()

    a.set_message_handler(handler)

    t1 = asyncio.create_task(a.handle_message(_dm_event("running", chat_id="1")))
    await first_started.wait()

    # /stop arrives while session "1" is active -- queued by guard.
    stop_event = MessageEvent(
        text="/stop",
        source=SessionSource(platform="stub", chat_id="1", chat_type="dm", user_id="42"),
    )
    t2 = asyncio.create_task(a.handle_message(stop_event))
    await asyncio.sleep(0)
    await t2
    # /stop is queued (same session key) and will drain later.
    assert "/stop" not in seen

    release.set()
    await t1
    # After drain, /stop should have been processed.
    for _ in range(20):
        await asyncio.sleep(0)
        if "/stop" in seen:
            break
    assert "/stop" in seen


# ---- drain on completion ----


@pytest.mark.asyncio
async def test_verify_pending_messages_are_drained_in_order_after_completion():
    """Validate that queued messages are re-dispatched in FIFO order.

    Queues two messages while the first handler is active, asserts only the
    first is handled during activity, then asserts the queued messages complete
    in order after release, since message ordering is a correctness invariant
    for chat applications.
    """
    a = _StubAdapter()
    first_started = asyncio.Event()
    release = asyncio.Event()
    handled: list[str] = []

    async def handler(adapter, event):
        """Record the event text, then stall until the test releases it."""
        handled.append(event.text)
        if event.text == "first":
            first_started.set()
            await release.wait()

    a.set_message_handler(handler)

    t1 = asyncio.create_task(a.handle_message(_dm_event("first", chat_id="c")))
    await first_started.wait()
    # Queue two messages while active.
    t_q1 = asyncio.create_task(a.handle_message(_dm_event("q1", chat_id="c")))
    t_q2 = asyncio.create_task(a.handle_message(_dm_event("q2", chat_id="c")))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await t_q1
    await t_q2
    assert handled == ["first"]

    release.set()
    await t1
    # Both queued messages get drained (in order).
    for _ in range(20):
        await asyncio.sleep(0)
        if len(handled) >= 3:
            break
    assert handled == ["first", "q1", "q2"]
