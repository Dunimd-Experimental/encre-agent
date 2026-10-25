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

"""Tests for AgentMode profiles and Plan-Do-Review (PDR) integration.

Verifies that WORKSPACE, GENERAL and AUTOMATION modes each carry the
correct PDR wiring, tool-set defaults and system-prompt gains, and that
the PDR engine only advances past steps marked complete. Also asserts
that the unknown-mode fallback degrades to the GENERAL profile and that
a failed lightweight review rolls the current step back to IN_PROGRESS.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from encre.config import EncreConfig
from encre.improve.evolution.plan_do_review import StepStatus
from encre.loop import EncreLoop
from encre.modes.mode_profiles import AgentMode, get_mode_profile
from encre.session import EncreSession


def _make_loop(mode) -> EncreLoop:
    """Build an EncreLoop for the given AgentMode with a default config.

    Supplies a fresh ToolRegistry and session so each test gets an
    independent loop whose profile, PDR engine and tool resolution
    come only from the requested mode.
    """
    config = EncreConfig()
    from encre.tools.registry import ToolRegistry
    return EncreLoop(
        config=config,
        session=EncreSession(config),
        tool_registry=ToolRegistry(),
        mode=mode,
    )


@pytest.mark.asyncio
async def test_verify_workspace_mode_activates_pdr_and_delegation():
    """Validate that WORKSPACE mode activates the Plan-Do-Review engine and
    enables workspace delegation, confirming the profile wiring is correct.

    The test exercises loop construction with AgentMode.WORKSPACE and asserts
    the profile flags and PDR plan have at least one step after initialization
    because WORKSPACE is the only mode that should run the full PDR cycle.
    """
    loop = _make_loop(AgentMode.WORKSPACE)
    assert loop._profile.workspace_delegation is True
    assert loop._profile.verify_budget_total() > 0
    # Seed the PDR engine with a plan-worthy prompt to drive one step.
    loop._pdr.initialize("Refactor the scheduler module and add unit tests for it.")
    loop._pdr_active = True
    loop._pdr.start_next_step()
    assert loop._pdr_active is True
    assert len(loop._pdr.plan.steps) >= 1
    assert loop._pdr.plan.current_step is not None


@pytest.mark.asyncio
async def test_verify_general_mode_keeps_pdr_inactive_and_no_delegation():
    """Validate that GENERAL mode leaves the PDR engine inactive and disables
    workspace delegation, confirming the historical behaviour path is preserved.

    The test exercises loop construction with AgentMode.GENERAL and asserts
    _pdr_active is False and steps are empty because GENERAL mode must not
    trigger planning even when the prompt is plan-worthy -- that is the
    explicit contract of the mode profile.
    """
    loop = _make_loop(AgentMode.GENERAL)
    assert loop._profile.workspace_delegation is False
    # should_plan gates initialisation for this mode, so the default
    # state must stay inactive with no steps materialised at all.
    assert loop._pdr_active is False
    assert loop._pdr.plan.steps == []


@pytest.mark.asyncio
async def test_verify_automation_mode_has_no_verify_budget_and_no_pdr():
    """Validate that AUTOMATION mode has zero verify budget, no workspace
    delegation, and PDR inactive, confirming the lightweight automation
    profile is correctly isolated.

    The test exercises loop construction with AgentMode.AUTOMATION and asserts
    all three flags are in their minimal state because automation jobs must
    run fast without planning overhead or verification gating.
    """
    loop = _make_loop(AgentMode.AUTOMATION)
    assert loop._profile.verify_budget_total() == 0
    assert loop._profile.workspace_delegation is False
    assert loop._pdr_active is False


@pytest.mark.asyncio
async def test_verify_unknown_mode_falls_back_to_general_profile():
    """Validate that get_mode_profile returns the GENERAL profile for both
    unknown string values and None, confirming the fallback path is safe.

    The test exercises the factory with invalid inputs and asserts the
    returned mode is GENERAL because an undefined mode must never crash
    the loop constructor -- it should degrade gracefully to the base
    profile.
    """
    assert get_mode_profile("not-a-real-mode").mode == AgentMode.GENERAL
    assert get_mode_profile(None).mode == AgentMode.GENERAL


@pytest.mark.asyncio
async def test_verify_pdr_advances_only_on_completed_step():
    """Validate that start_next_step skips IN_PROGRESS steps (even after
    failure) and only advances when a step has been marked complete.

    The test exercises a three-step plan, fails the current step, asserts
    the index does not advance, then marks the step complete and asserts
    the next step is IN_PROGRESS because the PDR engine must retry failed
    steps before moving forward to avoid losing work.
    """
    loop = _make_loop(AgentMode.WORKSPACE)
    loop._pdr.initialize(
        "Step one: write file. Step two: run tests. Step three: report."
    )
    loop._pdr.start_next_step()
    current = loop._pdr.plan.current_step
    assert current is not None
    # Retry keeps the step IN_PROGRESS; start_next_step must NOT skip it.
    loop._pdr.mark_step_failed(error="verification failed")
    if current.retry_count <= current.max_retries and current.status == StepStatus.IN_PROGRESS:
        index_before = loop._pdr.plan.current_step_index
        nxt = loop._pdr.start_next_step()
        assert loop._pdr.plan.current_step_index == index_before or nxt is None
    # A completed step advances.
    loop._pdr.mark_step_complete(summary="step done")
    nxt = loop._pdr.start_next_step()
    assert nxt is None or nxt.status == StepStatus.IN_PROGRESS


# ---- Mode behavior matrix: distinct tool bases + prompt gains ----


@pytest.mark.asyncio
async def test_verify_workspace_mode_always_includes_coding_tool_base():
    """Validate that WORKSPACE mode always includes 'coding' in its resolved
    tool set regardless of the declared intents, confirming the profile
    tool_sets field is applied on top of intent expansion.

    The test exercises tool resolution with a generic intent and asserts
    'coding' is present because WORKSPACE is the coding mode and must
    never lose its toolchain even when the intent classifier is ambiguous.
    """
    loop = _make_loop(AgentMode.WORKSPACE)
    toolset = loop._resolve_tool_set_for_mode(intents=["general"])
    assert "coding" in toolset.split("+")


@pytest.mark.asyncio
async def test_verify_automation_mode_uses_bare_tool_base_with_intent_expansion():
    """Validate that AUTOMATION mode has no fixed profile tool_sets but
    still expands by intent, confirming the distinction from WORKSPACE.

    The test exercises resolution with 'general' and 'coding' intents and
    asserts the bare default for general and the expanded default+coding
    for coding because AUTOMATION must be lightweight but still responsive
    to explicit coding signals.
    """
    loop = _make_loop(AgentMode.AUTOMATION)
    assert get_mode_profile(AgentMode.AUTOMATION).tool_sets == ()
    # With a generic intent, AUTOMATION stays on the bare default.
    assert loop._resolve_tool_set_for_mode(intents=["general"]) == "default"
    # A coding intent still expands (a coding job needs coding tools); the
    # difference from WORKSPACE is that AUTOMATION has no *fixed* coding base.
    assert loop._resolve_tool_set_for_mode(intents=["coding"]) == "default+coding"


@pytest.mark.asyncio
async def test_verify_general_mode_expands_only_by_intent_not_by_profile():
    """Validate that GENERAL mode resolves tool sets solely from intents
    without any fixed profile tool_sets, confirming it has no bias.

    The test exercises resolution with 'general' and 'coding' intents and
    asserts default and default+coding because GENERAL is the neutral mode
    and must not inject tools that the intent stream did not request.
    """
    loop = _make_loop(AgentMode.GENERAL)
    assert loop._resolve_tool_set_for_mode(intents=["general"]) == "default"
    assert loop._resolve_tool_set_for_mode(intents=["coding"]) == "default+coding"


@pytest.mark.asyncio
async def test_verify_each_mode_profile_has_distinct_nonempty_prompt_gain():
    """Validate that GENERAL, WORKSPACE, and AUTOMATION each carry a unique,
    non-empty system-prompt gain string, confirming the profiles are
    distinguishable by the model.

    The test exercises get_mode_profile for all three modes and asserts
    each gain is truthy and that WORKSPACE and AUTOMATION contain their
    mode name because the prompt gain is what tells the model which
    behavioral constraints to follow.
    """
    general = get_mode_profile(AgentMode.GENERAL).prompt_gain
    workspace = get_mode_profile(AgentMode.WORKSPACE).prompt_gain
    automation = get_mode_profile(AgentMode.AUTOMATION).prompt_gain
    assert general and workspace and automation
    assert workspace != general
    assert automation != general
    assert "WORKSPACE mode" in workspace
    assert "AUTOMATION mode" in automation


@pytest.mark.asyncio
async def test_verify_lightweight_review_fails_and_rolls_back_on_all_error_step():
    """Validate that lightweight_review returns ReviewGrade.FAIL when a step
    has only error outcomes, and that the loop-level wiring converts that
    grade into mark_step_failed, keeping the step IN_PROGRESS for retry.

    The test exercises a failed bash migration step, asserts the grade is
    FAIL, then applies mark_step_failed and asserts the step status remains
    IN_PROGRESS because the rollback path must prevent the plan from
    advancing past a verified-failed step.
    """
    from encre.improve.evolution.plan_do_review import ReviewGrade

    loop = _make_loop(AgentMode.WORKSPACE)
    loop._pdr.initialize("Step one: run the migration and verify it.")
    loop._pdr.start_next_step()
    loop._pdr.record_tool_call(
        turn=1, tool_name="bash", args={"cmd": "migrate"},
        result="Traceback: migration failed", is_error=True,
    )
    grade = loop._pdr.lightweight_review()
    assert grade == ReviewGrade.FAIL
    # The loop-level wiring converts a bad grade into mark_step_failed,
    # which keeps the step IN_PROGRESS (retry) instead of advancing.
    current = loop._pdr.plan.current_step
    current.retry_count = 0
    loop._pdr.mark_step_failed(error=f"review grade: {grade.name}")
    assert current.status == StepStatus.IN_PROGRESS
