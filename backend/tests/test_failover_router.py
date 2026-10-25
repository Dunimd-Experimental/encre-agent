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

"""Tests for the failover and routing backend layers.

Covers :class:`BackendHealth` failure accounting, the :class:`FailoverBackend`
primary/fallback chain, :class:`Route` category matching, the
:class:`RouterBackend` wrapper (construction, capability delegation, cost
tracking, connection-health monitoring and auth exposure), and the
:class:`CostTracker` and :class:`TaskCategory` supporting types.
"""

import asyncio

import httpx
import pytest
from encre.backend import create_backend
from encre.backends.auth import AuthManager
from encre.backends.base import BaseBackend
from encre.backends.failover import BackendHealth, FailoverBackend
from encre.backends.router import CostTracker, Route, RouterBackend, TaskCategory
from encre.utils.types import BackendFinish


# ===========================================================================
# MockBackend: minimal concrete backend for integration tests
# ===========================================================================

class MockBackend(BaseBackend):
    """Minimal backend for testing RouterBackend integration.

    Optionally raises an exception on chat() for testing failure paths.
    """
    def __init__(self, name: str = "mock", fail_on_call: bool = False,
                 fail_error: Exception | None = None) -> None:
        """Initialize the mock backend with optional failure injection."""
        self.model = name
        self._fail_on_call = fail_on_call
        self._fail_error = fail_error or httpx.ConnectError("mock connection error")

    async def chat(
        self,
        messages: list | None = None,
        tools: list | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = True,
        enable_caching: bool = False,
    ):
        """Yield a single completion or raise the injected failure."""
        if self._fail_on_call:
            raise self._fail_error
        yield BackendFinish(reason="stop", usage={})

    def supports_tool_calling(self) -> bool:
        """Report tool-calling capability to the router."""
        return True

    def context_window_size(self) -> int:
        """Return the modeled context window size."""
        return 128000

    def supports_thinking(self) -> bool:
        """Report whether extended reasoning is supported."""
        return False

    def supports_prompt_caching(self) -> bool:
        """Report whether prompt caching is supported."""
        return False

    def count_tokens(self, text: str) -> int:
        """Approximate token count by splitting on whitespace."""
        return len(text.split())

    async def aclose(self) -> None:
        """No-op teardown for the mock backend."""
        pass

# ===========================================================================
# BackendHealth
# ===========================================================================

class TestBackendHealth:
    """Validates the BackendHealth failure-tracking state machine.

    Exercises initialisation, failure recording, success-based reset,
    threshold-based cutover, and manual recovery. The health monitor is the
    signal the failover and router components gate traffic on.
    """

    def test_verify_initial_health_is_healthy(self):
        """A new BackendHealth starts healthy with all counters at zero."""
        bh = BackendHealth(name="openai")
        # A fresh backend must accept traffic immediately.
        assert bh.healthy is True
        # Starting at zero avoids a spurious cutover on the first failure.
        assert bh.consecutive_failures == 0
        assert bh.total_failures == 0
        assert bh.total_requests == 0

    def test_verify_record_failure_updates_counters(self):
        """record_failure increments the failure and request counters and stores last_error."""
        bh = BackendHealth(name="openai")
        bh.record_failure("timeout")
        # The streak counter drives the degradation decision.
        assert bh.consecutive_failures == 1
        # The lifetime total only ever grows.
        assert bh.total_failures == 1
        assert bh.total_requests == 1
        assert bh.last_error == "timeout"

    def test_verify_record_success_resets_consecutive_failures(self):
        """record_success breaks the failure streak without masking lifetime totals."""
        bh = BackendHealth(name="openai")
        bh.record_failure("err1")
        bh.record_failure("err2")
        bh.record_success()
        # Consecutive failures must reset after a success.
        assert bh.consecutive_failures == 0
        # Total requests counts successes as well as failures.
        assert bh.total_requests == 3
        assert bh.healthy is True

    def test_verify_consecutive_failures_trigger_unhealthy(self):
        """Crossing the failure threshold flips healthy to False."""
        bh = BackendHealth(name="openai")
        for i in range(3):
            bh.record_failure(f"error {i}")
        assert bh.healthy is False

    def test_verify_manual_recovery_resets_state(self):
        """Manual recovery via direct field writes restores healthy status."""
        import time
        bh = BackendHealth(name="openai")
        for _ in range(3):
            bh.record_failure("timeout")
        # Confirm unhealthy before manual intervention.
        assert bh.healthy is False
        # Rewind last_checked past the retry grace period, then clear the streak.
        bh.last_checked = time.time() - 400
        bh.healthy = True
        bh.consecutive_failures = 0
        assert bh.healthy is True


