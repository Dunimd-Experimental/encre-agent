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

"""Relay adapter: fronts N platforms over a single connector WebSocket.

:class:`RelayAdapter` is a
:class:`encre.gateway.platforms.base.BasePlatformAdapter` subclass registered
as the ``relay`` platform.  Where an adapter in :mod:`encre.gateway.platforms`
speaks one vendor's protocol directly, this one dials out to a connector over a
:class:`encre.gateway.relay.transport.RelayTransport` and lets the connector
front the real platform.  The gateway never learns which concrete platform it
is fronting -- the connector owns all platform-specific socket handling,
identity, and credential material.  Because of that, one ``RelayAdapter`` can
stand in for Discord, Telegram, Matrix, and others at once; the per-platform
behaviour is derived from the :class:`encre.gateway.relay.descriptor.CapabilityDescriptor`
returned at handshake, which is why ``max_message_length`` and
``splits_long_messages`` here are properties rather than class constants.

The adapter presents itself to :class:`encre.gateway.run.GatewayRunner` as an
ordinary platform, so the runner's authorization gate, session routing,
stream consumer, and delivery router all apply unchanged.  What differs:

- ``authorization_is_upstream`` is True, so
  :meth:`encre.gateway.platforms.base.BasePlatformAdapter.handle_message` skips
  the local five-layer authorization check.  The connector has already applied
  the platform's authorization policy at the edge.
- ``go_dormant()`` suspends the connection so the gateway can scale to zero.
  The connector buffers inbound traffic while the gateway is gone and replays
  it on reconnect, which is why this is distinct from ``disconnect``.
- ``_platform_by_chat`` stamps every chat_id with its originating platform so
  outbound replies carry the right platform hint.  A single WebSocket fronts N
  platforms, and the connector's egress guard needs that stamp to route.
- ``on_interrupt`` / ``_on_passthrough`` / ``_on_inbound`` bridge the
  connector's ``interrupt_inbound`` / ``passthrough_forward`` / ``inbound``
  frames into the adapter's existing per-session machinery.
- A 4401 close after a successful handshake is a terminal revocation: the
  adapter records a non-retryable ``relay_disabled`` fatal error so the runner
  does not loop reconnection attempts against a gateway the connector has
  deliberately revoked.
"""

import asyncio
import logging
from typing import Any

from encre.gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from encre.gateway.session import SessionSource
from encre.gateway.relay.descriptor import CapabilityDescriptor
from encre.gateway.relay.transport import RelayTransport
from encre.gateway.relay.ws_transport import WebSocketRelayTransport

logger = logging.getLogger("encre.gateway.relay.adapter")

# Fatal error code surfaced when the connector revokes the gateway.  It is the
# value :class:`GatewayRunner` reports in the adapter panel, and it is recorded
# as non-retryable so no reconnect loop runs against a revoked credential.
RELAY_DISABLED_CODE = "relay_disabled"


