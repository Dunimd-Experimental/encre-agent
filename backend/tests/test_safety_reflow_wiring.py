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

"""Runtime wiring of the behaviour -> safety-policy reflow (Task 4.1-4.3).

The unit-level invariants of :mod:`encre.safety_policy` are covered by
``test_security_policy_reflow.py``.  This module exercises the **wired** path
instead: the real :class:`~encre.loop.wiring._LoopWiringMixin` builds the real
:class:`~encre.safety_policy.EncreSafetyPolicyReflow` on the real event
backbone, and the real turn-tail hook in
:class:`~encre.loop.phases.end_of_turn.PhaseEndOfTurnMixin` drives it.  No
reflow, learner, bus or engine is mocked.

Covered invariants:

1. a correction recorded through the real feedback learner is compiled,
   persisted (encrypted) and pushed into the Rust engine by the turn-tail
   hook;
2. the hook is a no-op when the learner's record count did not change, and
   runs again as soon as a new correction arrives;
3. a reflow that raises (compile or sync) never propagates out of the hook and
   the turn teardown still completes;
4. an escalation preference stays ``ask`` end-to-end -- never ``allow``;
5. policies learned in a previous run are restored into the Rust engine when
   the loop is rebuilt;
6. the reflow is genuinely reachable from the production loop (anti-dormancy)
   and the loop still builds when no feedback learner is present.

The data root is redirected to ``tmp_path`` (``ENCRE_DATA_DIR``) and the
reflow's default storage path is resolved through the production
``get_data_dir`` helper, so the real user data directory is never touched.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from encre.config import EncreConfig
from encre.improve.feedback.learner import EncreFeedbackLearner
from encre.loop import EncreLoop
from encre.loop.turn_ctx import TurnContext
from encre.safety import EncreSafetyEngine
from encre.safety_policy import EncreSafetyPolicyReflow
from encre.session import EncreSession

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_learned_layer():
    """Keep the process-global Rust learned layer isolated between tests."""
    if _HAS_LEARNED_API:
        _native.permission_set_learned_policies("[]")
    yield
    if _HAS_LEARNED_API:
        _native.permission_set_learned_policies("[]")


@pytest.fixture
def learner(tmp_path) -> EncreFeedbackLearner:
    """A real feedback learner persisting to a throwaway path."""
    return EncreFeedbackLearner(storage_path=str(tmp_path / "feedback" / "corrections.json"))


@pytest.fixture
def policy_path(tmp_path, monkeypatch):
    """Return the policy path the *real* data-dir resolver produces under ``tmp_path``.

    The wiring constructs the reflow without an explicit ``storage_path``, so
    only the data root is redirected (``ENCRE_DATA_DIR``) -- the resolver
    itself is left in place so a regression in how the storage path is
    *computed* fails here instead of being masked by a patched-out resolver.

    Returns:
        The path the wired reflow must persist its policy document to.
    """
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data" / "safety" / "learned_policies.json"


def _config() -> EncreConfig:
    """A minimal config for the loop under test."""
    return EncreConfig(
        model="gpt-5.6",
        backend_type="openai",
        permission_mode="default",
        max_turns=10,
        max_tokens=4096,
        log_level="ERROR",
        enable_prompt_caching=False,
    )


def _fake_backend() -> MagicMock:
    """A backend double satisfying the loop's construction-time probes."""
    backend = MagicMock()
    backend.supports_tool_calling.return_value = True
    backend.supports_prompt_caching.return_value = False
    backend.context_window_size.return_value = 128000
    return backend


def _build_loop(
    config: EncreConfig, session: EncreSession,
    feedback: EncreFeedbackLearner | None,
) -> EncreLoop:
    """Build a real loop with the backend factory patched out."""
    with patch("encre.loop.create_backend") as mock_create_backend:
        mock_create_backend.return_value = _fake_backend()
        return EncreLoop(config=config, session=session, feedback=feedback)