# ===========================================================================
# FailoverBackend
# ===========================================================================

class TestFailoverBackend:
    """Validates the FailoverBackend primary/fallback chain.

    Covers construction, active-backend selection, health reporting, capability
    delegation, multi-backend chains, empty-input rejection, token counting, and
    async teardown, so the wrapper always satisfies the BaseBackend contract.
    """

    def test_verify_failover_backend_creation(self):
        """FailoverBackend wraps a primary/fallback pair and stays a BaseBackend."""
        be1 = create_backend("openai", api_key="sk-fake")
        be2 = create_backend("anthropic", api_key="sk-ant-fake")
        fb = FailoverBackend(backends=[("primary", be1), ("fallback", be2)])
        assert fb is not None
        assert isinstance(fb, BaseBackend)

    def test_verify_active_backend_starts_as_primary(self):
        """The first backend in the chain is elected active on construction."""
        be1 = create_backend("openai", api_key="sk-fake")
        be2 = create_backend("anthropic", api_key="sk-ant-fake")
        fb = FailoverBackend(backends=[("openai", be1), ("anthropic", be2)])
        assert fb.active_backend_name == "openai"

    def test_verify_get_health_reports_all_backends(self):
        """get_health reports every registered backend, with the primary healthy."""
        be1 = create_backend("openai", api_key="sk-fake")
        be2 = create_backend("anthropic", api_key="sk-ant-fake")
        fb = FailoverBackend(backends=[("openai", be1), ("anthropic", be2)])
        health = fb.get_health()
        assert "openai" in health
        assert "anthropic" in health
        # A fresh chain has no failures, so the primary must report healthy.
        assert health["openai"]["healthy"] is True

    def test_verify_context_window_size_delegates_correctly(self):
        """context_window_size delegates to a backend and returns a positive size."""
        be1 = create_backend("openai", api_key="sk-fake")
        be2 = create_backend("anthropic", api_key="sk-ant-fake")
        fb = FailoverBackend(backends=[("p", be1), ("f", be2)])
        assert fb.context_window_size() > 0

    def test_verify_supports_tool_calling_delegation(self):
        """supports_tool_calling delegates and returns a strict bool."""
        be1 = create_backend("openai", api_key="sk-fake")
        be2 = create_backend("anthropic", api_key="sk-ant-fake")
        fb = FailoverBackend(backends=[("p", be1), ("f", be2)])
        assert isinstance(fb.supports_tool_calling(), bool)

    def test_verify_supports_thinking_delegation(self):
        """supports_thinking delegates and returns a strict bool."""
        be1 = create_backend("openai", api_key="sk-fake")
        be2 = create_backend("anthropic", api_key="sk-ant-fake")
        fb = FailoverBackend(backends=[("p", be1), ("f", be2)])
        assert isinstance(fb.supports_thinking(), bool)

    def test_verify_supports_prompt_caching_delegation(self):
        """supports_prompt_caching delegates and returns a strict bool."""
        be1 = create_backend("openai", api_key="sk-fake")
        be2 = create_backend("anthropic", api_key="sk-ant-fake")
        fb = FailoverBackend(backends=[("p", be1), ("f", be2)])
        assert isinstance(fb.supports_prompt_caching(), bool)

    def test_verify_three_backend_chain(self):
        """A three-backend chain elects the first backend and reports all three."""
        be1 = create_backend("openai", api_key="sk-fake")
        be2 = create_backend("anthropic", api_key="sk-ant-fake")
        be3 = create_backend("deepseek", api_key="sk-fake")
        fb = FailoverBackend(backends=[("a", be1), ("b", be2), ("c", be3)])
        assert fb.active_backend_name == "a"
        assert len(fb.get_health()) == 3

    def test_verify_empty_backends_raises(self):
        """An empty backend list is rejected with ValueError."""
        with pytest.raises(ValueError, match="At least one backend"):
            FailoverBackend(backends=[])

    def test_verify_count_tokens_delegates(self):
        """count_tokens delegates to the active backend and returns an int."""
        be1 = create_backend("openai", api_key="sk-fake")
        fb = FailoverBackend(backends=[("p", be1)])
        assert isinstance(fb.count_tokens("hello"), int)

    def test_verify_aclose_teardown(self):
        """aclose drains the backends without raising."""
        be1 = create_backend("openai", api_key="sk-fake")
        fb = FailoverBackend(backends=[("p", be1)])
        asyncio.run(fb.aclose())


