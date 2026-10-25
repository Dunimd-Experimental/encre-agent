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

"""
ntfy platform adapter for Encre's IM gateway layer.

Every IM vendor in Encre owns one module under
``core/encre/gateway/platforms/<vendor>/adapter.py``. Each module implements
:class:`encre.gateway.platforms.base.BasePlatformAdapter`, translating vendor
traffic into the shared inbound
:class:`~encre.gateway.platforms.base.MessageEvent` model and the shared
outbound :class:`~encre.gateway.platforms.base.SendResult` model back into
vendor traffic. :class:`encre.gateway.run.GatewayRunner` owns the adapter
lifecycle (``connect`` / ``disconnect``) and session authorization;
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer` consumes the
``SendResult`` values returned by :meth:`NtfyAdapter.send`.

This adapter maps an ntfy topic onto a bidirectional channel using no vendor
SDK -- only ``httpx``, which is already an Encre dependency:

* inbound: a long-lived ``GET {server}/{topic}/json?poll=false`` streaming
  connection that emits one JSON object per line. A background task consumes
  the stream, decodes each line, drops duplicates and self-echoes, and routes
  survivors through :meth:`NtfyAdapter.handle_message`.
* outbound: ``POST {server}/{publish_topic}`` with a UTF-8 text body.

The module ships as the ``ea-connector-ntfy`` connector plugin; the adapter
spec registered by ``ea_connector_ntfy.plugin`` makes ``encre gateway``
discover it as the ``ntfy`` platform.
``_standalone_send`` is the out-of-process path used by the cron scheduler and
the send-message tool when no live :class:`GatewayRunner` exists in this
process.

Security model: ntfy has no authenticated user identity, and the publisher-set
``title`` field is attacker-controlled. Authorization therefore never reads
``title``; each topic is a single trusted channel and ``user_id`` is fixed to
the topic name. A real trust boundary requires a topic read token.

Configuration comes from ``platforms.ntfy.extra`` in ``config.yaml`` or from
``NTFY_*`` environment variables. The constructor reads ``config.extra`` first
and falls back to env; :func:`_env_enablement` seeds
``PlatformConfig.extra`` from env during config load. Keys: ``server``
(default ``https://ntfy.sh``), ``topic`` (required), ``publish_topic``
(defaults to ``topic``), ``token``, ``markdown``, plus
``NTFY_ALLOWED_USERS``, ``NTFY_ALLOW_ALL_USERS``, ``NTFY_HOME_CHANNEL`` and
``NTFY_HOME_CHANNEL_NAME``, which the gateway core interprets as allowlist and
home-channel hints.

Public API
----------
``NtfyAdapter``            adapter driven by ``GatewayRunner``
``check_requirements``     installability and minimal-config probe
``validate_config``        config-shape probe
``is_connected``           connected-platforms probe
``_env_enablement``        env-to-``PlatformConfig.extra`` seeding hook
``_standalone_send``       out-of-process publish for cron and send tools
``_build_adapter``         registry factory
"""


import asyncio

import json

import logging

import os

import time

import uuid

from datetime import datetime, timezone

from typing import Any, Dict, List, Optional


import httpx
HTTPX_AVAILABLE = True


from encre.gateway.config import Platform, PlatformConfig

from encre.gateway.platforms.base import (

    BasePlatformAdapter,

    MessageEvent,

    MessageType,

    SendResult,

)


logger = logging.getLogger(__name__)


class _FatalStreamError(Exception):

    """Terminal signal that the inbound stream cannot be fixed by retrying.

    Raised by :meth:`NtfyAdapter._consume_stream` when ntfy rejects the
    subscription itself rather than a transient network condition: HTTP 401
    (missing or bad token) and HTTP 404 (topic does not exist). The
    reconnection loop in :meth:`NtfyAdapter._run_stream` catches this type
    specifically and shuts the adapter down instead of walking the backoff
    table, because retrying a bad credential or a nonexistent topic can never
    succeed and would otherwise spin the gateway for the process lifetime.

    Invariant: only :class:`_FatalStreamError` bypasses the retry ladder; every
    other exception from the stream is treated as transient.

    Args:
        *args: Forwarded to :class:`Exception`; carries a short human-readable
            reason such as ``"401 Unauthorized"`` for log correlation.
    """


