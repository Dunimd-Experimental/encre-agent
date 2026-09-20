#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

from __future__ import annotations

"""Tests for BaseAdapter.handle_message inbound routing (Phase 1).

Covers the canonical inbound entry: source normalization, the two-level
guard (queue while active, bypass for /stop etc.), dispatch to a registered
message handler or the legacy process_with_stream, and pending-message
drainage on completion.
"""

import asyncio

import pytest

from encre.gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from encre.gateway.session import SessionSource, build_session_key


class _StubAdapter(BasePlatformAdapter):
    """Minimal adapter: only implements the abstract send()."""

    name = "stub"

    def __init__(self):
        super().__init__()
        self.sent: list[tuple[str, str]] = []

    async def connect(self, *, is_reconnect=False) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def send(self, chat_id, content, *, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))
        return SendResult(success=True, message_id="m1")


def _dm_event(text="hi", chat_id="123", user_id="42", platform="stub"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=platform, chat_id=chat_id, chat_type="dm", user_id=user_id),
    )


# 鈹€鈹€ source normalization 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


@pytest.mark.asyncio
async def test_verify_handle_message_dispatches_event_even_when_source_is_missing():
    """Validate that handle_message dispatches the event to the registered
    handler even when the event has no source, confirming source is optional.

    The test exercises construction of a MessageEvent without a source and
    asserts the handler received exactly one event because downstream
    processors may synthesize a source from platform context if needed.
    """
    a = _StubAdapter()
    seen = []

    async def handler(adapter, event):
        seen.append(event)

    a.set_message_handler(handler)
    # No source on the event.
    event = MessageEvent(text="hi")
    await a.handle_message(event)
    assert len(seen) == 1
    # Without source, the event is dispatched as-is.
    assert seen[0].text == "hi"


# 鈹€鈹€ dispatch 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


@pytest.mark.asyncio
async def test_verify_handle_message_awaits_registered_handler_with_event():
    """Validate that handle_message awaits the registered handler and passes
    the event object through, confirming the dispatch contract.

    The test exercises handler registration and emission and asserts the
    seen list contains the exact event because the handler is the single
    point of truth for inbound message processing.
    """
    a = _StubAdapter()
    seen = []

    async def handler(adapter, event):
        seen.append(event)

    a.set_message_handler(handler)
    event = _dm_event("hello")
    await a.handle_message(event)
    assert seen == [event]


@pytest.mark.asyncio
async def test_verify_handle_message_logs_warning_and_drops_when_no_handler():
    """Validate that handle_message does not raise when no handler is
    registered, instead logging a warning and dropping the message.

    The test exercises emission with an unconfigured adapter and asserts
    no exception is raised because the adapter must be resilient to
    misconfiguration during development and testing.
    """
    a = _StubAdapter()
    event = _dm_event("hello", chat_id="9", user_id="u1")
    # Should not raise - just logs warning
    await a.handle_message(event)


# 鈹€鈹€ two-level guard 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


@pytest.mark.asyncio
async def test_verify_concurrent_message_on_same_session_is_queued_not_dispatched():
    """Validate that a second message arriving on the same session_key while
    the first is active is queued, not dispatched concurrently.

    The test exercises two tasks on chat_id='1', asserts the second handler
    has not started when the first is midway, and confirms both complete
    in order after the first releases because the guard prevents race
    conditions on per-session state like tool call invariants.
    """
    a = _StubAdapter()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def handler(adapter, event):
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
    """Validate that messages from different chats (different session keys)
    run concurrently without queueing, confirming the guard is scoped per-session.

    The test exercises two tasks on chat_id='1' and chat_id='2' and asserts
    both handlers have started before either releases because different
    sessions must not block each other.
    """
    a = _StubAdapter()
    release = asyncio.Event()
    started: list[str] = []

    async def handler(adapter, event):
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
    """Validate that a bypass command like /stop arriving while a session is
    active is queued by the guard and processed only after the active
    session completes and drains the queue.

    The test exercises a running handler and a /stop event on the same
    chat_id and asserts /stop is not in seen until after the first handler
    releases and the drain loop completes because bypass commands still
    respect session ordering 鈥?they just skip the concurrency guard on
    their own turn.
    """
    a = _StubAdapter()
    release = asyncio.Event()
    first_started = asyncio.Event()
    seen: list[str] = []

    async def handler(adapter, event):
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


# 鈹€鈹€ drain on completion 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


@pytest.mark.asyncio
async def test_verify_pending_messages_are_drained_in_order_after_completion():
    """Validate that queued messages are re-dispatched in FIFO order after
    the active session completes, confirming the drain loop preserves
    message ordering.

    The test exercises two queued messages while the first handler is
    active, asserts only the first is handled during activity, then
    asserts both queued messages complete in order after release because
    message ordering is a correctness invariant for chat applications.
    """
    a = _StubAdapter()
    first_started = asyncio.Event()
    release = asyncio.Event()
    handled: list[str] = []

    async def handler(adapter, event):
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
