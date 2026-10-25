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
Callback-mode adapter for WeCom self-built enterprise applications.

This module implements the inbound half of WeCom's "self-built application"
integration. Unlike the WebSocket adapter in
:mod:`encre.gateway.platforms.wecom.adapter`, which speaks WeCom's ``aibot_*``
JSON frames, a self-built app is reached by WeCom itself: the app must expose
an HTTPS URL, and WeCom POSTs encrypted XML envelopes to it. This adapter
therefore owns a small aiohttp HTTP server that:

1. serves the GET URL-verification handshake, proving ownership of the URL by
   decrypting and echoing back WeCom's ``echostr``,
2. serves the POST message callbacks, verifying the ``msg_signature`` and
   decrypting the ``Encrypt`` blob through
   :class:`encre.gateway.platforms.wecom.wecom_crypto.WXBizMsgCrypt`,
3. normalises the decrypted XML into an Encre
   :class:`encre.gateway.platforms.base.MessageEvent` and enqueues it,
4. answers every callback immediately with the literal body ``success`` so
   WeCom does not retry it, and
5. delivers the agent's reply later through WeCom's proactive
   ``message/send`` HTTP API using a cached, lazily refreshed access token.

Lifecycle: :class:`encre.gateway.run.GatewayRunner` constructs this adapter
from a :class:`encre.gateway.config.PlatformConfig`, calls
:meth:`WecomCallbackAdapter.connect`, which binds the server and starts
:meth:`WecomCallbackAdapter._poll_loop`. The loop drains the internal queue and
forwards each event to ``handle_message``. Agent output arrives through
:meth:`WecomCallbackAdapter.send`, whose
:class:`~encre.gateway.platforms.base.SendResult` values are consumed by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.

Design points:

* Security-first parsing. Untrusted, pre-authentication request bodies are
  parsed with ``defusedxml`` to neutralise entity-expansion (billion laughs)
  and external entity expansion. Outbound response XML is built with the
  standard library inside :mod:`encre.gateway.platforms.wecom.wecom_crypto`
  and is never parsed back into this module.
* Multi-app scoping. Several self-built apps may be served by one gateway
  instance. Inbound messages are namespaced ``corp_id:user_id`` so the same
  person in two corps is not collapsed into one session, and each app keeps
  its own crypto object and access-token cache. Because a callback carries no
  app id, each callback is attempted against every configured app in order and
  the first one whose signature verifies wins.
* DoS hardening. Request bodies are capped at :data:`_MAX_BODY` twice: at the
  aiohttp ``client_max_size`` limit, which rejects them with 413, and by an
  explicit guard in the handler. The cap bounds the work an unauthenticated
  POST can force before any signature check runs.
* Deduplication. WeCom retries callbacks when a response times out, so inbound
  message ids are tracked in a bounded, time-windowed cache and repeated ids
  within the window are dropped.

Dependencies: ``aiohttp`` (HTTP server), ``httpx`` (outbound token and message
calls) and ``defusedxml`` (safe parsing). When any is missing,
:func:`check_wecom_callback_requirements` returns ``False`` and the adapter
refuses to start.

Public API:

* :class:`WecomCallbackAdapter` -- the adapter.
* :func:`check_wecom_callback_requirements` -- dependency probe.
* :data:`DEFAULT_HOST`, :data:`DEFAULT_PORT`, :data:`DEFAULT_PATH`,
  :data:`ACCESS_TOKEN_TTL_SECONDS`, :data:`MESSAGE_DEDUP_TTL_SECONDS`.