DEFAULT_SERVER = "https://ntfy.sh"

MAX_MESSAGE_LENGTH = 4096  # ntfy hard cap on a published message body

DEDUP_WINDOW_SECONDS = 300

DEDUP_MAX_SIZE = 1000

RECONNECT_BACKOFF = [2, 5, 10, 30, 60]

STREAM_TIMEOUT_SECONDS = 90  # ntfy keepalive default is 55s; leave headroom

_ECHO_TAG = "encre-agent"  # tag stamped on outbound messages for echo suppression


def _build_auth_header(token: str) -> Dict[str, str]:
    """Build the ``Authorization`` header for an ntfy request from a raw token.

    Shared by :meth:`NtfyAdapter._auth_headers` and :func:`_standalone_send`
    so the in-process and out-of-process paths cannot drift apart in auth
    shape or whitespace handling.

    Tokens are stripped of surrounding whitespace because pasted values often
    carry a trailing newline; an embedded newline would corrupt the header
    (``Authorization: Bearer foo\\n``) and ntfy rejects it. A token containing
    a colon is ``user:pass`` and becomes Basic auth with the *raw*
    ``user:pass`` string base64-encoded, which is what a standards-conformant
    Basic client produces; everything else is treated as an opaque Bearer
    secret. An empty or whitespace-only token yields ``{}`` so the caller can
    merge without an extra conditional.

    Args:
        token: Raw credential from ``NTFY_TOKEN`` or ``config.extra["token"]``.

    Returns:
        ``{"Authorization": "Bearer <token>"}`` for plain tokens,
        ``{"Authorization": "Basic <base64(user:pass)>"}`` for colon tokens,
        or ``{}`` when no usable token is present.
    """

    if not token:

        return {}

    token = token.strip()

    if not token:

        return {}
    # A colon marks "user:pass"; anything else is an opaque Bearer secret.

    if ":" in token:
        # Imported here rather than at module scope so the adapter import
        # cannot fail on a runtime that lacks the module.

        import base64

        encoded = base64.b64encode(token.encode()).decode()

        return {"Authorization": f"Basic {encoded}"}

    return {"Authorization": f"Bearer {token}"}


def _truncate_body(message: str, *, context: str) -> bytes:
    """Enforce the ntfy body cap and warn when content is dropped.

    ntfy rejects publishes over 4096 characters, so overshoot must be clipped
    before the request goes out; failing the send instead would surface as a
    generic HTTP error and give the operator no hint about the cause.

    Args:
        message: Unbounded plaintext produced upstream by the agent.
        context: Label embedded in the warning so adapter and standalone
            truncations are distinguishable in the log.

    Returns:
        The clipped message UTF-8 encoded, ready to use as an HTTP body.

    Note:
        Truncation is always safe here; ntfy is a pure notification channel, so
        no framing or protocol trailer can be broken by cutting the text.
    """

    if len(message) > MAX_MESSAGE_LENGTH:

        logger.warning(

            "%s: truncating message from %d to %d chars (ntfy limit)",

            context, len(message), MAX_MESSAGE_LENGTH,

        )

    return message[:MAX_MESSAGE_LENGTH].encode("utf-8")


def check_requirements() -> bool:
    """Decide whether this platform is installable and minimally usable.

    Used by the platform registry to filter ``encre gateway`` availability
    lists without constructing an adapter. ``NTFY_TOPIC`` is read straight
    from the environment so this probe avoids the cost of
    ``load_gateway_config()`` (which also mutates ``os.environ``) on every
    pre-flight check.

    Returns:
        ``True`` when ``httpx`` is importable and a subscribe topic is set.
    """

    if not HTTPX_AVAILABLE:

        return False

    topic = os.getenv("NTFY_TOPIC", "").strip()

    return bool(topic)


