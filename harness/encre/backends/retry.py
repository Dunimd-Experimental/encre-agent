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
Layered retry engine for LLM API calls.

Provides a configurable retry decorator that handles transient HTTP errors
(429 rate limits, 502/503/504 server errors, 529 overloaded), connection
timeouts, auth failures, and context-overflow adjustments — mirroring the
sophistication of production-grade agents like Claude Code.

Design decisions
----------------
- **Source-aware retry**: Foreground requests retry 529s; background ones bail
  immediately to avoid capacity-cascade amplification.
- **Retry-After support**: Server-provided wait times are honoured and take
  precedence over exponential backoff.
- **Consecutive-529 tracking**: After N consecutive overload errors the
  configured fallback callback fires, enabling automatic model downgrade.
- **Auth refresh on 401**: If an ``on_auth_required`` callback is registered,
  it is invoked before the retry so the client can refresh credentials.
- **Context overflow auto-adjust**: 400 ``max_tokens exceed context limit``
  errors are handled by reducing the output budget and retrying.
- **Progress notification**: An optional ``on_retry`` callback fires before
  each sleep with structured metadata (attempt, delay, error).
- **Persistent/unattended mode**: Unlimited retries with heartbeat-gated waits
  so the host environment does not mark the session idle.
- **Backward compatible**: All new config fields have ``None`` defaults and
  the existing ``retry_with_backoff`` / ``RetryConfig`` API is unchanged.

