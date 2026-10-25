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
Connection health monitoring for LLM API calls.

Provides utilities for detecting, classifying, and formatting connection-level
errors (SSL/TLS, DNS, TCP, stale keep-alive sockets) and a lightweight health
monitor that tracks per-endpoint connection quality.

Typical usage::

    from encre.backends.connection import (
        ConnectionHealthMonitor,
        format_connection_error,
    )

    monitor = ConnectionHealthMonitor()

    try:
        response = await client.post(url, json=payload)
    except httpx.ConnectError as exc:
        msg = format_connection_error(exc)
        logger.warning("Connection failed: %s", msg)
        monitor.record_failure(url, str(exc))
        if monitor.is_degraded(url):
            await client.aclose()
            client = httpx.AsyncClient(...)
"""

import asyncio
import contextlib
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from time import time
from typing import Any, AsyncIterator


logger = logging.getLogger("encre.backends.connection")


# ---------------------------------------------------------------------------
# SSL / TLS error codes (from OpenSSL, used by both Python/httpx and Node)
# ---------------------------------------------------------------------------

_SSL_ERROR_CODES: set[str] = {
    # Certificate verification
    "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
    "UNABLE_TO_GET_ISSUER_CERT",
    "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
    "CERT_SIGNATURE_FAILURE",
    "CERT_NOT_YET_VALID",
    "CERT_HAS_EXPIRED",
    "CERT_REVOKED",
    "CERT_REJECTED",
    "CERT_UNTRUSTED",
    # Self-signed
    "DEPTH_ZERO_SELF_SIGNED_CERT",
    "SELF_SIGNED_CERT_IN_CHAIN",
    # Chain
    "CERT_CHAIN_TOO_LONG",
    "PATH_LENGTH_EXCEEDED",
    # Hostname
    "ERR_TLS_CERT_ALTNAME_INVALID",
    "HOSTNAME_MISMATCH",
    # Handshake
    "ERR_TLS_HANDSHAKE_TIMEOUT",
    "ERR_SSL_WRONG_VERSION_NUMBER",
    "ERR_SSL_DECRYPTION_FAILED_OR_BAD_RECORD_MAC",
    # Python ssl module codes
    "CERTIFICATE_VERIFY_FAILED",
    "SSL: CERTIFICATE_VERIFY_FAILED",
    "SSLV3_ALERT_BAD_CERTIFICATE",
    "SSLV3_ALERT_UNKNOWN_CA",
    "TLSV1_ALERT_ACCESS_DENIED",
}


_STALE_CONNECTION_CODES: set[str] = {"ECONNRESET", "EPIPE"}


class ConnectionErrorCategory(Enum):
    """Categorised connection error types for diagnostics and user messaging."""

    SSL = "ssl"                        # SSL/TLS certificate or handshake errors
    TIMEOUT = "timeout"                # Connection or read timeout
    DNS = "dns"                        # DNS resolution failures
    RESET = "reset"                    # ECONNRESET / EPIPE (stale socket)
    REFUSED = "refused"                # ECONNREFUSED
    UNREACHABLE = "unreachable"        # EHOSTUNREACH / ENETUNREACH
    UNKNOWN = "unknown"                # Unclassified


@dataclass
class ConnectionErrorInfo:
    """Structured information about a connection error.

    Attributes:
        category: The classified error category.
        code: The platform-level error code (e.g. ``ECONNRESET``).
        message: The original error message.
        is_ssl: Whether this is an SSL/TLS certificate error (convenience).
    """

    category: ConnectionErrorCategory
    code: str = ""
    message: str = ""
    is_ssl: bool = False


def _walk_cause_chain(exc: BaseException, depth: int = 5) -> BaseException | None:
    """Walk the ``__cause__`` / ``__context__`` chain up to *depth* levels."""
    current: BaseException | None = exc
    for _ in range(depth):
        if not current:
            return None
        if hasattr(current, "code") and isinstance(getattr(current, "code", None), str):
            return current
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return None


def extract_connection_error(exc: BaseException) -> ConnectionErrorInfo | None:
    """Extract structured error info from a connection-level exception.

    Walks the cause chain to find the root error code and message.  Returns
    ``None`` if the exception is not connection-related.
    """
    if not isinstance(exc, Exception):
        return None

    cause = _walk_cause_chain(exc)
    code: str = ""
    msg: str = str(exc)

    if cause:
        code = getattr(cause, "code", "") or ""
        msg = getattr(cause, "message", "") or str(cause)

    if isinstance(exc, TimeoutError):
        return ConnectionErrorInfo(
            category=ConnectionErrorCategory.TIMEOUT,
            code=code or "TIMEOUT",
            message=msg,
        )

    if isinstance(exc, ConnectionRefusedError):
        return ConnectionErrorInfo(
            category=ConnectionErrorCategory.REFUSED,
            code=code or "ECONNREFUSED",
            message=msg,
        )

    # Check for SSL errors first (highest priority).
    if code in _SSL_ERROR_CODES:
        return ConnectionErrorInfo(
            category=ConnectionErrorCategory.SSL,
            code=code,
            message=msg,
            is_ssl=True,
        )

    # Check SSL via message content.
    if _is_ssl_message(msg):
        return ConnectionErrorInfo(
            category=ConnectionErrorCategory.SSL,
            code=code or "SSL_ERROR",
            message=msg,
            is_ssl=True,
        )

    if code in _STALE_CONNECTION_CODES:
        return ConnectionErrorInfo(
            category=ConnectionErrorCategory.RESET,
            code=code,
            message=msg,
        )

    if code == "ECONNREFUSED" or "connection refused" in msg.lower():
        return ConnectionErrorInfo(
            category=ConnectionErrorCategory.REFUSED,
            code=code or "ECONNREFUSED",
            message=msg,
        )

    if code in ("ENOTFOUND", "EAI_AGAIN") or "name or service not known" in msg.lower():
        return ConnectionErrorInfo(
            category=ConnectionErrorCategory.DNS,
            code=code or "ENOTFOUND",
            message=msg,
        )

    if code in ("ETIMEDOUT", "ETIMEOUT") or "timed out" in msg.lower():
        return ConnectionErrorInfo(
            category=ConnectionErrorCategory.TIMEOUT,
            code=code or "ETIMEDOUT",
            message=msg,
        )

    if code in ("EHOSTUNREACH", "ENETUNREACH"):
        return ConnectionErrorInfo(
            category=ConnectionErrorCategory.UNREACHABLE,
            code=code,
            message=msg,
        )

    # httpx-specific: check for specific error types.
    exctype = type(exc).__name__
    if exctype in ("ConnectError", "RemoteProtocolError", "TransportError"):
        if "timeout" in msg.lower():
            return ConnectionErrorInfo(
                category=ConnectionErrorCategory.TIMEOUT,
                code=code or exctype.upper(),
                message=msg,
            )
        if "connection closed" in msg.lower():
            return ConnectionErrorInfo(
                category=ConnectionErrorCategory.RESET,
                code=code or "ECONNRESET",
                message=msg,
            )
        if "ssl" in msg.lower() or "certificate" in msg.lower():
            return ConnectionErrorInfo(
                category=ConnectionErrorCategory.SSL,
                code=code or "SSL_ERROR",
                message=msg,
                is_ssl=True,
            )
        if "dns" in msg.lower() or "resolve" in msg.lower():
            return ConnectionErrorInfo(
                category=ConnectionErrorCategory.DNS,
                code=code or "ENOTFOUND",
                message=msg,
            )

    return None


def _is_ssl_message(msg: str) -> bool:
    """Check if an error message indicates an SSL/TLS problem."""
    lower = msg.lower()
    patterns = (
        "ssl", "certificate", "tls", "handshake",
        "cert verify failed", "unable to verify",
        "self signed", "unknown ca",
    )
    return any(p in lower for p in patterns)


def format_connection_error(exc: BaseException) -> str:
    """Return a user-friendly diagnostic string for a connection error.

    Inspired by Claude Code's ``formatAPIError`` in ``errorUtils.ts``.
    """
    info = extract_connection_error(exc)
    if info is None:
        return f"Unable to connect to API: {exc}"

    if info.is_ssl:
        hints = {
            "UNABLE_TO_VERIFY_LEAF_SIGNATURE": "SSL certificate verification failed. Check your proxy or corporate SSL certificates.",
            "UNABLE_TO_GET_ISSUER_CERT": "Unable to get issuer certificate. Check your proxy or corporate SSL certificates.",
            "UNABLE_TO_GET_ISSUER_CERT_LOCALLY": "Issuer certificate not found locally. Check your proxy or corporate SSL certificates.",
            "CERT_HAS_EXPIRED": "SSL certificate has expired.",
            "CERT_REVOKED": "SSL certificate has been revoked.",
            "DEPTH_ZERO_SELF_SIGNED_CERT": "Self-signed certificate detected. Check your proxy or corporate SSL certificates.",
            "SELF_SIGNED_CERT_IN_CHAIN": "Self-signed certificate in chain detected. Check your proxy settings.",
            "ERR_TLS_CERT_ALTNAME_INVALID": "SSL certificate hostname mismatch.",
            "HOSTNAME_MISMATCH": "SSL certificate hostname mismatch.",
            "CERT_NOT_YET_VALID": "SSL certificate is not yet valid.",
            "CERTIFICATE_VERIFY_FAILED": "SSL certificate verification failed.",
        }
        hint = hints.get(info.code, f"SSL error ({info.code})")
        return f"Unable to connect to API: {hint}"

    if info.category == ConnectionErrorCategory.TIMEOUT:
        return "Request timed out. Check your internet connection and proxy settings."

    if info.category == ConnectionErrorCategory.RESET:
        return f"Connection was reset ({info.code}). This may be a transient network issue."

    if info.category == ConnectionErrorCategory.REFUSED:
        return "Connection refused. The API server may be down or your firewall may be blocking the request."

    if info.category == ConnectionErrorCategory.DNS:
        return "DNS resolution failed. Check your internet connection and DNS settings."

    if info.category == ConnectionErrorCategory.UNREACHABLE:
        return f"Host unreachable ({info.code}). Check your network connectivity."

    return f"Unable to connect to API ({info.code or 'unknown error'})"


# ---------------------------------------------------------------------------
# Connection health monitor
# ---------------------------------------------------------------------------


@dataclass
class EndpointHealth:
    """Health state for a single API endpoint (base URL).

    Attributes:
        url: The base URL being monitored.
        consecutive_failures: Current streak of connection failures.
        total_failures: Lifetime connection failures.
        total_requests: Lifetime request count.
        last_failure_time: Timestamp of the most recent failure.
        last_error: String representation of the last error.
        degraded_since: Timestamp when consecutive failures crossed threshold,
            or 0 if not degraded.
    """

    url: str
    consecutive_failures: int = 0
    total_failures: int = 0
    total_requests: int = 0
    last_failure_time: float = 0.0
    last_error: str = ""
    degraded_since: float = 0.0


class ConnectionHealthMonitor:
    """Tracks connection health per endpoint and provides circuit-breaker
    semantics.

    When an endpoint exceeds the configured consecutive failure threshold,
    it is marked as *degraded*.  Degraded endpoints are automatically retried
    after a recovery grace period.

    Usage::

        monitor = ConnectionHealthMonitor(
            consecutive_threshold=3,
            recovery_grace=60.0,
        )
        monitor.record_success("https://api.anthropic.com")
        info = monitor.get_health("https://api.anthropic.com")
        if info and monitor.is_degraded("https://api.anthropic.com"):
            # Use a different endpoint or refresh the connection pool.
    """

    def __init__(
        self,
        consecutive_threshold: int = 3,
        recovery_grace: float = 300.0,
    ) -> None:
        """Initialize the connection health monitor.

        Args:
            consecutive_threshold: Number of consecutive failures before an
                endpoint is marked degraded (circuit opens).
            recovery_grace: Seconds a degraded endpoint stays degraded
                before it is automatically retried.
        """
        self._consecutive_threshold = consecutive_threshold
        self._recovery_grace = recovery_grace
        # Per-endpoint health records keyed by base URL.
        self._endpoints: dict[str, EndpointHealth] = {}

    def record_success(self, url: str) -> None:
        """Record a successful connection to *url*."""
        health = self._endpoints.setdefault(url, EndpointHealth(url=url))
        health.consecutive_failures = 0
        health.total_requests += 1
        health.degraded_since = 0.0

    def record_failure(self, url: str, error: str = "") -> None:
        """Record a connection failure to *url*."""
        now = time()
        health = self._endpoints.setdefault(url, EndpointHealth(url=url))
        health.consecutive_failures += 1
        health.total_failures += 1
        health.total_requests += 1
        health.last_failure_time = now
        health.last_error = error
        if (
            health.consecutive_failures >= self._consecutive_threshold
            and health.degraded_since == 0.0
        ):
            health.degraded_since = now

    def is_degraded(self, url: str) -> bool:
        """Return ``True`` if *url* is currently degraded (circuit open)."""
        health = self._endpoints.get(url)
        if health is None or health.degraded_since == 0.0:
            return False
        # Check if recovery grace period has elapsed.
        if time() - health.degraded_since >= self._recovery_grace:
            health.degraded_since = 0.0
            health.consecutive_failures = 0
            return False
        return True

    def get_health(self, url: str) -> EndpointHealth | None:
        """Return the health info for *url*, or ``None`` if unknown."""
        return self._endpoints.get(url)

    def get_all_health(self) -> dict[str, dict[str, Any]]:
        """Return health info for all tracked endpoints."""
        return {
            url: {
                "consecutive_failures": h.consecutive_failures,
                "total_failures": h.total_failures,
                "total_requests": h.total_requests,
                "degraded": self.is_degraded(url),
                "last_error": h.last_error,
            }
            for url, h in self._endpoints.items()
        }

    def reset(self, url: str | None = None) -> None:
        """Reset health for *url*, or all endpoints if ``None``."""
        if url is None:
            self._endpoints.clear()
        else:
            self._endpoints.pop(url, None)


# ---------------------------------------------------------------------------
# Keepalive / heartbeat helpers  (Phase 4)
# ---------------------------------------------------------------------------


class HeartbeatSession:
    """Context manager that emits periodic heartbeats during long-running
    operations, preventing the host environment from marking the session idle.

    Inspired by Claude Code's unattended-retry heartbeat pattern
    (``HEARTBEAT_INTERVAL_MS = 30_000`` in ``withRetry.ts``).

    Usage::

        async with HeartbeatSession(
            total_delay=300.0,
            interval=30.0,
            label="Waiting for rate limit reset",
            on_heartbeat=lambda remaining: logger.info(
                "Still waiting... %.0fs left", remaining,
            ),
        ):
            await long_running_operation()
    """

    def __init__(
        self,
        total_delay: float,
        interval: float = 30.0,
        label: str = "",
        on_heartbeat: Any = None,
    ) -> None:
        """Initialize a heartbeat session.

        Args:
            total_delay: Total time (seconds) to wait while emitting
                heartbeats when used as an async context manager.
            interval: Seconds between successive heartbeats.
            label: Optional human-readable label for the wait.
            on_heartbeat: Optional callback invoked on each heartbeat; it
                receives the remaining seconds and may be async.
        """
        self._total_delay = total_delay
        self._interval = interval
        self._label = label
        self._on_heartbeat = on_heartbeat

    async def __aenter__(self) -> "HeartbeatSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        remaining = self._total_delay
        while remaining > 0:
            chunk = min(remaining, self._interval)
            if self._on_heartbeat:
                try:
                    result = self._on_heartbeat(remaining)
                    if result is not None:
                        await result
                except Exception:
                    logger.exception("Heartbeat callback failed")
            await asyncio.sleep(chunk)
            remaining -= chunk

    @asynccontextmanager
    @staticmethod
    async def heartbeat_every(
        interval: float = 30.0,
        label: str = "",
        on_heartbeat: Any = None,
    ) -> AsyncIterator[None]:
        """Context manager that runs a background heartbeat task.

        The heartbeat coroutine runs every *interval* seconds while inside
        the context.  Useful for operations whose duration is not known in
        advance.

        Usage::

            async with HeartbeatSession.heartbeat_every(
                interval=15.0,
                on_heartbeat=lambda: logger.info("Still alive"),
            ):
                await some_long_operation()
        """
        stopped = False

        async def _heartbeat_loop() -> None:
            while not stopped:
                try:
                    result = on_heartbeat() if on_heartbeat else None
                    if result is not None:
                        await result
                except Exception:
                    logger.exception("Heartbeat callback failed")
                await asyncio.sleep(interval)

        task = asyncio.create_task(_heartbeat_loop())
        try:
            yield
        finally:
            stopped = True
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
