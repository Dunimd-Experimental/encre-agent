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

"""PolicyDecision: fail-closed semantics for every permission answer."""

from __future__ import annotations

import pytest

from encre.contracts.decisions import DecisionKind, PolicyDecision


def test_allow_is_the_only_affirmative_answer() -> None:
    assert PolicyDecision.allow("safe read").is_allowed
    assert not PolicyDecision.ask("needs confirmation").is_allowed
    assert not PolicyDecision.deny("blocked").is_allowed
    assert PolicyDecision.ask("x").needs_confirmation
    assert not PolicyDecision.allow("x").needs_confirmation


def test_reason_is_required() -> None:
    with pytest.raises(ValueError):
        PolicyDecision(decision=DecisionKind.ALLOW, reason="")


def test_risk_is_bounded() -> None:
    with pytest.raises(ValueError):
        PolicyDecision(decision=DecisionKind.ALLOW, reason="x", risk=1.5)
    with pytest.raises(ValueError):
        PolicyDecision(decision=DecisionKind.ALLOW, reason="x", risk=-0.1)
    assert PolicyDecision.allow("x", risk=1.0).risk == 1.0


def test_unknown_decision_strings_fail_closed() -> None:
    decision = PolicyDecision.from_dict(
        {"decision": "maybe", "reason": "from a newer policy engine"}
    )
    assert decision.decision is DecisionKind.DENY
    assert not decision.is_allowed


def test_roundtrip_and_enum_serialisation() -> None:
    original = PolicyDecision.deny("shell blocked", policy_id="safety.shell", risk=0.9)
    restored = PolicyDecision.from_dict(original.to_dict())
    assert restored.decision is DecisionKind.DENY
    assert restored.reason == "shell blocked"
    assert restored.policy_id == "safety.shell"
    assert restored.risk == 0.9


def test_fail_closed_helper() -> None:
    decision = PolicyDecision.fail_closed("policy engine crashed")
    assert decision.decision is DecisionKind.DENY
    assert "crashed" in decision.reason
