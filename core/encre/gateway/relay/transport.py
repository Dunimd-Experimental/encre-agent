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

"""Relay transport interface.

A :class:`RelayTransport` Protocol describing the contract between
:class:`encre.gateway.relay.adapter.RelayAdapter` and a concrete transport
(the production
:class:`encre.gateway.relay.ws_transport.WebSocketRelayTransport`, or a test
double).

The Protocol carries **normalized event and action dicts**, never raw bytes.
The connector has already verified the platform signature at the edge and
stripped tenant credentials, so the gateway always re-processes a sanitized
event and never handles a vendor payload or a vendor token.  Keeping bytes out
of this interface is what lets the gateway remain vendor-agnostic: no adapter
method here needs to know which platform is being fronted.

The Protocol is intentionally transport-agnostic.  A
:class:`encre.gateway.relay.ws_transport.WebSocketRelayTransport` implements it
over an outbound WebSocket, but an implementation could just as well sit on a
message queue or a test harness.  The adapter is written against this Protocol
only, so it never imports the WebSocket implementation -- and indeed the only
implementation coupling that exists is the import in
:mod:`encre.gateway.relay.__init__`, which does the wiring.

Public API:

- :class:`RelayTransport` -- the Protocol every implementation must satisfy.
"""

from typing import Any, Callable, Protocol, runtime_checkable

from encre.gateway.relay.descriptor import CapabilityDescriptor


@runtime_checkable
class RelayTransport(Protocol):
    """The transport surface a :class:`encre.gateway.relay.adapter.RelayAdapter` depends on.

    The gateway dials **out** to the connector's ``/relay`` endpoint, receives a
    :class:`encre.gateway.relay.descriptor.CapabilityDescriptor` at handshake,
    then exchanges normalized ``MessageEvent`` envelopes (inbound) and action
    dicts (outbound) over a per-turn bidirectional connection.

    Contract guarantees implementations must honour:

    - **Direction**: the gateway initiates; the connector never dials back.
      This is what makes a NATed or cloud-hosted gateway usable.
    - **Inbound is push, outbound is RPC**.  Inbound events arrive via the
      handler registered by :meth:`set_inbound_handler`; every outbound call is
      a request/response that returns a result dict.
    - **Reversal is terminal**.  A 4401 close after a successful handshake must
      be surfaced through :attr:`was_revoked`, not as a retryable failure, so
      the adapter records a non-retryable fatal error instead of looping.
    - **Handlers are called with dicts**, so a transport can decode a frame and
      hand it straight through without re-serialization.

    ``@runtime_checkable`` is applied so a test double can be validated with
    ``isinstance`` at runtime; the check verifies method presence only, not
    signatures.

    Thread/async safety: implementations are bound to a single event loop and
    must be safe to call concurrently from that loop, since outbound RPCs and
    inbound dispatch interleave freely.
    """

    async def connect(self) -> bool:
        """Open the transport and perform the handshake.

        Returns:
            True on success.

        Note:
            A 4401 close after a prior successful handshake must be surfaced via
            :attr:`was_revoked` (terminal), not a retryable failure.  A 4401
            before any handshake is retryable, since it may be a cold-start or
            not-yet-provisioned race.
        """
        ...

    async def disconnect(self) -> None:
        """Close the transport cleanly.

        Must fail any outstanding outbound RPC rather than leaving callers
        awaiting forever, since a cancelled task is not observable to the
        awaiting adapter.
        """
        ...

    async def handshake(self) -> CapabilityDescriptor:
        """Return the capability descriptor negotiated at connect time.

        Returns:
            The descriptor.  The adapter stores it and derives its
            ``max_message_length`` and ``splits_long_messages`` capabilities
            from it, so its values are fixed for the life of the connection.

        Raises:
            Exception: If the handshake has not completed.
        """
        ...

    def set_inbound_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """Register the callback for connector-to-gateway ``inbound`` frames.

        Args:
            handler: Receives the normalized ``MessageEvent`` dict.  Control
                frames that share the inbound path -- ``interrupt_inbound`` --
                are tagged on the same dict so one handler covers both.

        Note:
            Called synchronously and expected to complete immediately, since the
            transport's read loop invokes it inline.  Heavy work belongs in a
            spawned coroutine, which the adapter does.
        """
        ...

    def set_passthrough_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """Register the callback for ``passthrough_forward`` frames (Class-2/3
        webhooks like Discord interactions / Twilio).

        Args:
            handler: Receives the decoded forward envelope dict, shaped as
                ``{platform, botId, method, path, headers, bodyB64}``.  The
                connector has already verified the provider signature and
                stripped the shared-identity credential, so ``bodyB64`` is safe
                to hand to the gateway.
        """
        ...

    async def send_outbound(self, action: dict[str, Any], *, platform: str | None = None) -> dict[str, Any]:
        """Send an outbound action (``send`` / ``edit`` / ``typing``) and await
        the connector's ``outbound_result``.

        Args:
            action: The action payload, carrying an ``op`` discriminator plus
                the action-specific fields.
            platform: The originating platform stamp.  A single WebSocket fronts
                several platforms, so the connector's egress guard needs this
                to route the reply to the correct vendored identity.

        Returns:
            The result dict (``{success, message_id?, error?, retryable?,
            error_kind?}``).
        """
        ...

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Proxy a ``get_chat_info`` call to the connector.

        The gateway holds no vendor credential and therefore cannot query the
        platform itself for chat metadata.

        Args:
            chat_id: The chat to describe, in the connector's id space.

        Returns:
            A dict with at least ``{name, type}``.
        """
        ...

    async def send_interrupt(self, session_key: str, reason: str | None = None) -> None:
        """Egress a mid-turn ``/stop`` (``interrupt`` frame) for a session.

        Args:
            session_key: The session whose current turn should stop.
            reason: Optional human-readable reason, forwarded for connector-side
                logging.
        """
        ...

    async def go_idle(self, timeout_s: float = 10.0) -> bool:
        """Scale-to-zero primitive: emit ``going_idle`` and await
        ``going_idle_ack``.

        Args:
            timeout_s: How long to wait for the acknowledgment before giving up.

        Returns:
            True on ack, False on timeout.  A False result means the connector
            did not confirm, so the caller should keep the connection open
            rather than assuming the gateway may suspend.
        """
        ...

    async def send_follow_up(self, action: dict[str, Any], *, platform: str | None = None) -> dict[str, Any]:
        """Send a token-less ``follow_up`` action (A2 shared-identity capability).

        The gateway names the **session** it is already in plus the capability
        ``kind`` (e.g. ``discord.interaction_token``) -- never a token.  The
        connector resolves the real value from its vault, which is what keeps
        tenant credentials out of the gateway's memory entirely.

        Args:
            action: The follow-up action payload.
            platform: The originating platform stamp, as in
                :meth:`send_outbound`.

        Returns:
            The connector's result dict.
        """
        ...

    @property
    def was_revoked(self) -> bool:
        """True once the connector closed the socket with 4401 AFTER a prior
        successful handshake (a terminal revocation -- do not reconnect).

        Returning False means "not revoked", which is distinct from
        "disconnected": a transport can be disconnected for a retryable reason
        and still be eligible to reconnect.
        """
        ...
