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

"""WebSocket relay transport (production).

The gateway dials out to the connector's ``/relay`` WebSocket endpoint,
authenticates the upgrade with a per-gateway HMAC bearer token, and exchanges
newline-delimited JSON frames.

Outbound (gateway -> connector)::

    hello              {type, gatewayId?, identities?}
    outbound           {type, requestId, action}         (send/edit/typing)
    follow_up          {type, requestId, action}         (token-less capability)
    interrupt          {type, session_key, reason?}
    going_idle         {type}
    inbound_ack        {type, bufferId}

Inbound (connector -> gateway)::

    descriptor         {type, descriptor}                 (handshake result)
    inbound            {type, event, bufferId?}
    outbound_result    {type, requestId, result}
    interrupt_inbound  {type, session_key, chat_id}
    going_idle_ack     {type}
    passthrough_forward {type, forward, bufferId?}       (body base64)

The handshake is a strict two-frame exchange: the gateway sends ``hello`` and
the connector must answer with exactly one ``descriptor`` frame.  Anything else
aborts the connection, because the adapter's capabilities -- message length,
edit support, markdown dialect -- are all derived from that descriptor.

Outbound RPCs (``send``/``edit``/``typing``/``follow_up``/``get_chat_info``)
block on a per-``requestId`` :class:`asyncio.Future` until the matching
``outbound_result`` arrives, with a 30-second timeout.  A timeout yields a
failure result rather than raising, so the stream consumer can back off
instead of aborting an agent turn.

A 4401 close after a successful handshake is a terminal revocation: the
transport stops reconnecting and surfaces ``was_revoked=True`` so the adapter
can mark the ``relay`` platform as disabled (non-retryable fatal).  A 4401
before any successful handshake stays retryable -- it is usually a cold-start
or not-yet-provisioned race rather than a deliberate revocation.

Dial mechanics: the configured URL may be ``http(s)://`` and is rewritten to a
``ws(s)://host/relay`` target by :func:`_ws_dial_url`, so deployments can write
a plain HTTPS origin in ``config.yaml``.
"""

import asyncio
import json
import logging
import secrets
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from encre.gateway.relay.auth import make_upgrade_token
from encre.gateway.relay.descriptor import CapabilityDescriptor

logger = logging.getLogger("encre.gateway.relay.ws_transport")

# A 4401 close after a successful handshake indicates a terminal revocation.
_RELAY_UNAUTHORIZED_CLOSE_CODE = 4401

RECONNECT_BASE = 1.0
RECONNECT_MAX = 30.0
OUTBOUND_TIMEOUT = 30.0