@pytest.fixture
def loop(tmp_path, monkeypatch, learner, policy_path) -> EncreLoop:
    """A real loop wired with a real feedback learner and isolated data root."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    return _build_loop(_config(), EncreSession(_config()), learner)


def _turn(prompt: str = "do the thing") -> TurnContext:
    """A turn context with just enough state for the turn tail to run."""
    t = TurnContext(prompt, None, "", "", None)
    t.turn_start = time.time()
    t.assistant_content = "done"
    t.backend_usage = {"input_tokens": 10, "output_tokens": 5}
    return t


# ---------------------------------------------------------------------------
# 1. Correction -> compile -> persist -> sync, through the turn-tail hook
# ---------------------------------------------------------------------------

def test_correction_is_persisted_and_synced_by_hook(loop, policy_path):
    """A real correction ends up as a real, encrypted, enforced policy entry."""
    sensitive = "/home/user/private/secrets"
    loop.feedback.record_correction(
        "file_read",
        "permission_error",
        f"read {sensitive}/token.txt",
        f"Never read files under {sensitive}",
    )
    # Construction alone must not write the document.
    assert not policy_path.exists()

    loop._dispatch_safety_reflow()

    # Real file on disk, written as ciphertext by secure_io.
    assert policy_path.exists()
    raw = policy_path.read_text(encoding="utf-8")
    assert sensitive not in raw, "policy document must not store plaintext"

    # The reflow can read its own document back.
    entries = loop._policy_reflow.load()
    assert any(e.policy == "deny" and e.path_scope == sensitive for e in entries)

    # And the Rust engine -- the authorization source of truth -- has it.
    native = json.loads(_native.permission_get_learned_policies())
    assert any(e["path_scope"] == sensitive and e["policy"] == "deny" for e in native)


# ---------------------------------------------------------------------------
# 2. At most once per turn: unchanged record count short-circuits
# ---------------------------------------------------------------------------

def test_hook_short_circuits_until_a_new_correction_arrives(loop, policy_path):
    """No new correction means no additional work; a new one re-runs the hook."""
    loop.feedback.record_correction(
        "file_read", "permission_error", "read /srv/private/x", "Never read /srv/private",
    )
    loop._dispatch_safety_reflow()
    assert policy_path.exists()
    assert loop._last_reflow_record_count == loop.feedback.record_count == 1
    first_bytes = policy_path.read_bytes()

    # Two more runs with no new correction: nothing is recompiled or rewritten.
    # An untouched file is byte-identical -- no persist happened.
    loop._dispatch_safety_reflow()
    loop._dispatch_safety_reflow()
    assert policy_path.read_bytes() == first_bytes
    assert loop._last_reflow_record_count == 1

    # A new correction changes the count and the hook runs again.
    loop.feedback.record_correction(
        "bash", "permission_error", "blocked: rm -rf /tmp/y", "Never run rm -rf /tmp/y",
    )
    loop._dispatch_safety_reflow()
    assert loop._last_reflow_record_count == 2
    scopes = {e.path_scope for e in loop._policy_reflow.load()}
    assert {"/srv/private", "/tmp/y"} <= scopes


# ---------------------------------------------------------------------------
# 3. A failing reflow never breaks turn teardown
# ---------------------------------------------------------------------------

def _boom_reflow(
    learner: EncreFeedbackLearner, storage_path, stage: str,
) -> EncreSafetyPolicyReflow:
    """A real reflow whose compile or sync stage raises.

    Only the named stage is broken; everything else is the production code, so
    the assertion stays about the hook's behaviour rather than about a stub.
    """
    if stage == "compile":
        class _CompileBoom(EncreSafetyPolicyReflow):
            """Reflow test double whose ``compile`` stage always raises."""

            def compile(self):
                """Raise instead of compiling, simulating a failing compile stage."""
                raise RuntimeError("compile boom")
        return _CompileBoom(learner, storage_path=storage_path)

    class _SyncBoom(EncreSafetyPolicyReflow):
        """Reflow test double whose ``sync_to_native`` stage always raises."""

        def sync_to_native(self, entries=None):
            """Raise instead of syncing, simulating a failing native-sync stage."""
            raise RuntimeError("sync boom")
    return _SyncBoom(learner, storage_path=storage_path)


@pytest.mark.parametrize("stage", ["compile", "sync"])
async def test_failing_reflow_does_not_break_turn_teardown(
    loop, policy_path, stage,
):
    """A raising reflow is swallowed and the turn tail still completes."""
    loop.feedback.record_correction(
        "bash", "permission_error", "blocked: rm -rf /tmp/x", "Never run rm -rf /tmp/x",
    )
    loop._policy_reflow = _boom_reflow(loop.feedback, policy_path, stage)
    # The rollback store is a process-global data-tree path; it is stubbed so
    # this test never writes outside tmp_path.  The assertion below is about
    # teardown *reaching* the commit step past the failing hook.
    loop.rollback = MagicMock()

    # An empty prompt keeps the significance judge out of the picture, so the
    # assertion stays about the reflow hook.
    await loop._phase_end_of_turn(_turn(""))

    # Reached the end of the turn tail: the failure was isolated to the hook.
    loop.rollback.commit.assert_called_once()
    # The count is not advanced, so the next turn retries the reflow.
    assert loop._last_reflow_record_count == -1


# ---------------------------------------------------------------------------
# 4. The escalation hard constraint, end-to-end through the wired path
# ---------------------------------------------------------------------------

async def test_escalation_preference_is_ask_not_allow_when_wired(loop, policy_path):
    """An "always allow" preference never becomes `allow` on the wired path."""
    loop.feedback.record_correction(
        "bash",
        "permission_error",
        "command blocked: npm install",
        "Always allow running npm install without asking",
    )
    loop._dispatch_safety_reflow()

    assert policy_path.exists()
    entries = loop._policy_reflow.load()
    assert entries, "expected a compiled policy entry"
    assert not any(e.policy == "allow" for e in entries)
    assert all(e.policy in {"deny", "ask"} for e in entries)

    native = json.loads(_native.permission_get_learned_policies())
    assert native
    assert all(e["policy"] in {"deny", "ask"} for e in native)

    # The real safety engine reports `ask` for the exact command.
    engine = EncreSafetyEngine(_config())
    decision = await engine.check_tool_permission("bash", {"command": "npm install"})
    assert decision.behavior == "ask"
    assert decision.behavior != "allow"


# ---------------------------------------------------------------------------
# 5. Startup restore: policies learned in a previous run are re-pushed
# ---------------------------------------------------------------------------

def test_startup_restores_learned_policies_into_rust(loop, policy_path):
    """Rebuilding the loop restores the persisted policies into the engine."""
    sensitive = "/var/private/keys"
    loop.feedback.record_correction(
        "file_read",
        "permission_error",
        f"read {sensitive}/id_rsa",
        f"Never read {sensitive}",
    )
    loop._dispatch_safety_reflow()
    assert policy_path.exists()

    # Simulate a process restart: the Rust learned layer starts empty.
    _native.permission_set_learned_policies("[]")
    assert json.loads(_native.permission_get_learned_policies()) == []

    restarted = _build_loop(
        _config(),
        EncreSession(_config()),
        EncreFeedbackLearner(storage_path=None),
    )
    assert restarted._policy_reflow is not None
    native = json.loads(_native.permission_get_learned_policies())
    assert any(e["path_scope"] == sensitive and e["policy"] == "deny" for e in native)


# ---------------------------------------------------------------------------
# 6. Anti-dormancy: the reflow is reachable from the production loop
# ---------------------------------------------------------------------------

def test_reflow_is_reachable_from_the_production_loop(loop, tmp_path):
    """The loop really owns a reflow, exposed on the session, under tmp_path."""
    assert loop._policy_reflow is not None
    assert isinstance(loop._policy_reflow, EncreSafetyPolicyReflow)
    assert loop.session.policy_reflow is loop._policy_reflow
    # Never the real user data directory.
    assert str(loop._policy_reflow.storage_path).startswith(str(tmp_path))


def test_loop_without_feedback_learner_still_builds(tmp_path, monkeypatch, policy_path):
    """No feedback learner means no reflow, and the hook is a safe no-op."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    bare = _build_loop(_config(), EncreSession(_config()), None)

    assert bare._policy_reflow is None
    assert bare.feedback is None
    bare._dispatch_safety_reflow()  # must not raise
    assert bare._last_reflow_record_count == -1
    assert not policy_path.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-header"]))
