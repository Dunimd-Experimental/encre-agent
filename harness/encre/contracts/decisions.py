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

"""Canonical policy decision contract.

The harness has several safety layers (``safety``, ``autosafety``,
``safety_policy``, plugin guards, SSRF checks).  Each used to answer in its own
shape -- a bool here, a string there, an exception elsewhere.  :class:`PolicyDecision`
is the one answer every layer must produce, so the caller can record *why* an
action was allowed, denied, or escalated, and so an audit log can be replayed.

The three outcomes are exhaustive by design:

* ``ALLOW`` -- proceed without asking.
* ``ASK`` -- proceed only after an explicit human confirmation.
* ``DENY`` -- do not proceed.

Anything that is not clearly ``ALLOW`` must not be treated as allowed: the
default for an unknown or malformed decision is ``DENY`` (fail closed).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = ["DecisionKind", "PolicyDecision"]


class DecisionKind(str, Enum):
    """The three exhaustive policy outcomes."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class PolicyDecision:
    """One auditable policy answer.

    Args:
        decision: The outcome.
        reason: Human-readable justification (never empty).
        policy_id: Stable id of the rule/provider that decided.
        risk: Optional risk score in ``[0, 1]`` used for ranking/escalation.
        metadata: JSON-native supporting evidence (matched pattern, path, ...).
        ts: Decision timestamp.
    """

    decision: DecisionKind
    reason: str
    policy_id: str = ""
    risk: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if isinstance(self.decision, str):
            self.decision = DecisionKind(self.decision)
        if not self.reason.strip():
            raise ValueError("PolicyDecision.reason must be non-empty")
        if not 0.0 <= float(self.risk) <= 1.0:
            raise ValueError("PolicyDecision.risk must be within [0, 1]")

    @property
    def is_allowed(self) -> bool:
        """True only for an explicit ``ALLOW`` (``ASK`` is not allowed yet)."""
        return self.decision is DecisionKind.ALLOW

    @property
    def needs_confirmation(self) -> bool:
        """True when the caller must obtain human confirmation."""
        return self.decision is DecisionKind.ASK

    @classmethod
    def allow(cls, reason: str, **kwargs: Any) -> PolicyDecision:
        """Build an ``ALLOW`` decision."""
        return cls(DecisionKind.ALLOW, reason, **kwargs)

    @classmethod
    def ask(cls, reason: str, **kwargs: Any) -> PolicyDecision:
        """Build an ``ASK`` decision."""
        return cls(DecisionKind.ASK, reason, **kwargs)

    @classmethod
    def deny(cls, reason: str, **kwargs: Any) -> PolicyDecision:
        """Build a ``DENY`` decision."""
        return cls(DecisionKind.DENY, reason, **kwargs)

    @classmethod
    def fail_closed(cls, reason: str, **kwargs: Any) -> PolicyDecision:
        """Build a ``DENY`` decision for an internal error (fail closed)."""
        return cls(DecisionKind.DENY, reason or "policy evaluation failed", **kwargs)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-native dict representation."""
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "policy_id": self.policy_id,
            "risk": float(self.risk),
            "metadata": self.metadata,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PolicyDecision:
        """Build a decision from :meth:`to_dict` output; unknown values deny."""
        try:
            kind = DecisionKind(str(data.get("decision", "deny")))
        except ValueError:
            kind = DecisionKind.DENY
        return cls(
            decision=kind,
            reason=str(data.get("reason") or "unrecognised decision"),
            policy_id=str(data.get("policy_id", "")),
            risk=float(data.get("risk", 0.0) or 0.0),
            metadata=dict(data.get("metadata") or {}),
            ts=float(data.get("ts", 0.0) or 0.0),
        )
