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
Telegram-specific network helpers: proxy resolution and DNS fallback transport.

This module addresses connectivity problems that arise in restricted-network
environments where ``api.telegram.org`` DNS resolution returns an unreachable
IP, is poisoned, or is entirely blocked. It provides two mechanisms:

1. **Proxy resolution** -- delegates to the shared
   :func:`encre.gateway.platforms.base.resolve_proxy_url` to honor the
   ``TELEGRAM_PROXY`` environment variable and macOS system proxy settings.

2. **Fallback IP transport** -- :class:`TelegramFallbackTransport` wraps
   ``httpx.AsyncHTTPTransport`` with a hostname-preserving IP-rewrite retry
   loop. Requests continue to target ``https://api.telegram.org/...``
   logically (TLS SNI and the ``Host`` header remain unchanged), but on
   TCP connect failures the underlying connection is retried against one or
   more fallback IPv4 addresses. This is the programmatic equivalent of
   ``curl --resolve api.telegram.org:443:<ip>``.

Fallback IP discovery uses DNS-over-HTTPS (Google and Cloudflare) to resolve
A records for ``api.telegram.org``, with a hardcoded seed list as a last
resort when DoH is also blocked.

Public API:
    - :class:`TelegramFallbackTransport` -- an ``httpx.AsyncBaseTransport``
      subclass that retries against fallback IPs while preserving TLS/SNI.
    - :func:`parse_fallback_ip_env` -- parses a comma-separated IP list from
      an environment variable, validating and filtering each entry.
    - :func:`discover_fallback_ips` -- auto-discovers Telegram API IPs via
      DNS-over-HTTPS, falling back to a seed list on failure.
    - :func:`_resolve_proxy_url` -- thin wrapper around the shared proxy
      resolver, scoped to the Telegram API host and fallback IPs.

Internal helpers (prefixed with ``_``):
    - :func:`_normalize_fallback_ips` -- validates and filters a raw IP list.
    - :func:`_resolve_system_dns` -- resolves IPs via the OS resolver.
    - :func:`_query_doh_provider` -- queries one DoH provider for A records.
    - :func:`_rewrite_request_for_ip` -- clones an ``httpx.Request`` with a
      rewritten host IP while preserving the logical hostname and SNI.
    - :func:`_is_retryable_connect_error` -- classifies exceptions as
      transient connect failures worth retrying.