# ===========================================================================
# Route
# ===========================================================================

class TestRoute:
    """Validates the Route category-matching behaviour.

    Covers construction, coding-prompt matches, research-prompt matches, and
    no-match cases, producing the confidence scores the router ranks routes by.
    """

    def test_verify_route_creation(self):
        """Route stores its category and defaults priority to zero."""
        backend = create_backend("openai", api_key="sk-fake")
        route = Route(category=TaskCategory.CODING, backend=backend)
        assert route.category == TaskCategory.CODING
        assert route.priority == 0

    def test_verify_route_matches_coding_prompt(self):
        """A CODING route gives code-generation prompts positive confidence."""
        backend = create_backend("openai", api_key="sk-fake")
        route = Route(category=TaskCategory.CODING, backend=backend)
        confidence = route.matches("write a python function to sort a list")
        assert confidence > 0.0

    def test_verify_route_matches_research_prompt(self):
        """A RESEARCH route gives research prompts positive confidence."""
        backend = create_backend("openai", api_key="sk-fake")
        route = Route(category=TaskCategory.RESEARCH, backend=backend)
        confidence = route.matches("research the best database for microservices")
        assert confidence > 0.0

    def test_verify_route_no_match_for_unrelated_prompt(self):
        """A CODING route scores unrelated chit-chat as zero."""
        backend = create_backend("openai", api_key="sk-fake")
        route = Route(category=TaskCategory.CODING, backend=backend)
        confidence = route.matches("hello how are you")
        assert confidence == 0.0


# ===========================================================================
# RouterBackend
# ===========================================================================

class TestRouterBackend:
    """Validates the RouterBackend construction and basic contracts.

    Covers creation, last-route defaults, capability delegation, the cost
    tracking toggle, and the stats structure, so the wrapper satisfies the
    BaseBackend contract with or without category routes or cost observation.
    """

    def test_verify_router_backend_creation(self):
        """RouterBackend is instantiable and satisfies the BaseBackend interface."""
        routes = {TaskCategory.CODING: create_backend("openai", api_key="sk-fake")}
        default = create_backend("openai", api_key="sk-fake")
        rb = RouterBackend(routes=routes, default=default)
        assert isinstance(rb, BaseBackend)

    def test_verify_last_route_defaults_to_default_label(self):
        """last_route is 'default' before any chat call."""
        routes = {TaskCategory.CODING: create_backend("openai", api_key="sk-fake")}
        default = create_backend("openai", api_key="sk-fake")
        rb = RouterBackend(routes=routes, default=default)
        assert rb.last_route == "default"

    def test_verify_context_window_size_delegation(self):
        """context_window_size delegates to the default backend and stays positive."""
        default = create_backend("openai", api_key="sk-fake")
        rb = RouterBackend(routes={}, default=default)
        assert rb.context_window_size() > 0

    def test_verify_supports_tool_calling_delegation(self):
        """supports_tool_calling returns a bool from the default backend."""
        default = create_backend("openai", api_key="sk-fake")
        rb = RouterBackend(routes={}, default=default)
        assert isinstance(rb.supports_tool_calling(), bool)

    def test_verify_supports_thinking_delegation(self):
        """supports_thinking returns a bool from the default backend."""
        default = create_backend("openai", api_key="sk-fake")
        rb = RouterBackend(routes={}, default=default)
        assert isinstance(rb.supports_thinking(), bool)

    def test_verify_cost_tracker_enabled_flag(self):
        """track_costs=True attaches a CostTracker instance."""
        default = create_backend("openai", api_key="sk-fake")
        rb = RouterBackend(routes={}, default=default, track_costs=True)
        assert rb.cost_tracker is not None

    def test_verify_cost_tracker_disabled_flag(self):
        """track_costs=False leaves cost_tracker unset to avoid allocation overhead."""
        default = create_backend("openai", api_key="sk-fake")
        rb = RouterBackend(routes={}, default=default, track_costs=False)
        assert rb.cost_tracker is None

    def test_verify_route_stats_contains_all_keys(self):
        """route_stats contains both the default entry and each category route."""
        routes = {TaskCategory.CODING: create_backend("openai", api_key="sk-fake")}
        default = create_backend("openai", api_key="sk-fake")
        rb = RouterBackend(routes=routes, default=default)
        stats = rb.route_stats
        assert isinstance(stats, dict)
        assert "default" in stats
        assert TaskCategory.CODING in stats