def validate_config(config) -> bool:
    """Validate that a loaded ``PlatformConfig`` can drive an ntfy adapter.

    Unlike :func:`check_requirements` this sees the resolved config object, so
    it honours ``config.extra["topic"]`` as an alternative to the environment
    variable. Config wins when both are present, matching the constructor.

    Args:
        config: A :class:`encre.gateway.config.PlatformConfig` for ntfy.

    Returns:
        ``True`` when either source supplies a non-empty subscribe topic.
    """

    extra = getattr(config, "extra", {}) or {}

    topic = extra.get("topic") or os.getenv("NTFY_TOPIC", "")

    return bool(topic)


def is_connected(config) -> bool:
    """Report whether ntfy is configured for the connected-platforms view.

    This is a *configuration* probe, not a liveness probe: it never opens a
    socket and returns ``True`` as soon as a topic is known, so ``encre
    status`` can display the platform before :meth:`NtfyAdapter.connect` runs.
    The environment variable takes precedence because
    :func:`_env_enablement` only seeds ``extra`` when env is absent.

    Args:
        config: A :class:`encre.gateway.config.PlatformConfig` for ntfy.

    Returns:
        ``True`` when ``NTFY_TOPIC`` or ``config.extra["topic"]`` is set.
    """

    extra = getattr(config, "extra", {}) or {}

    topic = os.getenv("NTFY_TOPIC") or extra.get("topic", "")

    return bool(topic)


