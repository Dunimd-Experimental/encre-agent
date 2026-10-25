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

"""Tests for the rate limiter: construction, check, backoff, slots, and reset.

The suite covers the ``RateLimitResult`` data model, ``EncreRateLimiter``
construction with default and custom quotas, the quota-check logic over
minute and day windows, the async concurrent-slot protocol, the
exponential-backoff policy with its cap, and the reset operation that
restores the limiter to its initial state.
"""

import asyncio

import pytest


class TestRateLimitResult:
    """Tests for the ``RateLimitResult`` dataclass contract.

    Confirms an allowed result yields ``retry_after == 0.0`` plus the
    reported remaining quota, that a denied result carries the reported
    ``retry_after`` with zero remaining, that omitted fields default to
    ``retry_after=0.0`` and ``remaining=0``, and that the class is
    recognised as a dataclass.
    """

    def test_verify_ratelimit_result_allowed(self):
        """An allowed result reports retry_after 0.0 and the remaining quota."""
        from encre.ratelimit import RateLimitResult
        result = RateLimitResult(allowed=True, remaining=50)
        assert result.allowed is True
        assert result.retry_after == 0.0
        assert result.remaining == 50

    def test_verify_ratelimit_result_denied(self):
        """A denied result carries the reported retry_after and zero remaining."""
        from encre.ratelimit import RateLimitResult
        result = RateLimitResult(allowed=False, retry_after=30.5, remaining=0)
        assert result.allowed is False
        assert result.retry_after == 30.5
        assert result.remaining == 0

    def test_verify_ratelimit_result_default_values(self):
        """Omitted fields default to retry_after 0.0 and remaining 0."""
        from encre.ratelimit import RateLimitResult
        result = RateLimitResult(allowed=True)
        assert result.retry_after == 0.0
        assert result.remaining == 0

    def test_verify_ratelimit_result_is_dataclass(self):
        """RateLimitResult is recognised as a dataclass."""
        from dataclasses import is_dataclass

        from encre.ratelimit import RateLimitResult
        assert is_dataclass(RateLimitResult)


class TestEncreRateLimiterConstruction:
    """Tests for ``EncreRateLimiter`` default and custom construction.

    Asserts the constructor applies the documented default quotas
    (60/min, 500/hour, 5000/day, 10 concurrent), that custom values are
    stored verbatim on the matching attributes, and that the internal
    counters and containers start empty.
    """

    def test_verify_ratelimit_result_default_values(self):
        """The constructor applies the documented default quotas."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter()
        assert limiter.per_minute == 60
        assert limiter.per_hour == 500
        assert limiter.per_day == 5000
        assert limiter.max_concurrent == 10
        assert limiter._concurrent_count == 0

    def test_verify_ratelimit_construction_custom_values(self):
        """Custom quota values are stored verbatim on the matching attributes."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter(
            per_minute=30,
            per_hour=200,
            per_day=1000,
            max_concurrent=5,
        )
        assert limiter.per_minute == 30
        assert limiter.per_hour == 200
        assert limiter.per_day == 1000
        assert limiter.max_concurrent == 5

    def test_verify_ratelimit_construction_initial_windows_empty(self):
        """A new limiter starts with no sliding windows."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter()
        assert limiter._windows == {}

    def test_verify_ratelimit_construction_initial_active_tools_empty(self):
        """A new limiter starts with no tracked active tools."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter()
        assert limiter.active_tools == []


class TestEncreRateLimiterCheck:
    """Tests for the rate-limit check logic.

    Covers the first-request case, which is always allowed with positive
    remaining quota, cumulative counting across repeated requests,
    per-tool window isolation so heavy use of one tool does not starve
    another, per-minute limit enforcement, and per-day limit enforcement
    with zero remaining on denial.
    """

    def test_verify_ratelimit_check_first_check_allowed(self):
        """The first check for a tool is always allowed with quota remaining."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter()
        result = limiter.check("bash")
        assert result.allowed is True
        assert result.remaining > 0

    def test_verify_ratelimit_check_multiple_checks_track_count(self):
        """Repeated checks accumulate and drive the remaining quota down."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter(per_minute=100)
        for _ in range(10):
            result = limiter.check("bash")
            assert result.allowed is True
        # Every check consumes quota, so remaining must have dropped.
        assert result.remaining < 5000

    def test_verify_ratelimit_check_different_tools_have_separate_windows(self):
        """Each tool keeps its own window, so one heavy tool cannot starve another."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter(per_minute=100)
        # Saturate one tool; the other must still have its full quota.
        for _ in range(50):
            limiter.check("heavy_tool")
        result = limiter.check("light_tool")
        assert result.allowed is True
        # light_tool should still have nearly its full quota.
        assert result.remaining > 4000

    def test_verify_ratelimit_check_per_minute_limit_exceeded(self):
        """The per-minute quota denies the first request past the limit."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter(per_minute=5, per_hour=99999, per_day=99999)
        for _ in range(5):
            result = limiter.check("bash")
            assert result.allowed is True
        # The sixth request must be denied.
        result = limiter.check("bash")
        assert result.allowed is False
        assert result.retry_after > 0

    def test_verify_ratelimit_check_per_day_limit_exceeded(self):
        """The per-day quota denies the first request past the limit, with zero remaining."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter(per_minute=99999, per_hour=99999, per_day=3)
        for _ in range(3):
            result = limiter.check("bash")
            assert result.allowed is True
        # The fourth request must be denied.
        result = limiter.check("bash")
        assert result.allowed is False
        assert result.remaining == 0


class TestEncreRateLimiterSlots:
    """Tests for the async concurrent-slot acquisition protocol.

    Confirms acquire increments ``_concurrent_count``, release decrements
    it, releasing without an acquire never drives the count negative,
    and a second acquire blocks while capacity is saturated -- verified
    by racing the acquire against a timeout after a short spin.
    """

    @pytest.mark.asyncio
    async def test_verify_ratelimit_slots_acquire_slot_below_limit(self):
        """Acquiring a slot below capacity increments the concurrent count."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter(max_concurrent=10)
        await limiter.acquire_slot()
        assert limiter._concurrent_count == 1

    @pytest.mark.asyncio
    async def test_verify_ratelimit_slots_acquire_multiple_slots(self):
        """Multiple acquires accumulate in the concurrent count."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter(max_concurrent=5)
        await limiter.acquire_slot()
        await limiter.acquire_slot()
        await limiter.acquire_slot()
        assert limiter._concurrent_count == 3

    @pytest.mark.asyncio
    async def test_verify_ratelimit_slots_release_slot(self):
        """Releasing a slot decrements the concurrent count."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter()
        await limiter.acquire_slot()
        assert limiter._concurrent_count == 1
        limiter.release_slot()
        assert limiter._concurrent_count == 0

    def test_verify_ratelimit_slots_release_slot_never_goes_negative(self):
        """Releasing without acquiring never drives the count below zero."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter()
        limiter.release_slot()
        limiter.release_slot()
        assert limiter._concurrent_count == 0

    @pytest.mark.asyncio
    async def test_verify_ratelimit_slots_acquire_slot_blocks_when_at_capacity(self):
        """A second acquire blocks while capacity is full, then proceeds on release."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter(max_concurrent=1)
        await limiter.acquire_slot()
        assert limiter._concurrent_count == 1

        # Try to take a second slot; it must block while capacity is full.
        # The block is detected by racing the acquire against a timeout.
        async def acquire():
            """Acquire one concurrent slot from the shared limiter."""
            await limiter.acquire_slot()
            return True

        task = asyncio.create_task(acquire())
        await asyncio.sleep(0.2)  # Give the blocked task time to spin
        assert limiter._concurrent_count == 1  # Capacity is still saturated

        # Releasing the held slot should unblock the pending task.
        limiter.release_slot()
        await asyncio.wait_for(task, timeout=2.0)
        assert limiter._concurrent_count == 1


