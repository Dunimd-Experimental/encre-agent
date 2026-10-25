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

"""Tests for the goal system: definitions, results, statuses, events, and runners."""

import tempfile

import pytest


class TestGoalStatus:
    """Exercise :class:`GoalStatus` to confirm it is a well-formed enum.

    Three scenarios are covered: member presence, enum membership, and
    string/name conversion. Together they prove the status type is safe to use
    as a discriminating key in pattern matches and serialization, which
    requires stable identifiers so goal-state persistence can be compared
    across restarts.
    """

    def test_verify_all_status_members_are_present_and_not_none(self):
        """Assert every expected :class:`GoalStatus` member exists and is not None."""
        from encre.goal import GoalStatus

        # A missing member would indicate an incomplete enum definition and would
        # raise KeyError or AttributeError in any code that branches on the status.
        assert GoalStatus.PENDING is not None
        assert GoalStatus.IN_PROGRESS is not None
        assert GoalStatus.SUCCESS is not None
        assert GoalStatus.FAILED is not None
        assert GoalStatus.TIMEOUT is not None
        assert GoalStatus.MAX_ATTEMPTS is not None

    def test_verify_goal_status_is_subclass_of_enum(self):
        """Assert :class:`GoalStatus` is a true :class:`enum.Enum` subclass."""
        from enum import Enum

        from encre.goal import GoalStatus

        # The runner drives state transitions with enum comparison (is, ==), so the
        # type must be a real Enum rather than an IntEnum or a string wrapper.
        assert issubclass(GoalStatus, Enum)

    def test_verify_status_string_conversion_preserves_name(self):
        """Assert ``str()`` yields 'GoalStatus.X' and ``.name`` yields the bare member name."""
        from encre.goal import GoalStatus

        # str() serves human-readable logs while .name serves stable code comparisons.
        assert str(GoalStatus.PENDING) == "GoalStatus.PENDING"
        status = GoalStatus.SUCCESS
        assert status.name == "SUCCESS"


class TestGoalDefinition:
    """Exercise :class:`GoalDefinition` construction, defaults, and dataclass identity.

    Six scenarios are covered: minimal construction, default values, full
    construction, dataclass identity, and custom timeout and max_attempts.
    The schema is expected to be stable, with defaults that provide a safe
    ceiling (20 attempts, 3600s) for long-running autonomous goals without
    forcing explicit configuration on simple ones.
    """

    def test_verify_minimal_construction_preserves_required_fields(self):
        """Assert only ``description`` and ``success_criteria`` are required and stored verbatim."""
        from encre.goal import GoalDefinition

        # These two fields are the only mandatory inputs; everything else must come
        # from defaults, so the minimal path must not demand extra arguments.
        goal = GoalDefinition(
            description="Test goal",
            success_criteria="Tests pass",
        )
        assert goal.description == "Test goal"
        assert goal.success_criteria == "Tests pass"

    def test_verify_default_values_are_sensible_for_long_running_goals(self):
        """Assert the documented defaults are applied for the optional fields."""
        from encre.goal import GoalDefinition

        # The defaults form the safety envelope: too low and goals fail prematurely,
        # too high and the agent wastes resources.
        goal = GoalDefinition(
            description="Default test",
            success_criteria="All good",
        )
        assert goal.max_attempts == 20
        assert goal.timeout_seconds == 3600
        assert goal.evaluator_model == ""
        assert goal.evaluator_provider == ""

    def test_verify_full_construction_preserves_all_overrides(self):
        """Assert every explicitly provided field is stored, including evaluator settings."""
        from encre.goal import GoalDefinition

        # Custom evaluators and tighter timeouts are needed for production goals that
        # run against real codebases, so the full path must accept every knob.
        goal = GoalDefinition(
            description="Complex task",
            success_criteria="Zero errors, all features work",
            max_attempts=10,
            timeout_seconds=1800,
            evaluator_model="gpt-4o-mini",
            evaluator_provider="openai",
        )
        assert goal.description == "Complex task"
        assert goal.success_criteria == "Zero errors, all features work"
        assert goal.max_attempts == 10
        assert goal.timeout_seconds == 1800
        assert goal.evaluator_model == "gpt-4o-mini"
        assert goal.evaluator_provider == "openai"

    def test_verify_goal_definition_is_dataclass(self):
        """Assert :class:`GoalDefinition` is a real dataclass."""
        from dataclasses import is_dataclass

        from encre.goal import GoalDefinition

        # The runner builds definitions dynamically and relies on the generated
        # __init__, __repr__, and structural comparison for serialization.
        assert is_dataclass(GoalDefinition)

    def test_verify_non_default_timeout_is_preserved(self):
        """Assert a custom ``timeout_seconds`` is stored without being clamped."""
        from encre.goal import GoalDefinition

        # Short-running goals such as smoke tests need a tighter timeout than the
        # 3600s default, so explicit overrides must win over defaults.
        goal = GoalDefinition(
            description="Quick task",
            success_criteria="Done",
            timeout_seconds=300,
        )
        assert goal.timeout_seconds == 300

    def test_verify_non_default_max_attempts_is_preserved(self):
        """Assert a custom ``max_attempts`` is stored without being clamped."""
        from encre.goal import GoalDefinition

        # Complex multi-step goals may need more than the 20-attempt default to
        # converge, so explicit overrides must win over defaults.
        goal = GoalDefinition(
            description="Many attempts",
            success_criteria="Eventually",
            max_attempts=100,
        )
        assert goal.max_attempts == 100