class RelayAdapter(BasePlatformAdapter):
    """A platform adapter that fronts connector-backed platforms.

    Registered as the ``relay`` platform when ``GATEWAY_RELAY_URL`` or
    ``gateway.relay_url`` is configured (see :mod:`encre.gateway.relay`).

    Lifecycle: constructed with a transport -> :meth:`connect` opens the
    transport, runs the capability handshake, and spawns the revocation
    monitor -> inbound frames arrive as coroutines on
    ``_background_tasks`` -> :meth:`disconnect` tears the transport down.

    Invariants:

    - ``_running`` is False unless the transport connected **and** the
      capability handshake produced a descriptor.  ``_mark_connected`` is
      called only after ``handshake()``, never on a bare socket open.
    - ``authorization_is_upstream`` is always True, so the adapter never
      runs the local authorization check.  Reversing this would double-gate
      messages the connector already authorized.
    - Every outbound call goes through ``_platform_by_chat`` for the platform
      stamp.  Dropping the stamp would leave the connector unable to route a
      reply back to the right vendored identity.

    Concurrency: one adapter instance is owned by one event loop.  Inbound
    handling is dispatched as fire-and-forget coroutines on ``_background_tasks``
    so the transport's read loop never blocks on agent latency; the base
    class's two-level concurrency guard then serializes messages per session
    key.  All transport I/O is async, so no lock is needed.

    Attributes:
        _transport: The :class:`RelayTransport` this adapter exchanges frames
            with.  ``None`` means "never constructed", which is reported as a
            fatal error rather than a transient failure.
        _descriptor: The negotiated capability profile.  ``None`` until
            handshake; drives ``max_message_length`` and ``splits_long_messages``.
        _fatal_error_code / _fatal_error_message: Set by ``_set_fatal_error``;
            surfaced by the runner.  ``_fatal_error_retryable`` is not touched
            here, so it keeps the base default of True unless the transport sets
            it via ``_set_fatal_error(..., retryable=False)``.
        _platform_by_chat: chat_id -> originating platform name.  Populated from
            each inbound event's :class:`SessionSource` and read by
            :meth:`send`.  Entries are never evicted, which is fine because
            chat ids are stable per conversation.
        _active_sessions / _pending_messages: Inherited from the base; the
            two-level concurrency guard and per-session interrupt events live
            here.  ``_pending_messages`` is declared but the relay path does not
            populate it.
        _last_push_chat_id: Reserved for outbound push-target bookkeeping; the
            relay path does not currently write it.
        _background_tasks: Tracked coroutines (inbound handling, revocation
            monitor).  Each one removes itself via a done callback so the set
            does not grow with completed tasks.
    """

    name = "relay"

    def __init__(
        self,
        transport: RelayTransport | None = None,
        *,
        descriptor: CapabilityDescriptor | None = None,
        config=None,
        platform=None,
    ) -> None:
        """Create a relay adapter bound to *transport*.

        This constructor deliberately does **not** call
        :meth:`BasePlatformAdapter.__init__`, which requires a
        :class:`encre.gateway.config.PlatformConfig` and a
        :class:`encre.gateway.config.Platform` enum member.  The relay platform
        is not present in :mod:`encre.gateway.platform_registry`, so no
        ``PlatformConfig`` is ever built for it.  Instead the same private
        attributes the base would set are initialized inline here, which keeps
        every inherited helper -- :meth:`handle_message`, ``_mark_connected``,
        ``_set_fatal_error``, ``_spawn_task``, the two-level concurrency guard --
        working without a vendor config.

        ``config`` and ``platform`` are accepted only for call-signature parity
        with other adapters and are intentionally ignored.

        Args:
            transport: The relay transport to drive.  ``None`` is permitted for
                test doubles and makes :meth:`connect` fail with a fatal error.
            descriptor: An optional pre-negotiated capability descriptor, used
                when a caller already has one (e.g. tests).  A real handshake
                overwrites it.
            config: Unused; see above.
            platform: Unused; see above.
        """
        self._transport: RelayTransport | None = transport
        self._descriptor = descriptor
        self._running = False
        self._fatal_error_code: str | None = None
        self._fatal_error_message: str | None = None
        self._message_handler = None
        self._active_sessions: dict[str, asyncio.Event] = {}
        self._pending_messages: dict[str, MessageEvent] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._last_push_chat_id: str | None = None
        self._authz = None
        self._pairing = None
        # Outbound reply platform stamp: chat_id -> originating platform name.
        # A single WebSocket fronts N platforms, so the connector's egress guard
        # needs to know which platform a reply targets.
        self._platform_by_chat: dict[str, str] = {}

    # ---- capability bits ----

    @property
    def authorization_is_upstream(self) -> bool:
        """Return True to indicate the connector enforces authorization locally.

        :class:`GatewayRunner` and
        :meth:`BasePlatformAdapter.handle_message` both branch on this flag and
        skip the local authorization and ``/pair`` handling when it is True.
        The connector has already applied the platform's authorization policy
        before a message reaches the gateway, so re-checking locally would be
        both redundant and unable to see the platform-side allow-list.

        Returns:
            Always True for the relay platform.
        """
        return True

    @property
    def max_message_length(self) -> int:
        """Return the maximum message length from the descriptor.

        :class:`encre.gateway.stream_consumer` and
        :class:`encre.gateway.delivery.DeliveryRouter` read this before deciding
        whether to chunk or truncate an outbound payload, so it must reflect
        the connector's fronted platform rather than a relay-level constant.

        Returns:
            The negotiated limit, or 0 when no descriptor has been negotiated
            yet.  0 means "no limit" in the base contract, which is the correct
            answer for a pre-handshake adapter because nothing can be sent at
            that point.
        """
        if self._descriptor is not None:
            return self._descriptor.max_message_length
        return 0

    @property
    def splits_long_messages(self) -> bool:
        """Return True when the connector's fronted platform supports edit streaming.

        The delivery router hands the full payload to adapters that advertise
        self-chunking, and truncates with a pointer to an audit file for the
        rest.  Message *editing* is what lets a relayed platform stream deltas
        by updating one message, so the descriptor's ``supports_edit`` bit is
        the right proxy for "can chunk safely".

        Returns:
            True when the descriptor is present and reports edit support.
        """
        return bool(self._descriptor and self._descriptor.supports_edit)

    # Connectivity is reported from the transport, not from a local vendor client.
    @property
    def is_connected(self) -> bool:
        """Return True when the relay transport is connected and running.

        Unlike most adapters, which read a vendor client, the relay's only
        connection state is the transport's.  The ``is_connected`` attribute is
        read via ``getattr`` with a True default so a transport that omits the
        attribute (a minimal test double) is still treated as live.

        Returns:
            True only when ``_running`` is set *and* the transport reports
            connected.  ``_running`` alone is not sufficient because the
            transport can drop out from under the adapter.
        """
        return self._running and self._transport is not None and getattr(self._transport, "is_connected", True)

    # ---- lifecycle ----

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Open the relay transport and register inbound and passthrough handlers.

        Sequence: register the frame handlers -> open the transport -> run the
        capability handshake -> mark connected -> spawn the revocation monitor.
        Registering the handlers before ``transport.connect()`` avoids a window
        in which the read loop could be live with no inbound handler attached.

        Args:
            is_reconnect: Present for signature parity with the base contract.
                Reconnects are driven by the runner; this method is idempotent
                with respect to a fresh transport connect.

        Returns:
            True on success.  False when there is no transport, when the
            connector revoked the gateway (terminal, surfaced via
            ``relay_disabled``), or when the transport could not connect
            (retryable ``connect_failed``).

        Raises:
            RuntimeError: Propagated from
                :meth:`RelayTransport.handshake` if the handshake response is
                structurally invalid.  The caller (registration) treats this as
                a failed registration.
        """
        if self._transport is None:
            self._set_fatal_error(RELAY_DISABLED_CODE, "no relay transport configured")
            return False
        self._transport.set_inbound_handler(self._on_inbound)
        self._transport.set_passthrough_handler(self._on_passthrough)
        ok = await self._transport.connect()
        if not ok:
            if self._transport.was_revoked:
                self._set_fatal_error(
                    RELAY_DISABLED_CODE,
                    "relay connector revoked this gateway (4401 after handshake)",
                )
            else:
                self._set_fatal_error("connect_failed", "relay transport connect failed")
            return False
        self._descriptor = await self._transport.handshake()
        self._running = True
        self._mark_connected()
        # Monitor for a post-handshake revocation (4401) in the background.
        _t = asyncio.ensure_future(self._monitor_revocation())
        self._background_tasks.add(_t)
        _t.add_done_callback(self._background_tasks.discard)
        return True

    async def _monitor_revocation(self) -> None:
        """Watch the transport for a terminal 4401 revocation after handshake.

        A revocation can arrive long after the handshake, in the middle of an
        otherwise healthy conversation, and it arrives as a socket close rather
        than a frame.  The transport's read loop sets ``was_revoked`` when it
        sees a 4401 close after a successful handshake; polling it here turns
        that into the adapter-level fatal error the runner needs to stop
        reconnecting and to show a stable error in the adapter panel.

        Polling rather than a callback keeps the transport free of adapter
        knowledge -- the transport never imports the adapter.

        Note:
            Terminates either on revocation or when ``_running`` is cleared by
            :meth:`disconnect`, which makes the loop exit on the next 5-second
            tick.
        """
        transport = self._transport
        if transport is None:
            return
        while self._running:
            await asyncio.sleep(5.0)
            if getattr(transport, "was_revoked", False):
                self._set_fatal_error(
                    RELAY_DISABLED_CODE,
                    "relay connector revoked this gateway (4401 after handshake)",
                )
                return

    async def disconnect(self) -> None:
        """Stop the relay transport and mark the adapter as not running.

        Clears ``_running`` *before* closing the transport so the revocation
        monitor exits its poll loop instead of recording a spurious fatal error
        from a transport that is shutting down on purpose.  Fatal error state
        set by a real revocation is preserved by the base, so a revoked gateway
        still reports ``relay_disabled`` after shutdown.
        """
        self._running = False
        if self._transport is not None:
            await self._transport.disconnect()

    async def go_dormant(self) -> bool:
        """Scale-to-zero primitive: ask the connector to buffer and flip to idle.

        Distinct from :meth:`disconnect` -- the connector keeps the platform
        side alive, buffers inbound traffic while the gateway is gone, and
        replays it on reconnect.  A caller that only wanted to stop processing
        would use ``disconnect``, which tears the platform link down with it.

        Returns:
            True when the connector acknowledged the idle flip, False when there
            is no transport or the acknowledgment timed out.  A False return
            does not mean the gateway is still busy; it means the connector did
            not confirm, so the caller should keep the connection open.
        """
        if self._transport is None:
            return False
        return await self._transport.go_idle()

    # ---- BasePlatformAdapter abstract surface ----

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a message to *chat_id* via the relay transport.

        This is the outbound half of the adapter contract consumed by
        :class:`encre.gateway.stream_consumer.GatewayStreamConsumer` and
        :class:`encre.gateway.delivery.DeliveryRouter`: agent output arrives
        here as plain text and leaves as an ``outbound`` frame carrying a
        ``send`` action.  The platform stamp is looked up from
        ``_platform_by_chat`` so a chat_id that originated on one platform is
        not answered through another identity's socket.

        Args:
            chat_id: The destination chat, in the connector's id space.
            content: The message body.  Chunking is the caller's job here -- see
                :attr:`splits_long_messages`.
            reply_to: Optional id of the message to reply to.
            metadata: Optional platform-neutral extras forwarded verbatim in the
                action so the connector can apply them (e.g. a media payload).

        Returns:
            A :class:`SendResult`.  A disconnected transport yields a retryable
            transient failure rather than raising, so the stream consumer can
            back off instead of propagating an exception through the agent turn.
        """
        if self._transport is None or not self._transport.is_connected:
            return SendResult(success=False, error="relay not connected", retryable=True, error_kind="transient")
        action: dict[str, Any] = {"op": "send", "chat_id": chat_id, "content": content}
        if reply_to:
            action["reply_to"] = reply_to
        if metadata:
            action["metadata"] = metadata
        result = await self._transport.send_outbound(action, platform=self._platform_by_chat.get(chat_id))
        return self._result_from_transport(result)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Fetch chat metadata for *chat_id* via the relay transport.

        The gateway needs a human-readable chat name for session labels and the
        desktop UI, and it cannot ask the vendor directly because it holds no
        vendor credential.  The connector proxies the lookup instead.

        Args:
            chat_id: The chat to describe.

        Returns:
            A dict with at least ``name`` and ``type``.  When there is no
            transport a synthetic DM-shaped record is returned so callers that
            only need *something* to render never crash; ``chat_id`` itself is
            used as the display name.
        """
        if self._transport is None:
            return {"name": chat_id, "type": "dm"}
        return await self._transport.get_chat_info(chat_id)

    async def send_follow_up(
        self,
        session_key: str,
        kind: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a token-less follow_up action (A2 shared-identity capability).

        Some platforms need a short-lived token to act again on an existing
        conversation -- a Discord interaction token, for instance.  Rather than
        shipping that token to the gateway, the gateway names the session it is
        already in and the capability ``kind``, and the connector resolves the
        real value from its own vault.  The gateway therefore holds zero
        capability material, so a leaked gateway cannot wield tenant
        credentials.

        Args:
            session_key: The session the follow-up belongs to, used by the
                connector to look up the vault entry.
            kind: The capability name, e.g. ``discord.interaction_token``.
            content: The message body.
            metadata: Optional extras forwarded verbatim to the connector.

        Returns:
            A :class:`SendResult`, or a retryable failure when no transport is
            attached.
        """
        if self._transport is None:
            return SendResult(success=False, error="relay not connected", retryable=True)
        action = {
            "op": "follow_up",
            "session_key": session_key,
            "kind": kind,
            "content": content,
        }
        if metadata:
            action["metadata"] = metadata
        result = await self._transport.send_follow_up(action)
        return self._result_from_transport(result)

    @staticmethod
    def _result_from_transport(result: dict[str, Any]) -> SendResult:
        """Convert a transport result dict into a :class:`SendResult`.

        The transport speaks plain dicts because the frames are JSON; the adapter
        contract requires a typed result.  Every field is optional on the wire,
        so each one is coerced with a safe default -- a missing ``success`` is
        treated as a failure rather than a crash, and ``retryable`` defaults to
        False so an unknown outcome is never silently retried in a loop.

        Args:
            result: The ``outbound_result`` payload from the connector.

        Returns:
            A :class:`SendResult` suitable for the stream consumer and delivery
            router.
        """
        return SendResult(
            success=bool(result.get("success", False)),
            message_id=result.get("message_id"),
            error=result.get("error"),
            retryable=bool(result.get("retryable", False)),
            error_kind=result.get("error_kind"),
        )

    # ---- inbound / passthrough / interrupt bridging ----

    def _on_inbound(self, event: dict[str, Any]) -> None:
        """Bridge a connector ``inbound`` frame into the adapter's ``handle_message``.

        The connector delivers an already-normalized event dict; this method
        rebuilds the local :class:`MessageEvent` and :class:`SessionSource` and
        routes it through the standard inbound path, which is what keeps hooks,
        command routing, the two-level concurrency guard, and session key
        derivation identical to an in-process platform.

        Args:
            event: The normalized event dict.  An ``interrupt_inbound`` frame
                arrives here too, tagged with ``__interrupt__``, so this method
                is the single entry point for both message and control frames.

        Note:
            The rebuilt event always uses :attr:`MessageType.TEXT`.  Media is
            carried as ``media_urls`` / ``media_types`` lists, mirroring how
            in-process adapters represent an attachment alongside text.
        """
        # interrupt_inbound frames arrive via the same inbound handler path
        # (the transport tags them with __interrupt__).
        if event.get("__interrupt__"):
            self.on_interrupt(event.get("session_key", ""), event.get("chat_id", ""))
            return
        source_dict = event.get("source") or {}
        source = SessionSource.from_dict(source_dict) if source_dict else None
        chat_id = event.get("chat_id") or (source.chat_id if source else "")
        if source and chat_id:
            self._platform_by_chat[chat_id] = source.platform
        me = MessageEvent(
            text=event.get("text", ""),
            message_type=MessageType.TEXT,
            message_id=event.get("message_id"),
            reply_to_message_id=event.get("reply_to_message_id"),
            reply_to_text=event.get("reply_to_text"),
            media_urls=event.get("media_urls", []),
            media_types=event.get("media_types", []),
            raw_message=event.get("raw"),
            source=source,
        )
        _t = asyncio.ensure_future(self.handle_message(me))
        self._background_tasks.add(_t)
        _t.add_done_callback(self._background_tasks.discard)

    def _on_passthrough(self, forward: dict[str, Any]) -> None:
        """Bridge a ``passthrough_forward`` frame (Class-2/3 webhook).

        The connector already verified the provider signature at the edge and
        stripped the shared-identity credential before forwarding, so the
        gateway re-processes a sanitized, token-free body.  Default handling is
        to decode and route as an inbound message -- a Discord interaction, for
        example, decodes to a :class:`MessageEvent`.

        Args:
            forward: The decoded forward dict, ``{platform, botId, method,
                path, headers, bodyB64}``.  ``bodyB64`` is a base64-encoded
                provider payload that is platform-specific.

        Note:
            The base implementation logs and drops the frame, because decoding a
            provider body is platform-specific work.  A connector or subclass
            that fronts a specific platform decodes it into a
            :class:`MessageEvent` and calls ``handle_message`` itself.
        """
        logger.info("[relay] passthrough forward: platform=%s method=%s",
                    forward.get("platform"), forward.get("method"))
        # Subclasses / connectors decode platform-specific bodies; the base
        # implementation logs and drops, since decoding is platform-specific.

    def on_interrupt(self, session_key: str, chat_id: str) -> None:
        """Bridge a connector ``interrupt_inbound`` frame for a session.

        Cancels exactly that turn -- sibling sessions keep running -- by setting
        the per-session :class:`asyncio.Event` the base concurrency guard uses to
        signal "stop".

        Args:
            session_key: The session the connector wants interrupted.
            chat_id: The originating chat, included because the connector keys
                sessions by chat when a session key is not derivable.

        Note:
            Both keys are probed because the base registers active sessions by
            session key while the connector may only know the chat_id.  Setting
            an event that is not registered is harmless; the loop simply finds
            nothing to set.
        """
        logger.info("[relay] interrupt_inbound session=%s chat=%s", session_key, chat_id)
        # Setting the event is what the base guard checks for; nothing else in
        # the relay path needs to react to an interrupt.
        for key in [session_key, chat_id]:
            if key in self._active_sessions:
                self._active_sessions[key].set()
