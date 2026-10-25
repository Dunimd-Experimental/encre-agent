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

"""Metacognitive capability tracking for the evolution subsystem.

:class:`EncreMetaCognition` classifies each turn into skill domains (using
:data:`_DOMAIN_TAXONOMY`), tracks a rolling :class:`CapabilityProfile`
per domain, and reports known weaknesses / delegation suggestions so the
agent can be cautious or hand work off when its capability is low.
"""

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class CapabilityProfile:
    """Rolling performance profile for a single skill domain.

    The :meth:`update` method blends a fresh success/failure signal into the
    running ``score`` (exponential moving average) and raises ``confidence``
    as more samples accumulate.
    """
    domain: str
    score: float = 0.5
    confidence: float = 0.0
    sample_count: int = 0
    success_count: int = 0
    last_assessed: float = 0.0

    def update(self, success: bool, difficulty: float = 0.5) -> None:
        """Blend a new outcome into the running score and confidence.

        Difficulty scales the outcome: solving a harder task moves the score
        up more than solving an easy one (and likewise penalises failures on
        easy tasks harder).
        """
        self.sample_count += 1
        if success:
            self.success_count += 1
        raw = self.success_count / self.sample_count if self.sample_count > 0 else 0.5
        weight = 1.0 + min(max(float(difficulty), 0.0), 1.0) * 0.5
        if success:
            blend = raw * weight
        else:
            blend = raw / weight
        self.score = blend * 0.7 + self.score * 0.3
        self.confidence = min(self.sample_count / 20.0, 1.0)
        self.last_assessed = time.time()

    def to_dict(self) -> dict[str, Any]:
        """Serialise the profile to a plain dictionary."""
        return {
            "domain": self.domain,
            "score": self.score,
            "confidence": self.confidence,
            "sample_count": self.sample_count,
            "success_count": self.success_count,
            "last_assessed": self.last_assessed,
        }


# Domain taxonomy: keywords -> domain label
_DOMAIN_TAXONOMY: dict[str, str] = {
    "file read write edit path directory folder": "file_operations",
    "bash shell command execute run script terminal subprocess": "shell_execution",
    "grep search pattern regex find code": "code_search",
    "web fetch http url api request curl download": "web_access",
    "git commit branch merge diff repo repository": "version_control",
    "test unittest pytest coverage assertion verify": "testing",
    "debug traceback error exception fix bug diagnose": "debugging",
    "refactor restructure reorganize clean improve": "refactoring",
    "install package dependency pip npm module library": "dependency_management",
    "database sql query migration schema": "database",
    "docker container image deploy kubernetes": "devops",
    "auth authentication login token session oauth": "authentication",
    "math calculate compute statistics algorithm ml model": "computation",
    "document docstring comment readme documentation": "documentation",
    "config configuration yaml toml json settings": "configuration",
}


class EncreMetaCognition:
    """Tracks per-domain capability and surfaces self-awareness context."""
    def __init__(self) -> None:
        self._profiles: dict[str, CapabilityProfile] = {}
        self._delegation_history: list[dict[str, Any]] = []

    def assess_turn(
        self,
        prompt: str,
        tool_results: list[dict[str, Any]],
    ) -> None:
        """Update capability profiles for the domains touched this turn."""
        domains = _classify_domains(prompt)
        total = len(tool_results)
        errors = sum(1 for r in tool_results if r.get("is_error"))
        success = errors == 0 or (total > 0 and errors / total < 0.3)
        difficulty = _estimate_difficulty(prompt, tool_results)

        for domain in domains:
            if domain not in self._profiles:
                self._profiles[domain] = CapabilityProfile(domain=domain)
            self._profiles[domain].update(success, difficulty)

    def get_profile(self, domain: str | None = None) -> dict[str, Any] | CapabilityProfile:
        """Return one domain's profile (dict) or all profiles keyed by domain."""
        if domain:
            p = self._profiles.get(domain)
            return p.to_dict() if p else {"domain": domain, "score": 0.5, "confidence": 0.0}
        return {d: p.to_dict() for d, p in self._profiles.items()}

    def get_weakness_report(self) -> list[dict[str, Any]]:
        """Return low-scoring, sufficiently-confident domains."""
        weak: list[dict[str, Any]] = []
        for domain, profile in self._profiles.items():
            if profile.confidence > 0.3 and profile.score < 0.4:
                weak.append({
                    "domain": domain,
                    "score": profile.score,
                    "confidence": profile.confidence,
                    "samples": profile.sample_count,
                })
        weak.sort(key=lambda x: x["score"])
        return weak

    def should_delegate(self, task_description: str) -> tuple[bool, str]:
        """Decide whether a task should be delegated (avg capability too low)."""
        domains = _classify_domains(task_description)
        scores: list[float] = []
        for domain in domains:
            profile = self._profiles.get(domain)
            if profile and profile.confidence > 0.3:
                scores.append(profile.score)
        if not scores:
            return False, ""
        avg = sum(scores) / len(scores)
        if avg < 0.35:
            return True, f"Low capability ({avg:.2f}) in domains: {', '.join(domains)}"
        return False, ""

    def get_self_awareness_context(self) -> str:
        """Return a prompt snippet listing known weaknesses, or empty string."""
        weak = self.get_weakness_report()
        if not weak:
            return ""
        lines = ["**Self-awareness: known weaknesses**"]
        for w in weak[:
            5]:
            lines.append(f"  - {w['domain']}: score={w['score']:.2f} (n={w['samples']})")
        lines.append("Proceed carefully in these domains: gather more context, use the appropriate tools, or ask the user for help.")
        return "\n".join(lines)

    def record_delegation(self, task: str, delegate: str, success: bool) -> None:
        """Record an outsourcing event (bounded to the last 100)."""
        self._delegation_history.append({
            "task": task[:200],
            "delegate": delegate,
            "success": success,
            "timestamp": time.time(),
        })
        if len(self._delegation_history) > 100:
            self._delegation_history.pop(0)

    def reset(self) -> None:
        """Clear all profiles and delegation history."""
        self._profiles.clear()
        self._delegation_history.clear()


def _classify_domains(text: str) -> list[str]:
    """Map free text to one or more skill-domain labels."""
    matched: list[str] = []
    text_lower = text.lower()
    for keywords, domain in _DOMAIN_TAXONOMY.items():
        for kw in keywords.split():
            if kw in text_lower:
                matched.append(domain)
                break
    return matched if matched else ["general"]


def _estimate_difficulty(prompt: str, tool_results: list[dict[str, Any]]) -> float:
    """Heuristically score task difficulty in the 0.3-0.95 range."""
    score = 0.3
    if len(prompt) > 500:
        score += 0.1
    if len(prompt) > 2000:
        score += 0.1
    tools = len(tool_results)
    if tools > 3:
        score += 0.1
    if tools > 8:
        score += 0.1
    errors = sum(1 for r in tool_results if r.get("is_error"))
    if errors > 0:
        score += 0.1 * errors
    return min(score, 0.95)