class NtfyAdapter(BasePlatformAdapter):

    """ntfy adapter bridging a topic into Encre's gateway session pipeline.

    Inbound: a background task holds one streaming
    ``GET {server}/{topic}/json?poll=false`` connection and dispatches each
    decoded message event. Outbound: :meth:`send` POSTs UTF-8 text to the
    publish topic. A single :class:`httpx.AsyncClient` is shared by both
    directions and is created in :meth:`connect` / closed in
    :meth:`disconnect`.

    Attributes:
        _server: Base URL of the ntfy host, trailing slash stripped.
        _topic: Subscribe topic; ``user_id`` for every inbound message.
        _publish_topic: Reply topic; falls back to ``_topic`` when unset.
        _token: Optional Bearer or ``user:pass`` credential.
        _stream_task: Live reconnection task, ``None`` when disconnected.
        _http_client: Shared :class:`httpx.AsyncClient` or ``None``.
        _seen_messages: ``message_id -> monotonic timestamp`` dedup table,
            bounded by :data:`DEDUP_MAX_SIZE` and :data:`DEDUP_WINDOW_SECONDS`.

    Invariant:
        There is at most one live subscription at a time. :meth:`disconnect`
        cancels the task and waits for it, so a re-``connect`` cannot leave a
        second stream running against the same topic.

    Thread/async safety:
        All state is touched from the single event loop thread that
        :class:`encre.gateway.run.GatewayRunner` schedules this adapter on.
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):

        """Bind the adapter to its config without opening any connection.

        Args:
            config: Resolved :class:`encre.gateway.config.PlatformConfig` for
                the ``ntfy`` platform, typically with an ``extra`` mapping.

        Note:
            ``config.extra`` is consulted before the environment so an explicit
            ``config.yaml`` entry can pin a value the operator then changes in
            env. ``_topic`` may remain empty at construction time;
            :meth:`connect` treats that as a configuration failure rather than
            crashing the constructor.
        """

        platform = Platform("ntfy")

        super().__init__(config=config, platform=platform)

        extra = config.extra or {}

        self._server: str = (

            extra.get("server")

            or os.getenv("NTFY_SERVER_URL", DEFAULT_SERVER)

        ).rstrip("/")

        self._topic: str = extra.get("topic") or os.getenv("NTFY_TOPIC", "")

        self._publish_topic: str = (

            extra.get("publish_topic")

            or os.getenv("NTFY_PUBLISH_TOPIC", "")

            or self._topic

        )

        self._token: str = extra.get("token") or os.getenv("NTFY_TOKEN", "")

        self._stream_task: Optional[asyncio.Task] = None

        self._http_client: Optional["httpx.AsyncClient"] = None

        # Message deduplication: msg_id -> insertion timestamp

        self._seen_messages: Dict[str, float] = {}

    # -- Connection lifecycle -----------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:

        """Start the ntfy subscription and report readiness.

        Creates the shared HTTP client and spawns the background reconnection
        task; the method returns as soon as the task is scheduled, not when
        the first stream frame arrives, so the gateway runner can move on
        while the adapter establishes transport asynchronously.

        Args:
            is_reconnect: Unused by ntfy; kept for interface parity with
                adapters that need to distinguish cold starts from retries.

        Returns:
            ``True`` when the task is scheduled; ``False`` when ``httpx`` is
            missing or no subscribe topic is configured. A ``False`` result
            leaves no client or task behind.

        Raises:
            Nothing. Construction failures are logged and converted to
            ``False`` so a broken platform cannot abort gateway startup.
        """

        if not HTTPX_AVAILABLE:

            logger.warning("[%s] httpx not installed. Run: pip install httpx", self.name)

            return False

        if not self._topic:

            logger.warning("[%s] NTFY_TOPIC not configured", self.name)

            return False

        try:
            # timeout=None is deliberate: the stream reads indefinitely and any
            # client-level timeout would cancel a healthy long-lived session.

            self._http_client = httpx.AsyncClient(timeout=None)

            self._stream_task = asyncio.create_task(self._run_stream())

            self._mark_connected()

            logger.info("[%s] Connected 鈥?subscribing to %s/%s", self.name, self._server, self._topic)

            return True

        except Exception as e:

            logger.error("[%s] Failed to connect: %s", self.name, e)

            return False

    async def _run_stream(self) -> None:

        """Background loop that keeps the ntfy subscription alive.

        Re-opens :meth:`_consume_stream` after every failure, sleeping the
        :data:`RECONNECT_BACKOFF` ladder between attempts. The index advances
        one step per failure and resets to zero when a stream survives at
        least 60 seconds, so a healthy connection never pays for a stale
        backoff while a flapping link escalates to the 60 second ceiling.

        Terminates only on :class:`asyncio.CancelledError` (from
        :meth:`disconnect`) or :class:`_FatalStreamError` (HTTP 401 / 404),
        which also clears ``_running`` so the loop cannot restart itself.

        Returns:
            ``None``; the task's exit is what unblocks
            :meth:`disconnect`'s ``await`` on it.
        """

        backoff_idx = 0

        stream_start: float = 0.0

        url = f"{self._server}/{self._topic}/json"

        headers = self._auth_headers()

        while self._running:

            try:

                logger.debug("[%s] Opening stream to %s", self.name, url)

                stream_start = time.monotonic()

                await self._consume_stream(url, headers)

            except asyncio.CancelledError:

                return

            except _FatalStreamError:

                self._running = False

                return

            except Exception as e:

                if not self._running:

                    return

                logger.warning("[%s] Stream error: %s", self.name, e)

            if not self._running:

                return

            # A stream that outlives 60s proves the link is healthy, so the
            # backoff ladder is allowed to start over from the shortest delay.

            if time.monotonic() - stream_start >= 60.0:

                backoff_idx = 0

            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]

            logger.info("[%s] Reconnecting in %ds...", self.name, delay)

            await asyncio.sleep(delay)

            backoff_idx += 1

    async def _consume_stream(self, url: str, headers: Dict[str, str]) -> None:

        """Hold one streaming subscription and dispatch decoded messages.

        ``poll=false`` makes ntfy keep the response open and emit periodic
        ``event: keepalive`` lines, which is what keeps the connection
        useful to :class:`httpx`'s read timeout:
        :data:`STREAM_TIMEOUT_SECONDS` is only a dead-stream detector, not a
        session cap. Non-2xx status codes that mean the subscription itself is
        wrong (401, 404) are promoted to :class:`_FatalStreamError` and
        reported through :meth:`~BasePlatformAdapter._set_fatal_error` so the
        gateway runner surfaces a stable, non-retryable failure instead of
        reconnecting forever.

        Args:
            url: ``{server}/{topic}/json`` endpoint to subscribe to.
            headers: Request headers, normally from :meth:`_auth_headers`.

        Returns:
            ``None``; control returns to :meth:`_run_stream`, which decides
            whether to retry.

        Raises:
            _FatalStreamError: HTTP 401 or 404; retrying cannot help.
            httpx.HTTPStatusError: Any other 4xx/5xx from
                ``raise_for_status``; treated as transient upstream trouble.
        """

        # poll=false keeps a persistent streaming connection alive with keepalive events

        params = {"poll": "false"}

        async with self._http_client.stream(

            "GET",

            url,

            headers=headers,

            params=params,

            timeout=httpx.Timeout(connect=15.0, read=STREAM_TIMEOUT_SECONDS, write=15.0, pool=15.0),

        ) as response:

            if response.status_code == 401:

                logger.error(

                    "[%s] Authentication failed (401) 鈥?stopping reconnect loop. Check NTFY_TOKEN.",

                    self.name,

                )

                self._set_fatal_error(

                    "ntfy_unauthorized",

                    "ntfy server rejected auth (401). Check NTFY_TOKEN.",

                    retryable=False,

                )

                raise _FatalStreamError("401 Unauthorized")

            if response.status_code == 404:

                logger.error(

                    "[%s] Topic not found (404): %s 鈥?stopping reconnect loop.",

                    self.name, self._topic,

                )

                self._set_fatal_error(

                    "ntfy_topic_not_found",

                    f"ntfy topic '{self._topic}' returned 404. Check NTFY_TOPIC.",

                    retryable=False,

                )

                raise _FatalStreamError("404 Not Found")

            response.raise_for_status()

            async for line in response.aiter_lines():

                if not self._running:

                    return

                line = line.strip()

                if not line:

                    continue

                try:

                    event = json.loads(line)

                except json.JSONDecodeError:
                    # Keepalives and partial frames are not JSON; they are the
                    # expected heartbeat, so they are dropped silently.

                    continue

                if event.get("event") == "message":

                    await self._on_message(event)

    async def disconnect(self) -> None:

        """Tear down the subscription and release the shared HTTP client.

        Ordering matters: ``_running`` is cleared first so the stream loop
        observes the stop before it is cancelled, then the task is cancelled
        and awaited so no in-flight request survives, and only afterwards is
        the client closed. Closing the client before the task exits would let
        a cancelled request race the socket close. The dedup table is cleared
        because stale IDs from a previous lifetime would needlessly suppress
        fresh traffic on the next :meth:`connect`.

        Returns:
            ``None``. Idempotent: safe to call when the adapter never
            connected, since every field is ``None``-checked.
        """

        self._running = False

        self._mark_disconnected()

        if self._stream_task:

            self._stream_task.cancel()

            try:

                await self._stream_task

            except asyncio.CancelledError:

                pass

            self._stream_task = None

        if self._http_client:

            await self._http_client.aclose()

            self._http_client = None

        self._seen_messages.clear()

        logger.info("[%s] Disconnected", self.name)

    # -- Inbound message processing -----------------------------------------

    async def _on_message(self, event: Dict[str, Any]) -> None:

        """Turn one ntfy ``message`` frame into a gateway ``MessageEvent``.

        Pipeline: dedup by ``id`` -> drop self-echoes via :data:`_ECHO_TAG` ->
        drop empty bodies -> derive identity from the topic -> normalize the
        ``time`` field -> hand a
        :class:`~encre.gateway.platforms.base.MessageEvent` to
        :meth:`~BasePlatformAdapter.handle_message`, which is where
        :class:`encre.gateway.run.GatewayRunner` authorization and session
        routing take over.

        Args:
            event: Decoded ntfy frame; only ``id``, ``tags``, ``message``,
                ``topic`` and ``time`` are read.

        Returns:
            ``None``. Suppressed frames are logged at debug level and return
            without raising.
        """

        msg_id = event.get("id") or uuid.uuid4().hex

        if self._is_duplicate(msg_id):

            logger.debug("[%s] Duplicate message %s, skipping", self.name, msg_id)

            return

        # Echo-loop prevention: skip messages tagged by this adapter.

        tags = event.get("tags") or []

        if _ECHO_TAG in tags:

            logger.debug("[%s] Skipping own message (echo tag)", self.name)

            return

        text = (event.get("message") or "").strip()

        if not text:

            logger.debug("[%s] Empty message body, skipping", self.name)

            return

        topic = event.get("topic") or self._topic

        # ntfy has no native authenticated user identity. The title field is

        # publisher-controlled and must NOT be used for authorization - any

        # publisher who knows the topic can set title to an allowed username.

        # Treat ntfy as a single trusted channel; user_id is fixed to the

        # topic name. NTFY_ALLOWED_USERS is only a real trust boundary when

        # the topic itself is protected by a read token.

        user_id = topic

        user_name = topic

        source = self.build_source(

            chat_id=topic,

            chat_name=topic,

            chat_type="dm",

            user_id=user_id,

            user_name=user_name,

        )

        unix_ts = event.get("time")

        try:

            timestamp = (

                datetime.fromtimestamp(int(unix_ts), tz=timezone.utc)

                if unix_ts else datetime.now(tz=timezone.utc)

            )

        except (ValueError, OSError, TypeError):
            # Out-of-range or non-numeric timestamps come from misconfigured
            # publishers; falling back to arrival time keeps the event valid.

            timestamp = datetime.now(tz=timezone.utc)

        message_event = MessageEvent(

            text=text,

            message_type=MessageType.TEXT,

            source=source,

            message_id=msg_id,

            raw_message=event,

            timestamp=timestamp,

        )

        logger.debug("[%s] Message on topic %s: %s", self.name, topic, text[:80])

        await self.handle_message(message_event)

    # -- Deduplication ------------------------------------------------------

    def _is_duplicate(self, msg_id: str) -> bool:

        """Record a message id and report whether it had already been seen.

        ntfy redelivers on stream reconnect, so the same ``id`` can arrive
        twice across a reconnection. The table is checked *and* populated in
        one call; the write happens before the ``False`` return, which means
        two concurrent calls with the same id cannot both pass.

        Args:
            msg_id: The ntfy message ``id`` field, or a synthetic uuid for
                frames that omit it.

        Returns:
            ``True`` when the id was already recorded, meaning the caller must
            skip the frame.

        Note:
            Eviction is opportunistic and only runs when the table exceeds
            :data:`DEDUP_MAX_SIZE`, pruning entries older than
            :data:`DEDUP_WINDOW_SECONDS`. The table is therefore bounded by
            size and by age, never by wall-clock alone.
        """

        now = time.time()

        if len(self._seen_messages) > DEDUP_MAX_SIZE:

            cutoff = now - DEDUP_WINDOW_SECONDS

            self._seen_messages = {k: v for k, v in self._seen_messages.items() if v > cutoff}

        if msg_id in self._seen_messages:

            return True

        self._seen_messages[msg_id] = now

        return False

    # -- Outbound messaging -------------------------------------------------

    async def send(

        self,

        chat_id: str,

        content: str,

        reply_to: Optional[str] = None,

        metadata: Optional[Dict[str, Any]] = None,

    ) -> SendResult:

        """Publish ``content`` to the ntfy publish topic.

        This is the outbound hook consumed by
        :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`; each
        streamed agent delta arrives here as a chunked ``SendResult``. The
        effective topic is ``metadata["publish_topic"]`` first, then the
        configured publish topic, then ``chat_id`` -- the last step keeps the
        call usable even for a chat id that was never seen inbound.

        Args:
            chat_id: Encre chat identifier, which for ntfy is a topic name.
            content: Agent text; truncated to
                :data:`NtfyAdapter.MAX_MESSAGE_LENGTH` before publish.
            reply_to: Accepted for interface parity; ntfy has no threading.
            metadata: Optional dict; ``publish_topic`` overrides the default.

        Returns:
            A :class:`~encre.gateway.platforms.base.SendResult`. Success
            carries the ``id`` ntfy returned (or a synthetic 12-char uuid
            when the response body is not JSON). Failure carries an HTTP or
            exception message and no message id.

        Note:
            ``X-Tags`` always includes :data:`_ECHO_TAG`, which is what makes
            :meth:`_on_message` able to suppress its own output when the
            publish topic equals the subscribe topic.
        """

        metadata = metadata or {}

        publish_topic = metadata.get("publish_topic") or self._publish_topic or chat_id

        if not self._http_client:

            return SendResult(success=False, error="HTTP client not initialized")

        url = f"{self._server}/{publish_topic}"

        markdown_enabled = (self.config.extra or {}).get("markdown", False)

        headers = {

            **self._auth_headers(),

            "Content-Type": "text/plain; charset=utf-8",

            "X-Tags": _ECHO_TAG,

        }

        if markdown_enabled:

            headers["X-Markdown"] = "true"

        if len(content) > self.MAX_MESSAGE_LENGTH:

            logger.warning(

                "[%s] Message truncated from %d to %d chars (ntfy limit)",

                self.name, len(content), self.MAX_MESSAGE_LENGTH,

            )

        body = content[:self.MAX_MESSAGE_LENGTH]

        try:

            resp = await self._http_client.post(

                url, content=body.encode("utf-8"), headers=headers, timeout=15.0,

            )

            if resp.status_code < 300:

                try:

                    data = resp.json()

                    returned_id = data.get("id") or uuid.uuid4().hex[:12]

                except Exception:
                    # A non-JSON success body still means the publish landed;
                    # the synthetic id keeps SendResult.message_id non-empty.

                    returned_id = uuid.uuid4().hex[:12]

                return SendResult(success=True, message_id=returned_id)

            body_text = resp.text

            logger.warning("[%s] Send failed HTTP %d: %s", self.name, resp.status_code, body_text[:200])

            return SendResult(success=False, error=f"HTTP {resp.status_code}: {body_text[:200]}")

        except httpx.TimeoutException:

            return SendResult(success=False, error="Timeout publishing to ntfy")

        except Exception as e:

            logger.error("[%s] Send error: %s", self.name, e)

            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:

        """No-op: ntfy has no typing indicator primitive.

        Kept as an explicit method so the gateway runner can call it
        uniformly across adapters without an ``if`` on platform type.

        Args:
            chat_id: Target topic; unused.
            metadata: Unused.

        Returns:
            ``None`` immediately; never raises.
        """

        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:

        """Describe a chat id without contacting the ntfy server.

        ntfy exposes no chat metadata endpoint, so the topic name is the only
        available label and every topic is reported as a direct message.

        Args:
            chat_id: The topic name to describe.

        Returns:
            ``{"name": <topic>, "type": "dm"}``.
        """

        return {"name": chat_id, "type": "dm"}

    # -- Helpers ------------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:

        """Return request headers, adding ``Authorization`` when a token is set.

        Delegates to :func:`_build_auth_header` so the adapter and the
        standalone sender cannot disagree on Basic-versus-Bearer handling.

        Returns:
            ``{}`` when ``_token`` is empty, otherwise a single
            ``Authorization`` entry.
        """

        return _build_auth_header(self._token)


# ---------------------------------------------------------------------------

# Plugin registration

# ---------------------------------------------------------------------------


def _env_enablement() -> dict | None:

    """Seed ``PlatformConfig.extra`` from environment during config load.

    Called by the platform registry's env-enablement hook before adapter
    construction, so ``encre status`` and
    ``get_connected_platforms()`` can reflect an env-only setup without
    instantiating an HTTP client.

    Args:
        None; every value is read from ``os.environ``.

    Returns:
        ``None`` when ``NTFY_TOPIC`` is unset, which tells the caller to skip
        auto-enabling; otherwise a mapping with ``topic``, ``server``, and
        ``publish_topic``, ``token``, ``markdown`` when those env vars exist.

    Note:
        The special ``home_channel`` key is consumed by the core hook, which
        promotes it to a real ``HomeChannel`` dataclass on the
        ``PlatformConfig`` instead of merging it into ``extra``. Its ``name``
        falls back to the chat id, so a topic alone is sufficient.
    """

    topic = os.getenv("NTFY_TOPIC", "").strip()

    if not topic:

        return None

    seed: dict = {

        "topic": topic,

        "server": os.getenv("NTFY_SERVER_URL", DEFAULT_SERVER).rstrip("/"),

    }

    publish_topic = os.getenv("NTFY_PUBLISH_TOPIC", "").strip()

    if publish_topic:

        seed["publish_topic"] = publish_topic

    token = os.getenv("NTFY_TOKEN", "").strip()

    if token:

        seed["token"] = token

    markdown = os.getenv("NTFY_MARKDOWN", "").strip().lower()

    if markdown:
        # Only truthy spellings enable the header; any other value is ignored
        # rather than truth-tested, so "false" does not flip the flag on.

        seed["markdown"] = markdown in ("1", "true", "yes")

    home = os.getenv("NTFY_HOME_CHANNEL", "").strip() or topic

    if home:

        seed["home_channel"] = {

            "chat_id": home,

            "name": os.getenv("NTFY_HOME_CHANNEL_NAME", home),

        }

    return seed


async def _standalone_send(

    pconfig,

    chat_id: str,

    message: str,

    *,

    thread_id: Optional[str] = None,

    media_files: Optional[List[str]] = None,

    force_document: bool = False,

) -> Dict[str, Any]:

    """Out-of-process publish for cron jobs and send-message tooling.

    Used by ``tools/send_message_tool._send_via_adapter`` and the cron
    scheduler when the gateway runner is not in this process. Without this
    hook, ``deliver=ntfy`` cron jobs fail with
    ``No live adapter for platform``, so it opens its own short-lived
    :class:`httpx.AsyncClient` and reuses the same auth, tag and truncation
    helpers as the live adapter.

    Args:
        pconfig: Platform config; ``extra["server"]`` and
            ``extra["token"]`` are honoured over env.
        chat_id: Publish topic; takes precedence over every other source.
        message: Text body; clipped by :func:`_truncate_body`.
        thread_id: Accepted for signature parity only; ntfy has no thread.
        media_files: Accepted for signature parity only; ntfy has no media.
        force_document: Accepted for signature parity only.

    Returns:
        ``{"success": True, "platform": "ntfy", "chat_id": ..., "message_id":
        ...}`` on a 2xx publish, otherwise ``{"error": <reason>}``. The error
        dict never raises, which keeps the cron scheduler from aborting a run.

    Note:
        Markdown is honored when ``extra["markdown"]`` is truthy OR
        ``NTFY_MARKDOWN`` is a truthy spelling, so both wiring styles work.
    """

    if not HTTPX_AVAILABLE:

        return {"error": "ntfy standalone send: httpx not installed"}

    extra = getattr(pconfig, "extra", {}) or {}

    server = (

        extra.get("server")

        or os.getenv("NTFY_SERVER_URL", DEFAULT_SERVER)

    ).rstrip("/")

    publish_topic = (

        chat_id

        or extra.get("publish_topic")

        or os.getenv("NTFY_PUBLISH_TOPIC", "").strip()

        or extra.get("topic")

        or os.getenv("NTFY_TOPIC", "").strip()

    )

    if not publish_topic:

        return {"error": "ntfy standalone send: NTFY_TOPIC not configured"}

    token = extra.get("token") or os.getenv("NTFY_TOKEN", "")

    markdown_env = os.getenv("NTFY_MARKDOWN", "").strip().lower()

    markdown_enabled = bool(extra.get("markdown")) or markdown_env in ("1", "true", "yes")

    headers = {"Content-Type": "text/plain; charset=utf-8", "X-Tags": _ECHO_TAG, **_build_auth_header(token)}

    if markdown_enabled:

        headers["X-Markdown"] = "true"

    body = _truncate_body(message, context="ntfy standalone")

    url = f"{server}/{publish_topic}"

    try:

        async with httpx.AsyncClient(timeout=15.0) as client:

            resp = await client.post(url, content=body, headers=headers)

        if resp.status_code >= 300:

            return {"error": f"ntfy HTTP {resp.status_code}: {resp.text[:200]}"}

        try:

            data = resp.json()

            msg_id = data.get("id") or uuid.uuid4().hex[:12]

        except Exception:

            msg_id = uuid.uuid4().hex[:12]

        return {"success": True, "platform": "ntfy", "chat_id": publish_topic, "message_id": msg_id}

    except Exception as e:

        return {"error": f"ntfy standalone send failed: {e}"}


def _build_adapter(config):

    """Registry factory for :class:`NtfyAdapter`.

    Args:
        config: Resolved :class:`encre.gateway.config.PlatformConfig` for the
            ``ntfy`` platform, passed straight through.

    Returns:
        A new :class:`NtfyAdapter` instance; no connection is opened here, so
        adapter construction stays cheap for config-time probes.
    """

    return NtfyAdapter(config)

