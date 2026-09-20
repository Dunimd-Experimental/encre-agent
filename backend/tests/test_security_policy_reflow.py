#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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

"""Tests for behaviour -> safety-policy reflow (``encre.safety_policy``).

Covers the three hard invariants of the learned-policy layer:

1. A learned ``deny`` takes effect and rejects the offending call.
2. A preference that would widen access is compiled to ``ask`` -- never
   ``allow`` (the core assertion).
3. The dangerous-command blacklist cannot be weakened by a learned entry.

Plus the persistence round-trip, the Python <-> Rust bridge, and the event
publication.
"""

import json
from pathlib import Path

import pytest

from encre.config import EncreConfig
from encre.improve.feedback.learner import CorrectionRecord, EncreFeedbackLearner
from encre.safety import EncreSafetyEngine
from encre.safety_policy import EncreSafetyPolicyReflow, PolicyEntry

try:
    from encre import native as _native
except Exception:  # pragma: no cover - native build absent
    _native = None

_HAS_LEARNED_API = _native is not None and hasattr(
    _native, "permission_set_learned_policies"
)

pytestmark = pytest.mark.skipif(
    not _HAS_LEARNED_API,
    reason="native permission engine with learned-policy support is required",
)


def _dangerous_rules_path() -> Path:
    """Locate ``dangerous_commands.txt`` next to the safety module."""
    from encre import safety as _safety

    return Path(_safety.__file__).parent / "dangerous_commands.txt"


def _load_dangerous_rules() -> None:
    """Push the real blacklist into the engine so detection is deterministic."""
    path = _dangerous_rules_path()
    if path.exists():
        _native.permission_set_dangerous_patterns(path.read_text(encoding="utf-8"))


def _pick_dangerous_command() -> str:
    """Return a command the current platform's blacklist actually matches."""
    for candidate in ("sudo rm -rf /", "rm -rf /", "format C:", "rd /s /q C:\\"):
        payload = json.dumps({"command": candidate})
        if _native.permission_check_dangerous("bash", payload):
            return candidate
    pytest.skip("no dangerous pattern matched on this platform")


def _decision(tool: str, args: dict, mode: str) -> dict:
    """Run the Rust engine directly and return the decision dict."""
    return _native.permission_check_mode(tool, json.dumps(args), mode)


@pytest.fixture(autouse=True)
def _reset_learned_layer():
    """Keep the process-global learned layer isolated between tests."""
    if _HAS_LEARNED_API:
        _native.permission_set_learned_policies("[]")
    yield
    if _HAS_LEARNED_API:
        _native.permission_set_learned_policies("[]")


@pytest.fixture
def learner(tmp_path):
    """A feedback learner persisting to a throwaway path."""
    return EncreFeedbackLearner(storage_path=str(tmp_path / "feedback" / "corrections.json"))


@pytest.fixture
def reflow(learner, tmp_path):
    """A reflow bound to the learner and a throwaway policy document."""
    return EncreSafetyPolicyReflow(
        learner, storage_path=tmp_path / "safety" / "learned_policies.json"
    )


@pytest.fixture
def engine():
    """A default-mode safety engine wired to the native permission engine."""
    config = EncreConfig(
        model="gpt-5.6",
        permission_mode="default",
        max_turns=10,
        max_tokens=4096,
        log_level="WARNING",
    )
    return EncreSafetyEngine(config)


# ---------------------------------------------------------------------------
# 1. Learned deny takes effect
# ---------------------------------------------------------------------------

async def test_deny_policy_from_correction_is_enforced(reflow, learner, engine):
    """Repeated "never read <dir>" corrections compile to a scoped deny that fires."""
    sensitive = "/home/user/private/secrets"
    for _ in range(3):
        learner.record_correction(
            "file_read",
            "permission_error",
            f"read {sensitive}/token.txt",
            f"Never read files under {sensitive}",
        )

    entries = reflow.apply()
    denied = [e for e in entries if e.policy == "deny" and e.path_scope == sensitive]
    assert denied, f"expected a scoped deny entry, got {entries}"
    assert denied[0].tool == "file_read"

    blocked = await engine.check_tool_permission(
        "file_read", {"path": f"{sensitive}/token.txt"}
    )
    assert blocked.behavior == "deny"

    # The deny is path-scoped: an unrelated read is untouched.
    allowed = await engine.check_tool_permission(
        "file_read", {"path": "/home/user/public/readme.txt"}
    )
    assert allowed.behavior == "allow"


