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

"""Per-mode capability profiles for the Encre agent.

Encre is one agent engine with deliberately distinct capability profiles:

* GENERAL mode  -- delivers artifacts, speed/performance balanced. The
  benchmark targets are general-purpose agents (Manus, ChatGPT).
* WORKSPACE mode -- a stricter profile layered on top of GENERAL that
  delivers production-grade artifacts, effect-first. The benchmark targets
  are vertical domain agents (OpenCode, Claude Code, Codex). It enables the
  planner/architect contract loop and hard verification gating.
* AUTOMATION mode -- directly reuses the GENERAL engine but runs headless
  (no user to answer verify nudges or approve plans). It disables the
  interactive gates and lowers parallel fan-out so background jobs are
  cheap and quiet.

Every mutually-exclusive tuning knob in the engine (auto-continue vs
verify gates, parallel fan-out vs stability, compaction vs delivery
evidence) is resolved to a concrete value per profile here, so the engine
logic reads one source of truth instead of scattered module constants.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentMode(str, Enum):
    """The three capability profiles of the single Encre engine."""

    GENERAL = "general"
    WORKSPACE = "workspace"
    AUTOMATION = "automation"


@dataclass(frozen=True)
class ModeProfile:
    """Resolved tuning knobs for one capability profile.

    All fields have defaults equal to the pre-profile GENERAL behaviour so
    constructing a profile never changes existing behaviour by accident.
    """

    mode: AgentMode = AgentMode.GENERAL

    # ── Auto-continue (token-budget nudge) ──────────────────────────
    # GENERAL keeps the historical default; WORKSPACE continues longer
    # before stopping; AUTOMATION runs one turn per job by default so a
    # background task never silently re-enters itself.
    auto_continue: bool = True
    auto_continue_min_continues: int = 3
    auto_continue_min_delta: int = 500

    # ── Verification gating (verify-on-stop -> forced review -> hard gate)
    # GENERAL uses soft budgets (the historical default). WORKSPACE raises
    # every budget so a production-grade delivery cannot silently finish
    # with unverified changes. AUTOMATION disables all interactive gates:
    # a headless job has no user to respond to a nudge, and correctness is
    # carried by the job's own exit state instead.
    verify_on_stop_nudges: int = 2
    forced_reviews: int = 1
    verify_hard_gate: int = 4

    # ── Parallel tool fan-out (stability vs throughput) ─────────────
    # GENERAL keeps the historical default (env-tunable). WORKSPACE keeps
    # throughput high for production builds. AUTOMATION lowers it so
    # background jobs share CPU politely.
    parallel_fan_out: int = 10

    # ── Architecture contract loop (planner/architect -> implementation)
    # GENERAL injects a contract if one exists but never forces planning.
    # WORKSPACE additionally auto-delegates planner/architect before heavy
    # execution so the coder is anchored to a written design. AUTOMATION
    # skips contract injection entirely (short headless jobs).
    contract_inject: bool = True
    workspace_delegation: bool = False

    # ── Compaction / delivery-evidence coupling ─────────────────────
    # When True, the loop itself (core arbitration) contributes evidence
    # preservation points before every compaction: verification-ledger
    # notes (unverified files, failed checks), the active Plan-Do-Review
    # state and the token-budget state.  Plugin-reported preserve_points
    # are appended on top as an increment -- plugins extend, never own,
    # evidence protection.
    preserve_verify_evidence: bool = True

    # ── Tool-base differentiation ───────────────────────────────────
    # Extra tool sets always merged into the resolved tool base for this
    # profile (on top of the per-prompt intent-matched sets).  WORKSPACE
    # always has the coding toolchain available; AUTOMATION keeps the bare
    # default so background jobs cannot reach into interactive-only tools.
    tool_sets: tuple[str, ...] = ()

    # ── Mode-specific system-prompt gain ────────────────────────────
    # A short behaviour preamble injected into the system prompt so the
    # model actually *feels* the mode difference (not just tuning knobs).
    prompt_gain: str = ""

    # Open flags exposed to the frontend / scheduler.
    extra: dict[str, Any] = field(default_factory=dict)

    def verify_budget_total(self) -> int:
        return self.verify_on_stop_nudges + self.forced_reviews + self.verify_hard_gate

    def with_extra(self, **kwargs: Any) -> ModeProfile:
        return ModeProfile(
            mode=self.mode,
            auto_continue=self.auto_continue,
            auto_continue_min_continues=self.auto_continue_min_continues,
            auto_continue_min_delta=self.auto_continue_min_delta,
            verify_on_stop_nudges=self.verify_on_stop_nudges,
            forced_reviews=self.forced_reviews,
            verify_hard_gate=self.verify_hard_gate,
            parallel_fan_out=self.parallel_fan_out,
            contract_inject=self.contract_inject,
            workspace_delegation=self.workspace_delegation,
            preserve_verify_evidence=self.preserve_verify_evidence,
            tool_sets=self.tool_sets,
            prompt_gain=self.prompt_gain,
            extra={**self.extra, **kwargs},
        )


# Historical GENERAL defaults. Everything matches pre-profile behaviour.
GENERAL_PROFILE = ModeProfile(
    mode=AgentMode.GENERAL,
    auto_continue=True,
    auto_continue_min_continues=3,
    auto_continue_min_delta=500,
    verify_on_stop_nudges=2,
    forced_reviews=1,
    verify_hard_gate=4,
    parallel_fan_out=max(1, int(os.environ.get("ENCRE_MAX_TOOL_USE_CONCURRENCY", "10") or "10")),
    contract_inject=True,
    workspace_delegation=False,
    preserve_verify_evidence=True,
    tool_sets=(),
    prompt_gain=(
        "You are in GENERAL mode: no project workspace is bound. The live "
        "response register still decides what kind of turn this is -- this "
        "mode says WHERE anything produced goes (the session files directory) "
        "and not WHETHER this turn produces something. When it does, the bar "
        "is the same as any production artefact: usable exactly as delivered."
    ),
)

# Production-grade profile: effect-first, heavier verification budgets,
# planner/architect auto-delegation, continued auto-continue.
WORKSPACE_PROFILE = ModeProfile(
    mode=AgentMode.WORKSPACE,
    auto_continue=True,
    auto_continue_min_continues=5,
    auto_continue_min_delta=1000,
    verify_on_stop_nudges=3,
    forced_reviews=2,
    verify_hard_gate=6,
    parallel_fan_out=10,
    contract_inject=True,
    workspace_delegation=True,
    preserve_verify_evidence=True,
    tool_sets=("coding",),
    prompt_gain=(
        "You are in WORKSPACE mode: production-grade, effect-first work inside "
        "a project workspace.  Follow the workspace conventions, keep changes "
        "in scope, and verify your work -- production artifacts are not "
        "finished until the checks pass.  This raises the bar on what counts "
        "as done; it does not turn a question or a conversation into a change "
        "request.  The live response register decides that."
    ),
)

# Headless background profile: no interactive gates, quieter parallelism.
AUTOMATION_PROFILE = ModeProfile(
    mode=AgentMode.AUTOMATION,
    auto_continue=False,
    auto_continue_min_continues=3,
    auto_continue_min_delta=500,
    verify_on_stop_nudges=0,
    forced_reviews=0,
    verify_hard_gate=0,
    parallel_fan_out=4,
    contract_inject=False,
    workspace_delegation=False,
    preserve_verify_evidence=True,
    tool_sets=(),
    prompt_gain=(
        "You are in AUTOMATION mode: a headless background job, always a work "
        "register -- there is no one to chat with or to ask.  Finish the task "
        "in this run, deliver production-grade output rather than a demo, and "
        "report results plainly."
    ),
)


_PROFILES: dict[AgentMode, ModeProfile] = {
    AgentMode.GENERAL: GENERAL_PROFILE,
    AgentMode.WORKSPACE: WORKSPACE_PROFILE,
    AgentMode.AUTOMATION: AUTOMATION_PROFILE,
}


def get_mode_profile(mode: AgentMode | str | None) -> ModeProfile:
    """Resolve a mode value (enum or string) to its profile.

    Unknown or empty values fall back to the GENERAL profile so existing
    call sites keep behaving exactly as before.
    """
    if isinstance(mode, AgentMode):
        return _PROFILES.get(mode, GENERAL_PROFILE)
    if isinstance(mode, str):
        try:
            return _PROFILES[AgentMode(mode.lower())]
        except (ValueError, KeyError):
            return GENERAL_PROFILE
    return GENERAL_PROFILE
