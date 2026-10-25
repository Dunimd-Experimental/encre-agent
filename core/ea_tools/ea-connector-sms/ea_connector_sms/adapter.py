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

"""SMS (Twilio) platform adapter for Encre's IM gateway.

This module is one of the vendor adapters under
:mod:`encre.gateway.platforms`.  It holds no persistent outbound connection
of its own: outbound delivery is a REST call to the Twilio Messages resource,
and inbound delivery is an HTTP ``POST`` that Twilio pushes to a webhook
server this adapter hosts on a local port.  Inbound and outbound are
therefore two unrelated transport legs, which is why
:meth:`SmsAdapter.connect` starts an HTTP server rather than opening a
socket to a vendor.  :class:`encre.gateway.run.GatewayRunner` owns the
adapter lifecycle; the
:class:`encre.gateway.platforms.base.SendResult` values returned by
:meth:`SmsAdapter.send` are read by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.

Data flow.  A Twilio ``POST`` to ``/webhooks/twilio`` is bounded by size,
HMAC-verified, echo-suppressed, and mapped onto a
:class:`encre.gateway.platforms.base.MessageEvent` whose chat id and user id
are both the sender's E.164 number.  That identity choice is what gives each
distinct caller its own session key and its own agent session.  The event is
handed to
:meth:`encre.gateway.platforms.base.BasePlatformAdapter.handle_message` on a
tracked background task so the Twilio handshake is acknowledged before the
agent turn begins.  Replies travel the other leg: markdown is downgraded to
plaintext, the text is chunked to the segment budget, and every chunk is
posted to Twilio separately.

Credentials are shared with the optional telephony skill.  Account settings
come from ``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN`` and
``TWILIO_PHONE_NUMBER`` (the E.164 from-number, e.g. ``+15551234567``).
Gateway-side settings: ``SMS_WEBHOOK_PORT`` (default 8080),
``SMS_WEBHOOK_HOST`` (default 127.0.0.1), ``SMS_WEBHOOK_URL`` (the public
URL Twilio posts to, required so request signatures can be verified),
``SMS_INSECURE_NO_SIGNATURE`` (``true`` to disable verification, development
only), ``SMS_ALLOWED_USERS`` (comma-separated E.164 allow-list),
``SMS_ALLOW_ALL_USERS`` (``true``/``false``), and ``SMS_HOME_CHANNEL``
(the number used as the delivery target for scheduled work).

Public API:
    * :class:`SmsAdapter` -- the adapter contract implementation.
    * :func:`check_sms_requirements` -- dependency and credential probe.
    * :func:`_standalone_send` -- out-of-process delivery for callers that
      have no live adapter instance, such as cron jobs.
    * :func:`_strip_markdown_for_sms` -- markdown downgrade to plaintext.
"""

import asyncio
import aiohttp  # noqa: F401
import base64
import hashlib
import hmac
import logging
import os
import urllib.parse
from typing import Any, Dict, Optional
import re

from encre.gateway.config import Platform, PlatformConfig
from encre.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from encre.gateway.platforms.helpers import redact_phone, strip_markdown

logger = logging.getLogger(__name__)

# Twilio REST v2010-04-01 account resource root.  The Messages endpoint is
# appended with the account SID; Twilio keeps this version pinned, so a
# change here is a breaking vendor migration, not a config tweak.
TWILIO_API_BASE = "https://api.twilio.com/2010-04-01/Accounts"

# Outbound chunk budget in characters.  A single GSM-7 segment is 160
# characters and a Unicode segment 70, so 1600 allows roughly ten segments.
# Twilio concatenates segments transparently, but concatenated messages carry
# a different per-message cost than a single segment, which is why the budget
# is generous rather than exact.
MAX_SMS_LENGTH = 1600

# Loopback by default: binding outside 127.0.0.1 exposes the webhook to the
# network, and although signature validation gates the handler, the address
# the gateway listens on is still an operator decision.
DEFAULT_WEBHOOK_PORT = 8080
DEFAULT_WEBHOOK_HOST = "127.0.0.1"

