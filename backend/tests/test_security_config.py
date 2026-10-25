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

"""Tests for the security-related building blocks and configuration surface.

Covers the SSRF guard (URL validation, private-IP and metadata blocking,
hostname classification, safe-hostname extraction and DNS-cache clearing),
the per-key sliding-window rate limiter, the EncreConfig defaults and custom
construction, and the sandbox config/result data types.

Also exercises the telemetry record types and accumulator (tool calls, turns,
retries, summary, flush, reset, disabled mode) and the logging setup helpers,
so that a regression in any of these safety primitives is caught in one suite.
"""

import os
import tempfile

from encre.config import EncreConfig
from encre.logging_config import get_logger, setup_logging

# Redirect telemetry data to a temporary directory so the test run never
# pollutes the real production telemetry directory.
_test_telemetry_dir = tempfile.mkdtemp(prefix="encre_test_telemetry_")
os.environ["ENCRE_DATA_DIR"] = _test_telemetry_dir
from encre.ratelimit import EncreRateLimiter, RateLimitResult
from encre.ssrf import EncreSSRFGuard
from encre.telemetry import EncreTelemetry, RetryRecord, ToolCallRecord, TurnRecord


class TestSSRFGuard:
    """Validate the SSRF guard's URL validation and hostname-blocking logic.

    Exercises EncreSSRFGuard across ten scenarios: public URL acceptance,
    private-IP blocking, cloud-metadata blocking, non-HTTP rejection,
    invalid-URL rejection, private and public hostname classification,
    safe-hostname extraction, blocked-hostname extraction, and DNS-cache
    clearing. Together these cover both IP-based and hostname-based SSRF
    attack vectors.
    """

    def setup_method(self):
        """Create a fresh EncreSSRFGuard instance before each test method.

        Returns:
            A new EncreSSRFGuard instance assigned to self.guard.
        """
        self.guard = EncreSSRFGuard()

    def test_verify_validate_url_allows_public(self):
        """Validate that validate_url accepts a public HTTPS URL.

        Public HTTPS URLs are the normal case the guard must allow through,
        so the call with "https://example.com/resource" must return True.
        """
        result = self.guard.validate_url("https://example.com/resource")
        assert result is True

    def test_verify_validate_url_blocks_private_ip(self):
        """Validate that validate_url rejects URLs targeting private IP ranges.

        Localhost addresses are SSRF attack targets and must be blocked, so
        the call with "http://127.0.0.1/admin" must return False.
        """
        result = self.guard.validate_url("http://127.0.0.1/admin")
        assert result is False

    def test_verify_validate_url_blocks_metadata(self):
        """Validate that validate_url rejects cloud metadata endpoints.

        Metadata-service access is a well-known SSRF exploitation path, so
        the AWS metadata IP 169.254.169.254 must be blocked.
        """
        result = self.guard.validate_url("http://169.254.169.254/metadata")
        assert result is False

    def test_verify_validate_url_rejects_non_http(self):
        """Validate that validate_url rejects non-HTTP schemes such as FTP.

        Only HTTP and HTTPS are permitted by the SSRF guard policy, so an
        FTP URL must be rejected.
        """
        assert self.guard.validate_url("ftp://example.com/file") is False

    def test_verify_validate_url_rejects_invalid(self):
        """Validate that validate_url rejects syntactically invalid URLs.

        The parser must reject strings that do not resolve to a valid URL
        structure, so a bare non-URL string returns False.
        """
        assert self.guard.validate_url("not-a-url") is False

    def test_verify_is_blocked_hostname_private(self):
        """Validate that is_blocked_hostname returns True for private IPs.

        RFC 1918 private ranges must be blocked to prevent internal-network
        SSRF, so loopback, 10/8 and 192.168/16 addresses all return True.
        """
        assert self.guard.is_blocked_hostname("127.0.0.1") is True
        assert self.guard.is_blocked_hostname("10.0.0.1") is True
        assert self.guard.is_blocked_hostname("192.168.1.1") is True

    def test_verify_is_blocked_hostname_public(self):
        """Validate that is_blocked_hostname returns False for public IPs.

        Public DNS-server addresses are legitimate targets, so 8.8.8.8 must
        be reported as unblocked.
        """
        assert self.guard.is_blocked_hostname("8.8.8.8") is False

    def test_verify_extract_safe_hostname(self):
        """Validate that extract_safe_hostname returns the URL's host component.

        URL parsing must isolate the hostname for the subsequent blocking
        checks, so "https://example.com/path" yields "example.com".
        """
        hostname = self.guard.extract_safe_hostname("https://example.com/path")
        assert hostname == "example.com"

    def test_verify_extract_safe_hostname_blocked(self):
        """Validate that extract_safe_hostname returns None for blocked hosts.

        The guard must signal rejection at the extraction stage when the
        host is a private IP, so "http://127.0.0.1/admin" yields None.
        """
        hostname = self.guard.extract_safe_hostname("http://127.0.0.1/admin")
        assert hostname is None

    def test_verify_clear_dns_cache(self):
        """Validate that clear_dns_cache empties the internal DNS cache.

        Cache clearing must drop every cached resolution so the next request
        performs a fresh DNS lookup, leaving _dns_cache at length zero.
        """
        self.guard.clear_dns_cache()
        assert len(self.guard._dns_cache) == 0