This module is imported by :class:`ea_connector_telegram.adapter`
and is not intended for use outside the Telegram adapter package.
"""

import asyncio
import ipaddress
import logging
import socket
from typing import Iterable, Optional

import httpx

logger = logging.getLogger(__name__)

# The canonical Telegram Bot API hostname. All transport-layer logic in this
# module is scoped to this host; requests targeting other hosts are passed
# through unchanged.
_TELEGRAM_API_HOST = "api.telegram.org"

# DNS-over-HTTPS query timeout in seconds. Bounded so that a wedged DoH
# endpoint cannot noticeably delay the adapter's connect path. The adapter
# wraps the entire discovery in this bound and falls back to a seed list on
# timeout.
_DOH_TIMEOUT = 4.0

# DNS-over-HTTPS providers used to discover Telegram API IPv4 addresses.
# Google DNS uses the standard Google DoH JSON API; Cloudflare requires the
# ``Accept: application/dns-json`` header to select its DoH protocol.
# Both providers return A records in the ``Answer`` array with ``type == 1``.
_DOH_PROVIDERS: list[dict] = [
    {
        "url": "https://dns.google/resolve",
        "params": {"name": _TELEGRAM_API_HOST, "type": "A"},
        "headers": {},
    },
    {
        "url": "https://cloudflare-dns.com/dns-query",
        "params": {"name": _TELEGRAM_API_HOST, "type": "A"},
        "headers": {"Accept": "application/dns-json"},
    },
]

# Last-resort hardcoded IP addresses when DNS-over-HTTPS is also blocked.
# These are stable Telegram Bot API endpoints in the 149.154.160.0/20 block
# (the same seed used by prior implementations and by curl/resolve workflows).
# In practice these IPs change infrequently because Telegram's CDN is
# long-lived; they are meant as a safety net, not the primary path.
_SEED_FALLBACK_IPS: list[str] = ["149.154.166.110", "149.154.167.220"]


def _resolve_proxy_url(target_hosts=None) -> str | None:
    """Resolve a proxy URL for Telegram API requests.

    Delegates to the shared :func:`encre.gateway.platforms.base.resolve_proxy_url`
    implementation, which honors the ``TELEGRAM_PROXY`` environment variable
    and macOS system proxy detection. The ``target_hosts`` parameter is used
    by the shared resolver to determine whether the proxy applies to the
    requested destination.

    Args:
        target_hosts: An optional iterable of hostnames/IPs to check against
            the proxy rules. When ``None``, the resolver applies its default
            behavior (typically matching any host).

    Returns:
        The proxy URL string if a proxy is configured for the target hosts,
        or ``None`` if no proxy applies.
    """
    from encre.gateway.platforms.base import resolve_proxy_url
    return resolve_proxy_url("TELEGRAM_PROXY", target_hosts=target_hosts)


class TelegramFallbackTransport(httpx.AsyncBaseTransport):
    """Retry Telegram Bot API requests via fallback IPs while preserving TLS/SNI.

    This transport wraps ``httpx.AsyncHTTPTransport`` with a hostname-preserving
    IP-rewrite retry loop. Requests continue to target
    ``https://api.telegram.org/...`` logically -- the TLS SNI and ``Host``
    header remain ``api.telegram.org`` -- but on TCP connect failures the
    underlying connection is retried against one or more fallback IPv4
    addresses.

    The equivalent shell command is ``curl --resolve api.telegram.org:443:<ip>``.
    This is useful in restricted-network environments where DNS returns an
    unreachable IP or is entirely blocked.

    The transport maintains a "sticky" fallback IP: once a fallback IP
    succeeds, subsequent requests try that IP first (before the primary DNS
    path), reducing connect latency. If the sticky IP later fails, it is
    reset and the transport falls back to the normal attempt order.

    Thread-safety: the ``_sticky_lock`` guards the sticky IP field against
    concurrent reads/writes from multiple async tasks. The lock is an
    ``asyncio.Lock`` and must be acquired only from within a running event
    loop.

    Lifecycle: construct with ``__init__``, use via ``handle_async_request``,
    and release via ``aclose``. The primary and fallback transports are all
    closed by ``aclose``; failure to call it may leak socket connections.

    Attributes:
        _fallback_ips: Validated, deduplicated list of fallback IPv4
            addresses to try on connect failure.
        _primary: The default ``httpx.AsyncHTTPTransport`` used for the
            primary DNS path (no IP rewrite).
        _fallbacks: Mapping from fallback IP to its dedicated
            ``httpx.AsyncHTTPTransport`` instance. Each fallback IP has its
            own transport so that per-IP connection pools are independent.
        _sticky_ip: The fallback IP that most recently succeeded, or
            ``None`` if no fallback has succeeded yet. Protected by
            ``_sticky_lock``.
        _sticky_lock: An ``asyncio.Lock`` guarding ``_sticky_ip`` against
            concurrent access from multiple async tasks.
    """

    def __init__(self, fallback_ips: Iterable[str], **transport_kwargs):
        """Initialize the fallback transport with validated IPs and proxy settings.

        Args:
            fallback_ips: An iterable of raw IP address strings. Each entry
                is validated via :func:`_normalize_fallback_ips` (must be a
                valid public IPv4 address). Invalid entries are logged and
                skipped. Duplicates are removed preserving first-occurrence
                order.
            **transport_kwargs: Additional keyword arguments forwarded to
                each ``httpx.AsyncHTTPTransport`` instance. May include
                ``proxy``, ``limits``, ``cert_verify``, etc. If ``proxy`` is
                not provided but a proxy URL is resolved by
                :func:`_resolve_proxy_url`, it is injected here.
        """
        self._fallback_ips = list(dict.fromkeys(_normalize_fallback_ips(fallback_ips)))
        proxy_url = _resolve_proxy_url(target_hosts=[_TELEGRAM_API_HOST, *self._fallback_ips])
        if proxy_url and "proxy" not in transport_kwargs:
            transport_kwargs["proxy"] = proxy_url
        self._primary = httpx.AsyncHTTPTransport(**transport_kwargs)
        self._fallbacks = {
            ip: httpx.AsyncHTTPTransport(**transport_kwargs) for ip in self._fallback_ips
        }
        self._sticky_ip: Optional[str] = None
        self._sticky_lock = asyncio.Lock()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Handle a single HTTP request with fallback IP retry.

        For requests targeting ``_TELEGRAM_API_HOST``, this method attempts
        the connection in the following order:

        1. The sticky fallback IP (if set and different from the primary).
        2. The primary DNS path (``ip is None``).
        3. Each remaining fallback IP in list order.

        If the sticky IP is set and a retry is needed, the primary path is
        tried after the sticky IP so that a DNS resolution fix (e.g. VPN
        switch) can recover without waiting for the next transport cycle.

        On success with a fallback IP, the sticky IP is updated (under
        ``_sticky_lock``) so that subsequent requests try it first. On
        failure of the sticky IP, the sticky state is cleared and the loop
        continues to the next candidate.

        Non-retryable errors (e.g. HTTP 4xx/5xx application errors, TLS
        handshake failures) are re-raised immediately. Only connect-timeout
        and connect-error exceptions are retried.

        Args:
            request: The ``httpx.Request`` to send. The request's URL host
                is checked against ``_TELEGRAM_API_HOST``; non-matching
                requests are passed through to the primary transport without
                any fallback retry.

        Returns:
            The ``httpx.Response`` from the first successful attempt.

        Raises:
            Exception: The last encountered exception if all attempts fail,
                or any non-retryable exception immediately.
        """
        if request.url.host != _TELEGRAM_API_HOST or not self._fallback_ips:
            return await self._primary.handle_async_request(request)

        sticky_ip = self._sticky_ip
        attempt_order: list[Optional[str]] = [sticky_ip] if sticky_ip else [None]
        if sticky_ip:
            # If the sticky IP is set, retry the primary DNS path after a
            # sticky failure so a DNS fix (VPN switch, resolver change) can
            # recover without waiting for the next transport cycle.
            attempt_order.append(None)
        for ip in self._fallback_ips:
            if ip != sticky_ip:
                attempt_order.append(ip)

        last_error: Exception | None = None
        for ip in attempt_order:
            candidate = request if ip is None else _rewrite_request_for_ip(request, ip)
            transport = self._primary if ip is None else self._fallbacks[ip]
            try:
                response = await transport.handle_async_request(candidate)
                if ip is not None and self._sticky_ip != ip:
                    async with self._sticky_lock:
                        if self._sticky_ip != ip:
                            self._sticky_ip = ip
                            logger.warning(
                                "[Telegram] Primary api.telegram.org path unreachable; using sticky fallback IP %s",
                                ip,
                            )
                return response
            except Exception as exc:
                last_error = exc
                if not _is_retryable_connect_error(exc):
                    raise
                if ip is not None and ip == self._sticky_ip:
                    async with self._sticky_lock:
                        if self._sticky_ip == ip:
                            self._sticky_ip = None
                            logger.warning(
                                "[Telegram] Sticky fallback IP %s failed; resetting to primary DNS path",
                                ip,
                            )
                if ip is None:
                    logger.warning(
                        "[Telegram] Primary api.telegram.org connection failed (%s); trying fallback IPs %s",
                        exc,
                        ", ".join(self._fallback_ips),
                    )
                    continue
                logger.warning("[Telegram] Fallback IP %s failed: %s", ip, exc)
                continue

        if last_error is None:
            raise RuntimeError("All Telegram fallback IPs exhausted but no error was recorded")
        raise last_error

    async def aclose(self) -> None:
        """Close all underlying transports and release socket connections.

        Closes the primary transport first, then all fallback transports.
        Each transport's ``aclose`` is awaited sequentially; failures in one
        do not prevent the others from being closed. This method is called
        during adapter shutdown (``_shutdown_abandoned_app`` or explicit
        ``disconnect``) and must be awaited to avoid leaking file descriptors.

        Returns:
            None.
        """
        await self._primary.aclose()
        for transport in self._fallbacks.values():
            await transport.aclose()


