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
WebSocket adapter for WeCom (Enterprise WeChat) AI Bots.

This module is the WebSocket half of Encre's WeCom support. A WeCom AI Bot
reaches the adapter over a persistent WebSocket rather than being called back,
so the adapter owns both directions of the wire: it authenticates with
``aibot_subscribe``, receives inbound ``aibot_msg_callback`` events, sends
agent output with ``aibot_send_msg`` or a reply frame bound to an inbound
``req_id``, and uploads outbound attachments through the
``aibot_upload_media_init`` / ``_chunk`` / ``_finish`` sequence. Inbound
image, file, voice and video attachments are downloaded best-effort and cached
as local paths so the agent can read them. The callback-mode sibling in
:mod:`encre.gateway.platforms.wecom.callback_adapter` covers self-built apps;
it shares no code with this module.

Lifecycle: :class:`encre.gateway.run.GatewayRunner` constructs
:class:`WeComAdapter` from a :class:`encre.gateway.config.PlatformConfig`, then
calls :meth:`WeComAdapter.connect`. That opens the socket, subscribes, and
starts :meth:`_listen_loop` plus :meth:`_heartbeat_loop`. Outbound
:class:`~encre.gateway.platforms.base.SendResult` values are consumed by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.

Notable behaviours:

* Request correlation. Every frame carries a ``headers.req_id``. Outbound
  requests register an :class:`asyncio.Future` keyed by that id; inbound frames
  with a matching id resolve it. Reply frames reuse the inbound id instead of
  generating a new one.
* Access policy at intake. DM and group policy are enforced when the callback
  arrives, before any agent work, via
  :meth:`_is_dm_intake_allowed` and :meth:`_is_group_allowed`.
* Text aggregation. WeCom clients split long messages around 4000 characters.
  Plain-text callbacks are therefore batched per session for a quiet period,
  using a longer delay when the latest chunk is near the split point.
* Proactive group sends are impossible for AI Bots, so a reply is only sent
  into a group when a prior inbound ``req_id`` is available.

Configuration in config.yaml:

    platforms:
      wecom:
        enabled: true
        extra:
          bot_id: "your-bot-id"          # or WECOM_BOT_ID env var
          secret: "your-secret"          # or WECOM_SECRET env var
          websocket_url: "wss://openws.work.weixin.qq.com"
          dm_policy: "pairing"           # open | allowlist | disabled | pairing
          allow_from: ["user_id_1"]
          group_policy: "pairing"        # open | allowlist | disabled | pairing
          group_allow_from: ["group_id_1"]
          groups:
            group_id_1:
              allow_from: ["user_id_1"]

Public API:

* :class:`WeComAdapter` -- the adapter.
* :func:`check_wecom_requirements` -- dependency probe.
* :func:`qr_scan_for_bot_info` -- terminal QR-code credential wizard.
* :func:`_standalone_send` -- out-of-process delivery helper for cron jobs.
* :func:`_is_connected`, :func:`_callback_is_connected`, :func:`_build_adapter`,
  :func:`_build_callback_adapter`, :func:`_check_requirements`,
  :func:`interactive_setup` -- platform registry glue.