class TestRateLimiter:
    """Validate EncreRateLimiter construction and per-key rate tracking.

    Covers configuration, production defaults, the RateLimitResult contract
    for both allowed and denied outcomes, first-request allowance, and
    independence of rate windows across distinct tool keys.
    """

    def test_verify_create(self):
        """Validate that EncreRateLimiter stores the per_minute parameter.

        The constructor must preserve the configured rate limit rather than
        normalising it away.
        """
        rl = EncreRateLimiter(per_minute=60)
        assert rl.per_minute == 60

    def test_verify_defaults(self):
        """Validate that EncreRateLimiter uses the documented default limits.

        Default construction must yield the production defaults for the
        rate-limiting policy.
        """
        rl = EncreRateLimiter()
        assert rl.per_minute == 60
        assert rl.per_hour == 500
        assert rl.max_concurrent == 10

    def test_verify_rate_limit_result(self):
        """Validate that RateLimitResult stores allowed and remaining fields.

        The result object is the contract between the limiter and its caller,
        so both fields must survive construction unchanged.
        """
        rr = RateLimitResult(allowed=True, remaining=5)
        assert rr.allowed is True
        assert rr.remaining == 5

    def test_verify_rate_limit_result_denied(self):
        """Validate that RateLimitResult stores the denied state and retry_after.

        Denied results must communicate when the caller may retry, so the
        retry_after value must be preserved.
        """
        rr = RateLimitResult(allowed=False, remaining=0, retry_after=10.0)
        assert rr.allowed is False
        assert rr.retry_after == 10.0

    def test_verify_first_request_allowed(self):
        """Validate that the first request within the rate window is allowed.

        A single request must never be blocked by the limiter itself, so the
        check on an unused key returns allowed=True.
        """
        rl = EncreRateLimiter(per_minute=999)
        result = rl.check("tool_a")
        assert result.allowed is True

    def test_verify_different_keys_independent(self):
        """Validate that rate limits are tracked independently per key.

        Each key owns its own sliding window, so a request on a different
        key is unaffected by traffic on the first key.
        """
        rl = EncreRateLimiter(per_minute=999)
        rl.check("tool_a")
        result = rl.check("tool_b")
        assert result.allowed is True