"""

import asyncio
import logging
import socket as _socket
import threading
import time
from typing import Any, Dict, List, Optional

# WeCom callback bodies are attacker-controlled until the signature check
# passes, so they are parsed through defusedxml to block entity-expansion and
# external entity expansion. `fromstring` is a drop-in for the stdlib call;
# the response XML built in wecom_crypto.py is never parsed back here.
import defusedxml.ElementTree as ET
DEFUSEDXML_AVAILABLE = True

from aiohttp import web
AIOHTTP_AVAILABLE = True

import httpx
HTTPX_AVAILABLE = True

from encre.gateway.config import Platform, PlatformConfig
from encre.gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from .wecom_crypto import WXBizMsgCrypt, WeComCryptoError

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8645
DEFAULT_PATH = "/wecom/callback"
# Cap pre-auth request bodies. WeCom callbacks are small encrypted XML
# envelopes (media is delivered out-of-band by MediaId, never inline), so
# 64 KiB is ample for any legitimate message while bounding the work an
# unauthenticated POST can force before signature verification runs.
_MAX_BODY = 65_536
# WeCom documents an access-token lifetime of about 7200 seconds; use the
# same value as a safe default when the token endpoint omits `expires_in`.
ACCESS_TOKEN_TTL_SECONDS = 7200
# Duplicate callbacks inside this window (WeCom retries on timeout) are
# dropped. Doubles as the eviction horizon for the seen-message-id cache.
MESSAGE_DEDUP_TTL_SECONDS = 300


def check_wecom_callback_requirements() -> bool:
    """Report whether every optional dependency the callback adapter needs is installed.

    The adapter needs ``aiohttp`` for the HTTP server, ``httpx`` for the
    outbound token and message calls, and ``defusedxml`` for parsing
    unauthenticated callback bodies. Losing any one of them makes the adapter
    unusable, so the platform is registered as unavailable rather than failing
    partway through startup.

    Returns:
        bool: ``True`` when aiohttp, httpx and defusedxml are all importable.
    """
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE and DEFUSEDXML_AVAILABLE


class WecomCallbackAdapter(BasePlatformAdapter):
    """WeCom self-built-app callback adapter backed by a local HTTP server.

    Responsibilities:

    * Bind an aiohttp server on a configurable host, port and path that
      receives WeCom's encrypted XML callbacks and URL-verification GETs.
    * Verify each callback signature, decrypt the body, and turn it into a
      :class:`MessageEvent` placed on an internal asyncio queue.
    * Deduplicate retried callbacks and remember which app each user belongs
      to so outbound replies route to the right app and agent id.
    * Send agent replies proactively through WeCom's ``message/send`` API,
      managing a per-app access-token cache with lazy refresh.

    Configuration comes from :class:`PlatformConfig.extra`, which may hold
    either a single-app mapping (``corp_id``, ``corp_secret``, ``agent_id``,
    ``token``, ``encoding_aes_key``) or a list under ``apps``.

    Lifecycle: :meth:`__init__` parses config only and never opens sockets, so
    an adapter may be constructed even when its dependencies are missing;
    :meth:`connect` performs the real checks, binds the port and starts the
    poll loop; :meth:`disconnect` cancels the poll loop and releases both the
    aiohttp runner and the httpx client.

    Thread and async safety: ``_seen_messages`` is touched from the HTTP
    handler task only, but is guarded by a threading lock because aiohttp may
    run the request handler on a different executor thread; the access-token
    cache is only ever read and written from the single event-loop thread.

    Attributes:
        _apps: One dict per configured self-built app.
        _runner, _site, _app: The aiohttp server components, ``None`` until
            ``connect``.
        _http_client: Shared httpx client used for all outbound WeCom calls.
        _message_queue: Inbound events waiting to be dispatched.
        _seen_messages: ``{message_id: monotonic-free wall clock seconds}``
            deduplication cache, bounded and time-windowed.
        _user_app_map: ``{corp_id:user_id: app_name}`` routing table for
            outbound replies.
        _access_tokens: ``{app_name: {"token", "expires_at"}}`` token cache.
    """

    def __init__(self, config: PlatformConfig):
        """Parse configuration and initialise empty runtime state.

        No sockets are opened here. When ``PlatformConfig.extra`` is empty or
        holds neither an ``apps`` list nor a ``corp_id``, ``_apps`` ends up
        empty and :meth:`connect` reports failure instead of binding a port
        for nothing.

        Args:
            config: The platform configuration; transport and credential
                fields are read from ``config.extra``.
        """
        super().__init__(config, Platform.WECOM_CALLBACK)
        extra = config.extra or {}
        self._host = str(extra.get("host") or DEFAULT_HOST)
        self._port = int(extra.get("port") or DEFAULT_PORT)
        self._path = str(extra.get("path") or DEFAULT_PATH)
        self._apps: List[Dict[str, Any]] = self._normalize_apps(extra)
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._app: Optional[web.Application] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._message_queue: asyncio.Queue[MessageEvent] = asyncio.Queue()
        self._poll_task: Optional[asyncio.Task] = None
        self._seen_messages: Dict[str, float] = {}
        self._seen_lock = threading.Lock()
        self._user_app_map: Dict[str, str] = {}
        self._access_tokens: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # App normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _user_app_key(corp_id: str, user_id: str) -> str:
        """Build the cross-corp scoping key for a user.

        A WeCom user id is only unique inside a single corp, so chat ids carry
        the corp id as a prefix. Single-app deployments leave ``corp_id``
        empty and get a bare user id back.

        Args:
            corp_id: The WeCom corp id, or an empty string when not scoping.
            user_id: The WeCom user id.

        Returns:
            str: ``"{corp_id}:{user_id}"`` when a corp id is present, else the
            bare ``user_id``.
        """
        return f"{corp_id}:{user_id}" if corp_id else user_id

    @staticmethod
    def _normalize_apps(extra: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Coerce the raw ``extra`` mapping into a list of app dictionaries.

        Two config shapes are supported. When ``extra["apps"]`` is a non-empty
        list of dicts, those are copied back as-is. Otherwise a single-app
        mapping expressed through top-level keys (``corp_id``,
        ``corp_secret``, ``agent_id``, ``token``, ``encoding_aes_key``) is
        wrapped into one dict. Anything else yields an empty list, which makes
        :meth:`connect` refuse to start.

        Args:
            extra: The ``PlatformConfig.extra`` mapping.

        Returns:
            List[Dict[str, Any]]: One entry per configured self-built app.
        """
        apps = extra.get("apps")
        if isinstance(apps, list) and apps:
            return [dict(app) for app in apps if isinstance(app, dict)]
        if extra.get("corp_id"):
            return [
                {
                    "name": extra.get("name") or "default",
                    "corp_id": extra.get("corp_id", ""),
                    "corp_secret": extra.get("corp_secret", ""),
                    "agent_id": str(extra.get("agent_id", "")),
                    "token": extra.get("token", ""),
                    "encoding_aes_key": extra.get("encoding_aes_key", ""),
                }
            ]
        return []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Bind the HTTP server and start draining inbound callbacks.

        Args:
            is_reconnect: Forwarded by :class:`encre.gateway.run.GatewayRunner`
                on every retry, per the
                :class:`encre.gateway.platforms.base.BasePlatformAdapter`
                contract. A callback adapter keeps no server-side queue to
                preserve, so the value is accepted and discarded. The keyword
                must nevertheless remain in the signature, or the reconnect
                watcher raises ``TypeError`` and the platform stays offline
                with no visible error.

        Returns:
            bool: ``True`` when the server is listening, ``False`` when no
            apps are configured, a dependency is missing, the port is already
            in use, or startup failed for any other reason.
        """
        # Callback adapters have no server-side queue to preserve across a
        # reconnect, so the flag is accepted and dropped. It must stay in the
        # signature or GatewayRunner's reconnect call raises TypeError and the
        # platform silently stays offline.
        del is_reconnect
        if not self._apps:
            logger.warning("[WecomCallback] No callback apps configured")
            return False
        if not check_wecom_callback_requirements():
            logger.warning("[WecomCallback] aiohttp/httpx not installed")
            return False

        # Fail fast on an occupied port: binding is what would otherwise raise
        # an opaque OSError deep inside the aiohttp site startup.
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("127.0.0.1", self._port))
            logger.error("[WecomCallback] Port %d already in use", self._port)
            return False
        except (ConnectionRefusedError, OSError):
            pass

        try:
            # Tighter keepalive so idle CLOSE_WAIT connections drain promptly
            # instead of pinning file descriptors for the default lifetime.
            from encre.gateway.platforms._http_client_limits import platform_httpx_limits
            self._http_client = httpx.AsyncClient(timeout=20.0, limits=platform_httpx_limits())
            # client_max_size rejects oversized bodies at the aiohttp layer
            # with 413 -- before the handler runs and before any signature
            # work -- so an attacker cannot force large allocs pre-auth.
            self._app = web.Application(client_max_size=_MAX_BODY)
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_get(self._path, self._handle_verify)
            self._app.router.add_post(self._path, self._handle_callback)
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
            self._poll_task = asyncio.create_task(self._poll_loop())
            self._mark_connected()
            logger.info(
                "[WecomCallback] HTTP server listening on %s:%s%s",
                self._host, self._port, self._path,
            )
            for app in self._apps:
                # Warm each app's token cache so the first real reply is not
                # delayed by an authentication round trip. Failures here are
                # non-fatal: _get_access_token retries on demand.
                try:
                    await self._refresh_access_token(app)
                except Exception as exc:
                    logger.warning(
                        "[WecomCallback] Initial token refresh failed for app '%s': %s",
                        app.get("name", "default"), exc,
                    )
            return True
        except Exception:
            await self._cleanup()
            logger.exception("[WecomCallback] Failed to start")
            return False

    async def disconnect(self) -> None:
        """Stop the poll loop, close the server, and release all resources.

        Cancelling the poll task first prevents new events being enqueued while
        the server is torn down. Both the runner and the httpx client are
        closed independently inside :meth:`_cleanup` so one failing cannot leak
        the other.

        Returns:
            None
        """
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await self._cleanup()
        self._mark_disconnected()
        logger.info("[WecomCallback] Disconnected")

    async def _cleanup(self) -> None:
        """Tear down the aiohttp runner, site, application and httpx client.

        Idempotent, so it is safe both on a failed :meth:`connect` and on
        :meth:`disconnect`. Each resource is nulled as it goes so a partially
        constructed server never keeps a stale reference alive.

        Returns:
            None
        """
        self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    # ------------------------------------------------------------------
    # Outbound: proactive send via access-token API
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Deliver a text reply to a WeCom user through ``message/send``.

        The recipient is the user id extracted from ``chat_id`` (everything
        after an optional ``corp_id:`` prefix). Callback mode is inherently
        one-to-one, so there is no group fan-out here. On the first attempt, if
        WeCom rejects the access token (errcode 40001 unknown token, 42001
        expired token), the cached token is evicted and the send is retried
        exactly once with a freshly fetched token.

        Args:
            chat_id: Scoped chat id, optionally ``corp_id:user_id``.
            content: Plain-text body, truncated to 2048 characters by the
                WeCom text-message limit.
            reply_to: Optional upstream message id being answered. Unused here;
                the parameter exists to match the base signature.
            metadata: Optional per-call data. Unused here.

        Returns:
            SendResult: Success or failure, with the WeCom ``msgid`` and the
            full response body attached on success.
        """
        app = self._resolve_app_for_chat(chat_id)
        touser = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        try:
            payload = {
                "touser": touser,
                "msgtype": "text",
                "agentid": int(str(app.get("agent_id") or 0)),
                "text": {"content": content[:2048]},
                "safe": 0,
            }
            for _attempt in range(2):
                token = await self._get_access_token(app)
                resp = await self._http_client.post(
                    f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
                    json=payload,
                )
                data = resp.json()
                errcode = data.get("errcode")
                if errcode in {40001, 42001} and _attempt == 0:
                    # WeCom rejected the token, so evict the cached entry to
                    # force a fresh fetch on the retry. Second-attempt token
                    # errors fall through and are reported as a send failure.
                    logger.warning(
                        "[WecomCallback] Token rejected for app '%s' (errcode=%s), refreshing",
                        app.get("name", "default"), errcode,
                    )
                    self._access_tokens.pop(app["name"], None)
                    continue
                if errcode != 0:
                    return SendResult(success=False, error=str(data))
                return SendResult(
                    success=True,
                    message_id=str(data.get("msgid", "")),
                    raw_response=data,
                )
            return SendResult(success=False, error="send failed after token refresh")
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    def _resolve_app_for_chat(self, chat_id: str) -> Dict[str, Any]:
        """Pick the app that should deliver a reply to ``chat_id``.

        Uses the routing table filled in while handling inbound callbacks. A
        legacy bare user id with no corp prefix is resolved by looking for a
        unique ``*:user_id`` match. When nothing is known the first configured
        app is used, which is correct for all single-app deployments.

        Args:
            chat_id: Scoped chat id (``corp_id:user_id``) or bare ``user_id``.

        Returns:
            Dict[str, Any]: The matched app dict, or ``self._apps[0]`` as a
            last resort.
        """
        app_name = self._user_app_map.get(chat_id)
        if not app_name and ":" not in chat_id:
            # Bare user_id with no corp prefix: accept the answer only when
            # exactly one app claims the user, to avoid mis-routing.
            matching = [k for k in self._user_app_map if k.endswith(f":{chat_id}")]
            if len(matching) == 1:
                app_name = self._user_app_map.get(matching[0])
        app = self._get_app_by_name(app_name) if app_name else None
        return app or self._apps[0]

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return minimal chat metadata for a callback-mode conversation.

        Callback mode is always a direct message between the user and one
        self-built app, so the shape is fixed and the chat id doubles as the
        display name.

        Args:
            chat_id: The scoped chat id.

        Returns:
            Dict[str, Any]: ``{"name": chat_id, "type": "dm"}``.
        """
        return {"name": chat_id, "type": "dm"}

    # ------------------------------------------------------------------
    # Inbound: HTTP callback handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        """GET ``/health`` liveness probe.

        Deliberately performs no adapter work, so orchestrators can distinguish
        "the HTTP server is up" from "the WeCom credentials are valid".

        Args:
            request: The incoming aiohttp request (unused).

        Returns:
            web.Response: A JSON ``{"status": "ok", "platform": ...}`` body.
        """
        return web.json_response({"status": "ok", "platform": "wecom_callback"})

    async def _handle_verify(self, request: web.Request) -> web.Response:
        """GET callback path -- WeCom's URL-verification handshake.

        WeCom sends ``msg_signature``, ``timestamp``, ``nonce`` and an
        encrypted ``echostr`` as query parameters. Each configured app's crypto
        helper is tried in turn until one verifies and decrypts ``echostr``;
        that plaintext is returned verbatim, which proves ownership of the URL.

        Args:
            request: The aiohttp request carrying the handshake query params.

        Returns:
            web.Response: The decrypted ``echostr`` as plain text on success,
            or 403 when no configured app verifies the signature.
        """
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")
        echostr = request.query.get("echostr", "")
        for app in self._apps:
            try:
                crypt = self._crypt_for_app(app)
                plain = crypt.verify_url(msg_signature, timestamp, nonce, echostr)
                return web.Response(text=plain, content_type="text/plain")
            except Exception:
                continue
        return web.Response(status=403, text="signature verification failed")

    async def _handle_callback(self, request: web.Request) -> web.Response:
        """POST callback path -- receive one encrypted message callback.

        Reads the body after an explicit size guard, then walks the configured
        apps attempting to verify and decrypt it. The first app whose signature
        verifies wins; its plaintext is converted to a
        :class:`MessageEvent`, deduplicated, and enqueued. The handler records
        which app the sender belongs to before returning, so the reply can be
        routed later. The callback is acknowledged with ``success``
        immediately regardless of downstream processing, because WeCom retries
        on any non-2xx or slow response.

        Args:
            request: The aiohttp request with the encrypted XML body.

        Returns:
            web.Response: ``success`` (200) when handled or deduplicated, 413
            when the payload exceeds the size cap, or 400 when no app can
            decrypt it.
        """
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")
        # Second, in-handler size guard on top of client_max_size: rejects an
        # oversized payload before any XML parse or signature work (DoS, zip
        # bombs).
        body_bytes = await request.read()
        if len(body_bytes) > _MAX_BODY:
            logger.warning("[WecomCallback] Payload too large (%d bytes) -- rejected", len(body_bytes))
            return web.Response(status=413, text="payload too large")
        body = body_bytes.decode("utf-8", errors="replace")

        for app in self._apps:
            try:
                decrypted = self._decrypt_request(
                    app, body, msg_signature, timestamp, nonce,
                )
                event = self._build_event(app, decrypted)
                if event is not None:
                    # WeCom retries callbacks whose acknowledgement was missed,
                    # which would otherwise deliver one user message to the
                    # agent multiple times.
                    if event.message_id:
                        now = time.time()
                        with self._seen_lock:
                            if event.message_id in self._seen_messages:
                                if now - self._seen_messages[event.message_id] < MESSAGE_DEDUP_TTL_SECONDS:
                                    logger.debug("[WecomCallback] Duplicate MsgId %s, skipping", event.message_id)
                                    return web.Response(text="success", content_type="text/plain")
                                del self._seen_messages[event.message_id]
                            self._seen_messages[event.message_id] = now
                            if len(self._seen_messages) > 2000:
                                # Hard cap on the dedup cache: evict anything
                                # outside the TTL window once past the limit.
                                cutoff = now - MESSAGE_DEDUP_TTL_SECONDS
                                self._seen_messages = {k: v for k, v in self._seen_messages.items() if v > cutoff}
                    # Record the owning app so outbound replies route to the
                    # right agent id and access token.
                    if event.source and event.source.user_id:
                        map_key = self._user_app_key(
                            str(app.get("corp_id") or ""), event.source.user_id,
                        )
                        self._user_app_map[map_key] = app["name"]
                    await self._message_queue.put(event)
                # Acknowledge now; the agent's reply is sent later through the
                # proactive message/send API.
                return web.Response(text="success", content_type="text/plain")
            except WeComCryptoError:
                # Signature or decrypt failure: this app does not own the
                # callback, keep trying the next one.
                continue
            except Exception:
                logger.exception("[WecomCallback] Error handling message")
                break
        return web.Response(status=400, text="invalid callback payload")

    async def _poll_loop(self) -> None:
        """Drain the inbound queue and dispatch each event to the gateway.

        Runs until cancelled. Each dequeued :class:`MessageEvent` is handed to
        ``handle_message`` in a tracked background task so a slow agent turn
        cannot stall the queue and cause WeCom to retry the callback.

        Returns:
            None
        """
        while True:
            event = await self._message_queue.get()
            try:
                task = asyncio.create_task(self.handle_message(event))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except Exception:
                logger.exception("[WecomCallback] Failed to enqueue event")

    # ------------------------------------------------------------------
    # XML / crypto helpers
    # ------------------------------------------------------------------

    def _decrypt_request(
        self, app: Dict[str, Any], body: str,
        msg_signature: str, timestamp: str, nonce: str,
    ) -> str:
        """Parse the envelope and decrypt its ``Encrypt`` child.

        The envelope is plain (unencrypted) XML that WeCom wraps the ciphertext
        in; only the ``Encrypt`` element is encrypted. Extraction is done with
        the defused parser because this runs before authentication.

        Args:
            app: The app dict whose credentials to use.
            body: The raw callback XML body as text.
            msg_signature: WeCom-supplied signature query parameter.
            timestamp: WeCom-supplied timestamp query parameter.
            nonce: WeCom-supplied nonce query parameter.

        Returns:
            str: The decrypted plaintext XML.

        Raises:
            WeComCryptoError: Signature verification or decryption failed.
        """
        root = ET.fromstring(body)
        encrypt = root.findtext("Encrypt", default="")
        crypt = self._crypt_for_app(app)
        return crypt.decrypt(msg_signature, timestamp, nonce, encrypt).decode("utf-8")

    def _build_event(self, app: Dict[str, Any], xml_text: str) -> Optional[MessageEvent]:
        """Convert decrypted WeCom XML into an Encre :class:`MessageEvent`.

        Lifecycle callbacks such as ``enter_agent`` and ``subscribe`` carry no
        content; they are acknowledged silently by returning ``None``. Only
        ``text`` and ``event`` message types are materialised, everything else
        is ignored. When WeCom omits a message id (common for event messages)
        one is synthesised from the sender and create time so deduplication
        still has a stable key.

        Args:
            app: The app dict that received the message; supplies the corp id
                fallback when the payload omits ``ToUserName``.
            xml_text: The decrypted XML plaintext.

        Returns:
            Optional[MessageEvent]: The constructed event, or ``None`` when
            the message should be acknowledged but ignored.
        """
        root = ET.fromstring(xml_text)
        msg_type = (root.findtext("MsgType") or "").lower()
        # Nothing to do for these lifecycle events; just ack them upstream.
        if msg_type == "event":
            event_name = (root.findtext("Event") or "").lower()
            if event_name in {"enter_agent", "subscribe"}:
                return None
        if msg_type not in {"text", "event"}:
            return None

        user_id = root.findtext("FromUserName", default="")
        corp_id = root.findtext("ToUserName", default=app.get("corp_id", ""))
        scoped_chat_id = self._user_app_key(corp_id, user_id)
        content = root.findtext("Content", default="").strip()
        if not content and msg_type == "event":
            content = "/start"
        msg_id = (
            root.findtext("MsgId")
            or f"{user_id}:{root.findtext('CreateTime', default='0')}"
        )
        source = self.build_source(
            chat_id=scoped_chat_id,
            chat_name=user_id,
            chat_type="dm",
            user_id=user_id,
            user_name=user_id,
        )
        return MessageEvent(
            text=content,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=xml_text,
            message_id=msg_id,
        )

    def _crypt_for_app(self, app: Dict[str, Any]) -> WXBizMsgCrypt:
        """Build a crypto helper bound to one app's credentials.

        A fresh instance is created per callback rather than cached because
        construction is cheap (one base64 decode) and the object holds no
        state that would benefit from reuse.

        Args:
            app: The app dict holding ``token``, ``encoding_aes_key`` and
                ``corp_id``.

        Returns:
            WXBizMsgCrypt: A helper bound to those credentials.
        """
        return WXBizMsgCrypt(
            token=str(app.get("token") or ""),
            encoding_aes_key=str(app.get("encoding_aes_key") or ""),
            receive_id=str(app.get("corp_id") or ""),
        )

    def _get_app_by_name(self, name: Optional[str]) -> Optional[Dict[str, Any]]:
        """Look up a configured app by its ``name`` field.

        Args:
            name: The app name to find, or ``None``.

        Returns:
            Optional[Dict[str, Any]]: The matching app dict, or ``None``.
        """
        if not name:
            return None
        for app in self._apps:
            if app.get("name") == name:
                return app
        return None

    # ------------------------------------------------------------------
    # Access-token management
    # ------------------------------------------------------------------

    async def _get_access_token(self, app: Dict[str, Any]) -> str:
        """Return a valid access token for ``app``, refreshing a stale one.

        Reads the in-memory cache first. A cached token expiring within the
        next 60 seconds is treated as stale so the send path never presents a
        token WeCom is about to reject.

        Args:
            app: The app dict whose token is requested.

        Returns:
            str: A usable access token.
        """
        cached = self._access_tokens.get(app["name"])
        now = time.time()
        if cached and cached.get("expires_at", 0) > now + 60:
            return cached["token"]
        return await self._refresh_access_token(app)

    async def _refresh_access_token(self, app: Dict[str, Any]) -> str:
        """Fetch a fresh access token from WeCom's ``gettoken`` endpoint.

        The token is cached keyed by app name with an absolute expiry so
        concurrent senders reuse one fetch. The 60-second margin in
        :meth:`_get_access_token` plus this cache is what keeps outbound sends
        free of per-message authentication round trips.

        Args:
            app: The app dict providing ``corp_id`` and ``corp_secret``.

        Returns:
            str: The newly issued access token.

        Raises:
            RuntimeError: WeCom returned a non-zero ``errcode``.
        """
        resp = await self._http_client.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={
                "corpid": app.get("corp_id"),
                "corpsecret": app.get("corp_secret"),
            },
        )
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom token refresh failed: {data}")
        token = data["access_token"]
        expires_in = int(data.get("expires_in", ACCESS_TOKEN_TTL_SECONDS))
        self._access_tokens[app["name"]] = {
            "token": token,
            "expires_at": time.time() + expires_in,
        }
        logger.info(
            "[WecomCallback] Token refreshed for app '%s' (corp=%s), expires in %ss",
            app.get("name", "default"),
            app.get("corp_id", ""),
            expires_in,
        )
        return token