class TestGoalResult:
    """Exercise :class:`GoalResult` construction, defaults, and milestone mutability.

    Seven scenarios are covered: default construction, full construction, failed
    status, timeout status, max-attempts status, milestone mutability, and
    dataclass identity. The result shape must stay stable for both success and
    failure reporting, with ``milestones`` kept as a mutable list so the
    runner can append progress markers while executing.
    """

    def test_verify_default_construction_sets_all_optional_fields_to_empty_or_zero(self):
        """Assert all optional fields fall back to their documented defaults."""
        from encre.goal import GoalResult, GoalStatus

        # Results are often created upfront and populated incrementally, so empty
        # defaults prevent AttributeError on the very first access.
        result = GoalResult(status=GoalStatus.PENDING)
        assert result.status == GoalStatus.PENDING
        assert result.summary == ""
        assert result.attempts == 0
        assert result.elapsed_seconds == 0.0
        assert result.final_output == ""
        assert result.milestones == []

    def test_verify_full_construction_preserves_all_fields(self):
        """Assert every explicitly provided field, including milestones, is preserved."""
        from encre.goal import GoalResult, GoalStatus

        # The result is serialized into goal history, so it must keep the exact
        # values the runner reports rather than normalizing them away.
        result = GoalResult(
            status=GoalStatus.SUCCESS,
            summary="All tests passed",
            attempts=3,
            elapsed_seconds=45.2,
            final_output="Task completed successfully",
            milestones=["Step 1 done", "Step 2 done", "All done"],
        )
        assert result.status == GoalStatus.SUCCESS
        assert result.summary == "All tests passed"
        assert result.attempts == 3
        assert result.elapsed_seconds == 45.2
        assert result.final_output == "Task completed successfully"
        assert len(result.milestones) == 3
        assert "Step 1 done" in result.milestones

    def test_verify_failed_status_result_carries_summary_and_attempt_count(self):
        """Assert a FAILED result preserves both the summary and the attempt count."""
        from encre.goal import GoalResult, GoalStatus

        # Failure reports are the primary diagnostic output: they must say why the
        # goal failed and how many attempts were consumed.
        result = GoalResult(
            status=GoalStatus.FAILED,
            summary="Could not complete the task",
            attempts=10,
            elapsed_seconds=600.0,
        )
        assert result.status == GoalStatus.FAILED
        assert result.attempts == 10

    def test_verify_timeout_status_result_carries_elapsed_seconds(self):
        """Assert a TIMEOUT result carries the elapsed time that breached the limit."""
        from encre.goal import GoalResult, GoalStatus

        # Callers need to know how close the goal came to completion before the
        # timeout fired, so the elapsed figure must survive serialization.
        result = GoalResult(
            status=GoalStatus.TIMEOUT,
            summary="Timed out",
            elapsed_seconds=3601.0,
        )
        assert result.status == GoalStatus.TIMEOUT

    def test_verify_max_attempts_status_result_carries_attempt_count(self):
        """Assert a MAX_ATTEMPTS result records the exhausted attempt count."""
        from encre.goal import GoalResult, GoalStatus

        # The FAILED versus MAX_ATTEMPTS distinction is which ceiling was hit first,
        # so the attempt count is what carries that boundary information.
        result = GoalResult(
            status=GoalStatus.MAX_ATTEMPTS,
            summary="Reached max attempts",
            attempts=20,
        )
        assert result.status == GoalStatus.MAX_ATTEMPTS

    def test_verify_milestones_list_is_mutable_after_construction(self):
        """Assert the milestones list can be appended to after construction."""
        from encre.goal import GoalResult, GoalStatus

        # The runner must record progress markers during execution, so the container
        # must be a real mutable list rather than a frozen tuple.
        result = GoalResult(status=GoalStatus.IN_PROGRESS, milestones=["started"])
        result.milestones.append("middle")
        result.milestones.append("almost done")
        assert len(result.milestones) == 3

    def test_verify_goal_result_is_dataclass(self):
        """Assert :class:`GoalResult` is a real dataclass."""
        from dataclasses import is_dataclass

        from encre.goal import GoalResult

        # The runner builds results dynamically and depends on dataclass behavior
        # when serializing them to the goal history store.
        assert is_dataclass(GoalResult)


