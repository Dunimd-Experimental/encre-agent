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

"""Typed event stream: a projection over the append-only backbone.

:class:`EventStream` is the single contract between the agent loop and every
consumer of agent events (frontend transport, plugin taps, telemetry).  The
loop publishes typed events; any number of subscribers iterate them
asynchronously via :meth:`EventStream.subscribe`.

The stream no longer owns the truth.  When it is attached to an
:class:`~encre.events.ledger.EventSourcedBus` (the production wiring) every
published event is written to the backbone as a :class:`LedgerEvent` and the
stream simply *projects* the typed object back out to its subscribers::

    EventStream.publish(ev)
        -> bus.publish(kind, payload={"event": ev})
        -> bus fans the LedgerEvent out to the stream's backbone observer
        -> observer extracts ev and fans it out to subscribers

The kind is ``"lifecycle:<ClassName>"`` for the typed session-lifecycle
dataclasses (``SessionStarted`` / ``TurnStarted`` / ...) and ``"agent_event"``
for anything else.  Constructing a stream without a bus (``EventStream("x")``)
keeps the historical standalone behaviour so lightweight embeddings and tests
still work -- the fan-out logic itself is shared, only the ingress differs.

Design notes:

* **Fan-out** -- every subscriber gets its own bounded queue; a slow
  consumer never blocks the loop or other subscribers.
* **Backpressure** -- when a subscriber's queue is full the newest event is
  dropped *for that subscriber only* and a warning is logged.
* **Lifecycle** -- :meth:`close` ends every subscription gracefully
  (pending buffered events are still delivered before iteration stops) and
  detaches the stream from the backbone.
"""

import asyncio
from typing import TYPE_CHECKING, AsyncIterator, Generic, TypeVar

from encre.events.lifecycle import (
    CheckpointCreated,
    SessionEnded,
    SessionStarted,
    TurnEnded,
    TurnStarted,
    UserMessagePersisted,
)
from encre.logging_config import get_logger

if TYPE_CHECKING:
    from encre.events.ledger import EventSourcedBus, LedgerEvent

logger = get_logger(__name__)

T = TypeVar("T")

#: Typed lifecycle dataclasses published as ``lifecycle:<ClassName>``.
_LIFECYCLE_TYPES = (
    SessionStarted,
    SessionEnded,
    TurnStarted,
    TurnEnded,
    CheckpointCreated,
    UserMessagePersisted,
)


def _event_kind(event: object) -> str:
    """Return the backbone kind for a typed stream event.

    Lifecycle dataclasses get a ``"lifecycle:<ClassName>"`` kind so the ledger
    read-model can distinguish them; every other payload is a generic
    ``"agent_event"``.
    """
    if isinstance(event, _LIFECYCLE_TYPES):
        return f"lifecycle:{type(event).__name__}"
    return "agent_event"


class _Subscription(Generic[T]):
    """A single subscriber view over an :class:`EventStream`.

    Async-iterates events from a private queue until the stream is closed
    and the queue is drained.
    """

    __slots__ = ("_queue", "_stream")

    def __init__(self, stream: "EventStream[T]") -> None:
        self._stream = stream
        self._queue: asyncio.Queue[T | None] = asyncio.Queue(
            maxsize=stream.buffer_size,
        )

    def _deliver(self, event: T) -> bool:
        """Enqueue ``event``; return ``False`` when the queue is full."""
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            return False

    def _seal(self) -> None:
        """Enqueue the end-of-stream sentinel (drop-oldest if full)."""
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def __aiter__(self) -> "_Subscription[T]":
        return self

    async def __anext__(self) -> T:
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event


class EventStream(Generic[T]):
    """Typed publish/subscribe event stream projected over the backbone.

    Args:
        name: Human-readable stream name (used in logs).
        buffer_size: Per-subscriber queue bound.  Events are dropped for a
            subscriber whose queue is full; the publisher never blocks.
        bus: Optional :class:`~encre.events.ledger.EventSourcedBus`.  When
            supplied (production wiring) every published event flows through
            the backbone and is projected back to subscribers.  When omitted
            the stream fans out directly.
        session_id: Session id stamped on events published to the backbone
            (events that carry their own ``session_id`` win).
    """

    def __init__(
        self,
        name: str = "events",
        buffer_size: int = 4096,
        *,
        bus: "EventSourcedBus | None" = None,
        session_id: str = "",
    ) -> None:
        self.name = name
        self.buffer_size = buffer_size
        self.session_id = session_id
        self._bus = bus
        self._subs: list[_Subscription[T]] = []
        self._closed = False
        self._lock = asyncio.Lock()
        if self._bus is not None:
            self._bus.subscribe("*", self._on_backbone)

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has been called."""
        return self._closed

    @property
    def subscriber_count(self) -> int:
        """Number of active subscriptions (diagnostics)."""
        return len(self._subs)

    def _on_backbone(self, ledger_event: "LedgerEvent") -> None:
        """Project a backbone event back to this stream's subscribers.

        Only events carrying a typed object under ``payload["event"]`` are
        projected; unrelated backbone traffic (state changes, phase hooks) is
        ignored so subscribers see exactly what they used to.
        """
        event = ledger_event.payload.get("event")
        if event is None:
            return
        self._fanout(event)

    def _fanout(self, event: T) -> bool:
        """Deliver ``event`` to every subscriber without blocking."""
        if self._closed:
            return False
        if not self._subs:
            return True
        for sub in list(self._subs):
            if not sub._deliver(event):
                logger.warning(
                    "[event_stream:%s] subscriber queue full -- event dropped "
                    "(type=%s)",
                    self.name, type(event).__name__,
                )
        return True

    def publish(self, event: T) -> bool:
        """Publish ``event`` through the backbone (or fan out directly).

        Args:
            event: The typed event to broadcast.

        Returns:
            ``True`` when the event was accepted (published to the backbone,
            or fanned out to the subscribers / into the void); ``False`` when
            the stream -- or its backbone -- is closed.
        """
        if self._closed:
            return False
        if self._bus is None:
            return self._fanout(event)
        session_id = self.session_id or str(getattr(event, "session_id", "") or "")
        recorded = self._bus.publish(
            _event_kind(event),
            session_id=session_id,
            payload={"event": event},
        )
        return recorded is not None

    def subscribe(self) -> _Subscription[T]:
        """Open a new subscription (async iterator over events).

        Returns:
            An async iterator yielding every event published after this
            call, until :meth:`close` drains the stream.

        Raises:
            RuntimeError: When the stream is already closed.
        """
        if self._closed:
            raise RuntimeError(f"event stream '{self.name}' is closed")
        sub = _Subscription(self)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: _Subscription[T]) -> None:
        """Remove a subscription so it no longer receives events."""
        try:
            self._subs.remove(sub)
        except ValueError:
            pass

    async def close(self) -> None:
        """Close the stream, detach from the backbone and seal subscriptions.

        Buffered events remain consumable; after the buffer drains the
        async iterators raise :class:`StopAsyncIteration`.
        """
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._bus is not None:
                self._bus.unsubscribe("*", self._on_backbone)
            for sub in list(self._subs):
                sub._seal()
            self._subs.clear()