def _ws_dial_url(url: str) -> str:
    """Normalize a relay URL to a ws(s)://host/relay dial target.

    Converts https:// to wss:// and http:// to ws://; the path is forced to
    end with /relay (the connector's WebSocket endpoint).

    Args:
        url: The configured relay URL, typically the value of
            ``GATEWAY_RELAY_URL``.  Query and fragment are preserved so an
            operator can pass connector routing hints without a dedicated
            config key.

    Returns:
        A dialable ``ws(s)://`` URL whose path ends with ``/relay``.

    Note:
        An unrecognized scheme falls back to ``ws`` for ``http``-like input and
        to ``wss`` only for an explicit ``https``.  The fallback exists so a
        typo in the scheme does not crash at dial time but still dials
        plaintext-ish and fails visibly.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme == "https":
        scheme = "wss"
    elif scheme == "http":
        scheme = "ws"
    elif scheme not in ("ws", "wss"):
        scheme = "wss" if scheme == "https" else "ws"
    path = parsed.path or ""
    if not path.endswith("/relay"):
        path = (path.rstrip("/") + "/relay") if path else "/relay"
    return urlunparse((scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))


class WebSocketRelayTransport:
    """Production relay transport over an outbound WebSocket.

    Implements the :class:`encre.gateway.relay.transport.RelayTransport`
    Protocol. The gateway dials the connector, authenticates with a per-gateway
    HMAC bearer token, receives a :class:`CapabilityDescriptor` at handshake,
    and then exchanges frames.

    Lifecycle: constructed with a URL and identity -> :meth:`connect` opens the
    socket, performs the ``hello``/``descriptor`` exchange, and spawns the read
    loop -> inbound frames are dispatched and outbound RPCs resolved ->
    :meth:`disconnect` closes the socket and fails any outstanding RPC.

    Concurrency: all state is owned by one event loop.  Outbound RPCs and
    inbound dispatch interleave freely, and :attr:`_pending` is a plain dict
    because every read/write of it happens inside a single synchronous block
    with no ``await`` in between.

    Attributes:
        _url: The normalized dial target.
        _platform / _bot_id / _identities: The advertised identity set.  One
            WebSocket can front N platforms; ``_identities`` is echoed in the
            hello frame so the connector knows which identities to activate.
        _connect_timeout: Bounds both the dial and the handshake wait.
        _outbound_timeout: Bounds a single outbound RPC.
        _gateway_id / _upgrade_secret: The WS-upgrade credential pair.  When
            either is missing the transport dials unauthenticated, which the
            connector may accept or reject.
        _reconnect / _reconnect_base / _reconnect_max: Backoff knobs.  Retries
            are driven externally by :class:`encre.gateway.run.GatewayRunner`
            today, so these are retained here for an internal loop rather than
            read on every call.
        _ws: The live ``websockets`` connection, or None when disconnected.
        _running: The transport intends to be up.  Cleared by :meth:`connect`
            on failure and by :meth:`disconnect`.
        _connected: A socket is currently usable.  Cleared by the read loop on
            any close and by a failed :meth:`_send_raw`.
        _handshake_done: A ``descriptor`` frame was received at least once.
            This is the flag that distinguishes a terminal 4401 (after) from a
            retryable 4401 (before).
        _was_revoked: Terminal revocation observed.  Once True it is never
            cleared, because reconnecting against a revoked credential is
            hostile behaviour.
        _descriptor: The negotiated capability profile.
        _pending: request_id -> future awaiting its ``outbound_result``.
        _going_idle_ack: Future awaiting ``going_idle_ack``.  At most one idle
            request is in flight at a time.
        _read_task: The read-loop coroutine.  Cancelled during disconnect; not
            awaited, since awaiting a cancelled reader would deadlock a shutdown.
        _read_buf: Leftover bytes between complete lines.  Frames are
            newline-delimited JSON, so a socket read may split or batch lines.
    """

    def __init__(
        self,
        url: str,
        platform: str,
        bot_id: str | None = None,
        *,
        identities: list[dict[str, Any]] | None = None,
        connect_timeout_s: float = 30.0,
        outbound_timeout_s: float = OUTBOUND_TIMEOUT,
        gateway_id: str | None = None,
        upgrade_secret: str | None = None,
        reconnect: bool = False,
        reconnect_backoff_s: float = RECONNECT_BASE,
        reconnect_max_backoff_s: float = RECONNECT_MAX,
    ) -> None:
        """Create a transport bound to *url*.

        Args:
            url: The connector origin.  Normalized by :func:`_ws_dial_url`.
            platform: The primary platform this transport fronts.  Used to
                label the connection; the full identity set is in *identities*.
            bot_id: The primary bot/identity id on the fronted platform.
            identities: All identities this gateway fronts, echoed in the hello
                frame.  Empty list is allowed and means "let the connector
                decide".
            connect_timeout_s: Bound on both the dial and the handshake wait.
            outbound_timeout_s: Bound on a single outbound RPC.
            gateway_id: The gateway identifier for the upgrade token payload.
            upgrade_secret: The per-gateway HMAC secret.  Together with
                *gateway_id* this produces the bearer token; missing either
                yields an unauthenticated dial.
            reconnect: Whether an internal reconnect loop is wanted.  Retries
                are currently owned by the runner.
            reconnect_backoff_s: Initial backoff for a reconnect loop.
            reconnect_max_backoff_s: Backoff ceiling.
        """
        self._url = _ws_dial_url(url)
        self._platform = platform
        self._bot_id = bot_id
        self._identities = identities or []
        self._connect_timeout = connect_timeout_s
        self._outbound_timeout = outbound_timeout_s
        self._gateway_id = gateway_id
        self._upgrade_secret = upgrade_secret
        self._reconnect = reconnect
        self._reconnect_base = reconnect_backoff_s
        self._reconnect_max = reconnect_max_backoff_s

        self._ws: Any = None
        self._running = False
        self._connected = False
        self._handshake_done = False
        # True once the connector closed with 4401 after a successful handshake
        # (terminal revocation -- stop reconnecting).
        self._was_revoked = False
        self._descriptor: CapabilityDescriptor | None = None
        # Maps request IDs to futures awaiting the matching outbound_result.
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Future awaiting the connector's going_idle_ack response.
        self._going_idle_ack: asyncio.Future[None] | None = None
        self._inbound_handler: Callable[[dict[str, Any]], None] | None = None
        self._passthrough_handler: Callable[[dict[str, Any]], None] | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._read_buf = ""

    # ---- properties ----

    @property
    def was_revoked(self) -> bool:
        """Return True if the connector revoked this gateway (4401 after handshake).

        Read by :class:`encre.gateway.relay.adapter.RelayAdapter` to decide
        whether a failed connect is terminal.  Never resets: a revoked gateway
        must be re-provisioned, not re-dialed.

        Returns:
            True once a 4401 close was observed after a successful handshake.
        """
        return self._was_revoked

    @property
    def is_connected(self) -> bool:
        """Return True when the WebSocket is connected.

        Returns:
            True while a socket is open and usable.  Cleared immediately on
            read-loop termination or a failed send, so outbound calls fail fast
            with a retryable result instead of blocking until their timeout.
        """
        return self._connected

    # ---- handler registration ----

    def set_inbound_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """Register the callback invoked when an inbound frame arrives.

        Args:
            handler: Receives the normalized event dict.  Control frames routed
                through the inbound path, notably the ``__interrupt__`` tagged
                ``interrupt_inbound`` event, arrive here too.

        Note:
            Must be registered before :meth:`connect` completes, otherwise the
            read loop could be live with no handler attached and frames would be
            silently dropped.  :meth:`connect` registers nothing itself.
        """
        self._inbound_handler = handler

    def set_passthrough_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """Register the callback invoked when a passthrough_forward frame arrives.

        Args:
            handler: Receives the decoded forward envelope
                (``{platform, botId, method, path, headers, bodyB64}``) whose
                provider signature the connector already verified.
        """
        self._passthrough_handler = handler

    # ---- connect / handshake ----

    async def connect(self) -> bool:
        """Open the WebSocket, perform the handshake, and start the read loop.

        Sequence: mint the upgrade token -> dial with a bounded timeout -> send
        ``hello`` -> require a ``descriptor`` answer -> mark connected -> spawn
        the read loop.

        Returns:
            True on success.  False when the dial fails, when the handshake
            answer is missing, or when the first frame is not a descriptor.
            On a terminal 4401 revocation the failure is additionally surfaced
            via :attr:`was_revoked` so the caller must not retry.

        Note:
            ``_running`` is set before the dial and cleared on every failure
            path, since the adapter polls transport state after ``connect``
            returns and must never see a half-open transport reported as live.
        """
        import websockets

        self._running = True
        headers: dict[str, str] = {}
        if self._gateway_id and self._upgrade_secret:
            token = make_upgrade_token(self._gateway_id, self._upgrade_secret)
            headers["Authorization"] = f"Bearer {token}"

        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self._url,
                    additional_headers=headers,
                    max_size=10 * 1024 * 1024,
                    ping_interval=15.0,
                    ping_timeout=10.0,
                ),
                timeout=self._connect_timeout,
            )
        except Exception as e:
            logger.warning("[relay] connect to %s failed: %s %s", self._url, type(e).__name__, e)
            self._running = False
            return False

        # Send hello frame and wait for the descriptor handshake response.
        hello: dict[str, Any] = {"type": "hello"}
        if self._gateway_id:
            hello["gatewayId"] = self._gateway_id
        if self._identities:
            hello["identities"] = self._identities
        await self._send_raw(hello)

        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=self._connect_timeout)
        except Exception as e:
            logger.warning("[relay] handshake descriptor not received: %s", e)
            await self._ws.close()
            self._running = False
            return False

        frame = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if frame.get("type") != "descriptor":
            logger.error("[relay] expected descriptor, got %s", frame.get("type"))
            await self._ws.close()
            self._running = False
            return False

        self._descriptor = CapabilityDescriptor.from_dict(frame["descriptor"])
        self._connected = True
        self._handshake_done = True
        logger.info("[relay] handshake OK: platform=%s label=%s max_len=%d",
                    self._descriptor.platform, self._descriptor.label,
                    self._descriptor.max_message_length)
        self._read_task = asyncio.ensure_future(self._read_loop())
        return True

    async def handshake(self) -> CapabilityDescriptor:
        """Return the negotiated capability descriptor from the handshake.

        Returns:
            The :class:`CapabilityDescriptor` parsed from the connector's
            ``descriptor`` frame.  The adapter derives its ``max_message_length``
            and ``splits_long_messages`` capabilities from this object, so it is
            returned rather than re-read from the wire.

        Raises:
            RuntimeError: When no handshake has completed yet, i.e.
                :meth:`connect` was never called or failed.
        """
        if self._descriptor is None:
            raise RuntimeError("handshake not complete -- call connect() first")
        return self._descriptor

    async def disconnect(self) -> None:
        """Close the WebSocket and clean up all pending state.

        Order matters: clear the intent-to-run and connection flags first so a
        concurrent outbound call sees "not connected" and returns a retryable
        failure, then cancel the read loop, close the socket, and finally fail
        every outstanding RPC.  Awaiting the read loop is deliberately avoided,
        since the loop is blocked on socket reads and would stall a shutdown.

        Note:
            ``_handshake_done`` and ``_was_revoked`` are preserved, so a
            revocation observed before shutdown still reports
            :attr:`was_revoked` as True after teardown.
        """
        self._running = False
        self._connected = False
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        # Fail any pending outbound RPCs.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("relay transport closed"))
        self._pending.clear()
        if self._going_idle_ack is not None and not self._going_idle_ack.done():
            self._going_idle_ack.set_exception(RuntimeError("relay transport closed"))
            self._going_idle_ack = None

    # ---- outbound RPC ----

    async def _send_with_request(
        self,
        frame_type: str,
        action: dict[str, Any],
        *,
        platform: str | None = None,
    ) -> dict[str, Any]:
        """Send an outbound frame and await the matching outbound_result.

        Each request gets a fresh 64-bit random ``requestId`` and a future that
        the read loop resolves when the connector's matching ``outbound_result``
        arrives.  Registering the future *before* sending the frame closes the
        race in which the result would arrive and be dropped for an unknown
        request id.

        Args:
            frame_type: The outbound frame kind, e.g. ``outbound`` or
                ``follow_up``.
            action: The action payload the connector executes.
            platform: The originating platform stamp, needed because one
                WebSocket fronts several platforms.

        Returns:
            The connector's result dict, or a failure dict when the transport is
            not connected or the request timed out.  Timeouts and send errors
            are converted to results rather than raised so the adapter can
            report a retryable failure to the stream consumer.

        Raises:
            Exception: Propagated only from a transport error that escapes
                :meth:`_send_raw`, which normally swallows socket failures.
        """
        if not self._connected:
            return {"success": False, "error": "relay not connected"}
        request_id = secrets.token_hex(8)
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = fut
        frame: dict[str, Any] = {"type": frame_type, "requestId": request_id, "action": action}
        if platform:
            frame["platform"] = platform
        try:
            await self._send_raw(frame)
            return await asyncio.wait_for(fut, timeout=self._outbound_timeout)
        except asyncio.TimeoutError:
            logger.warning("[relay] outbound %s timed out (requestId=%s)", frame_type, request_id)
            return {"success": False, "error": "outbound timed out"}
        finally:
            self._pending.pop(request_id, None)

    async def send_outbound(self, action: dict[str, Any], *, platform: str | None = None) -> dict[str, Any]:
        """Send an outbound action (send/edit/typing) and await the result.

        Args:
            action: The action payload, carrying an ``op`` discriminator.
            platform: The originating platform stamp for the connector's egress
                guard.

        Returns:
            The connector's ``outbound_result`` payload.
        """
        return await self._send_with_request("outbound", action, platform=platform)

    async def send_follow_up(self, action: dict[str, Any], *, platform: str | None = None) -> dict[str, Any]:
        """Send a token-less follow_up action and await the result.

        The gateway names the session and capability kind; the connector
        resolves the real token from its vault, so no capability material ever
        enters the gateway process.

        Args:
            action: The follow-up action payload.
            platform: The originating platform stamp.

        Returns:
            The connector's ``outbound_result`` payload.
        """
        return await self._send_with_request("follow_up", action, platform=platform)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Fetch chat metadata for the given chat_id via the relay.

        Reuses the generic outbound RPC path with an ``op`` of
        ``get_chat_info``; the connector answers with a ``chat_info`` object in
        the result.

        Args:
            chat_id: The chat to describe.

        Returns:
            The connector's ``chat_info`` dict when the call succeeds,
            otherwise a synthetic ``{"name": chat_id, "type": "dm"}`` record so
            callers that only need a display name never crash.
        """
        result = await self._send_with_request(
            "outbound", {"op": "get_chat_info", "chat_id": chat_id}
        )
        if result.get("success"):
            return result.get("chat_info", {"name": chat_id, "type": "dm"})
        return {"name": chat_id, "type": "dm"}

    async def send_interrupt(self, session_key: str, reason: str | None = None) -> None:
        """Send an interrupt frame to cancel the given session's current turn.

        Fire-and-forget: unlike the outbound RPCs there is no result frame, so
        the caller cannot learn whether the interrupt landed.  A disconnected
        transport is a silent no-op rather than an error, since an interrupt is
        best-effort.

        Args:
            session_key: The session whose turn should stop.
            reason: Optional reason forwarded to the connector for its logs.
        """
        if not self._connected:
            return
        frame: dict[str, Any] = {"type": "interrupt", "session_key": session_key}
        if reason:
            frame["reason"] = reason
        await self._send_raw(frame)

    async def go_idle(self, timeout_s: float = 10.0) -> bool:
        """Emit going_idle and await going_idle_ack (scale-to-zero primitive).

        The connector buffers inbound platform traffic while the gateway is
        away and replays it on reconnect, so the acknowledgment is what makes it
        safe to suspend the process.

        Args:
            timeout_s: How long to wait for the acknowledgment.

        Returns:
            True if the connector acknowledged the idle flip.  False when the
            transport is not connected or the acknowledgment timed out -- a
            timeout means the connector never confirmed, so the gateway must
            keep the connection open.

        Note:
            The future is cleared in ``finally``, so an acknowledgment that
            arrives after a timeout is dropped rather than leaking into the
            next idle attempt.
        """
        if not self._connected:
            return False
        loop = asyncio.get_event_loop()
        self._going_idle_ack = loop.create_future()
        try:
            await self._send_raw({"type": "going_idle"})
            await asyncio.wait_for(self._going_idle_ack, timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            logger.warning("[relay] going_idle_ack timed out")
            return False
        finally:
            self._going_idle_ack = None

    # ---- raw send ----

    async def _send_raw(self, frame: dict[str, Any]) -> None:
        """Send a raw JSON frame as a newline-terminated message.

        The trailing newline is part of the wire framing: the read loop on the
        other end splits on newlines, so a frame sent without one would never
        be delivered as a complete line.

        Args:
            frame: The frame dict to send.

        Note:
            A send failure clears ``_connected`` and logs rather than raising.
            This is deliberate -- outbound callers convert "not connected" into
            a retryable result, whereas an exception here would escape through
            an agent turn.  The tradeoff is that a silently dropped frame is
            only visible through the warning log.
        """
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(frame, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("[relay] send failed: %s", e)
            self._connected = False

    # ---- read loop / frame dispatch ----

    async def _read_loop(self) -> None:
        """Continuously read frames from the WebSocket and dispatch them.

        Frames are newline-delimited JSON, and a single socket read may return
        a partial line or several lines at once.  Reads are therefore
        accumulated in :attr:`_read_buf`, split on newlines, and the tail kept
        for the next read.

        Note:
            A malformed line is logged and skipped rather than fatal, so one
            corrupted frame cannot take the connection down.  Handler
            exceptions are likewise caught, because a handler bug must not
            close the socket.
        """
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                self._read_buf += raw
                # Newline-delimited JSON: split complete lines, keep the tail.
                while "\n" in self._read_buf:
                    line, self._read_buf = self._read_buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        await self._handle_frame(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("[relay] bad frame: %.80s", line)
                    except Exception as e:
                        logger.warning("[relay] frame handler error: %s %s", type(e).__name__, e)
        except Exception as e:
            # Detect a 4401 close after a successful handshake as revocation.
            close_code = getattr(getattr(e, "code", None), "value", None) or getattr(e, "code", None)
            if close_code == _RELAY_UNAUTHORIZED_CLOSE_CODE and self._handshake_done:
                self._was_revoked = True
                logger.error("[relay] closed 4401 after handshake -- terminal revocation, not reconnecting")
            else:
                logger.info("[relay] read loop ended: %s %s", type(e).__name__, e)
        finally:
            self._connected = False
            # Fail pending RPCs.
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("relay connection closed"))
            self._pending.clear()

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        """Dispatch a received frame to the appropriate handler based on its type.

        Every frame carries a ``type`` discriminator and is routed as follows:

        - ``inbound``: delivered to the inbound handler, and an ``inbound_ack``
          is sent back if a ``bufferId`` was present.  The ack is what lets the
          connector stop buffering a message the gateway has accepted.
        - ``outbound_result``: resolves the future registered by
          :meth:`_send_with_request`, keyed by ``requestId``.
        - ``interrupt_inbound``: re-wrapped as a tagged inbound event so the
          adapter's single inbound handler can reach ``on_interrupt``.
        - ``going_idle_ack``: resolves the pending :attr:`_going_idle_ack`.
        - ``passthrough_forward``: delivered to the passthrough handler.
        - ``descriptor``: accepted even after the initial handshake, so a
          re-handshake or a late capability refresh is applied.
        - anything else: logged at debug and ignored, which is the
          forward-compatibility path that lets the connector add frame kinds
          without breaking an older gateway.

        Args:
            frame: A parsed frame dict.  Unrecognized fields are ignored.
        """
        ftype = frame.get("type", "")
        if ftype == "inbound":
            if self._inbound_handler:
                self._inbound_handler(frame.get("event", {}))
            # Acknowledge buffered delivery if a bufferId was present.
            buf = frame.get("bufferId")
            if buf:
                await self._send_raw({"type": "inbound_ack", "bufferId": buf})
        elif ftype == "outbound_result":
            fut = self._pending.get(frame.get("requestId", ""))
            if fut is not None and not fut.done():
                fut.set_result(frame.get("result", {}))
        elif ftype == "interrupt_inbound":
            # Bridged by the adapter's on_interrupt via the inbound handler path.
            if self._inbound_handler:
                self._inbound_handler({
                    "__interrupt__": True,
                    "session_key": frame.get("session_key"),
                    "chat_id": frame.get("chat_id"),
                })
        elif ftype == "going_idle_ack":
            if self._going_idle_ack is not None and not self._going_idle_ack.done():
                self._going_idle_ack.set_result(None)
        elif ftype == "passthrough_forward":
            if self._passthrough_handler:
                self._passthrough_handler(frame.get("forward", {}))
        elif ftype == "descriptor":
            # A late or re-handshake descriptor -- accept it.
            self._descriptor = CapabilityDescriptor.from_dict(frame.get("descriptor", {}))
        else:
            logger.debug("[relay] unhandled frame type: %s", ftype)