class TestGoalEvent:
    """Exercise :class:`GoalEvent` construction and defaults for the observer pipeline.

    Five scenarios are covered: full construction, defaults, a success event, a
    failed event, and dataclass identity. Events must carry enough context for
    downstream observers to render progress UI and audit logs without extra
    lookups, while ``attempt`` and ``message`` stay optional so high-level
    status transitions need not report per-attempt detail.
    """

    def test_verify_construction_preserves_status_attempt_and_message(self):
        """Assert status, attempt, and message are stored verbatim when supplied."""
        from encre.goal import GoalEvent, GoalStatus

        # The observer pipeline routes on these three fields to drive the goal
        # progress widget, so none of them may be dropped or renamed.
        event = GoalEvent(
            status=GoalStatus.IN_PROGRESS,
            attempt=1,
            message="Working on it",
        )
        assert event.status == GoalStatus.IN_PROGRESS
        assert event.attempt == 1
        assert event.message == "Working on it"

    def test_verify_default_values_are_zero_and_empty_for_optional_fields(self):
        """Assert attempt defaults to 0 and message defaults to an empty string."""
        from encre.goal import GoalEvent, GoalStatus

        # High-level transitions such as PENDING -> IN_PROGRESS do not always have an
        # associated attempt number, so the minimal construction path must be safe.
        event = GoalEvent(status=GoalStatus.PENDING)
        assert event.attempt == 0
        assert event.message == ""

    def test_verify_success_event_carries_attempt_and_message(self):
        """Assert a SUCCESS event preserves the attempt count and completion message."""
        from encre.goal import GoalEvent, GoalStatus

        # The success event is the terminal signal, so it must be fully informative
        # rather than a bare status flip.
        event = GoalEvent(
            status=GoalStatus.SUCCESS,
            attempt=5,
            message="Goal achieved",
        )
        assert event.status == GoalStatus.SUCCESS
        assert event.attempt == 5

    def test_verify_failed_event_carries_attempt_and_message(self):
        """Assert a FAILED event preserves the attempt count and failure message."""
        from encre.goal import GoalEvent, GoalStatus

        # Failure events are the main input to the goal auditor, so the diagnostic
        # detail must not be lost on construction.
        event = GoalEvent(
            status=GoalStatus.FAILED,
            attempt=20,
            message="All attempts exhausted",
        )
        assert event.status == GoalStatus.FAILED

    def test_verify_goal_event_is_dataclass(self):
        """Assert :class:`GoalEvent` is a real dataclass."""
        from dataclasses import is_dataclass

        from encre.goal import GoalEvent

        # Events are collected into lists and compared structurally by the observer
        # pipeline, which relies on dataclass equality.
        assert is_dataclass(GoalEvent)


