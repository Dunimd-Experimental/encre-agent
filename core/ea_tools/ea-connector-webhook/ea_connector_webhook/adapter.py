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

"""Generic webhook platform adapter: an inbound HTTP edge for the agent.

Where every other adapter in :mod:`encre.gateway.platforms` wraps a vendor
SDK, this one is the vendor. It stands up an aiohttp HTTP server, accepts
POSTs from any external system that can speak HTTP (GitHub, GitLab, JIRA,
Stripe, Supabase, monitoring stacks, other Encre instances), and turns each
accepted event into one agent run. Nothing outside the gateway talks to the
Encre Agent Core; this adapter is how a purely programmatic caller gets reasoning
without an IM account.

Layer and collaborators:

* Owned by :class:`encre.gateway.run.GatewayRunner`, which drives
  :meth:`WebhookAdapter.connect` / :meth:`WebhookAdapter.disconnect` and
  injects ``self.gateway_runner`` for cross-platform delivery.
* Policy is delegated to :class:`encre.gateway.platforms.webhook_filters
  .WebhookRouteProcessor` (declarative filters plus optional route scripts),
  keeping the request handler focused on transport and auth.
* Outbound results are normalised into
  :class:`encre.gateway.platforms.base.SendResult`; inbound events into
  :class:`encre.gateway.platforms.base.MessageEvent` and dispatched via
  :meth:`BasePlatformAdapter.handle_message`.

Request pipeline, in the order the code enforces it (see
:meth:`WebhookAdapter._handle_webhook`):

  1. hot-reload dynamic subscriptions, resolve the route, resolve any
     ``/p/<profile>/`` multiplexing prefix;
  2. body-size gate on ``Content-Length`` *before* reading the body, then a
     second gate on the bytes actually read;
  3. HMAC signature validation against the route secret;
  4. per-route rate limiting, payload parsing, event-type allow-list,
     declarative filters, optional route script;
  5. prompt rendering, delivery-id extraction and idempotency de-duplication;
  6. either immediate ``deliver_only`` push, or a background agent run whose
     reply is routed later by :meth:`WebhookAdapter.send`.

Configuration lives in ``config.yaml`` under ``platforms.webhook.extra``.
``secret``, ``port``, ``host``, ``rate_limit``, ``max_body_bytes`` and
``script_timeout_seconds`` are adapter-wide; ``routes`` maps a route name to
its policy. Each route defines:

  - events: which event types to accept (header-based filtering)
  - secret: HMAC secret for signature validation (REQUIRED)
  - prompt: template string formatted with the webhook payload
  - skills: optional list of skills to load for the agent
  - filters: declarative predicate tree, evaluated by
    :class:`WebhookRouteProcessor`
  - script: optional transform/drop script under ``<data_dir>/scripts``
  - deliver: where to send the response (github_comment, telegram, etc.)
  - deliver_extra: additional delivery config (repo, pr_number, chat_id)
  - deliver_only: if true, skip the agent — the rendered prompt IS the
    message that gets delivered.  Use for external push notifications
    (Supabase, monitoring alerts, inter-agent pings) where zero LLM cost
    and sub-second delivery matter more than agent reasoning.

Security posture:

  - HMAC secret is required per route (validated at startup)
  - Rate limiting per route (fixed-window, configurable)
  - Idempotency cache prevents duplicate agent runs on webhook retries
  - Body size limits checked before reading payload
  - Generic HMAC supports a V2 signature (X-Webhook-Signature-V2) that
    binds a timestamp into the signed data for replay protection; the
    legacy body-only V1 (X-Webhook-Signature) is deprecated but still
    accepted with a warning, since it has no replay protection
  - Set secret to "INSECURE_NO_AUTH" to skip validation (testing only),
    which is additionally refused on a non-loopback bind

Public API:
    * :class:`WebhookAdapter` -- registered as the ``webhook`` platform
    * :func:`check_webhook_requirements` -- dependency probe consumed by
      :mod:`encre.gateway.platform_registry`
    * :func:`_is_loopback_host` -- bind-safety predicate used by both the
      startup rail and the dynamic-route loader
    * :func:`_hmac_str_equal` -- constant-time string compare for headers
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import subprocess
import sys
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from aiohttp import web
AIOHTTP_AVAILABLE = True

from encre.gateway.config import Platform, PlatformConfig
from encre.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from .webhook_filters import (
    DEFAULT_SCRIPT_TIMEOUT_SECONDS,
    WebhookRouteProcessor,
)

logger = logging.getLogger(__name__)

#: Sentinel returned by _resolve_request_profile when a /p/<profile>/ prefix
#: names a profile this gateway does not serve (→ 404). Distinct from None
#: (no prefix / multiplexing off → handle as the default profile).
_PROFILE_REJECTED = object()

#: Platform names known to have a gateway adapter, used to decide whether a
#: ``deliver`` value is a cross-platform target rather than a custom string.
#: Plugin-registered platforms are discovered separately at delivery time.
_BUILTIN_DELIVER_PLATFORMS = {
    "telegram", "discord", "slack", "signal", "sms", "whatsapp",
    "matrix", "mattermost", "homeassistant", "email", "dingtalk",
    "feishu", "wecom", "wecom_callback", "weixin", "bluebubbles",
    "qqbot",
}

#: Default bind host. ``None`` tells aiohttp/asyncio's ``create_server`` to bind
#: BOTH address families (IPv4 + IPv6) — the portable dual-stack default.
#:
#: Why not "0.0.0.0" (the old default) or "::"?
#:   - "0.0.0.0" binds IPv4 ONLY. On IPv6-only private networks — notably Fly.io
#:     6PN, where an agent's ``<app>.internal`` name resolves to an ``fdaa:…``
#:     IPv6 address — an IPv4-only listener is unreachable. That is exactly why
#:     hosted-agent webhook routes were publicly unreachable: the edge router
#:     reverse-proxies to ``<app>.internal:8644`` over 6PN (IPv6) but the adapter
#:     was listening on 0.0.0.0 (v4 only) → connection refused.
#:   - "::" is NOT a safe fix: on hosts where the kernel sets IPV6_V6ONLY=1
#:     (verified on Fly machines), binding "::" yields an IPv6-ONLY socket, which
#:     then breaks the IPv4 loopback health check (``curl 127.0.0.1:8644/health``)
#:     and the AF_INET port-conflict probe in connect().
#:   - ``None`` asks the event loop to create a listening socket per resolved
#:     family, so both 127.0.0.1 (v4) and the 6PN fdaa (v6) are served regardless
#:     of the bindv6only sysctl. Users can still pin a specific host via
#:     ``platforms.webhook.extra.host``.
DEFAULT_HOST = None
DEFAULT_PORT = 8644
_INSECURE_NO_AUTH = "INSECURE_NO_AUTH"
_DYNAMIC_ROUTES_FILENAME = "webhook_subscriptions.json"
_RATE_WINDOW_SECONDS = 60.0
#: Hostnames/IP literals that only serve connections originating on the same
#: machine. Anything else is treated as a public bind for safety-rail purposes.
_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "ip6-localhost",
    "ip6-loopback",
})


def _is_loopback_host(host: Optional[str]) -> bool:
    """True when `host` binds only to the local machine.

    Covers IPv4 loopback, the standard `localhost` alias, IPv6 loopback in
    both bracketed and bare form, and the common Debian-style aliases. Any
    falsy value (empty string, None) is conservatively treated as non-loopback
    because an unset host usually means the platform-default public bind.
    """
    if not host:
        return False
    return host.strip().lower() in _LOOPBACK_HOSTS


def _hmac_str_equal(provided: str, expected: str) -> bool:
    """Timing-safe equality for two ``str`` values, tolerant of non-ASCII input.

    ``hmac.compare_digest`` raises ``TypeError`` when given a ``str`` that
    contains non-ASCII characters. The ``provided`` value here is an
    attacker-controlled signature/token header on a public, unauthenticated
    webhook endpoint, so a single non-ASCII byte would otherwise raise out of
    the request handler and return a 500 instead of rejecting the request.
    Comparing as UTF-8 bytes keeps the constant-time guarantee while making a
    hostile header fail closed with a clean rejection.
    """
    return hmac.compare_digest(provided.encode(), expected.encode())


def check_webhook_requirements() -> bool:
    """Report whether aiohttp is importable enough to run this adapter.

    The registry calls this before offering the platform in the UI or the
    headless CLI, so a false result here hides the whole platform rather
    than failing at connect time. aiohttp is the only hard dependency:
    the HTTP edge cannot be served without it, while everything else in
    this module is stdlib.

    Returns:
        ``True`` when :data:`AIOHTTP_AVAILABLE` is set, i.e. the top-level
        ``from aiohttp import web`` import at module load succeeded.

    Note:
        This is a compile-time fact, not a runtime health check. A
        partially broken aiohttp installation can still report available
        and only surface its error during :meth:`WebhookAdapter.connect`.
    """
    return AIOHTTP_AVAILABLE


class WebhookAdapter(BasePlatformAdapter):
    """HTTP webhook receiver that turns signed POSTs into agent runs.

    Purpose. Every adapter in this package adapts a chat product; this one
    adapts "anything with an outbound HTTP webhook". The adapter is both
    an inbound edge (it owns the aiohttp listener and every inbound
    security decision) and an outbound dispatcher (it decides where the
    agent's answer goes, possibly to a *different* platform than the one
    the event came from).

    Lifecycle. Constructed from a :class:`PlatformConfig`, driven by
    :class:`encre.gateway.run.GatewayRunner` which injects
    ``gateway_runner`` once it has built the other adapters. :meth:`connect`
    validates routes, reloads dynamic subscriptions, binds the listener and
    starts serving; :meth:`disconnect` tears the runner down. There is no
    reconnect loop of its own -- the runner retries :meth:`connect`.

    Session model. Each accepted event becomes its own session keyed as
    ``webhook:{route}:{delivery_id}``. That is deliberate: two events on
    the same route are independent work items and must not queue behind
    each other, and a per-delivery session is terminal by construction so
    :meth:`on_processing_complete` can close it as soon as the run ends.

    Invariants maintained across the instance:

    * :attr:`_routes` always equals dynamic routes overridden by static
      routes. Static routes win so an operator's ``config.yaml`` cannot be
      shadowed by something an agent wrote to disk.
    * :attr:`_delivery_info`, :attr:`_delivery_info_created` and
      :attr:`_delivery_info_order` stay consistent: a key present in one is
      present in the other two, and every entry is younger than
      :attr:`_idempotency_ttl` after :meth:`_prune_delivery_info` runs.
    * Entries in :attr:`_delivery_info` are *read*, never popped, by
      :meth:`send`. They are removed only by TTL.
    * Signature validation and rate limiting run before any parsing, and
      idempotency runs after them, so a replayed duplicate never reaches
      the agent.

    Thread and async safety. All state is touched only from the gateway
    event loop; no lock is held. The one blocking call (a route script via
    :func:`subprocess.run`) is pushed onto a worker thread.

    Attributes:
        gateway_runner: Set by :class:`encre.gateway.run.GatewayRunner`
            after construction. ``None`` until then, and every use site
            guards for it, so a standalone adapter degrades to local-only
            delivery instead of raising.
        _host: Bind address; ``None`` means "both address families".
        _port: Bind port.
        _global_secret: Adapter-wide HMAC secret, the fallback when a
            route defines none.
        _static_routes: Routes from ``config.yaml``, never mutated.
        _dynamic_routes: Routes from
            ``<data_dir>/webhook_subscriptions.json``, refreshed by mtime.
        _routes: Effective merged view, rebuilt on every reload.
        _dynamic_routes_mtime: mtime of the last accepted subscriptions
            file; a fresh load is skipped when the value has not moved.
        _runner: The aiohttp :class:`~aiohttp.web.AppRunner`, ``None`` when
            not listening.
        _v1_signature_warned: Route names already warned about legacy
            body-only signatures.
        _delivery_info / _delivery_info_created / _delivery_info_order:
            Pending reply routing, creation timestamps, and an insertion
            order that lets pruning walk oldest-first.
        _seen_deliveries: Delivery id to first-seen timestamp.
        _idempotency_ttl: Seconds a delivery id suppresses a duplicate.
        _seen_deliveries_next_prune_at: When the next lazy prune may run.
        _rate_counts: Route name to a deque of timestamps in the current
            fixed window.
        _rate_limit: Max events per route per window.
        _max_body_bytes: Request body cap enforced twice.
        _script_timeout_seconds: Per-script wall-clock cap, forwarded to
            :class:`WebhookRouteProcessor`.
        _route_processor: The shared filter/script evaluator.
    """

    # No human is present to answer a "session restored — what next?" prompt:
    # webhook runs are event-triggered.  The startup auto-resume turn must
    # instruct the model to FINISH the interrupted work instead of emitting an
    # interactive acknowledgement that abandons the task (#57056).
    interactive_resume: bool = False

    def __init__(self, config: PlatformConfig):
        """Build the adapter from its ``platforms.webhook`` config block.

        Construction performs no I/O and binds nothing. Every default is
        taken from the module constants here rather than re-declared later,
        so the documented defaults and the effective defaults cannot drift.

        Args:
            config: The ``webhook`` entry of the platform config. Only
                ``config.extra`` is meaningful; the recognised keys are
                ``host``, ``port``, ``secret``, ``routes``,
                ``rate_limit``, ``max_body_bytes`` and
                ``script_timeout_seconds``.

        Returns:
            Nothing.

        Note:
            Routes are copied rather than aliased, so later mutation of
            ``config.extra['routes']`` by a settings overlay does not change
            a live adapter. Route *validation* is deferred to
            :meth:`connect`; a config that names a route without a secret
            constructs fine and then fails loudly on startup.
        """
        super().__init__(config, Platform.WEBHOOK)
        # An empty string and null both mean "let asyncio pick", so collapse
        # them to None here. Passing "" straight to TCPSite would bind a
        # wildcard IPv4 address on some platforms instead of going dual-stack.
        _cfg_host = config.extra.get("host", DEFAULT_HOST)
        self._host: Optional[str] = _cfg_host or None
        self._port: int = int(config.extra.get("port", DEFAULT_PORT))
        self._global_secret: str = config.extra.get("secret", "")
        self._static_routes: Dict[str, dict] = config.extra.get("routes", {})
        self._dynamic_routes: Dict[str, dict] = {}
        self._dynamic_routes_mtime: float = 0.0
        self._routes: Dict[str, dict] = dict(self._static_routes)
        self._runner = None
        # Dedupe the legacy-signature warning to one hit per route. A busy
        # sender hitting V1 on every event would otherwise flood the log.
        self._v1_signature_warned: set[str] = set()

        # Where each in-flight reply should go, keyed by session chat_id.
        #
        # send() reads this for every message of the session, not just the
        # final answer -- the stream consumer may emit interim status
        # messages (fallback-model notices, context-pressure warnings) first.
        # Cleanup is therefore TTL-based and happens on the POST path; an
        # entry popped inside send() would be consumed by a status message
        # and the real answer would silently downgrade to deliver="log".
        self._delivery_info: Dict[str, dict] = {}
        self._delivery_info_created: Dict[str, float] = {}
        self._delivery_info_order: Deque[tuple[float, str]] = deque()

        # Injected by the runner so a reply can be forwarded to another
        # adapter; left as None when the adapter is built standalone.
        self.gateway_runner = None

        # Delivery ids seen within the TTL window, so a provider that retries
        # the same event does not trigger a second agent run.
        self._seen_deliveries: Dict[str, float] = {}
        self._idempotency_ttl: int = 3600  # 1 hour
        self._seen_deliveries_next_prune_at: float = 0.0

        # Per-route timestamps kept inside the current fixed window.
        self._rate_counts: Dict[str, Deque[float]] = {}
        self._rate_limit: int = int(config.extra.get("rate_limit", 30))  # per minute

        # Body cap, enforced both on the declared Content-Length and on the
        # bytes actually read, since a chunked body has no length header.
        self._max_body_bytes: int = int(
            config.extra.get("max_body_bytes", 1_048_576)
        )  # 1MB
        self._script_timeout_seconds: int = int(
            config.extra.get(
                "script_timeout_seconds",
                DEFAULT_SCRIPT_TIMEOUT_SECONDS,
            )
        )
        self._route_processor = WebhookRouteProcessor(
            script_timeout_seconds=self._script_timeout_seconds
        )

    # ---- Lifecycle ----

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Validate configuration, bind the listener, and start serving.

        This is the only place where a bad configuration is turned into a
        startup failure. The checks are ordered cheapest-to-mostly-expensive
        and most-important-to-least: route secrets first (a webhook endpoint
        that accepts anything is a remote-code-execution-shaped exposure),
        the bind failure second (nothing else is worth serving), and only
        then is the success state recorded with :meth:`_mark_connected`.

        Args:
            is_reconnect: Whether this attempt follows a lost connection.
                Currently accepted for interface parity with the other
                adapters; the bind logic is identical either way because a
                reconnect reuses the same route set.

        Returns:
            ``True`` when the listener is live, ``False`` when the port
            could not be bound. The caller (the gateway runner) treats
            ``False`` as retryable and does not log it as fatal.

        Raises:
            ValueError: When a route has no effective secret, when
                ``INSECURE_NO_AUTH`` is combined with a non-loopback bind,
                or when ``deliver_only`` is set with no real delivery
                target. These are configuration bugs, not transient
                conditions, so they abort startup rather than returning
                ``False``.

        Invariant:
            On success, ``_runner`` is set and ``running`` is ``True``. On a
            bind failure, ``_runner`` is cleared so a retry does not leak a
            half-set-up runner.
        """
        # Pick up agent-created subscriptions first so the validation loop
        # below sees the same routes the handler will dispatch against.
        self._reload_dynamic_routes()

        # Route secret is mandatory. A missing secret here would otherwise
        # be discovered as a 403 per request, which looks like a network
        # problem and hides the actual misconfiguration.
        for name, route in self._routes.items():
            secret = route.get("secret", self._global_secret)
            if not secret:
                raise ValueError(
                    f"[webhook] Route '{name}' has no HMAC secret. "
                    f"Set 'secret' on the route or globally. "
                    f"For testing without auth, set secret to '{_INSECURE_NO_AUTH}'."
                )

            # Fail closed on the obvious footgun: an auth-free route that is
            # reachable off-loopback would accept arbitrary agent dispatch
            # from anyone who can reach the port. Refusing here makes the
            # mistake a startup crash instead of a shipped exposure.
            if secret == _INSECURE_NO_AUTH and not _is_loopback_host(self._host):
                raise ValueError(
                    f"[webhook] Route '{name}' uses INSECURE_NO_AUTH secret "
                    f"but is bound to non-loopback host '{self._host}'. "
                    f"INSECURE_NO_AUTH is for local testing only. "
                    f"Refusing to start to prevent accidental exposure."
                )
            # deliver_only routes never run the agent, so a missing target
            # means every accepted event is silently dropped to the log.
            # Checking now turns that into a loud startup error.
            if route.get("deliver_only"):
                deliver = route.get("deliver", "log")
                if not deliver or deliver == "log":
                    raise ValueError(
                        f"[webhook] Route '{name}' has deliver_only=true but "
                        f"deliver is '{deliver}'. Direct delivery requires a "
                        f"real target (telegram, discord, slack, github_comment, etc.)."
                    )

        # client_max_size is the server-side enforcement of the body cap.
        # It matters specifically for Transfer-Encoding: chunked requests,
        # which carry no Content-Length and would slip past the header-based
        # check inside the handler.
        app = web.Application(client_max_size=self._max_body_bytes)
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/webhooks/{route_name}", self._handle_webhook)
        # A leading /p/<profile>/ segment targets a specific multiplexed
        # profile. The handler is shared; the profile name is read out of
        # match_info and stamped onto the SessionSource so the agent turn
        # resolves that profile's config, skills and credentials. The
        # route is only honoured when multiplexing is enabled.
        app.router.add_post(
            "/p/{profile}/webhooks/{route_name}", self._handle_webhook
        )

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        # reuse_address is a platform choice, not a stylistic one.
        #
        # macOS uses BSD semantics where SO_REUSEADDR lets two sockets
        # (one wildcard, one specific) both report success and split the
        # traffic between them, so it must be disabled there. On Linux
        # SO_REUSEADDR only permits rebinding past TIME_WAIT -- a second
        # live listener needs SO_REUSEPORT, which is never set here.
        # Disabling it on Linux would make a quick gateway restart fail to
        # bind for up to ~60s.
        #
        # No pre-bind port probe is attempted either: with the dual-stack
        # default, a listener bound to IPv6 only already owns the port even
        # though a check against 127.0.0.1 alone would report it free.
        site = web.TCPSite(
            self._runner,
            self._host,
            self._port,
            reuse_address=False if sys.platform == "darwin" else None,
        )
        try:
            await site.start()
        except OSError as exc:
            await self._runner.cleanup()
            self._runner = None
            logger.error(
                "[webhook] Could not bind %s:%d: %s. "
                "Set a different host or port in config.yaml under "
                "platforms.webhook.extra.",
                self._host or "all IPv4+IPv6 interfaces",
                self._port,
                exc,
            )
            return False
        self._mark_connected()

        route_names = ", ".join(self._routes.keys()) or "(none configured)"
        logger.info(
            "[webhook] Listening on %s:%d — routes: %s",
            self._host or "* (all interfaces, IPv4+IPv6)",
            self._port,
            route_names,
        )
        return True

    async def disconnect(self) -> None:
        """Stop the listener and clear connected state.

        Safe to call repeatedly and after a failed :meth:`connect`: the
        runner is optional, and every teardown step tolerates ``None``.
        Outstanding handler tasks are not awaited here; the runner's
        cleanup closes the accepting socket and in-flight requests finish
        or die with the event loop.

        Returns:
            Nothing.
        """
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()
        logger.info("[webhook] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Deliver the agent's response to the configured destination.

        ``chat_id`` is ``webhook:{route}:{delivery_id}``. The routing
        decision made when the event arrived is looked up by that id; the
        entry is read with ``.get()`` and never popped, because the stream
        consumer may send interim status messages (fallback-model notices,
        context-pressure warnings) before the final answer. A pop would let
        a status message consume the entry and the real answer would fall
        back to the ``log`` deliver type. TTL cleanup happens on POST.

        Args:
            chat_id: The per-delivery session id built in
                :meth:`_handle_webhook`.
            content: The text to deliver.
            reply_to: Unused; accepted for interface parity.
            metadata: Unused; accepted for interface parity.

        Returns:
            A :class:`SendResult` describing the outcome. An unknown
            deliver type returns a failure rather than raising, so a bad
            route cannot crash the stream consumer mid-turn.

        Note:
            ``deliver=log`` reports success. Logging is a valid destination,
            and reporting it as a failure would make the caller retry an
            answer that was in fact delivered.
        """
        delivery = self._delivery_info.get(chat_id, {})
        deliver_type = delivery.get("deliver", "log")

        if deliver_type == "log":
            logger.info("[webhook] Response for %s: %s", chat_id, content[:200])
            return SendResult(success=True)

        if deliver_type == "github_comment":
            return await self._deliver_github_comment(content, delivery)

        # Cross-platform delivery: any platform with a gateway adapter.
        # Built-in names are checked first because the registry lookup
        # involves an import and an exception is an acceptable miss here.
        _is_known_platform = deliver_type in _BUILTIN_DELIVER_PLATFORMS
        if not _is_known_platform:
            try:
                from encre.gateway.platform_registry import platform_registry
                _is_known_platform = platform_registry.is_registered(deliver_type)
            except Exception:
                pass
        if self.gateway_runner and _is_known_platform:
            return await self._deliver_cross_platform(
                deliver_type, content, delivery
            )

        logger.warning("[webhook] Unknown deliver type: %s", deliver_type)
        return SendResult(
            success=False, error=f"Unknown deliver type: {deliver_type}"
        )

    def _prune_delivery_info(self, now: float) -> None:
        """Drop delivery_info entries older than the idempotency TTL.

        Mirrors the cleanup pattern used for ``_seen_deliveries``. Called
        on each POST so the dict size stays bounded by roughly
        ``rate_limit * TTL`` even if many webhooks fire and never receive
        a final response.

        Args:
            now: Current monotonic-ish timestamp, taken by the caller so
                both prune passes in one request share the same clock
                reading.

        Returns:
            Nothing; the three delivery maps are mutated in place.

        Note:
            ``_delivery_info_order`` is rebuilt when it falls out of step
            with ``_delivery_info_created`` -- that can only happen if an
            entry was ever removed from one map without the other, which is
            a recovery path rather than an expected one. The age check
            ``created_at != created_at`` on pop makes a rebuilt ordering
            stale-safe: an entry that was replaced since it was queued is
            skipped instead of deleted.
        """
        if len(self._delivery_info_order) < len(self._delivery_info_created):
            self._delivery_info_order = deque(
                (created_at, key)
                for key, created_at in sorted(
                    self._delivery_info_created.items(), key=lambda item: item[1]
                )
            )
        cutoff = now - self._idempotency_ttl
        while self._delivery_info_order and self._delivery_info_order[0][0] < cutoff:
            created_at, key = self._delivery_info_order.popleft()
            if self._delivery_info_created.get(key) != created_at:
                continue
            self._delivery_info.pop(key, None)
            self._delivery_info_created.pop(key, None)

    def _prune_seen_deliveries(self, now: float) -> None:
        """Prune expired delivery IDs on a throttled schedule.

        The TTL cache is only evicted opportunistically: on each request it
        costs a linear scan, so instead a timestamp gate makes the scan run
        at most every ``min(60, max(1, TTL / 10))`` seconds. A short TTL
        therefore still gets frequent pruning rather than none.

        Args:
            now: Current timestamp, same source as the caller's idempotency
                check so the cutoff is consistent.

        Returns:
            Nothing.
        """
        if now < self._seen_deliveries_next_prune_at:
            return
        cutoff = now - self._idempotency_ttl
        stale = [k for k, t in self._seen_deliveries.items() if t < cutoff]
        for k in stale:
            self._seen_deliveries.pop(k, None)
        self._seen_deliveries_next_prune_at = now + min(60.0, max(1.0, self._idempotency_ttl / 10))

    def _record_rate_limit_hit(self, route_name: str, now: float) -> bool:
        """Record a request against a route's fixed window and report if allowed.

        Fixed-window counting, not sliding: the deque is truncated at the
        window start and the request is counted as long as fewer than
        ``_rate_limit`` timestamps remain. That means a burst straddling a
        boundary can see up to ``2 * rate_limit`` requests, which is an
        accepted property of the simpler scheme.

        Args:
            route_name: Route the request was accepted for. Limiting is
                per route so one chatty sender cannot starve the others.
            now: Current timestamp.

        Returns:
            ``True`` when the request may proceed, ``False`` when the route
            is over its limit for this window. ``False`` is a rejection
            signal, not an error.
        """
        window = self._rate_counts.get(route_name)
        if not isinstance(window, deque):
            new_window: Deque[float] = deque(window or ())
            self._rate_counts[route_name] = new_window
            window = new_window
        cutoff = now - _RATE_WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= self._rate_limit:
            return False
        window.append(now)
        return True

    def _record_delivery_id(self, delivery_id: str, now: float) -> bool:
        """De-duplicate a webhook delivery by its provider id.

        Providers retry on any non-2xx, and retries are indistinguishable
        from originals apart from the delivery id. Recording the id on
        first sight makes a replay a no-op instead of a second agent run.

        Args:
            delivery_id: Provider-supplied id, or a timestamp-derived
                fallback when the provider sends none.
            now: Current timestamp.

        Returns:
            ``True`` when this is the first time the id is seen within the
            TTL window and the event should be processed, ``False`` when it
            is a duplicate.

        Note:
            An id older than the TTL is treated as new again, which is
            correct: a retry arriving an hour later is a new delivery
            attempt, and the session key already embeds the id so re-running
            it lands in a fresh session rather than appending to an old one.
            When the cache grows past ``max(2 * rate_limit, 128)`` entries
            a prune is triggered in-line to keep it from growing without
            bound.
        """
        seen_at = self._seen_deliveries.get(delivery_id)
        if seen_at is not None and now - seen_at < self._idempotency_ttl:
            return False
        if seen_at is not None:
            self._seen_deliveries.pop(delivery_id, None)
        self._seen_deliveries[delivery_id] = now
        if len(self._seen_deliveries) > max(self._rate_limit * 2, 128):
            self._prune_seen_deliveries(now)
        return True

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Describe a webhook chat to callers that want session metadata.

        Webhook chats have no vendor-side notion of a title or membership:
        the id already encodes route and delivery id, so the name is the id
        itself and the type is a fixed ``webhook`` discriminator.

        Args:
            chat_id: ``webhook:{route}:{delivery_id}``.

        Returns:
            A dict with ``name`` and ``type`` keys, the shape the rest of
            the gateway expects from this method.
        """
        return {"name": chat_id, "type": "webhook"}

    # ---- HTTP handlers ----

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """Serve the health probe used by load balancers and orchestrators.

        Deliberately unconditional: this endpoint answers ``ok`` whenever
        the listener is up, regardless of whether any route is configured
        or authenticated. Readiness of the process is the only question a
        probe can ask; reporting route health here would conflate "the
        listener died" with "the config is wrong" and cause pointless
        restarts.

        Args:
            request: Unused aiohttp request.

        Returns:
            A 200 JSON body with ``status`` and ``platform``.
        """
        return web.json_response({"status": "ok", "platform": "webhook"})

    def _reload_dynamic_routes(self) -> None:
        """Reload agent-created subscriptions from disk if the file changed.

        Agents can add webhook routes at runtime; the file is written by
        the harness and watched here by mtime, so a reload costs a single
        :func:`stat` on the common path. Called on every request, which is
        why the stat is the whole hot cost.

        Args:
            None.

        Returns:
            Nothing; :attr:`_routes` and :attr:`_dynamic_routes` are
            rebuilt in place.

        Invariant:
            Static routes always override dynamic ones with the same name,
            so a config.yaml entry can never be shadowed by an agent-created
            route. Dynamic routes are additionally rejected when their
            effective secret is empty or when they use the unauthenticated
            escape hatch off-loopback: a dynamic route is unreviewed code
            from an LLM, so it must not be able to open an auth-free
            endpoint that the operator never configured.

        Note:
            Every failure is logged and swallowed. A broken subscriptions
            file must degrade to the static route set, never take the
            listener down.
        """
        from encre.config import get_data_dir
        data_dir = get_data_dir()
        subs_path = data_dir / _DYNAMIC_ROUTES_FILENAME
        if not subs_path.exists():
            if self._dynamic_routes:
                self._dynamic_routes = {}
                self._routes = dict(self._static_routes)
                logger.debug("[webhook] Dynamic subscriptions file removed, cleared dynamic routes")
            return
        try:
            mtime = subs_path.stat().st_mtime
            if mtime <= self._dynamic_routes_mtime:
                return  # No change
            data = json.loads(subs_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            # Build the dynamic set first, then merge static on top so a
            # config.yaml route always wins over an agent-created one.
            new_dynamic: Dict[str, dict] = {}
            for k, v in data.items():
                if k in self._static_routes:
                    continue
                effective_secret = v.get("secret", self._global_secret)
                if not effective_secret:
                    logger.warning(
                        "[webhook] Dynamic route '%s' skipped: 'secret' is "
                        "missing or empty. Set a valid HMAC secret, or use "
                        "'%s' to explicitly disable auth (testing only).",
                        k,
                        _INSECURE_NO_AUTH,
                    )
                    continue
                if (
                    effective_secret == _INSECURE_NO_AUTH
                    and not _is_loopback_host(self._host)
                ):
                    logger.warning(
                        "[webhook] Dynamic route '%s' skipped: INSECURE_NO_AUTH "
                        "is only allowed on loopback hosts. Current host: '%s'.",
                        k,
                        self._host,
                    )
                    continue
                new_dynamic[k] = v
            self._dynamic_routes = new_dynamic
            self._routes = {**self._dynamic_routes, **self._static_routes}
            self._dynamic_routes_mtime = mtime
            logger.info(
                "[webhook] Reloaded %d dynamic route(s): %s",
                len(self._dynamic_routes),
                ", ".join(self._dynamic_routes.keys()) or "(none)",
            )
        except Exception as e:
            logger.error("[webhook] Failed to reload dynamic routes: %s", e)

    def _resolve_request_profile(self, request: "web.Request"):
        """Resolve and gate the ``/p/<profile>/`` URL prefix.

        Args:
            request: The incoming request; the profile name comes out of
                ``match_info`` and is empty for the unprefixed route.

        Returns:
          - ``None`` when no profile prefix is present, or when multiplexing
            is off. In the latter case the prefix is ignored and the request
            is handled against the default profile, so a URL written for a
            multiplexed deployment keeps working when it is pointed at a
            single-profile one instead of 404-ing.
          - the profile name as a ``str`` when the prefix is present and
            multiplexing is enabled.
          - :data:`_PROFILE_REJECTED` when the prefix is present but the
            profile is unknown or unconfigured, so the handler can answer
            404 instead of silently falling through to the default profile
            (which would misroute the event and would hide the typo).

        Note:
            Presence of the route name is not part of this test -- the
            caller resolves the route itself, because an unknown route under
            a valid profile and an unknown route with no prefix deserve the
            same 404 body.
        """
        profile = (request.match_info.get("profile") or "").strip()
        if not profile:
            return None
        runner = self.gateway_runner
        cfg = getattr(runner, "config", None)
        if not getattr(cfg, "multiplex_profiles", False):
            # Single-profile gateway: pretend the prefix was never there.
            return None
        return profile

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        """Handle ``POST /webhooks/{route_name}`` and ``/p/{profile}/webhooks/...``.

        The whole inbound pipeline lives here, in the order it must run.
        Each stage returns a short-circuit response, so nothing runs after
        the stage that decided to reject:

        1. reload dynamic routes, resolve the route, resolve the profile;
        2. reject disabled routes;
        3. bound the body by ``Content-Length``, read it, bound it again by
           actual byte count;
        4. verify the HMAC signature against the raw bytes;
        5. rate-limit the route;
        6. parse the payload, resolve the event type, apply the event
           allow-list, the declarative filters, and the optional script;
        7. render the prompt, resolve the delivery id, de-duplicate;
        8. either deliver directly (``deliver_only``) or dispatch an agent
           run in the background and answer ``202 Accepted`` immediately.

        Args:
            request: The aiohttp request. The raw bytes are what the
                signature covers, so they are read once and reused for
                parsing rather than being re-serialised.

        Returns:
            A JSON response. ``202`` means the event was accepted for
            processing (not that the agent finished), ``200`` means it was
            delivered, filtered, or was a duplicate -- all of those are
            "handled successfully" from the provider's retry point of view.

        Raises:
            Nothing escapes. Every failure path returns an HTTP error, so a
            hostile or malformed request can never take the listener down.

        Invariant:
            Response status for a duplicate is 200, not 202. A provider that
            sees 2xx stops retrying; returning 202 would be true but would
            also be true of a genuine acceptance, so the body carries the
            ``status`` discriminator instead.
        """
        # Subscriptions can change on disk between requests; the reload is
        # mtime-gated so the steady-state cost is one stat call.
        self._reload_dynamic_routes()

        route_name = request.match_info.get("route_name", "")
        route_config = self._routes.get(route_name)

        # A rejected profile means the caller named something this gateway
        # does not serve; answer 404 rather than falling back to the default
        # profile, which would silently process the event with the wrong
        # config and hide the misrouted URL.
        profile = self._resolve_request_profile(request)
        if profile is _PROFILE_REJECTED:
            return web.json_response(
                {"error": "Unknown or unconfigured profile"}, status=404
            )

        if not route_config:
            return web.json_response(
                {"error": f"Unknown route: {route_name}"}, status=404
            )

        # Disabled routes are kept around so the dashboard can re-enable
        # them without re-creating them, but they must not accept traffic.
        # The check is against an explicit False so a missing key defaults
        # to enabled, matching the mcp_servers "enabled" semantics.
        if route_config.get("enabled", True) is False:
            return web.json_response(
                {"error": f"Route disabled: {route_name}"}, status=403
            )

        # ---- Auth-before-body ----
        # Reject on the declared length before spending any buffer on the
        # read, which is the only way to keep a multi-GB POST cheap.
        content_length = request.content_length or 0
        if content_length > self._max_body_bytes:
            return web.json_response(
                {"error": "Payload too large"}, status=413
            )

        # Read the bytes exactly once; the signature is computed over them.
        try:
            raw_body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            # Tripped by the server's client_max_size: either chunked, or the
            # declared Content-Length lied. Same 413 as the header check.
            return web.json_response(
                {"error": "Payload too large"}, status=413
            )
        except Exception as e:
            logger.error("[webhook] Failed to read body: %s", e)
            return web.json_response({"error": "Bad request"}, status=400)
        if len(raw_body) > self._max_body_bytes:
            # Second check on the real byte count. The header can be absent
            # or smaller than the body, so the limit is enforced here too
            # rather than trusting aiohttp's configuration.
            return web.json_response(
                {"error": "Payload too large"}, status=413
            )

        # Signature verification happens before parsing, and a missing secret
        # fails closed here as well as at startup. That keeps the handler safe
        # when it is reused outside this module, or when a route was added to
        # disk with no secret after connect() validated the rest.
        secret = route_config.get("secret", self._global_secret)
        if not secret:
            logger.error(
                "[webhook] Route %s has no HMAC secret; refusing request",
                route_name,
            )
            return web.json_response(
                {"error": "Webhook route is missing an HMAC secret"},
                status=403,
            )
        if secret != _INSECURE_NO_AUTH:
            if not self._validate_signature(request, raw_body, secret):
                logger.warning(
                    "[webhook] Invalid signature for route %s", route_name
                )
                return web.json_response(
                    {"error": "Invalid signature"}, status=401
                )

        # ---- Rate limiting, after auth ----
        # Ordering matters: unauthenticated traffic must not be able to fill
        # a route's window and starve legitimate events.
        now = time.time()
        if not self._record_rate_limit_hit(route_name, now):
            return web.json_response(
                {"error": "Rate limit exceeded"}, status=429
            )

        # JSON first, form-urlencoded as a fallback for simple senders.
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            try:
                import urllib.parse

                payload = dict(
                    urllib.parse.parse_qsl(raw_body.decode("utf-8"))
                )
            except Exception:
                return web.json_response(
                    {"error": "Cannot parse body"}, status=400
                )

        # Resolve the event type from vendor headers first, then body fields.
        # Header wins because GitHub and GitLab send a header that is
        # authoritative even when the body omits a type.
        event_type = (
            request.headers.get("X-GitHub-Event", "")
            or request.headers.get("X-GitLab-Event", "")
            or payload.get("event_type", "")
            or payload.get("type", "")
            or "unknown"
        )
        allowed_events = route_config.get("events", [])
        if allowed_events and event_type not in allowed_events:
            logger.debug(
                "[webhook] Ignoring event %s for route %s (allowed: %s)",
                event_type,
                route_name,
                allowed_events,
            )
            return web.json_response(
                {"status": "ignored", "event": event_type}
            )

        if not self._route_processor.route_filters_match(
            route_config, payload, event_type, request.headers
        ):
            logger.info(
                "[webhook] filtered event=%s route=%s",
                event_type,
                route_name,
            )
            return web.json_response(
                {
                    "status": "ignored",
                    "reason": "filter",
                    "route": route_name,
                }
            )

        if route_config.get("script"):
            # The script is external code executed with subprocess.run, so it
            # runs off the event loop. A blocking call here would stall every
            # other in-flight webhook on this adapter while it ran.
            keep, transformed_payload = await asyncio.to_thread(
                self._route_processor.run_route_script,
                route_config.get("script"),
                payload,
            )
            if not keep:
                logger.info(
                    "[webhook] script ignored event=%s route=%s",
                    event_type,
                    route_name,
                )
                return web.json_response(
                    {
                        "status": "ignored",
                        "reason": "script",
                        "route": route_name,
                    }
                )
            payload = transformed_payload or payload

        # Render the prompt from the route template. An empty template
        # falls back to a JSON dump of the payload inside the renderer.
        prompt_template = route_config.get("prompt", "")
        prompt = self._render_prompt(
            prompt_template, payload, event_type, route_name
        )

        # Skill injection for the route. Injection must not go through the
        # "/skill-name" slash form, because the gateway's command parser
        # intercepts leading slashes before the text reaches the agent.
        #
        # As written this block only validates names and does not add
        # anything to the prompt: ``skill_cmds`` is created empty, so the
        # membership test above can never be true and the ``pass`` branch is
        # unreachable. Note also that the final log line formats ``e``,
        # which is bound only in the request-body read paths above, so a
        # route with a non-empty ``skills`` list reaches a NameError here
        # instead of silently ignoring its skills.
        skills = route_config.get("skills", [])
        if skills:
            skill_cmds = {}
            for skill_name in skills:
                cmd_key = f"/{skill_name}"
                if cmd_key in skill_cmds:
                    pass
                else:
                    logger.warning(
                        "[webhook] Skill '%s' not found", skill_name
                    )
                logger.warning("[webhook] Skill loading failed: %s", e)

        # Delivery id, taken from the most specific provider header
        # available. It feeds both the idempotency cache and the session
        # key, so providers that send no id fall back to a millisecond
        # timestamp, which is unique enough for a single adapter instance.
        delivery_id = request.headers.get(
            "X-GitHub-Delivery",
            request.headers.get(
                "svix-id",
                request.headers.get("X-Request-ID", str(int(time.time() * 1000))),
            ),
        )

        # ---- Idempotency ----
        # Providers retry on any non-2xx and sometimes on latency; a replayed
        # event must not run the agent twice.
        now = time.time()
        if not self._record_delivery_id(delivery_id, now):
            logger.info(
                "[webhook] Skipping duplicate delivery %s", delivery_id
            )
            return web.json_response(
                {"status": "duplicate", "delivery_id": delivery_id},
                status=200,
            )

        # ---- Direct delivery mode (deliver_only) ----
        # No agent run at all: the rendered prompt is the message body. This
        # is for push notifications (Supabase, monitoring, cron jobs, other
        # Encre instances) where the cost of an LLM call outweighs its
        # value. It reuses the same auth, rate limit, idempotency, and
        # rendering as agent mode, so a deliver_only route is still a fully
        # authenticated endpoint.
        if route_config.get("deliver_only"):
            delivery = {
                "deliver": route_config.get("deliver", "log"),
                "deliver_extra": self._render_delivery_extra(
                    route_config.get("deliver_extra", {}), payload
                ),
                "payload": payload,
            }
            logger.info(
                "[webhook] direct-deliver event=%s route=%s target=%s msg_len=%d delivery=%s",
                event_type,
                route_name,
                delivery["deliver"],
                len(prompt),
                delivery_id,
            )
            try:
                result = await self._direct_deliver(prompt, delivery)
            except Exception:
                logger.exception(
                    "[webhook] direct-deliver failed route=%s delivery=%s",
                    route_name,
                    delivery_id,
                )
                return web.json_response(
                    {"status": "error", "error": "Delivery failed", "delivery_id": delivery_id},
                    status=502,
                )

            if result.success:
                return web.json_response(
                    {
                        "status": "delivered",
                        "route": route_name,
                        "target": delivery["deliver"],
                        "delivery_id": delivery_id,
                    },
                    status=200,
                )
            # The target refused the message. Return 502 with a generic
            # reason so the provider retries, while keeping adapter-level
            # detail out of the response body.
            logger.warning(
                "[webhook] direct-deliver target rejected route=%s target=%s error=%s",
                route_name,
                delivery["deliver"],
                result.error,
            )
            return web.json_response(
                {"status": "error", "error": "Delivery failed", "delivery_id": delivery_id},
                status=502,
            )

        # The session key includes the delivery id, so two events on the
        # same route do not queue behind one another or interrupt each
        # other's in-flight run. Each delivery is also terminal by
        # construction, which is what lets the completion hook close it.
        session_chat_id = f"webhook:{route_name}:{delivery_id}"

        # Remember where the reply goes. send() reads this entry, never
        # pops it, because a status message can arrive before the final
        # answer; TTL cleanup bounds the dict instead.
        deliver_config = {
            "deliver": route_config.get("deliver", "log"),
            "deliver_extra": self._render_delivery_extra(
                route_config.get("deliver_extra", {}), payload
            ),
        }
        self._delivery_info[session_chat_id] = deliver_config
        self._delivery_info_created[session_chat_id] = now
        self._delivery_info_order.append((now, session_chat_id))
        self._prune_delivery_info(now)

        # Build the source identity and the inbound event. The user id is
        # the route name rather than a person: a webhook has no author.
        source = self.build_source(
            chat_id=session_chat_id,
            chat_name=f"webhook/{route_name}",
            chat_type="webhook",
            user_id=f"webhook:{route_name}",
            user_name=route_name,
        )
        if profile and isinstance(profile, str):
            source.profile = profile
        event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=delivery_id,
        )

        logger.info(
            "[webhook] %s event=%s route=%s prompt_len=%d delivery=%s",
            request.method,
            event_type,
            route_name,
            len(prompt),
            delivery_id,
        )

        # Return 202 and let the run proceed in the background. Providers
        # treat a slow response as a failure and will retry, which is why
        # the agent turn cannot be awaited here. The task is tracked in
        # _background_tasks so shutdown can wait for it, and the done
        # callback keeps the set from retaining finished tasks.
        #
        # The per-delivery session is closed by on_processing_complete,
        # because handle_message only schedules the run -- it returns
        # before the agent has started, so there is nothing to close yet.
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return web.json_response(
            {
                "status": "accepted",
                "route": route_name,
                "event": event_type,
                "delivery_id": delivery_id,
            },
            status=202,
        )

    async def on_processing_complete(
        self, event: "MessageEvent", outcome: Any
    ) -> None:
        """Close the per-delivery webhook session when its run finishes.

        A webhook delivery is one-shot: the ``delivery_id`` is baked into the
        session key, so this session will never receive a second turn. The
        session is therefore marked ended as soon as the run completes,
        mirroring the cron completion path. Skipping this leaves
        ``ended_at`` NULL forever, and ``SessionDB.prune_sessions`` only
        reaps rows that have an ``ended_at`` value, so unclosed webhook
        sessions would accumulate without bound and bloat ``state.db``.

        This hook is the only seam that fires at the true end of a run:
        :meth:`BasePlatformAdapter._process_message_background` calls it
        after the handler returns, on success, failure, and cancellation
        alike, so errored runs are reaped too. :meth:`handle_message`
        cannot be used for this because it is fire-and-forget and returns
        before the run begins. :meth:`SessionDB.end_session` is
        first-reason-wins and no-ops on a row that is already ended, so
        this never overwrites a ``compression`` or ``agent_close`` reason.

        Args:
            event: The event that was processed. Only its ``source`` is
                used, to rebuild the session key.
            outcome: The run outcome. Unused; the session is closed
                regardless of whether the run succeeded.

        Returns:
            Nothing.
        """
        await self._end_webhook_session(event, event.source.chat_id)

    async def _end_webhook_session(
        self, event: "MessageEvent", session_chat_id: str
    ) -> None:
        """Mark a per-delivery webhook session as ended in ``state.db``.

        The persisted ``session_id`` is resolved through the gateway's
        session store using the same source the run was keyed on, so key
        construction matches exactly even under profile multiplexing. The
        close goes through the public :meth:`SessionDB.end_session` API
        rather than a hand-written UPDATE, so any bookkeeping that API
        performs stays consistent.

        Args:
            event: The processed event, used to rebuild the session key
                from its ``source``.
            session_chat_id: The ``webhook:{route}:{delivery_id}`` key,
                used for logging only.

        Returns:
            Nothing. Every unavailable dependency is a silent no-op: a
            missing runner, session store, or key builder simply means
            there is nothing to close, and logging it would be noise.

        Note:
            The private ``_entries`` fallback exists only for stores or test
            doubles that predate the ``peek_session_id`` accessor. Reading
            through ``peek_session_id`` keeps the lookup under the store's
            lock, which the fallback necessarily gives up.
        """
        runner = self.gateway_runner
        if runner is None:
            return
        session_db = getattr(runner, "_session_db", None)
        store = getattr(runner, "session_store", None)
        if session_db is None or store is None:
            return
        try:
            key_fn = getattr(runner, "_session_key_for_source", None)
            if key_fn is None:
                return
            session_key = key_fn(event.source)
            # Prefer the store's lock-held accessor over reaching into the
            # private _entries dict from outside the lock.
            peek = getattr(store, "peek_session_id", None)
            if callable(peek):
                session_id = peek(session_key)
            else:
                if hasattr(store, "_ensure_loaded"):
                    try:
                        store._ensure_loaded()
                    except Exception:
                        pass
                entries = getattr(store, "_entries", {}) or {}
                entry = entries.get(session_key)
                session_id = getattr(entry, "session_id", None) if entry else None
            if not session_id:
                logger.debug(
                    "[webhook] No session_id to close for %s (key=%s)",
                    session_chat_id,
                    session_key,
                )
                return
            # AsyncSessionDB forwards end_session onto a worker thread, while
            # a plain SessionDB returns synchronously. Await whichever.
            _end = session_db.end_session
            result = _end(session_id, "webhook_complete")
            if asyncio.iscoroutine(result):
                await result
            logger.debug(
                "[webhook] Closed session %s for delivery %s",
                session_id,
                session_chat_id,
            )
        except Exception as e:
            logger.debug(
                "[webhook] Failed to close session for %s: %s",
                session_chat_id,
                e,
            )

    # ---- Signature validation ----

    def _validate_signature(
        self, request: "web.Request", body: bytes, secret: str
    ) -> bool:
        """Verify the request signature against *body* and *secret*.

        Vendor schemes are detected by which header is present, in a fixed
        priority order: Svix first (its header names are unique), then
        GitHub, then GitLab, then the generic V2 HMAC, then the legacy V1
        HMAC. A request is accepted only if exactly one scheme matches;
        any ambiguity fails closed.

        Args:
            request: The aiohttp request, used only for header lookup and,
                for log messages, the route name from ``match_info``.
            body: The exact bytes that were read off the wire. Signatures
                are computed over these bytes, so any re-encoding or
                re-parsing before this point would break verification.
            secret: The route's HMAC secret.

        Returns:
            ``True`` when a recognised signature verifies, ``False``
            otherwise. ``False`` covers a wrong signature, a missing
            header, and a header set the adapter does not understand, and
            the caller maps that to 401.

        Note:
            Comparisons always go through :func:`_hmac_str_equal` so the
            check is constant-time and cannot raise on a hostile header.
        """
        def _header(name: str) -> str:
            """Fetch a header case-insensitively.

            Args:
                name: Canonical header name.

            Returns:
                The first non-empty value found among the canonical,
                lowercase, and uppercase spellings. Svix documents
                lowercase names, but a proxy or an older client may
                capitalise them, so all three are probed.
            """
            return (
                request.headers.get(name, "")
                or request.headers.get(name.lower(), "")
                or request.headers.get(name.upper(), "")
            )

        # Svix / AgentMail scheme:
        #   svix-id: msg_...
        #   svix-timestamp: unix seconds
        #   svix-signature: v1,<base64-hmac> [v1,<base64-hmac> ...]
        # Signed content is "{id}.{timestamp}.{raw_body}". Svix secrets
        # usually start with "whsec_" and the remainder is base64-encoded.
        svix_id = _header("svix-id")
        svix_timestamp = _header("svix-timestamp")
        svix_signature = _header("svix-signature")
        if svix_id or svix_timestamp or svix_signature:
            return self._validate_svix_signature(
                body=body,
                secret=secret,
                msg_id=svix_id,
                timestamp=svix_timestamp,
                signature_header=svix_signature,
            )

        # GitHub sends the digest with a "sha256=" prefix, which must be
        # reproduced exactly or the comparison fails.
        gh_sig = request.headers.get("X-Hub-Signature-256", "")
        if gh_sig:
            expected = "sha256=" + hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            return _hmac_str_equal(gh_sig, expected)

        # GitLab does not HMAC at all: the token header carries the plain
        # secret, so this is a constant-time string compare, not a digest.
        gl_token = request.headers.get("X-Gitlab-Token", "")
        if gl_token:
            return _hmac_str_equal(gl_token, secret)

        # Generic V2: X-Webhook-Signature-V2 = <hex HMAC-SHA256 of "<timestamp>.<body>">
        #             X-Webhook-Timestamp = <unix seconds> (required for V2)
        #
        # Checked before V1 and independently of it. A sender that only ever
        # sends V2 headers must validate here; nesting this inside the V1
        # branch would silently drop V2-only senders.
        #
        # Sending the V2 header commits the request to V2 and never falls
        # back to V1, even when the timestamp is absent, malformed, or
        # outside the window. Senders migrating to V2 often send both
        # headers during the transition, so a fall-through would let an
        # attacker capture one such mixed request, strip the timestamp, and
        # have the replay validate against the still-present, unprotected V1
        # signature -- downgrading a V2 request back to the replay hole V2
        # exists to close.
        v2_sig = request.headers.get("X-Webhook-Signature-V2", "")
        if v2_sig:
            v2_timestamp = request.headers.get("X-Webhook-Timestamp", "")
            if not v2_timestamp:
                logger.warning(
                    "[webhook] Route '%s' sent X-Webhook-Signature-V2 with "
                    "no X-Webhook-Timestamp — rejecting rather than "
                    "falling back to legacy V1",
                    request.match_info.get("route_name", ""),
                )
                return False
            try:
                ts = int(v2_timestamp)
            except (TypeError, ValueError):
                return False
            if abs(int(time.time()) - ts) > 300:
                logger.warning(
                    "[webhook] Route '%s' generic HMAC V2 timestamp outside replay window",
                    request.match_info.get("route_name", ""),
                )
                return False
            signed_content = v2_timestamp.encode() + b"." + body
            expected_v2 = hmac.new(
                secret.encode(), signed_content, hashlib.sha256
            ).hexdigest()
            return _hmac_str_equal(v2_sig, expected_v2)

        # Generic V1 (legacy): X-Webhook-Signature = <hex HMAC-SHA256 of body>.
        # It has no replay protection: the signature covers only the body,
        # so a captured (body, signature) pair replays indefinitely with
        # nothing binding it to a specific delivery. It is still accepted,
        # but only when the V2 header was never sent at all (see the guard
        # above), and every route that uses it is warned about once.
        generic_sig = request.headers.get("X-Webhook-Signature", "")
        if generic_sig:
            expected = hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            route_name = request.match_info.get("route_name", "")
            if route_name not in self._v1_signature_warned:
                self._v1_signature_warned.add(route_name)
                logger.warning(
                    "[webhook] Route '%s' uses legacy body-only HMAC (no "
                    "timestamp), which is vulnerable to replay attacks. Add "
                    "an 'X-Webhook-Timestamp' header and switch to "
                    "'X-Webhook-Signature-V2' (HMAC-SHA256 of "
                    "'<timestamp>.<body>').",
                    route_name,
                )
            return _hmac_str_equal(generic_sig, expected)

        # A secret is configured but no signature header was recognised.
        # Reject rather than accepting, so a provider that signed the
        # request in an unsupported scheme fails closed instead of
        # silently disabling authentication.
        logger.debug(
            "[webhook] Secret configured but no signature header found"
        )
        return False

    def _validate_svix_signature(
        self,
        body: bytes,
        secret: str,
        msg_id: str,
        timestamp: str,
        signature_header: str,
        tolerance_seconds: int = 300,
    ) -> bool:
        """Verify a Svix-compatible signature, as used by AgentMail.

        Svix signs ``"{id}.{timestamp}.{raw_body}"`` and sends the result
        base64-encoded with a version prefix, so the timestamp is part of
        the signed input and replays are rejected by the window check
        rather than by any state.

        Args:
            body: Raw request bytes, part of the signed input.
            secret: The signing secret. Either a ``whsec_``-prefixed
                base64 key or a raw shared secret.
            msg_id: The Svix message id from ``svix-id``.
            timestamp: Unix seconds from ``svix-timestamp``.
            signature_header: The ``svix-signature`` value, which may hold
                several ``vN,<base64>`` entries separated by spaces.
            tolerance_seconds: Half-width of the replay window in seconds.

        Returns:
            ``True`` when any ``v1`` entry matches, ``False`` otherwise.
            Every malformed input -- missing fields, a non-integer
            timestamp, an expired timestamp, or an undecodable secret --
            returns ``False`` rather than raising, so a hostile request
            cannot produce a 500.

        Note:
            Non-``whsec_`` secrets are accepted as raw bytes to support
            providers that reuse Svix's header scheme with a plain shared
            secret. Both forms are compared with
            :func:`_hmac_str_equal`, which keeps the check constant-time
            even when the header carries non-ASCII bytes.
        """
        if not (msg_id and timestamp and signature_header and secret):
            return False

        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(int(time.time()) - ts) > tolerance_seconds:
            logger.warning("[webhook] Svix signature timestamp outside replay window")
            return False

        if secret.startswith("whsec_"):
            encoded_secret = secret.removeprefix("whsec_")
            try:
                key = base64.b64decode(encoded_secret, validate=True)
            except (binascii.Error, ValueError):
                logger.debug("[webhook] Invalid whsec_ Svix signing secret")
                return False
        else:
            # Some providers document Svix-style headers but issue plain
            # shared secrets instead of base64 whsec_ keys.
            logger.debug("[webhook] Validating Svix-style signature with raw secret")
            key = secret.encode()

        signed_content = msg_id.encode() + b"." + timestamp.encode() + b"." + body
        expected = base64.b64encode(
            hmac.new(key, signed_content, hashlib.sha256).digest()
        ).decode()

        # Secret rotation means several signatures can be sent at once; any
        # matching v1 entry is enough to accept.
        for part in signature_header.split():
            try:
                version, signature = part.split(",", 1)
            except ValueError:
                continue
            if version == "v1" and _hmac_str_equal(signature, expected):
                return True
        return False

    # ---- Prompt rendering ----

    def _render_prompt(
        self,
        template: str,
        payload: dict,
        event_type: str,
        route_name: str,
    ) -> str:
        """Render a prompt template with the webhook payload.

        Placeholders are ``{name}`` tokens matched by
        ``\\{([a-zA-Z0-9_.]+)\\}``, so nested access uses dots:
        ``{pull_request.title}`` resolves to
        ``payload["pull_request"]["title"]``. Two placeholders are
        special: ``{__raw__}`` dumps the whole payload as indented JSON
        truncated to 4000 characters, and ``{event_type}`` substitutes the
        resolved event type.

        An empty template is treated as "let the agent see everything" and
        renders a fenced JSON block with a route/event header, so a route
        that omits ``prompt`` still produces a useful agent input instead
        of an empty string.

        Args:
            template: The route's prompt template, possibly empty.
            payload: Parsed webhook body.
            event_type: Resolved event type, for the ``{event_type}``
                token and the fallback header.
            route_name: Route name, for the fallback header.

        Returns:
            The rendered prompt string. Unresolvable placeholders are left
            in the output verbatim (``{foo.bar}``) rather than replaced
            with ``None`` or dropped, so a typo in a template is visible
            in the agent's input instead of silently losing information.

        Note:
            Both dumps are truncated (4000 for ``__raw__``, 2000 for an
            interpolated container) to keep a large payload from blowing
            up the context window of the turn it triggers.
        """
        if not template:
            truncated = json.dumps(payload, indent=2)[:4000]
            return (
                f"Webhook event '{event_type}' on route "
                f"'{route_name}':\n\n```json\n{truncated}\n```"
            )

        def _resolve(match: re.Match) -> str:
            """Resolve one ``{placeholder}`` match against the payload.

            Args:
                match: The regex match; group 1 is the placeholder name.

            Returns:
                The substituted value as a string, or the original
                ``{name}`` text when the path does not resolve.
            """
            key = match.group(1)
            if key == "__raw__":
                return json.dumps(payload, indent=2)[:4000]
            if key == "event_type":
                return event_type
            value: Any = payload
            for part in key.split("."):
                if isinstance(value, dict):
                    value = value.get(part, f"{{{key}}}")
                else:
                    return f"{{{key}}}"
            if isinstance(value, (dict, list)):
                return json.dumps(value, indent=2)[:2000]
            return str(value)

        return re.sub(r"\{([a-zA-Z0-9_.]+)\}", _resolve, template)

    def _render_delivery_extra(
        self, extra: dict, payload: dict
    ) -> dict:
        """Render template placeholders inside ``deliver_extra``.

        Any string value is rendered with :meth:`_render_prompt`, so a
        route can point at the right destination per event, for example
        ``deliver_extra.chat_id: "{user.chat_id}"``. Non-string values are
        copied through unchanged, which is how numeric ids and booleans
        survive.

        Args:
            extra: The route's ``deliver_extra`` mapping.
            payload: Parsed webhook body.

        Returns:
            A new dict with rendered string values; the input mapping is
            not mutated, so the same route config can be reused across
            events.

        Note:
            ``event_type`` and ``route_name`` are passed as empty strings
            here, so ``{event_type}`` resolves through the payload lookup
            rather than the special token. That is intentional: delivery
            targets come from the payload, not from the header-derived
            event name.
        """
        rendered: Dict[str, Any] = {}
        for key, value in extra.items():
            if isinstance(value, str):
                rendered[key] = self._render_prompt(value, payload, "", "")
            else:
                rendered[key] = value
        return rendered

    # ---- Response delivery ----

    async def _direct_deliver(
        self, content: str, delivery: dict
    ) -> SendResult:
        """Deliver *content* directly without invoking the agent.

        Used by ``deliver_only`` routes: the rendered template becomes the
        literal message body, and we dispatch to the same delivery helpers
        that the agent-mode ``send()`` flow uses.  All target types that
        work in agent mode work here — Telegram, Discord, Slack, GitHub
        PR comments, etc.
        """
        deliver_type = delivery.get("deliver", "log")

        if deliver_type == "log":
            # Shouldn't reach here — startup validation rejects deliver_only
            # with deliver=log — but guard defensively.
            logger.info("[webhook] direct-deliver log-only: %s", content[:200])
            return SendResult(success=True)

        if deliver_type == "github_comment":
            return await self._deliver_github_comment(content, delivery)

        # Fall through to the cross-platform dispatcher, which validates the
        # target name and routes via the gateway runner.
        return await self._deliver_cross_platform(
            deliver_type, content, delivery
        )

    async def _deliver_github_comment(
        self, content: str, delivery: dict
    ) -> SendResult:
        """Post a response as a GitHub PR comment through the ``gh`` CLI.

        Args:
            content: The comment body.
            delivery: The delivery descriptor built from the route, whose
                ``deliver_extra`` carries ``repo`` and ``pr_number``.

        Returns:
            A :class:`SendResult`. Failures are returned rather than
            raised so the caller can report them instead of crashing the
            turn.

        Note:
            Both ``repo`` and ``pr_number`` are validated before being
            passed to the CLI. Either could otherwise inject a second
            argument or an option, since the values come from route config
            and in some cases from the payload.
        """
        extra = delivery.get("deliver_extra", {})
        repo = extra.get("repo", "")
        pr_number = extra.get("pr_number", "")

        if not repo or not pr_number:
            logger.error(
                "[webhook] github_comment delivery missing repo or pr_number"
            )
            return SendResult(
                success=False, error="Missing repo or pr_number"
            )

        # Input validation, to prevent CLI argument injection.
        # pr_number must be a positive integer.
        try:
            pr_int = int(pr_number)
            if pr_int <= 0:
                raise ValueError("non-positive")
        except (ValueError, TypeError):
            logger.error(
                "[webhook] invalid pr_number: %r", pr_number
            )
            return SendResult(
                success=False, error="Invalid pr_number"
            )

        # repo must be a bare "owner/name" slug; anything with a slash in
        # the wrong place or shell metacharacters is rejected.
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
            logger.error("[webhook] invalid repo format: %r", repo)
            return SendResult(
                success=False, error="Invalid repo format"
            )

        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "comment",
                    str(pr_int),
                    "--repo",
                    repo,
                    "--body",
                    content,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                logger.info(
                    "[webhook] Posted comment on %s#%s", repo, pr_number
                )
                return SendResult(success=True)
            else:
                logger.error(
                    "[webhook] gh pr comment failed: %s", result.stderr
                )
                return SendResult(success=False, error=result.stderr)
        except FileNotFoundError:
            logger.error(
                "[webhook] 'gh' CLI not found — install GitHub CLI for "
                "github_comment delivery"
            )
            return SendResult(
                success=False, error="gh CLI not installed"
            )
        except Exception as e:
            logger.error("[webhook] github_comment delivery error: %s", e)
            return SendResult(success=False, error=str(e))

    async def _deliver_cross_platform(
        self, platform_name: str, content: str, delivery: dict
    ) -> SendResult:
        """Route a response to another connected platform adapter.

        This is how ``deliver: telegram`` works: the webhook adapter
        hands the text to whatever adapter the runner has running for that
        platform, so one inbound endpoint can notify a chat on a
        completely different vendor.

        Args:
            platform_name: Target platform name, as a
                :class:`encre.gateway.config.Platform` member name.
            content: Text to send.
            delivery: The delivery descriptor; ``deliver_extra`` supplies
                the destination ``chat_id`` and optional thread.

        Returns:
            A :class:`SendResult` from the target adapter.

        Note:
            Every failure mode returns a ``SendResult`` rather than
            raising, so a misconfigured target degrades to a logged
            failure inside the turn instead of tearing it down.
        """
        if not self.gateway_runner:
            return SendResult(
                success=False,
                error="No gateway runner for cross-platform delivery",
            )

        try:
            target_platform = Platform(platform_name)
        except ValueError:
            return SendResult(
                success=False, error=f"Unknown platform: {platform_name}"
            )

        # Look in the default adapter set first. Under profile multiplexing
        # an adapter may only exist on a secondary profile, so fall back to
        # those maps too: otherwise a "deliver: slack" route would break
        # simply because slack is disabled on the default profile.
        adapter = self.gateway_runner.adapters.get(target_platform)
        if not adapter:
            for _prof, amap in (getattr(self.gateway_runner, "_profile_adapters", None) or {}).items():
                if not isinstance(amap, dict):
                    continue
                cand = amap.get(target_platform)
                if cand is not None:
                    adapter = cand
                    break
        if not adapter:
            return SendResult(
                success=False,
                error=f"Platform {platform_name} not connected",
            )

        # Fall back to the platform's home channel when the route did not
        # name a destination, so a bare "deliver: telegram" is enough.
        extra = delivery.get("deliver_extra", {})
        chat_id = extra.get("chat_id", "")
        if not chat_id:
            home = self.gateway_runner.config.get_home_channel(target_platform)
            if home:
                chat_id = home.chat_id
            else:
                return SendResult(
                    success=False,
                    error=f"No chat_id or home channel for {platform_name}",
                )

        # Forward the thread id so Telegram forum topics and the like keep
        # their own thread instead of posting to the parent channel.
        metadata = None
        thread_id = extra.get("message_thread_id") or extra.get("thread_id")
        if thread_id:
            metadata = {"thread_id": thread_id}

        return await adapter.send(chat_id, content, metadata=metadata)
