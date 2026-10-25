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

"""BlueBubbles iMessage platform adapter.

Bridges Apple's iMessage onto the Encre gateway by speaking to a locally
hosted BlueBubbles server (the macOS app that exposes iMessage as a REST
API). iMessage is not reachable from a Linux box directly, so the adapter
never talks to Apple itself; everything flows through the BlueBubbles HTTP
server that the operator has installed on a Mac with Messages running.

Direction of data flow:

* Outbound: the agent produces text or media,
  :class:`encre.gateway.stream_consumer.GatewayStreamConsumer` calls
  :meth:`BlueBubblesAdapter.send` (or a ``send_*`` variant), and the
  adapter resolves the target to a chat GUID and posts it to the
  BlueBubbles REST API.
* Inbound: the adapter hosts a small aiohttp HTTP server on a localhost
  port, registers that URL with BlueBubbles at connect time, and receives
  ``new-message`` / ``updated-message`` events as POSTs. Each event is
  normalised into a
  :class:`encre.gateway.platforms.base.MessageEvent` and dispatched
  through :meth:`BasePlatformAdapter.handle_message`, which resolves the
  session, runs the Rust-core agent, and pushes deltas back through
  :meth:`send`.

BlueBubbles payloads are inconsistent in a way that matters here: field
names vary by server version (``chatGuid`` vs ``chat_guid``), the record
may live under ``data``, ``data[0]``, ``message``, or at the top level,
and attachment bytes only ever travel over the REST API -- never over the
webhook. See :meth:`BlueBubblesAdapter._handle_webhook` and
:meth:`BlueBubblesAdapter._download_attachment` for the exact mapping.

Public API:
    * :class:`BlueBubblesAdapter` -- the
      :class:`encre.gateway.platforms.base.BasePlatformAdapter` subclass
      registered under the name ``bluebubbles``
    * :func:`check_bluebubbles_requirements` -- dependency probe consumed
      by :mod:`encre.gateway.platform_registry`
"""

import asyncio
import json
import logging
import os
import re
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp  # noqa: F401
import httpx

from encre.gateway.config import Platform, PlatformConfig
from encre.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
    cache_audio_from_bytes,
    cache_document_from_bytes,
)
from encre.gateway.platforms.helpers import strip_markdown

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Local bind address for the inbound webhook listener. iMessage traffic
#: is relayed by a co-located BlueBubbles server, so the listener never
#: needs to be reachable from outside the host.
DEFAULT_WEBHOOK_HOST = "127.0.0.1"
#: BlueBubbles webhook events are small JSON or form payloads; attachment
#: bytes come back over the REST API rather than through the webhook. One
#: MiB is generous headroom for a large attachment manifest while still
#: preventing an oversized or chunked body from being buffered without
#: limit.
_WEBHOOK_MAX_BODY_BYTES = 1_048_576
#: Default inbound webhook port. Kept distinct from the generic webhook
#: adapter's 8644 so both can run in one gateway process.
DEFAULT_WEBHOOK_PORT = 8645
DEFAULT_WEBHOOK_PATH = "/bluebubbles-webhook"
#: BlueBubbles caps a single text message part at 4000 characters.
MAX_TEXT_LENGTH = 4000

#: Default group wake words. iMessage exposes no stable bot identity of
#: the kind Slack gives with ``<@U123>``, Telegram with ``@botname``, or
#: Matrix with an MXID, so a group message cannot be matched against the
#: agent's own address. These conservative phrases give
#: ``require_mention: true`` a usable one-line enablement path for users
#: who do not configure their own patterns. The ``(?<![\w@])`` guard keeps
#: a wake word from firing inside a longer word or handle.
DEFAULT_MENTION_PATTERNS = [
    r"(?<![\w@])@?encre\s+agent\b[,:\-]?",
    r"(?<![\w@])@?encre\b[,:\-]?",
]

#: Tapback reaction codes, keyed by BlueBubbles'
#: ``associatedMessageType`` value. The 2000 range is "tapback added" and
#: the 3000 range is "tapback removed"; the value is the same iMessage
#: reaction either way, only the direction differs. These arrive as
#: message events and must be filtered out, otherwise every tapback
#: triggers an agent turn.
_TAPBACK_ADDED = {
    2000: "love", 2001: "like", 2002: "dislike",
    2003: "laugh", 2004: "emphasize", 2005: "question",
}
_TAPBACK_REMOVED = {
    3000: "love", 3001: "like", 3002: "dislike",
    3003: "laugh", 3004: "emphasize", 3005: "question",
}

#: The only webhook event types that carry a user message. Anything else
#: (typing, read receipts, chat changes, attachment transfers) is
#: acknowledged and dropped.
_MESSAGE_EVENTS = {"new-message", "message", "updated-message"}