class TestEncreGoalRunnerConstruction:
    """Exercise :class:`EncreGoalRunner` construction, injection, and prompt shape.

    Four scenarios are covered: construction from config only, construction with
    all explicit dependencies, the evaluator prompt type, and goal-prompt
    rendering. The runner is expected to be a self-contained orchestrator that
    receives every subsystem at construction time so ``execute()`` has no
    late-binding surprises.
    """

    def test_verify_construction_with_config_only_creates_all_subsystems(self):
        """Assert config-only construction wires non-None default subsystems."""
        from encre.config import EncreConfig
        from encre.goal import EncreGoalRunner

        # The runner must be fully operational without explicit dependency injection,
        # so every subsystem must be materialized from the config alone.
        config = EncreConfig(model="gpt-5.6", backend_type="openai")
        runner = EncreGoalRunner(config=config)
        assert runner is not None
        assert runner.config is config
        assert runner.tool_registry is not None
        assert runner.hook_system is not None
        assert runner.safety is not None
        assert runner.telemetry is not None

    def test_verify_construction_with_all_params_preserves_identity(self):
        """Assert explicitly provided subsystems are stored by identity, not replaced."""
        from encre.config import EncreConfig
        from encre.goal import EncreGoalRunner
        from encre.hooks.system import EncreHookSystem
        from encre.safety import EncreSafetyEngine
        from encre.tools.registry import ToolRegistry

        # Injected subsystems may carry pre-registered state such as hooks and tool
        # registrations, so the runner must keep the caller's instances verbatim.
        config = EncreConfig(model="claude-sonnet-4-20250514", backend_type="anthropic")
        tools = ToolRegistry()
        hooks = EncreHookSystem()
        safety = EncreSafetyEngine(config)

        runner = EncreGoalRunner(
            config=config,
            tool_registry=tools,
            hook_system=hooks,
            safety=safety,
        )
        assert runner.tool_registry is tools
        assert runner.hook_system is hooks
        assert runner.safety is safety

    def test_verify_evaluator_system_prompt_is_nonempty_string_with_expected_keywords(self):
        """Assert the evaluator system prompt is a non-empty string with expected keywords."""
        from encre.config import EncreConfig
        from encre.goal import EncreGoalRunner

        # This prompt drives the LLM judge that decides whether a result meets the
        # success criteria, so it must be wired and recognizably the evaluator prompt.
        config = EncreConfig(model="gpt-5.6", backend_type="openai")
        runner = EncreGoalRunner(config=config)
        assert isinstance(runner.EVALUATOR_SYSTEM_PROMPT, str)
        assert "goal completion evaluator" in runner.EVALUATOR_SYSTEM_PROMPT.lower()

    def test_verify_build_goal_prompt_includes_description_and_success_criteria(self):
        """Assert the rendered goal prompt contains both the description and the criteria."""
        from encre.config import EncreConfig
        from encre.goal import EncreGoalRunner, GoalDefinition

        # The prompt is the primary contract sent to the LLM, so it must carry both
        # the task and the acceptance criteria for the model to judge correctly.
        config = EncreConfig(model="gpt-5.6", backend_type="openai")
        runner = EncreGoalRunner(config=config)
        goal = GoalDefinition(
            description="Implement login",
            success_criteria="Login endpoint works with JWT",
        )
        prompt = runner._build_goal_prompt(goal)
        assert "GOAL: Implement login" in prompt
        assert "SUCCESS CRITERIA: Login endpoint works with JWT" in prompt
        assert "autonomously" in prompt.lower()