def _normalize_fallback_ips(values: Iterable[str]) -> list[str]:
    """Validate and filter a raw list of IP address strings.

    Each value is converted to a string, stripped, and parsed via
    ``ipaddress.ip_address``. The following are rejected with a debug
    warning:

    - Empty or whitespace-only strings.
    - Strings that are not valid IP addresses.
    - IPv6 addresses (the Bot API is IPv4-only; IPv6 fallback is not
      supported because many restricted networks block IPv6 entirely).
    - Private, loopback, link-local, and unspecified addresses (these are
      not routable on the public internet and would cause the transport to
      hang or fail silently).

    Valid addresses are returned as their canonical string form.

    Args:
        values: An iterable of raw IP address strings (may include invalid
            entries, duplicates, or empty strings).

    Returns:
        A list of validated, deduplicated IPv4 address strings in
        first-occurrence order. May be empty if no valid entries were
        provided.
    """
    normalized: list[str] = []
    for value in values:
        raw = str(value).strip()
        if not raw:
            continue
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            logger.warning("Ignoring invalid Telegram fallback IP: %r", raw)
            continue
        if addr.version != 4:
            logger.warning("Ignoring non-IPv4 Telegram fallback IP: %s", raw)
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            logger.warning("Ignoring private/internal Telegram fallback IP: %s", raw)
            continue
        normalized.append(str(addr))
    return normalized