# ===========================================================================
# RouterBackend: connection monitor and auth integration
# ===========================================================================

class TestRouterBackendIntegration:
    """Validates connection-health monitoring and auth exposure.

    Drives :class:`RouterBackend` against failing :class:`MockBackend` instances
    plus an :class:`AuthManager`, checking that failures are recorded, health is
    surfaced, degraded routes fall back to the default, and the original
    selection is preserved when every route is degraded.
    """

    @pytest.mark.asyncio
    async def test_verify_get_health_exposes_connection_and_auth(self):
        """get_health exposes connection, auth, and last_route sections."""
        coding = MockBackend(name="coding")
        default = MockBackend(name="default")
        rb = RouterBackend(routes={TaskCategory.CODING: coding}, default=default)
        health = rb.get_health()
        assert "connection" in health
        assert "auth" in health
        assert health["auth"] is None
        assert isinstance(health["connection"], dict)
        assert "last_route" in health

    @pytest.mark.asyncio
    async def test_verify_auth_manager_visible_in_health(self):
        """An attached AuthManager is reported with its provider and key state."""
        auth = AuthManager(provider="test", api_key="sk-test")
        coding = MockBackend(name="coding")
        default = MockBackend(name="default")
        rb = RouterBackend(
            routes={TaskCategory.CODING: coding}, default=default,
            auth_manager=auth,
        )
        health = rb.get_health()
        assert health["auth"] is not None
        assert health["auth"]["provider"] == "test"
        assert health["auth"]["has_primary"] is True

    @pytest.mark.asyncio
    async def test_verify_connection_failure_is_recorded(self):
        """A chat-time connection error increments the route failure counters."""
        coding = MockBackend(name="coding", fail_on_call=True)
        default = MockBackend(name="default")
        rb = RouterBackend(routes={TaskCategory.CODING: coding}, default=default)

        with pytest.raises(httpx.ConnectError):
            async for _ in rb.chat(
                messages=[{"role": "user", "content": "write a python function"}]
            ):
                pass

        # The coding route must have picked up the failure.
        rh = rb._connection_monitor.get_health(TaskCategory.CODING)
        assert rh is not None
        assert rh.consecutive_failures >= 1
        assert rh.total_failures >= 1

    @pytest.mark.asyncio
    async def test_verify_successful_call_resets_consecutive_failures(self):
        """A successful call clears the consecutive-failure streak."""
        coding = MockBackend(name="coding")
        default = MockBackend(name="default")
        rb = RouterBackend(routes={TaskCategory.CODING: coding}, default=default)

        # Pre-record a failure so we can verify success clears it.
        rb._connection_monitor.record_failure(TaskCategory.CODING, "previous error")
        assert rb._connection_monitor.get_health(TaskCategory.CODING).consecutive_failures == 1

        # Successful call.
        async for _ in rb.chat(
            messages=[{"role": "user", "content": "write a python function"}]
        ):
            pass

        rh = rb._connection_monitor.get_health(TaskCategory.CODING)
        assert rh is not None
        assert rh.consecutive_failures == 0
        assert rh.total_requests >= 1

    @pytest.mark.asyncio
    async def test_verify_degraded_route_falls_back_to_default(self):
        """A degraded CODING route causes fallback to the default backend."""
        coding = MockBackend(name="coding")
        default = MockBackend(name="default")
        rb = RouterBackend(routes={TaskCategory.CODING: coding}, default=default)

        # Push the coding route past the degradation threshold.
        for _ in range(3):
            rb._connection_monitor.record_failure(TaskCategory.CODING, "timeout")
        assert rb._connection_monitor.is_degraded(TaskCategory.CODING)

        # Coding prompt, but the degraded route should fall back to default.
        async for _ in rb.chat(
            messages=[{"role": "user", "content": "write a python function"}]
        ):
            pass

        assert rb.last_route == "default"

    @pytest.mark.asyncio
    async def test_verify_all_routes_degraded_preserves_original_selection(self):
        """When every route is degraded the router keeps its original selection."""
        coding = MockBackend(name="coding", fail_on_call=True)
        default = MockBackend(name="default", fail_on_call=True)
        rb = RouterBackend(routes={TaskCategory.CODING: coding}, default=default)

        # Degrade both routes.
        for _ in range(3):
            rb._connection_monitor.record_failure(TaskCategory.CODING, "timeout")
            rb._connection_monitor.record_failure("default", "timeout")
        assert rb._connection_monitor.is_degraded(TaskCategory.CODING)
        assert rb._connection_monitor.is_degraded("default")

        # All degraded: use the original selection (coding) despite degradation.
        with pytest.raises(httpx.ConnectError):
            async for _ in rb.chat(
                messages=[{"role": "user", "content": "write a python function"}]
            ):
                pass

        assert rb.last_route == TaskCategory.CODING

    @pytest.mark.asyncio
    async def test_verify_non_connection_error_is_still_recorded(self):
        """Non-connection errors such as ValueError still count as failures."""
        coding = MockBackend(
            name="coding", fail_on_call=True,
            fail_error=ValueError("bad request"),
        )
        default = MockBackend(name="default")
        rb = RouterBackend(routes={TaskCategory.CODING: coding}, default=default)

        with pytest.raises(ValueError, match="bad request"):
            async for _ in rb.chat(
                messages=[{"role": "user", "content": "write a python function"}]
            ):
                pass

        rh = rb._connection_monitor.get_health(TaskCategory.CODING)
        assert rh is not None
        assert rh.consecutive_failures >= 1


