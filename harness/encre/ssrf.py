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

"""Server-Side Request Forgery (SSRF) guard for Encre Agent.

Protects against SSRF attacks by blocking requests to private, loopback,
link-local, and other non-routable IP address ranges. This is critical
for any feature that fetches URLs from user input (web search, image
download, document parsing) where an attacker could craft a URL pointing
to internal services.

The guard performs DNS resolution to catch hostname-based attacks (e.g.,
``http://127.0.0.1`` disguised as a friendly domain), and checks both
IPv4 and IPv6 address ranges. A short TTL DNS cache prevents repeated
lookups for the same hostname within a short window.

Usage::

    guard = EncreSSRFGuard()
    if guard.validate_url(user_supplied_url):
        fetch(user_supplied_url)
    else:
        raise ValueError("Blocked URL")
"""

from __future__ import annotations

import ipaddress
import socket
from typing import ClassVar
from urllib.parse import urlparse



from encre.logging_config import get_logger
logger = get_logger(__name__)


class EncreSSRFGuard:
    """Validates URLs against SSRF by blocking private and non-routable IPs.

    The guard maintains two levels of protection:

    1. **IP range blocking** — Blocks all addresses in RFC 1918, link-local,
       loopback, and other non-public ranges. Both IPv4 and IPv6 are checked.
    2. **Hostname suffix blocking** — Blocks hostnames ending in common
       internal suffixes (``.internal``, ``.local``, ``.localhost``, ``.lan``,
       ``.intranet``) unless explicitly whitelisted.

    Whitelisting is possible via class-level ``ALLOWED_HOSTNAMES`` and
    ``ALLOWED_SUFFIXES`` overrides, or instance-level ``_whitelist_v4`` /
    ``_whitelist_v6`` network lists. These are checked before blocked
    ranges, so an explicitly allowed address bypasses the block.

    A DNS cache with a 300-second TTL avoids repeated resolution lookups
    for frequently accessed hostnames. Call :meth:`clear_dns_cache` to
    force a fresh lookup when the network configuration changes.

    Attributes:
        BLOCKED_V4: IPv4 CIDR blocks that are always blocked.
        BLOCKED_V6: IPv6 CIDR blocks that are always blocked.
        BLOCKED_SUFFIXES: Hostname suffixes that indicate internal services.
        ALLOWED_HOSTNAMES: Exact hostnames (lowercase) to whitelist.
        ALLOWED_SUFFIXES: Hostname suffixes to whitelist.
    """

    BLOCKED_V4: ClassVar[list[str]] = [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "0.0.0.0/8",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "198.18.0.0/15",
        "127.0.0.0/8",
    ]
    BLOCKED_V6: ClassVar[list[str]] = [
        "fc00::/7",
        "fe80::/10",
        "::1",
        "::ffff:0:0/96",
        "2001:db8::/32",
        "ff00::/8",
    ]
    WHITELIST_V4: ClassVar[list[str]] = []
    WHITELIST_V6: ClassVar[list[str]] = []
    #: Hostname suffixes that must never be fetched (internal/link-local names)
    #: unless explicitly allowed via ``ALLOWED_HOSTNAMES``/``ALLOWED_SUFFIXES``.
    BLOCKED_SUFFIXES: ClassVar[tuple[str, ...]] = (
        ".internal", ".local", ".localhost", ".lan", ".intranet",
    )
    #: Explicitly allowed hostnames (exact match, lowercase) and suffixes.
    ALLOWED_HOSTNAMES: ClassVar[frozenset[str]] = frozenset()
    ALLOWED_SUFFIXES: ClassVar[tuple[str, ...]] = ()

    def __init__(self) -> None:
        """Initialize the guard with parsed CIDR blocks and empty DNS cache.

        Parses the class-level CIDR string lists into ``ipaddress`` network
        objects for efficient membership testing. The DNS cache starts empty
        and fills lazily as hostnames are resolved.
        """
        self._blocked_v4: list[ipaddress.IPv4Network] = [ipaddress.IPv4Network(n) for n in self.BLOCKED_V4]
        self._blocked_v6: list[ipaddress.IPv6Network] = [ipaddress.IPv6Network(n) for n in self.BLOCKED_V6]
        self._whitelist_v4: list[ipaddress.IPv4Network] = [ipaddress.IPv4Network(n) for n in self.WHITELIST_V4]
        self._whitelist_v6: list[ipaddress.IPv6Network] = [ipaddress.IPv6Network(n) for n in self.WHITELIST_V6]
        self._dns_cache: dict[str, tuple[float, list[ipaddress.IPv4Address | ipaddress.IPv6Address]]] = {}
        self._dns_cache_ttl: float = 300.0

    def _resolve_hostname(self, hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        """Resolve a hostname to IP addresses, using a TTL-based cache.

        Attempts to parse the hostname as a literal IP address first (for
        direct IP inputs like ``127.0.0.1``). If that fails, falls back to
        DNS resolution via ``socket.getaddrinfo``. The result is cached for
        ``_dns_cache_ttl`` seconds to avoid repeated lookups.

        Args:
            hostname: The hostname or IP literal to resolve.

        Returns:
            A list of resolved IP addresses. May be empty if resolution
            fails or the hostname is a literal that could not be parsed.
        """
        import time
        now = time.time()
        if hostname in self._dns_cache:
            cached_at, addrs = self._dns_cache[hostname]
            if now - cached_at < self._dns_cache_ttl:
                return addrs
        addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        try:
            addr = ipaddress.ip_address(hostname)
            addrs = [addr]
        except ValueError:
            try:
                infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
                seen: set[str] = set()
                for _, _, _, _, sockaddr in infos:
                    ip_str = sockaddr[0]
                    if ip_str not in seen:
                        seen.add(ip_str)
                        addrs.append(ipaddress.ip_address(ip_str))
            except socket.gaierror:
                logger.debug("exception suppressed", exc_info=True)
        self._dns_cache[hostname] = (now, addrs)
        return addrs

    def is_blocked_hostname(self, hostname: str) -> bool:
        """Check if a hostname resolves to a blocked address or internal suffix.

        Performs a two-stage check:

        1. **Suffix check** — If the hostname ends with one of ``BLOCKED_SUFFIXES``
           (e.g., ``.internal``, ``.local``), it is blocked immediately unless
           explicitly whitelisted via ``ALLOWED_HOSTNAMES`` or ``ALLOWED_SUFFIXES``.
        2. **IP check** — Resolves the hostname and checks each address against
           the blocked IP ranges.

        Args:
            hostname: The hostname to check.

        Returns:
            ``True`` if the hostname should be blocked, ``False`` otherwise.
            Empty strings return ``False`` (nothing to block).
        """
        if not hostname:
            return False
        name = hostname.rstrip(".").lower()
        if name in self.ALLOWED_HOSTNAMES:
            return False
        if any(name.endswith(sfx) for sfx in self.ALLOWED_SUFFIXES):
            return False
        if any(name == sfx.lstrip(".") or name.endswith(sfx) for sfx in self.BLOCKED_SUFFIXES):
            return True
        addrs = self._resolve_hostname(hostname)
        return any(self.is_blocked_address(addr) for addr in addrs)

    def is_blocked_address(self, addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        """Check if an IP address falls within a blocked range.

        Handles IPv4-mapped IPv6 addresses (``::ffff:7f00:1``) by unwrapping
        the mapped IPv4 address and checking against IPv4 ranges. All other
        addresses are checked against their native family's blocked ranges.

        Args:
            addr: The IP address to check.

        Returns:
            ``True`` if the address is in a blocked range, ``False`` otherwise.
        """
        if isinstance(addr, ipaddress.IPv6Address):
            if addr.ipv4_mapped:
                mapped = ipaddress.IPv4Address(addr.ipv4_mapped)
                return self._is_blocked_v4(mapped)
            return self._is_blocked_v6(addr)
        return self._is_blocked_v4(addr)

    def _is_blocked_v4(self, addr: ipaddress.IPv4Address) -> bool:
        """Check an IPv4 address against whitelist and blocked ranges.

        Whitelist entries are checked first — if the address matches any
        whitelisted network, it is allowed regardless of blocked ranges.
        This enables operators to permit specific internal subnets when
        needed (e.g., for testing).

        Args:
            addr: The IPv4 address to check.

        Returns:
            ``True`` if the address is blocked, ``False`` if whitelisted or public.
        """
        for net in self._whitelist_v4:
            if addr in net:
                return False
        return any(addr in net for net in self._blocked_v4)

    def _is_blocked_v6(self, addr: ipaddress.IPv6Address) -> bool:
        """Check an IPv6 address against whitelist and blocked ranges.

        Whitelist entries are checked first — if the address matches any
        whitelisted network, it is allowed regardless of blocked ranges.

        Args:
            addr: The IPv6 address to check.

        Returns:
            ``True`` if the address is blocked, ``False`` if whitelisted or public.
        """
        for net in self._whitelist_v6:
            if addr in net:
                return False
        return any(addr in net for net in self._blocked_v6)

    def validate_url(self, url_str: str) -> bool:
        """Validate a URL for SSRF safety.

        Checks that the URL:
        - Parses successfully with a valid scheme (``http`` or ``https`` only)
        - Has a hostname component
        - Does not resolve to a blocked address

        Args:
            url_str: The URL string to validate.

        Returns:
            ``True`` if the URL is safe to fetch, ``False`` otherwise.
        """
        try:
            parsed = urlparse(url_str)
        except Exception:
            return False
        if not parsed.hostname:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        return not self.is_blocked_hostname(parsed.hostname)

    def extract_safe_hostname(self, url_str: str) -> str | None:
        """Extract the hostname from a URL if it passes SSRF validation.

        This is a convenience wrapper that combines URL parsing, hostname
        extraction, and SSRF blocking into a single call. Returns ``None``
        for invalid URLs or blocked hostnames.

        Args:
            url_str: The URL string to process.

        Returns:
            The hostname if the URL is valid and not blocked, ``None`` otherwise.
        """
        try:
            parsed = urlparse(url_str)
        except Exception:
            return None
        if not parsed.hostname:
            return None
        if self.is_blocked_hostname(parsed.hostname):
            return None
        return parsed.hostname

    def clear_dns_cache(self) -> None:
        """Clear the DNS resolution cache.

        Forces subsequent hostname checks to perform fresh DNS lookups.
        Useful when the local DNS configuration changes (e.g., switching
        VPN profiles) or when long-running processes need to avoid stale
        resolution results.
        """
        self._dns_cache.clear()