Typical usage::

    from encre.backends.retry import retry_with_backoff, RetryConfig

    config = RetryConfig(
        max_retries=10,
        base_delay=0.5,
        on_fallback_triggered=lambda m, f: logger.warning(
            "Fallback from %s to %s", m, f
        ),
    )

    @retry_with_backoff(config=config)
    async def call_llm_api(client, payload):
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.json()
"""

import asyncio
import inspect
import logging
import random
import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, TypeVar

import httpx

logger = logging.getLogger("encre.backends.retry")

F = TypeVar("F", bound=Callable[..., Any])

# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class ErrorClass(Enum):
    """Categorised API error types for analytics and retry decision-making."""

    RATE_LIMIT = "rate_limit"            # 429
    OVERLOADED = "overloaded"            # 529 or overloaded_error in body
    SERVER_ERROR = "server_error"        # 5xx (500, 502, 503, 504)
    AUTH_ERROR = "auth_error"            # 401, 403
    TIMEOUT = "timeout"                  # 408 / APIConnectionTimeout
    CONNECTION_ERROR = "connection_error"  # DNS, TCP, SSL failures
    STALE_CONNECTION = "stale_connection"  # ECONNRESET, EPIPE
    CONTEXT_OVERFLOW = "context_overflow"  # 400 input_length + max_tokens > limit
    INVALID_REQUEST = "invalid_request"   # 400 / 422 (non-overflow)
    NOT_FOUND = "not_found"              # 404
    UNKNOWN = "unknown"


def classify_error(exc: Exception) -> ErrorClass:
    """Classify an exception into a specific error category.

    Walks the cause chain for connection-level errors and inspects
    ``httpx.HTTPStatusError`` status codes / response bodies for API-level
    errors.
    """
    if isinstance(exc, httpx.TimeoutException):
        return ErrorClass.TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        cause = _walk_cause_chain(exc)
        if cause and hasattr(cause, "code"):
            code = cause.code
            if code in ("ECONNRESET", "EPIPE"):
                return ErrorClass.STALE_CONNECTION
        return ErrorClass.CONNECTION_ERROR
    if isinstance(exc, httpx.RemoteProtocolError):
        if "connection closed" in str(exc).lower():
            return ErrorClass.STALE_CONNECTION
        return ErrorClass.CONNECTION_ERROR
    if isinstance(exc, httpx.TransportError):
        cause_error = _walk_cause_chain(exc)
        if cause_error and hasattr(cause_error, "code"):
            if cause_error.code in ("ECONNRESET", "EPIPE"):
                return ErrorClass.STALE_CONNECTION
        return ErrorClass.CONNECTION_ERROR

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        body = _try_read_body(exc.response)

        if status == 429:
            return ErrorClass.RATE_LIMIT
        if status in (502, 503, 504, 500):
            return ErrorClass.SERVER_ERROR
        if status == 529:
            return ErrorClass.OVERLOADED
        if status in (401, 403):
            return ErrorClass.AUTH_ERROR
        if status == 408:
            return ErrorClass.TIMEOUT
        if status == 404:
            return ErrorClass.NOT_FOUND
        if status == 400:
            if body and _is_context_overflow(body):
                return ErrorClass.CONTEXT_OVERFLOW
            return ErrorClass.INVALID_REQUEST
        if status == 422:
            return ErrorClass.INVALID_REQUEST
        return ErrorClass.UNKNOWN

    return ErrorClass.UNKNOWN


def _walk_cause_chain(exc: Exception, depth: int = 5) -> Exception | None:
    """Walk the ``__cause__`` / ``__context__`` chain up to *depth* levels."""
    current: Exception | None = exc
    for _ in range(depth):
        if not current:
            return None
        if hasattr(current, "code") and isinstance(current.code, str):
            return current
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return None


_CONTEXT_OVERFLOW_PATTERN = re.compile(
    r"input length and `max_tokens` exceed context limit:\s*(\d+)\s*\+\s*(\d+)\s*>\s*(\d+)"
)
_OVERLOADED_PATTERN = re.compile(r'"type"\s*:\s*"overloaded_error"')


def _is_context_overflow(body: str) -> bool:
    return bool(_CONTEXT_OVERFLOW_PATTERN.search(body))


def _parse_context_overflow(body: str) -> tuple[int, int, int] | None:
    """Return ``(input_tokens, requested_max_tokens, context_limit)`` or None."""
    m = _CONTEXT_OVERFLOW_PATTERN.search(body)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _is_overloaded_error(body: str) -> bool:
    return bool(_OVERLOADED_PATTERN.search(body))


def _try_read_body(response: httpx.Response) -> str | None:
    try:
        return response.text
    except Exception:
        return None


def _is_connection_error(exc: Exception) -> bool:
    return isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError, httpx.TransportError))


def _is_stale_connection(exc: Exception) -> bool:
    if not _is_connection_error(exc):
        return False
    cause = _walk_cause_chain(exc)
    return bool(cause and hasattr(cause, "code") and cause.code in ("ECONNRESET", "EPIPE"))


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class RetryEvent:
    """Emitted before a retry sleep, analogous to ``SystemAPIErrorMessage``.

    Attributes:
        attempt: Which retry attempt this is (1-indexed).
        max_retries: The configured maximum number of retries.
        delay_ms: How long the caller will wait before retrying (milliseconds).
        error_class: The :class:`ErrorClass` of the triggering error.
        status_code: HTTP status code (0 for non-HTTP errors).
        message: Human-readable error summary.
    """

    attempt: int
    max_retries: int
    delay_ms: int
    error_class: ErrorClass
    status_code: int
    message: str


# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------


@dataclass
class RetryConfig:
    """Configuration for the layered retry engine.

    All new fields default to ``None`` / sensible defaults so existing callers
    are unaffected.

    Attributes:
        max_retries: Maximum retry attempts for 5xx / connection errors.
        base_delay: Base delay in seconds for exponential backoff.
        max_delay: Maximum delay cap in seconds (standard).
        retryable_status_codes: HTTP status codes that trigger retry.
        retryable_exceptions: Exception types that trigger retry.
        rate_limit_retries: Separate (higher) retry budget for 429.
        consecutive_529_threshold: Consecutive 529s before triggering fallback.
            Set to 0 to disable.
        query_source: If ``"background"``, 529/overloaded errors are *not*
            retried (avoids capacity-cascade amplification).
        on_fallback_triggered: Async callback ``(original_model, fallback_model)``
            invoked when consecutive 529 threshold is reached.
        on_auth_required: Async callback ``() -> None`` invoked before retrying
            a 401/403, so the caller can refresh credentials.
        on_retry: Async callback ``(event: RetryEvent) -> None`` invoked before
            each retry sleep.
        persistent_retry: If True, retries 429/529 indefinitely with a higher
            max delay and heartbeat-gated waits for unattended sessions.
        persistent_max_delay: Max delay cap in seconds for persistent mode.
        heartbeat_interval: Seconds between heartbeats during persistent waits.
        adjust_max_tokens_on_overflow: If True, on 400 context overflow the
            decorator reduces max_tokens and retries.
    """

    max_retries: int = 8
    base_delay: float = 2.0
    max_delay: float = 120.0
    retryable_status_codes: set[int] = field(
        default_factory=lambda: {429, 502, 503, 504}
    )
    retryable_exceptions: set[type[Exception]] = field(
        default_factory=lambda: {
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.TransportError,
        }
    )
    rate_limit_retries: int = 8

    # --- Layered retry enhancements ----------------------------------------
    consecutive_529_threshold: int = 3
    query_source: str | None = None
    on_fallback_triggered: Callable[..., Any] | None = None
    on_auth_required: Callable[..., Any] | None = None
    on_retry: Callable[..., Any] | None = None
    persistent_retry: bool = False
    persistent_max_delay: float = 300.0
    heartbeat_interval: float = 30.0
    adjust_max_tokens_on_overflow: bool = False


# Default configuration suitable for most LLM API providers.
DEFAULT_RETRY_CONFIG = RetryConfig()

#: Ambient retry-notice sink for the turn currently being run.
#:
#: The retry engine knows nothing about transports, and a backend instance is
#: shared and long-lived, so a caller cannot hand it a per-turn callback
#: without rebuilding the backend.  This context variable carries that callback
#: through the task context instead: the loop installs one before driving
#: ``backend.chat()``, and the engine invokes whatever it finds here (an
#: explicit ``RetryConfig.on_retry`` still wins).  A model call issued outside a
#: turn -- compaction, a background probe -- finds ``None`` and stays quiet.
RETRY_NOTICE_SINK: ContextVar[Callable[[RetryEvent], Any] | None] = ContextVar(
    "encre_retry_notice_sink", default=None
)

# Static 529 close-code that some providers return.
_529_STATUS = 529

# Safety buffer for context overflow auto-adjustment.
_CONTEXT_OVERFLOW_SAFETY_BUFFER = 1000
_CONTEXT_OVERFLOW_FLOOR_TOKENS = 3000


# ---------------------------------------------------------------------------
# Retry decision helpers
# ---------------------------------------------------------------------------


def _get_retry_after(response: httpx.Response) -> float | None:
    """Return ``retry-after`` in seconds, or ``None``."""
    val = response.headers.get("retry-after")
    if val is None:
        val = response.headers.get("Retry-After")
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _get_rate_limit_reset(response: httpx.Response) -> float | None:
    """Return rate-limit reset delay in seconds from custom headers, or None."""
    val = response.headers.get("x-ratelimit-reset")
    if val is not None:
        try:
            return float(val)
        except (ValueError, TypeError):
            return None
    return None


def _is_retryable_status(status: int, body: str | None, error_class: ErrorClass, config: RetryConfig) -> bool:
    """Decide whether *status* + *error_class* warrants a retry.

    Source-aware: background queries skip 529/overloaded retries.
    """
    # Non-HTTP status (connection errors) — retryable by exception catch below.
    if status == 0:
        return True

    base_retryable = status in config.retryable_status_codes

    # 529 is not in the default retryable set — check explicitly.
    if status == _529_STATUS or (body and _is_overloaded_error(body)):
        # Background sources bail immediately on 529.
        if config.query_source == "background":
            return False
        return True

    # Auth errors retryable only if a refresh callback is registered.
    if status in (401, 403) and config.on_auth_required is not None:
        return True

    # Context overflow is retryable only if auto-adjust is enabled.
    if error_class == ErrorClass.CONTEXT_OVERFLOW and config.adjust_max_tokens_on_overflow:
        return True

    return base_retryable


# ---------------------------------------------------------------------------
# Delay computation
# ---------------------------------------------------------------------------


def _compute_delay(
    attempt: int,
    config: RetryConfig,
    retry_after: float | None = None,
    rate_limit_reset: float | None = None,
) -> float:
    """Compute sleep delay with optional server-directed waits.

    Priority:
    1. ``retry-after`` header (server directive, capped at ``max_delay``)
    2. Rate-limit reset header (``x-ratelimit-reset``, capped at ``max_delay``)
    3. Exponential backoff with equal jitter (cap/2 + random(0, cap/2))
    """
    max_delay = config.persistent_max_delay if config.persistent_retry else config.max_delay
    if retry_after is not None and retry_after > 0:
        return min(retry_after, max_delay)
    if rate_limit_reset is not None and rate_limit_reset > 0:
        return min(rate_limit_reset, max_delay)

    cap = min(config.base_delay * (2**attempt), max_delay)

    # Equal jitter — smoother retry distribution than full jitter.
    half = cap / 2.0
    return half + random.uniform(0, half)


# ---------------------------------------------------------------------------
# Retry state
# ---------------------------------------------------------------------------


class _RetryState:
    """Mutable state for a single retry loop invocation.

    Tracks consecutive errors, persistent attempt counter, and provides
    convenience helpers.
    """

    def __init__(self, config: RetryConfig) -> None:
        self.config = config
        self.consecutive_529_errors = 0
        self.persistent_attempt = 0
        self.last_error: Exception | None = None
        self.last_error_class: ErrorClass = ErrorClass.UNKNOWN
        self.last_status_code: int = 0
        self.last_body: str | None = None
        # Context overflow adjustment (propagated via kwargs passthrough)
        self.max_tokens_override: int | None = None

    def record_error(self, exc: Exception) -> None:
        self.last_error = exc
        self.last_error_class = classify_error(exc)

        if isinstance(exc, httpx.HTTPStatusError):
            self.last_status_code = exc.response.status_code
            self.last_body = _try_read_body(exc.response)
        else:
            self.last_status_code = 0
            self.last_body = None

        if self.last_status_code == _529_STATUS or (
            self.last_body and _is_overloaded_error(self.last_body)
        ):
            self.consecutive_529_errors += 1
        else:
            self.consecutive_529_errors = 0

    def should_fallback(self) -> bool:
        """True when consecutive 529s exceed the threshold."""
        threshold = self.config.consecutive_529_threshold
        return threshold > 0 and self.consecutive_529_errors >= threshold

    def compute_delay(self) -> float:
        retry_after: float | None = None
        rate_limit_reset: float | None = None
        if isinstance(self.last_error, httpx.HTTPStatusError):
            retry_after = _get_retry_after(self.last_error.response)
            rate_limit_reset = _get_rate_limit_reset(self.last_error.response)

        return _compute_delay(
            attempt=self.persistent_attempt,
            config=self.config,
            retry_after=retry_after,
            rate_limit_reset=rate_limit_reset,
        )

    def check_context_overflow(self) -> int | None:
        """If body indicates a context overflow, return a reduced ``max_tokens``."""
        if not self.config.adjust_max_tokens_on_overflow:
            return None
        if not isinstance(self.last_error, httpx.HTTPStatusError):
            return None
        parsed = _parse_context_overflow(self.last_body or "")
        if not parsed:
            return None
        input_tokens, _requested_tokens, context_limit = parsed
        available = max(0, context_limit - input_tokens - _CONTEXT_OVERFLOW_SAFETY_BUFFER)
        if available < _CONTEXT_OVERFLOW_FLOOR_TOKENS:
            return None  # not enough room even after reduction
        return max(_CONTEXT_OVERFLOW_FLOOR_TOKENS, available)

    def make_retry_event(self, delay_ms: int, attempt: int | None = None) -> RetryEvent:
        # ``attempt`` is the caller's loop index (0-based); report it 1-based so
        # the first retry reads "attempt 1". Without it the field was left at 0
        # for every non-persistent retry.
        reported = (attempt + 1) if attempt is not None else self.persistent_attempt
        return RetryEvent(
            attempt=reported,
            max_retries=0 if self.config.persistent_retry else self.config.max_retries,
            delay_ms=delay_ms,
            error_class=self.last_error_class,
            status_code=self.last_status_code,
            message=str(self.last_error or ""),
        )


# ---------------------------------------------------------------------------
# Main retry loop
# ---------------------------------------------------------------------------


async def _notify_on_retry(config: RetryConfig, event: RetryEvent) -> None:
    """Announce an imminent retry to whoever is observing.

    An explicit ``RetryConfig.on_retry`` wins; otherwise the ambient
    :data:`RETRY_NOTICE_SINK` is used, which is how a turn observes retries
    without every backend having to be rebuilt with a callback. Best-effort by
    construction: a broken observer must never turn a retryable error into a
    hard failure.
    """
    sink = config.on_retry if config.on_retry is not None else RETRY_NOTICE_SINK.get()
    if sink is None:
        return
    try:
        result = sink(event)
        if result is not None:
            await result
    except Exception:
        logger.exception("retry notice observer failed")


async def _notify_fallback(config: RetryConfig, original: str, fallback: str) -> None:
    """Invoke the fallback callback if registered."""
    if config.on_fallback_triggered is None:
        return
    try:
        result = config.on_fallback_triggered(original, fallback)
        if result is not None:
            await result
    except Exception:
        logger.exception("on_fallback_triggered callback failed")


async def _notify_auth_required(config: RetryConfig) -> None:
    """Invoke the auth refresh callback if registered."""
    if config.on_auth_required is None:
        return
    try:
        result = config.on_auth_required()
        if result is not None:
            await result
    except Exception:
        logger.exception("on_auth_required callback failed")


# ---------------------------------------------------------------------------
# Heartbeat sleep for persistent mode
# ---------------------------------------------------------------------------


async def _sleep_with_heartbeat(
    delay_ms: int,
    config: RetryConfig,
    state: _RetryState,
    emit: Callable[[RetryEvent], Any] | None = None,
) -> None:
    """Sleep for *delay_ms*, yielding heartbeats every ``heartbeat_interval``.

    In persistent mode the host environment might kill an idle session; periodic
    heartbeats (via the ``emit`` callback) keep the session alive.
    """
    remaining = delay_ms / 1000.0
    hb = config.heartbeat_interval
    while remaining > 0:
        if emit and state.last_error_class == ErrorClass.OVERLOADED:
            event = state.make_retry_event(int(remaining * 1000))
            emit(event)
        chunk = min(remaining, hb)
        await asyncio.sleep(chunk)
        remaining -= chunk
    if config.persistent_retry:
        state.persistent_attempt += 1


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def retry_with_backoff(
    config: RetryConfig = DEFAULT_RETRY_CONFIG,
) -> Callable[[F], F]:
    """Decorator that adds layered retry logic to an async function or generator.

    See :class:`RetryConfig` for configuration details.
    """
    def decorator(func: F) -> F:
        if inspect.isasyncgenfunction(func):
            return _wrap_async_gen(func, config)  # type: ignore[return-value]

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            state = _RetryState(config)

            attempt = 0
            # Total attempts = retries + 1 initial try; None means unbounded (persistent mode).
            max_attempts = (config.max_retries + 1) if not config.persistent_retry else None

            while max_attempts is None or attempt < max_attempts:
                state.persistent_attempt = attempt

                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    state.record_error(exc)

                    # --- Handle context overflow: adjust max_tokens and retry ---
                    if state.last_error_class == ErrorClass.CONTEXT_OVERFLOW:
                        if not config.adjust_max_tokens_on_overflow:
                            raise
                        overflow_max_tokens = state.check_context_overflow()
                        if overflow_max_tokens is None:
                            raise
                        logger.info(
                            "Context overflow: reducing max_tokens to %d (attempt %d)",
                            overflow_max_tokens, attempt + 1,
                        )
                        delay = state.compute_delay()
                        event = state.make_retry_event(int(delay * 1000), attempt)
                        await _notify_on_retry(config, event)
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue

                    # --- Fallback trigger: consecutive 529s ---
                    if state.should_fallback():
                        logger.warning(
                            "Consecutive 529 threshold reached (%d/%d)",
                            state.consecutive_529_errors, config.consecutive_529_threshold,
                        )
                        raise

                    # --- Non-retryable: fail immediately ---
                    eclass = state.last_error_class
                    status = state.last_status_code
                    body = state.last_body
                    if not _is_retryable_status(status, body, eclass, config):
                        raise

                    # --- Auth refresh before retry ---
                    if status in (401, 403) and config.on_auth_required is not None:
                        logger.info("Auth error (status %d), refreshing credentials", status)
                        await _notify_auth_required(config)

                    # --- Exhausted retry budget ---
                    if status == 429:
                        if attempt >= config.rate_limit_retries and not config.persistent_retry:
                            raise
                    elif status == _529_STATUS or (body and _is_overloaded_error(body)):
                        if attempt >= config.rate_limit_retries and not config.persistent_retry:
                            raise
                    elif not config.persistent_retry and attempt >= config.max_retries:
                        raise

                    # --- Sleep & notify ---
                    delay = state.compute_delay()
                    delay_ms = int(delay * 1000)
                    event = state.make_retry_event(delay_ms, attempt)
                    await _notify_on_retry(config, event)

                    if config.persistent_retry:
                        await _sleep_with_heartbeat(delay_ms, config, state)
                    else:
                        await asyncio.sleep(delay)

                    attempt += 1

            # All retries exhausted (non-persistent only).
            if state.last_error is not None:
                raise state.last_error

        return wrapper  # type: ignore[return-value]

    return decorator


def _wrap_async_gen(func, config: RetryConfig):
    """Wrap an async generator with layered retry logic.

    Only failures that occur BEFORE the first yielded event are retried.
    Once an event has been delivered to the caller it cannot be retracted,
    so a mid-stream failure propagates instead of re-invoking the generator
    (which would duplicate text and tool calls).
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        state = _RetryState(config)

        attempt = 0
        max_attempts = (config.max_retries + 1) if not config.persistent_retry else None

        while max_attempts is None or attempt < max_attempts:
            state.persistent_attempt = attempt

            yielded_any = False
            try:
                async for item in func(*args, **kwargs):
                    yield item
                    yielded_any = True
                return  # Stream completed successfully.
            except Exception as exc:
                # Mid-stream failure: deltas already yielded cannot be
                # retracted, so retrying would duplicate them.  Propagate.
                if yielded_any:
                    raise
                state.record_error(exc)

                # --- Context overflow ---
                if state.last_error_class == ErrorClass.CONTEXT_OVERFLOW:
                    if not config.adjust_max_tokens_on_overflow:
                        raise
                    overflow_max_tokens = state.check_context_overflow()
                    if overflow_max_tokens is None:
                        raise
                    logger.info(
                        "Context overflow: reducing max_tokens to %d (attempt %d)",
                        overflow_max_tokens, attempt + 1,
                    )
                    delay = state.compute_delay()
                    event = state.make_retry_event(int(delay * 1000), attempt)
                    await _notify_on_retry(config, event)
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue

                # --- Fallback trigger ---
                if state.should_fallback():
                    raise

                # --- Non-retryable ---
                eclass = state.last_error_class
                status = state.last_status_code
                body = state.last_body
                if not _is_retryable_status(status, body, eclass, config):
                    raise

                # --- Auth refresh ---
                if status in (401, 403) and config.on_auth_required is not None:
                    await _notify_auth_required(config)

                # --- Exhausted budget ---
                if status == 429:
                    if attempt >= config.rate_limit_retries and not config.persistent_retry:
                        raise
                elif status == _529_STATUS or (body and _is_overloaded_error(body)):
                    if attempt >= config.rate_limit_retries and not config.persistent_retry:
                        raise
                elif not config.persistent_retry and attempt >= config.max_retries:
                    raise

                # --- Sleep & notify ---
                delay = state.compute_delay()
                delay_ms = int(delay * 1000)
                event = state.make_retry_event(delay_ms, attempt)
                await _notify_on_retry(config, event)

                if config.persistent_retry:
                    await _sleep_with_heartbeat(delay_ms, config, state)
                else:
                    await asyncio.sleep(delay)

                attempt += 1

        if state.last_error is not None:
            raise state.last_error

    return wrapper
