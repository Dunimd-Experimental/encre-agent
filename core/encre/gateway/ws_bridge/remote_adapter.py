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

"""Server-side proxy presenting a remote adapter as a local platform adapter.

When a remote adapter completes the HELLO handshake with
:class:`encre.gateway.ws_bridge.server.WsBridgeServer`, the server wraps
that socket in a :class:`RemotePlatformAdapter` instance.  To the rest
of the gateway this object is indistinguishable from an in-process
:class:`encre.gateway.platforms.base.BasePlatformAdapter`, so
:class:`encre.gateway.run.GatewayRunner` and
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer` drive it
without knowing the adapter's code runs in a different process.

Data flow:

* Inbound (remote -> agent): the remote side sends a
  ``SUBMIT`` / ``SUBMIT_STREAM`` frame over the socket; the bridge
  server turns it into a :class:`encre.gateway.platforms.base.MessageEvent`
  and dispatches it through :meth:`BasePlatformAdapter.handle_message`.
* Outbound (agent -> remote): calls to :meth:`send` are translated into
  ``TEXT_DELTA`` frames written back over the same WebSocket, so the
  remote side receives the agent's reply and can deliver it to the IM
  platform.

Because the socket is already asynchronous, this adapter advertises
:class:`~encre.gateway.platforms.base.BasePlatformAdapter` async delivery
support and the runner never blocks a thread waiting for it.

Public API: :class:`RemotePlatformAdapter`.
"""

import asyncio
import json
import logging
from typing import Any

from encre.gateway.config import Platform, PlatformConfig
from encre.gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from encre.gateway.ws_bridge.protocol import GatewayMessage, GatewayOp

logger = logging.getLogger("encre.gateway.ws_bridge.remote_adapter")


class RemotePlatformAdapter(BasePlatformAdapter):
    """Wraps one remote WebSocket connection as a local BasePlatformAdapter.

    Acts as a proxy for an adapter that lives behind the WS bridge: the
    remote side sends ``SUBMIT`` / ``SUBMIT_STREAM`` frames, which the
    bridge server converts into ``MessageEvent`` dispatches on this
    object, and this object's :meth:`send` calls become ``TEXT_DELTA``
    frames returned over the socket.  The gateway therefore never needs
    to know whether an adapter is local or remote.

    Lifecycle: constructed by
    :class:`encre.gateway.ws_bridge.server.WsBridgeServer` with an
    already-open socket, immediately ``connect()``-ed (which is a no-op
    beyond setting the running flag, because the transport is owned by
    the bridge server), and ``disconnect()``-ed when the socket closes
    or the bridge shuts down.

    Invariants:

    * This object never opens or re-establishes its own transport.  The
      socket is provided and owned by the bridge server; reconnect
      responsibility belongs to
      :class:`encre.gateway.ws_bridge.client.GatewayClient` on the
      remote side.
    * ``name`` is the remote adapter's declared name, not a
      configuration-derived id, so gateway-side session keys
      (``<adapter>:<chat_id>``) keep working across reconnects of the
      same remote adapter.
    * :attr:`_seq` only ever increases within one adapter instance, so
      the server->remote frame sequence is monotonic per connection.

    Attributes:
        _ws: The live WebSocket to the remote adapter, or ``None``
            after :meth:`disconnect`.
        _remote_name: The adapter name declared in the HELLO handshake.
        _seq: Monotonic send counter for frames written by :meth:`send`.
    """

    # The gateway can deliver replies to this adapter without blocking on a
    # synchronous reply, since the WS socket is already asynchronous.
    supports_async_delivery: bool = True

    def __init__(
        self,
        config: PlatformConfig,
        platform: Platform,
        ws: Any,
        remote_name: str,
    ) -> None:
        """Build the proxy for one connected remote adapter.

        Args:
            config: The platform configuration section for this adapter,
                used by the base class for shared settings such as
                reply-delay and typing indicators.
            platform: The platform enum this adapter serves.  Remote
                adapters reuse an existing platform so session keys and
                routing match in-process adapters.
            ws: The connected WebSocket object for this adapter.  It is
                borrowed, not owned: closing it is the bridge server's
                job, though :meth:`disconnect` will also close it.
            remote_name: The adapter name the remote side declared in
                its HELLO frame.

        Raises:
            Nothing.
        """
        super().__init__(config=config, platform=platform)
        self._ws = ws
        self._remote_name = remote_name
        self._seq = 0

    @property
    def name(self) -> str:  # type: ignore[override]
        """Return the remote adapter's identifier, overriding the base.

        The base class derives its name from configuration; a remote
        adapter has no local configuration identity, so this returns the
        name the remote side declared during the HELLO handshake.  The
        override is what makes gateway-side session keys stable across
        remote reconnects.

        Returns:
            The adapter name as declared by the remote adapter.
        """
        return self._remote_name

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Mark the adapter running; the WS link is already established.

        The underlying socket is owned by the bridge server, so
        "connecting" here only flips the running flag rather than
        opening a new transport.  Returning ``True`` unconditionally is
        correct because the bridge server only constructs this object
        for a socket that already completed its handshake.

        Args:
            is_reconnect: Whether this is a reconnect attempt.  Unused,
                since reconnecting is handled by
                :class:`encre.gateway.ws_bridge.client.GatewayClient`
                on the other side.

        Returns:
            ``True`` to signal the gateway that the adapter is usable.
        """
        self._running = True
        return True

    async def disconnect(self) -> None:
        """Mark the adapter stopped and close the underlying socket.

        Args:
            None.

        Returns:
            ``None``.

        Raises:
            Nothing; socket close errors are swallowed so a connection
            that already died on the remote side can still be torn down
            cleanly.

        Note:
            :attr:`_ws` is set to ``None`` afterwards so a late
            :meth:`send` returns a retryable failure instead of writing
            to a closed transport.
        """
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send reply content back to the remote adapter over the socket.

        Serializes a ``TEXT_DELTA`` frame keyed to ``chat_id`` (used as
        the wire session id) and writes it to the socket.  The remote
        side collects those deltas and delivers the assembled text to
        its IM platform.

        Args:
            chat_id: The conversation/session id; mapped to the wire
                session id so the remote adapter can route the reply to
                the right chat.
            content: The text to deliver.
            reply_to: Optional id of the message being replied to.
                Not encoded into the current frame shape; kept in the
                signature for interface parity with local adapters.
            metadata: Optional delivery metadata.  Not encoded into the
                current frame shape for the same reason.

        Returns:
            A :class:`SendResult` indicating success, or a failure with
            ``retryable=True`` when the socket write raised, so the
            gateway can decide whether to retry.

        Raises:
            Nothing; transport errors are converted into a
            :class:`SendResult` rather than propagated, because the
            stream consumer expects a structured answer.
        """
        if not self._ws:
            return SendResult(success=False, error="WS disconnected")
        try:
            self._seq += 1
            msg = GatewayMessage.text_delta(content, session_id=chat_id)
            msg.seq = self._seq
            await self._ws.send(json.dumps(msg.to_dict()))
            return SendResult(success=True)
        except Exception as e:
            logger.error("[remote-adapter] %s send error: %s", self._remote_name, e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Return minimal chat-info metadata for a remote conversation.

        Remote adapters do not expose richer chat details (they hold no
        IM-side state), so this returns only the identifiers the gateway
        needs to label the conversation in the UI and in session keys.

        Args:
            chat_id: The conversation/session id being described.

        Returns:
            A dict with the chat id, a fixed ``"remote"`` type so the UI
            can distinguish remote conversations, and the adapter name.
        """
        return {"id": chat_id, "type": "remote", "adapter": self._remote_name}