# ===========================================================================
# CostTracker
# ===========================================================================

class TestCostTracker:
    """Validates CostTracker accumulation and serialisation.

    Covers initial state, single-model recording, multi-model aggregation,
    cache-aware recording, and dict export so cost accounting stays accurate
    across heterogeneous backend usage.
    """

    def test_verify_cost_tracker_initial_state(self):
        """A fresh CostTracker starts at zero for every counter."""
        ct = CostTracker()
        assert ct.total_cost_usd == 0.0
        assert ct.total_input_tokens == 0
        assert ct.total_output_tokens == 0
        assert ct.cache_hit_tokens == 0

    def test_verify_record_usage_accumulates_correctly(self):
        """record() accumulates tokens, cost, and the per-model request tally."""
        ct = CostTracker()
        ct.record(model="gpt-5.6", input_tokens=100, output_tokens=50, cost_usd=0.0005)
        assert ct.total_input_tokens == 100
        assert ct.total_output_tokens == 50
        assert ct.total_cost_usd == 0.0005
        assert ct.requests_by_model["gpt-5.6"] == 1

    def test_verify_multiple_models_aggregate_correctly(self):
        """Recording across models sums the totals while keeping per-model maps."""
        ct = CostTracker()
        ct.record(model="gpt-5.6", input_tokens=100, output_tokens=50, cost_usd=0.0005)
        ct.record(model="claude-sonnet-5", input_tokens=200, output_tokens=100, cost_usd=0.003)  # noqa: E501
        assert ct.total_input_tokens == 300
        assert ct.total_output_tokens == 150
        assert ct.total_cost_usd == 0.0035
        assert len(ct.cost_by_model) == 2
        assert ct.requests_by_model["gpt-5.6"] == 1
        assert ct.requests_by_model["claude-sonnet-5"] == 1

    def test_verify_cache_hit_tokens_and_savings_are_recorded(self):
        """Cache-hit tokens and savings are tracked separately from raw totals."""
        ct = CostTracker()
        ct.record(model="claude-sonnet-5", input_tokens=1000, output_tokens=50,
                  cost_usd=0.01, cache_hit=500, cache_savings=0.005)
        assert ct.cache_hit_tokens == 500
        assert ct.cache_savings_usd == 0.005

    def test_verify_to_dict_exports_accumulated_state(self):
        """to_dict exports the top-level totals plus the nested per-model maps."""
        ct = CostTracker()
        ct.record(model="gpt-5.6", input_tokens=100, output_tokens=50, cost_usd=0.0005)
        d = ct.to_dict()
        assert d["total_input_tokens"] == 100
        assert d["total_output_tokens"] == 50
        assert "cost_by_model" in d
        assert "requests_by_model" in d


# ===========================================================================
# TaskCategory
# ===========================================================================

class TestTaskCategory:
    """Validates the TaskCategory string constants.

    Asserts every category constant maps to its canonical string, since the
    router compares these strings when selecting routes and reporting stats.
    """

    def test_verify_all_category_constants_are_defined(self):
        """Every TaskCategory constant matches its canonical string value."""
        assert TaskCategory.CLASSIFICATION == "classification"
        assert TaskCategory.REASONING == "reasoning"
        assert TaskCategory.CODING == "coding"
        assert TaskCategory.RESEARCH == "research"
        assert TaskCategory.WRITING == "writing"
        assert TaskCategory.PLANNING == "planning"
        assert TaskCategory.EXECUTION == "execution"
        assert TaskCategory.SUMMARIZATION == "summarization"
