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
Auth management for LLM API calls.

Provides credential lifecycle management — key rotation, automatic refresh on
401/403 errors, and failure tracking.  Designed to integrate with the layered
retry engine via ``RetryConfig.on_auth_required``.

Typical usage::

    from encre.backends.auth import AuthManager, AuthEvent
    from encre.backends.retry import RetryConfig

    auth = AuthManager(provider="anthropic")

    # Register a refresh callback — called on 401/403.
    auth.on_auth_required = lambda: auth.set_api_key(new_key)

    config = RetryConfig(
        on_auth_required=auth.refresh,
    )
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("encre.backends.auth")


@dataclass
class AuthHealth:
    """Tracks authentication health for a credential source.

    Attributes:
        last_auth_error: Timestamp of the last auth failure (401/403).
        consecutive_auth_failures: Consecutive auth failures before success.
        total_auth_failures: Lifetime auth failure count.
        last_refresh_time: Timestamp of the last credential refresh.
        refresh_count: Total number of credential refreshes.
    """

    last_auth_error: float = 0.0
    consecutive_auth_failures: int = 0
    total_auth_failures: int = 0
    last_refresh_time: float = 0.0
    refresh_count: int = 0


class AuthManager:
    """Manages API credential lifecycle for a provider.

    Provides:
    - Credential storage with rotation
    - Auto-refresh callback for the retry engine
    - Auth failure tracking with circuit-breaker
    - Atomic credential swap

    Usage::

        auth = AuthManager(
            provider="anthropic",
            api_key="sk-ant-...",
            fallback_keys=["sk-ant-alt1-...", "sk-ant-alt2-..."],
        )

        # Use with retry config:
        config = RetryConfig(on_auth_required=auth.refresh)
    """

    def __init__(
        self,
        provider: str = "",
        api_key: str = "",
        fallback_keys: list[str] | None = None,
        max_consecutive_auth_failures: int = 5,
    ) -> None:
        """Initialize the auth manager.

        Args:
            provider: Provider name (used for logging/diagnostics).
            api_key: The primary API key.
            fallback_keys: Optional list of backup keys to rotate through
                when the primary key fails authentication.
            max_consecutive_auth_failures: Threshold above which the
                credential source is considered degraded.
        """
        self.provider = provider
        self._primary_key: str = api_key
        self._fallback_keys: list[str] = list(fallback_keys or [])
        self._current_fallback_index: int = -1  # -1 = using primary
        # Lock guarding credential swaps during async refreshes.
        self._lock = asyncio.Lock()
        self._health = AuthHealth()
        self._max_consecutive = max_consecutive_auth_failures

    @property
    def api_key(self) -> str:
        """Return the currently active API key."""
        # A negative index means the primary key is in use.
        if self._current_fallback_index < 0:
            return self._primary_key
        idx = self._current_fallback_index
        # Guard against an out-of-range index; fall back to the primary.
        if idx < len(self._fallback_keys):
            return self._fallback_keys[idx]
        return self._primary_key

    def set_api_key(self, key: str) -> None:
        """Set a new primary API key (resets to primary)."""
        self._primary_key = key
        self._current_fallback_index = -1

    def add_fallback_key(self, key: str) -> None:
        """Add a fallback key to the rotation."""
        if key not in self._fallback_keys:
            self._fallback_keys.append(key)

    def record_auth_failure(self) -> None:
        """Record a 401/403 auth failure and rotate to next key."""
        self._health.last_auth_error = time.time()
        self._health.consecutive_auth_failures += 1
        self._health.total_auth_failures += 1
        self._rotate_to_next_key()

    def record_auth_success(self) -> None:
        """Record a successful auth after previous failures."""
        self._health.consecutive_auth_failures = 0

    def _rotate_to_next_key(self) -> None:
        """Advance to the next key in the rotation."""
        # Total credentials = the primary plus every registered fallback.
        total_keys = 1 + len(self._fallback_keys)
        # Wrap around so rotation cycles back to the primary after the last.
        self._current_fallback_index = (self._current_fallback_index + 1) % total_keys
        logger.info(
            "Auth failure for %s, rotating to key %d/%d",
            self.provider,
            self._current_fallback_index + 2,
            total_keys,
        )

    async def refresh(self) -> None:
        """Async callback for ``RetryConfig.on_auth_required``.

        Records the auth failure, rotates credentials, and returns
        so the retry loop can retry with the new key.
        """
        async with self._lock:
            self.record_auth_failure()
            self._health.last_refresh_time = time.time()
            self._health.refresh_count += 1
            logger.info(
                "Auth refreshed for %s, using key slot %d",
                self.provider,
                self._current_fallback_index + 1,
            )

    def is_auth_degraded(self) -> bool:
        """Return ``True`` if consecutive auth failures exceed threshold."""
        return self._health.consecutive_auth_failures >= self._max_consecutive

    def get_health(self) -> dict[str, Any]:
        """Return auth health diagnostics."""
        return {
            "provider": self.provider,
            "has_primary": bool(self._primary_key),
            "fallback_keys": len(self._fallback_keys),
            "active_key_slot": self._current_fallback_index + 1,
            "consecutive_auth_failures": self._health.consecutive_auth_failures,
            "total_auth_failures": self._health.total_auth_failures,
            "refresh_count": self._health.refresh_count,
            "is_degraded": self.is_auth_degraded(),
            "last_auth_error_age": (
                time.time() - self._health.last_auth_error
                if self._health.last_auth_error
                else None
            ),
        }