def parse_fallback_ip_env(value: str | None) -> list[str]:
    """Parse a comma-separated IP list from an environment variable.

    Splits the input string on commas, strips whitespace from each part,
    and validates via :func:`_normalize_fallback_ips`. Empty parts (from
    trailing commas or consecutive delimiters) are silently dropped.

    Args:
        value: A comma-separated string of IP addresses, or ``None``.

    Returns:
        A list of validated IPv4 address strings, or an empty list if
        ``value`` is ``None`` or no valid entries were found.
    """
    if not value:
        return []
    parts = [part.strip() for part in value.split(",")]
    return _normalize_fallback_ips(parts)


def _resolve_system_dns() -> set[str]:
    """Return the IPv4 addresses that the OS resolver gives for api.telegram.org.

    Uses ``socket.getaddrinfo`` with ``AF_INET`` to restrict the result to
    IPv4 addresses. This runs in a worker thread (called via
    ``asyncio.to_thread``) because ``getaddrinfo`` is blocking and may hang
    for minutes if the OS resolver is wedged (broken VPN, misconfigured DNS).

    The result is used only for a debug log line in :func:`discover_fallback_ips`
    when DoH yields no usable answers; it never gates the discovery path.

    Returns:
        A set of IPv4 address strings, or an empty set on any exception
        (DNS failure, timeout, or blocked resolver).
    """
    try:
        results = socket.getaddrinfo(_TELEGRAM_API_HOST, 443, socket.AF_INET)
        return {addr[4][0] for addr in results}
    except Exception:
        return set()


async def _query_doh_provider(
    client: httpx.AsyncClient, provider: dict
) -> list[str]:
    """Query one DNS-over-HTTPS provider and return A-record IPs.

    Sends a GET request to the provider's DoH endpoint with the configured
    parameters and headers, parses the JSON response, and extracts all
    ``type == 1`` (A record) entries from the ``Answer`` array. Each IP is
    validated via ``ipaddress.ip_address`` before being returned; invalid
    entries are silently skipped.

    Errors (network failure, HTTP error, JSON parse failure) are caught and
    logged at debug level; the function returns an empty list rather than
    raising, so that a single failing provider does not abort the entire
    discovery process.

    Args:
        client: The ``httpx.AsyncClient`` to use for the request. Must have
            a timeout configured (the caller sets this to ``_DOH_TIMEOUT``).
        provider: A dict with keys ``url``, ``params``, and ``headers``
            describing the DoH endpoint to query.

    Returns:
        A list of validated IPv4 address strings from the A records, or an
        empty list on any error.
    """
    try:
        resp = await client.get(
            provider["url"], params=provider["params"], headers=provider["headers"]
        )
        resp.raise_for_status()
        data = resp.json()
        ips: list[str] = []
        for answer in data.get("Answer", []):
            if answer.get("type") != 1:  # A record
                continue
            raw = answer.get("data", "").strip()
            try:
                ipaddress.ip_address(raw)
                ips.append(raw)
            except ValueError:
                continue
        return ips
    except Exception as exc:
        logger.debug("DoH query to %s failed: %s", provider["url"], exc)
        return []