class TestEncreGoalLoopConstruction:
    """Exercise :class:`EncreGoalLoop` construction, overrides, and input guards.

    Four scenarios are covered: basic construction, custom parameters, runner
    property delegation, and the missing-description error. The loop is a thin
    orchestrator over the runner and must enforce its own invariants while
    delegating config and subsystem references to the attached agent.
    """

    def test_verify_basic_construction_sets_description_success_criteria_and_defaults(self):
        """Assert construction stores the provided values and applies default budgets."""
        from encre.config import EncreConfig
        from encre.goal import EncreGoalLoop

        config = EncreConfig(model="gpt-5.6", backend_type="openai")

        # A minimal stand-in for the real agent, exposing only the attributes the
        # loop constructor reads.
        class MockAgent:
            """Minimal stand-in for :class:`EncreAgent` exposing loop constructor inputs."""

            def __init__(self):
                """Populate the subsystem attributes the goal loop expects on the agent."""
                self.config = config
                from encre.hooks.system import EncreHookSystem
                from encre.memdir.system import EncreMemorySystem
                from encre.safety import EncreSafetyEngine
                from encre.skills.registry import EncreSkillRegistry
                from encre.telemetry import EncreTelemetry
                from encre.tools.registry import ToolRegistry

                self.tool_registry = ToolRegistry()
                self.hook_system = EncreHookSystem()
                self.safety = EncreSafetyEngine(config)
                self.memory_system = EncreMemorySystem(auto_memory_path=tempfile.mkdtemp())
                self.skill_registry = EncreSkillRegistry()
                self.telemetry = EncreTelemetry(enabled=False)

        # The loop must be immediately executable after construction, with no extra
        # configuration step required before execute() can run.
        agent = MockAgent()
        loop = EncreGoalLoop(
            agent=agent,
            description="Test description",
            success_criteria="Test criteria",
        )
        assert loop is not None
        assert loop._description == "Test description"
        assert loop._success_criteria == "Test criteria"
        assert loop._max_attempts == 20
        assert loop._timeout_seconds == 3600
        assert loop.runner is not None

    def test_verify_construction_with_custom_params_applies_overrides(self):
        """Assert explicit ``max_attempts`` and ``timeout_seconds`` replace the defaults."""
        from encre.config import EncreConfig
        from encre.goal import EncreGoalLoop

        config = EncreConfig(model="gpt-5.6", backend_type="openai")

        class MockAgent:
            """Minimal stand-in for :class:`EncreAgent` exposing loop constructor inputs."""

            def __init__(self):
                """Populate the subsystem attributes the goal loop expects on the agent."""
                self.config = config
                from encre.hooks.system import EncreHookSystem
                from encre.memdir.system import EncreMemorySystem
                from encre.safety import EncreSafetyEngine
                from encre.skills.registry import EncreSkillRegistry
                from encre.telemetry import EncreTelemetry
                from encre.tools.registry import ToolRegistry

                self.tool_registry = ToolRegistry()
                self.hook_system = EncreHookSystem()
                self.safety = EncreSafetyEngine(config)
                self.memory_system = EncreMemorySystem(auto_memory_path=tempfile.mkdtemp())
                self.skill_registry = EncreSkillRegistry()
                self.telemetry = EncreTelemetry(enabled=False)

        # Long-running goals need tighter control over retry and timeout budgets, so
        # caller-supplied values must take precedence over the built-in defaults.
        agent = MockAgent()
        loop = EncreGoalLoop(
            agent=agent,
            description="Custom desc",
            success_criteria="Custom criteria",
            max_attempts=5,
            timeout_seconds=600,
        )
        assert loop._max_attempts == 5
        assert loop._timeout_seconds == 600

    def test_verify_runner_delegates_config_and_subsystem_references_to_agent(self):
        """Assert the internal runner shares the agent's config and subsystem instances."""
        from encre.config import EncreConfig
        from encre.goal import EncreGoalLoop

        config = EncreConfig(model="gpt-5.6", backend_type="openai")

        class MockAgent:
            """Minimal stand-in for :class:`EncreAgent` exposing loop constructor inputs."""

            def __init__(self):
                """Populate the subsystem attributes the goal loop expects on the agent."""
                self.config = config
                from encre.hooks.system import EncreHookSystem
                from encre.memdir.system import EncreMemorySystem
                from encre.safety import EncreSafetyEngine
                from encre.skills.registry import EncreSkillRegistry
                from encre.telemetry import EncreTelemetry
                from encre.tools.registry import ToolRegistry

                self.tool_registry = ToolRegistry()
                self.hook_system = EncreHookSystem()
                self.safety = EncreSafetyEngine(config)
                self.memory_system = EncreMemorySystem(auto_memory_path=tempfile.mkdtemp())
                self.skill_registry = EncreSkillRegistry()
                self.telemetry = EncreTelemetry(enabled=False)

        # The runner must operate on the agent's own registrations and hook handlers,
        # so it shares subsystem state instead of duplicating it.
        agent = MockAgent()
        loop = EncreGoalLoop(agent=agent)
        assert loop.runner.config is agent.config
        assert loop.runner.tool_registry is agent.tool_registry
        assert loop.runner.hook_system is agent.hook_system

    @pytest.mark.asyncio
    async def test_verify_execute_raises_when_description_is_missing(self):
        """Assert ``execute()`` raises ValueError when no description was ever supplied."""
        import pytest
        from encre.config import EncreConfig
        from encre.goal import EncreGoalLoop

        config = EncreConfig(model="gpt-5.6", backend_type="openai")

        class MockAgent:
            """Minimal stand-in for :class:`EncreAgent` exposing loop constructor inputs."""

            def __init__(self):
                """Populate the subsystem attributes the goal loop expects on the agent."""
                self.config = config
                from encre.hooks.system import EncreHookSystem
                from encre.memdir.system import EncreMemorySystem
                from encre.safety import EncreSafetyEngine
                from encre.skills.registry import EncreSkillRegistry
                from encre.telemetry import EncreTelemetry
                from encre.tools.registry import ToolRegistry

                self.tool_registry = ToolRegistry()
                self.hook_system = EncreHookSystem()
                self.safety = EncreSafetyEngine(config)
                self.memory_system = EncreMemorySystem(auto_memory_path=tempfile.mkdtemp())
                self.skill_registry = EncreSkillRegistry()
                self.telemetry = EncreTelemetry(enabled=False)

        # A goal without a description is semantically undefined and must not proceed
        # silently to the LLM, so the guard fires at execution time.
        agent = MockAgent()
        loop = EncreGoalLoop(agent=agent)

        with pytest.raises(ValueError, match="description"):
            # No description is provided either here or at construction time.
            await loop.execute()