"""

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import aiohttp
import httpx

AIOHTTP_AVAILABLE = True
HTTPX_AVAILABLE = True

from encre.gateway.config import Platform, PlatformConfig
from encre.gateway.platforms.helpers import MessageDeduplicator
from encre.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
def _atomic_write(p, d):
    """Write a mapping to disk as pretty-printed JSON.

    Creates every missing parent directory first so the helper can be pointed
    at a path inside a directory tree that does not exist yet. The dump is
    pretty-printed and UTF-8 without ASCII escaping so non-ASCII content stays
    readable in the stored file.

    Args:
        p: Destination path, as a string or path-like object.
        d: The mapping to serialise.

    Returns:
        None
    """
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
def env_float(n, d):
    """Read an environment variable as a float, falling back to a default.

    Args:
        n: The environment variable name.
        d: The value returned when the variable is unset.

    Returns:
        float: The parsed value, or ``d``.
    """
    return float(os.environ.get(n, d))

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"

# Application-level command names exchanged over the WebSocket: the vocabulary
# of the vendor's AI Bot protocol. APP_CMD_SUBSCRIBE authenticates the
# connection, APP_CMD_CALLBACK and APP_CMD_LEGACY_CALLBACK deliver user
# messages, APP_CMD_EVENT_CALLBACK delivers lifecycle events, APP_CMD_SEND
# initiates a proactive message, APP_CMD_RESPONSE answers a specific inbound
# request, and APP_CMD_PING keeps an idle connection alive. The
# APP_CMD_UPLOAD_MEDIA_* triple runs a chunked attachment upload.
APP_CMD_SUBSCRIBE = "aibot_subscribe"
APP_CMD_CALLBACK = "aibot_msg_callback"
APP_CMD_LEGACY_CALLBACK = "aibot_callback"
APP_CMD_EVENT_CALLBACK = "aibot_event_callback"
APP_CMD_SEND = "aibot_send_msg"
APP_CMD_RESPONSE = "aibot_respond_msg"
APP_CMD_PING = "ping"
APP_CMD_UPLOAD_MEDIA_INIT = "aibot_upload_media_init"
APP_CMD_UPLOAD_MEDIA_CHUNK = "aibot_upload_media_chunk"
APP_CMD_UPLOAD_MEDIA_FINISH = "aibot_upload_media_finish"

CALLBACK_COMMANDS = {APP_CMD_CALLBACK, APP_CMD_LEGACY_CALLBACK}
NON_RESPONSE_COMMANDS = CALLBACK_COMMANDS | {APP_CMD_EVENT_CALLBACK}
# Callbacks and lifecycle events are one-way pushes. Frames whose command is in
# this set never resolve an outstanding request future, so a stray callback
# cannot be mistaken for the answer to an earlier request that happens to reuse
# the same req_id.
MAX_MESSAGE_LENGTH = 4000
CONNECT_TIMEOUT_SECONDS = 20.0
REQUEST_TIMEOUT_SECONDS = 15.0
HEARTBEAT_INTERVAL_SECONDS = 30.0
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]

DEDUP_MAX_SIZE = 1000

IMAGE_MAX_BYTES = 10 * 1024 * 1024
VIDEO_MAX_BYTES = 10 * 1024 * 1024
VOICE_MAX_BYTES = 2 * 1024 * 1024
FILE_MAX_BYTES = 20 * 1024 * 1024
ABSOLUTE_MAX_BYTES = FILE_MAX_BYTES
UPLOAD_CHUNK_SIZE = 512 * 1024
MAX_UPLOAD_CHUNKS = 100
VOICE_SUPPORTED_MIMES = {"audio/amr"}


def check_wecom_requirements() -> bool:
    """Report whether every runtime dependency the WeCom adapter needs is installed.

    ``aiohttp`` carries the WebSocket control connection and ``httpx`` performs
    the media downloads. The probe is polled by the platform registry through
    :func:`_check_requirements`, so a deployment missing either library
    registers WeCom as unavailable instead of failing during startup.

    Returns:
        bool: ``True`` when both aiohttp and httpx are importable.
    """
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


def _coerce_list(value: Any) -> List[str]:
    """Coerce a config value into a trimmed list of strings.

    Accepts the several shapes config files tend to use for allowlists and
    returns one canonical form so downstream matching never has to branch.

    Args:
        value: ``None``, a comma-separated string, an iterable of values, or a
            single scalar.

    Returns:
        List[str]: The trimmed entries. ``None`` and empty strings yield ``[]``;
        a bare scalar yields a one-element list.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _normalize_entry(raw: str) -> str:
    """Normalise one allowlist entry by stripping vendor and type prefixes.

    Operators routinely copy prefixed ids such as ``wecom:user:foo`` or
    ``group:chat123`` out of other tools. Stripping them here means an entry
    can be written with or without the prefix and still match.

    Args:
        raw: The raw entry as written in the config.

    Returns:
        str: The bare identifier, stripped of whitespace and prefixes.
    """
    value = str(raw).strip()
    value = re.sub(r"^wecom:", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(user|group):", "", value, flags=re.IGNORECASE)
    return value.strip()


def _entry_matches(entries: List[str], target: str) -> bool:
    """Check whether ``target`` is allowed by an allowlist.

    Comparison is case-insensitive and each entry is normalised first, so
    ``User_A``, ``wecom:user:user_a`` and ``USER_A`` are equivalent. A ``*``
    entry permits everyone.

    Args:
        entries: The configured allowlist.
        target: The identifier to test.

    Returns:
        bool: ``True`` when some entry matches.
    """
    normalized_target = str(target).strip().lower()
    for entry in entries:
        normalized = _normalize_entry(entry).lower()
        if normalized == "*" or normalized == normalized_target:
            return True
    return False


class WeComAdapter(BasePlatformAdapter):
    """WeCom AI Bot adapter over a persistent WebSocket connection.

    Responsibilities:

    * Own the connection lifecycle: open the socket, authenticate with
      ``aibot_subscribe``, drive application-level heartbeats, and reconnect
      with exponential backoff when the link drops.
    * Turn inbound ``aibot_msg_callback`` frames into Encre
      :class:`encre.gateway.platforms.base.MessageEvent` objects, after
      enforcing DM and group access policy and reassembling messages that the
      WeCom client split near the 4000-character limit.
    * Deliver agent output as text and as native image, video, voice and file
      attachments, choosing between a proactive ``aibot_send_msg`` frame and a
      reply frame bound to a remembered inbound ``req_id``.

    Threading: the whole object lives on one asyncio event loop. All mutable
    state is coroutine-local; no locks are used because nothing outside the
    loop touches it.

    Invariants:

    * ``_pending_responses`` maps an in-flight ``req_id`` to the future waiting
      on it, and is always cleaned up even on timeout.
    * ``_reply_req_ids`` and ``_last_chat_req_ids`` are bounded by
      :data:`DEDUP_MAX_SIZE` and evict their oldest entry, so a long-running
      gateway cannot leak memory across many chats.

    Attributes:
        _bot_id, _secret, _ws_url: Authenticated endpoint configuration.
        _dm_policy, _allow_from, _group_policy, _group_allow_from, _groups:
            Access policy configuration.
        _session, _ws: The aiohttp client session and live WebSocket.
        _http_client: The shared httpx client used for media downloads.
        _pending_responses: Outstanding request futures keyed by ``req_id``.
        _dedup: Inbound message-id deduplicator.
        _reply_req_ids: ``{message_id: inbound req_id}`` for reply frames.
        _last_chat_req_ids: ``{chat_id: inbound req_id}`` for group replies.
        _pending_text_batches, _pending_text_batch_tasks: In-flight text
            aggregation state per session key.
        _device_id: Per-process random id sent during subscribe.
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    SUPPORTS_MESSAGE_EDITING = False
    # Chunk length above which a WeCom client is presumed to have split the
    # message at the 4000-char protocol limit and another chunk is expected.
    # Crossing this threshold selects the longer batch delay in
    # _flush_text_batch.
    _SPLIT_THRESHOLD = 3900

    def __init__(self, config: PlatformConfig):
        """Parse configuration and initialise runtime state.

        Reads every field from ``PlatformConfig.extra`` with environment
        variable fallbacks, so a deployment can be configured entirely through
        env vars. No sockets are opened here; :meth:`connect` does that.

        Args:
            config: The platform configuration carrying credentials, endpoint
                and access policy.

        Raises:
            ValueError: An integer field such as ``port`` is not parseable.
        """
        super().__init__(config, Platform.WECOM)

        extra = config.extra or {}
        self._bot_id = str(extra.get("bot_id") or os.getenv("WECOM_BOT_ID", "")).strip()
        self._secret = str(extra.get("secret") or os.getenv("WECOM_SECRET", "")).strip()
        self._ws_url = str(
            extra.get("websocket_url")
            or extra.get("websocketUrl")
            or os.getenv("WECOM_WEBSOCKET_URL", DEFAULT_WS_URL)
        ).strip() or DEFAULT_WS_URL

        self._dm_policy = str(extra.get("dm_policy") or os.getenv("WECOM_DM_POLICY", "pairing")).strip().lower()
        # dm_policy falls back to WECOM_DM_POLICY, so the allowlist must fall
        # back to WECOM_ALLOWED_USERS for the same reason. Without it an
        # env-only deployment (dm_policy=allowlist via env, no config extra)
        # would run with an empty allowlist and drop every authorized DM at
        # intake.
        self._allow_from = _coerce_list(
            extra.get("allow_from")
            or extra.get("allowFrom")
            or os.getenv("WECOM_ALLOWED_USERS", "")
        )

        self._group_policy = str(extra.get("group_policy") or os.getenv("WECOM_GROUP_POLICY", "pairing")).strip().lower()
        self._group_allow_from = _coerce_list(extra.get("group_allow_from") or extra.get("groupAllowFrom"))
        self._groups = extra.get("groups") if isinstance(extra.get("groups"), dict) else {}

        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._http_client: Optional["httpx.AsyncClient"] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._pending_responses: Dict[str, asyncio.Future] = {}
        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE)
        self._reply_req_ids: Dict[str, str] = {}

        # Text batching merges rapid successive callbacks into a single event.
        # This exists because WeCom clients split long messages around 4000
        # characters, so one user message typically arrives as several
        # callbacks that must be reassembled before dispatch.
        self._text_batch_delay_seconds = env_float("ENCRE_WECOM_TEXT_BATCH_DELAY_SECONDS", 0.6)
        self._text_batch_split_delay_seconds = env_float("ENCRE_WECOM_TEXT_BATCH_SPLIT_DELAY_SECONDS", 2.0)
        self._pending_text_batches: Dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}
        self._device_id = uuid.uuid4().hex
        self._last_chat_req_ids: Dict[str, str] = {}

    # ---- Connection lifecycle ----

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Open the WebSocket, authenticate, and start the background loops.

        Missing dependencies or credentials are reported as retryable fatal
        errors so the gateway can surface the reason instead of looping on a
        configuration problem that will never heal on its own.

        Args:
            is_reconnect: Part of the
                :class:`encre.gateway.platforms.base.BasePlatformAdapter`
                contract and set by :class:`encre.gateway.run.GatewayRunner`
                on every retry. This adapter ignores it because authentication
                is performed fresh on each call, but the keyword must stay in
                the signature or callers fail with ``TypeError``.

        Returns:
            bool: ``True`` when the socket is open, authenticated, and both
            the listen and heartbeat loops are running.
        """
        if not AIOHTTP_AVAILABLE:
            message = "WeCom startup failed: aiohttp not installed"
            self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
            logger.warning("[%s] %s. Run: pip install aiohttp", self.name, message)
            return False
        if not HTTPX_AVAILABLE:
            message = "WeCom startup failed: httpx not installed"
            self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
            logger.warning("[%s] %s. Run: pip install httpx", self.name, message)
            return False
        if not self._bot_id or not self._secret:
            message = "WeCom startup failed: WECOM_BOT_ID and WECOM_SECRET are required"
            self._set_fatal_error("wecom_missing_credentials", message, retryable=True)
            logger.warning("[%s] %s", self.name, message)
            return False

        try:
            # Tighter keepalive lets idle CLOSE_WAIT connections drain promptly
            # instead of pinning file descriptors for the default lifetime.
            from encre.gateway.platforms._http_client_limits import platform_httpx_limits
            self._http_client = httpx.AsyncClient(
                timeout=30.0, follow_redirects=True, limits=platform_httpx_limits(),
            )
            await self._open_connection()
            self._mark_connected()
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            logger.info("[%s] Connected to %s", self.name, self._ws_url)
            return True
        except Exception as exc:
            message = f"WeCom startup failed: {exc}"
            self._set_fatal_error("wecom_connect_error", message, retryable=True)
            logger.error("[%s] Failed to connect: %s", self.name, exc, exc_info=True)
            await self._cleanup_ws()
            if self._http_client:
                await self._http_client.aclose()
                self._http_client = None
            return False

    async def disconnect(self) -> None:
        """Tear down the connection and every background task.

        Cancels the listen and heartbeat loops, fails all outstanding request
        futures so their callers see an error instead of hanging, closes both
        the socket/session pair and the httpx client, and clears the
        deduplication cache.

        Returns:
            None
        """
        self._running = False
        self._mark_disconnected()

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        self._fail_pending_responses(RuntimeError("WeCom adapter disconnected"))
        await self._cleanup_ws()

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._dedup.clear()
        logger.info("[%s] Disconnected", self.name)

    async def _cleanup_ws(self) -> None:
        """Close the live WebSocket and its parent client session.

        Both references are cleared whether or not they were open, so repeated
        calls are safe and a stale reference can never be reused by a later
        :meth:`_open_connection`.

        Returns:
            None
        """
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _open_connection(self) -> None:
        """Open a new WebSocket and authenticate over it.

        Replaces any existing connection first, then sends ``aibot_subscribe``
        with the bot credentials and a per-process device id and waits for the
        handshake acknowledgement. The shared ``req_id`` in the frame headers
        is what ties the subscription request to its reply.

        Returns:
            None

        Raises:
            RuntimeError: The subscription acknowledgement reported a non-zero
                ``errcode``.
            TimeoutError: No acknowledgement arrived within the connect
                timeout.
        """
        await self._cleanup_ws()
        self._session = aiohttp.ClientSession(trust_env=True)
        self._ws = await self._session.ws_connect(
            self._ws_url,
            heartbeat=HEARTBEAT_INTERVAL_SECONDS * 2,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )

        req_id = self._new_req_id("subscribe")
        await self._send_json(
            {
                "cmd": APP_CMD_SUBSCRIBE,
                "headers": {"req_id": req_id},
                "body": {
                    "bot_id": self._bot_id,
                    "secret": self._secret,
                    "device_id": self._device_id,
                },
            }
        )

        auth_payload = await self._wait_for_handshake(req_id)
        errcode = auth_payload.get("errcode", 0)
        if errcode not in {0, None}:
            errmsg = auth_payload.get("errmsg", "authentication failed")
            raise RuntimeError(f"{errmsg} (errcode={errcode})")

    async def _wait_for_handshake(self, req_id: str) -> Dict[str, Any]:
        """Wait for the ``aibot_subscribe`` acknowledgement.

        Frames that arrive before the acknowledgement are discarded: the
        gateway may already stream pings, and any payload whose ``req_id`` does
        not match is either unrelated or pre-authentication traffic. The
        remaining budget is recomputed on every iteration so a single receive
        can never run past the overall connect deadline.

        Args:
            req_id: The request id sent in the subscribe frame.

        Returns:
            Dict[str, Any]: The acknowledgement payload.

        Raises:
            RuntimeError: The WebSocket is absent, or closes during auth.
            TimeoutError: No matching acknowledgement arrived in time.
        """
        if not self._ws:
            raise RuntimeError("WebSocket not initialized")

        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_SECONDS
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for WeCom subscribe acknowledgement")

            msg = await asyncio.wait_for(self._ws.receive(), timeout=remaining)
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if not payload:
                    continue
                if payload.get("cmd") == APP_CMD_PING:
                    continue
                if self._payload_req_id(payload) == req_id:
                    return payload
                logger.debug("[%s] Ignoring pre-auth payload: %s", self.name, payload.get("cmd"))
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                raise RuntimeError("WeCom websocket closed during authentication")

    async def _listen_loop(self) -> None:
        """Read WebSocket events forever, reconnecting on errors.

        Owns the reconnect policy. Any exception from :meth:`_read_events` is
        treated as a link failure: outstanding request futures are failed, the
        adapter sleeps for the current backoff step, then retries
        :meth:`_open_connection` until it succeeds. A clean cancellation
        returns without retrying.

        Returns:
            None
        """
        backoff_idx = 0
        while self._running:
            try:
                await self._read_events()
                backoff_idx = 0
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if not self._running:
                    return
                logger.warning("[%s] WebSocket error: %s", self.name, exc)
                self._fail_pending_responses(RuntimeError("WeCom connection interrupted"))

                # Clamp the index into the backoff table so a prolonged outage
                # cannot run past its end; a successful reconnect resets it.
                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                backoff_idx += 1
                await asyncio.sleep(delay)

                try:
                    await self._open_connection()
                    backoff_idx = 0
                    self._mark_connected()
                    logger.info("[%s] Reconnected", self.name)
                except Exception as reconnect_exc:
                    logger.warning("[%s] Reconnect failed: %s", self.name, reconnect_exc)

    async def _read_events(self) -> None:
        """Read WebSocket frames until the connection closes.

        Returns rather than raising when the socket closes, so the reconnect
        decision stays with :meth:`_listen_loop`. Only TEXT frames are
        interpreted; control frames such as pongs are ignored.

        Returns:
            None

        Raises:
            RuntimeError: The socket is not connected, or a close or error
                frame was received.
        """
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload:
                    await self._dispatch_payload(payload)
            elif msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSING}:
                raise RuntimeError("WeCom websocket closed")

    async def _heartbeat_loop(self) -> None:
        """Send application-level pings on an interval.

        The vendor closes idle connections server side, so an application-level
        ping is required in addition to the TCP keepalive configured at connect
        time. A failed send never propagates: it only indicates the link is
        already broken, which :meth:`_listen_loop` detects and acts on.

        Returns:
            None
        """
        try:
            while self._running:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if not self._ws or self._ws.closed:
                    continue
                try:
                    await self._send_json(
                        {
                            "cmd": APP_CMD_PING,
                            "headers": {"req_id": self._new_req_id("ping")},
                            "body": {},
                        }
                    )
                except Exception as exc:
                    logger.debug("[%s] Heartbeat send failed: %s", self.name, exc)
        except asyncio.CancelledError:
            pass

    async def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        """Route one inbound WebSocket payload.

        The resolution order is deliberate. A frame whose ``req_id`` matches an
        outstanding request resolves it, but only when its command is not in
        :data:`NON_RESPONSE_COMMANDS`, so a callback can never be mistaken for
        the answer to an earlier request. Callbacks are then handed to the
        message pipeline, and pings and lifecycle events are dropped.

        Args:
            payload: A parsed JSON frame.

        Returns:
            None
        """
        req_id = self._payload_req_id(payload)
        cmd = str(payload.get("cmd") or "")

        if req_id and req_id in self._pending_responses and cmd not in NON_RESPONSE_COMMANDS:
            future = self._pending_responses.get(req_id)
            if future and not future.done():
                future.set_result(payload)
            return

        if cmd in CALLBACK_COMMANDS:
            await self._on_message(payload)
            return
        if cmd in {APP_CMD_PING, APP_CMD_EVENT_CALLBACK}:
            return

        logger.debug("[%s] Ignoring websocket payload: %s", self.name, cmd or payload)

    def _fail_pending_responses(self, exc: Exception) -> None:
        """Fail every outstanding request future.

        Called when the link drops so that callers awaiting a request receive
        an exception instead of hanging for the full request timeout. The map
        is iterated over a copy because it is emptied during the loop.

        Args:
            exc: The exception to set on each pending future.

        Returns:
            None
        """
        for req_id, future in list(self._pending_responses.items()):
            if not future.done():
                future.set_exception(exc)
            self._pending_responses.pop(req_id, None)

    async def _send_json(self, payload: Dict[str, Any]) -> None:
        """Send one raw JSON frame over the active WebSocket.

        Args:
            payload: The frame to transmit.

        Returns:
            None

        Raises:
            RuntimeError: The WebSocket is absent or closed.
        """
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")
        await self._ws.send_json(payload)

    async def _send_request(self, cmd: str, body: Dict[str, Any], timeout: float = REQUEST_TIMEOUT_SECONDS) -> Dict[str, Any]:
        """Send a request frame and await the correlated response.

        A unique ``req_id`` is generated and registered in
        ``_pending_responses`` before the frame goes out, and removed in a
        ``finally`` clause, so a timeout can never leave a stale future behind
        for a later frame to resolve.

        Args:
            cmd: The application command name.
            body: The frame body.
            timeout: Per-request timeout in seconds.

        Returns:
            Dict[str, Any]: The correlated response payload.

        Raises:
            RuntimeError: The WebSocket is absent or closed.
            asyncio.TimeoutError: No correlated response arrived in time.
        """
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")

        req_id = self._new_req_id(cmd)
        future = asyncio.get_running_loop().create_future()
        self._pending_responses[req_id] = future
        try:
            await self._send_json({"cmd": cmd, "headers": {"req_id": req_id}, "body": body})
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        finally:
            self._pending_responses.pop(req_id, None)

    async def _send_reply_request(
        self,
        reply_req_id: str,
        body: Dict[str, Any],
        cmd: str = APP_CMD_RESPONSE,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        """Send a reply frame correlated to an inbound callback ``req_id``.

        Replies reuse the ``req_id`` of the callback they answer instead of
        generating a new one. This is the only way to post into a group chat,
        because AI Bots cannot initiate ``APP_CMD_SEND`` in groups.

        Args:
            reply_req_id: The inbound request id the reply is bound to.
            body: The frame body.
            cmd: The reply command, ``APP_CMD_RESPONSE`` by default.
            timeout: Per-request timeout in seconds.

        Returns:
            Dict[str, Any]: The correlated response payload.

        Raises:
            ValueError: ``reply_req_id`` is empty.
            RuntimeError: The WebSocket is absent or closed.
            asyncio.TimeoutError: No correlated response arrived in time.
        """
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")

        normalized_req_id = str(reply_req_id or "").strip()
        if not normalized_req_id:
            raise ValueError("reply_req_id is required")

        future = asyncio.get_running_loop().create_future()
        self._pending_responses[normalized_req_id] = future
        try:
            await self._send_json(
                {"cmd": cmd, "headers": {"req_id": normalized_req_id}, "body": body}
            )
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        finally:
            self._pending_responses.pop(normalized_req_id, None)

    @staticmethod
    def _new_req_id(prefix: str) -> str:
        """Build a unique request id.

        The human-readable prefix makes an in-flight frame identifiable in
        logs without dumping its whole payload.

        Args:
            prefix: A short label naming the operation.

        Returns:
            str: ``"{prefix}-{uuid4 hex}"``.
        """
        return f"{prefix}-{uuid.uuid4().hex}"

    @staticmethod
    def _payload_req_id(payload: Dict[str, Any]) -> str:
        """Read the ``req_id`` out of a frame's headers.

        Args:
            payload: A parsed JSON frame.

        Returns:
            str: The request id, or an empty string when the headers are
            missing or do not carry one.
        """
        headers = payload.get("headers")
        if isinstance(headers, dict):
            return str(headers.get("req_id") or "")
        return ""

    @staticmethod
    def _parse_json(raw: Any) -> Optional[Dict[str, Any]]:
        """Parse one inbound frame body as JSON.

        Never raises. A malformed frame, or a frame whose top level is not an
        object, is logged at debug level and dropped so a single bad frame
        cannot take the connection down.

        Args:
            raw: The raw frame text.

        Returns:
            Optional[Dict[str, Any]]: The parsed object, or ``None``.
        """
        try:
            payload = json.loads(raw)
        except Exception:
            logger.debug("Failed to parse WeCom payload: %r", raw)
            return None
        return payload if isinstance(payload, dict) else None

    # ---- Inbound message parsing ----

    async def _on_message(self, payload: Dict[str, Any]) -> None:
        """Process one inbound WeCom message callback event.

        Deduplicates, applies the access policy, extracts text and media,
        normalises the result into a :class:`MessageEvent`, and either batches
        it for aggregation or dispatches it to ``handle_message`` immediately.

        Args:
            payload: The parsed callback frame.

        Returns:
            None
        """
        body = payload.get("body")
        if not isinstance(body, dict):
            return

        msg_id = str(body.get("msgid") or self._payload_req_id(payload) or uuid.uuid4().hex)
        if self._dedup.is_duplicate(msg_id):
            logger.debug("[%s] Duplicate message %s ignored", self.name, msg_id)
            return
        self._remember_reply_req_id(msg_id, self._payload_req_id(payload))

        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
        sender_id = str(sender.get("userid") or "").strip()
        chat_id = str(body.get("chatid") or sender_id).strip()
        if not chat_id:
            logger.debug("[%s] Missing chat id, skipping message", self.name)
            return

        is_group = str(body.get("chattype") or "").lower() == "group"
        if is_group:
            if not self._is_group_allowed(chat_id, sender_id):
                logger.debug("[%s] Group %s / sender %s blocked by policy", self.name, chat_id, sender_id)
                return
        elif not self._is_dm_intake_allowed(sender_id):
            logger.debug("[%s] DM sender %s blocked by policy", self.name, sender_id)
            return

        # Record the inbound req_id only after the policy checks so proactive
        # sends to this chat can fall back to APP_CMD_RESPONSE. That fallback
        # is mandatory for groups: AI Bots cannot initiate APP_CMD_SEND in a
        # group chat, so every group reply must be bound to a prior inbound
        # req_id.
        self._remember_chat_req_id(chat_id, self._payload_req_id(payload))

        text, reply_text = self._extract_text(body)
        # A group message normally begins with an @mention of the bot, which
        # would mask slash commands such as "@BotName /approve". Stripping it
        # makes those commands parse correctly.
        if is_group and text:
            text = re.sub(r"^@\S+\s*", "", text).strip()
        media_urls, media_types = await self._extract_media(body)
        message_type = self._derive_message_type(body, text, media_types)
        has_reply_context = bool(reply_text and (text or media_urls))

        if not text and reply_text and not media_urls:
            text = reply_text

        if not text and not media_urls:
            logger.debug("[%s] Empty WeCom message skipped", self.name)
            return

        source = self.build_source(
            chat_id=chat_id,
            chat_type="group" if is_group else "dm",
            user_id=sender_id or None,
            user_name=sender_id or None,
        )

        event = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            raw_message=payload,
            message_id=msg_id,
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=f"quote:{msg_id}" if has_reply_context else None,
            reply_to_text=reply_text if has_reply_context else None,
            timestamp=datetime.now(tz=timezone.utc),
        )

        # Only plain text is batched. Commands and media arrive as single
        # callbacks and are never split by the WeCom client, so they dispatch
        # immediately for lower latency.
        if message_type == MessageType.TEXT and self._text_batch_delay_seconds > 0:
            self._enqueue_text_event(event)
        else:
            await self.handle_message(event)

    # ---- Text message aggregation (handles WeCom client-side splits) ----

    def _text_batch_key(self, event: MessageEvent) -> str:
        """Derive the session-scoped key used to bucket text batches.

        Delegating to
        :func:`encre.gateway.session.build_session_key` keeps the batching
        boundary identical to the session boundary, so a batch is never split
        across sessions and never merges two sessions together.

        Args:
            event: The incoming event, whose source drives the key.

        Returns:
            str: The session key.
        """
        from encre.gateway.session import build_session_key
        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
            profile=event.source.profile,
        )

    def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Buffer a text event and restart the flush timer.

        WeCom clients split a long user message near 4000 characters, so the
        chunks arrive within a few hundred milliseconds of each other. Merging
        them into one event before dispatch keeps the agent from seeing a
        half-sentence.

        Each incoming event replaces the previous flush task, so the quiet
        period is measured from the most recent chunk rather than from the
        first one. Any media carried by a later chunk is folded into the batch
        as well.

        Args:
            event: The text event to buffer.

        Returns:
            None
        """
        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
        else:
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            # Fold any media carried by a later chunk into the batch.
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)

        # Replace the previous flush task so the quiet period is measured from
        # the most recent chunk rather than from the first one.
        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(
            self._flush_text_batch(key)
        )

    async def _flush_text_batch(self, key: str) -> None:
        """Wait out the quiet period and then dispatch the aggregated text.

        The delay depends on the length of the most recent chunk. A chunk that
        is near the 4000-character split point almost certainly has a
        continuation arriving, so the longer
        ``_text_batch_split_delay_seconds`` window is used. Otherwise the short
        ``_text_batch_delay_seconds`` window applies and normal messages stay
        responsive.

        Args:
            key: The session key the batch is keyed by.

        Returns:
            None
        """
        current_task = asyncio.current_task()
        try:
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            if last_len >= self._SPLIT_THRESHOLD:
                delay = self._text_batch_split_delay_seconds
            else:
                delay = self._text_batch_delay_seconds
            await asyncio.sleep(delay)
            # A superseding enqueue can cancel this task while it sleeps.
            # Because Task cancellation is delivered at the next await point,
            # a cancel issued immediately after the sleep completes can land at
            # handle_message() rather than here. By then this task would
            # already have popped the merged event, so the superseding task
            # would find an empty batch and the message would be lost. The
            # ownership check below is synchronous -- no await between the
            # sleep and the pop -- so no other coroutine can alter the task
            # registry in between.
            if self._pending_text_batch_tasks.get(key) is not current_task:
                return
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            logger.info(
                "[WeCom] Flushing text batch %s (%d chars)",
                key, len(event.text or ""),
            )
            await self.handle_message(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    @staticmethod
    def _extract_text(body: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        """Extract plain text and quoted text from a callback payload.

        Covers the shapes WeCom sends: a plain ``text`` block, a ``mixed``
        container holding several ``msg_item`` entries, a ``voice`` block whose
        transcription arrives in ``content``, and an ``appmsg`` attachment
        whose ``title`` carries the file name. Quoted text is read from the
        ``quote`` block for both text and voice quotes.

        Args:
            body: The callback body.

        Returns:
            Tuple[str, Optional[str]]: The joined text body, and the quoted
            text when the message quotes another message.
        """
        text_parts: List[str] = []
        reply_text: Optional[str] = None
        msgtype = str(body.get("msgtype") or "").lower()

        if msgtype == "mixed":
            _raw_mixed = body.get("mixed")
            mixed = _raw_mixed if isinstance(_raw_mixed, dict) else {}
            _raw_items = mixed.get("msg_item")
            items = _raw_items if isinstance(_raw_items, list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("msgtype") or "").lower() == "text":
                    _raw_text = item.get("text")
                    text_block = _raw_text if isinstance(_raw_text, dict) else {}
                    content = str(text_block.get("content") or "").strip()
                    if content:
                        text_parts.append(content)
        else:
            text_block = body.get("text") if isinstance(body.get("text"), dict) else {}
            content = str(text_block.get("content") or "").strip()
            if content:
                text_parts.append(content)

            if msgtype == "voice":
                voice_block = body.get("voice") if isinstance(body.get("voice"), dict) else {}
                voice_text = str(voice_block.get("content") or "").strip()
                if voice_text:
                    text_parts.append(voice_text)

            # An appmsg attachment carries the file name in title, so the
            # agent gets some indication of what was sent.
            if msgtype == "appmsg":
                appmsg = body.get("appmsg") if isinstance(body.get("appmsg"), dict) else {}
                title = str(appmsg.get("title") or "").strip()
                if title:
                    text_parts.append(title)

        quote = body.get("quote") if isinstance(body.get("quote"), dict) else {}
        quote_type = str(quote.get("msgtype") or "").lower()
        if quote_type == "text":
            quote_text = quote.get("text") if isinstance(quote.get("text"), dict) else {}
            reply_text = str(quote_text.get("content") or "").strip() or None
        elif quote_type == "voice":
            quote_voice = quote.get("voice") if isinstance(quote.get("voice"), dict) else {}
            reply_text = str(quote_voice.get("content") or "").strip() or None

        return "\n".join(part for part in text_parts if part).strip(), reply_text

    async def _extract_media(self, body: Dict[str, Any]) -> Tuple[List[str], List[str]]:
        """Download inbound media and return cached local paths.

        Collects every media reference a message can carry -- a top-level
        ``image`` or ``file`` block, an ``image`` entry inside a ``mixed``
        container, an ``appmsg`` attachment, and an image or file quote -- and
        caches each one locally. Failures are swallowed per item, so one bad
        attachment does not discard the rest of the message.

        Args:
            body: The callback body.

        Returns:
            Tuple[List[str], List[str]]: Cached paths and their matching MIME
            types, in parallel order.
        """
        media_paths: List[str] = []
        media_types: List[str] = []
        refs: List[Tuple[str, Dict[str, Any]]] = []
        msgtype = str(body.get("msgtype") or "").lower()

        if msgtype == "mixed":
            _raw_mixed = body.get("mixed")
            mixed = _raw_mixed if isinstance(_raw_mixed, dict) else {}
            _raw_items = mixed.get("msg_item")
            items = _raw_items if isinstance(_raw_items, list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("msgtype") or "").lower()
                if item_type == "image" and isinstance(item.get("image"), dict):
                    refs.append(("image", item["image"]))
        else:
            if isinstance(body.get("image"), dict):
                refs.append(("image", body["image"]))
            if msgtype == "file" and isinstance(body.get("file"), dict):
                refs.append(("file", body["file"]))
            # appmsg wraps file attachments such as PDF, Word and Excel.
            if msgtype == "appmsg" and isinstance(body.get("appmsg"), dict):
                appmsg = body["appmsg"]
                if isinstance(appmsg.get("file"), dict):
                    refs.append(("file", appmsg["file"]))
                elif isinstance(appmsg.get("image"), dict):
                    refs.append(("image", appmsg["image"]))

        quote = body.get("quote") if isinstance(body.get("quote"), dict) else {}
        quote_type = str(quote.get("msgtype") or "").lower()
        if quote_type == "image" and isinstance(quote.get("image"), dict):
            refs.append(("image", quote["image"]))
        elif quote_type == "file" and isinstance(quote.get("file"), dict):
            refs.append(("file", quote["file"]))

        for kind, ref in refs:
            cached = await self._cache_media(kind, ref)
            if cached:
                path, content_type = cached
                media_paths.append(path)
                media_types.append(content_type)

        return media_paths, media_types

    async def _cache_media(self, kind: str, media: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """Cache one inbound media reference to local storage.

        Handles both transport shapes: an inline ``base64`` payload and a
        remote ``url``, the latter optionally protected by a per-file
        ``aeskey`` that the downloaded bytes must be AES-256-CBC decrypted
        with before they can be used.

        Args:
            kind: ``"image"`` or ``"file"``, deciding which cache helper to
                use and how to derive the extension.
            media: The media block, carrying ``base64`` or ``url`` plus
                optional ``filename``, ``name`` and ``aeskey``.

        Returns:
            Optional[Tuple[str, str]]: The cached path and MIME type, or
            ``None`` when the media cannot be decoded, downloaded or
            recognised as the expected kind.
        """
        if "base64" in media and media.get("base64"):
            try:
                raw = self._decode_base64(media["base64"])
            except Exception as exc:
                logger.debug("[%s] Failed to decode %s base64 media: %s", self.name, kind, exc)
                return None

            if kind == "image":
                ext = self._detect_image_ext(raw)
                try:
                    return cache_image_from_bytes(raw, ext), self._mime_for_ext(ext, fallback="image/jpeg")
                except ValueError as exc:
                    logger.warning("[%s] Rejected non-image bytes: %s", self.name, exc)
                    return None

            filename = str(media.get("filename") or media.get("name") or "wecom_file")
            return cache_document_from_bytes(raw, filename), mimetypes.guess_type(filename)[0] or "application/octet-stream"

        url = str(media.get("url") or "").strip()
        if not url:
            return None

        try:
            raw, headers = await self._download_remote_bytes(url, max_bytes=ABSOLUTE_MAX_BYTES)
        except Exception as exc:
            logger.debug("[%s] Failed to download %s from %s: %s", self.name, kind, url, exc)
            return None

        aes_key = str(media.get("aeskey") or "").strip()
        if aes_key:
            try:
                raw = self._decrypt_file_bytes(raw, aes_key)
            except Exception as exc:
                logger.debug("[%s] Failed to decrypt %s from %s: %s", self.name, kind, url, exc)
                return None

        content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip() or "application/octet-stream"
        if kind == "image":
            ext = self._guess_extension(url, content_type, fallback=self._detect_image_ext(raw))
            try:
                return cache_image_from_bytes(raw, ext), content_type or self._mime_for_ext(ext, fallback="image/jpeg")
            except ValueError as exc:
                logger.warning("[%s] Rejected non-image bytes from %s: %s", self.name, url, exc)
                return None

        filename = self._guess_filename(url, headers.get("content-disposition"), content_type)
        return cache_document_from_bytes(raw, filename), content_type

    @staticmethod
    def _decode_base64(data: str) -> bytes:
        """Decode an inline base64 payload, tolerating a data: URI prefix.

        Args:
            data: A raw base64 string, or a ``data:`` URI prefixing one.

        Returns:
            bytes: The decoded bytes.
        """
        payload = data.split(",", 1)[-1].strip()
        return base64.b64decode(payload)

    @staticmethod
    def _detect_image_ext(data: bytes) -> str:
        """Sniff an image container extension from magic bytes.

        Magic bytes are authoritative here because the resulting extension is
        what downstream tools use to decide how to open the file. Returns
        ``.jpg`` when the container is not recognised.

        Args:
            data: The decoded image bytes.

        Returns:
            str: A dotted extension.
        """
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        return ".jpg"

    @staticmethod
    def _mime_for_ext(ext: str, fallback: str = "application/octet-stream") -> str:
        """Look up the MIME type registered for an extension.

        Args:
            ext: A dotted extension.
            fallback: Returned when the extension is not registered.

        Returns:
            str: The MIME type.
        """
        return mimetypes.types_map.get(ext.lower(), fallback)

    @staticmethod
    def _guess_extension(url: str, content_type: str, fallback: str) -> str:
        """Derive a file extension from the response and the source URL.

        Preference order: the response content type, then the URL path suffix,
        then the caller-supplied fallback (usually from byte sniffing).

        Args:
            url: The source URL.
            content_type: The response content type.
            fallback: Used when neither of the above yields an extension.

        Returns:
            str: A dotted extension.
        """
        ext = mimetypes.guess_extension(content_type) if content_type else None
        if ext:
            return ext
        path_ext = Path(urlparse(url).path).suffix
        if path_ext:
            return path_ext
        return fallback

    @staticmethod
    def _guess_filename(url: str, content_disposition: Optional[str], content_type: str) -> str:
        """Derive a sensible file name for a downloaded document.

        Prefers the server-provided ``Content-Disposition`` file name, then the
        last URL path segment, adding an extension guessed from the content
        type when the name has none at all.

        Args:
            url: The source URL.
            content_disposition: The response header value, if any.
            content_type: The response content type.

        Returns:
            str: A file name.
        """
        if content_disposition:
            match = re.search(r'filename="?([^";]+)"?', content_disposition)
            if match:
                return match.group(1)

        name = Path(urlparse(url).path).name or "document"
        if "." not in name:
            ext = mimetypes.guess_extension(content_type) or ".bin"
            name = f"{name}{ext}"
        return name

    @staticmethod
    def _derive_message_type(body: Dict[str, Any], text: str, media_types: List[str]) -> MessageType:
        """Choose the normalised inbound message type.

        Document MIME types win, then images, then voice. A captioned image is
        reported as TEXT rather than PHOTO because the caption is the
        actionable content and the downstream pipeline already receives the
        image path separately.

        Args:
            body: The callback body.
            text: The extracted text.
            media_types: The extracted MIME types.

        Returns:
            MessageType: The best matching type.
        """
        if any(mtype.startswith(("application/", "text/")) for mtype in media_types):
            return MessageType.DOCUMENT
        if any(mtype.startswith("image/") for mtype in media_types):
            return MessageType.TEXT if text else MessageType.PHOTO
        if str(body.get("msgtype") or "").lower() == "voice":
            return MessageType.VOICE
        return MessageType.TEXT

    # ---- Policy helpers ----

    @property
    def enforces_own_access_policy(self) -> bool:
        """Signal that this adapter enforces its own access control.

        The gateway consults this flag to decide whether to apply its generic
        allow/deny handling. WeCom gates messages itself at callback intake
        through :meth:`_is_dm_intake_allowed` and :meth:`_is_group_allowed`,
        so the generic layer must stand down.

        Returns:
            bool: Always ``True``.
        """
        return True

    def _open_dm_opted_in(self) -> bool:
        """Report whether an "open" DM policy has been opted into.

        An open policy is not trusted on its own. Either a gateway-wide override
        or a WeCom-specific one must be set, so a bare ``dm_policy: open`` in
        config cannot silently accept every stranger.

        Returns:
            bool: ``True`` when either override env var is set truthily.
        """
        if os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"}:
            return True
        return os.getenv("WECOM_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"}

    def _is_dm_allowed(self, sender_id: str) -> bool:
        """Check DM permission without the pairing shortcut.

        This answers "may this user be served", which is stricter than
        :meth:`_is_dm_intake_allowed`, which answers "may this message enter
        the pipeline". The ``pairing`` policy is deliberately not accepted
        here, so a message admitted for pairing is still subject to the real
        decision.

        Args:
            sender_id: The sender's user id.

        Returns:
            bool: ``True`` when the sender is allowed.
        """
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return _entry_matches(self._allow_from, sender_id)
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    def _is_dm_intake_allowed(self, sender_id: str) -> bool:
        """Check whether a DM may enter the pipeline.

        Empty senders are rejected outright. ``pairing`` admits the message so
        a pairing flow can start; ``open`` requires an explicit opt-in;
        ``allowlist`` requires a matching entry; ``disabled`` and any unknown
        policy reject.

        Args:
            sender_id: The sender's user id.

        Returns:
            bool: ``True`` when the message may be processed.
        """
        principal = str(sender_id or "").strip()
        if not principal:
            return False
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return _entry_matches(self._allow_from, principal)
        if self._dm_policy == "pairing":
            return True
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    def _is_group_allowed(self, chat_id: str, sender_id: str) -> bool:
        """Check whether a group message may be processed.

        ``pairing`` is rejected outright because there is no pairing flow for
        groups. ``allowlist`` requires the group id to be listed, after which
        a per-group sender allowlist is applied when one is configured. A
        group with no sender list accepts any of its members.

        Args:
            chat_id: The group id.
            sender_id: The sender's user id.

        Returns:
            bool: ``True`` when the message may be processed.
        """
        if self._group_policy == "disabled":
            return False
        if self._group_policy == "pairing":
            return False
        if self._group_policy == "allowlist" and not _entry_matches(self._group_allow_from, chat_id):
            return False

        group_cfg = self._resolve_group_cfg(chat_id)
        sender_allow = _coerce_list(group_cfg.get("allow_from") or group_cfg.get("allowFrom"))
        if sender_allow:
            return _entry_matches(sender_allow, sender_id)
        return True

    def _resolve_group_cfg(self, chat_id: str) -> Dict[str, Any]:
        """Resolve the per-group configuration for a group id.

        Tries the exact id, then a case-insensitive match, then a ``"*"``
        wildcard entry. Callers treat the empty mapping returned on a miss as
        "no extra rules configured".

        Args:
            chat_id: The group id.

        Returns:
            Dict[str, Any]: The matching configuration.
        """
        if not isinstance(self._groups, dict):
            return {}
        if chat_id in self._groups and isinstance(self._groups[chat_id], dict):
            return self._groups[chat_id]
        lowered = chat_id.lower()
        for key, value in self._groups.items():
            if isinstance(key, str) and key.lower() == lowered and isinstance(value, dict):
                return value
        wildcard = self._groups.get("*")
        return wildcard if isinstance(wildcard, dict) else {}

    def _remember_reply_req_id(self, message_id: str, req_id: str) -> None:
        """Remember the inbound ``req_id`` for a message.

        Stores the callback's ``req_id`` under the message id so a later
        reply can be correlated to it. The map is bounded by
        :data:`DEDUP_MAX_SIZE` and evicts its oldest entry to keep a
        long-running gateway from leaking memory.

        Args:
            message_id: The inbound message id.
            req_id: The inbound request id to remember.

        Returns:
            None
        """
        normalized_message_id = str(message_id or "").strip()
        normalized_req_id = str(req_id or "").strip()
        if not normalized_message_id or not normalized_req_id:
            return
        self._reply_req_ids[normalized_message_id] = normalized_req_id
        while len(self._reply_req_ids) > DEDUP_MAX_SIZE:
            self._reply_req_ids.pop(next(iter(self._reply_req_ids)))

    def _remember_chat_req_id(self, chat_id: str, req_id: str) -> None:
        """Cache the most recent inbound ``req_id`` per chat.

        Serves as a fallback reply target when a proactive send into a group
        has no explicit ``reply_to``: AI Bots cannot initiate ``APP_CMD_SEND``
        in a group, so the send must be issued as ``APP_CMD_RESPONSE`` bound
        to some prior inbound ``req_id``. Bounded like ``_reply_req_ids`` so a
        long-running gateway does not leak memory across many chats.

        Args:
            chat_id: The chat the request id is associated with.
            req_id: The inbound request id to remember.

        Returns:
            None
        """
        normalized_chat_id = str(chat_id or "").strip()
        normalized_req_id = str(req_id or "").strip()
        if not normalized_chat_id or not normalized_req_id:
            return
        self._last_chat_req_ids[normalized_chat_id] = normalized_req_id
        while len(self._last_chat_req_ids) > DEDUP_MAX_SIZE:
            self._last_chat_req_ids.pop(next(iter(self._last_chat_req_ids)))

    def _reply_req_id_for_message(self, reply_to: Optional[str]) -> Optional[str]:
        """Resolve the reply ``req_id`` for a message id.

        Ids prefixed with ``"quote:"`` are local markers used to flag quoted
        context, not real inbound message ids, so they never resolve.

        Args:
            reply_to: The message id to reply to.

        Returns:
            Optional[str]: The stored inbound request id, or ``None``.
        """
        normalized = str(reply_to or "").strip()
        if not normalized or normalized.startswith("quote:"):
            return None
        return self._reply_req_ids.get(normalized)

    # ---- Outbound messaging ----

    @staticmethod
    def _guess_mime_type(filename: str) -> str:
        """Infer a MIME type from a file name.

        ``audio/amr`` is handled explicitly because the platform audio
        container is not always present in the system MIME database, and it
        is the only voice format the platform will accept.

        Args:
            filename: The file name to inspect.

        Returns:
            str: The inferred MIME type.
        """
        mime_type = mimetypes.guess_type(filename)[0]
        if mime_type:
            return mime_type
        if Path(filename).suffix.lower() == ".amr":
            return "audio/amr"
        return "application/octet-stream"

    @staticmethod
    def _normalize_content_type(content_type: str, filename: str) -> str:
        """Normalise a response content type into a usable MIME type.

        Strips parameters such as ``; charset=utf-8`` and canonicalises the
        ambiguous ``application/octet-stream`` and ``text/plain`` values back
        to a type inferred from the file name, since those two are what many
        download servers return regardless of the actual payload.

        Args:
            content_type: The response content type, possibly with params.
            filename: The resolved file name, used for the fallback.

        Returns:
            str: The normalised MIME type.
        """
        normalized = str(content_type or "").split(";", 1)[0].strip().lower()
        guessed = WeComAdapter._guess_mime_type(filename)
        if not normalized:
            return guessed
        if normalized in {"application/octet-stream", "text/plain"}:
            return guessed
        return normalized

    @staticmethod
    def _detect_wecom_media_type(content_type: str) -> str:
        """Map a MIME type onto the platform's attachment kinds.

        The platform distinguishes image, video, voice and generic file, each
        of which has its own size limit and its own message payload shape. Any
        audio type maps to voice, with ``application/ogg`` accepted as well
        because some clients label OGG audio that way.

        Args:
            content_type: The MIME type to classify.

        Returns:
            str: ``"image"``, ``"video"``, ``"voice"`` or ``"file"``.
        """
        mime_type = str(content_type or "").strip().lower()
        if mime_type.startswith("image/"):
            return "image"
        if mime_type.startswith("video/"):
            return "video"
        if mime_type.startswith("audio/") or mime_type == "application/ogg":
            return "voice"
        return "file"

    @staticmethod
    def _apply_file_size_limits(file_size: int, detected_type: str, content_type: Optional[str] = None) -> Dict[str, Any]:
        """Enforce the platform's per-kind attachment size limits.

        Anything past the absolute 20 MB ceiling is rejected outright. Below
        it, a type that exceeds its own limit (image or video 10 MB, voice
        2 MB) is downgraded to a plain file rather than rejected, because the
        bytes still arrive safely that way. Voice is additionally downgraded
        when its container is not one of :data:`VOICE_SUPPORTED_MIMES`, since
        the platform only plays AMR audio natively.

        Args:
            file_size: Payload size in bytes.
            detected_type: The classified attachment kind.
            content_type: The MIME type, used for the voice format check.

        Returns:
            Dict[str, Any]: ``final_type``, ``rejected``, ``reject_reason``,
            ``downgraded`` and ``downgrade_note``.
        """
        file_size_mb = file_size / (1024 * 1024)
        normalized_type = str(detected_type or "file").lower()
        normalized_content_type = str(content_type or "").strip().lower()

        if file_size > ABSOLUTE_MAX_BYTES:
            return {
                "final_type": normalized_type,
                "rejected": True,
                "reject_reason": (
                    f"File size {file_size_mb:.2f}MB exceeds WeCom's 20MB maximum limit and cannot be sent."
                    "Please try compressing the file or reducing its size."
                ),
                "downgraded": False,
                "downgrade_note": None,
            }

        if normalized_type == "image" and file_size > IMAGE_MAX_BYTES:
            return {
                "final_type": "file",
                "rejected": False,
                "reject_reason": None,
                "downgraded": True,
                "downgrade_note": f"Image size {file_size_mb:.2f}MB exceeds 10MB limit, sent as file instead",
            }

        if normalized_type == "video" and file_size > VIDEO_MAX_BYTES:
            return {
                "final_type": "file",
                "rejected": False,
                "reject_reason": None,
                "downgraded": True,
                "downgrade_note": f"Video size {file_size_mb:.2f}MB exceeds 10MB limit, sent as file instead",
            }

        if normalized_type == "voice":
            if normalized_content_type and normalized_content_type not in VOICE_SUPPORTED_MIMES:
                return {
                    "final_type": "file",
                    "rejected": False,
                    "reject_reason": None,
                    "downgraded": True,
                    "downgrade_note": (
                        f"Voice format {normalized_content_type} is not supported; WeCom only supports AMR, sent as file instead"
                    ),
                }
            if file_size > VOICE_MAX_BYTES:
                return {
                    "final_type": "file",
                    "rejected": False,
                    "reject_reason": None,
                    "downgraded": True,
                    "downgrade_note": f"Voice size {file_size_mb:.2f}MB exceeds 2MB limit, sent as file instead",
                }

        return {
            "final_type": normalized_type,
            "rejected": False,
            "reject_reason": None,
            "downgraded": False,
            "downgrade_note": None,
        }

    @staticmethod
    def _response_error(response: Dict[str, Any]) -> Optional[str]:
        """Format a non-zero platform ``errcode`` into a readable error string.

        Args:
            response: A platform response payload.

        Returns:
            Optional[str]: A formatted message, or ``None`` when the response
            reports success.
        """
        errcode = response.get("errcode", 0)
        if errcode in {0, None}:
            return None
        errmsg = str(response.get("errmsg") or "unknown error")
        return f"WeCom errcode {errcode}: {errmsg}"

    @classmethod
    def _raise_for_wecom_error(cls, response: Dict[str, Any], operation: str) -> None:
        """Raise when a platform response reports an error.

        Args:
            response: A platform response payload.
            operation: A short label naming the caller, used in the message.

        Returns:
            None

        Raises:
            RuntimeError: The response carried a non-zero ``errcode``.
        """
        error = cls._response_error(response)
        if error:
            raise RuntimeError(f"{operation} failed: {error}")

    @staticmethod
    def _decrypt_file_bytes(encrypted_data: bytes, aes_key: str) -> bytes:
        """Decrypt an attachment with its per-file AES-256-CBC key.

        The key arrives base64 encoded and unpadded. The IV is the first 16
        bytes of the decoded key, the same convention used by the callback
        codec in :mod:`encre.gateway.platforms.wecom.wecom_crypto`. PKCS#7
        padding is validated after decryption rather than trusted blindly.

        Args:
            encrypted_data: The ciphertext.
            aes_key: The base64-encoded 256-bit key.

        Returns:
            bytes: The decrypted, unpadded bytes.

        Raises:
            ValueError: The inputs are empty, the key decodes to the wrong
            length, or the padding is invalid.
        """
        if not encrypted_data:
            raise ValueError("encrypted_data is empty")
        if not aes_key:
            raise ValueError("aes_key is required")

        # WeCom ships the key unpadded, so restore base64 alignment before
        # decoding.
        aes_key = aes_key + '=' * ((4 - len(aes_key) % 4) % 4)
        key = base64.b64decode(aes_key)
        if len(key) != 32:
            raise ValueError(f"Invalid WeCom AES key length: expected 32 bytes, got {len(key)}")

        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(encrypted_data) + decryptor.finalize()

        pad_len = decrypted[-1]
        if pad_len < 1 or pad_len > 32 or pad_len > len(decrypted):
            raise ValueError(f"Invalid PKCS#7 padding value: {pad_len}")
        if any(byte != pad_len for byte in decrypted[-pad_len:]):
            raise ValueError("Invalid PKCS#7 padding: padding bytes mismatch")

        return decrypted[:-pad_len]

    async def _download_remote_bytes(
        self,
        url: str,
        max_bytes: int,
    ) -> Tuple[bytes, Dict[str, str]]:
        """Stream a remote media URL into memory with size and SSRF checks.

        The URL is validated against the SSRF guard before any request leaves
        the process. The size limit is enforced twice: against the declared
        ``Content-Length`` header, and, because that header is optional and
        untrusted, against the accumulated byte count while streaming.

        A temporary client is created and closed when the adapter has no
        shared one, so this remains safe to call outside a connected adapter.

        Args:
            url: The URL to download.
            max_bytes: Maximum number of bytes to accept.

        Returns:
            Tuple[bytes, Dict[str, str]]: The downloaded bytes and the
            lower-cased response headers.

        Raises:
            ValueError: The URL fails the SSRF check, or the payload exceeds
                ``max_bytes``.
            RuntimeError: httpx is unavailable.
        """
        from encre.ssrf import EncreSSRFGuard
        if not EncreSSRFGuard().validate_url(url):
            raise ValueError(f"Blocked unsafe URL (SSRF protection): {url[:80]}")

        if not HTTPX_AVAILABLE:
            raise RuntimeError("httpx is required for WeCom media download")

        client = self._http_client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        created_client = client is not self._http_client
        try:
            async with client.stream(
                "GET",
                url,
                headers={
                    "User-Agent": "EncreAgent/1.0",
                    "Accept": "*/*",
                },
            ) as response:
                response.raise_for_status()
                headers = {key.lower(): value for key, value in response.headers.items()}
                content_length = headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    raise ValueError(
                        f"Remote media exceeds WeCom limit: {int(content_length)} bytes > {max_bytes} bytes"
                    )

                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError(
                            f"Remote media exceeds WeCom limit while downloading: {len(data)} bytes > {max_bytes} bytes"
                        )

                return bytes(data), headers
        finally:
            if created_client:
                await client.aclose()

    @staticmethod
    def _looks_like_url(media_source: str) -> bool:
        """Report whether a media source is an http(s) URL.

        Args:
            media_source: A URL or a local path.

        Returns:
            bool: ``True`` for http or https schemes.
        """
        parsed = urlparse(str(media_source or ""))
        return parsed.scheme in {"http", "https"}

    async def _load_outbound_media(
        self,
        media_source: str,
        file_name: Optional[str] = None,
    ) -> Tuple[bytes, str, str]:
        """Load an outbound media source into memory.

        Accepts an http(s) URL, a ``file:`` URL, or a bare local path; relative
        paths are resolved against the current working directory. An
        unreplaced markdown-style placeholder, still wrapped in angle brackets,
        is rejected explicitly so a templating bug surfaces as a clear error
        instead of a missing-file failure.

        Args:
            media_source: The URL or path to load.
            file_name: A preferred file name, overriding any inferred one.

        Returns:
            Tuple[bytes, str, str]: The bytes, the content type, and the
            resolved file name.

        Raises:
            ValueError: The source is empty or is an unreplaced placeholder.
            FileNotFoundError: The local file does not exist.
        """
        source = str(media_source or "").strip()
        if not source:
            raise ValueError("media source is required")
        if re.fullmatch(r"<[^>\n]+>", source):
            raise ValueError(f"Media placeholder was not replaced with a real file path: {source}")

        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            data, headers = await self._download_remote_bytes(source, max_bytes=ABSOLUTE_MAX_BYTES)
            content_disposition = headers.get("content-disposition")
            resolved_name = file_name or self._guess_filename(source, content_disposition, headers.get("content-type", ""))
            content_type = self._normalize_content_type(headers.get("content-type", ""), resolved_name)
            return data, content_type, resolved_name

        if parsed.scheme == "file":
            local_path = Path(unquote(parsed.path)).expanduser()
        else:
            local_path = Path(source).expanduser()

        if not local_path.is_absolute():
            local_path = (Path.cwd() / local_path).resolve()

        if not local_path.exists() or not local_path.is_file():
            raise FileNotFoundError(f"Media file not found: {local_path}")

        data = local_path.read_bytes()
        resolved_name = file_name or local_path.name
        content_type = self._normalize_content_type("", resolved_name)
        return data, content_type, resolved_name

    async def _prepare_outbound_media(
        self,
        media_source: str,
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Load and validate an outbound media source before uploading.

        Loads the bytes, classifies them into a platform attachment kind, and
        applies the size limits. The returned mapping drives the upload path:
        ``rejected`` means the file cannot be sent at all, ``downgraded``
        means it is sent as a plain file and a notice should be shown.

        Args:
            media_source: The URL or path to load.
            file_name: A preferred file name.

        Returns:
            Dict[str, Any]: ``data``, ``content_type``, ``file_name``,
            ``detected_type``, plus the :meth:`_apply_file_size_limits` keys.
        """
        data, content_type, resolved_name = await self._load_outbound_media(media_source, file_name=file_name)
        detected_type = self._detect_wecom_media_type(content_type)
        size_check = self._apply_file_size_limits(len(data), detected_type, content_type)
        return {
            "data": data,
            "content_type": content_type,
            "file_name": resolved_name,
            "detected_type": detected_type,
            **size_check,
        }

    async def _upload_media_bytes(self, data: bytes, media_type: str, filename: str) -> Dict[str, Any]:
        """Upload bytes through the three-step chunked media upload flow.

        ``aibot_upload_media_init`` declares the payload and yields an
        ``upload_id``; the data is then sent in :data:`UPLOAD_CHUNK_SIZE`
        pieces as base64; ``aibot_upload_media_finish`` finalises the upload
        and returns the ``media_id`` to attach to a message. Chunk indexes are
        0-based to match the official SDK.

        Args:
            data: The payload bytes.
            media_type: The classified attachment kind.
            filename: The file name to report.

        Returns:
            Dict[str, Any]: ``type``, ``media_id`` and ``created_at``.

        Raises:
            ValueError: The payload is empty, or the chunk count exceeds
                :data:`MAX_UPLOAD_CHUNKS`.
            RuntimeError: A step reported an error or omitted its id.
        """
        if not data:
            raise ValueError("Cannot upload empty media")

        total_size = len(data)
        total_chunks = (total_size + UPLOAD_CHUNK_SIZE - 1) // UPLOAD_CHUNK_SIZE
        if total_chunks > MAX_UPLOAD_CHUNKS:
            raise ValueError(
                f"File too large: {total_chunks} chunks exceeds maximum of {MAX_UPLOAD_CHUNKS} chunks"
            )

        init_response = await self._send_request(
            APP_CMD_UPLOAD_MEDIA_INIT,
            {
                "type": media_type,
                "filename": filename,
                "total_size": total_size,
                "total_chunks": total_chunks,
                "md5": hashlib.md5(data).hexdigest(),
            },
        )
        self._raise_for_wecom_error(init_response, "media upload init")

        init_body = init_response.get("body") if isinstance(init_response.get("body"), dict) else {}
        upload_id = str(init_body.get("upload_id") or "").strip()
        if not upload_id:
            raise RuntimeError(f"media upload init failed: missing upload_id in response {init_response}")

        for chunk_index, start in enumerate(range(0, total_size, UPLOAD_CHUNK_SIZE)):
            chunk = data[start : start + UPLOAD_CHUNK_SIZE]
            chunk_response = await self._send_request(
                APP_CMD_UPLOAD_MEDIA_CHUNK,
                {
                    "upload_id": upload_id,
                    # Chunk indexes are 0-based, matching the official SDK.
                    "chunk_index": chunk_index,
                    "base64_data": base64.b64encode(chunk).decode("ascii"),
                },
            )
            self._raise_for_wecom_error(chunk_response, f"media upload chunk {chunk_index}")

        finish_response = await self._send_request(
            APP_CMD_UPLOAD_MEDIA_FINISH,
            {"upload_id": upload_id},
        )
        self._raise_for_wecom_error(finish_response, "media upload finish")

        finish_body = finish_response.get("body") if isinstance(finish_response.get("body"), dict) else {}
        media_id = str(finish_body.get("media_id") or "").strip()
        if not media_id:
            raise RuntimeError(f"media upload finish failed: missing media_id in response {finish_response}")

        return {
            "type": str(finish_body.get("type") or media_type),
            "media_id": media_id,
            "created_at": finish_body.get("created_at"),
        }

    async def _send_media_message(self, chat_id: str, media_type: str, media_id: str) -> Dict[str, Any]:
        """Send an uploaded attachment proactively into a chat.

        The payload nests the attachment under a key equal to its kind, which
        is how the platform distinguishes image, video, voice and file bodies.
        This proactive path is unavailable in group chats.

        Args:
            chat_id: The destination chat.
            media_type: The attachment kind.
            media_id: The id returned by the upload flow.

        Returns:
            Dict[str, Any]: The platform response.

        Raises:
            RuntimeError: The platform reported an error.
        """
        response = await self._send_request(
            APP_CMD_SEND,
            {
                "chatid": chat_id,
                "msgtype": media_type,
                media_type: {"media_id": media_id},
            },
        )
        self._raise_for_wecom_error(response, "send media message")
        return response

    async def _send_reply_markdown(self, reply_req_id: str, content: str) -> Dict[str, Any]:
        """Reply with text, bound to an inbound request id.

        The body is truncated to :data:`MAX_MESSAGE_LENGTH` before sending;
        markdown syntax in the text is preserved for the platform renderer.

        Args:
            reply_req_id: The inbound request id the reply is bound to.
            content: The text to send.

        Returns:
            Dict[str, Any]: The platform response.

        Raises:
            RuntimeError: The platform reported an error.
        """
        response = await self._send_reply_request(
            reply_req_id,
            {
                "msgtype": "text",
                "text": {"content": content[:self.MAX_MESSAGE_LENGTH]},
            },
        )
        self._raise_for_wecom_error(response, "send reply text")
        return response

    async def _send_reply_media_message(
        self,
        reply_req_id: str,
        media_type: str,
        media_id: str,
    ) -> Dict[str, Any]:
        """Reply with an uploaded attachment, bound to an inbound request id.

        Mirrors :meth:`_send_media_message` on the reply path, which is the
        only way to post an attachment into a group chat.

        Args:
            reply_req_id: The inbound request id the reply is bound to.
            media_type: The attachment kind.
            media_id: The id returned by the upload flow.

        Returns:
            Dict[str, Any]: The platform response.

        Raises:
            RuntimeError: The platform reported an error.
        """
        response = await self._send_reply_request(
            reply_req_id,
            {
                "msgtype": media_type,
                media_type: {"media_id": media_id},
            },
        )
        self._raise_for_wecom_error(response, "send reply media message")
        return response

    async def _send_followup_markdown(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
    ) -> Optional[SendResult]:
        """Send a best-effort supplementary text message.

        Used for captions and for downgrade notices after an attachment has
        been sent. Failures are logged and returned rather than raised, so a
        failed notice never masks the success of the primary attachment send.

        Args:
            chat_id: The destination chat.
            content: The text to send; empty content is a no-op.
            reply_to: An optional message id to reply to.

        Returns:
            Optional[SendResult]: The result, or ``None`` when nothing was
            sent.
        """
        if not content:
            return None
        result = await self.send(chat_id=chat_id, content=content, reply_to=reply_to)
        if not result.success:
            logger.warning("[%s] Follow-up markdown send failed: %s", self.name, result.error)
        return result

    async def _send_media_source(
        self,
        chat_id: str,
        media_source: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send an attachment from a source path or URL, with an optional caption.

        Loads and validates the source, uploads it, then sends the attachment
        through the reply path when an inbound ``req_id`` is available and
        through the proactive path otherwise. The caption and any downgrade
        notice are separate follow-up messages, since the platform has no
        caption field on an attachment body.

        Args:
            chat_id: The destination chat.
            media_source: A URL or local path.
            caption: Optional text sent after the attachment.
            file_name: Optional preferred file name.
            reply_to: Optional message id to reply to.

        Returns:
            SendResult: Success with the full response chain in
            ``raw_response``, or a failure carrying the reason.
        """
        if not chat_id:
            return SendResult(success=False, error="chat_id is required")

        try:
            prepared = await self._prepare_outbound_media(media_source, file_name=file_name)
        except FileNotFoundError as exc:
            return SendResult(success=False, error=str(exc))
        except Exception as exc:
            logger.error("[%s] Failed to prepare outbound media %s: %s", self.name, media_source, exc)
            return SendResult(success=False, error=str(exc))

        if prepared["rejected"]:
            await self._send_followup_markdown(
                chat_id,
                f"⚠️ {prepared['reject_reason']}",
                reply_to=reply_to,
            )
            return SendResult(success=False, error=prepared["reject_reason"])

        reply_req_id = self._reply_req_id_for_message(reply_to)
        if not reply_req_id and chat_id in self._last_chat_req_ids:
            reply_req_id = self._last_chat_req_ids[chat_id]

        try:
            upload_result = await self._upload_media_bytes(
                prepared["data"],
                prepared["final_type"],
                prepared["file_name"],
            )
            if reply_req_id:
                media_response = await self._send_reply_media_message(
                    reply_req_id,
                    prepared["final_type"],
                    upload_result["media_id"],
                )
            else:
                media_response = await self._send_media_message(
                    chat_id,
                    prepared["final_type"],
                    upload_result["media_id"],
                )
        except asyncio.TimeoutError:
            return SendResult(success=False, error="Timeout sending media to WeCom")
        except Exception as exc:
            logger.error("[%s] Failed to send media %s: %s", self.name, media_source, exc)
            return SendResult(success=False, error=str(exc))

        caption_result = None
        downgrade_result = None
        if caption:
            caption_result = await self._send_followup_markdown(
                chat_id,
                caption,
                reply_to=reply_to,
            )
        if prepared["downgraded"] and prepared["downgrade_note"]:
            downgrade_result = await self._send_followup_markdown(
                chat_id,
                f"ℹ️ {prepared['downgrade_note']}",
                reply_to=reply_to,
            )

        return SendResult(
            success=True,
            message_id=self._payload_req_id(media_response) or uuid.uuid4().hex[:12],
            raw_response={
                "upload": upload_result,
                "media": media_response,
                "caption": caption_result.raw_response if caption_result else None,
                "caption_error": caption_result.error if caption_result and not caption_result.success else None,
                "downgrade": downgrade_result.raw_response if downgrade_result else None,
                "downgrade_error": downgrade_result.error if downgrade_result and not downgrade_result.success else None,
            },
        )

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send plain text to a WeCom chat.

        The reply path is preferred when an inbound ``req_id`` is available for
        ``reply_to`` or for the chat, and the proactive ``aibot_send_msg`` path
        is used otherwise. The proactive path cannot reach group chats, which
        is what makes the reply fallback necessary.

        Text goes out as ``msgtype: "text"`` rather than markdown to avoid
        platform-specific rich-content rendering issues; markdown syntax in the
        body is preserved for the renderer.

        Args:
            chat_id: The destination chat.
            content: The text to send, truncated to
                :data:`MAX_MESSAGE_LENGTH`.
            reply_to: An optional message id to reply to.
            metadata: Accepted for signature compatibility and ignored.

        Returns:
            SendResult: Success with the platform response, or a failure
            carrying the reason.
        """
        del metadata

        if not chat_id:
            return SendResult(success=False, error="chat_id is required")

        try:
            reply_req_id = self._reply_req_id_for_message(reply_to)

            if not reply_req_id and chat_id in self._last_chat_req_ids:
                reply_req_id = self._last_chat_req_ids[chat_id]

            if reply_req_id:
                response = await self._send_reply_markdown(reply_req_id, content)
            else:
                response = await self._send_request(
                    APP_CMD_SEND,
                    {
                        "chatid": chat_id,
                        "msgtype": "text",
                        "text": {"content": content[:self.MAX_MESSAGE_LENGTH]},
                    },
                )
        except asyncio.TimeoutError:
            return SendResult(success=False, error="Timeout sending message to WeCom")
        except Exception as exc:
            logger.error("[%s] Send failed: %s", self.name, exc)
            return SendResult(success=False, error=str(exc))

        error = self._response_error(response)
        if error:
            return SendResult(success=False, error=error)

        return SendResult(
            success=True,
            message_id=self._payload_req_id(response) or uuid.uuid4().hex[:12],
            raw_response=response,
        )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image, falling back to a plain text message when it fails.

        When the attachment upload fails and the source is a URL, the caption
        and the URL are sent as text instead, so the content still reaches the
        chat. A local path gets no such fallback, because a path is not
        something the recipient can open.

        Args:
            chat_id: The destination chat.
            image_url: A URL or local path to an image.
            caption: Optional text sent with the image.
            reply_to: An optional message id to reply to.
            metadata: Accepted for signature compatibility and ignored.

        Returns:
            SendResult: The attachment result, or the fallback text result.
        """
        del metadata

        result = await self._send_media_source(
            chat_id=chat_id,
            media_source=image_url,
            caption=caption,
            reply_to=reply_to,
        )
        if result.success or not self._looks_like_url(image_url):
            return result

        logger.warning("[%s] Falling back to text send for image URL %s: %s", self.name, image_url, result.error)
        fallback_text = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id=chat_id, content=fallback_text, reply_to=reply_to)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send an image read from a local file path.

        Args:
            chat_id: The destination chat.
            image_path: A local path to the image file.
            caption: Optional text sent with the image.
            reply_to: An optional message id to reply to.
            **kwargs: Additional platform-specific options, ignored here.

        Returns:
            SendResult: The send result.
        """
        del kwargs
        return await self._send_media_source(
            chat_id=chat_id,
            media_source=image_path,
            caption=caption,
            reply_to=reply_to,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document read from a local file path.

        Args:
            chat_id: The destination chat.
            file_path: A local path to the document.
            caption: Optional text sent with the document.
            file_name: Optional file name to present to the recipient.
            reply_to: An optional message id to reply to.
            **kwargs: Additional platform-specific options, ignored here.

        Returns:
            SendResult: The send result.
        """
        del kwargs
        return await self._send_media_source(
            chat_id=chat_id,
            media_source=file_path,
            caption=caption,
            file_name=file_name,
            reply_to=reply_to,
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a voice message read from a local audio file.

        Only AMR audio is playable natively; other containers are downgraded to
        a plain file with a notice, per :meth:`_apply_file_size_limits`.

        Args:
            chat_id: The destination chat.
            audio_path: A local path to the audio file.
            caption: Optional text sent with the audio.
            reply_to: An optional message id to reply to.
            **kwargs: Additional platform-specific options, ignored here.

        Returns:
            SendResult: The send result.
        """
        del kwargs
        return await self._send_media_source(
            chat_id=chat_id,
            media_source=audio_path,
            caption=caption,
            reply_to=reply_to,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video read from a local file path.

        Videos above the 10 MB limit are downgraded to a plain file with a
        notice, per :meth:`_apply_file_size_limits`.

        Args:
            chat_id: The destination chat.
            video_path: A local path to the video file.
            caption: Optional text sent with the video.
            reply_to: An optional message id to reply to.
            **kwargs: Additional platform-specific options, ignored here.

        Returns:
            SendResult: The send result.
        """
        del kwargs
        return await self._send_media_source(
            chat_id=chat_id,
            media_source=video_path,
            caption=caption,
            reply_to=reply_to,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No-op: this adapter has no typing indicator.

        The WebSocket protocol exposes no typing status, so the call succeeds
        without sending anything.

        Args:
            chat_id: The chat the status would target.
            metadata: Optional extra data.

        Returns:
            None
        """
        del chat_id, metadata

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return minimal chat metadata for a chat id.

        Chat ids cannot be resolved to display names over this protocol, so the
        id itself is used as the name and the type is inferred from a ``group``
        prefix convention in the id.

        Args:
            chat_id: The chat id to describe.

        Returns:
            Dict[str, Any]: A mapping with ``name`` and ``type``.
        """
        return {
            "name": chat_id,
            "type": "group" if chat_id and chat_id.lower().startswith("group") else "dm",
        }

    async def get_qrcode_url(self) -> tuple[str, str]:
        """Fetch a scan-to-bind QR code and render it as a data URI.

        Talks to the same vendor endpoints as :func:`qr_scan_for_bot_info`
        but returns an SVG data URI suitable for a plain ``<img>`` tag in
        the desktop UI. Without the optional ``qrcode`` package the raw
        authorisation URL is returned instead.

        Returns:
            A ``(display_url, scode)`` pair; ``scode`` is the opaque token
            later passed to :meth:`poll_qrcode_status`.

        Raises:
            RuntimeError: When the QR code cannot be generated.

        Note:
            The ``work.weixin.qq.com/ai/qc/{generate,query_result}`` endpoints
            serve the vendor admin console rather than the public developer API,
            so they may change without notice.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_QR_GENERATE_URL, params={"source": "encre"})
            resp.raise_for_status()
            payload = resp.json()
        data = payload.get("data") or {}
        scode = str(data.get("scode") or "").strip()
        auth_url = str(data.get("auth_url") or "").strip()
        if not scode or not auth_url:
            raise RuntimeError(f"WeCom QR generate failed: {payload}")
        display_url = auth_url
        import qrcode.image.svg

        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
        )
        qr.add_data(auth_url)
        img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        svg_data = img.to_string()
        if isinstance(svg_data, str):
            svg_data = svg_data.encode("utf-8")
        display_url = f"data:image/svg+xml;base64,{base64.b64encode(svg_data).decode('ascii')}"
        return display_url, scode

    async def poll_qrcode_status(self, qrcode_token: str) -> dict | None:
        """Poll the scan result once and return credentials when complete.

        Mirrors the other scan adapters: a falsy result means "not finished
        yet" and the caller keeps polling until its deadline. The WeCom
        endpoint exposes neither a scanned nor a server-side expiry
        intermediate state, so only the terminal ``success`` status is
        distinguishable; the caller's deadline provides the timeout.

        Args:
            qrcode_token: The ``scode`` returned by :meth:`get_qrcode_url`.

        Returns:
            ``{"bot_id", "secret"}`` when the scan succeeded, otherwise
            ``None`` (pending or transient failure).
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(_QR_QUERY_URL, params={"scode": qrcode_token})
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            logger.debug("WeCom QR poll error: %s", exc)
            return None
        data = payload.get("data") or {}
        if str(data.get("status") or "").lower() != "success":
            return None
        bot_info = data.get("bot_info") or {}
        bot_id = str(bot_info.get("botid") or bot_info.get("bot_id") or "").strip()
        secret = str(bot_info.get("secret") or "").strip()
        if not bot_id or not secret:
            logger.warning(
                "WeCom QR: scan reported success but bot_info missing or incomplete: %s",
                data,
            )
            return None
        return {"bot_id": bot_id, "secret": secret}


# ------------------------------------------------------------------
# QR code scan flow for obtaining bot credentials
# ------------------------------------------------------------------

# Endpoints and pacing for the QR credential wizard. The work.weixin.qq.com
# paths are internal to the vendor admin console rather than part of the
# public developer API, so they may change without notice.
_QR_GENERATE_URL = "https://work.weixin.qq.com/ai/qc/generate"
_QR_QUERY_URL = "https://work.weixin.qq.com/ai/qc/query_result"
_QR_CODE_PAGE = "https://work.weixin.qq.com/ai/qc/gen?source=encre&scode="
_QR_POLL_INTERVAL = 3  # seconds
_QR_POLL_TIMEOUT = 300  # 5 minutes


def qr_scan_for_bot_info(
    *,
    timeout_seconds: int = _QR_POLL_TIMEOUT,
) -> Optional[Dict[str, str]]:
    """Run the QR scan flow that provisions ``bot_id`` and ``secret``.

    Fetches a one-time QR code, renders it as ASCII art in the terminal, then
    polls the query endpoint until the scan completes or the deadline passes.
    When the optional ``qrcode`` package is absent the user is given the same
    authorisation URL to open on their phone instead.

    Args:
        timeout_seconds: Total time to wait for the scan, in seconds.

    Returns:
        Optional[Dict[str, str]]: ``{"bot_id": ..., "secret": ...}`` on
        success, or ``None`` on any failure or on timeout.

    Note:
        The ``work.weixin.qq.com/ai/qc/{generate,query_result}`` endpoints
        serve the vendor admin console rather than the public developer API,
        so they may change without notice.
    """
    import urllib.request
    import urllib.parse

    generate_url = f"{_QR_GENERATE_URL}?source=encre"

    # ---- Step 1: Fetch QR code ----
    print("  Connecting to WeCom...", end="", flush=True)
    try:
        req = urllib.request.Request(generate_url, headers={"User-Agent": "EncreAgent/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.error("WeCom QR: failed to fetch QR code: %s", exc)
        print(f" failed: {exc}")
        return None

    data = raw.get("data") or {}
    scode = str(data.get("scode") or "").strip()
    auth_url = str(data.get("auth_url") or "").strip()

    if not scode or not auth_url:
        logger.error("WeCom QR: unexpected response format: %s", raw)
        print(" failed: unexpected response format")
        return None

    print(" done.")

    # ---- Step 2: Render QR code in terminal ----
    print()
    qr_rendered = False
    import qrcode as _qrcode
    qr = _qrcode.QRCode()
    qr.add_data(auth_url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    qr_rendered = True

    page_url = f"{_QR_CODE_PAGE}{urllib.parse.quote(scode)}"
    if qr_rendered:
        print(f"\n  Scan the QR code above, or open this URL directly:\n  {page_url}")
    else:
        print(f"  Open this URL in WeCom on your phone:\n\n  {page_url}\n")
        print("  Tip: pip install qrcode  to display a scannable QR code here next time")
    print()
    print("  Fetching configuration results...", end="", flush=True)

    # ---- Step 3: Poll for result ----
    deadline = time.monotonic() + timeout_seconds
    query_url = f"{_QR_QUERY_URL}?scode={urllib.parse.quote(scode)}"
    poll_count = 0

    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(query_url, headers={"User-Agent": "EncreAgent/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("WeCom QR poll error: %s", exc)
            time.sleep(_QR_POLL_INTERVAL)
            continue

        poll_count += 1
        # Emit one dot per poll so the user sees progress between intervals.
        print(".", end="", flush=True)

        result_data = result.get("data") or {}
        status = str(result_data.get("status") or "").lower()

        if status == "success":
            print()  # end the progress line before printing the result.
            bot_info = result_data.get("bot_info") or {}
            bot_id = str(bot_info.get("botid") or bot_info.get("bot_id") or "").strip()
            secret = str(bot_info.get("secret") or "").strip()
            if bot_id and secret:
                return {"bot_id": bot_id, "secret": secret}
            logger.warning(
                "WeCom QR: scan reported success but bot_info missing or incomplete: %s",
                result_data,
            )
            print(
                "  QR scan reported success but no bot credentials were returned.\n"
                "  This usually means the bot was not actually created on the WeCom side.\n"
                "  Falling back to manual credential entry."
            )
            return None

        time.sleep(_QR_POLL_INTERVAL)

    print()  # end the progress line before printing the timeout message.
    print(f"  QR scan timed out ({timeout_seconds // 60} minutes). Please try again.")
    return None


# ---- Platform registration glue ----
#
# The adapters are wired into the platform registry through this block.
# _build_adapter and _build_callback_adapter are the lazily called factories,
# _check_requirements reports whether the runtime dependencies are importable,
# _is_connected and _callback_is_connected decide whether a platform counts as
# configured, and _standalone_send supplies an out-of-process delivery path for
# cron jobs that run without the gateway. The two adapters are exposed as
# separate platforms because they need entirely different credentials: a bot
# id and secret for the WebSocket adapter, a corp id and secret per app for the
# callback adapter.


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Deliver a message out of process, through a temporary adapter.

    Satisfies the standalone sender contract so that cron jobs delivered to
    WeCom work even when they run without the gateway. Opens an ephemeral
    :class:`WeComAdapter`, connects, sends, disconnects, and reports the result
    as a mapping rather than raising, since the caller is a tool result.

    Args:
        pconfig: A PlatformConfig carrying the bot credentials.
        chat_id: The destination chat.
        message: The text to send.
        thread_id: Ignored; retained for contract compatibility.
        media_files: Ignored; retained for contract compatibility.
        force_document: Ignored; retained for contract compatibility.

    Returns:
        Dict[str, Any]: A success mapping carrying ``message_id``, or an
        ``error`` mapping describing what went wrong.
    """
    if not check_wecom_requirements():
        return {"error": "WeCom requirements not met. Need aiohttp + WECOM_BOT_ID/SECRET."}
    try:
        adapter = WeComAdapter(pconfig)
        connected = await adapter.connect()
        if not connected:
            return {"error": f"WeCom: failed to connect - {getattr(adapter, 'fatal_error_message', None) or 'unknown error'}"}
        try:
            result = await adapter.send(chat_id, message)
            if not result.success:
                return {"error": f"WeCom send failed: {result.error}"}
            return {
                "success": True,
                "platform": "wecom",
                "chat_id": chat_id,
                "message_id": result.message_id,
            }
        finally:
            await adapter.disconnect()
    except Exception as e:
        return {"error": f"WeCom send failed: {e}"}


def interactive_setup() -> None:
    """Interactive setup hook.

    Credential entry is handled by :func:`qr_scan_for_bot_info`, so this hook
    has no wizard to run and only records the call.

    Returns:
        None
    """
    logger.info("[wecom] interactive_setup stub (encre not available)")
def _is_connected(config) -> bool:
    """Report whether the WebSocket adapter is configured.

    A bot id in the platform extra is enough to count the platform as
    configured, which is the condition the gateway uses to offer WeCom at all.

    Args:
        config: The platform configuration.

    Returns:
        bool: ``True`` when ``extra["bot_id"]`` is set.
    """
    extra = getattr(config, "extra", {}) or {}
    return bool(extra.get("bot_id"))


def _callback_is_connected(config) -> bool:
    """Report whether the callback adapter is configured.

    Callback mode needs a corp id for a single-app setup, or an ``apps`` block
    listing several self-built apps.

    Args:
        config: The platform configuration.

    Returns:
        bool: ``True`` when either credential form is present.
    """
    extra = getattr(config, "extra", {}) or {}
    return bool(extra.get("corp_id") or extra.get("apps"))


def _build_adapter(config):
    """Registry factory for the WebSocket adapter.

    Args:
        config: The platform configuration.

    Returns:
        WeComAdapter: A newly constructed adapter.
    """
    return WeComAdapter(config)


def _build_callback_adapter(config):
    """Registry factory for the callback adapter.

    The import is local so that importing this module never requires aiohttp or
    defusedxml when only the WebSocket adapter is in use.

    Args:
        config: The platform configuration.

    Returns:
        WecomCallbackAdapter: A newly constructed callback adapter.
    """
    from .callback_adapter import WecomCallbackAdapter
    return WecomCallbackAdapter(config)


# ---- Platform registration ----
#
# The legacy bottom-of-module PlatformEntry registration moved to
# ``plugin.py``; this module only keeps the availability probe the spec
# reuses.


def _check_requirements() -> bool:
    """Report whether the WebSocket adapter's dependencies are importable.

    This is the availability hook the gateway-adapter spec polls before
    offering the platform, so a missing aiohttp or httpx hides WeCom rather
    than failing at connect time.

    Returns:
        bool: ``True`` when aiohttp and httpx are importable.
    """
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE
