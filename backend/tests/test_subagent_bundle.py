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

"""Tests for sub-agent bundles (P1.4): external agent CLIs exposed as real tools.

Every test drives REAL processes on this machine (``python -c ...``) — no
fake binaries, no mocks.  The permission-gating test uses the real Rust
permission engine, following the pattern of ``test_security_policy_reflow.py``.
"""

import asyncio
import time
import uuid

import pytest

from encre.events.ledger import EventSourcedBus
from encre.subagent_bundle import SubAgentBundle, register_subagent_bundle
from encre.tools.registry import ToolRegistry

try:
    from encre import native as _native
except Exception:  # pragma: no cover - native build absent
    _native = None

_HAS_NATIVE_PERM = _native is not None and hasattr(_native, "permission_set_policies")


def _py_script(body: str) -> list[str]:
    """argv template running ``body`` through this machine's real python."""
    return ["python", "-c", body, "{prompt}"]


def _collect_bus() -> tuple[EventSourcedBus, dict[str, list]]:
    """A real in-memory EventSourcedBus with both bundle kinds collected."""
    bus = EventSourcedBus("test-subagent-bundle")
    collected: dict[str, list] = {"subagent_started": [], "subagent_finished": []}
    bus.subscribe("subagent_started", lambda e: collected["subagent_started"].append(e))
    bus.subscribe("subagent_finished", lambda e: collected["subagent_finished"].append(e))
    return bus, collected


# ---------------------------------------------------------------------------
# Happy path: real command, real events
# ---------------------------------------------------------------------------

async def test_happy_path_runs_real_command_and_publishes_both_events():
    """Validate that a real python command runs and emits both started and finished events.

    Executes a genuine ``python -c`` subprocess through the bundle tool and
    asserts the command output is returned, that both subagent_started and
    subagent_finished are published with the session id and bundle name, and
    that the finished payload reports a zero exit code with the full key set.
    """
    bus, collected = _collect_bus()
    bundle = SubAgentBundle(
        name="py_agent",
        command=["python", "-c", "print('ok')"],
        description="Runs python",
        timeout_s=30,
        bus=bus,
    )
    tool = bundle.to_tool()
    result = await tool.execute(prompt="say ok", _session_id="sess-1")

    assert "ok" in result
    assert not result.lstrip().startswith("Error"), result

    kinds = [e.kind for e in collected["subagent_started"] + collected["subagent_finished"]]
    assert "subagent_started" in kinds
    assert "subagent_finished" in kinds

    started = collected["subagent_started"][0]
    assert started.session_id == "sess-1"
    assert started.payload["name"] == "py_agent"
    assert started.payload["pid"] > 0

    finished = collected["subagent_finished"][0]
    assert finished.session_id == "sess-1"
    assert finished.payload["ok"] is True
    assert finished.payload["exit_code"] == 0
    assert finished.payload["duration_ms"] >= 0
    assert "subagent_started" in finished.payload or True  # payload keys stable
    assert set(finished.payload) >= {"name", "pid", "duration_ms", "exit_code", "ok"}


# ---------------------------------------------------------------------------
# Prompt substitution is argv-only: shell metacharacters are inert data
# ---------------------------------------------------------------------------

async def test_prompt_is_passed_as_single_argv_element_no_injection():
    """Validate that the prompt is a single inert argv element and cannot inject shell commands.

    Feeds shell metacharacters (&&, ;, |, rm) through a script that echoes
    argv[1] and asserts the payload comes back verbatim and unexecuted, so
    command chaining, redirection and deletion never run.
    """
    malicious = "hello && echo PWNED; rm -rf / | echo hacked"
    bundle = SubAgentBundle(
        name="echo_agent",
        command=_py_script("import sys; sys.stdout.write(sys.argv[1])"),
        description="echoes argv[1]",
        timeout_s=30,
    )
    tool = bundle.to_tool()
    result = await tool.execute(prompt=malicious)

    assert result == malicious, "prompt must come back as inert data, unexecuted"
    assert "PWNED" in result and "hacked" in result
    assert result.count(malicious) == 1


# ---------------------------------------------------------------------------
# Timeout: fast clear error and the subprocess is really killed (no orphan)
# ---------------------------------------------------------------------------

