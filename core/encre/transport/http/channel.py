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

"""Agent channel over raw HTTP: REST endpoints plus NDJSON streaming.

Implements :class:`HTTPChannel`, one of the concrete
:class:`encre.channels.base.Channel` surfaces.  It exists so that clients that
cannot keep a long-lived WebSocket open -- shell scripts, CI jobs, the
backend benchmark runners -- still have a first-class way into the same agent
runtime that the Electron renderer uses.  Nothing here talks to IM vendors;
those are :mod:`encre.gateway.platforms`.

The transport is hand-rolled on top of :func:`asyncio.start_server` rather
than ``aiohttp`` or ``http.server``.  The request path reads a single request
line, one header block and one body, then answers with ``Connection: close``,
so a TCP connection carries exactly one HTTP transaction.  There is no keep-
alive, no chunked encoding and no pipelining, which keeps the parser to a few
tens of lines and means every inbound client must open a fresh socket per
call.  A heavier framework buys nothing for an endpoint that only ever returns
short JSON or a delta stream.

Endpoints, all mirroring actions the desktop client issues over WebSocket in
the unified WebSocket channel:

    * ``GET /health`` -- liveness probe plus the live session count.
    * ``GET /sessions`` -- the on-disk session index from
      :class:`encre.server.session_manager.SessionManager`.
    * ``POST /chat`` -- the work endpoint; JSON reply, or NDJSON deltas when
      the request sets ``"stream": true``.
    * ``DELETE /sessions/:id`` -- cancel then delete a session from disk.
    * ``POST /sessions/:id/cancel`` -- abort an in-flight run.

Inbound direction: HTTP bytes -> a parsed ``prompt`` ->
:meth:`encre.protocol.session_router.EventRouter.submit` or
:meth:`~encre.protocol.session_router.EventRouter.submit_stream`, tagged with
``channel.name == "http_api"`` so telemetry and session badges attribute the
run to this transport.  Outbound direction:
:class:`~encre.utils.types.AgentEvent` instances are reduced to three wire
shapes (``delta``, ``tool_result``, ``finish``) rather than the full event
model -- NDJSON consumers want prose and tool outcomes, not the fine-grained
tool-call framing that the desktop renderer renders incrementally.

Public API:
    HTTPChannel -- the channel class; everything else is transport plumbing.
"""

import asyncio
import json
import logging
from typing import Any

from encre.channels.base import Channel
from encre.protocol.session_router import EventRouter
from encre.utils.types import Finish, TextDelta, ToolResult

logger = logging.getLogger("encre.channels.http")