class TestConfig:
    """Validate EncreConfig default values and custom construction.

    Covers defaults, custom model/backend/turns/tokens, backend kwargs, the
    permission mode, the sandbox-enabled flag, and the tool result size cap,
    treating the config object as the single source of truth for runtime
    parameters.
    """

    def test_verify_defaults(self):
        """Validate that EncreConfig() initializes with expected default values.

        Default construction must match the documented baseline: a positive
        token budget, zero turns to mean unlimited, and no model selected.
        """
        cfg = EncreConfig()
        # max_turns=0 means unlimited; model="" until a ModelConfig is selected.
        assert cfg.max_tokens > 0
        assert cfg.max_turns == 0
        assert cfg.model == ""

    def test_verify_custom_config(self):
        """Validate that EncreConfig preserves custom model and backend settings.

        Custom configuration must not be silently overwritten, so each
        supplied field has to round-trip to the same value.
        """
        cfg = EncreConfig(
            model="claude-sonnet-4-20250514",
            backend_type="anthropic",
            max_turns=25,
            max_tokens=32768,
        )
        assert cfg.model == "claude-sonnet-4-20250514"
        assert cfg.backend_type == "anthropic"
        assert cfg.max_turns == 25

    def test_verify_backend_kwargs(self):
        """Validate that EncreConfig stores backend_kwargs intact.

        Backend-specific parameters must pass through unchanged to the model
        provider, so the temperature value is preserved verbatim.
        """
        cfg = EncreConfig(backend_kwargs={"temperature": 0.7})
        assert cfg.backend_kwargs["temperature"] == 0.7

    def test_verify_permission_mode_default(self):
        """Validate that the default permission_mode is 'bypass'.

        The default policy allows all tool executions without user
        confirmation, so 'bypass' is expected out of default construction.
        """
        cfg = EncreConfig()
        assert cfg.permission_mode == "bypass"

    def test_verify_sandbox_enabled_default(self):
        """Validate that sandbox_enabled defaults to True.

        Sandbox isolation is enabled by default for safety, so the flag must
        be True without any explicit configuration.
        """
        cfg = EncreConfig()
        assert cfg.sandbox_enabled is True

    def test_verify_tool_result_max_chars(self):
        """Validate that tool_result_max_chars is stored and retrievable.

        The truncation threshold must be configurable per deployment, so a
        custom value round-trips through the constructor.
        """
        cfg = EncreConfig(tool_result_max_chars=50000)
        assert cfg.tool_result_max_chars == 50000


class TestTelemetry:
    """Validate the EncreTelemetry record types and summary pipeline.

    Covers ToolCallRecord, TurnRecord and RetryRecord construction, the three
    record_* appenders, get_summary aggregation, flush serialisation, reset,
    and the no-op behaviour of a disabled telemetry instance.
    """

    def setup_method(self):
        """Create a fresh EncreTelemetry instance before each test method.

        Returns:
            A new EncreTelemetry instance assigned to self.tel.
        """
        self.tel = EncreTelemetry()

    def test_verify_tool_call_record(self):
        """Validate that ToolCallRecord stores all fields correctly.

        The record is the atomic unit of telemetry data, so tool name,
        latency, success flag and token count must all be preserved.
        """
        tcr = ToolCallRecord(
            tool_name="bash", latency_ms=1500.0, success=True, tokens_used=100
        )
        assert tcr.tool_name == "bash"
        assert tcr.latency_ms == 1500.0
        assert tcr.success is True
        assert tcr.tokens_used == 100

    def test_verify_turn_record(self):
        """Validate that TurnRecord stores turn_number, event_count, latency_ms.

        Turn-level aggregation depends on accurate counting, so all three
        fields must survive construction.
        """
        tr = TurnRecord(turn_number=1, event_count=2, latency_ms=3000.0)
        assert tr.turn_number == 1
        assert tr.event_count == 2
        assert tr.latency_ms == 3000.0

    def test_verify_retry_record(self):
        """Validate that RetryRecord stores attempt, error_type, error_detail, delay_s.

        Retry records must capture the full error context for later
        error-rate analysis, so every field is asserted.
        """
        rr = RetryRecord(
            attempt=2, error_type="http_status", error_detail="429", delay_s=1.0
        )
        assert rr.attempt == 2
        assert rr.error_type == "http_status"
        assert rr.error_detail == "429"
        assert rr.delay_s == 1.0

    def test_verify_record_tool_call(self):
        """Validate that record_tool_call appends to the tool_calls list.

        Each recorded call must land in the accumulator, so one call yields
        exactly one entry.
        """
        self.tel.record_tool_call("bash", 2000.0, True, 100)
        assert len(self.tel.tool_calls) == 1

    def test_verify_record_turn(self):
        """Validate that record_turn appends to the turns list.

        Every turn must be recorded so latency and event counts can be
        aggregated later.
        """
        self.tel.record_turn(1, 2, 3000.0)
        assert len(self.tel.turns) == 1

    def test_verify_record_retry(self):
        """Validate that record_retry appends to the retries list.

        Retry events must be captured to support error-rate analysis.
        """
        self.tel.record_retry(1, "http_status", "429", 1.0)
        assert len(self.tel.retries) == 1

    def test_verify_get_summary(self):
        """Validate that get_summary returns a dict with total_tool_calls.

        The summary must aggregate the recorded calls into a reportable
        format, so the count reflects the single recorded tool call.
        """
        self.tel.record_tool_call("bash", 1000.0, True, 100)
        summary = self.tel.get_summary()
        assert isinstance(summary, dict)
        assert summary["total_tool_calls"] == 1

    def test_verify_flush(self):
        """Validate that flush persists recorded data and returns a summary dict.

        Flush must serialise the in-memory buffer into an external store and
        still report the aggregated totals.
        """
        self.tel.record_tool_call("bash", 1000.0, True, 100)
        result = self.tel.flush()
        assert isinstance(result, dict)
        assert result["total_tool_calls"] == 1

    def test_verify_reset(self):
        """Validate that reset clears all recorded telemetry data.

        Reset must provide a clean slate for a new session without restarting
        the process.
        """
        self.tel.record_tool_call("bash", 1000.0, True, 100)
        self.tel.reset()
        assert len(self.tel.tool_calls) == 0

    def test_verify_disabled_telemetry(self):
        """Validate that telemetry is discarded when enabled=False.

        A disabled instance must not accumulate any data, so recording a
        tool call leaves the list empty.
        """
        tel = EncreTelemetry(enabled=False)
        tel.record_tool_call("bash", 1000.0, True, 100)
        assert len(tel.tool_calls) == 0