async def discover_fallback_ips() -> list[str]:
    """Auto-discover Telegram API IPv4 addresses via DNS-over-HTTPS.

    Resolves ``api.telegram.org`` through Google and Cloudflare DoH in
    parallel and returns all unique A records. IPs that match the local
    system resolver are kept rather than excluded: in many restricted
    networks the system-DNS IP is the most reliable path to the API and a
    transient primary-path failure should be retried against the same
    address via the IP-rewrite path before the seed list is consulted.

    The system-DNS leg runs ``socket.getaddrinfo`` in a worker thread with
    a timeout bound equal to ``_DOH_TIMEOUT``. If the OS resolver is wedged
    (broken VPN, misconfigured DNS), the system leg is abandoned and the
    discovery proceeds with DoH results alone. The system result only feeds
    a debug log line, so it must never gate the discovery path.

    If DoH yields no usable A records (all providers failed, all responses
    empty, or all IPs failed validation), the function falls back to the
    hardcoded seed list :data:`_SEED_FALLBACK_IPS`.

    Args:
        None.

    Returns:
        A list of validated IPv4 address strings. Never empty: the seed
        list is returned as a last resort.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(_DOH_TIMEOUT)) as client:
        doh_tasks = [_query_doh_provider(client, p) for p in _DOH_PROVIDERS]
        system_dns_task = asyncio.ensure_future(asyncio.to_thread(_resolve_system_dns))
        results = await asyncio.gather(*doh_tasks, return_exceptions=True)

    # The system-resolver leg runs socket.getaddrinfo in a worker thread with
    # no timeout of its own. A wedged OS resolver (broken VPN/DNS) can sit
    # for minutes. Its result only feeds the no-usable-answers log line
    # below, so it must never gate discovery: bound it and move on. The DoH
    # legs are already bounded by the client timeout above.
    system_ips: set[str] = set()
    try:
        system_result = await asyncio.wait_for(system_dns_task, timeout=_DOH_TIMEOUT)
        if isinstance(system_result, set):
            system_ips = system_result
    except Exception:
        logger.debug("System-DNS resolution for %s did not complete in time", _TELEGRAM_API_HOST)

    doh_ips: list[str] = []
    for r in results:
        if isinstance(r, list):
            doh_ips.extend(r)

    # Deduplicate preserving order
    seen: set[str] = set()
    candidates: list[str] = []
    for ip in doh_ips:
        if ip not in seen:
            seen.add(ip)
            candidates.append(ip)

    # Validate through existing normalization
    validated = _normalize_fallback_ips(candidates)

    if validated:
        logger.debug("Discovered Telegram fallback IPs via DoH: %s", ", ".join(validated))
        return validated

    logger.info(
        "DoH discovery yielded no usable IPs (system DNS: %s); using seed fallback IPs %s",
        ", ".join(system_ips) or "unknown",
        ", ".join(_SEED_FALLBACK_IPS),
    )
    return list(_SEED_FALLBACK_IPS)


def _rewrite_request_for_ip(request: httpx.Request, ip: str) -> httpx.Request:
    """Clone an httpx.Request with a rewritten host IP, preserving SNI/Host.

    Creates a new ``httpx.Request`` that targets ``ip`` on the same port
    and path as the original, but preserves:

    - The original ``Host`` header (``api.telegram.org``) so the Bot API
      routes the request to the correct virtual host.
    - The ``sni_hostname`` extension set to the original hostname, so the
      TLS handshake sends the correct SNI for certificate validation.
    - The original request stream (body) unchanged.

    This is the transport-layer equivalent of
    ``curl --resolve api.telegram.org:443:<ip>``.

    Args:
        request: The original ``httpx.Request`` to clone.
        ip: The IPv4 address string to rewrite the host to. Must be a
            valid public IP (validated by the caller).

    Returns:
        A new ``httpx.Request`` targeting ``ip`` with preserved hostname,
        SNI, and body.
    """
    original_host = request.url.host or _TELEGRAM_API_HOST
    url = request.url.copy_with(host=ip)
    headers = request.headers.copy()
    headers["host"] = original_host
    extensions = dict(request.extensions)
    extensions["sni_hostname"] = original_host
    return httpx.Request(
        method=request.method,
        url=url,
        headers=headers,
        stream=request.stream,
        extensions=extensions,
    )


def _is_retryable_connect_error(exc: Exception) -> bool:
    """Classify an exception as a transient connect failure worth retrying.

    Only ``httpx.ConnectTimeout`` and ``httpx.ConnectError`` are considered
    retryable. These indicate that the TCP connection was never established
    (timeout or refused), so retrying against a different IP is safe.

    Application-level errors (HTTP 4xx/5xx), TLS handshake failures, and
    read/write timeouts are NOT retryable: the connection was established
    and the error is specific to that request, so retrying against a
    different IP is unlikely to help.

    Args:
        exc: The exception to classify.

    Returns:
        ``True`` if the exception is a connect-timeout or connect-error
        (safe to retry against a fallback IP), ``False`` otherwise.
    """
    return isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError))
