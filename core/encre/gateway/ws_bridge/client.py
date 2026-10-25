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

"""Adapter-side dialer for the remote-adapter WebSocket bridge.

This module implements :class:`GatewayClient`, the SDK a remote or
plugin platform adapter uses to talk to
:class:`encre.gateway.ws_bridge.server.WsBridgeServer`.  It is a thin
transport layer: it serialises
:class:`~encre.gateway.ws_bridge.protocol.GatewayMessage` objects over
one WebSocket and hides all the connection bookkeeping from callers,
so a plugin author only needs to call :meth:`GatewayClient.connect` and
:meth:`GatewayClient.submit` / :meth:`GatewayClient.submit_stream`.

Only remote adapters instantiate this client.  In-process core adapters
(:mod:`encre.gateway.platforms`) are driven directly by
:class:`encre.gateway.run.GatewayRunner` and never cross a socket.

Responsibilities handled here:

* Connection lifecycle -- connect, automatic reconnect with exponential
  backoff capped at :data:`RECONNECT_MAX`, and graceful disconnect.
* Liveness -- an application-level heartbeat loop pinging every
  :data:`HEARTBEAT_INTERVAL` seconds, on top of the WebSocket PING/PONG
  that :mod:`websockets` already performs.
* Request/response correlation -- every outbound request carries a
  ``request_id``; inbound frames bearing the same id are routed to a
  per-request queue so :meth:`submit` and :meth:`submit_stream` return
  the right data.  Frames with no matching ``request_id`` fall through
  to a shared broadcast queue.
* Two high-level entry points: :meth:`submit` (collect the whole reply
  into a string) and :meth:`submit_stream` (an async generator yielding
  :class:`~encre.utils.types.AgentEvent` objects as they arrive).

Public API: :class:`GatewayClient`; the backoff/heartbeat constants are
module-level tunables.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from encre.gateway.ws_bridge.protocol import GatewayMessage, GatewayOp
from encre.utils.types import AgentEvent, Finish, TextDelta, ToolResult

logger = logging.getLogger("encre.gateway.ws_bridge.client")

# Base delay (seconds) for the first reconnect attempt; doubled each retry.
RECONNECT_BASE = 1.0
# Upper bound on reconnect delay so retries never wait indefinitely.
RECONNECT_MAX = 30.0
# Seconds between application-level heartbeat pings sent to keep the
# connection alive.
HEARTBEAT_INTERVAL = 15.0


class GatewayClient:
    """WebSocket client connecting a remote adapter to a WsBridgeServer.

    Thin transport layer: sends and receives
    :class:`~encre.gateway.ws_bridge.protocol.GatewayMessage` objects,
    handles reconnection with exponential backoff and heartbeats, and
    routes server replies back to the in-flight request that is waiting
    for them.

    Lifecycle: construct with the adapter name and target URL, call
    :meth:`connect` (which returns once the HELLO handshake succeeds and
    keeps reconnecting in the same coroutine if it later drops), then
    :meth:`submit` / :meth:`submit_stream` at will, and finish with
    :meth:`disconnect`.  Background read and heartbeat tasks are created
    by :meth:`connect` and are owned by :attr:`_background_tasks`.

    Invariants:

    * :attr:`_connected` is only ever ``True`` between a successful HELLO
      handshake and the moment a read loop exits or
      :meth:`disconnect` is called.  All public entry points check it
      first and fail fast, so a caller never blocks on a dead socket.
    * Every pending ``request_id`` has at most one queue in
      :attr:`_pending`, inserted before the request is sent and removed
      on every exit path (terminal frame, timeout, cancellation, or
      disconnect).  Forgetting a removal would leak the queue forever
      because ``_pending`` is never garbage collected on its own.
    * Queue consumers treat ``None`` as a sentinel meaning "the
      connection is gone, stop waiting", which is how
      :meth:`disconnect` and the read loop's ``finally`` clause wake
      every blocked waiter at once.

    Thread/async safety: all state is touched only from the caller's own
    event loop, so there is no locking.  Running two event loops against
    one client instance is unsupported.

    Attributes:
        _name: Adapter identifier sent in the HELLO handshake.
        _url: Bridge server URL to dial.
        _capabilities: Capability strings advertised in the HELLO.
        _ws: The live WebSocket, ``None`` when disconnected.
        _running: Intent flag; ``False`` stops the reconnect loop.
        _connected: Whether the handshake is currently live.
        _seq: Monotonic send counter for log correlation.
        _listeners: Emitter-style lifecycle callback registry.
        _msg_queue: Shared queue for frames without a matching request.
        _pending: Per-``request_id`` reply queues.
        _background_tasks: Owned heartbeat/read background tasks.
    """

    def __init__(
        self,
        adapter_name: str,
        url: str = "ws://127.0.0.1:18792/gateway",
        capabilities: list[str] | None = None,
    ) -> None:
        """Construct the client without opening any connection.

        Args:
            adapter_name: The identifier this adapter presents to the
                server in the HELLO frame.  It becomes the adapter's key
                in the server's registry and is what the runner uses as
                the channel name for adapter-default sessions.
            url: The bridge server WebSocket URL.  Defaults to the
                documented localhost endpoint, which is what a
                containerised adapter in the same host would use.
            capabilities: Optional capability strings advertised during
                the handshake so the server can gate features.

        Raises:
            Nothing; :meth:`connect` performs the first network attempt.
        """
        self._name = adapter_name
        self._url = url
        self._capabilities = capabilities or []
        self._ws: Any = None
        self._running = False
        self._connected = False
        self._seq = 0
        self._listeners: dict[str, list[Any]] = {}
        self._msg_queue: asyncio.Queue[GatewayMessage | None] = asyncio.Queue()
        self._pending: dict[str, asyncio.Queue[GatewayMessage | None]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()

    @property
    def is_connected(self) -> bool:
        """Report whether the bridge link is currently live.

        Returns:
            ``True`` between a successful HELLO handshake and the next
            disconnect.  Callers use this to decide whether to buffer a
            message or report failure, since :meth:`submit` and
            :meth:`submit_stream` both fail fast when it is ``False``.
        """
        return self._connected

    def on(self, event: str, callback: Any) -> None:
        """Register a callback for a client lifecycle event.

        Supported events are emitter-style strings such as
        ``"connected"`` and ``"disconnected"``, emitted by
        :meth:`connect` and :meth:`_read_loop`.  Multiple callbacks per
        event are allowed and invoked in registration order.

        Args:
            event: The lifecycle event name to subscribe to.
            callback: A sync callable invoked with no arguments when the
                event fires.

        Raises:
            Nothing.
        """
        self._listeners.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Any) -> None:
        """Remove a previously registered callback for an event.

        Args:
            event: The lifecycle event name.
            callback: The callback to detach.  Removing a callback that
                was never registered is a no-op, never an error, so
                teardown code can call :meth:`off` unconditionally.

        Raises:
            Nothing.
        """
        self._listeners.setdefault(event, []).append(callback)
        self._listeners[event] = [cb for cb in self._listeners[event] if cb is not callback]

    async def connect(self) -> None:
        """Open the WebSocket, run the handshake, and keep the link alive.

        The method returns as soon as one connection succeeds: the HELLO
        frame is sent, the server's HELLO reply is awaited, and the
        heartbeat and read background tasks are started.  After a
        successful return the link keeps itself alive in the background,
        and if the socket later drops the read loop flips
        :attr:`is_connected` off and any *new* call to :meth:`connect`
        re-establishes it.

        On failure the method loops forever -- until
        :attr:`_running` is cleared by :meth:`disconnect` or the
        surrounding task is cancelled -- retrying with exponential
        backoff: ``RECONNECT_BASE * 2**(attempt - 1)``, capped at
        :data:`RECONNECT_MAX`.  Attempt numbering restarts from zero on
        every successful handshake, so a client that reconnects cleanly
        pays no accumulated delay on the next outage.

        Args:
            None.

        Returns:
            ``None`` once the connection is established.

        Raises:
            asyncio.CancelledError: If the surrounding task is cancelled
                during a backoff sleep or a handshake attempt.

        Raises:
            Nothing else; a bad handshake response raises
            ``ConnectionError`` internally and is retried.
        """
        import websockets
        self._running = True
        attempt = 0
        while self._running:
            try:
                self._ws = await websockets.connect(
                    self._url,
                    max_size=10 * 1024 * 1024,
                    ping_interval=HEARTBEAT_INTERVAL,
                    ping_timeout=10.0,
                )
                hello = GatewayMessage.hello(self._name)
                hello.data["capabilities"] = self._capabilities
                await self._send(hello)

                response_raw = await self._ws.recv()
                response = GatewayMessage.from_dict(json.loads(response_raw))
                if response.op == GatewayOp.HELLO:
                    self._connected = True
                    attempt = 0
                    logger.info("%s connected to WS bridge at %s", self._name, self._url)
                    self._emit("connected")
                    _t1 = asyncio.ensure_future(self._heartbeat_loop())
                    _t2 = asyncio.ensure_future(self._read_loop())
                    self._background_tasks.add(_t1)
                    self._background_tasks.add(_t2)
                    return
                else:
                    raise ConnectionError("Unexpected handshake response")

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    attempt += 1
                    delay = min(RECONNECT_BASE * (2 ** (attempt - 1)), RECONNECT_MAX)
                    logger.warning("%s WS bridge reconnect in %.1fs (attempt %d): %s", self._name, delay, attempt, e)
                    self._connected = False
                    self._emit("disconnected")
                    await asyncio.sleep(delay)

    async def disconnect(self) -> None:
        """Close the connection and wake every waiting reader.

        Clears the running and connected flags, closes the socket
        (ignoring errors since it may already be gone), and pushes
        ``None`` sentinels into the shared message queue and every
        pending request queue.  The sentinels are what unblock
        :meth:`submit` / :meth:`submit_stream` callers that are waiting
        on ``asyncio.wait_for``; without them they would hang until
        their own timeout expired.

        Args:
            None.

        Returns:
            ``None``.

        Raises:
            Nothing; socket close failures are suppressed so teardown is
            always safe to call, even twice.
        """
        self._running = False
        self._connected = False
        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        await self._msg_queue.put(None)
        for queue in self._pending.values():
            await queue.put(None)
        logger.info("%s disconnected from WS bridge", self._name)

    async def submit(self, prompt: str, session_id: str | None = None, system_prompt: str | None = None) -> str:
        """Send a one-shot prompt and return the entire reply as text.

        A fresh ``request_id`` is generated and registered in
        :attr:`_pending` before the SUBMIT frame is written, so a reply
        that arrives before the send returns cannot be lost.  The
        method then drains that queue, appending every ``TEXT_DELTA``
        payload until a terminal ``FINISH`` / ``ERROR`` frame arrives,
        the connection drops, or the wait is abandoned.

        Args:
            prompt: The user prompt to submit.
            session_id: Optional session identifier for conversation
                continuity; omit to let the server pick the
                adapter-default session.
            system_prompt: Optional system prompt overriding the
                server's default.

        Returns:
            The concatenated reply text, or ``""`` when the client is
            not connected or the turn ended without producing text.

        Note:
            Empty-string returns mean different things -- "not
            connected", "empty reply", or "server timed out" -- so a
            caller that must distinguish them should use
            :meth:`submit_stream`, which always yields a terminal
            ``Finish`` carrying the reason.

        Raises:
            asyncio.CancelledError: Propagated after removing this
                request's queue from :attr:`_pending`.
        """
        if not self._connected:
            return ""
        request_id = uuid.uuid4().hex
        response_queue: asyncio.Queue[GatewayMessage | None] = asyncio.Queue()
        self._pending[request_id] = response_queue
        msg = GatewayMessage.submit(prompt, session_id, system_prompt, request_id=request_id)
        await self._send(msg)
        parts: list[str] = []
        timeout_count = 0
        max_timeouts = 10
        while self._connected:
            try:
                response = await asyncio.wait_for(response_queue.get(), timeout=30.0)
                if response is None:
                    break
                if response.op == GatewayOp.TEXT_DELTA:
                    parts.append(response.data.get("text", ""))
                elif response.op == GatewayOp.FINISH:
                    break
                elif response.op == GatewayOp.ERROR:
                    break
            except TimeoutError:
                timeout_count += 1
                if timeout_count >= max_timeouts:
                    break
                continue
            except asyncio.CancelledError:
                self._pending.pop(request_id, None)
                raise
            except Exception:
                break
        self._pending.pop(request_id, None)
        return "".join(parts)

    async def submit_stream(
        self,
        prompt: str,
        session_id: str | None = None,
        system_prompt: str | None = None,
        *,
        source: dict[str, Any] | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Submit a prompt and stream back agent events.

        Same correlation scheme as :meth:`submit`, but instead of
        collecting text the generator yields one
        :class:`~encre.utils.types.AgentEvent` per server message:
        ``TEXT_DELTA`` becomes a ``TextDelta``, ``TOOL_RESULT`` becomes
        a ``ToolResult``, and either ``FINISH`` or ``ERROR`` becomes a
        terminal ``Finish`` with the appropriate reason and detail.  A
        ``Finish`` is always yielded exactly once, whether the turn
        completed or the transport failed, so callers can rely on a
        single terminal event per request.

        Args:
            prompt: The user prompt to submit.
            session_id: Optional session identifier for conversation
                continuity.
            system_prompt: Optional system prompt overriding the
                server's default.
            source: Optional origin metadata describing the message
                origin, forwarded to the server so it can resolve a
                session instead of using the adapter default.

        Yields:
            :class:`~encre.utils.types.AgentEvent` objects as they
            arrive, terminated by exactly one ``Finish``.

        Returns:
            An async generator (this is an async generator function, so
            iteration is required before any network activity happens).

        Raises:
            asyncio.CancelledError: Propagated after removing this
                request's queue from :attr:`_pending`.
        """
        if not self._connected:
            yield Finish(reason="error", error="WS bridge not connected")
            return
        request_id = uuid.uuid4().hex
        response_queue: asyncio.Queue[GatewayMessage | None] = asyncio.Queue()
        self._pending[request_id] = response_queue
        msg = GatewayMessage.submit_stream(prompt, session_id, system_prompt, source=source, request_id=request_id)
        await self._send(msg)
        timeout_count = 0
        max_timeouts = 10
        while self._connected:
            try:
                response = await asyncio.wait_for(response_queue.get(), timeout=30.0)
                if response is None:
                    self._pending.pop(request_id, None)
                    yield Finish(reason="error", error="WS bridge connection closed")
                    return
                if response.op == GatewayOp.TEXT_DELTA:
                    yield TextDelta(text=response.data.get("text", ""))
                elif response.op == GatewayOp.TOOL_RESULT:
                    d = response.data
                    yield ToolResult(id=d.get("id", ""), content=d.get("content", ""), is_error=d.get("is_error", False))
                elif response.op == GatewayOp.FINISH:
                    self._pending.pop(request_id, None)
                    yield Finish(
                        reason=response.data.get("reason", "done"),
                        error=response.data.get("error", ""),
                        usage=response.data.get("usage"),
                        context_usage=response.data.get("context_usage"),
                    )
                    return
                elif response.op == GatewayOp.ERROR:
                    self._pending.pop(request_id, None)
                    yield Finish(reason="error", error=response.data.get("message", ""))
                    return
            except TimeoutError:
                timeout_count += 1
                if timeout_count >= max_timeouts:
                    self._pending.pop(request_id, None)
                    yield Finish(reason="error", error="Server did not respond")
                    return
                continue
            except asyncio.CancelledError:
                self._pending.pop(request_id, None)
                raise
            except Exception:
                self._pending.pop(request_id, None)
                yield Finish(reason="error", error="WS bridge connection lost")
                return
        self._pending.pop(request_id, None)

    async def _send(self, msg: GatewayMessage) -> None:
        """Serialize and write one message over the socket.

        Assigns the next :attr:`_seq` value for log correlation, then
        writes the JSON form.  Failures are logged and swallowed rather
        than raised, so a transient write error does not tear down the
        caller; the read loop is responsible for noticing a dead socket
        and flipping :attr:`_connected` off.

        Args:
            msg: The message to transmit.  Its ``seq`` field is
                overwritten in place.

        Returns:
            ``None``.

        Raises:
            Nothing; a ``None`` socket (pre-connect) is silently a
            no-op so handshake frames can be sent defensively.
        """
        self._seq += 1
        msg.seq = self._seq
        if not self._ws:
            return
        try:
            await self._ws.send(json.dumps(msg.to_dict()))
        except Exception as e:
            logger.warning("[ws-client] %s send error: %s", self._name, e)

    async def _heartbeat_loop(self) -> None:
        """Periodically emit application-level heartbeat pings.

        Runs as a background task owned by :attr:`_background_tasks`.
        It sleeps :data:`HEARTBEAT_INTERVAL` seconds between pings, so
        it costs one frame every 15 s per connection.  Any exception
        breaks the loop and lets :meth:`_read_loop`'s cleanup take over
        rather than retrying, because a send failure here almost always
        means the read side will also fail shortly.

        Args:
            None.

        Returns:
            ``None``.

        Raises:
            Nothing.
        """
        while self._running and self._connected:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await self._send(GatewayMessage.heartbeat())
            except Exception:
                break

    async def _read_loop(self) -> None:
        """Continuously read server frames and route them to a queue.

        For each inbound frame the loop parses JSON and resolves the
        opcode, answers server heartbeats with an ack, then dispatches:
        a frame whose payload carries a ``request_id`` matching an entry
        in :attr:`_pending` goes to that request's queue; anything else
        goes to the shared :attr:`_msg_queue`.  Frames that fail to
        parse are dropped silently so a malformed message from a foreign
        peer cannot wedge the connection.

        Args:
            None.

        Returns:
            ``None``.

        Raises:
            Nothing.

        Invariant:
            The ``finally`` clause runs on *every* exit -- clean socket
            close, parse-independent exception, or cancellation -- and is
            what flips ``_connected`` off, fires ``"disconnected"``, and
            pushes ``None`` sentinels into every queue.  Removing the
            sentinels would leave blocked :meth:`submit` callers hanging
            for their full 30 s timeout each.
        """
        try:
            async for raw in self._ws:
                try:
                    msg = GatewayMessage.from_dict(json.loads(raw))
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                if msg.op == GatewayOp.HEARTBEAT:
                    await self._send(GatewayMessage.heartbeat_ack())
                request_id = str(msg.data.get("request_id", ""))
                queue = self._pending.get(request_id) if request_id else None
                if queue is not None:
                    await queue.put(msg)
                else:
                    await self._msg_queue.put(msg)
        except Exception:
            pass
        finally:
            self._connected = False
            self._emit("disconnected")
            await self._msg_queue.put(None)
            for queue in self._pending.values():
                await queue.put(None)

    def _emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Fire every listener registered for one lifecycle event.

        Each callback is invoked best-effort: exceptions raised by a
        listener are swallowed so one bad subscriber cannot break the
        emit loop or, worse, the read loop that calls
        :meth:`_emit("disconnected")`.

        Args:
            event: The lifecycle event name.
            *args: Positional arguments forwarded to each listener.
            **kwargs: Keyword arguments forwarded to each listener.

        Returns:
            ``None``.

        Raises:
            Nothing.
        """
        for cb in self._listeners.get(event, []):
            with contextlib.suppress(Exception):
                cb(*args, **kwargs)