# ---------------------------------------------------------------------------
# 2. Escalation preference is downgraded to `ask`
# ---------------------------------------------------------------------------

async def test_escalation_preference_is_downgraded_to_ask(reflow, learner, engine):
    """A "always allow" preference must compile to `ask`, never `allow`."""
    for _ in range(2):
        learner.record_correction(
            "bash",
            "permission_error",
            "command blocked: npm install",
            "Always allow running npm install without asking",
        )

    entries = reflow.apply()
    assert entries, "expected a compiled policy entry"
    # Core assertion: nothing is ever `allow`.
    assert not any(e.policy == "allow" for e in entries)
    assert all(e.policy in {"deny", "ask"} for e in entries)
    assert any(e.policy == "ask" for e in entries)

    # Rust enforces the same invariant on its own.
    native_entries = json.loads(_native.permission_get_learned_policies())
    assert native_entries
    assert all(e["policy"] in {"deny", "ask"} for e in native_entries)

    decision = await engine.check_tool_permission("bash", {"command": "npm install"})
    assert decision.behavior == "ask"
    assert decision.behavior != "allow"


def test_compile_never_yields_allow(tmp_path):
    """Direct compile: deny stays deny, escalation becomes ask."""
    reflow = EncreSafetyPolicyReflow(storage_path=tmp_path / "policies.json")
    entries = reflow.compile_records(
        [
            CorrectionRecord(
                tool_name="bash",
                error_type="permission_error",
                error_context="blocked: ls",
                user_correction="Always allow running ls",
            ),
            CorrectionRecord(
                tool_name="file_read",
                error_type="permission_error",
                error_context="read /var/secret",
                user_correction="Never read /var/secret",
            ),
        ]
    )
    assert all(e.policy in {"deny", "ask"} for e in entries)
    assert {e.policy for e in entries} == {"ask", "deny"}
    # The escalation entry is scoped to the command it mentioned.
    assert any(e.policy == "ask" and e.command_scope == "ls" for e in entries)
    assert any(e.policy == "deny" and e.path_scope == "/var/secret" for e in entries)


# ---------------------------------------------------------------------------
# 3. Dangerous patterns are not bypassed by learning
# ---------------------------------------------------------------------------

async def test_dangerous_command_not_bypassed_by_learned_policy(reflow, learner, engine):
    """A learned "allow this dangerous command" cannot make it run silently."""
    _load_dangerous_rules()
    dangerous = _pick_dangerous_command()
    learner.record_correction(
        "bash",
        "permission_error",
        f"command blocked: {dangerous}",
        f"Always allow running {dangerous}",
    )
    entries = reflow.apply()
    assert entries
    assert all(e.policy != "allow" for e in entries)

    # Default mode: still requires confirmation / refuses -- never auto-allowed.
    default_decision = _decision("bash", {"command": dangerous}, "default")
    assert default_decision["behavior"] in {"ask", "deny"}
    assert default_decision["behavior"] != "allow"

    # Bypass mode: the blacklist refuses outright; the learned `ask` must not
    # downgrade that `deny` to a pass.
    bypass_decision = _decision("bash", {"command": dangerous}, "bypass")
    assert bypass_decision["behavior"] == "deny"

    # A hand-crafted learned `allow` is dropped by the engine entirely.
    _native.permission_set_learned_policies(
        json.dumps([{"tool": "bash", "policy": "allow", "reason": "malicious"}])
    )
    assert json.loads(_native.permission_get_learned_policies()) == []
    assert _decision("bash", {"command": dangerous}, "bypass")["behavior"] == "deny"


def test_learned_deny_is_stricter_than_dangerous_ask(tmp_path):
    """A learned deny tightens a dangerous command's `ask` into a `deny`."""
    _load_dangerous_rules()
    dangerous = _pick_dangerous_command()
    reflow = EncreSafetyPolicyReflow(storage_path=tmp_path / "policies.json")
    entries = reflow.compile_records(
        [
            CorrectionRecord(
                tool_name="bash",
                error_type="permission_error",
                error_context=f"command blocked: {dangerous}",
                user_correction=f"Never run {dangerous}",
            )
        ]
    )
    reflow.sync_to_native(entries)
    # Default mode would normally ask; the learned deny makes it a hard deny.
    assert _decision("bash", {"command": dangerous}, "default")["behavior"] == "deny"


