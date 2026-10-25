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
Failover backend -- chains multiple backends with automatic failover.

:class:`FailoverBackend` wraps an ordered list of :class:`BaseBackend`
instances and tries them in turn.  When the active backend raises an
exception or yields a :class:`BackendError` (including 529/overloaded), the
partial event stream is discarded and the next healthy backend is attempted.
Only a complete, successful stream is forwarded to the caller.

Per-backend health is tracked via :class:`BackendHealth`; backends that fail
consecutively are marked unhealthy and skipped for a recovery grace period.
Consecutive 529 errors are tracked separately and, once they reach
``RetryConfig.consecutive_529_threshold``, trigger the
``on_fallback_triggered`` callback.
"""

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from encre.backends.base import BaseBackend
from encre.backends.connection import (
    ConnectionHealthMonitor,
    format_connection_error,
)
from encre.backends.retry import (
    DEFAULT_RETRY_CONFIG,
    ErrorClass,
    RetryConfig,
    classify_error,
)
from encre.utils.types import BackendError, BackendEvent, BackendFinish


from encre.logging_config import get_logger


logger = get_logger(__name__)

logger = logging.getLogger("encre.backends.failover")


@dataclass
class BackendHealth:
    """Health tracking for a single backend in the failover chain.

    Attributes:
        name: Backend identifier.
        healthy: Whether the backend is considered healthy.
        consecutive_failures: Total consecutive failures of any kind.
        consecutive_529_errors: Consecutive 529/overloaded errors (tracked
            separately for model-fallback decisions).
        last_checked: Timestamp of the last health check.
        last_error: String representation of the last error.
        total_requests: Lifetime request count.
        total_failures: Lifetime failure count.
    """

    name: str
    healthy: bool = True
    consecutive_failures: int = 0
    consecutive_529_errors: int = 0
    last_checked: float = 0.0
    last_error: str = ""
    total_requests: int = 0
    total_failures: int = 0

    def record_success(self) -> None:
        """Reset failure counters after a successful request.

        Marks the backend healthy again and clears both the generic and
        529-specific consecutive failure counters.
        """
        self.healthy = True
        self.consecutive_failures = 0
        self.consecutive_529_errors = 0
        self.total_requests += 1

    def record_failure(self, error: str, is_overloaded: bool = False) -> None:
        """Record a failed request and update health state.

        Args:
            error: A string description of the failure.
            is_overloaded: True if the failure was a 529/overloaded error,
                which is tracked separately for model-fallback decisions.
        """
        self.consecutive_failures += 1
        self.total_failures += 1
        self.total_requests += 1
        self.last_error = error
        # Track 529/overloaded streaks separately; any other error resets it.
        if is_overloaded:
            self.consecutive_529_errors += 1
        else:
            self.consecutive_529_errors = 0
        # Trip the health flag once failures accumulate past the threshold.
        if self.consecutive_failures >= 3:
            self.healthy = False


class FailoverBackend(BaseBackend):
    """Backend that chains multiple backends with automatic failover.

    When the primary backend fails (timeout, rate limit, API error), the next
    backend in the chain is tried.  Health status is tracked and unhealthy
    backends are skipped for a grace period before being retried.

    Consecutive 529 (overloaded) errors are tracked separately: when the
    threshold from ``RetryConfig.consecutive_529_threshold`` is reached on a
    backend, the ``on_fallback_triggered`` callback is fired and the next
    backend in the chain is tried.

    Events are buffered from each backend attempt so that partial output
    from a failed backend is never yielded to the caller.  Only a clean,
    complete stream from a successful backend reaches the consumer.

    Usage:
        config = RetryConfig(
            consecutive_529_threshold=3,
            on_fallback_triggered=lambda o, f: logger.warning(
                "Falling back %s -> %s", o, f
            ),
        )
        failover = FailoverBackend(
            backends=[
                ("gpt-5", OpenAIBackend(model="gpt-5", api_key="...")),
                ("claude-sonnet", AnthropicBackend(model="claude-sonnet-5", api_key="...")),
            ],
            retry_config=config,
        )
    """

    MAX_CONSECUTIVE_FAILURES = 3
    RECOVERY_GRACE_PERIOD = 300.0

    def __init__(
        self,
        backends: list[tuple[str, BaseBackend]],
        retry_config: RetryConfig = DEFAULT_RETRY_CONFIG,
        connection_monitor: ConnectionHealthMonitor | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the failover backend.

        Args:
            backends: Ordered list of ``(name, backend)`` tuples. The first
                entry is treated as the primary backend and its capabilities
                are used for capability queries.
            retry_config: Retry/fallback configuration (thresholds and
                callbacks) shared across the chain.
            connection_monitor: Optional connection health monitor; a new
                one is created when not provided.

        Raises:
            ValueError: If ``backends`` is empty.
        """
        # ``create_backend`` forwards per-model thinking kwargs that do not
        # apply to the failover wrapper itself; ignore them safely.
        _ = kwargs
        if not backends:
            raise ValueError("At least one backend is required")
        self._backends: list[tuple[str, BaseBackend]] = backends
        self._retry_config = retry_config
        # One health record per backend, keyed by its name.
        self._health: dict[str, BackendHealth] = {
            name: BackendHealth(name=name) for name, _ in backends
        }
        self._connection_monitor = connection_monitor or ConnectionHealthMonitor()
        # The first backend is the primary used for capability delegation.
        self._primary = backends[0][1]
        self._active_name: str = backends[0][0]
        self._lock = asyncio.Lock()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = True,
        enable_caching: bool = False,
        cache_edits_state: Any = None,
    ) -> AsyncGenerator[BackendEvent, None]:
        """Send a chat completion request with automatic failover.

        Events from each backend attempt are buffered.  If the current
        backend fails (exception or BackendError), the buffer is discarded
        and the next healthy backend is tried.  Only a complete, successful
        stream reaches the caller.

        When consecutive 529 overload errors on a backend exceed
        ``RetryConfig.consecutive_529_threshold``, the
        ``on_fallback_triggered`` callback fires and the next backend in the
        chain is used.
        """
        errors: list[str] = []

        for name, backend in self._backends:
            health = self._health[name]

            # Skip backends degraded at the connection level.
            if self._connection_monitor.is_degraded(name):
                logger.warning("Backend %s connection degraded, skipping", name)
                continue

            # Skip unhealthy backends still in their recovery grace period.
            if not health.healthy:
                elapsed = time.time() - health.last_checked
                if elapsed < self.RECOVERY_GRACE_PERIOD:
                    continue
                health.healthy = True
                health.consecutive_failures = 0
                health.consecutive_529_errors = 0
                logger.info(
                    "Backend %s recovery grace period elapsed (%.0fs), re-enabling",
                    name, elapsed,
                )

            health.last_checked = time.time()

            # Check consecutive 529 threshold *before* attempting.
            threshold = self._retry_config.consecutive_529_threshold
            if threshold > 0 and health.consecutive_529_errors >= threshold:
                logger.warning(
                    "Backend %s consecutive 529 errors (%d/%d), skipping",
                    name, health.consecutive_529_errors, threshold,
                )
                health.healthy = True  # Not permanently unhealthy, just overloaded
                continue

            buffer: list[BackendEvent] = []
            failed = False
            error_msg = ""
            is_overloaded = False

            try:
                async with self._lock:
                    self._active_name = name

                # Forward the cache-edits state so prompt-cache edits survive
                # failover.  Skipped when unset (None) so legacy chat()
                # implementations predating the parameter keep working.
                _chat_kwargs: dict[str, Any] = {
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": tool_choice,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": stream,
                    "enable_caching": enable_caching,
                }
                if cache_edits_state is not None:
                    _chat_kwargs["cache_edits_state"] = cache_edits_state
                async for event in backend.chat(**_chat_kwargs):
                    if isinstance(event, BackendError):
                        error_msg = event.error
                        failed = True
                        is_overloaded = self._is_overloaded_error(event.error)
                        break
                    buffer.append(event)
                    if isinstance(event, BackendFinish):
                        break

            except Exception as e:
                error_msg = str(e)
                failed = True
                is_overloaded = self._is_overloaded_exception(e)
                if not is_overloaded:
                    error_class = classify_error(e)
                    if error_class in (
                        ErrorClass.TIMEOUT,
                        ErrorClass.CONNECTION_ERROR,
                        ErrorClass.STALE_CONNECTION,
                    ):
                        self._connection_monitor.record_failure(
                            name, format_connection_error(e),
                        )

            if failed:
                health.record_failure(error_msg, is_overloaded=is_overloaded)
                errors.append(f"[{name}] {error_msg}")

                # Fire fallback callback when consecutive 529 threshold is hit.
                if (
                    is_overloaded
                    and threshold > 0
                    and health.consecutive_529_errors >= threshold
                ):
                    self._fire_fallback(name)
                continue

            health.record_success()
            self._connection_monitor.record_success(name)
            for event in buffer:
                yield event
            return

        yield BackendError(
            error=f"All backends failed: {'; '.join(errors)}"
        )

    def _fire_fallback(self, failed_name: str) -> None:
        """Invoke the fallback callback with original -> next model info."""
        cb = self._retry_config.on_fallback_triggered
        if cb is None:
            return

        # Determine the next backend name for the callback.
        next_name: str | None = None
        found = False
        for name, _ in self._backends:
            if found:
                next_name = name
                break
            if name == failed_name:
                found = True

        try:
            result = cb(failed_name, next_name or "(none)")
            if result is not None:
                asyncio.ensure_future(result)
        except Exception:
            logger.exception("on_fallback_triggered callback failed")

    @staticmethod
    def _is_overloaded_error(error_text: str) -> bool:
        """Heuristic: does the error text indicate a 529 / overloaded response?

        The status code is matched with a word-boundary regex so substrings
        like ``"max_tokens 5290"`` do not misfire.
        """
        lower = error_text.lower()
        return (
            re.search(r"\b529\b", lower) is not None
            or "overloaded" in lower
            or "capacity" in lower
        )

    @staticmethod
    def _is_overloaded_exception(exc: Exception) -> bool:
        """Check if an exception represents a 529 / overloaded condition."""
        try:
            return classify_error(exc) == ErrorClass.OVERLOADED
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
        if hasattr(exc, "response") and hasattr(exc.response, "status_code"):
            return exc.response.status_code == 529

        text = str(exc).lower()
        if (
            re.search(r"\b529\b", text) is not None
            or "overloaded" in text
            or "capacity" in text
        ):
            return True

        return False

    def supports_tool_calling(self) -> bool:
        """Delegate tool-calling capability to the primary backend."""
        return self._primary.supports_tool_calling()

    def context_window_size(self) -> int:
        """Delegate the context window size to the primary backend."""
        return self._primary.context_window_size()

    def supports_thinking(self) -> bool:
        """Delegate thinking-token support to the primary backend."""
        return self._primary.supports_thinking()

    def supports_prompt_caching(self) -> bool:
        """Delegate prompt-caching support to the primary backend."""
        return self._primary.supports_prompt_caching()

    def count_tokens(self, text: str) -> int:
        """Delegate token counting to the primary backend."""
        return self._primary.count_tokens(text)

    def get_health(self) -> dict[str, dict[str, Any]]:
        """Return a per-backend health snapshot for diagnostics.

        Combines the failover chain's health records with the connection
        monitor's degraded state into a single dictionary keyed by name.
        """
        conn_health = self._connection_monitor.get_all_health()
        return {
            name: {
                "healthy": h.healthy,
                "consecutive_failures": h.consecutive_failures,
                "consecutive_529_errors": h.consecutive_529_errors,
                "total_requests": h.total_requests,
                "total_failures": h.total_failures,
                "last_error": h.last_error,
                "connection_degraded": conn_health.get(name, {}).get("degraded", False),
            }
            for name, h in self._health.items()
        }

    @property
    def active_backend_name(self) -> str:
        """Return the name of the backend most recently attempted/active."""
        return self._active_name

    async def aclose(self) -> None:
        """Close every backend in the chain, ignoring individual errors."""
        for _, backend in self._backends:
            with contextlib.suppress(Exception):
                await backend.aclose()
