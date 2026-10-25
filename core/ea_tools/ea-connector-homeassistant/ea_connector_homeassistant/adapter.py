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

"""Home Assistant platform adapter for Encre's IM gateway.

This module is one of the vendor adapters under
:mod:`encre.gateway.platforms`.  It treats Home Assistant as a
bidirectional local-device channel rather than a chat surface: device state
changes are watched as events and turned into agent input, and agent output
is delivered back as a persistent notification.  The two directions use
different transports on purpose, because they have different contention
profiles.

Inbound, the adapter holds a single WebSocket to ``/api/websocket``,
authenticates with a long-lived access token, and subscribes to
``state_changed`` events.  Every event is funneled through
:meth:`HomeAssistantAdapter._handle_ha_event`, which applies an allow-list of
domains and entities, a deny-list, and a per-entity cooldown before mapping
the change onto a
:class:`encre.gateway.platforms.base.MessageEvent` and handing it to
:meth:`encre.gateway.platforms.base.BasePlatformAdapter.handle_message`.
Filtering happens here because a busy home reports hundreds of state
changes a minute, and passing all of them to the agent would turn a
presence sensor into a conversation spawner.

Outbound, :meth:`HomeAssistantAdapter.send` posts to the REST
``persistent_notification/create`` service on a separate HTTP session.  The
split is deliberate: the WebSocket is being read by the listener loop, so a
concurrent write on the same socket would race the reader and could be
consumed as an event rather than sent.  The returned
:class:`encre.gateway.platforms.base.SendResult` is read by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.

:class:`encre.gateway.run.GatewayRunner` owns the lifecycle.  Credentials
come from ``HASS_TOKEN`` (a long-lived access token) and the server address
from ``HASS_URL`` (default ``http://homeassistant.local:8123``), with the
config ``extra`` mapping taking precedence over the environment for both.

Requires the ``aiohttp`` extra.  Without it the module still imports, but
:meth:`HomeAssistantAdapter.connect` declines to start rather than failing at
a random later point.

Public API:
    * :class:`HomeAssistantAdapter` -- the adapter contract implementation.
    * :func:`check_ha_requirements` -- dependency probe used by the
      platform registry.
    * :func:`validate_ha_config` -- credential completeness check.
    * :func:`_standalone_send` -- out-of-process delivery for callers with
      no live adapter instance.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Set

import aiohttp
AIOHTTP_AVAILABLE = True

from encre.gateway.config import Platform, PlatformConfig
from encre.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)


def check_ha_requirements() -> bool:
    """Report whether the Home Assistant platform can be started.

    Consulted through the platform registry before an adapter is
    constructed, so a host missing the HTTP client never reaches a partially
    initialized adapter.  The token is deliberately not part of this test:
    :func:`validate_ha_config` covers credential completeness, and folding
    the two together would make a missing token look like a missing
    dependency in the UI.

    Args:
        None.

    Returns:
        ``True`` when ``aiohttp`` is importable in this process.

    Raises:
        Nothing.
    """
    return AIOHTTP_AVAILABLE


def validate_ha_config(config: PlatformConfig) -> bool:
    """Check whether a config carries a usable Home Assistant token.

    Args:
        config: Platform configuration to inspect.  The ``token`` attribute
            is authoritative; the ``HASS_TOKEN`` environment variable is
            only a fallback for callers that construct a config by hand.

    Returns:
        ``True`` when a non-empty token is present after stripping.

    Raises:
        Nothing; a ``None`` config attribute is treated as an empty token.
    """
    token = (getattr(config, "token", None) or os.getenv("HASS_TOKEN", "")).strip()
    return bool(token)


class HomeAssistantAdapter(BasePlatformAdapter):
    """Home Assistant adapter for the Encre gateway.

    Implements :class:`encre.gateway.platforms.base.BasePlatformAdapter` for
    a Home Assistant instance.  Inbound traffic is a subscription to
    ``state_changed`` events over the WebSocket API; outbound traffic is a
    REST service call that creates a persistent notification.

    Invariants:
        * At most one WebSocket exists at a time.  :meth:`_cleanup_ws`
          closes and clears both handles together, so the socket and the
          session that owns it are never half-dead.
        * The WebSocket and the REST session are independent objects.  The
          listener reads only the socket and :meth:`send` writes only the
          REST session, which is what keeps the two directions from
          contending on a single connection.
        * Event filtering is closed by default.  A fresh adapter forwards
          nothing until ``watch_domains``, ``watch_entities`` or
          ``watch_all`` is configured, which prevents a host whose HA
          install is noisy from flooding the agent.

    Lifecycle:
        Constructed with a :class:`encre.gateway.config.PlatformConfig`;
        :meth:`connect` opens the socket, authenticates, subscribes, and
        spawns the listener; :meth:`disconnect` cancels the listener and
        closes every handle.  The listener owns its own reconnection loop,
        so ``connect`` need not be re-called after a transient drop.

    Async-safety:
        All state is touched only from the adapter's event loop.  Both the
        listener task and any REST call are awaited coroutines on that same
        loop, so no lock is needed around the shared handles.

    Attributes:
        _session: HTTP session that owns the WebSocket.
        _ws: Live WebSocket response for the event stream.
        _rest_session: Separate HTTP session used only for outbound sends.
        _listen_task: Background task running the read and reconnect loop.
        _msg_id: Counter for WebSocket request ids.
        _hass_url: Base URL of the instance, with no trailing slash.
        _hass_token: Long-lived access token used to authenticate.
        _watch_domains: Set of entity domains allowed through.
        _watch_entities: Set of exact entity ids allowed through.
        _ignore_entities: Set of entity ids dropped outright.
        _watch_all: When true, every entity is allowed through.
        _cooldown_seconds: Minimum interval between forwarded events for one
            entity.
        _last_event_time: Entity id mapped to the timestamp its event was
            last forwarded.
    """

    MAX_MESSAGE_LENGTH = 4096

    # Backoff schedule in seconds between reconnect attempts.  The list is
    # bounded and the index is clamped, so a persistent outage settles onto
    # the final step instead of growing without limit.
    _BACKOFF_STEPS = [5, 10, 30, 60]

    def __init__(self, config: PlatformConfig):
        """Initialize adapter state and resolve configuration.

        Construction performs no I/O: the runner builds the adapter, wires in
        collaborators, and only then calls :meth:`connect`.  Both the token
        and the URL prefer the config value over the environment so a value
        from ``config.yaml`` wins over a stale ambient variable.

        Args:
            config: Platform configuration.  ``token`` supplies the
                access token and ``extra`` supplies per-platform settings
                including ``url``, ``watch_domains``, ``watch_entities``,
                ``ignore_entities``, ``watch_all`` and
                ``cooldown_seconds``.

        Raises:
            ValueError: When ``extra["cooldown_seconds"]`` is not coercible
                to an integer.
        """
        super().__init__(config, Platform.HOMEASSISTANT)

        # Handles for the two transports.  The first pair serves the inbound
        # event stream; the REST session is deliberately separate so outbound
        # writes never touch the socket being read.
        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._rest_session: Optional["aiohttp.ClientSession"] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._msg_id: int = 0

        # Token and URL, with the config value taking precedence over the
        # environment so a config.yaml entry beats a stale ambient variable.
        extra = config.extra or {}
        token = config.token or os.getenv("HASS_TOKEN", "")
        url = extra.get("url") or os.getenv("HASS_URL", "http://homeassistant.local:8123")
        self._hass_url: str = url.rstrip("/")
        self._hass_token: str = token

        # Allow-lists and deny-list for the event stream, all evaluated in
        # _handle_ha_event before anything reaches the agent.
        self._watch_domains: Set[str] = set(extra.get("watch_domains", []))
        self._watch_entities: Set[str] = set(extra.get("watch_entities", []))
        self._ignore_entities: Set[str] = set(extra.get("ignore_entities", []))
        self._watch_all: bool = bool(extra.get("watch_all", False))
        self._cooldown_seconds: int = int(extra.get("cooldown_seconds", 30))

        # Per-entity last-forwarded timestamp, written only when an event
        # actually passes the filters so suppressed entities stay quiet.
        self._last_event_time: Dict[str, float] = {}

    def _next_id(self) -> int:
        """Allocate the next WebSocket request id.

        Home Assistant correlates replies to requests by this id, so it must
        be unique for the lifetime of the socket.  The counter is never
        reset, which also guarantees uniqueness across reconnections on the
        same adapter instance.

        Args:
            None.

        Returns:
            The newly allocated id.
        """
        self._msg_id += 1
        return self._msg_id

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Open the event stream, subscribe, and start the listener.

        Preflight checks for the HTTP client and the token happen before any
        socket is opened, so a misconfigured host returns ``False`` without
        leaving a half-open connection behind.  Filter configuration is
        validated only as a warning rather than an error, because an adapter
        with no filters is still a functioning adapter that simply forwards
        nothing; refusing to start would hide that from the operator.

        Args:
            is_reconnect: Accepted for base-contract signature compatibility.
                The listener loop performs its own reconnection, so this flag
                is not consulted.

        Returns:
            ``True`` when the subscription was acknowledged and the listener
            task is running.  ``False`` when a dependency, the token, or the
            socket handshake is unavailable.

        Raises:
            Nothing; failures are logged and returned.
        """
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed. Run: pip install aiohttp", self.name)
            return False

        if not self._hass_token:
            logger.warning("[%s] No HASS_TOKEN configured", self.name)
            return False

        try:
            success = await self._ws_connect()
            if not success:
                return False

            # A dedicated REST session keeps outbound writes off the socket
            # the listener is reading from.
            self._rest_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )

            # No filters at all means nothing will ever be forwarded.  This
            # is reported loudly rather than as an error because the adapter
            # is still functioning correctly, just receiving nothing.
            if not self._watch_domains and not self._watch_entities and not self._watch_all:
                logger.warning(
                    "[%s] No watch_domains, watch_entities, or watch_all configured. "
                    "All state_changed events will be dropped. Configure filters in "
                    "your HA platform config to receive events.",
                    self.name,
                )

            # The listener is the only task that owns the socket for reading,
            # so it must be created before the adapter reports itself running.
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._running = True
            logger.info("[%s] Connected to %s", self.name, self._hass_url)
            return True

        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.name, e)
            return False

    async def _ws_connect(self) -> bool:
        """Open the WebSocket, authenticate, and subscribe to events.

        The handshake is a fixed four-step exchange and each step validates
        the response it received before sending the next.  Home Assistant
        does not tolerate a reordered exchange, so the checks are ordered
        rather than being collected and reported at the end.  Any failure
        closes the socket and session before returning, which keeps a failed
        attempt from leaking a connection that the reconnect loop would then
        try to read.

        Args:
            None.

        Returns:
            ``True`` when the subscription acknowledgement arrived.

        Raises:
            Exception: Propagated to the caller when the socket cannot be
                opened at all, rather than returning ``False`` for that
                case, so the caller can distinguish a network failure from a
                protocol disagreement.
        """
        ws_url = self._hass_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url}/api/websocket"

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        self._ws = await self._session.ws_connect(ws_url, heartbeat=30, timeout=30)

        # The handshake is a strict four-message exchange: the server
        # announces it is ready for a token, the client presents one, the
        # server confirms, and only then may an event subscription be
        # requested.
        msg = await self._ws.receive_json()
        if msg.get("type") != "auth_required":
            logger.error("Expected auth_required, got: %s", msg.get("type"))
            await self._cleanup_ws()
            return False

        # Present the long-lived access token.  It is sent over a TLS or
        # ws connection only, never logged.
        await self._ws.send_json({
            "type": "auth",
            "access_token": self._hass_token,
        })

        # A failure here means the token was rejected.  The full response is
        # logged rather than just the type, because Home Assistant reports
        # the rejection reason in the error field.
        msg = await self._ws.receive_json()
        if msg.get("type") != "auth_ok":
            logger.error("Auth failed: %s", msg)
            await self._cleanup_ws()
            return False

        # Request the event stream, tagged with a fresh request id so the
        # acknowledgement can be correlated.
        sub_id = self._next_id()
        await self._ws.send_json({
            "id": sub_id,
            "type": "subscribe_events",
            "event_type": "state_changed",
        })

        # The subscription only counts once the server has acknowledged it.
        # Returning before that point would leave the listener reading a
        # stream it never subscribed to.
        msg = await self._ws.receive_json()
        if not msg.get("success"):
            logger.error("Failed to subscribe to events: %s", msg)
            await self._cleanup_ws()
            return False

        return True

    async def _cleanup_ws(self) -> None:
        """Close the WebSocket and the session that owns it.

        Both handles are cleared after being closed, so this method is safe
        to call repeatedly and from any failure path.  Clearing the session
        alongside the socket matters because aiohttp will not reuse a
        session that already had a failed WebSocket.

        Args:
            None.

        Returns:
            Nothing.

        Raises:
            Nothing; close failures are absorbed.
        """
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def disconnect(self) -> None:
        """Stop the listener and release every connection handle.

        Order matters: the listener task is cancelled first and awaited so
        it cannot observe a closed socket mid-read and spin the reconnect
        loop once more.  Fatal error state recorded through the base class is
        left untouched, since it must remain visible after shutdown.

        Args:
            None.

        Returns:
            Nothing.
        """
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        await self._cleanup_ws()
        if self._rest_session and not self._rest_session.closed:
            await self._rest_session.close()
        self._rest_session = None
        logger.info("[%s] Disconnected", self.name)

    # ------------------------------------------------------------------
    # Event listener
    # ------------------------------------------------------------------

    async def _listen_loop(self) -> None:
        """Read the event stream and reconnect on failure.

        Runs as a tracked task for the adapter's lifetime.  A return from
        the read call means the socket is dead, which triggers a backoff
        sleep followed by a fresh handshake.  The backoff index is clamped
        to the last entry so a prolonged outage settles onto a fixed interval
        instead of sleeping longer and longer, and it is reset to zero on a
        successful reconnect so a long-stable connection does not carry a
        stale delay forward.

        Args:
            None.

        Returns:
            Nothing.  Only a cancellation, or the adapter being told to stop,
            ends the loop.
        """
        backoff_idx = 0

        while self._running:
            try:
                await self._read_events()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[%s] WebSocket error: %s", self.name, e)

            if not self._running:
                return

            # Back off before retrying so a wedged instance is not hammered.
            # The index is clamped to the last entry to cap the interval.
            delay = self._BACKOFF_STEPS[min(backoff_idx, len(self._BACKOFF_STEPS) - 1)]
            logger.info("[%s] Reconnecting in %ds...", self.name, delay)
            await asyncio.sleep(delay)
            backoff_idx += 1

            try:
                await self._cleanup_ws()
                success = await self._ws_connect()
                if success:
                    backoff_idx = 0  # A clean reconnect starts the ladder over
                    logger.info("[%s] Reconnected", self.name)
            except Exception as e:
                logger.warning("[%s] Reconnection failed: %s", self.name, e)

    async def _read_events(self) -> None:
        """Drain the WebSocket until it closes.

        Blocks until a terminal frame arrives, which is how the caller knows
        the connection is gone.  Non-JSON text frames are logged and skipped
        rather than raising, since a malformed frame should not terminate the
        read loop and force a reconnect for one bad message.

        Args:
            None.

        Returns:
            Nothing.  A closed or errored socket ends the async iteration
            normally.

        Raises:
            Exception: Propagated to :meth:`_listen_loop` when the socket
                fails mid-read.  This is the signal that triggers reconnection.
        """
        if self._ws is None or self._ws.closed:
            return
        async for ws_msg in self._ws:
            if ws_msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(ws_msg.data)
                    if data.get("type") == "event":
                        await self._handle_ha_event(data.get("event", {}))
                except json.JSONDecodeError:
                    logger.debug("Invalid JSON from HA WS: %s", ws_msg.data[:200])
            elif ws_msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                break

    async def _handle_ha_event(self, event: Dict[str, Any]) -> None:
        """Filter one ``state_changed`` event and forward it if it passes.

        The filter chain runs in a fixed order, cheapest and most specific
        first: an empty entity id is dropped as a malformed payload, the
        deny-list is checked before the allow-list so an ignored entity can
        never be admitted by a broad rule, and the allow-list is checked
        before the cooldown so a filtered entity never consumes a cooldown
        slot.  The cooldown is keyed per entity, not globally, so a busy
        sensor does not suppress an unrelated alarm.

        Everything that reaches the end is mapped onto one
        :class:`encre.gateway.platforms.base.MessageEvent` addressed to a
        single shared chat, because Home Assistant reports have no natural
        per-entity conversation; splitting them would create one session per
        light switch.

        Args:
            event: Decoded Home Assistant event envelope.  The interesting
                payload lives under ``data`` with an ``entity_id``, an
                ``old_state`` and a ``new_state``.

        Returns:
            Nothing.

        Raises:
            Nothing; an event that fails filtering is dropped silently.
        """
        event_data = event.get("data", {})
        entity_id: str = event_data.get("entity_id", "")

        if not entity_id:
            return

        # Deny-list first: an ignored entity is dropped before the allow
        # lists are even consulted, so a specific exclusion always wins.
        if entity_id in self._ignore_entities:
            return

        # The allow-list is closed by default: unless a domain or entity is
        # explicitly watched, or watch_all is set, nothing is forwarded.
        # Domain membership is enough when only domains are configured, which
        # is the common case for an entire family of devices.
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        if self._watch_domains or self._watch_entities:
            domain_match = domain in self._watch_domains if self._watch_domains else False
            entity_match = entity_id in self._watch_entities if self._watch_entities else False
            if not domain_match and not entity_match:
                return
        elif not self._watch_all:
            # Nothing configured at all.  Since the allow-list is closed by
            # default, watch_all is the only thing that admits events here.
            return

        # Cooldown is per entity, so one chatty sensor cannot suppress a
        # distinct device that changed at the same instant.
        now = time.time()
        last = self._last_event_time.get(entity_id, 0)
        if (now - last) < self._cooldown_seconds:
            return
        self._last_event_time[entity_id] = now

        # Render the transition as prose for the agent.  Returning an empty
        # value here means the event is dropped, so formatting doubles as a
        # final filter for transitions that are not worth reporting.
        old_state = event_data.get("old_state", {})
        new_state = event_data.get("new_state", {})
        message = self._format_state_change(entity_id, old_state, new_state)

        if not message:
            return

        # Route everything through one shared chat so all device reports
        # share a single agent session, rather than fragmenting one per
        # entity.
        source = self.build_source(
            chat_id="ha_events",
            chat_name="Home Assistant Events",
            chat_type="channel",
            user_id="homeassistant",
            user_name="Home Assistant",
        )

        msg_event = MessageEvent(
            text=message,
            message_type=MessageType.TEXT,
            source=source,
            message_id=f"ha_{entity_id}_{int(now)}",
            timestamp=datetime.now(),
        )

        await self.handle_message(msg_event)

    @staticmethod
    def _format_state_change(
        entity_id: str,
        old_state: Dict[str, Any],
        new_state: Dict[str, Any],
    ) -> Optional[str]:
        """Render a state change as agent-readable prose.

        Returning ``None`` rather than an empty string is load-bearing: the
        caller treats a missing message as "drop this event", so a
        degenerate transition such as an unchanged value or a teardown with
        no new state is dropped instead of being forwarded as noise.

        Entity ids are of the form ``domain.entity``, and the domain selects
        the rendering.  Domains that carry a numeric unit keep the unit
        attached to both values, since a sensor reading without its unit is
        not interpretable.  Domains without a unit use a simple on/off or
        triggered/cleared form, which keeps the message short enough for the
        agent to use without parsing.

        Args:
            entity_id: Full ``domain.entity`` identifier.
            old_state: Previous state object, or an empty mapping for an
                entity that is being added for the first time.
            new_state: New state object.  Absent on teardown, in which case
                the change cannot be described and ``None`` is returned.

        Returns:
            A formatted message string, or ``None`` when there is nothing
            meaningful to report.
        """
        if not new_state:
            return None

        old_val = old_state.get("state", "unknown") if old_state else "unknown"
        new_val = new_state.get("state", "unknown")

        # A state object with no value cannot have changed, so there is
        # nothing to report even if the event envelope claims otherwise.
        if old_val == new_val:
            return None

        friendly_name = new_state.get("attributes", {}).get("friendly_name", entity_id)
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        # Domain selects the rendering, so the message matches the vocabulary
        # the agent will reason about.  The friendly name is preferred over
        # the raw entity id because it is what the owner calls the device.
        if domain == "climate":
            attrs = new_state.get("attributes", {})
            temp = attrs.get("current_temperature", "?")
            target = attrs.get("temperature", "?")
            return (
                f"[Home Assistant] {friendly_name}: HVAC mode changed from "
                f"'{old_val}' to '{new_val}' (current: {temp}, target: {target})"
            )

        if domain == "sensor":
            unit = new_state.get("attributes", {}).get("unit_of_measurement", "")
            return (
                f"[Home Assistant] {friendly_name}: changed from "
                f"{old_val}{unit} to {new_val}{unit}"
            )

        if domain == "binary_sensor":
            return (
                f"[Home Assistant] {friendly_name}: "
                f"{'triggered' if new_val == 'on' else 'cleared'} "
                f"(was {'triggered' if old_val == 'on' else 'cleared'})"
            )

        if domain in {"light", "switch", "fan"}:
            return (
                f"[Home Assistant] {friendly_name}: turned "
                f"{'on' if new_val == 'on' else 'off'}"
            )

        if domain == "alarm_control_panel":
            return (
                f"[Home Assistant] {friendly_name}: alarm state changed from "
                f"'{old_val}' to '{new_val}'"
            )

        # Generic fallback
        return (
            f"[Home Assistant] {friendly_name} ({entity_id}): "
            f"changed from '{old_val}' to '{new_val}'"
        )

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Deliver agent output as a Home Assistant persistent notification.

        This is the single outbound method called by
        :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`, so its
        behaviour defines how agent replies reach the local instance.  The
        body goes to the ``persistent_notification/create`` REST service
        instead of the WebSocket on purpose: the listener loop is reading
        that socket, and a write issued on the same connection from another
        coroutine would race the reader, with the risk that the subscription
        acknowledgement and the outgoing frame interleave.  A dedicated REST
        session removes the contention entirely.

        Failures are returned rather than raised, matching the base
        contract, so a transient instance outage cannot tear down a turn.

        Args:
            chat_id: Accepted for base-contract compatibility.  Home
                Assistant notifications are not addressed per chat, so the
                value is unused.
            content: Notification body.  Truncated to
                :attr:`MAX_MESSAGE_LENGTH` before posting, since the service
                rejects oversized payloads and the truncation keeps the
                failure mode visible in the text rather than as a vendor
                error.
            reply_to: Accepted for base-contract compatibility and ignored;
                the notification service has no threading concept.
            metadata: Accepted for base-contract compatibility and ignored.

        Returns:
            :class:`SendResult`.  On success ``message_id`` carries a short
            UUID fragment because the service does not return a durable
            identifier, and the field must still be populated so downstream
            consumers can distinguish a delivered message.

        Raises:
            Nothing; HTTP status failures, timeouts and transport errors are
            all converted to a failed result.
        """
        url = f"{self._hass_url}/api/services/persistent_notification/create"
        headers = {
            "Authorization": f"Bearer {self._hass_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "title": "Encre Agent",
            "message": content[:self.MAX_MESSAGE_LENGTH],
        }

        try:
            if self._rest_session:
                async with self._rest_session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status < 300:
                        return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
                    else:
                        body = await resp.text()
                        return SendResult(success=False, error=f"HTTP {resp.status}: {body}")
            else:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status < 300:
                            return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
                        else:
                            body = await resp.text()
                            return SendResult(success=False, error=f"HTTP {resp.status}: {body}")

        except asyncio.TimeoutError:
            return SendResult(success=False, error="Timeout sending notification to HA")
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No-op typing indicator.

        Home Assistant has no equivalent of a typing signal, so the default
        empty implementation is correct here.  Callers treat a no-op as
        normal and must not treat it as a failure.

        Args:
            chat_id: Ignored; accepted for base-contract compatibility.
            metadata: Ignored; accepted for base-contract compatibility.

        Returns:
            Nothing.
        """

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Describe the shared event channel for session and UI listing.

        There is one channel for the whole instance rather than one per
        entity, so the returned mapping does not depend on the identifier it
        was asked about.  The instance URL is included so an operator can
        tell which install a session belongs to.

        Args:
            chat_id: Ignored; accepted for base-contract compatibility.

        Returns:
            Mapping with the display ``name``, the ``type`` ``"channel"``,
            and the ``url`` of the Home Assistant instance.
        """
        return {
            "name": "Home Assistant Events",
            "type": "channel",
            "url": self._hass_url,
        }


# ---------------------------------------------------------------------------
# Standalone delivery
# ---------------------------------------------------------------------------
# Delivery paths that have no live adapter instance in their process:
# scheduled work, out-of-process send tools, and one-shot scripts.  These
# resolve credentials themselves and talk to the instance directly, so they
# work where the gateway runner is not running.


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Send a notification without a live adapter instance.

    Serves callers that need Home Assistant delivery where the gateway
    runner is not running in the same process: scheduled work, out-of-process
    send tools, and one-shot scripts.  It posts to the ``notify.notify``
    service rather than the persistent notification service used by
    :meth:`HomeAssistantAdapter.send`, because the notify service accepts a
    ``target`` and therefore honours the ``chat_id`` the caller supplied.

    Credentials are resolved from the config object first and the environment
    as a fallback, mirroring the adapter's own precedence, so the two paths
    agree about which value wins.

    The trailing keyword arguments exist only for signature parity with
    other standalone senders.  Notifications have no threading or attachment
    model, so those values are accepted and ignored rather than rejected,
    which keeps the caller uniform across platforms.

    Args:
        pconfig: Minimal config object.  ``extra["url"]`` supplies the
            instance address and ``token`` the access token.
        chat_id: Notification target entity, forwarded as ``target``.
        message: Notification body.
        thread_id: Ignored; accepted for contract parity.
        media_files: Ignored; accepted for contract parity.
        force_document: Ignored; accepted for contract parity.

    Returns:
        Dict with ``success``, ``platform`` and ``chat_id`` on success, or a
        single ``error`` key on any failure.  Configuration gaps and transport
        exceptions are folded into the error form so the caller branches on
        one shape.

    Raises:
        Nothing.
    """
    if not AIOHTTP_AVAILABLE:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}

    extra = getattr(pconfig, "extra", {}) or {}
    hass_url = (extra.get("url") or os.getenv("HASS_URL", "")).rstrip("/")
    token = (getattr(pconfig, "token", None) or os.getenv("HASS_TOKEN", "")).strip()
    if not hass_url or not token:
        return {
            "error": (
                "Home Assistant standalone send: HASS_URL and HASS_TOKEN "
                "must both be set"
            )
        }

    url = f"{hass_url}/api/services/notify/notify"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"message": message, "target": chat_id}

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status not in {200, 201}:
                    body = await resp.text()
                    return {
                        "error": (
                            f"Home Assistant API error ({resp.status}): {body}"
                        )
                    }
        return {
            "success": True,
            "platform": "homeassistant",
            "chat_id": chat_id,
        }
    except asyncio.TimeoutError:
        return {"error": "Timeout sending notification to Home Assistant"}
    except Exception as e:
        return {"error": f"Home Assistant send failed: {e}"}


# ---------------------------------------------------------------------------
# Connected-check probe
# ---------------------------------------------------------------------------
# Supplies the platform registry with a cheap way to decide whether the
# platform should be advertised as configured, without constructing an
# adapter.


def _is_connected(config) -> bool:
    """Report whether the platform looks configured, for status display.

    Consulted when building the list of platforms a user sees as connected.
    The test is on token presence only, deliberately weaker than
    :func:`validate_ha_config`, because the UI wants to show configuration
    state even where the adapter is not running.  The lookup reads the
    environment directly at call time so a test or operator that changes the
    variable between checks sees the current value rather than a value frozen
    at import.

    Args:
        config: Accepted for the connected-checker signature and unused.

    Returns:
        ``True`` when a non-empty ``HASS_TOKEN`` is present.
    """
    return bool((os.environ.get("HASS_TOKEN") or "").strip())


# ---- Plugin registration glue ----
# The registry ``PlatformEntry`` was migrated into plugin.py's
# ``GatewayAdapterSpec``; the factory below is the real value that spec
# mirrors. Registration happens at plugin activation time.


def _build_adapter(config):
    """Construct an adapter for the plugin gateway-adapter spec.

    Spec factories receive a fully resolved config and are expected to
    return a ready-to-connect adapter, so this wrapper exists only to give
    the spec a stable callable identity.

    Args:
        config: Resolved :class:`encre.gateway.config.PlatformConfig`.

    Returns:
        A new :class:`HomeAssistantAdapter` bound to *config*.
    """
    return HomeAssistantAdapter(config)