class HTTPChannel(Channel):
    """HTTP channel exposing the agent as a REST API with session management.

    One instance is one listening socket serving many independent clients.
    Sessions are NOT owned by the connection: an HTTP client has no persistent
    identity, so a request names the session explicitly via ``session_id`` and
    the channel defers all session policy to the shared
    :class:`encre.protocol.session_router.EventRouter` handed in by
    :meth:`start`.
    socket; this class is the opposite extreme of that design.

    Endpoints:
      GET  /health          -- health check plus active session count
      GET  /sessions        -- list sessions from the on-disk index
      POST /chat            -- submit prompt, returns JSON or an NDJSON stream
      DELETE /sessions/:id  -- cancel then delete a session from disk
      POST /sessions/:id/cancel -- cancel a running session

    Invariants maintained:
      * ``_server`` is only bound to a socket after :meth:`start` returns, and
        :meth:`stop` closes it; a second :meth:`start` replaces the previous
        binding rather than leaking one.
      * Requests arriving before :meth:`start` installs a router are answered
        with HTTP 503 rather than raising, so a probe hitting a cold server
        gets a clean "not ready" instead of a 500.
      * The router is never cancelled by this channel -- it outlives any
        single client and serves other channels too.
      * Every response carries ``Connection: close`` and the connection is
        torn down in the ``finally`` of :meth:`_handle_client`, so no reader
        state can bleed between transactions.

    Async safety: entirely asyncio-based and non-reentrant with respect to a
    single connection (one transaction at a time), while different clients run
    concurrently as separate tasks created by the event loop.

    Attributes:
        name: ``"http_api"``.  Tagged onto every session this channel submits
            and recorded by telemetry as the originating channel, so the string
            is stable and set at class definition time.
        _host / _port: Listen address, bound at :meth:`start`.  Defaults to
            loopback on 18792, one above the WebSocket channel's 18791, so the
            two can run on the same host without colliding.
        _server: The bound :class:`asyncio.AbstractServer`, ``None`` before
            :meth:`start` and after :meth:`stop` completes.
        _router: The shared event router; ``None`` until :meth:`start` has been
            called, which is what the 503 guards above key off of.
    """

    name = "http_api"

    def __init__(self, host: str = "127.0.0.1", port: int = 18792) -> None:
        """Create a channel without binding a socket.

        Construction stores only the listen address; no listening socket, no
        accept loop and no router exist until :meth:`start` runs.  This keeps
        the class cheap to build during server composition, where
        :class:`encre.server.app.EncreServer` constructs every channel first
        and starts them in turn.

        Args:
            host: Interface to bind.  ``"127.0.0.1"`` by default because the
                agent's HTTP surface is a local control plane, not a public
                endpoint; binding it to a LAN address is the operator's
                deliberate choice.
            port: TCP port.  ``18792`` sits beside the WebSocket channel's
                ``18791``.
        """
        self._host = host
        self._port = port
        self._server: asyncio.AbstractServer | None = None
        self._router: EventRouter | None = None

    async def start(self, router: EventRouter) -> None:
        """Bind the listening socket and begin accepting HTTP connections.

        Unlike the WebSocket channel, which keeps its accept loop alive for the
        channel's whole lifetime, this method returns as soon as the socket is
        bound: the loop is the event loop's own, and every connection becomes
        an independent task running :meth:`_handle_client`.  :meth:`stop` is
        therefore required to release the port.

        Args:
            router: The shared :class:`encre.protocol.session_router.EventRouter`.
                Held as ``_router`` and passed to every request; also the
                source of the active-session count reported by ``/health``.
        """
        self._router = router
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._host,
            port=self._port,
        )
        logger.info(
            "HTTP API channel ready: http://%s:%s",
            self._host,
            self._port,
        )

    async def stop(self) -> None:
        """Stop accepting new connections and release the port.

        :meth:`asyncio.AbstractServer.close` only stops the listen backlog; it
        does not wait for in-flight handlers.  ``wait_closed()`` is what makes
        this awaitable usable as a shutdown barrier, so a server can shut down
        the channel and rely on the port being free immediately afterwards.
        In-flight transactions are not aborted here; they drain on their own,
        each closing its writer in the ``finally`` of :meth:`_handle_client`.

        Safe to call before :meth:`start` (nothing to close) and to call twice
        (the second call finds ``_server`` still bound to a closed object and
        re-closes it), which is the contract :class:`Channel` requires so
        teardown code can call it unconditionally.
        """
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Read and answer one HTTP transaction on a fresh TCP connection.

        Called once per accepted connection by the event loop, with no shared
        state between invocations beyond ``_router`` and ``_host``/``_port``.
        The method is the only place the raw wire format is interpreted, so
        everything after it works with an already-parsed request.

        A connection that closes before sending anything is treated as a silent
        probe (load balancer health checks, port scanners) and closed without a
        response rather than with an error status, since there is nothing to
        report on and replying would double the noise on a busy port.

        Args:
            reader: Bytes received from the client.
            writer: Bytes to send back; always closed, in every code path.

        Note:
            Errors are caught and answered with HTTP 500 plus the exception
            text rather than propagated, because raising out of this coroutine
            would only log an unhandled task exception and leave the client
            hanging on a connection the server has already torn down.
        """
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            request_str = request_line.decode("utf-8").strip()
            parts = request_str.split(" ")
            if len(parts) < 2:
                self._respond(writer, 400, {"error": "Bad request"})
                return
            method, path = parts[0], parts[1]

            # Header block: CRLF-terminated "Key: Value" lines terminated by a
            # blank line.  Keys are lower-cased so "Content-Length" and
            # "content-length" read identically below; splitting on the first
            # ":" only keeps values that legitimately contain colons (dates,
            # auth schemes) intact.
            headers: dict[str, str] = {}
            while True:
                header_line = await reader.readline()
                header_str = header_line.decode("utf-8").strip()
                if not header_str:
                    break
                if ":" in header_str:
                    key, value = header_str.split(":", 1)
                    headers[key.strip().lower()] = value.strip()

            # Body: read exactly the declared length and silently drop bodies
            # over 10 MiB by treating them as empty.  The cap stops a hostile or
            # misconfigured client from pinning one readexactly() task for a
            # large upload; legitimate prompts are tiny, and a silently-ignored
            # body surfaces downstream as an "empty prompt" 400 rather than a
            # memory spike.  A missing Content-Length means an empty body, which
            # matches every endpoint except POST /chat.
            content_length = int(headers.get("content-length", "0"))
            body = ""
            if content_length > 0 and content_length < 10 * 1024 * 1024:
                raw_body = await reader.readexactly(content_length)
                body = raw_body.decode("utf-8")

            await self._route(method, path, body, headers, writer)

        except Exception as e:
            logger.error("HTTP client error: %s", e)
            self._respond(writer, 500, {"error": str(e)})
        finally:
            writer.close()

    async def _route(
        self,
        method: str,
        path: str,
        body: str,
        headers: dict[str, str],
        writer: asyncio.StreamWriter,
    ) -> None:
        """Dispatch a parsed request to the matching action.

        Pure if/elif ladder over ``(method, path)`` -- no routing table, because
        there are five endpoints and the only dynamic part is the session id
        embedded in the path.  Order matters: the two session routes are keyed
        on a path prefix/suffix match, so the exact ``GET /health`` and
        ``POST /chat`` checks come before them.

        Implements ``GET /health``, ``GET /sessions``,
        ``DELETE /sessions/:id``, ``POST /sessions/:id/cancel`` and
        ``POST /chat`` (delegating to :meth:`_handle_chat`).

        Args:
            method: HTTP verb, as written by the client.
            path: Raw request target including the session id segment.
            body: Decoded request body, possibly empty.
            headers: Parsed header map, keyed in lower case.
            writer: Stream to write the answer to.

        Note:
            ``router`` is read into a local so that every branch below tests
            the same object it dispatches through, and so all branches fail
            closed to 503 when the router has not been installed yet.
        """
        router = self._router

        # Liveness probe.  No router guard here on purpose: even while the
        # channel is still warming up, an operator wants a 200 to know the
        # socket is alive, so the active count degrades to 0 instead of a 503.
        if method == "GET" and path == "/health":
            self._respond(writer, 200, {
                "status": "ok",
                "active_sessions": self._router.session_manager.active_count if self._router else 0,
            })
            return

        # Session index read, served from the file-backed store rather than
        # from memory so the HTTP client sees the same record set the desktop
        # renderer lists.  Returns the raw index rows unchanged.
        if method == "GET" and path == "/sessions":
            if router is None:
                self._respond(writer, 503, {"error": "Not ready"})
                return
            sessions = router.session_manager.query_index()
            self._respond(writer, 200, {"sessions": sessions})
            return

        # Delete a session.  The id is everything after the first "/sessions/"
        # (split with maxsplit=1, so ids containing "/" are not mangled).  The
        # cancel happens BEFORE the disk delete: an in-flight run would keep
        # writing its transcript back into a directory that has just been
        # removed, and would keep holding a concurrency slot after the record
        # is gone.  The router's cancel is best-effort and returns False for
        # already-idle ids, which is the expected case for a stale session.
        if method == "DELETE" and path.startswith("/sessions/"):
            if router is None:
                self._respond(writer, 503, {"error": "Not ready"})
                return
            sid = path.split("/sessions/", 1)[1]
            router.cancel_session(sid)
            ok = router.session_manager.delete_session_from_disk(sid)
            if ok:
                self._respond(writer, 200, {"deleted": sid})
            else:
                self._respond(writer, 404, {"error": "Session not found"})
            return

        # Cancel without deleting.  The route is matched on a suffix so it can
        # be reached without changing the DELETE handler above; the id is
        # recovered by splitting on "/sessions/" and then chopping the
        # trailing "/cancel" segment.  The router's return value is what decides
        # the answer here: it reports whether anything was actually running, so
        # an unknown or already-idle id answers 404 instead of a false success.
        if method == "POST" and path.endswith("/cancel") and "/sessions/" in path:
            if router is None:
                self._respond(writer, 503, {"error": "Not ready"})
                return
            sid = path.split("/sessions/", 1)[1].rsplit("/cancel", 1)[0]
            if router.cancel_session(sid):
                self._respond(writer, 200, {"cancelled": sid})
            else:
                self._respond(writer, 404, {"error": "Session not found or not running"})
            return

        # The work endpoint.  Exact path match, so any typos or extra path
        # segments fall through to the 404 below.
        if method == "POST" and path == "/chat":
            if router is None:
                self._respond(writer, 503, {"error": "Not ready"})
                return
            await self._handle_chat(body, headers, writer)
            return

        self._respond(writer, 404, {"error": "Not found"})

    async def _handle_chat(
        self,
        body: str,
        _headers: dict[str, str],
        writer: asyncio.StreamWriter,
    ) -> None:
        """Submit one prompt and answer either with a full reply or a stream.

        The request body is ``{"prompt": str, "session_id": str?,
        "system_prompt": str?, "stream": bool?}``.  There is no authentication
        here: the channel binds to loopback by default and sits inside the
        Encre Agent Desktop's local control plane, so it inherits whatever network
        exposure the operator gives the host.  Headers are accepted but unused
        (``_headers``) -- the parameter is kept so the signature stays stable if
        an auth token header is ever added without changing every call site.

        Two response shapes:
          * ``stream`` false (default): run to completion and answer with the
            concatenated assistant text.  Only text deltas survive the trip, so
            a run that did only tool work returns an empty string.
          * ``stream`` true: answer with ``application/x-ndjson`` immediately
            and emit one JSON object per line as events arrive, terminated by a
            ``finish`` line.  Streaming the status line first is what makes the
            client see deltas as they happen instead of one big reply at the
            end of the run.

        Args:
            body: Raw request body, empty for a bodyless call.
            _headers: Parsed headers, currently unused.
            writer: Stream to write the answer to.

        Note:
            NDJSON frames are flushed with one :meth:`asyncio.StreamWriter.drain`
            after the whole run rather than after every line.  Per-line drains
            would round-trip the event loop once per delta for no benefit, since
            the socket's send buffer already absorbs the bursts.  The trade-off
            is that a consumer which stops reading stalls the producer through
            OS-level backpressure -- which is the desired behaviour, since it is
            the only thing keeping the agent from outrunning a slow client.
        """
        router = self._router
        if router is None:
            self._respond(writer, 503, {"error": "Router not ready"})
            return

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._respond(writer, 400, {"error": "Invalid JSON"})
            return

        prompt = data.get("prompt", "")
        if not prompt.strip():
            self._respond(writer, 400, {"error": "Empty prompt"})
            return

        session_id = data.get("session_id")
        system_prompt = data.get("system_prompt")
        stream = data.get("stream", False)

        # Streamed mode: the status line goes out BEFORE the first event so the
        # client can start reading immediately.  From here on this connection
        # can no longer carry an HTTP error status -- the only feedback left is
        # a stream that ends short.
        if stream:
            self._write_status(writer, 200, "application/x-ndjson")
            try:
                # The agent emits many event types (thinking deltas, tool-call
                # framing, permission requests, engine prompts).  Only three
                # cross the HTTP wire: prose, tool outcomes, and the terminal
                # marker.  Everything else is silently dropped, which is the
                # correct default -- NDJSON consumers have no UI to render the
                # fine-grained framing the desktop client does.
                async for event in router.submit_stream(
                    self.name, prompt,
                    session_id=session_id,
                    system_prompt=system_prompt,
                ):
                    if isinstance(event, TextDelta) and event.text:
                        line = json.dumps({"type": "delta", "content": event.text}, ensure_ascii=False) + "\n"
                        writer.write(line.encode("utf-8"))
                    elif isinstance(event, ToolResult):
                        # Tool output can be arbitrarily large (a full file
                        # read).  Truncating at 500 chars keeps one NDJSON
                        # frame small enough to not stall the socket; the full
                        # result is still in the session transcript on disk.
                        line = json.dumps({
                            "type": "tool_result",
                            "id": event.id,
                            "content": event.content[:500],
                        }, ensure_ascii=False) + "\n"
                        writer.write(line.encode("utf-8"))
                    elif isinstance(event, Finish):
                        payload: dict[str, Any] = {"type": "finish", "reason": event.reason}
                        if event.usage:
                            payload["usage"] = event.usage
                        if event.context_usage:
                            payload["context_usage"] = event.context_usage
                        line = json.dumps(payload, ensure_ascii=False) + "\n"
                        writer.write(line.encode("utf-8"))
                # One drain for the whole run: the socket send buffer absorbs
                # the bursts, and a single drain is what applies backpressure
                # to the agent when a consumer stops reading.
                await writer.drain()
            except Exception as e:
                # Status is already on the wire, so this can only be logged;
                # the client observes a stream that ends without a "finish"
                # line, which is its signal that the run failed.
                logger.error("HTTP stream error: %s", e)
        else:
            result = await router.submit(
                self.name, prompt,
                session_id=session_id,
                system_prompt=system_prompt,
            )
            # Submit() decides the effective session id itself when the client
            # did not send one, so the reply cannot be filled in from the
            # request alone.  A throwaway session is created and removed right
            # away so the on-disk index never gains a record for a session no
            # one can reach; ``info`` is otherwise unused below.  The echoed id
            # is still the caller-supplied one, so a headless client that omits
            # "session_id" gets None back and cannot resume that run over HTTP.
            info = router.session_manager.create_session()
            await router.session_manager.remove_session(info.session_id)
            self._respond(writer, 200, {
                "response": result,
                "session_id": session_id,
            })

    # --- Response writers, shared by every endpoint above ---

    @staticmethod
    def _respond(writer: asyncio.StreamWriter, status: int, data: dict[str, Any]) -> None:
        """Write one complete JSON response with its status line.

        The single answer helper for every non-streamed endpoint.  It writes the
        header block and the body without draining, relying on the ``finally``
        in :meth:`_handle_client` to close the writer -- closing a writer
        flushes the pending buffer, so an explicit drain here would only add a
        round trip to the event loop per response.

        The trailing CRLF after the JSON body is deliberately outside the HTTP
        framing: it keeps the last frame on its own line when the raw TCP
        stream is inspected in logs or a packet capture, which makes a short
        JSON reply readable next to a bodyless transaction.

        Args:
            writer: Stream to write to.
            status: HTTP status code, expected to be one of the keys in
                :meth:`_write_status`'s map; anything else degrades to 200.
            data: Response body, JSON-encoded with ``ensure_ascii=False`` so
                non-ASCII prompt echoes come back readable rather than escaped.
        """
        body = json.dumps(data, ensure_ascii=False)
        HTTPChannel._write_status(writer, status, "application/json")
        writer.write(body.encode("utf-8"))
        writer.write(b"\r\n")

    @staticmethod
    def _write_status(writer: asyncio.StreamWriter, status: int, content_type: str) -> None:
        """Write the response start line and the fixed header set.

        Shared by :meth:`_respond` (``application/json``) and directly by the
        streaming path in :meth:`_handle_chat` (``application/x-ndjson``), which
        is why the content type is a parameter rather than a constant.

        The header block is fixed for every response:

          * ``Access-Control-Allow-Origin: *`` with the methods and headers
            allow-lists lets a browser tab reach the endpoint.  A wildcard
            origin is acceptable because the responses carry no credentials and
            the listener defaults to loopback.
          * ``Connection: close`` ends the transaction at the protocol level,
            matching the one-request-per-connection parser in
            :meth:`_handle_client`, so there is nothing to delimit between
            transactions.
          * The final ``\\r\\n`` is the header/body separator; everything written
            after this call is body.

        Args:
            writer: Stream to write to.
            status: HTTP status code to look up in the phrase map.
            content_type: MIME type for the ``Content-Type`` header.

        Note:
            An unknown code degrades to ``200 OK`` instead of raising, so a new
            status introduced in a caller cannot break the response path.  The
            cost is a misleading status line, so the map below should be
            extended whenever a new code appears in a caller.
        """
        # Canonical status phrases for the codes this channel can produce.
        # Add entries here whenever a caller introduces a new code.
        status_map = {
            200: b"200 OK",
            400: b"400 Bad Request",
            404: b"404 Not Found",
            500: b"500 Internal Server Error",
            503: b"503 Service Unavailable",
        }
        status_line = status_map.get(status, b"200 OK")
        headers = (
            f"HTTP/1.1 {status_line.decode()}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS\r\n"
            f"Access-Control-Allow-Headers: Content-Type, Authorization\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        writer.write(headers.encode("utf-8"))