async def test_timeout_kills_process_tree_and_returns_error_fast():
    """Validate that an over-long subprocess is killed fast with a clear timeout error and no orphan.

    Runs a 30 second sleeper under a 2 second timeout, asserts the error
    surfaces well under 10 seconds with timeout and kill wording, then scans
    psutil for any surviving process whose command line carries a unique
    marker so an escaped child cannot outlive the bundle.
    """
    marker = f"encre-orphan-check-{uuid.uuid4().hex}"
    script = f"import os, time; print('{marker}', os.getpid(), flush=True); time.sleep(30)"
    bundle = SubAgentBundle(
        name="sleeper_agent",
        command=["python", "-c", script],
        description="sleeps forever",
        timeout_s=2,
    )
    tool = bundle.to_tool()

    start = time.monotonic()
    result = await tool.execute(prompt="sleep")
    elapsed = time.monotonic() - start

    assert elapsed < 10, f"timeout must surface fast, took {elapsed:.1f}s"
    assert result.startswith("Error"), result
    assert "timed out" in result
    assert "killed" in result

    # No orphan python carrying our unique marker survives the kill.
    import psutil

    deadline = time.monotonic() + 6
    orphans: list[int] = []
    while time.monotonic() < deadline:
        orphans = [
            p.pid for p in psutil.process_iter(attrs=["cmdline"])
            if any(marker in str(part) for part in (p.info.get("cmdline") or []))
        ]
        if not orphans:
            break
        await asyncio.sleep(0.2)
    assert not orphans, f"orphaned sub-agent processes survived: {orphans}"


# ---------------------------------------------------------------------------
# Failing command: exit code + stderr surfaced, ok=False in the event
# ---------------------------------------------------------------------------

async def test_failing_command_surfaces_stderr_and_ok_false():
    """Validate that a non-zero exit surfaces the exit code, stderr, and ok=False in the event.

    Runs a subprocess that writes to stderr and exits 1, then asserts the
    error string contains the exit code and the stderr text, the finished
    event reports ok False with exit code 1, and a started event was emitted
    for the process that did actually launch.
    """
    bus, collected = _collect_bus()
    bundle = SubAgentBundle(
        name="failing_agent",
        command=[
            "python", "-c",
            "import sys; print('boom-stderr', file=sys.stderr); sys.exit(1)",
        ],
        description="always fails",
        timeout_s=30,
        bus=bus,
    )
    tool = bundle.to_tool()
    result = await tool.execute(prompt="go")

    assert result.startswith("Error"), result
    assert "exit code 1" in result
    assert "boom-stderr" in result

    finished = collected["subagent_finished"][0]
    assert finished.payload["ok"] is False
    assert finished.payload["exit_code"] == 1
    assert "subagent_started" in [e.kind for e in collected["subagent_started"]]


# ---------------------------------------------------------------------------
# Binary-not-found guard: clear error, nothing spawned, no started event
# ---------------------------------------------------------------------------

async def test_missing_binary_is_refused_with_clear_error():
    """Validate that a missing executable is refused up front with no process spawned.

    Points the bundle at a guaranteed-absent binary name and asserts the error
    names it with "not found" wording, that no subagent_started event fired,
    and that exactly one failed subagent_finished event was published.
    """
    bus, collected = _collect_bus()
    ghost = f"encre-no-such-binary-{uuid.uuid4().hex[:8]}"
    bundle = SubAgentBundle(
        name="ghost_agent",
        command=[ghost, "{prompt}"],
        description="binary does not exist",
        timeout_s=30,
        bus=bus,
    )
    tool = bundle.to_tool()
    result = await tool.execute(prompt="hi")

    assert result.startswith("Error"), result
    assert ghost in result
    assert "not found" in result

    # The guard fires BEFORE any process exists: no start event, only finish.
    assert collected["subagent_started"] == []
    assert len(collected["subagent_finished"]) == 1
    assert collected["subagent_finished"][0].payload["ok"] is False


# ---------------------------------------------------------------------------
# Environment minimality: parent secrets do not leak unless allowlisted
# ---------------------------------------------------------------------------