class TestLoggingConfig:
    """Validate logging setup and logger retrieval.

    Covers setup_logging and get_logger to confirm the logging subsystem is
    functional and hands out usable logger objects.
    """

    def test_verify_setup_logging(self):
        """Validate that setup_logging completes without raising.

        The call configures global state and returns None on success, so
        reaching the end of the test means the setup succeeded.
        """
        setup_logging(level="WARNING")
        # setup_logging returns None (configures global state)

    def test_verify_get_logger(self):
        """Validate that get_logger returns a non-None Logger instance.

        Every module must be able to obtain a logger for structured output.
        """
        logger = get_logger("test.module")
        assert logger is not None


class TestSandboxTypes:
    """Validate the sandbox data types imported from encre.sandbox.types.

    Covers SandboxConfig custom construction and defaults, plus a successful
    and an error SandboxResult, to confirm the types module exposes a stable
    API surface.
    """

    def test_verify_sandbox_config(self):
        """Validate that SandboxConfig accepts custom image and timeout values.

        The config types must support deployment-specific overrides, so both
        fields round-trip through the constructor.
        """
        from encre.sandbox.types import SandboxConfig

        cfg = SandboxConfig(image="ubuntu:22.04", timeout=30)
        assert cfg.image == "ubuntu:22.04"
        assert cfg.timeout == 30

    def test_verify_sandbox_config_defaults(self):
        """Validate that SandboxConfig() exposes the documented defaults.

        The defaults define the baseline sandbox configuration: a slim Python
        image, no network access, and a modest memory ceiling.
        """
        from encre.sandbox.types import NetworkPolicy, SandboxConfig

        cfg = SandboxConfig()
        assert cfg.image == "python:3.11-slim"
        assert cfg.network.policy is NetworkPolicy.NONE
        assert cfg.resource.memory_limit == "512m"

    def test_verify_sandbox_result(self):
        """Validate that SandboxResult stores stdout, stderr, exit_code, duration_ms.

        The result is the primary contract between the sandbox and its
        caller, so a successful execution must report code zero and its
        stdout payload.
        """
        from encre.sandbox.types import SandboxResult

        sr = SandboxResult(stdout="success", stderr="", exit_code=0, duration_ms=1200.0)
        assert sr.exit_code == 0
        assert sr.stdout == "success"

    def test_verify_sandbox_result_error(self):
        """Validate that SandboxResult stores error diagnostics correctly.

        Error results must preserve every diagnostic field so downstream
        logging and reporting can explain the failure.
        """
        from encre.sandbox.types import SandboxResult

        sr = SandboxResult(
            stdout="", stderr="command not found", exit_code=1, timed_out=True
        )
        assert sr.exit_code == 1
        assert sr.timed_out is True