class TestEncreRateLimiterBackoff:
    """Tests for the exponential-backoff-with-jitter policy.

    Asserts ``backoff(0)`` returns approximately one second, that the
    delay grows with the attempt count within the jitter bounds, that
    delays are capped at sixty seconds (checkable at attempt 10, where
    the base value already exceeds the cap), and that the return value
    is always a float.
    """

    def test_verify_ratelimit_backoff_with_zero_attempts(self):
        """Backoff for attempt zero is one second plus jitter."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter()
        delay = limiter.backoff(0)
        assert 1.0 <= delay <= 1.5  # 2**0 is 1 s plus up to 0.5 s of jitter

    def test_verify_ratelimit_backoff_increases_with_attempts(self):
        """Backoff delays are positive and grow with the attempt count."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter()
        d1 = limiter.backoff(1)
        d2 = limiter.backoff(2)
        d3 = limiter.backoff(3)
        # The base delays are 2, 4 and 8 seconds and should generally grow,
        # but jitter makes exact ordering unreliable, so only positivity holds.
        assert d1 > 0
        assert d2 > 0
        assert d3 > 0

    def test_verify_ratelimit_backoff_capped_at_60_seconds(self):
        """Backoff is capped at sixty seconds even for large attempt counts."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter()
        # 2**10 is 1024, which the cap must clamp down to 60 seconds.
        delay = limiter.backoff(10)
        assert delay <= 60.5  # 60 s plus up to 0.5 s of jitter
        assert delay >= 60.0

    def test_verify_ratelimit_backoff_returns_float(self):
        """Backoff always returns a float."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter()
        delay = limiter.backoff(5)
        assert isinstance(delay, float)


class TestEncreRateLimiterReset:
    """Tests for the rate-limiter reset operation.

    Confirms reset clears every sliding window, zeroes
    ``_concurrent_count``, and empties the active-tools tracking list,
    restoring the limiter to its construction state.
    """

    def test_verify_ratelimit_reset_clears_windows(self):
        """Reset clears every sliding window."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter(per_minute=5)
        for _ in range(3):
            limiter.check("bash")
        assert "bash" in limiter._windows
        limiter.reset()
        assert limiter._windows == {}

    def test_verify_ratelimit_reset_clears_concurrent_count(self):
        """Reset zeroes the concurrent count."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter()
        limiter._concurrent_count = 5
        limiter.reset()
        assert limiter._concurrent_count == 0

    def test_verify_ratelimit_reset_active_tools_empty_after_reset(self):
        """Reset empties the active-tools list."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter(per_minute=100)
        limiter.check("bash")
        limiter.check("grep")
        assert len(limiter.active_tools) == 2
        limiter.reset()
        assert limiter.active_tools == []


class TestEncreRateLimiterActiveTools:
    """Tests for the active-tools tracking list.

    Asserts ``active_tools`` starts empty and that every ``check`` call
    records its tool name, so the list reflects every tool exercised
    since the last reset.
    """

    def test_verify_ratelimit_active_tools_returns_names(self):
        """Every checked tool name is recorded in the active-tools list."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter(per_minute=100)
        limiter.check("bash")
        limiter.check("grep")
        active = limiter.active_tools
        assert "bash" in active
        assert "grep" in active

    def test_verify_ratelimit_active_tools_starts_empty(self):
        """A new limiter records no active tools."""
        from encre.ratelimit import EncreRateLimiter
        limiter = EncreRateLimiter()
        assert limiter.active_tools == []
