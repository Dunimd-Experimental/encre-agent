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

"""Agent-side WebSocket bridge for remote platform adapters.

This module implements :class:`WsBridgeServer`, a localhost WebSocket
server that accepts connections from remote or plugin adapters and
routes their traffic into
:class:`encre.gateway.run.GatewayRunner`.  Each accepted connection is
wrapped as a :class:`~encre.gateway.ws_bridge.remote_adapter.RemotePlatformAdapter`
so the runner drives it exactly like an in-process adapter.

It is used ONLY for adapters that cannot run in-process.  Core adapters
(:mod:`encre.gateway.platforms`) run in the same process and interact
directly with the runner, bypassing this server entirely.  Note that
this server is unrelated to
:class:`encre.transport.ws.EncreWSHandler`, which serves the desktop
renderer over a different protocol.

The server is the bridge's server half: it owns the listening socket,
performs the HELLO handshake, tracks live connections, answers
heartbeats, enforces the connection cap, and routes inbound
``SUBMIT`` / ``SUBMIT_STREAM`` / ``CANCEL`` / ``ADAPTER_UPDATE`` /
``SHUTDOWN`` frames into the runner while streaming the runner's reply
events back out as ``TEXT_DELTA`` / ``TOOL_RESULT`` / ``FINISH``
frames.

Architecture::

    RemoteAdapter  <== WS ==>  WsBridgeServer  <== in-process ==>  GatewayRunner -> EventRouter
    RemoteAdapter  <== WS ==>  WsBridgeServer  <== in-process ==>  GatewayRunner <- EventRouter

Public API: :class:`WsBridgeServer` with :meth:`WsBridgeServer.start`,
:meth:`WsBridgeServer.stop`, and :meth:`WsBridgeServer.get_status`.
"""

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from encre.gateway.ws_bridge.protocol import GatewayMessage, GatewayOp
from encre.gateway.session import SessionSource
from encre.utils.types import Finish, TextDelta, ToolResult

logger = logging.getLogger("encre.gateway.ws_bridge.server")

# Seconds a connection may be missing before it is considered dead (currently
# informational; heartbeats use the websockets library's own ping machinery).
RECONNECT_TIMEOUT = 30.0
# Seconds between server->client heartbeat pings.
HEARTBEAT_INTERVAL = 15.0
# Maximum number of in-flight request frames queued before backpressure.
MAX_PENDING = 256


class _AdapterConnection:
    """Mutable record describing one live remote adapter connection.

    Holds the socket plus the bookkeeping the server needs to reason
    about an adapter without holding it: identity, capabilities, the
    connection timestamp, and heartbeat liveness.  One instance is
    created per accepted socket in
    :meth:`WsBridgeServer._handle_connection`, stored in the server's
    adapter registry under the adapter's name, and dropped when the
    socket closes.

    Everything here is intentionally a plain mutable attribute rather
    than a dataclass field with validation, because ADAPTER_UPDATE
    frames rewrite ``status`` and ``capabilities`` in place over the
    connection's lifetime.
    """

    def __init__(self, name: str, ws: Any) -> None:
        """Record a freshly accepted socket.

        Args:
            name: A provisional name (``remote-N``) used until the
                adapter declares its own name in the HELLO frame.
            ws: The connected WebSocket object for this adapter.

        Raises:
            Nothing.
        """
        self.name = name
        self.ws = ws
        self.connected_at = time.time()
        self.last_heartbeat = time.time()
        self.status: str = "connected"
        self.capabilities: list[str] = []

    @property
    def uptime(self) -> float:
        """Return how many seconds this connection has been alive.

        Computed on demand rather than cached so status snapshots are
        always current without a background updater task.

        Returns:
            Seconds since :attr:`connected_at`, as a float.
        """
        return time.time() - self.connected_at