# ---------------------------------------------------------------------------
# Persistence round-trip (real disk, not an in-memory illusion)
# ---------------------------------------------------------------------------

def test_policy_persists_and_reloads_from_disk(reflow, learner):
    """Compiled entries survive an encrypted write + read on a fresh instance."""
    sensitive = "/var/private/keys"
    learner.record_correction(
        "file_read",
        "permission_error",
        f"read {sensitive}/id_rsa",
        f"Never read {sensitive}",
    )
    applied = reflow.apply()
    assert applied

    # Real file on disk, written as ciphertext by secure_io.
    assert reflow.storage_path.exists()
    raw = reflow.storage_path.read_text(encoding="utf-8")
    assert sensitive not in raw, "policy document must not store plaintext"

    # A brand-new instance reads it back and can rebuild the Rust layer.
    fresh = EncreSafetyPolicyReflow(storage_path=reflow.storage_path)
    loaded = fresh.load()
    assert [e.to_dict() for e in loaded] == [e.to_dict() for e in applied]
    fresh.sync_to_native(loaded)
    native_entries = json.loads(_native.permission_get_learned_policies())
    assert any(e["path_scope"] == sensitive and e["policy"] == "deny" for e in native_entries)


# ---------------------------------------------------------------------------
# Python <-> Rust bridge
# ---------------------------------------------------------------------------

def test_python_rust_bridge_exposes_learned_policies(reflow, learner):
    """The Rust policy table reports the learned entries back to Python."""
    learner.record_correction(
        "file_write",
        "permission_error",
        "write /etc/cron.d/job",
        "Never write /etc/cron.d",
    )
    reflow.apply()

    state = json.loads(_native.permission_get_policies())
    assert state.get("learned"), f"learned layer missing from {state}"
    assert any(e.get("path_scope") == "/etc/cron.d" for e in state["learned"])

    learned = json.loads(_native.permission_get_learned_policies())
    assert any(e["policy"] == "deny" and e["tool"] == "file_write" for e in learned)


def test_set_policies_does_not_wipe_learned_layer(reflow, learner):
    """Updating the tool/capability table must not drop learned policies."""
    learner.record_correction(
        "file_read",
        "permission_error",
        "read /srv/private/x",
        "Never read /srv/private",
    )
    reflow.apply()
    before = json.loads(_native.permission_get_learned_policies())
    assert before

    # Re-submit the current table (as the settings panel would) without a
    # `learned` field.
    current = json.loads(_native.permission_get_policies())
    _native.permission_set_policies(
        json.dumps({"tools": current.get("tools", {}), "capabilities": current.get("capabilities", {})})
    )
    after = json.loads(_native.permission_get_learned_policies())
    assert after == before


# ---------------------------------------------------------------------------
# Event backbone publication
# ---------------------------------------------------------------------------

def test_policy_change_publishes_event(learner, tmp_path):
    """Applying a policy change publishes on the event backbone."""
    from encre.events.ledger import EventSourcedBus

    bus = EventSourcedBus("test-safety", ledger_path=tmp_path / "ledger.jsonl")
    reflow = EncreSafetyPolicyReflow(
        learner, storage_path=tmp_path / "policies.json", bus=bus
    )
    learner.record_correction(
        "bash",
        "permission_error",
        "command blocked: rm -rf /tmp/x",
        "Never run rm -rf",
    )
    reflow.apply()
    bus.flush()

    kinds = bus.project("")["kinds"]
    assert kinds.get("safety_policy_updated") == 1


def test_merge_prefers_deny_over_ask(tmp_path):
    """When a deny and an ask target the same scope, the deny wins."""
    reflow = EncreSafetyPolicyReflow(storage_path=tmp_path / "policies.json")
    entries = reflow._merge(
        [
            PolicyEntry(tool="bash", command_scope="rm -rf", policy="ask"),
            PolicyEntry(tool="bash", command_scope="rm -rf", policy="deny"),
        ]
    )
    assert len(entries) == 1
    assert entries[0].policy == "deny"