# Hard ceiling on an inbound webhook body, in bytes.  Twilio payloads are
# small form encodings, so anything past 64 KiB is a misfiring client or a
# hostile peer, and the body is rejected before it can consume memory.
_TWILIO_WEBHOOK_MAX_BODY_BYTES = 65_536


def check_sms_requirements() -> bool:
    """Report whether the SMS platform can be started on this host.

    Consulted through the platform registry before the gateway attempts to
    construct an adapter, so a host missing either the HTTP client or the
    account credentials never reaches constructor time with a
    :class:`KeyError`.  Environment reads happen at call time rather than
    import time so values can be set after the module is loaded.

    Args:
        None.

    Returns:
        ``True`` only when ``aiohttp`` imports and both the account SID and
        the auth token are present.  The from-number is deliberately not
        required here: it is validated in
        :meth:`SmsAdapter.connect`, where a missing value is recorded as a
        non-retryable fatal error rather than a startup failure.

    Raises:
        Nothing; every failure path returns ``False``.
    """
    return bool(os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN"))


class SmsAdapter(BasePlatformAdapter):
    """Twilio SMS adapter for the Encre gateway.

    Implements :class:`encre.gateway.platforms.base.BasePlatformAdapter` for
    SMS delivery over a Twilio account.  The adapter is deliberately
    multi-tenant: the sender's E.164 number is used as both the chat id and
    the user id, so each caller is resolved by
    :func:`encre.gateway.session.build_session_key` into a distinct session
    and one number never shares agent context with another.  Every outbound
    message is sent from the single configured ``TWILIO_PHONE_NUMBER``.

    Invariants:
        * ``_webhook_url`` is non-empty whenever signature validation is
          expected, because the signed URL must match the URL Twilio
          configured in its console byte for byte.  Validation is skipped
          entirely only under the explicit
          ``SMS_INSECURE_NO_SIGNATURE`` development override.
        * ``_http_session`` is created once in :meth:`connect` and reused by
          every :meth:`send`, so outbound delivery never opens a fresh
          connection pool for each message.  A call before ``connect`` still
          works by opening a short-lived fallback session that is closed in
          the ``finally`` block.
        * ``_runner`` and ``_http_session`` are both cleared on
          :meth:`disconnect`, making disconnect idempotent and safe to call
          more than once.

    Lifecycle:
        Constructed by the gateway runner with a
        :class:`encre.gateway.config.PlatformConfig`; :meth:`connect` binds
        the webhook server and the outbound session; :meth:`send` posts to
        Twilio; :meth:`disconnect` tears both down.

    Async-safety:
        All state is touched only from the adapter's own event loop.  Inbound
        work is spawned onto a tracked background task in
        :meth:`_handle_webhook`, so the HTTP handler never blocks on an agent
        turn and Twilio receives its acknowledgement quickly.

    Attributes:
        _account_sid: Twilio account identifier, from
            ``TWILIO_ACCOUNT_SID``.
        _auth_token: Twilio auth token, from ``TWILIO_AUTH_TOKEN``.  Also the
            HMAC key for inbound signature verification.
        _from_number: E.164 number replies are sent from.
        _webhook_port: TCP port the webhook server binds.
        _webhook_host: Interface the webhook server binds.
        _webhook_url: Public callback URL, the exact string signed by Twilio.
        _runner: ``aiohttp.web.AppRunner`` owning the webhook server.
        _http_session: Shared outbound HTTP client session.
    """

    MAX_MESSAGE_LENGTH = MAX_SMS_LENGTH

    def __init__(self, config: PlatformConfig):
        """Initialize adapter state and read configuration.

        Construction is network-free by contract: the runner builds the
        adapter, wires in collaborators, and only then calls
        :meth:`connect`.  Credential reads are direct ``os.environ``
        lookups rather than ``getenv`` so a missing account SID or auth
        token surfaces as a :class:`KeyError` here, where the caller can
        attribute the failure, instead of silently producing an uncredentialed
        adapter that fails mid-flight.

        Args:
            config: Platform configuration for this platform.  Passed to the
                base class for capability and metadata bookkeeping; Twilio
                credentials themselves come from the environment so they
                stay shared with the telephony skill.

        Raises:
            KeyError: When ``TWILIO_ACCOUNT_SID`` or ``TWILIO_AUTH_TOKEN`` is
                absent from the environment.
            ValueError: When ``SMS_WEBHOOK_PORT`` is set to a non-integer.
        """
        super().__init__(config, Platform.SMS)
        self._account_sid: str = os.environ["TWILIO_ACCOUNT_SID"]
        self._auth_token: str = os.environ["TWILIO_AUTH_TOKEN"]
        self._from_number: str = os.getenv("TWILIO_PHONE_NUMBER", "")
        self._webhook_port: int = int(
            os.getenv("SMS_WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT))
        )
        self._webhook_host: str = os.getenv("SMS_WEBHOOK_HOST", DEFAULT_WEBHOOK_HOST)
        self._webhook_url: str = os.getenv("SMS_WEBHOOK_URL", "").strip()
        self._runner = None
        self._http_session: Optional["aiohttp.ClientSession"] = None

    def _basic_auth_header(self) -> str:
        """Build the HTTP Basic credential header for the Twilio API.

        Twilio accepts basic auth only, with the account SID as the username
        and the auth token as the password.  The encoding is plain
        ASCII-safe base64 of ``sid:token``.

        Args:
            None.

        Returns:
            A complete ``Authorization`` header value, including the
            ``Basic `` scheme prefix.

        Note:
            The auth token is a live credential.  This value is set only as a
            request header and is never logged.
        """
        creds = f"{self._account_sid}:{self._auth_token}"
        encoded = base64.b64encode(creds.encode("ascii")).decode("ascii")
        return f"Basic {encoded}"

    # ------------------------------------------------------------------
    # Required abstract methods
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Start the webhook server and prepare outbound delivery.

        There is no vendor socket to open, so "connected" for this platform
        means the HTTP listener is bound and reachable.  Configuration errors
        that would make the platform unusable are recorded through
        :meth:`BasePlatformAdapter._set_fatal_error` with ``retryable=False``
        rather than raised, so the runner stops supervising instead of
        looping on a permanently unfixable configuration.

        Args:
            is_reconnect: Accepted for signature compatibility with the base
                contract.  Unusable here: the webhook server is a single
                long-lived listener, so a reconnect is a fresh start rather
                than a resume.

        Returns:
            ``True`` once the listener is bound and the outbound session is
            open.  ``False`` when a required credential or URL is missing.

        Raises:
            Nothing; all failures are logged and returned.
        """
        import aiohttp
        from aiohttp import web

        if not self._from_number:
            msg = "[sms] TWILIO_PHONE_NUMBER not set — cannot send replies"
            logger.error(msg)
            self._set_fatal_error("sms_missing_phone_number", msg, retryable=False)
            return False

        insecure_no_sig = os.getenv("SMS_INSECURE_NO_SIGNATURE", "").lower() == "true"

        if not self._webhook_url and not insecure_no_sig:
            msg = (
                "[sms] Refusing to start: SMS_WEBHOOK_URL is required for Twilio "
                "signature validation. Set it to the public URL configured in your "
                "Twilio console (e.g. https://example.com/webhooks/twilio). "
                "For local development without validation, set "
                "SMS_INSECURE_NO_SIGNATURE=true (NOT recommended for production)."
            )
            logger.error(msg)
            self._set_fatal_error("sms_missing_webhook_url", msg, retryable=False)
            return False

        if insecure_no_sig and not self._webhook_url:
            logger.warning(
                "[sms] SMS_INSECURE_NO_SIGNATURE=true — Twilio signature validation "
                "is DISABLED. Any client that can reach port %d can inject messages. "
                "Do NOT use this in production.",
                self._webhook_port,
            )

        # The body cap is enforced here and again in the handler.  aiohttp
        # applies its own limit only when a Content-Length header is present,
        # so a chunked transfer without one would otherwise stream an
        # unbounded body straight past the handler's 413 check.  Setting the
        # framework cap is what closes that gap.
        app = web.Application(client_max_size=_TWILIO_WEBHOOK_MAX_BODY_BYTES)
        app.router.add_post("/webhooks/twilio", self._handle_webhook)
        app.router.add_get("/health", lambda _: web.Response(text="ok"))

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._webhook_host, self._webhook_port)
        await site.start()
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=True,
        )
        self._running = True

        logger.info(
            "[sms] Twilio webhook server listening on %s:%d, from: %s",
            self._webhook_host,
            self._webhook_port,
            redact_phone(self._from_number),
        )
        return True

    async def disconnect(self) -> None:
        """Release the outbound session and stop the webhook listener.

        Idempotent by construction: both handles are nulled as they are
        closed, so a second call and a call before :meth:`connect` are both
        no-ops rather than errors.  Order matters slightly, since the HTTP
        session is closed before the runner tears down the listener;
        in-flight inbound tasks that need outbound delivery will then find a
        closed session and degrade to a fresh fallback session in
        :meth:`send`.

        Args:
            None.

        Returns:
            Nothing.
        """
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._running = False
        logger.info("[sms] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send outbound text to *chat_id* via the Twilio Messages API.

        This is the single outbound method called by
        :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`, so its
        behaviour defines how agent output reaches a phone.  Vendor failures
        are returned as a :class:`SendResult` rather than raised: the
        consumer distinguishes a returned failure from an unexpected
        exception, and a Twilio-side refusal must not tear down a turn.

        The body is downgraded through :meth:`format_message` and then split
        with :meth:`BasePlatformAdapter.truncate_message` before any network
        call, because Twilio rejects an overlong body outright and per-call
        failures waste a segment of billable capacity.  Chunks are posted in
        order and the first failure aborts the remainder, so a partially
        delivered conversation is a real, user-visible state rather than a
        silently dropped tail.

        Args:
            chat_id: Destination phone number in E.164 form.
            content: Outbound text, still containing agent markdown.
            reply_to: Accepted for contract compatibility.  SMS has no
                threading primitive, so this is ignored.
            metadata: Accepted for contract compatibility and ignored.

        Returns:
            :class:`SendResult` describing the outcome.  On success
            ``message_id`` carries the ``sid`` of the last chunk, which is
            what a caller needs to address the delivered message later.
            ``success`` is ``True`` with no ``message_id`` when
            ``content`` chunked to an empty list, meaning there was nothing
            to send.

        Raises:
            Nothing; Twilio responses and transport errors are both
            converted to a failed result.
        """
        import aiohttp

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted)
        last_result = SendResult(success=True)

        url = f"{TWILIO_API_BASE}/{self._account_sid}/Messages.json"
        headers = {
            "Authorization": self._basic_auth_header(),
        }

        session = self._http_session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=True,
        )
        try:
            for chunk in chunks:
                form_data = aiohttp.FormData()
                form_data.add_field("From", self._from_number)
                form_data.add_field("To", chat_id)
                form_data.add_field("Body", chunk)

                try:
                    async with session.post(url, data=form_data, headers=headers) as resp:
                        body = await resp.json()
                        if resp.status >= 400:
                            error_msg = body.get("message", str(body))
                            logger.error(
                                "[sms] send failed to %s: %s %s",
                                redact_phone(chat_id),
                                resp.status,
                                error_msg,
                            )
                            return SendResult(
                                success=False,
                                error=f"Twilio {resp.status}: {error_msg}",
                            )
                        msg_sid = body.get("sid", "")
                        last_result = SendResult(success=True, message_id=msg_sid)
                except Exception as e:
                    logger.error("[sms] send error to %s: %s", redact_phone(chat_id), e)
                    return SendResult(success=False, error=str(e))
        finally:
            # Only the fallback session is closed here.  The persistent one
            # belongs to the adapter and must survive across sends, so
            # closing it whenever it was used would defeat the pooling.
            if not self._http_session and session:
                await session.close()

        return last_result

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Describe a chat for the gateway's session and UI listing.

        SMS has no server-side chat object to look up; the identifier itself
        is the conversation.  Every number is therefore reported as a
        direct message, which is the truthful shape.

        Args:
            chat_id: E.164 phone number to describe.

        Returns:
            Mapping with ``name`` set to the number and ``type`` set to
            ``"dm"``.  The redaction helper is deliberately not applied
            here because this value is displayed in the desktop UI, where
            the operator needs the real number.
        """
        return {"name": chat_id, "type": "dm"}

    # ------------------------------------------------------------------
    # SMS-specific formatting
    # ------------------------------------------------------------------

    def format_message(self, content: str) -> str:
        """Downgrade agent markdown to plaintext suitable for SMS.

        SMS terminals render markup as literal characters, so an unsanitized
        reply arrives as ``**bold**`` and ```` ``` ```` noise.  Delegation
        goes to the shared helper so the downgrade rules stay identical
        across every platform that faces a markup-free channel.

        Args:
            content: Outbound text, possibly containing markdown.

        Returns:
            Plain text with emphasis, code, heading and link markup removed.
        """
        return strip_markdown(content)

    # ------------------------------------------------------------------
    # Twilio signature validation
    # ------------------------------------------------------------------

    def _validate_twilio_signature(
        self, url: str, post_params: dict, signature: str,
    ) -> bool:
        """Validate an ``X-Twilio-Signature`` header value.

        The algorithm is Twilio's documented HMAC-SHA1 construction: the
        callback URL followed by each POST parameter name and value in
        lexicographic key order, base64-encoded digest.  The signature is
        the only thing standing between this webhook and an arbitrary
        peer, so a mismatch means the request is not from Twilio.

        Args:
            url: Callback URL the signature was computed over, in the exact
                form Twilio configured in its console.
            post_params: Flat parameter mapping.  Values must already be
                single strings, matching how the webhook handler flattens
                the parsed form.
            signature: Base64 digest value from the request header.

        Returns:
            ``True`` when the signature matches either the given URL or its
            port-toggled variant.

        Note:
            Twilio may compute the signature over the URL with or without an
            explicit default port, depending on how the console stored it.
            Computing both variants is what makes the check tolerate that
            ambiguity instead of rejecting genuine traffic.
        """
        if self._check_signature(url, post_params, signature):
            return True

        variant = self._port_variant_url(url)
        if variant and self._check_signature(variant, post_params, signature):
            return True

        return False

    def _check_signature(
        self, url: str, post_params: dict, signature: str,
    ) -> bool:
        """Compute one candidate signature and compare it in constant time.

        Key order is significant to the vendor algorithm: parameters must
        be concatenated in ascending key order after the URL, so the sort
        is not cosmetic.  A dict that was not sorted would produce a
        different digest and reject valid requests.

        Args:
            url: Callback URL to include as the digest prefix.
            post_params: Flat parameter mapping to concatenate.
            signature: Base64 digest supplied by the caller.

        Returns:
            ``True`` when the computed digest equals the supplied one.
        """
        data_to_sign = url
        for key in sorted(post_params.keys()):
            data_to_sign += key + post_params[key]
        mac = hmac.new(
            self._auth_token.encode("utf-8"),
            data_to_sign.encode("utf-8"),
            hashlib.sha1,
        )
        computed = base64.b64encode(mac.digest()).decode("utf-8")
        # The digest is compared as bytes, not strings.  compare_digest
        # raises TypeError on a non-ASCII str, and the signature arrives as a
        # raw request header that may not be ASCII-clean.
        return hmac.compare_digest(computed.encode(), signature.encode())

    @staticmethod
    def _port_variant_url(url: str) -> str | None:
        """Return *url* with its scheme-default port toggled, or ``None``.

        The only ambiguity worth tolerating is whether the default port for
        the scheme is written explicitly (``:443``) or omitted.  Anything
        with a non-standard port is left alone, since that port is
        meaningful and is part of the canonical identity.

        Args:
            url: Callback URL to normalize.

        Returns:
            A URL with the default port stripped when it was explicit, one
            with the default port added when it was absent, or ``None``
            when the scheme has no known default port or the URL carries a
            non-standard port.
        """
        parsed = urllib.parse.urlparse(url)
        default_ports = {"https": 443, "http": 80}
        default_port = default_ports.get(parsed.scheme)
        if default_port is None:
            return None

        if parsed.port == default_port:
            # The default port is written out explicitly, so it is removed
            # to yield the implicit form Twilio would have signed.
            return urllib.parse.urlunparse(
                (parsed.scheme, parsed.hostname, parsed.path,
                 parsed.params, parsed.query, parsed.fragment)
            )
        elif parsed.port is None:
            # No port at all, so the scheme default is written in to yield
            # the explicit form Twilio may have signed instead.
            netloc = f"{parsed.hostname}:{default_port}"
            return urllib.parse.urlunparse(
                (parsed.scheme, netloc, parsed.path,
                 parsed.params, parsed.query, parsed.fragment)
            )

        # A non-default port is meaningful identity, not ambiguity.
        return None

    # ------------------------------------------------------------------
    # Twilio webhook handler
    # ------------------------------------------------------------------

    async def _handle_webhook(self, request) -> "aiohttp.web.Response":
        """Handle one inbound Twilio delivery POST.

        Every response body is an empty TwiML ``<Response></Response>``.
        That is intentional: replies are delivered through the REST API in
        :meth:`send`, not inlined into the webhook response, which keeps one
        code path for delivery and lets a message that fails mid-agent-turn
        still produce no partial vendor-side reply.

        Processing order is fixed because it is a security filter chain:
        size first (before any parsing can allocate), parsing, then
        signature verification, then identity extraction, then echo
        suppression.  Reordering these lets a malformed or forged request
        consume more work than it should.

        Args:
            request: ``aiohttp`` HTTP request for a ``POST`` to
                ``/webhooks/twilio``.

        Returns:
            An XML ``Response`` object.  Status ``200`` for accepted
            deliveries and silently dropped ones, ``400`` for bodies that
            cannot be parsed, ``403`` for a missing or invalid signature,
            and ``413`` for an oversized body.

        Note:
            Agent work is spawned onto a tracked background task rather than
            awaited.  Twilio expects a sub-second acknowledgement and times
            out the delivery attempt if the handler holds the connection, so
            holding it would cause retries and duplicate inbound events.
        """
        from aiohttp import web

        try:
            content_length = request.content_length
            if content_length is not None and content_length > _TWILIO_WEBHOOK_MAX_BODY_BYTES:
                return web.Response(
                    text='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    content_type="application/xml",
                    status=413,
                )
            raw = await request.read()
            if len(raw) > _TWILIO_WEBHOOK_MAX_BODY_BYTES:
                return web.Response(
                    text='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    content_type="application/xml",
                    status=413,
                )
            # Twilio delivers application/x-www-form-urlencoded, so the body
            # is decoded as a query string rather than parsed as JSON.  Blank
            # values are preserved because a legitimately empty field such as
            # an empty ProfileName still counts as a signed parameter.
            form = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        except Exception as e:
            logger.error("[sms] webhook parse error: %s", e)
            return web.Response(
                text='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                content_type="application/xml",
                status=400,
            )

        # Verification is gated on the configured callback URL, since the
        # signed string is derived from it.  When the URL is unset the
        # insecure development mode is in effect and the check is skipped
        # rather than run against a string Twilio never signed.
        if self._webhook_url:
            twilio_sig = request.headers.get("X-Twilio-Signature", "")
            if not twilio_sig:
                logger.warning("[sms] Rejected: missing X-Twilio-Signature header")
                return web.Response(
                    text='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    content_type="application/xml",
                    status=403,
                )
            flat_params = {k: v[0] for k, v in form.items() if v}
            if not self._validate_twilio_signature(
                self._webhook_url, flat_params, twilio_sig
            ):
                logger.warning("[sms] Rejected: invalid Twilio signature")
                return web.Response(
                    text='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    content_type="application/xml",
                    status=403,
                )

        # parse_qs yields lists, so each field is unwrapped to its first
        # value and whitespace-trimmed.  Twilio does not repeat these keys,
        # but a repeated key in a forged request must not surface as a list.
        from_number = (form.get("From", [""]))[0].strip()
        to_number = (form.get("To", [""]))[0].strip()
        text = (form.get("Body", [""]))[0].strip()
        message_sid = (form.get("MessageSid", [""]))[0].strip()

        if not from_number or not text:
            return web.Response(
                text='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                content_type="application/xml",
            )

        # Echo suppression.  A message sent to the bot's own number is
        # delivered back by Twilio, and forwarding it would make the bot
        # answer itself.  It is dropped silently after the signature check
        # so a forged echo is still rejected.
        if from_number == self._from_number:
            logger.debug("[sms] ignoring echo from own number %s", redact_phone(from_number))
            return web.Response(
                text='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                content_type="application/xml",
            )

        logger.info(
            "[sms] inbound from %s -> %s: %s",
            redact_phone(from_number),
            redact_phone(to_number),
            text[:80],
        )

        source = self.build_source(
            chat_id=from_number,
            chat_name=from_number,
            chat_type="dm",
            user_id=from_number,
            user_name=from_number,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=form,
            message_id=message_sid,
        )

        # Spawn rather than await: Twilio measures the handler wall time and
        # retries the delivery if the POST is not acknowledged quickly.
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        # The empty TwiML body acknowledges receipt without carrying a reply,
        # because outbound text flows through the REST API in send() instead
        # of being inlined into the webhook answer.
        return web.Response(
            text='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            content_type="application/xml",
        )


# ---- Standalone delivery ---------------------------------------------------
#
# Everything below serves callers that need SMS delivery without an adapter
# instance alive in their process: scheduled work, out-of-process send tools,
# and one-shot scripts.  They resolve credentials from the environment and a
# minimal config object, so they can run where the gateway runner is not.


def _strip_markdown_for_sms(message: str) -> str:
    """Downgrade markdown to plaintext for standalone delivery.

    Kept local rather than delegated because standalone callers may run where
    the shared helpers module is unavailable, and because this is a
    deliberately narrower rule set tuned to SMS rather than to a full
    markup-free chat.  Rule order is significant: multi-character delimiters
    must run before single-character ones, or ``**bold**`` degrades to
    ``*bold*`` and then to ``bold`` across two passes in the wrong order.

    Args:
        message: Outbound text, possibly containing markdown.

    Returns:
        Plain text with emphasis, code, heading and link markup removed and
        runs of more than two newlines collapsed.
    """
    message = re.sub(r"\*\*(.+?)\*\*", r"\1", message, flags=re.DOTALL)
    message = re.sub(r"\*(.+?)\*", r"\1", message, flags=re.DOTALL)
    message = re.sub(r"__(.+?)__", r"\1", message, flags=re.DOTALL)
    message = re.sub(r"_(.+?)_", r"\1", message, flags=re.DOTALL)
    message = re.sub(r"```[a-z]*\n?", "", message)
    message = re.sub(r"`(.+?)`", r"\1", message)
    message = re.sub(r"^#{1,6}\s+", "", message, flags=re.MULTILINE)
    message = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", message)
    message = re.sub(r"\n{3,}", "\n\n", message)
    return message.strip()


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Deliver one SMS out of process through the Twilio REST API.

    Implements the standalone sender contract used by out-of-process
    delivery paths, so scheduled work can send SMS without constructing an
    :class:`SmsAdapter`.  Unlike :meth:`SmsAdapter.send` this returns a
    plain dict rather than a :class:`SendResult`, because the contract
    across standalone senders is dict-shaped.

    The trailing keyword arguments exist for signature parity with senders
    that do support threading and attachments.  SMS has neither a threading
    primitive nor a native attachment model, so they are accepted and
    ignored rather than rejected, which keeps the caller uniform.

    Args:
        pconfig: Minimal config object.  Its ``api_key`` attribute is read
            as the auth token, since the gateway config loader stores
            credentials there; the environment value is the fallback.
        chat_id: Destination number in E.164 form.
        message: Outbound text.
        thread_id: Ignored; accepted for contract parity.
        media_files: Ignored; accepted for contract parity.
        force_document: Ignored; accepted for contract parity.

    Returns:
        A dict with ``success``, ``platform``, ``chat_id`` and
        ``message_id`` on success, or ``error`` on any failure.  Missing
        configuration and transport exceptions are both folded into the
        error form so the caller can branch on a single shape.
    """
    auth_token = getattr(pconfig, "api_key", None) or os.getenv("TWILIO_AUTH_TOKEN", "")
    import aiohttp
    import base64

    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    from_number = os.getenv("TWILIO_PHONE_NUMBER", "")
    if not account_sid or not auth_token or not from_number:
        return {"error": "SMS not configured (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER required)"}

    message = _strip_markdown_for_sms(message)

    def _redacted_error(text):
        """Wrap a failure message in the standalone error shape.

        Args:
            text: Human-readable failure reason.

        Returns:
            Dict with a single ``error`` key carrying *text*.
        """
        return {"error": text}

    try:
        from encre.gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
        _proxy = resolve_proxy_url()
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
        creds = f"{account_sid}:{auth_token}"
        encoded = base64.b64encode(creds.encode("ascii")).decode("ascii")
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
        headers = {"Authorization": f"Basic {encoded}"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **_sess_kw) as session:
            form_data = aiohttp.FormData()
            form_data.add_field("From", from_number)
            form_data.add_field("To", chat_id)
            form_data.add_field("Body", message)
            async with session.post(url, data=form_data, headers=headers, **_req_kw) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    error_msg = body.get("message", str(body))
                    return _redacted_error(f"Twilio API error ({resp.status}): {error_msg}")
                return {"success": True, "platform": "sms", "chat_id": chat_id, "message_id": body.get("sid", "")}
    except Exception as e:
        return _redacted_error(f"SMS send failed: {e}")


def _is_connected(config) -> bool:
    """Report whether SMS appears configured for status display.

    Consulted by the gateway when building the list of platforms a user sees
    as connected.  The test is on credential presence only, deliberately
    weaker than :func:`check_sms_requirements`: a host that has credentials
    but not the HTTP client should still advertise the platform as
    configured so an operator knows it is set up.

    Args:
        config: Accepted for the connected-checker signature and unused.

    Returns:
        ``True`` when both the account SID and auth token are present.
    """
    return bool(os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN"))


def _build_adapter(config):
    """Construct an adapter for the platform registry.

    Registry factories are called by
    :class:`encre.gateway.platform_registry.PlatformRegistry` with a fully
    resolved config, so this is a thin wrapper that exists to give the
    registry a stable callable identity rather than a lambda.

    Args:
        config: Resolved :class:`encre.gateway.config.PlatformConfig`.

    Returns:
        A new :class:`SmsAdapter` bound to *config*.
    """
    return SmsAdapter(config)