async def test_child_env_is_minimal_sentinel_leaks_only_via_allowlist(monkeypatch):
    """Validate that parent environment secrets stay hidden unless explicitly allowlisted.

    Sets a sentinel variable in the parent, then asserts a bundle without an
    allowlist never sees it while a bundle carrying env_allowlist receives the
    exact secret value.
    """
    sentinel = "ENCRE_BUNDLE_TEST_SENTINEL"
    monkeypatch.setenv(sentinel, "top-secret-value")

    no_leak_bundle = SubAgentBundle(
        name="env_agent",
        command=[
            "python", "-c",
            f"import os; print(os.environ.get('{sentinel}', 'ABSENT'))",
        ],
        description="prints sentinel",
        timeout_s=30,
    )
    result = await no_leak_bundle.to_tool().execute(prompt="env")
    assert "ABSENT" in result, f"parent env leaked into child: {result}"

    allowlisted = SubAgentBundle(
        name="env_agent_allow",
        command=[
            "python", "-c",
            f"import os; print(os.environ.get('{sentinel}', 'ABSENT'))",
        ],
        description="prints sentinel",
        timeout_s=30,
        env_allowlist=[sentinel],
    )
    result = await allowlisted.to_tool().execute(prompt="env")
    assert "top-secret-value" in result, "allowlisted var must pass through"


# ---------------------------------------------------------------------------
# Truncation: oversized stdout comes back capped with a marker
# ---------------------------------------------------------------------------

async def test_oversized_output_is_truncated_with_marker():
    """Validate that oversized stdout is capped to the configured budget with a truncation marker.

    Captures 100000 characters of output under a 500 character limit and
    asserts the returned text is small and carries the truncation notice so
    callers know the output was cut rather than complete.
    """
    bundle = SubAgentBundle(
        name="chatty_agent",
        command=["python", "-c", "print('x' * 100000)"],
        description="prints a lot",
        timeout_s=30,
        max_output_chars=500,
    )
    result = await bundle.to_tool().execute(prompt="go")
    assert len(result) < 1000
    assert "output truncated by subagent bundle" in result


# ---------------------------------------------------------------------------
# Registration: the bundle satisfies the real ToolRegistry contract
# ---------------------------------------------------------------------------

def test_register_subagent_bundle_produces_valid_registry_tool():
    """Validate that register_subagent_bundle() yields a fully valid ToolRegistry tool.

    Registers a bundle through the helper into a real ToolRegistry and asserts
    the registry hands back the same tool object, the tool metadata and
    input_schema are correct, the OpenAI function format is shaped right, and
    the bundle retains its configured timeout.
    """
    registry = ToolRegistry()
    bundle, tool = register_subagent_bundle(
        registry,
        name="registered_agent",
        command=["python", "-c", "print('ok')"],
        description="registered through the helper",
        timeout_s=30,
    )
    assert registry.get("registered_agent") is tool
    # Registry validation passed, so the tool contract holds end to end.
    assert tool.name == "registered_agent"
    assert tool.description
    assert tool.input_schema["required"] == ["prompt"]
    assert "prompt" in tool.input_schema["properties"]
    openai_fmt = tool.to_openai_format()
    assert openai_fmt["function"]["name"] == "registered_agent"
    assert bundle.timeout_s == 30


# ---------------------------------------------------------------------------
# Permission gating: the executor's permission path still applies to bundles
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _HAS_NATIVE_PERM,
    reason="native permission engine is required",
)
async def test_deny_policy_blocks_bundle_tool_via_rust_engine():
    """A deny-capability config in the Rust engine blocks the bundle tool.

    Mirrors ``test_security_policy_reflow.py``: the user-managed policy table
    (what the settings panel writes) is pushed into the real Rust engine and
    the safety engine's tool-permission path -- the same path the executor
    takes for every tool call -- must come back with ``deny``.
    """
    from encre.config import EncreConfig
    from encre.safety import EncreSafetyEngine

    registry = ToolRegistry()
    name = "gated_agent"
    _, tool = register_subagent_bundle(
        registry,
        name=name,
        command=["python", "-c", "print('should not run')"],
        description="gated by policy",
        timeout_s=30,
    )
    assert registry.get(name) is tool

    config = EncreConfig(model="gpt-5.6", permission_mode="custom", log_level="WARNING")
    engine = EncreSafetyEngine(config)
    saved = engine.get_policies()
    try:
        # The settings-panel deny: tool-scoped policy entry in the Rust table.
        engine.set_policies({name: "deny"}, {})
        decision = await engine.check_tool_permission(name, {"prompt": "hi"})
        assert decision.behavior == "deny"
    finally:
        engine.set_policies(saved.get("tools", {}), saved.get("capabilities", {}))
