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

"""Tests for EncreSSRFGuard URL validation, hostname blocking, and DNS caching.

The guard is the SSRF prevention layer that decides whether an outbound URL
may be fetched at all. These tests cover construction and default blocklists,
URL parsing and classification, hostname-to-blocklist lookup, safe hostname
extraction, DNS result memoization, and cache eviction.
"""


class TestEncreSSRFGuardCreation:
    """Engineered to validate EncreSSRFGuard instantiation and default state.

    This test class exercises guard construction across 3 scenarios to ensure
    the SSRF prevention layer initializes with sane defaults: an empty DNS cache,
    a 300-second TTL, pre-seeded private-reserved IPv4/IPv6 blocklists, and no
    user-supplied whitelist entries. The design follows the principle of fail-safe
    defaults so that any guard instance denies internal-network access immediately
    upon creation without requiring explicit configuration.
    """

    def test_verify_guard_creation(self):
        """Validate that EncreSSRFGuard instantiates with correct default fields.

        The test constructs a bare guard and asserts that _dns_cache is an empty
        dict, _dns_cache_ttl equals 300.0, confirming the TTL bootstrap logic.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard is not None
        assert guard._dns_cache == {}
        assert guard._dns_cache_ttl == 300.0

    def test_verify_default_blocked_v4_subnets(self):
        """Validate that the guard ships with non-empty IPv4 and IPv6 blocklists.

        The test asserts both _blocked_v4 and _blocked_v6 contain entries, because
        the SSRF guard must deny all RFC 1918, RFC 4193, RFC 3927, RFC 5735,
        RFC 6598, and other reserved ranges by default -- no empty blocklist is
        acceptable for a security primitive.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert len(guard._blocked_v4) > 0
        assert len(guard._blocked_v6) > 0

    def test_verify_default_whitelist_is_empty(self):
        """Validate that no whitelist entries exist on a fresh guard instance.

        The test asserts both _whitelist_v4 and _whitelist_v6 are empty because
        the whitelist is opt-in; a newly created guard must not trust any host.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert len(guard._whitelist_v4) == 0
        assert len(guard._whitelist_v6) == 0


class TestValidateUrl:
    """Engineered to validate the URL parsing and classification pipeline.

    This test class exercises validate_url across 11 scenarios -- public IPs,
    localhost, private ranges, IPv6 loopback, zero addresses, link-local,
    non-HTTP schemes, malformed input, and missing hostnames -- to ensure the
    SSRF guard correctly distinguishes safe outbound requests from probes that
    target internal infrastructure. The design gates on scheme (http/https only)
    and IP-classification before any network I/O occurs.
    """

    def test_verify_public_ip_urls_are_safe(self):
        """Validate that publicly routable IPs pass SSRF validation.

        The test submits Google's and Cloudflare's DNS resolvers and asserts
        True because these addresses are not in any RFC 1918/4193/3927/reserved
        range and use the allowed http/https scheme.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.validate_url("https://8.8.8.8/path?q=1") is True
        assert guard.validate_url("https://1.1.1.1") is True

    def test_verify_http_public_ip_is_allowed(self):
        """Validate that plain HTTP to a public IP is accepted.

        The test targets example.com's IP (93.184.216.34) via http:// and asserts
        True because the scheme is whitelisted and the address is publicly routable.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.validate_url("http://93.184.216.34") is True  # example.com IP

    def test_verify_localhost_is_blocked(self):
        """Validate that localhost and 127.0.0.1 URLs are rejected.

        The test asserts False for both http and https to 127.0.0.1 because
        localhost addresses are the primary SSRF vector -- an attacker can force
        the host to fetch internal services via 127.0.0.1.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.validate_url("http://127.0.0.1:8080/api") is False
        assert guard.validate_url("https://127.0.0.1") is False

    def test_verify_private_ipv4_ranges_are_blocked(self):
        """Validate that RFC 1918 private IPv4 ranges are rejected.

        The test sends 192.168.x.x, 10.x.x.x, and 172.16.x.x URLs and asserts
        False for each because these subnets are reserved for internal use and
        must never be reachable from an outbound request path.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.validate_url("http://192.168.1.1") is False
        assert guard.validate_url("https://10.0.0.1/admin") is False
        assert guard.validate_url("http://172.16.0.1") is False

    def test_verify_ipv6_loopback_is_blocked(self):
        """Validate that IPv6 ::1 (loopback) URLs are rejected.

        The test asserts False because [::1] is the IPv6 equivalent of 127.0.0.1
        and constitutes the same SSRF risk for dual-stack services.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.validate_url("http://[::1]:8080") is False

    def test_verify_zero_address_is_blocked(self):
        """Validate that 0.0.0.0 URLs are rejected.

        The test asserts False because 0.0.0.0 is a non-routable meta-address
        that should never be the target of an outbound HTTP request.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.validate_url("http://0.0.0.0") is False

    def test_verify_link_local_is_blocked(self):
        """Validate that 169.254.x.x link-local addresses are rejected.

        The test asserts False because RFC 3927 link-local addresses are scoped
        to the local subnet and are commonly exploited in SSRF metadata-endpoint
        attacks (e.g., AWS EC2 169.254.169.254).
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.validate_url("http://169.254.1.1") is False

    def test_verify_non_http_schemes_are_blocked(self):
        """Validate that ftp, file, and ssh schemes are rejected.

        The test submits URLs with ftp://, file://, and ssh:// schemes and
        asserts False for each because the SSRF guard only permits http and
        https -- any other scheme is an unacceptable protocol for outbound I/O.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.validate_url("ftp://8.8.8.8") is False
        assert guard.validate_url("file:///etc/passwd") is False
        assert guard.validate_url("ssh://8.8.8.8") is False

    def test_verify_invalid_url_returns_false(self):
        """Validate that malformed or empty URLs are rejected.

        The test passes raw strings and an empty string, asserting False for
        both because the URL parser cannot extract a valid host from invalid
        input, and the guard must never attempt resolution on malformed input.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.validate_url("not-a-url") is False
        assert guard.validate_url("") is False

    def test_verify_empty_hostname_returns_false(self):
        """Validate that a URL with no hostname component is rejected.

        The test passes 'http://' and asserts False because an authority-less
        URL has no target host, making it neither safe nor meaningful for
        outbound request validation.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.validate_url("http://") is False


class TestIsBlockedHostname:
    """Engineered to validate the hostname-to-blocklist lookup path.

    This test class exercises is_blocked_hostname across 4 scenarios -- empty
    strings, localhost aliases, private IPs, and public IPs -- to ensure the
    hostname classifier correctly maps hostnames to their IP-classification
    before performing any DNS lookup. The design short-circuits on empty input
    and known-safe public addresses.
    """

    def test_verify_empty_hostname_is_not_blocked(self):
        """Validate that an empty hostname string returns False.

        The test asserts False because there is no host to block when the input
        is empty; the caller should handle this before invoking the guard.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.is_blocked_hostname("") is False

    def test_verify_localhost_variants_are_blocked(self):
        """Validate that all localhost representations are flagged as blocked.

        The test asserts True for 127.0.0.1, 'localhost', and ::1 because all
        three resolve to the loopback interface and pose identical SSRF risk.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.is_blocked_hostname("127.0.0.1") is True
        assert guard.is_blocked_hostname("localhost") is True
        assert guard.is_blocked_hostname("::1") is True

    def test_verify_private_ips_are_blocked(self):
        """Validate that RFC 1918 addresses are all flagged as blocked.

        The test checks 192.168.1.100, 10.0.0.5, and 172.16.5.5 and asserts
        True for each because these are the three canonical private IPv4 ranges
        that must never be reachable from an external request path.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.is_blocked_hostname("192.168.1.100") is True
        assert guard.is_blocked_hostname("10.0.0.5") is True
        assert guard.is_blocked_hostname("172.16.5.5") is True

    def test_verify_public_ips_are_not_blocked(self):
        """Validate that routable public IPs pass the blocklist check.

        The test asserts False for 8.8.8.8 (Google DNS) and 1.1.1.1 (Cloudflare)
        because these addresses are globally routable and outside all reserved
        ranges, so the guard must not suppress legitimate outbound traffic.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.is_blocked_hostname("8.8.8.8") is False
        assert guard.is_blocked_hostname("1.1.1.1") is False


class TestExtractSafeHostname:
    """Engineered to validate the hostname extraction from parsed URLs.

    This test class exercises extract_safe_hostname across 4 scenarios -- safe
    URLs, blocked URLs, invalid URLs, and URLs with explicit ports -- to ensure
    the guard returns the canonical hostname and None when the URL would be
    blocked. The design parses the URL, classifies the host, and either
    returns the raw hostname or None to signal rejection upstream.
    """

    def test_verify_safe_url_returns_extracted_hostname(self):
        """Validate that a safe URL yields its IP hostname component.

        The test asserts the returned hostname equals '8.8.8.8' because the
        parser must strip the scheme and path and return only the authority host.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        hostname = guard.extract_safe_hostname("https://8.8.8.8/path")
        assert hostname == "8.8.8.8"

    def test_verify_blocked_url_returns_none(self):
        """Validate that a blocked URL yields None instead of a hostname.

        The test asserts None because the guard rejects 127.0.0.1 and callers
        must not receive a hostname to attempt resolution against -- returning
        None signals immediate rejection at the extraction stage.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        hostname = guard.extract_safe_hostname("http://127.0.0.1/secret")
        assert hostname is None

    def test_verify_invalid_url_returns_none(self):
        """Validate that unparseable URLs yield None for the hostname.

        The test passes a raw string and an empty string, asserting None for
        both because the URL parser cannot extract a host and the guard must
        treat unparseable input as a rejection signal.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.extract_safe_hostname("not_a_url") is None
        assert guard.extract_safe_hostname("") is None

    def test_verify_https_url_with_port_extracts_hostname_only(self):
        """Validate that port numbers are stripped during hostname extraction.

        The test asserts the returned hostname is '1.1.1.1' (without ':443')
        because the caller needs the bare host for blocklist lookup, not the
        full authority string including the port.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        hostname = guard.extract_safe_hostname("https://1.1.1.1:443/v1")
        assert hostname == "1.1.1.1"


class TestDNSResolution:
    """Engineered to validate the DNS caching layer inside the SSRF guard.

    This test class exercises DNS resolution behavior across 2 scenarios --
    hostname classification that triggers caching, and cache population
    verification -- to ensure the guard memoizes resolved addresses and avoids
    redundant lookups on repeated hostname checks. The design stores IPs in
    _dns_cache keyed by hostname so that subsequent blocked/allowed decisions
    reuse the cached result within the TTL window.
    """

    def test_verify_hostname_classification_populates_cache(self):
        """Validate that is_blocked_hostname caches resolved IP addresses.

        The test calls is_blocked_hostname on a public IP and asserts the cache
        now contains that IP as a key, confirming that every classification
        attempt is memoized to avoid repeated DNS resolution overhead.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert guard.is_blocked_hostname("8.8.8.8") is False
        assert "8.8.8.8" in guard._dns_cache

    def test_verify_dns_cache_initially_empty(self):
        """Validate that a fresh guard starts with an empty DNS cache.

        The test asserts len(guard._dns_cache) == 0 because the cache must be
        clean on construction; stale entries from a prior process or test run
        must not leak into a new guard instance.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        assert len(guard._dns_cache) == 0


class TestClearDNSCache:
    """Engineered to validate the DNS cache eviction mechanism.

    This test class exercises clear_dns_cache across 2 scenarios -- cache
    emptying after population and idempotent double-clear -- to ensure the
    guard can be forced to forget all cached resolutions without crashing.
    The design supports operational resets (e.g. after blocklist updates)
    so that stale cached decisions do not outlive their intended validity.
    """

    def test_verify_clear_dns_cache_removes_all_entries(self):
        """Validate that clear_dns_cache empties the cache completely.

        The test populates the cache via a hostname lookup, asserts at least
        one entry exists, calls clear_dns_cache, and then asserts the cache
        is empty -- confirming the eviction path removes every stored entry.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        guard.is_blocked_hostname("8.8.8.8")
        assert len(guard._dns_cache) >= 1
        guard.clear_dns_cache()
        assert len(guard._dns_cache) == 0

    def test_verify_clear_dns_cache_is_idempotent(self):
        """Validate that calling clear_dns_cache twice does not raise.

        The test invokes clear_dns_cache consecutively on an empty cache and
        asserts no exception is raised and the cache remains at length 0,
        confirming the operation is safely idempotent for defensive coding.
        """
        from encre.ssrf import EncreSSRFGuard
        guard = EncreSSRFGuard()
        guard.clear_dns_cache()
        guard.clear_dns_cache()
        assert len(guard._dns_cache) == 0