#: Log redaction patterns. iMessage identifies people by phone number and
#: email, which are personal data, so both are scrubbed before any
#: payload-derived value reaches agent.log.
_PHONE_RE = re.compile(r"\+?\d{7,15}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

#: LRU cap for resolved chat-GUID lookups. GUID resolution is a network
#: round trip per miss, and the same contact is contacted repeatedly, so
#: caching is what keeps :meth:`send` latency flat.
_GUID_CACHE_SIZE = 500


def _redact(text: str) -> str:
    """Mask phone numbers and email addresses before logging.

    Args:
        text: Log fragment that may embed a contact handle.

    Returns:
        The input with each matched handle replaced by ``[REDACTED]``.
        Matching is greedy across both digit runs (international numbers)
        and email shapes, applied in that order so an email is consumed
        before the digit pattern can eat its local part.
    """
    text = _PHONE_RE.sub("[REDACTED]", text)
    text = _EMAIL_RE.sub("[REDACTED]", text)
    return text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_bluebubbles_requirements() -> bool:
    """Report whether the optional imports for this adapter are installed.

    Consumed by :mod:`encre.gateway.platform_registry` through the
    ``check_fn`` of the ``bluebubbles`` entry so a host missing the extra
    is reported as unsupported rather than failing at import time.

    Returns:
        ``True`` when both ``aiohttp`` (inbound webhook server) and
        ``httpx`` (outbound REST client) import cleanly.
    """
    return True


def _normalize_server_url(raw: str) -> str:
    """Normalise a user-supplied BlueBubbles server URL.

    Operators commonly paste a bare ``macbook.local:8080`` without a
    scheme, so a missing ``http://``/``https://`` is added; trailing
    slashes are stripped so callers can concatenate a path segment
    without producing ``//``.

    Args:
        raw: Raw value from ``extra.server_url`` or
            ``BLUEBUBBLES_SERVER_URL``; may be ``None``.

    Returns:
        A scheme-bearing, slash-free-of-trailing-slash URL, or ``""`` when
        the input is empty. An empty return value is what makes
        :meth:`connect` refuse to start instead of crashing later on a
        malformed URL.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, flags=re.I):
        value = f"http://{value}"
    return value.rstrip("/")


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class BlueBubblesAdapter(BasePlatformAdapter):
    """Encre adapter for iMessage, fronted by a local BlueBubbles server.

    Lifecycle: constructed from a :class:`encre.gateway.config.PlatformConfig`,
    :meth:`connect` opens the outbound HTTP client, probes the server, and
    starts the inbound webhook listener; :meth:`disconnect` unregisters
    the webhook, closes the client, and tears down the aiohttp runner.

    The adapter is a single async citizen -- every method is a coroutine or
    is called only from within the gateway event loop. State is not
    lock-protected because there is exactly one event loop; the
    :attr:`_guid_cache` is an :class:`collections.OrderedDict` used purely
    as an LRU and is touched only inside coroutines.

    Outbound capability note: ``SUPPORTS_MESSAGE_EDITING`` is ``False``
    because BlueBubbles exposes no reliable edit endpoint, and
    ``splits_long_messages`` is ``True`` because
    :meth:`encre.gateway.stream_consumer.GatewayStreamConsumer` otherwise
    would treat the 4000-character cap as a vendor error rather than as a
    normalisation duty.

    Attributes:
        server_url: Normalised BlueBubbles REST base URL.
        password: Shared secret for the REST API and the inbound webhook.
            BlueBubbles cannot send custom headers on webhook POSTs, so the
            same password doubles as the webhook token in the query string.
        webhook_host / webhook_port / webhook_path: Bind target of the
            inbound listener.
        send_read_receipts: Whether accepted inbound messages are marked
            read back to the sender.
        require_mention: Gate group messages behind a wake word. iMessage
            has no bot identity, so this is the only lever that keeps the
            agent quiet in a group.
        _mention_patterns: Compiled wake-word regexes; see
            :meth:`_compile_mention_patterns`.
        client: Outbound :class:`httpx.AsyncClient`; ``None`` until
            :meth:`connect` succeeds.
        _runner: Inbound aiohttp :class:`~aiohttp.web.AppRunner`.
        _private_api_enabled: Server feature flag. The ``private-api``
            method is required for replies, typing and read receipts.
        _helper_connected: Whether the macOS Messages helper is attached.
            Without it, every private-api call fails, so the flags gate
            three families of calls.
        _guid_cache: ``chat identifier -> chat GUID`` LRU.
    """

    platform = Platform.BLUEBUBBLES
    SUPPORTS_MESSAGE_EDITING = False
    MAX_MESSAGE_LENGTH = MAX_TEXT_LENGTH
    splits_long_messages = True

    def __init__(self, config: PlatformConfig):
        """Configure the adapter from the ``bluebubbles`` platform config.

        Every setting is read config-first and environment-second, so a
        ``config.yaml`` entry always wins over a stale variable in a
        long-lived gateway process.

        Args:
            config: The ``bluebubbles`` platform block. Recognised keys in
                ``config.extra`` are ``server_url``, ``password``,
                ``webhook_host``, ``webhook_port``, ``webhook_path``,
                ``send_read_receipts``, ``require_mention`` and
                ``mention_patterns``; each also has a ``BLUEBUBBLES_*``
                environment equivalent.

        Returns:
            Nothing. Nothing is opened here: :meth:`connect` performs all
            I/O, so construction cannot leave a half-open client behind.

        Note:
            The ``password`` is used for two different purposes -- REST
            authentication and inbound webhook authentication -- because
            BlueBubbles will not send custom headers on webhook POSTs. See
            :attr:`password`.
        """
        super().__init__(config, Platform.BLUEBUBBLES)
        extra = config.extra or {}
        self.server_url = _normalize_server_url(
            extra.get("server_url") or os.getenv("BLUEBUBBLES_SERVER_URL", "")
        )
        self.password = extra.get("password") or os.getenv("BLUEBUBBLES_PASSWORD", "")
        self.webhook_host = (
            extra.get("webhook_host")
            or os.getenv("BLUEBUBBLES_WEBHOOK_HOST", DEFAULT_WEBHOOK_HOST)
        )
        self.webhook_port = int(
            extra.get("webhook_port")
            or os.getenv("BLUEBUBBLES_WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT))
        )
        self.webhook_path = (
            extra.get("webhook_path")
            or os.getenv("BLUEBUBBLES_WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH)
        )
        # BlueBubbles registers the path as given, so a relative value would
        # produce a URL it cannot reach. Prefixing is cheaper than failing.
        if not str(self.webhook_path).startswith("/"):
            self.webhook_path = f"/{self.webhook_path}"
        self.send_read_receipts = bool(extra.get("send_read_receipts", True))
        _require_mention = extra.get("require_mention")
        if _require_mention is None:
            _require_mention = os.getenv("BLUEBUBBLES_REQUIRE_MENTION")
        # str() so a YAML boolean and an env string like "1" compare the same.
        self.require_mention = str(_require_mention).strip().lower() in {"true", "1", "yes", "on"}
        self._mention_patterns = self._compile_mention_patterns(
            extra["mention_patterns"]
            if "mention_patterns" in extra
            else os.getenv("BLUEBUBBLES_MENTION_PATTERNS")
        )
        self.client: Optional[httpx.AsyncClient] = None
        self._runner = None
        self._private_api_enabled: Optional[bool] = None
        self._helper_connected: bool = False
        self._guid_cache: OrderedDict[str, str] = OrderedDict()

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _api_url(self, path: str) -> str:
        """Build an authenticated BlueBubbles REST URL.

        BlueBubbles authenticates by appending the password as a query
        parameter rather than via an Authorization header, so the password
        is percent-encoded and joined with ``?`` or ``&`` depending on
        whether the path already carries a query string.

        Args:
            path: API path such as ``/api/v1/message/text``.

        Returns:
            The fully qualified URL with the password appended. The
            password never appears in any log emitted by this method.
        """
        sep = "&" if "?" in path else "?"
        return f"{self.server_url}{path}{sep}password={quote(self.password, safe='')}"

    @staticmethod
    def _compile_mention_patterns(raw: Any) -> List[re.Pattern]:
        """Compile group-mention wake words from config or the environment.

        Three input shapes are supported so the same setting works from
        ``config.yaml`` (a real list) and from an environment variable
        (where it must be a string, either JSON or delimited):

        * ``None`` -- use :data:`DEFAULT_MENTION_PATTERNS`.
        * ``str`` -- try a JSON array first, then fall back to splitting
          on newlines and commas.
        * ``list`` -- used as-is.
        * anything else -- wrapped in a single-element list.

        Args:
            raw: Raw value from ``extra.mention_patterns`` or
                ``BLUEBUBBLES_MENTION_PATTERNS``.

        Returns:
            Compiled, case-insensitive patterns. Blank entries are dropped
            and an invalid regex is skipped with a warning rather than
            raising: a typo in one pattern must not prevent the whole
            adapter from starting, and it must not silently disable group
            gating either -- the remaining patterns still apply.
        """
        if raw is None:
            patterns = list(DEFAULT_MENTION_PATTERNS)
        elif isinstance(raw, str):
            text = raw.strip()
            try:
                loaded = json.loads(text) if text else []
            except Exception:
                loaded = None
            patterns = loaded if isinstance(loaded, list) else [
                part.strip()
                for line in text.splitlines()
                for part in line.split(",")
            ]
        elif isinstance(raw, list):
            patterns = raw
        else:
            patterns = [raw]

        compiled: List["re.Pattern"] = []
        for pattern in patterns:
            text = str(pattern).strip()
            if not text:
                continue
            try:
                compiled.append(re.compile(text, re.IGNORECASE))
            except re.error as exc:
                logger.warning("[bluebubbles] Invalid mention pattern %r: %s", text, exc)
        return compiled

    def _message_matches_mention_patterns(self, text: str) -> bool:
        """Test whether a group message contains any configured wake word.

        Args:
            text: Inbound message body, before mention stripping.

        Returns:
            ``True`` when at least one pattern matches anywhere in the
            text. Returns ``False`` when there is no text or no patterns
            at all, which is the safe direction: an unconfigured adapter
            that has ``require_mention`` on cannot accidentally accept every
            group message.
        """
        if not text or not self._mention_patterns:
            return False
        return any(pattern.search(text) for pattern in self._mention_patterns)

    def _clean_mention_text(self, text: str) -> str:
        """Strip a leading BlueBubbles wake word before dispatch.

        Custom mention patterns are regular expressions and may match
        anywhere, so only a match anchored at the start of the message is
        removed. Stripping a mid-message match would delete ordinary prose
        from the user's prompt, and stripping a bare ``@encre`` in the
        middle of a sentence would corrupt the request the agent is about
        to run.

        Args:
            text: Inbound message body.

        Returns:
            The body with a leading wake word (plus any trailing comma,
            colon or dash used as a separator) removed. Falls back to the
            unmodified text when nothing anchored at the start, or when
            stripping would leave the prompt empty.
        """
        if not text:
            return text
        for pattern in self._mention_patterns:
            match = pattern.match(text.lstrip())
            if match:
                cleaned = text.lstrip()[match.end():].lstrip(" ,:-")
                return cleaned or text
        return text

    async def _api_get(self, path: str) -> Dict[str, Any]:
        """Perform an authenticated GET against the BlueBubbles REST API.

        Args:
            path: API path, for example ``/api/v1/server/info``.

        Returns:
            The decoded JSON response body.

        Raises:
            httpx.HTTPStatusError: On any non-2xx status.
            AssertionError: When called before :meth:`connect` has opened
                the client; callers always gate on :attr:`client`.
        """
        assert self.client is not None
        res = await self.client.get(self._api_url(path))
        res.raise_for_status()
        return res.json()

    async def _api_post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Perform an authenticated POST against the BlueBubbles REST API.

        Args:
            path: API path, for example ``/api/v1/message/text``.
            payload: Request body, sent as JSON.

        Returns:
            The decoded JSON response body.

        Raises:
            httpx.HTTPStatusError: On any non-2xx status.
            AssertionError: When called before :meth:`connect`.
        """
        assert self.client is not None
        res = await self.client.post(self._api_url(path), json=payload)
        res.raise_for_status()
        return res.json()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Open the REST client, start the inbound webhook listener, and register it.

        Three things must succeed in order, and the order is load-bearing:

        1. The server must be reachable -- a dead ``ping`` means there is
           nothing to register a webhook against.
        2. The listener must be bound -- the registration URL must be
           accepting connections by the time the server dials it.
        3. The webhook must be registered -- BlueBubbles only delivers
           events to URLs it has been explicitly told about.

        Args:
            is_reconnect: Whether this is a retry of an earlier failed or
                lost connection. Retries are idempotent: a pre-existing
                registration is reused rather than duplicated, so a crash
                between steps 2 and 3 of the first attempt does not leave
                stray webhooks behind.

        Returns:
            ``True`` when the adapter is fully connected and dispatching.

        Note:
            On a failed ``ping`` the client is closed and cleared before
            returning ``False``; leaking an open client would keep the
            socket pool alive across retries and exhaust file descriptors.
        """
        if not self.server_url or not self.password:
            logger.error(
                "[bluebubbles] BLUEBUBBLES_SERVER_URL and BLUEBUBBLES_PASSWORD are required"
            )
            return False
        from aiohttp import web

        # Tighter keepalive so idle CLOSE_WAIT drains promptly (#18451).
        from encre.gateway.platforms._http_client_limits import platform_httpx_limits
        self.client = httpx.AsyncClient(timeout=30.0, limits=platform_httpx_limits())
        try:
            await self._api_get("/api/v1/ping")
            info = await self._api_get("/api/v1/server/info")
            server_data = (info or {}).get("data", {})
            self._private_api_enabled = bool(server_data.get("private_api"))
            self._helper_connected = bool(server_data.get("helper_connected"))
            logger.info(
                "[bluebubbles] connected to %s (private_api=%s, helper=%s)",
                self.server_url,
                self._private_api_enabled,
                self._helper_connected,
            )
        except Exception as exc:
            logger.error(
                "[bluebubbles] cannot reach server at %s: %s", self.server_url, exc
            )
            if self.client:
                await self.client.aclose()
                self.client = None
            return False

        # Explicit body cap: BlueBubbles webhook events are small JSON (or
        # form-encoded) payloads. client_max_size makes aiohttp enforce the
        # cap on every read path — including chunked requests that carry no
        # Content-Length (same pattern as webhook.py / raft, #58536/#58902).
        app = web.Application(client_max_size=_WEBHOOK_MAX_BODY_BYTES)
        app.router.add_get("/health", lambda _: web.Response(text="ok"))
        app.router.add_post(self.webhook_path, self._handle_webhook)
        # The webhook auth value is carried in the query string because the
        # BlueBubbles webhook API cannot send custom headers. Do not let
        # aiohttp access logs write that request target to agent.log.
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.webhook_host, self.webhook_port)
        await site.start()
        self._mark_connected()
        logger.info(
            "[bluebubbles] webhook listening on http://%s:%s%s",
            self.webhook_host,
            self.webhook_port,
            self.webhook_path,
        )

        # Register webhook with BlueBubbles server. This is required for the
        # server to know where to send events; delivery is not opt-in.
        await self._register_webhook()

        return True

    async def disconnect(self) -> None:
        """Unregister the webhook, close the client, and stop the listener.

        Unregistration runs first so the server stops POSTing events to a
        listener that is about to disappear; the reverse order would leave
        a burst of 5xx deliveries on the server side during shutdown.
        Cleanup is best-effort -- :meth:`_unregister_webhook` swallows its
        own errors so a server that is already unreachable cannot block
        gateway shutdown.
        """
        # Unregister webhook before cleaning up
        await self._unregister_webhook()

        if self.client:
            await self.client.aclose()
            self.client = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()

    @property
    def _webhook_url(self) -> str:
        """Compute the external webhook URL BlueBubbles will dial back into.

        Loopback bind addresses are reported as ``localhost`` because the
        BlueBubbles server is a separate process on the same machine and
        needs a hostname it can resolve, not a wildcard literal.

        Returns:
            ``http://<host>:<port><path>`` without any credential.
        """
        host = self.webhook_host
        if host in {"0.0.0.0", "127.0.0.1", "localhost", "::"}:
            host = "localhost"
        return f"http://{host}:{self.webhook_port}{self.webhook_path}"

    @property
    def _webhook_register_url(self) -> str:
        """Webhook URL registered with BlueBubbles, password in the query.

        BlueBubbles posts events to the exact URL registered via
        ``/api/v1/webhook`` and its registration API does not support
        custom headers, so embedding the password in the URL is the only
        way to authenticate inbound webhooks without disabling auth
        altogether. :meth:`_handle_webhook` reads it back out of
        ``request.query`` and compares it against :attr:`password`.

        Returns:
            The registration URL, with the password percent-encoded.
        """
        base = self._webhook_url
        if self.password:
            return f"{base}?password={quote(self.password, safe='')}"
        return base

    @property
    def _webhook_register_url_for_log(self) -> str:
        """Webhook registration URL safe for logs.

        Returns:
            The same URL as :attr:`_webhook_register_url` with the password
            replaced by ``***``, for use in log statements only.
        """
        base = self._webhook_url
        if self.password:
            return f"{base}?password=***"
        return base

    async def _find_registered_webhooks(self, url: str) -> list:
        """List BlueBubbles webhook registrations matching a URL.

        Used both to avoid duplicate registrations on reconnect and to
        sweep duplicates left behind by a crash. Matching is on exact URL
        equality because the URL embeds the password, which doubles the
        comparison as a credential check: a stale registration from a
        differently-credentialed configuration is not claimed as ours.

        Args:
            url: Registration URL to search for, including query string.

        Returns:
            The matching registration dicts, or ``[]`` when the lookup
            fails or nothing matches. Failures are silent by design -- this
            is a read-ahead optimisation, not an authoritative check.
        """
        try:
            res = await self._api_get("/api/v1/webhook")
            data = res.get("data")
            if isinstance(data, list):
                return [wh for wh in data if wh.get("url") == url]
        except Exception:
            pass
        return []

    async def _register_webhook(self) -> bool:
        """Register this webhook URL with the BlueBubbles server.

        BlueBubbles requires webhooks to be registered via API before it
        will send events. Checks for an existing registration first to
        avoid duplicates (e.g. after a crash without clean shutdown).

        Args:
            None.

        Returns:
            ``True`` when the server will deliver events to this adapter,
            either because of a pre-existing registration or a successful
            new one. ``False`` when the client is closed, the registration
            was refused, or the request failed -- in which case inbound
            messages simply never arrive and the adapter stays silently
            outbound-only.
        """
        if not self.client:
            return False

        webhook_url = self._webhook_register_url

        # Crash resilience — reuse an existing registration if present
        existing = await self._find_registered_webhooks(webhook_url)
        if existing:
            logger.info(
                "[bluebubbles] webhook already registered: %s",
                self._webhook_register_url_for_log,
            )
            return True

        # The two message events are the only ones the server needs to push;
        # attachment bytes are fetched over REST, and everything else is
        # filtered inside _handle_webhook anyway.
        payload = {
            "url": webhook_url,
            "events": ["new-message", "updated-message"],
        }

        try:
            res = await self._api_post("/api/v1/webhook", payload)
            status = res.get("status", 0)
            if 200 <= status < 300:
                logger.info(
                    "[bluebubbles] webhook registered with server: %s",
                    self._webhook_register_url_for_log,
                )
                return True
            else:
                logger.warning(
                    "[bluebubbles] webhook registration returned status %s: %s",
                    status,
                    res.get("message"),
                )
                return False
        except Exception as exc:
            logger.warning(
                "[bluebubbles] failed to register webhook with server: %s",
                exc,
            )
            return False

    async def _unregister_webhook(self) -> bool:
        """Unregister this webhook URL from the BlueBubbles server.

        Removes *all* matching registrations to clean up any duplicates
        left by prior crashes.

        Returns:
            ``True`` when at least one registration was deleted. All errors
            are swallowed and logged at debug level: unregistration is a
            nicety during shutdown, and a failure here must never block the
            gateway from exiting.
        """
        if not self.client:
            return False

        webhook_url = self._webhook_register_url
        removed = False

        try:
            for wh in await self._find_registered_webhooks(webhook_url):
                wh_id = wh.get("id")
                if wh_id:
                    res = await self.client.delete(
                        self._api_url(f"/api/v1/webhook/{wh_id}")
                    )
                    res.raise_for_status()
                    removed = True
            if removed:
                logger.info(
                    "[bluebubbles] webhook unregistered: %s",
                    self._webhook_register_url_for_log,
                )
        except Exception as exc:
            logger.debug(
                "[bluebubbles] failed to unregister webhook (non-critical): %s",
                exc,
            )
        return removed

    # ------------------------------------------------------------------
    # Chat GUID resolution
    # ------------------------------------------------------------------

    async def _resolve_chat_guid(self, target: str) -> Optional[str]:
        """Resolve an email/phone to a BlueBubbles chat GUID.

        If *target* already contains a semicolon (raw GUID format like
        ``iMessage;-;user@example.com``), it is returned as-is.  Otherwise
        the adapter queries the BlueBubbles chat list and matches strictly
        on ``chatIdentifier`` / ``identifier``.

        Participant membership is intentionally NOT used as a fallback:
        the same contact can appear in a 1:1 DM and in any number of group
        chats, so a participant match would let an outbound DM reply leak
        into a group thread (see #24157). When no exact chat identity
        matches, return ``None`` and let the caller create a fresh DM
        explicitly via ``_create_chat_for_handle``.
        """
        target = (target or "").strip()
        if not target:
            return None
        # Already a raw GUID
        if ";" in target:
            return target
        if target in self._guid_cache:
            self._guid_cache.move_to_end(target)
            return self._guid_cache[target]
        try:
            # The chat list is paginated; 100 is the largest single page the
            # server will return, which covers any realistic contact count.
            payload = await self._api_post(
                "/api/v1/chat/query",
                {"limit": 100, "offset": 0},
            )
            for chat in payload.get("data", []) or []:
                guid = chat.get("guid") or chat.get("chatGuid")
                identifier = chat.get("chatIdentifier") or chat.get("identifier")
                if identifier == target:
                    if guid:
                        self._guid_cache[target] = guid
                        # Pop from the front to keep the dict bounded; the
                        # oldest entry is always the least useful one.
                        while len(self._guid_cache) > _GUID_CACHE_SIZE:
                            self._guid_cache.popitem(last=False)
                    return guid
        except Exception:
            pass
        return None

    async def _create_chat_for_handle(
        self, address: str, message: str
    ) -> SendResult:
        """Create a new chat by sending the first message to *address*.

        Used by :meth:`send` when the target resolves to a raw address
        rather than an existing chat, which is the normal situation for a
        first-ever outbound message to a new contact.

        Args:
            address: Email or phone number to start the conversation with.
            message: First message body; the server creates the chat as a
                side effect of sending it, so there is no separate
                create-then-send dance.

        Returns:
            A :class:`SendResult` carrying the new message GUID. Every
            failure returns ``success=False`` with the vendor error text
            rather than raising, per the
            :class:`encre.gateway.platforms.base.SendResult` contract that
            a single blocked vendor must not take down the stream consumer.
        """
        payload = {
            "addresses": [address],
            "message": message,
            "tempGuid": f"temp-{datetime.utcnow().timestamp()}",
        }
        try:
            res = await self._api_post("/api/v1/chat/new", payload)
            data = res.get("data") or {}
            msg_id = data.get("guid") or data.get("messageGuid") or "ok"
            return SendResult(success=True, message_id=str(msg_id), raw_response=res)
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    # ------------------------------------------------------------------
    # Text sending
    # ------------------------------------------------------------------

    @staticmethod
    def truncate_message(content: str, max_length: int = MAX_TEXT_LENGTH) -> List[str]:
        """Split text into BlueBubbles-sized chunks without page markers.

        Delegates to the shared chunker in
        :class:`encre.gateway.platforms.base.BasePlatformAdapter` and then
        strips the ``(N/M)`` page indicator it appends. iMessage bubbles
        flow naturally with no way to mark continuation, so an indicator
        would be misleading rather than informative.

        Args:
            content: Full outbound text.
            max_length: Per-bubble character cap.

        Returns:
            Ordered chunks, each within *max_length* characters.
        """
        # Use the base splitter but skip pagination indicators — iMessage
        # bubbles flow naturally without "(1/3)" suffixes.
        chunks = BasePlatformAdapter.truncate_message(content, max_length)
        return [re.sub(r"\s*\(\d+/\d+\)$", "", c) for c in chunks]

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send text, split across as many iMessage bubbles as needed.

        This is the outbound entry point consumed by
        :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.

        Splitting is staged in two passes. First, the body is split on
        blank-line paragraph breaks so each thought becomes its own bubble
        -- iMessage has no block-structure concept, so this is the closest
        available rendering of a multi-paragraph answer. Second, any
        paragraph that still exceeds the cap is chunked with
        :meth:`truncate_message`.

        Args:
            chat_id: Target chat. May be a chat GUID (already containing
                ``;``), a chat identifier, or a raw email/phone address.
            content: Markdown-formatted agent output; see
                :meth:`format_message` for the flattening step.
            reply_to: GUID of the message to reply to. Only honoured when
                the server advertises the private API *and* the helper is
                attached, since thread origination requires both.
            metadata: Ignored; accepted for interface compatibility.

        Returns:
            The :class:`SendResult` of the last successfully sent chunk,
            which is what carries the newest message GUID for downstream
            reply threading.

        Note:
            GUID resolution is repeated per chunk rather than once, so a
            chat created between chunks by another turn still resolves
            correctly. Only the first chunk carries the reply metadata,
            because iMessage treats a follow-up bubble in the same turn as
            already part of the thread.
        """
        text = self.format_message(content)
        if not text:
            return SendResult(success=False, error="BlueBubbles send requires text")
        # Split on paragraph breaks first (double newlines) so each thought
        # becomes its own iMessage bubble, then truncate any that are still
        # too long.
        paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
        chunks: List[str] = []
        for para in (paragraphs or [text]):
            if len(para) <= self.MAX_MESSAGE_LENGTH:
                chunks.append(para)
            else:
                chunks.extend(self.truncate_message(para, max_length=self.MAX_MESSAGE_LENGTH))
        last = SendResult(success=True)
        for chunk in chunks:
            guid = await self._resolve_chat_guid(chat_id)
            if not guid:
                # If the target looks like an address, try creating a new chat
                if self._private_api_enabled and (
                    "@" in chat_id or re.match(r"^\+\d+", chat_id)
                ):
                    return await self._create_chat_for_handle(chat_id, chunk)
                return SendResult(
                    success=False,
                    error=f"BlueBubbles chat not found for target: {chat_id}",
                )
            payload: Dict[str, Any] = {
                "chatGuid": guid,
                # tempGuid is the client-side id used for retries; a
                # timestamp-derived value keeps it unique per call.
                "tempGuid": f"temp-{datetime.utcnow().timestamp()}",
                "message": chunk,
            }
            if reply_to and self._private_api_enabled and self._helper_connected:
                payload["method"] = "private-api"
                payload["selectedMessageGuid"] = reply_to
                payload["partIndex"] = 0
            try:
                res = await self._api_post("/api/v1/message/text", payload)
                data = res.get("data") or {}
                msg_id = data.get("guid") or data.get("messageGuid") or "ok"
                last = SendResult(
                    success=True, message_id=str(msg_id), raw_response=res
                )
            except Exception as exc:
                return SendResult(success=False, error=str(exc))
        return last

    # ------------------------------------------------------------------
    # Media sending (outbound)
    # ------------------------------------------------------------------

    async def _send_attachment(
        self,
        chat_id: str,
        file_path: str,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
        is_audio_message: bool = False,
    ) -> SendResult:
        """Send a file attachment via BlueBubbles multipart upload.

        Shared implementation for every media variant. Captions are sent
        as a separate text bubble immediately after the attachment, which
        is how iMessage renders an annotated file; BlueBubbles does not
        accept a caption inline with the attachment part.

        Args:
            chat_id: Target chat identifier or GUID.
            file_path: Local path to the file to upload.
            filename: Display name; defaults to the basename of
                *file_path*, which is what the recipient sees in the
                conversation.
            caption: Optional text sent as a follow-up bubble.
            is_audio_message: Marks the upload as a voice message, which
                is what makes iMessage render a waveform instead of a
                generic file card.

        Returns:
            A :class:`SendResult` with the attachment's message GUID.
            Uploads are given a 120 s timeout because a large video over a
            local bridge can take substantially longer than the 30 s REST
            default; a generic timeout failure here would otherwise look
            like a permanent vendor error.
        """
        if not self.client:
            return SendResult(success=False, error="Not connected")
        if not os.path.isfile(file_path):
            return SendResult(success=False, error=f"File not found: {file_path}")

        guid = await self._resolve_chat_guid(chat_id)
        if not guid:
            return SendResult(success=False, error=f"Chat not found: {chat_id}")

        fname = filename or os.path.basename(file_path)
        try:
            # The file handle stays open for the duration of the request so
            # httpx can stream it; closing it early would upload an empty body.
            with open(file_path, "rb") as f:
                files = {"attachment": (fname, f, "application/octet-stream")}
                data: Dict[str, str] = {
                    "chatGuid": guid,
                    "name": fname,
                    "tempGuid": uuid.uuid4().hex,
                }
                if is_audio_message:
                    data["isAudioMessage"] = "true"
                res = await self.client.post(
                    self._api_url("/api/v1/message/attachment"),
                    files=files,
                    data=data,
                    timeout=120,
                )
                res.raise_for_status()
                result = res.json()

            if caption:
                await self.send(chat_id, caption)

            # BlueBubbles reports success in the body's status field rather
            # than relying solely on the HTTP status code.
            if result.get("status") == 200:
                rdata = result.get("data") or {}
                msg_id = rdata.get("guid") if isinstance(rdata, dict) else None
                return SendResult(
                    success=True, message_id=msg_id, raw_response=result
                )
            return SendResult(
                success=False,
                error=result.get("message", "Attachment upload failed"),
            )
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image referenced by URL.

        The URL is first cached to a local file because BlueBubbles
        requires a multipart body upload and cannot fetch remote URLs on
        behalf of the sender.

        Args:
            chat_id: Target chat.
            image_url: Remote or local image URL.
            caption: Optional caption bubble.
            reply_to / metadata: Ignored; accepted for interface
                compatibility with the base adapter.

        Returns:
            A :class:`SendResult`. Falls back to
            :meth:`BasePlatformAdapter.send_image` when the cache step
            fails, which may still be able to handle a local path the
            cache helper could not resolve.
        """
        try:
            from encre.gateway.platforms.base import cache_image_from_url

            local_path = await cache_image_from_url(image_url)
            return await self._send_attachment(chat_id, local_path, caption=caption)
        except Exception:
            return await super().send_image(chat_id, image_url, caption, reply_to)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send an image from a local path.

        Args:
            chat_id: Target chat.
            image_path: Local image path.
            caption: Optional caption bubble.
            reply_to / kwargs: Accepted for interface compatibility.

        Returns:
            A :class:`SendResult`.
        """
        return await self._send_attachment(chat_id, image_path, caption=caption)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a voice recording, rendered as an iMessage voice message.

        Args:
            chat_id: Target chat.
            audio_path: Local audio path.
            caption: Optional caption bubble.
            reply_to / kwargs: Accepted for interface compatibility.

        Returns:
            A :class:`SendResult`. The ``is_audio_message`` flag is what
            makes the recipient see a voice bubble rather than a file.
        """
        return await self._send_attachment(
            chat_id, audio_path, caption=caption, is_audio_message=True
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video attachment.

        Args:
            chat_id: Target chat.
            video_path: Local video path.
            caption: Optional caption bubble.
            reply_to / kwargs: Accepted for interface compatibility.

        Returns:
            A :class:`SendResult`.
        """
        return await self._send_attachment(chat_id, video_path, caption=caption)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send an arbitrary document attachment.

        Args:
            chat_id: Target chat.
            file_path: Local file path.
            caption: Optional caption bubble.
            file_name: Override for the recipient-visible filename.
            reply_to / kwargs: Accepted for interface compatibility.

        Returns:
            A :class:`SendResult`.
        """
        return await self._send_attachment(
            chat_id, file_path, filename=file_name, caption=caption
        )

    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a GIF-style animation.

        iMessage has no distinct animation medium, so this degrades to the
        image path and the animation arrives as a photo.

        Args:
            chat_id: Target chat.
            animation_url: Remote animation URL.
            caption: Optional caption bubble.
            reply_to / metadata: Accepted for interface compatibility.

        Returns:
            A :class:`SendResult`.
        """
        return await self.send_image(
            chat_id, animation_url, caption, reply_to, metadata
        )

    # ------------------------------------------------------------------
    # Typing indicators
    # ------------------------------------------------------------------

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Signal that the agent is composing a reply.

        Gated on both the private API and an attached helper because
        typing status is a private-api endpoint; attempting it against a
        server without either would generate a noisy error per message.

        Args:
            chat_id: Target chat identifier or GUID.
            metadata: Ignored; accepted for interface compatibility.

        Returns:
            Nothing. Failures are swallowed -- a typing indicator that
            cannot be sent is a cosmetic loss, never a reason to surface
            an error to the user.
        """
        if not self._private_api_enabled or not self._helper_connected or not self.client:
            return
        try:
            guid = await self._resolve_chat_guid(chat_id)
            if guid:
                # The GUID contains semicolons and colons, so it must be
                # percent-encoded or the path segment would be malformed.
                encoded = quote(guid, safe="")
                await self.client.post(
                    self._api_url(f"/api/v1/chat/{encoded}/typing"), timeout=5
                )
        except Exception:
            pass

    async def stop_typing(self, chat_id: str) -> None:
        """Clear a typing indicator.

        The DELETE variant of :meth:`send_typing`; called by the stream
        consumer when a turn finishes without producing a message.

        Args:
            chat_id: Target chat identifier or GUID.

        Returns:
            Nothing. Failures are swallowed for the same reason as
            :meth:`send_typing`.
        """
        if not self._private_api_enabled or not self._helper_connected or not self.client:
            return
        try:
            guid = await self._resolve_chat_guid(chat_id)
            if guid:
                encoded = quote(guid, safe="")
                await self.client.delete(
                    self._api_url(f"/api/v1/chat/{encoded}/typing"), timeout=5
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Read receipts
    # ------------------------------------------------------------------

    async def mark_read(self, chat_id: str) -> bool:
        """Mark the conversation as read from the agent's side.

        Args:
            chat_id: Target chat identifier or GUID.

        Returns:
            ``True`` only when the server accepted the call. ``False``
            when the capability is unavailable, the chat does not resolve,
            or the request failed. Callers treat a ``False`` as a no-op
            rather than an error.
        """
        if not self._private_api_enabled or not self._helper_connected or not self.client:
            return False
        try:
            guid = await self._resolve_chat_guid(chat_id)
            if guid:
                encoded = quote(guid, safe="")
                await self.client.post(
                    self._api_url(f"/api/v1/chat/{encoded}/read"), timeout=5
                )
                return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Tapback reactions
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Chat info
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Describe a chat for the UI and for session key scoping.

        Group detection works off the ``;+;`` segment in the raw GUID,
        which is BlueBubbles' way of encoding a group chat (a 1:1 chat is
        ``iMessage;-;<address>`` while a group is ``iMessage;+;<address>``).

        Args:
            chat_id: Chat GUID or identifier.

        Returns:
            A dict with ``name`` and ``type``, plus ``participants`` when
            the server returned them. Falls back to the raw ``chat_id`` as
            the display name when lookup fails, so the UI always has
            something sensible to render.
        """
        is_group = ";+;" in (chat_id or "")
        info: Dict[str, Any] = {
            "name": chat_id,
            "type": "group" if is_group else "dm",
        }
        try:
            guid = await self._resolve_chat_guid(chat_id)
            if guid:
                encoded = quote(guid, safe="")
                # with=participants opts the server into returning the
                # participant roster, which is otherwise omitted.
                res = await self._api_get(
                    f"/api/v1/chat/{encoded}?with=participants"
                )
                data = (res or {}).get("data", {})
                display_name = (
                    data.get("displayName")
                    or data.get("chatIdentifier")
                    or chat_id
                )
                participants = []
                for p in data.get("participants", []) or []:
                    addr = (p.get("address") or "").strip()
                    if addr:
                        participants.append(addr)
                info["name"] = display_name
                if participants:
                    info["participants"] = participants
        except Exception:
            pass
        return info

    def format_message(self, content: str) -> str:
        """Flatten agent output for an iMessage bubble.

        iMessage renders a plain bubble with no markdown engine, so all
        markup is stripped before sending; leaving it in would show the
        recipient literal ``**bold**`` markers.

        Args:
            content: Raw agent output.

        Returns:
            Markdown-free text.
        """
        return strip_markdown(content)

    # ------------------------------------------------------------------
    # Inbound attachment downloading (from #4588)
    # ------------------------------------------------------------------

    async def _download_attachment(
        self, att_guid: str, att_meta: Dict[str, Any]
    ) -> Optional[str]:
        """Download an attachment from BlueBubbles and cache it locally.

        Returns the local file path on success, None on failure.

        Attachment bytes never arrive through the webhook -- the event only
        carries metadata such as ``guid``, ``mimeType`` and
        ``transferName`` -- so every inbound media message requires a REST
        download before the agent can see it. The downloaded bytes are
        handed to the shared cache helpers in
        :mod:`encre.gateway.platforms.base`, which place them in the Encre
        cache directory and return a path that the stream consumer can read
        and that the desktop UI can display.

        Args:
            att_guid: Attachment GUID, used as the download path segment.
            att_meta: Attachment metadata record from the webhook event.

        Returns:
            The local cached path, or ``None`` when the download fails. A
            ``None`` is non-fatal: the message still dispatches as text, so
            a single broken attachment cannot lose the whole message.
        """
        if not self.client:
            return None
        try:
            encoded = quote(att_guid, safe="")
            resp = await self.client.get(
                self._api_url(f"/api/v1/attachment/{encoded}/download"),
                timeout=60,
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.content

            mime = (att_meta.get("mimeType") or "").lower()
            transfer_name = att_meta.get("transferName", "")

            # MIME-to-extension tables exist because the cache helpers name
            # files by extension, and iMessage reports HEIC/HEIF which no
            # downstream consumer on a Linux host can render. Those are
            # mapped to .jpg to match what the cache layer expects.
            if mime.startswith("image/"):
                ext_map = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/gif": ".gif",
                    "image/webp": ".webp",
                    "image/heic": ".jpg",
                    "image/heif": ".jpg",
                    "image/tiff": ".jpg",
                }
                ext = ext_map.get(mime, ".jpg")
                return cache_image_from_bytes(data, ext)

            # CAF is Apple's native voice format; it is normalised to .mp3
            # because the downstream audio path assumes an MP3-compatible
            # extension for transcription tooling.
            if mime.startswith("audio/"):
                ext_map = {
                    "audio/mp3": ".mp3",
                    "audio/mpeg": ".mp3",
                    "audio/ogg": ".ogg",
                    "audio/wav": ".wav",
                    "audio/x-caf": ".mp3",
                    "audio/mp4": ".m4a",
                    "audio/aac": ".m4a",
                }
                ext = ext_map.get(mime, ".mp3")
                return cache_audio_from_bytes(data, ext)

            # Videos, documents, and everything else
            filename = transfer_name or f"file_{uuid.uuid4().hex[:8]}"
            return cache_document_from_bytes(data, filename)

        except Exception as exc:
            logger.warning(
                "[bluebubbles] failed to download attachment %s: %s",
                _redact(att_guid),
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Webhook handling
    # ------------------------------------------------------------------

    def _extract_payload_record(
        self, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Locate the message record inside a webhook payload.

        BlueBubbles does not have one stable envelope shape across server
        versions: the record may sit directly under ``data``, be the first
        element of a ``data`` list, live under ``message``, or be the
        payload itself. Probing in that order keeps every version
        working with a single code path.

        Args:
            payload: Fully decoded webhook body.

        Returns:
            The first record-shaped dict found, or ``None`` when the body
            does not contain one. Callers treat ``None`` as an empty dict,
            which then fails the later field-presence check with a 400
            instead of an AttributeError.
        """
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    return item
        if isinstance(payload.get("message"), dict):
            return payload.get("message")
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _value(*candidates: Any) -> Optional[str]:
        """Return the first candidate that is a non-blank string.

        The vendor uses several names for the same field across versions,
        so every field read goes through this helper with all plausible
        keys as arguments. Keeping the fallback chain in one place means
        the mapping from vendor shape to
        :class:`encre.gateway.platforms.base.MessageEvent` is auditable in
        a single spot.

        Args:
            *candidates: Values pulled from the payload, in priority order.

        Returns:
            The first stripped, non-empty string, or ``None``.
        """
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    async def _handle_webhook(self, request):
        """Inbound webhook handler: authenticate, normalise, and dispatch.

        This is where a BlueBubbles vendor payload becomes a
        :class:`encre.gateway.platforms.base.MessageEvent`. The steps run
        in a deliberate order, each of which is cheap relative to the
        agent turn that follows:

        1. Authenticate. The password arrives in the query string because
           BlueBubbles cannot send custom headers on webhook POSTs.
        2. Decode the body, accepting both JSON and form-encoded payloads.
        3. Drop non-message events, messages sent by the local user
           (``isFromMe``), and tapback reactions, which arrive as message
           events.
        4. Download attachments over REST and map them to
           :class:`encre.gateway.platforms.base.MessageType` values.
        5. Build the :class:`encre.gateway.session.SessionSource` and
           dispatch through :meth:`BasePlatformAdapter.handle_message`.

        Args:
            request: The aiohttp request.

        Returns:
            ``200`` for every accepted or intentionally ignored event, so
            the server stops retrying. ``401`` for a bad credential,
            ``400`` for an undecodable body or a message missing the fields
            needed to route it.

        Note:
            Dispatch is fire-and-forget: the handler creates a background
            task and returns ``200`` immediately. BlueBubbles treats a slow
            response as a failure and retries, which would double-run the
            agent. The task is tracked in ``_background_tasks`` so a
            shutdown cannot orphan it mid-run.
        """
        from aiohttp import web

        # Every credential channel is probed in priority order; the query
        # string is primary because that is what _webhook_register_url puts
        # there.
        token = (
            request.query.get("password")
            or request.query.get("guid")
            or request.headers.get("x-password")
            or request.headers.get("x-guid")
            or request.headers.get("x-bluebubbles-guid")
        )
        if token != self.password:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            raw = await request.read()
            body = raw.decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except Exception:
                # Some server versions POST form-encoded with the real
                # payload nested in a single field.
                from urllib.parse import parse_qs

                form = parse_qs(body)
                payload_str = (
                    form.get("payload")
                    or form.get("data")
                    or form.get("message")
                    or [""]
                )[0]
                payload = json.loads(payload_str) if payload_str else {}
        except Exception as exc:
            logger.error("[bluebubbles] webhook parse error: %s", exc)
            return web.json_response({"error": "invalid payload"}, status=400)

        event_type = self._value(payload.get("type"), payload.get("event")) or ""
        # Only process message events; silently acknowledge everything else
        if event_type and event_type not in _MESSAGE_EVENTS:
            return web.Response(text="ok")

        record = self._extract_payload_record(payload) or {}
        # Our own outbound sends echo back as inbound events; without this
        # check the agent would answer its own replies forever.
        is_from_me = bool(
            record.get("isFromMe")
            or record.get("fromMe")
            or record.get("is_from_me")
        )
        if is_from_me:
            return web.Response(text="ok")

        # Skip tapback reactions delivered as messages
        assoc_type = record.get("associatedMessageType")
        if isinstance(assoc_type, int) and assoc_type in {
            **_TAPBACK_ADDED,
            **_TAPBACK_REMOVED,
        }:
            return web.Response(text="ok")

        text = (
            self._value(
                record.get("text"), record.get("message"), record.get("body")
            )
            or ""
        )

        # --- Inbound attachment handling ---
        attachments = record.get("attachments") or []
        media_urls: List[str] = []
        media_types: List[str] = []
        msg_type = MessageType.TEXT

        for att in attachments:
            att_guid = att.get("guid", "")
            if not att_guid:
                continue
            cached = await self._download_attachment(att_guid, att)
            if cached:
                mime = (att.get("mimeType") or "").lower()
                media_urls.append(cached)
                media_types.append(mime)
                if mime.startswith("image/"):
                    msg_type = MessageType.PHOTO
                # The uti check catches a CAF voice memo that arrived with
                # no mimeType at all, which happens on some server versions.
                elif mime.startswith("audio/") or (att.get("uti") or "").endswith(
                    "caf"
                ):
                    msg_type = MessageType.VOICE
                elif mime.startswith("video/"):
                    msg_type = MessageType.VIDEO
                else:
                    msg_type = MessageType.DOCUMENT

        # With multiple attachments, prefer PHOTO if any images present
        if len(media_urls) > 1:
            mime_prefixes = {(m or "").split("/")[0] for m in media_types}
            if "image" in mime_prefixes:
                msg_type = MessageType.PHOTO

        # A media-only message still needs a non-empty text field so the
        # downstream pipeline treats it as a real turn.
        if not text and media_urls:
            text = "(attachment)"
        # --- End attachment handling ---

        chat_guid = self._value(
            record.get("chatGuid"),
            payload.get("chatGuid"),
            record.get("chat_guid"),
            payload.get("chat_guid"),
            payload.get("guid"),
        )
        # Fallback: BlueBubbles v1.9+ webhook payloads omit top-level chatGuid;
        # the chat GUID is nested under data.chats[0].guid instead.
        if not chat_guid:
            _chats = record.get("chats") or []
            if _chats and isinstance(_chats[0], dict):
                chat_guid = _chats[0].get("guid") or _chats[0].get("chatGuid")
        chat_identifier = self._value(
            record.get("chatIdentifier"),
            record.get("identifier"),
            payload.get("chatIdentifier"),
            payload.get("identifier"),
        )
        # The sender handle is nested under handle.address on newer servers
        # and flat on older ones; both shapes are probed.
        sender = (
            self._value(
                record.get("handle", {}).get("address")
                if isinstance(record.get("handle"), dict)
                else None,
                record.get("sender"),
                record.get("from"),
                record.get("address"),
            )
            or chat_identifier
            or chat_guid
        )
        if not (chat_guid or chat_identifier) and sender:
            chat_identifier = sender
        if not sender or not (chat_guid or chat_identifier) or not text:
            return web.json_response({"error": "missing message fields"}, status=400)

        # The GUID is preferred as the session key because an identifier can
        # change (a user edits a display name) while a GUID is immutable,
        # and splitting one conversation across two GUIDs would fork the session.
        session_chat_id = chat_guid or chat_identifier
        is_group = bool(record.get("isGroup")) or (";+;" in (chat_guid or ""))
        if is_group and self.require_mention:
            if not self._message_matches_mention_patterns(text):
                logger.debug(
                    "[bluebubbles] ignoring group message (require_mention=true, no mention pattern matched)"
                )
                return web.Response(text="ok")
            text = self._clean_mention_text(text)
        source = self.build_source(
            chat_id=session_chat_id,
            chat_name=chat_identifier or sender,
            chat_type="group" if is_group else "dm",
            user_id=sender,
            user_name=sender,
            chat_id_alt=chat_identifier,
        )
        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            raw_message=payload,
            message_id=self._value(
                record.get("guid"),
                record.get("messageGuid"),
                record.get("id"),
            ),
            # threadOriginatorGuid is set on a threaded reply; it is what
            # lets the agent see which message was answered.
            reply_to_message_id=self._value(
                record.get("threadOriginatorGuid"),
                record.get("associatedMessageGuid"),
            ),
            media_urls=media_urls,
            media_types=media_types,
        )
        # Fire-and-forget so the server does not retry; tracked so shutdown
        # can wait for in-flight runs.
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        # Fire-and-forget read receipt
        if self.send_read_receipts and session_chat_id:
            asyncio.create_task(self.mark_read(session_chat_id))

        return web.Response(text="ok")