class WsBridgeServer:
    """WebSocket bridge accepting remote adapter connections.

    Accepts WebSocket connections from remote adapters and routes
    messages to and from the GatewayRunner.

    Architecture::

        RemoteAdapter  <== WS ==>  WsBridgeServer  <== in-process ==>  GatewayRunner -> EventRouter
        RemoteAdapter  <== WS ==>  WsBridgeServer  <== in-process ==>  GatewayRunner <- EventRouter

    Each remote adapter connection is tracked in :attr:`_adapters`.  The
    bridge performs the HELLO handshake, answers HEARTBEAT pings,
    enforces the maximum connection cap, and dispatches inbound
    ``SUBMIT`` / ``SUBMIT_STREAM`` / ``CANCEL`` / ``ADAPTER_UPDATE`` /
    ``SHUTDOWN`` frames into the parent
    :class:`encre.gateway.run.GatewayRunner`.  Replies from the runner's
    event router are streamed back as bridge wire messages.

    Lifecycle: construct with the runner (and optionally a non-default
    host/port/cap), :meth:`start` to bind the listening socket,
    :meth:`stop` to close every adapter socket and the listener.  The
    runner owns this object; :mod:`encre.gateway.run` starts and stops
    it with the gateway itself.

    Invariants:

    * :attr:`_adapters` maps adapter name to
      :class:`_AdapterConnection` and is the single source of truth for
      "which remote adapters are live".  Every exit path of the per-
      connection coroutine removes its entry, so the registry never
      holds a dead socket.
    * :attr:`_seq` is monotonic across the server's whole lifetime and
      shared by all connections, because it exists only for cross-
      connection log correlation.
    * Background submit tasks are added to :attr:`_background_tasks` so
      they cannot be garbage-collected mid-flight; the set is a plain
      reference holder, not a cancellation mechanism.

    Attributes:
        _runner: The :class:`encre.gateway.run.GatewayRunner` that
            executes submitted prompts.  Accessed through its private
            ``_router``, ``_instances`` attributes, so this server is
            coupled to the runner's internals by design.
        _host, _port: Listen address of the bridge endpoint.
        _max_connections: Hard cap on simultaneous adapter sockets.
        _server: The bound :class:`asyncio.AbstractServer`, ``None``
            before :meth:`start` or after :meth:`stop`.
        _running: Whether the server is serving.
        _adapters: Registry of live remote adapter connections.
        _seq: Monotonic send counter for log correlation.
        _background_tasks: Owned submit background tasks.
    """

    def __init__(
        self,
        runner: Any,
        host: str = "127.0.0.1",
        port: int = 18792,
        max_connections: int = 32,
    ) -> None:
        """Bind the server to a runner without listening yet.

        Args:
            runner: The gateway runner that executes submitted prompts
                and owns the event router plus the in-process adapter
                instances.
            host: Interface to listen on.  ``127.0.0.1`` keeps the
                bridge unreachable from the network, which is the
                security default: adapters are trusted local processes.
            port: TCP port for the bridge endpoint.
            max_connections: Maximum number of simultaneous remote
                adapter sockets; further connections are refused with
                close code 4001.

        Raises:
            Nothing; binding happens in :meth:`start`.
        """
        self._runner = runner
        self._host = host
        self._port = port
        self._max_connections = max_connections
        self._server: asyncio.AbstractServer | None = None
        self._running = False
        self._adapters: dict[str, _AdapterConnection] = {}
        self._seq = 0
        self._background_tasks: set[asyncio.Task[Any]] = set()

    @property
    def adapter_count(self) -> int:
        """Return the number of currently connected remote adapters.

        Returns:
            The size of the connection registry.  Used both for the
            capacity check in :meth:`_handle_connection` and for status
            snapshots.
        """
        return len(self._adapters)

    @property
    def adapters(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot of each connected adapter's public metadata.

        Builds a fresh dict per call so a caller that holds the result
        cannot mutate the server's internal records.

        Returns:
            A mapping of adapter name to a dict with ``name``,
            ``status``, ``uptime`` and ``capabilities``.
        """
        return {
            name: {
                "name": conn.name,
                "status": conn.status,
                "uptime": conn.uptime,
                "capabilities": conn.capabilities,
            }
            for name, conn in self._adapters.items()
        }

    async def start(self) -> None:
        """Begin listening for WebSocket connections.

        Imports :mod:`websockets` lazily so that deployments which never
        enable the bridge do not pay for the dependency import, then
        serves :meth:`_handle_connection` for every inbound socket.

        Args:
            None.

        Returns:
            ``None`` once the socket is bound and accepting.

        Raises:
            OSError: If the configured port is already in use; the
                caller (the gateway runner) is expected to surface this
                as a startup failure.

        Note:
            ``ping_interval`` uses the application
            :data:`HEARTBEAT_INTERVAL`, which doubles as the WebSocket
            protocol keepalive period; the bridge's own HEARTBEAT op
            then runs on the same cadence through the adapter's
            client-side loop.
        """
        self._running = True
        import websockets
        self._server = await websockets.serve(
            self._handle_connection,
            host=self._host,
            port=self._port,
            max_size=10 * 1024 * 1024,
            ping_interval=HEARTBEAT_INTERVAL,
            ping_timeout=10.0,
        )
        logger.info("WS bridge listening on ws://%s:%s/gateway", self._host, self._port)

    async def stop(self) -> None:
        """Stop listening and close every open adapter connection.

        Closes all live sockets, clears the adapter registry, and stops
        the underlying server.  Safe to call even if :meth:`start` was
        never called or was already stopped.

        Args:
            None.

        Returns:
            ``None``.

        Raises:
            Nothing; individual socket close errors are suppressed so
            teardown never fails halfway and leaves the registry
            populated.
        """
        self._running = False
        for conn in list(self._adapters.values()):
            with contextlib.suppress(Exception):
                await conn.ws.close()
        self._adapters.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        logger.info("WS bridge stopped")

    def get_status(self) -> dict[str, Any]:
        """Return a status snapshot for health and diagnostic endpoints.

        Consumed by the gateway's admin surface and re-serialized into
        the ``gateway_status`` frame that
        :class:`encre.transport.ws.EncreWSHandler` pushes to the desktop
        renderer, which is how the UI shows whether remote adapters are
        attached.

        Returns:
            A dict with ``running``, ``host``, ``port``,
            ``adapter_count`` and the per-adapter ``adapters`` snapshot.
        """
        return {
            "running": self._running,
            "host": self._host,
            "port": self._port,
            "adapter_count": self.adapter_count,
            "adapters": self.adapters,
        }

    async def _handle_connection(self, ws: Any, _path: str | None = None) -> None:
        """Per-connection coroutine: handshake, then dispatch loop.

        Runs one instance per accepted socket.  It rejects the
        connection (close code 4001) when the capacity cap is reached,
        then reads frames in a loop, parsing each JSON message into a
        :class:`GatewayMessage` and dispatching on its ``op``:

        * HELLO performs the handshake and registers the adapter under
          the name it declared (replacing the provisional ``remote-N``).
        * HEARTBEAT refreshes the liveness timestamp and is answered.
        * SUBMIT / SUBMIT_STREAM spawn background tasks so one slow turn
          cannot stall the connection's frame loop or block other
          requests on the same socket.
        * CANCEL forwards the session id to the runner's event router.
        * ADAPTER_UPDATE rewrites this connection's status and
          capabilities in place.
        * SHUTDOWN is the remote's clean-teardown request and breaks the
          loop.

        Args:
            ws: The WebSocket protocol object for this connection.
            _path: The request path, unused; present for the websockets
                callback signature.

        Returns:
            ``None``.

        Raises:
            Nothing; read and parse errors leave the loop, and the
            ``finally`` clause always unregisters the adapter so the
            registry cannot retain a dead socket.

        Invariant:
            The adapter is registered only after a HELLO frame arrives.
            A socket that connects and immediately dies therefore never
            appears in the registry, which keeps
            :meth:`_sync_adapter_list` free of phantom adapters.
        """
        if len(self._adapters) >= self._max_connections:
            await ws.close(4001, "Server at capacity")
            return

        adapter_name = f"remote-{len(self._adapters) + 1}"
        conn = _AdapterConnection(adapter_name, ws)

        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send(ws, GatewayMessage.error("Invalid JSON"))
                    continue

                msg = GatewayMessage.from_dict(data)

                if msg.op == GatewayOp.HELLO:
                    adapter_name = msg.data.get("name", adapter_name)
                    conn.name = adapter_name
                    conn.capabilities = msg.data.get("capabilities", [])
                    self._adapters[adapter_name] = conn
                    logger.info("Remote adapter '%s' connected (%d total)", adapter_name, self.adapter_count)
                    await self._send(ws, GatewayMessage.hello(adapter_name))
                    self._sync_adapter_list()

                elif msg.op == GatewayOp.HEARTBEAT:
                    conn.last_heartbeat = time.time()
                    await self._send(ws, GatewayMessage.heartbeat_ack())

                elif msg.op == GatewayOp.SUBMIT:
                    _t = asyncio.ensure_future(self._handle_submit(ws, conn, msg))
                    self._background_tasks.add(_t)

                elif msg.op == GatewayOp.SUBMIT_STREAM:
                    _t2 = asyncio.ensure_future(self._handle_submit_stream(ws, conn, msg))
                    self._background_tasks.add(_t2)

                elif msg.op == GatewayOp.CANCEL:
                    session_id = msg.data.get("session_id", "")
                    router = getattr(self._runner, "_router", None)
                    if router:
                        router.cancel_session(session_id)

                elif msg.op == GatewayOp.ADAPTER_UPDATE:
                    conn.status = msg.data.get("status", conn.status)
                    conn.capabilities = msg.data.get("capabilities", conn.capabilities)

                elif msg.op == GatewayOp.SHUTDOWN:
                    break

        except Exception:
            pass
        finally:
            if adapter_name in self._adapters:
                del self._adapters[adapter_name]
            logger.info("Remote adapter '%s' disconnected (%d remaining)", adapter_name, self.adapter_count)
            self._sync_adapter_list()

    async def _handle_submit(self, ws: Any, conn: _AdapterConnection, msg: GatewayMessage) -> None:
        """Handle a one-shot SUBMIT frame: run the turn, return the reply.

        Validates the prompt, resolves the target session (from a
        ``source`` dict when present, otherwise from an explicit
        ``session_id`` or the adapter's default session), runs the
        runner's synchronous submit path (collecting the whole reply),
        then writes one ``TEXT_DELTA`` followed by a terminal
        ``FINISH``.  Failures are reported as ``ERROR`` frames rather
        than raising, so the remote adapter always receives a terminal
        frame for the ``request_id`` it sent.

        Args:
            ws: The connection socket to write replies to.
            conn: The originating :class:`_AdapterConnection`, used as
                the channel name when no platform source is supplied.
            msg: The parsed SUBMIT message.

        Returns:
            ``None``.

        Raises:
            Nothing; every failure mode is converted into an ``ERROR``
            frame carrying the original ``request_id``.
        """
        prompt = msg.data.get("prompt", "")
        request_id = str(msg.data.get("request_id", ""))
        if not prompt.strip():
            logger.warning("[ws-bridge] %s submit empty prompt", conn.name)
            await self._send(ws, GatewayMessage.error("Empty prompt", request_id=request_id))
            return
        session_id = msg.data.get("session_id")
        system_prompt = msg.data.get("system_prompt")
        source_dict = msg.data.get("source")
        logger.info("[ws-bridge] %s submit prompt=%.60s session=%s", conn.name, prompt, session_id or "(new)")
        router = getattr(self._runner, "_router", None)
        if router is None:
            logger.warning("[ws-bridge] %s submit failed -- runner not ready", conn.name)
            await self._send(ws, GatewayMessage.error("Engine not ready", request_id=request_id))
            return
        try:
            async with router.iclaw_context():
                if source_dict is not None:
                    source = SessionSource.from_dict(source_dict)
                    session_id = await self._runner.resolve_session(conn.name, source)
                    channel_name = source.platform
                else:
                    if not session_id:
                        # A remote adapter that omits a structured source cannot
                        # be routed per-conversation; fall back to a per-adapter
                        # source so the session is still persisted rather than
                        # held only in process memory.
                        source = SessionSource(platform=conn.name, chat_id=conn.name, chat_type="dm")
                        session_id = await self._runner.resolve_session(conn.name, source)
                    channel_name = conn.name
                result = await router.submit(
                    channel_name, prompt,
                    session_id=session_id,
                    system_prompt=system_prompt,
                )
                if isinstance(result, str) and result:
                    await self._send(ws, GatewayMessage.text_delta(result, session_id=session_id or "", request_id=request_id))
                await self._send(ws, GatewayMessage.finish(request_id=request_id))
        except Exception as e:
            logger.error("[ws-bridge] %s submit error: %s %s", conn.name, type(e).__name__, e)
            await self._send(ws, GatewayMessage.error(str(e), request_id=request_id))

    async def _handle_submit_stream(self, ws: Any, conn: _AdapterConnection, msg: GatewayMessage) -> None:
        """Handle a SUBMIT_STREAM frame: run the turn and stream events back.

        Resolves the session exactly like :meth:`_handle_submit`, but
        instead of collecting a single reply it iterates the runner's
        ``submit_stream`` async generator and forwards every event as it
        arrives: ``TextDelta`` becomes a ``TEXT_DELTA`` frame,
        ``ToolResult`` becomes a ``TOOL_RESULT`` frame, and ``Finish``
        becomes the terminal ``FINISH`` frame carrying reason, usage and
        error detail.  Cumulative text length is tracked for the finish
        log line so a stalled stream is diagnosable without decoding
        every frame.

        Args:
            ws: The connection socket to write replies to.
            conn: The originating :class:`_AdapterConnection`.
            msg: The parsed SUBMIT_STREAM message.

        Returns:
            ``None``.

        Raises:
            Nothing; every failure mode is converted into an ``ERROR``
            frame carrying the original ``request_id``.

        Note:
            Usage is copied with ``dict(event.usage)`` only when the
            value exposes ``items``; anything else (a dataclass without
            dict mapping) becomes ``{}`` so the frame always carries a
            JSON-serializable object.
        """
        prompt = msg.data.get("prompt", "")
        request_id = str(msg.data.get("request_id", ""))
        if not prompt.strip():
            logger.warning("[ws-bridge] %s submit_stream empty prompt", conn.name)
            await self._send(ws, GatewayMessage.error("Empty prompt", request_id=request_id))
            return
        session_id = msg.data.get("session_id")
        system_prompt = msg.data.get("system_prompt")
        source_dict = msg.data.get("source")
        logger.info("[ws-bridge] %s submit_stream prompt=%.60s session=%s", conn.name, prompt, session_id or "(new)")
        router = getattr(self._runner, "_router", None)
        if router is None:
            logger.warning("[ws-bridge] %s submit_stream failed -- runner not ready", conn.name)
            await self._send(ws, GatewayMessage.error("Engine not ready", request_id=request_id))
            return
        text_len = 0
        try:
            async with router.iclaw_context():
                if source_dict is not None:
                    source = SessionSource.from_dict(source_dict)
                    session_id = await self._runner.resolve_session(conn.name, source)
                    channel_name = source.platform
                else:
                    if not session_id:
                        # See _handle_submit: a source-less remote adapter gets a
                        # persisted per-adapter fallback instead of an in-memory
                        # shared session.
                        source = SessionSource(platform=conn.name, chat_id=conn.name, chat_type="dm")
                        session_id = await self._runner.resolve_session(conn.name, source)
                    channel_name = conn.name
                async for event in router.submit_stream(
                    channel_name, prompt,
                    session_id=session_id,
                    system_prompt=system_prompt,
                ):
                    if isinstance(event, TextDelta) and event.text:
                        text_len += len(event.text)
                        await self._send(ws, GatewayMessage.text_delta(event.text, session_id=session_id or "", request_id=request_id))
                    elif isinstance(event, ToolResult):
                        await self._send(ws, GatewayMessage.tool_result(
                            event.id or "", event.content or "", event.is_error, request_id=request_id
                        ))
                    elif isinstance(event, Finish):
                        usage = None
                        if event.usage:
                            usage = dict(event.usage) if hasattr(event.usage, "items") else {}
                        context_usage = None
                        if event.context_usage:
                            context_usage = dict(event.context_usage) if hasattr(event.context_usage, "items") else {}
                        if event.error:
                            logger.warning("[ws-bridge] %s finish with error: %s (text_len=%d)", conn.name, event.error, text_len)
                        else:
                            logger.info("[ws-bridge] %s finish reason=%s text_len=%d", conn.name, event.reason, text_len)
                        await self._send(ws, GatewayMessage.finish(event.reason, usage, event.error or "", request_id=request_id, context_usage=context_usage))
        except Exception as e:
            logger.error("[ws-bridge] %s submit_stream error: %s %s", conn.name, type(e).__name__, e)
            await self._send(ws, GatewayMessage.error(str(e), request_id=request_id))

    def _sync_adapter_list(self) -> None:
        """Publish the merged adapter list to the runner's event router.

        Called after every registration and deregistration so the runner
        always sees the full set of available adapters: in-process
        instances plus currently connected remote adapters.  Without
        this sync the runner would route submissions to names it no
        longer has, or fail to route to a freshly connected remote
        adapter.

        Args:
            None.

        Returns:
            ``None``.

        Raises:
            Nothing; a runner without an event router or without a
            ``set_connected_adapters`` hook is silently ignored, which
            is how older runner shapes stay compatible.
        """
        router = getattr(self._runner, "_router", None)
        if router and hasattr(router, "set_connected_adapters"):
            # Merge in-process adapters + remote adapters
            in_process = list(getattr(self._runner, "_instances", {}).keys())
            remote = list(self._adapters.keys())
            router.set_connected_adapters(in_process + remote)

    async def _send(self, ws: Any, msg: GatewayMessage) -> None:
        """Serialize and write one message to a connection socket.

        Assigns the next :attr:`_seq` value then writes the JSON form.
        Send failures are logged and swallowed rather than raised, so a
        dead socket cannot crash the dispatch loop; the socket's own
        read loop exits on the next iteration and cleanup removes the
        adapter from the registry.

        Args:
            ws: The target WebSocket protocol object.
            msg: The message to transmit.  Its ``seq`` field is
                overwritten in place with the server-wide counter.

        Returns:
            ``None``.

        Raises:
            Nothing.
        """
        self._seq += 1
        msg.seq = self._seq
        try:
            await ws.send(json.dumps(msg.to_dict()))
        except Exception as e:
            logger.warning("[ws-bridge] send error: %s", e)
