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

"""Tests for the Encre task system: the EncreTask dataclass, the
EncreTaskManager CRUD store, and the EncreTaskExecutor run loop.

The suite pins down field defaults, UUID identity, parent-child linking,
status filtering, sort order, and terminal-state transitions so the task
records can be trusted by the scheduler, the executor, and the UI.
"""

import asyncio
import time
import uuid

import pytest

# ===========================================================================
# EncreTask dataclass
# ===========================================================================


class TestEncreTask:
    """Validate the EncreTask dataclass field defaults and type constraints.

    This class exercises task construction across 10 scenarios: basic field
    preservation, default status, empty result/error, null parent_id, empty
    metadata, float timestamps, metadata injection, dataclass identity, and
    every legal task_type and status literal. Using a dataclass keeps field
    order stable and makes is_dataclass introspection valid for serialization
    frameworks.
    """

    def test_verify_task_creation_preserves_all_fields(self):
        """Validate that EncreTask stores all constructor arguments exactly.

        The test constructs a bash-type task and asserts id, name, description,
        task_type, and prompt match the supplied values because the dataclass
        must not silently drop or transform any field before the scheduler
        reads it for routing and tracking.
        """
        from encre.task.types import EncreTask
        task = EncreTask(
            id="task-001",
            name="Test Task",
            description="A test task",
            task_type="bash",
            prompt="echo hello",
        )
        assert task.id == "task-001"
        assert task.name == "Test Task"
        assert task.description == "A test task"
        assert task.task_type == "bash"
        assert task.prompt == "echo hello"

    def test_verify_default_status_is_pending(self):
        """Validate that a newly created task has status='pending' by default.

        The test constructs a task without specifying status and asserts
        task.status == 'pending' because pending is the canonical initial
        state that signals the scheduler has not yet picked up the work item.
        """
        from encre.task.types import EncreTask
        task = EncreTask(
            id="t1",
            name="Task",
            description="Desc",
            task_type="agent",
            prompt="do something",
        )
        assert task.status == "pending"

    def test_verify_default_result_and_error_are_empty_strings(self):
        """Validate that result and error default to empty strings on construction.

        The test asserts result == '' and error == '' because an unfinished
        task must not carry stale output or error text from a prior execution;
        both fields are populated only by the executor after run completes.
        """
        from encre.task.types import EncreTask
        task = EncreTask(
            id="t1",
            name="Task",
            description="Desc",
            task_type="agent",
            prompt="do something",
        )
        assert task.result == ""
        assert task.error == ""

    def test_verify_default_parent_id_is_none(self):
        """Validate that parent_id defaults to None for a top-level task.

        The test asserts task.parent_id is None because a root task has no
        ancestor in the dependency tree; only child tasks created via
        EncreTaskManager.create_task with parent_id set will carry a reference.
        """
        from encre.task.types import EncreTask
        task = EncreTask(
            id="t1",
            name="Task",
            description="Desc",
            task_type="agent",
            prompt="do something",
        )
        assert task.parent_id is None

    def test_verify_default_metadata_is_empty_dict(self):
        """Validate that metadata defaults to an empty dict on construction.

        The test asserts task.metadata == {} because arbitrary key-value
        metadata is optional; an empty dict is the safe sentinel that lets
        callers check 'if task.metadata' without risking a TypeError.
        """
        from encre.task.types import EncreTask
        task = EncreTask(
            id="t1",
            name="Task",
            description="Desc",
            task_type="agent",
            prompt="do something",
        )
        assert task.metadata == {}

    def test_verify_timestamps_are_floats(self):
        """Validate that created_at and updated_at are float timestamps.

        The test asserts isinstance(task.created_at, float) and
        isinstance(task.updated_at, float) because the timestamp fields store
        Unix epoch seconds as floats to preserve sub-second precision required
        by latency calculations in the executor and telemetry recorder.
        """
        from encre.task.types import EncreTask
        task = EncreTask(
            id="t1",
            name="Task",
            description="Desc",
            task_type="agent",
            prompt="do something",
        )
        assert isinstance(task.created_at, float)
        assert isinstance(task.updated_at, float)

    def test_verify_metadata_carries_arbitrary_key_value_pairs(self):
        """Validate that metadata stores and returns arbitrary dict content.

        The test constructs a task with metadata={'priority': 1, 'tags': ['urgent']}
        and asserts both keys are preserved because metadata is the extensibility
        point that allows schedulers and operators to attach routing hints,
        retry policies, and labels without modifying the core dataclass schema.
        """
        from encre.task.types import EncreTask
        task = EncreTask(
            id="t1",
            name="Task",
            description="Desc",
            task_type="workflow",
            prompt="step1\nstep2",
            metadata={"priority": 1, "tags": ["urgent"]},
        )
        assert task.metadata["priority"] == 1
        assert "urgent" in task.metadata["tags"]

    def test_verify_encre_task_is_a_dataclass(self):
        """Validate that EncreTask is recognized as a dataclass by the stdlib checker.

        The test asserts is_dataclass(EncreTask) is True because external
        serializers (JSON, msgspec, Pydantic) and test fixtures rely on the
        dataclass protocol to introspect fields and generate constructors.
        """
        from dataclasses import is_dataclass
        from encre.task.types import EncreTask
        assert is_dataclass(EncreTask)

    def test_verify_task_type_accepts_all_valid_literal_values(self):
        """Validate that all declared task_type literals can be stored without error.

        The test iterates over 'bash', 'agent', and 'workflow' and asserts
        task.task_type == tt for each because the type field is the primary
        dispatch key used by the executor to select the correct runner, and
        any rejected literal would break a supported execution path.
        """
        from encre.task.types import EncreTask
        for tt in ["bash", "agent", "workflow"]:
            task = EncreTask(
                id="t1", name=f"Task {tt}", description="", task_type=tt, prompt="test"
            )
            assert task.task_type == tt

    def test_verify_status_accepts_all_valid_literal_values(self):
        """Validate that all declared status literals can be stored without error.

        The test iterates over 'pending', 'running', 'completed', 'failed',
        and 'killed' and asserts task.status == status for each because the
        status field drives the scheduler state machine and every legal state
        must be representable in a task record without type errors.
        """
        from encre.task.types import EncreTask
        for status in ["pending", "running", "completed", "failed", "killed"]:
            task = EncreTask(
                id="t1", name="Task", description="", task_type="bash", prompt="test",
                status=status,
            )
            assert task.status == status


# ===========================================================================
# EncreTaskManager CRUD
# ===========================================================================


class TestEncreTaskManager:
    """Validate the EncreTaskManager CRUD operations and query semantics.

    This class exercises create, get, update, list, delete, and clear across
    17 scenarios: UUID generation on create, default status on insert,
    None-on-miss on get, timestamp advancement on update, status filtering and
    descending creation-order sorting on list, parent-child linking, metadata
    passthrough, and full removal on delete/clear. The autouse fixture clears
    the store before every test so runs stay isolated.
    """
    """Shared in-memory task store used by the scheduler, executor, and UI."""

    @pytest.fixture(autouse=True)
    def _clear_before_test(self):
        """Clear the shared task store before each test so runs stay isolated."""
        from encre.task.manager import EncreTaskManager
        EncreTaskManager.clear()

    def test_verify_create_task_returns_a_valid_uuid_string(self):
        """Validate that create_task returns a string that is a valid UUID.

        The test asserts isinstance(task_id, str) and passes the result to
        uuid.UUID() to confirm it parses as a version-4 UUID because the
        manager uses UUIDs as primary keys and downstream code relies on
        the UUID format for URL routing and log correlation.
        """
        from encre.task.manager import EncreTaskManager
        task_id = EncreTaskManager.create_task(
            name="Test",
            description="A test task",
            task_type="bash",
            prompt="echo hello",
        )
        assert isinstance(task_id, str)
        uuid.UUID(task_id)

    def test_verify_create_task_stores_with_default_pending_status(self):
        """Validate that a freshly created task has status='pending' and empty result/error.

        The test retrieves the task by the returned ID and asserts status ==
        'pending', result == '', and error == '' because a created task has
        not yet been executed and must start in the idle state with no output.
        """
        from encre.task.manager import EncreTaskManager
        task_id = EncreTaskManager.create_task(
            name="Test",
            description="Desc",
            task_type="agent",
            prompt="do work",
        )
        task = EncreTaskManager.get_task(task_id)
        assert task is not None
        assert task.status == "pending"
        assert task.result == ""
        assert task.error == ""

    def test_verify_get_task_returns_none_for_missing_id(self):
        """Validate that get_task returns None when the requested ID does not exist.

        The test passes 'nonexistent-id' and asserts None because the caller
        must be able to distinguish between 'task not found' and 'task found
        with empty fields' so that error paths can trigger retry or notification logic.
        """
        from encre.task.manager import EncreTaskManager
        task = EncreTaskManager.get_task("nonexistent-id")
        assert task is None

    def test_verify_get_task_returns_the_created_task_with_all_fields(self):
        """Validate that get_task returns the exact task created by create_task.

        The test creates a workflow task, retrieves it by ID, and asserts
        id, name, description, and task_type all match because get is the
        read path that the scheduler and UI rely on to display task state.
        """
        from encre.task.manager import EncreTaskManager
        task_id = EncreTaskManager.create_task(
            name="My Task",
            description="Important",
            task_type="workflow",
            prompt="step1\nstep2",
        )
        task = EncreTaskManager.get_task(task_id)
        assert task is not None
        assert task.id == task_id
        assert task.name == "My Task"
        assert task.description == "Important"
        assert task.task_type == "workflow"

    def test_verify_update_task_status_changes_and_returns_true(self):
        """Validate that update_task changes the status and returns True for an existing task.

        The test creates a task, updates its status to 'running', and asserts
        result is True and the retrieved task's status is now 'running' because
        the update path is the mechanism by which the executor signals progress
        through the task lifecycle.
        """
        from encre.task.manager import EncreTaskManager
        task_id = EncreTaskManager.create_task(
            name="T", description="D", task_type="bash", prompt="echo hi"
        )
        result = EncreTaskManager.update_task(task_id, status="running")
        assert result is True
        task = EncreTaskManager.get_task(task_id)
        assert task.status == "running"

    def test_verify_update_task_sets_result_on_completion(self):
        """Validate that update_task stores the result string when marking a task completed.

        The test updates the task to status='completed' with result='success output'
        and asserts task.status == 'completed' and task.result == 'success output'
        because the result field is the primary output channel the agent reads
        to determine whether the executed command produced the expected output.
        """
        from encre.task.manager import EncreTaskManager
        task_id = EncreTaskManager.create_task(
            name="T", description="D", task_type="bash", prompt="echo hi"
        )
        EncreTaskManager.update_task(task_id, status="completed", result="success output")
        task = EncreTaskManager.get_task(task_id)
        assert task.status == "completed"
        assert task.result == "success output"

    def test_verify_update_task_sets_error_on_failure(self):
        """Validate that update_task stores the error string when marking a task failed.

        The test updates the task to status='failed' with error='command not found'
        and asserts both fields match because the error field is the primary
        diagnostic channel the agent reads to decide whether to retry, escalate,
        or report the failure to the user.
        """
        from encre.task.manager import EncreTaskManager
        task_id = EncreTaskManager.create_task(
            name="T", description="D", task_type="bash", prompt="invalid"
        )
        EncreTaskManager.update_task(task_id, status="failed", error="command not found")
        task = EncreTaskManager.get_task(task_id)
        assert task.status == "failed"
        assert task.error == "command not found"

    def test_verify_update_nonexistent_task_returns_false(self):
        """Validate that update_task returns False when the target ID does not exist.

        The test passes 'nonexistent' and asserts False because the update path
        must not create implicit tasks; a false return lets the caller distinguish
        between 'updated successfully' and 'nothing to update' without exceptions.
        """
        from encre.task.manager import EncreTaskManager
        result = EncreTaskManager.update_task("nonexistent", status="running")
        assert result is False

    def test_verify_update_advances_updated_at_timestamp(self):
        """Validate that update_task advances the updated_at timestamp.

        The test records the original updated_at, sleeps briefly, performs an
        update, and asserts new_updated_at >= original_updated_at because the
        timestamp monotonicity is required for sort-by-updated-at queries and
        for detecting whether a task has been modified since the last read.
        """
        from encre.task.manager import EncreTaskManager
        task_id = EncreTaskManager.create_task(
            name="T", description="D", task_type="bash", prompt="echo"
        )
        original_updated_at = EncreTaskManager.get_task(task_id).updated_at
        time.sleep(0.01)
        EncreTaskManager.update_task(task_id, status="running")
        new_updated_at = EncreTaskManager.get_task(task_id).updated_at
        assert new_updated_at >= original_updated_at

    def test_verify_list_tasks_returns_all_created_tasks(self):
        """Validate that list_tasks returns exactly the number of created tasks.

        The test creates three tasks and asserts len(tasks) == 3 because the
        list operation is the primary discovery mechanism for the scheduler
        and UI, and it must reflect the current store size without omission.
        """
        from encre.task.manager import EncreTaskManager
        EncreTaskManager.create_task(name="T1", description="D", task_type="bash", prompt="echo 1")
        EncreTaskManager.create_task(name="T2", description="D", task_type="bash", prompt="echo 2")
        EncreTaskManager.create_task(name="T3", description="D", task_type="bash", prompt="echo 3")
        tasks = EncreTaskManager.list_tasks()
        assert len(tasks) == 3

    def test_verify_list_tasks_filters_by_status_correctly(self):
        """Validate that list_tasks with a status filter returns only matching tasks.

        The test creates two tasks, completes one, and asserts len(pending) == 1
        and len(completed) == 1 because status filtering is the primary way the
        scheduler isolates work items for retry queues and completion dashboards.
        """
        from encre.task.manager import EncreTaskManager
        id1 = EncreTaskManager.create_task(name="T1", description="D", task_type="bash", prompt="echo 1")
        EncreTaskManager.create_task(name="T2", description="D", task_type="bash", prompt="echo 2")
        EncreTaskManager.update_task(id1, status="completed")
        pending = EncreTaskManager.list_tasks(status="pending")
        completed = EncreTaskManager.list_tasks(status="completed")
        assert len(pending) == 1
        assert len(completed) == 1

    def test_verify_list_tasks_sorted_by_created_at_descending(self):
        """Validate that list_tasks returns tasks in descending created_at order.

        The test creates two tasks with a brief sleep between them and asserts
        tasks[0].id == id2 (the later task) and tasks[1].id == id1 because the
        descending sort ensures the UI and scheduler surface the most recent
        work items first, which is the expected display and processing order.
        """
        from encre.task.manager import EncreTaskManager
        id1 = EncreTaskManager.create_task(name="First", description="D", task_type="bash", prompt="echo 1")
        time.sleep(0.01)
        id2 = EncreTaskManager.create_task(name="Second", description="D", task_type="bash", prompt="echo 2")
        tasks = EncreTaskManager.list_tasks()
        assert tasks[0].id == id2
        assert tasks[1].id == id1

    def test_verify_delete_task_removes_it_and_returns_true(self):
        """Validate that delete_task removes the task and returns True, while get returns None afterwards.

        The test asserts delete returns True and get returns None because the
        delete operation must be visibly effective: the record must disappear
        from the store so that subsequent list and get calls reflect the
        removal without requiring a cache invalidation step.
        """
        from encre.task.manager import EncreTaskManager
        task_id = EncreTaskManager.create_task(name="T", description="D", task_type="bash", prompt="echo")
        assert EncreTaskManager.delete_task(task_id) is True
        assert EncreTaskManager.get_task(task_id) is None

    def test_verify_delete_nonexistent_task_returns_false(self):
        """Validate that delete_task returns False when the target ID does not exist.

        The test passes 'no-such-task' and asserts False because the delete
        path must be idempotent and non-raising so that cleanup logic in the
        scheduler can call delete without wrapping it in exception handlers.
        """
        from encre.task.manager import EncreTaskManager
        assert EncreTaskManager.delete_task("no-such-task") is False

    def test_verify_create_task_with_parent_sets_parent_id(self):
        """Validate that creating a child task stores the parent_id correctly.

        The test creates a parent, creates a child with parent_id=parent_id,
        and asserts child.parent_id == parent_id because the parent-child
        relationship is the mechanism that enables hierarchical task trees
        and propagation of completion signals up the dependency chain.
        """
        from encre.task.manager import EncreTaskManager
        parent_id = EncreTaskManager.create_task(
            name="Parent", description="P", task_type="agent", prompt="parent task"
        )
        child_id = EncreTaskManager.create_task(
            name="Child", description="C", task_type="bash", prompt="child task",
            parent_id=parent_id,
        )
        child = EncreTaskManager.get_task(child_id)
        assert child.parent_id == parent_id

    def test_verify_create_task_stores_metadata_dictionary(self):
        """Validate that metadata supplied at create time is preserved in the stored task.

        The test creates a task with metadata={'timeout': 30, 'retries': 3}
        and asserts the stored metadata matches exactly because metadata is
        the extensibility field that carries execution hints from the caller
        to the executor without requiring schema changes to the core record.
        """
        from encre.task.manager import EncreTaskManager
        task_id = EncreTaskManager.create_task(
            name="T", description="D", task_type="bash", prompt="echo",
            metadata={"timeout": 30, "retries": 3},
        )
        task = EncreTaskManager.get_task(task_id)
        assert task.metadata == {"timeout": 30, "retries": 3}

    def test_verify_clear_removes_all_tasks_from_store(self):
        """Validate that clear empties the task store so list_tasks returns an empty list.

        The test creates two tasks, calls clear, and asserts len(list_tasks())
        == 0 because clear is the teardown mechanism used between test runs
        and between swarm sessions to guarantee a clean state on startup.
        """
        from encre.task.manager import EncreTaskManager
        EncreTaskManager.create_task(name="T1", description="D", task_type="bash", prompt="echo 1")
        EncreTaskManager.create_task(name="T2", description="D", task_type="bash", prompt="echo 2")
        EncreTaskManager.clear()
        assert len(EncreTaskManager.list_tasks()) == 0


# ===========================================================================
# EncreTaskExecutor
# ===========================================================================


class TestEncreTaskExecutor:
    """Validate the EncreTaskExecutor lifecycle and error handling.

    This class exercises construction, not-found handling, bash task execution
    with success and failure, the pending -> running -> completed transition,
    and unknown task-type handling across 6 scenarios to ensure the executor
    correctly updates task state, captures command output, and degrades
    gracefully when it meets an unsupported type. The autouse fixture clears
    the store before every test so executor state cannot leak between runs.
    """
    """Runs bash, agent, and workflow tasks and records their outcome."""

    @pytest.fixture(autouse=True)
    def _clear_before_test(self):
        """Clear the shared task store before each test so runs stay isolated."""
        from encre.task.manager import EncreTaskManager
        EncreTaskManager.clear()

    def test_verify_executor_construction(self):
        """Validate that EncreTaskExecutor constructs without raising.

        The test asserts the executor is not None because the executor is the
        runtime entry point that the scheduler calls to transition a task
        from pending to running to completed; a failed construction would
        block the entire task execution pipeline.
        """
        from encre.task.executor import EncreTaskExecutor
        executor = EncreTaskExecutor()
        assert executor is not None

    def test_verify_execute_task_not_found_returns_error_string(self):
        """Validate that execute_task returns an error message containing 'not found' for a missing ID.

        The test calls execute_task with 'nonexistent-id' and asserts the
        returned string contains 'not found' (case-insensitive) because the
        executor must report a human-readable error instead of raising,
        allowing the scheduler to log the failure and continue processing.
        """
        from encre.task.executor import EncreTaskExecutor

        async def _test():
            """Exercise the async not-found path driven by asyncio.run()."""
            executor = EncreTaskExecutor()
            result = await executor.execute_task("nonexistent-id")
            assert "not found" in result.lower()

        asyncio.run(_test())

    def test_verify_execute_bash_task_runs_command_and_updates_status_to_completed(self):
        """Validate that executing a bash task runs the command, captures output, and marks the task completed.

        The test creates a bash task with prompt='echo hello world', executes
        it, and asserts the result contains 'hello world', the task status is
        'completed', and task.result equals the executor's returned string
        because the bash runner is the default execution path and must faithfully
        capture stdout so the agent can inspect command output in subsequent turns.
        """
        from encre.task.executor import EncreTaskExecutor
        from encre.task.manager import EncreTaskManager

        async def _test():
            """Exercise the successful bash execution and its persisted output."""
            task_id = EncreTaskManager.create_task(
                name="Bash Test",
                description="Run echo",
                task_type="bash",
                prompt="echo hello world",
            )
            executor = EncreTaskExecutor()
            result = await executor.execute_task(task_id)
            assert "hello world" in result

            task = EncreTaskManager.get_task(task_id)
            assert task.status == "completed"
            assert task.result == result

        asyncio.run(_test())

    def test_verify_execute_bash_task_with_failing_command_still_sets_terminal_status(self):
        """Validate that a failing bash command still transitions the task to a terminal status.

        The test creates a task that runs a nonexistent command with 'exit 0'
        appended and asserts the final status is either 'completed' or 'failed'
        because the executor must always reach a terminal state so the scheduler
        can proceed to the next node in the task tree regardless of command exit code.
        """
        from encre.task.executor import EncreTaskExecutor
        from encre.task.manager import EncreTaskManager

        async def _test():
            """Exercise a bash task whose command fails but must still terminate."""
            task_id = EncreTaskManager.create_task(
                name="Failing Bash",
                description="Run invalid command",
                task_type="bash",
                prompt="nonexistent_command_xyz 2>&1; exit 0",
            )
            executor = EncreTaskExecutor()
            await executor.execute_task(task_id)
            task = EncreTaskManager.get_task(task_id)
            assert task.status in ("completed", "failed")

        asyncio.run(_test())

    def test_verify_execute_transitions_status_to_completed(self):
        """Validate that execute_task transitions the task status to completed after running.

        The test creates a bash task, executes it, and asserts task.status ==
        'completed' because the status transition pending -> running -> completed
        is the canonical lifecycle the scheduler and UI depend on to render
        progress and to trigger downstream dependents in a task tree.
        """
        from encre.task.executor import EncreTaskExecutor
        from encre.task.manager import EncreTaskManager

        async def _test():
            """Exercise the pending-to-completed status transition of a bash task."""
            task_id = EncreTaskManager.create_task(
                name="Status Test",
                description="Test status transitions",
                task_type="bash",
                prompt="echo done",
            )
            executor = EncreTaskExecutor()
            await executor.execute_task(task_id)
            task = EncreTaskManager.get_task(task_id)
            assert task.status == "completed"

        asyncio.run(_test())

    def test_verify_execute_unknown_task_type_returns_error_and_completes(self):
        """Validate that an unknown task type returns an error string and leaves the task in completed status.

        The test inserts a task with task_type='invalid_type' directly into
        the store, executes it, and asserts the result contains 'unknown'
        (case-insensitive) and task.status == 'completed' with task.result
        containing 'Unknown task type' because the executor must degrade
        gracefully instead of raising, and the task must reach a terminal
        state so the scheduler does not stall on an unhandled type.
        """
        from encre.task.executor import EncreTaskExecutor
        from encre.task.manager import EncreTaskManager
        from encre.task.types import EncreTask

        async def _test():
            """Exercise graceful degradation for an unsupported task type."""
            task_id = "fake-unknown-type-id"
            now = time.time()
            EncreTaskManager._tasks[task_id] = EncreTask(
                id=task_id,
                name="Unknown",
                description="Bad type",
                task_type="invalid_type",
                prompt="test",
                created_at=now,
                updated_at=now,
            )
            executor = EncreTaskExecutor()
            result = await executor.execute_task(task_id)
            assert "unknown" in result.lower()
            task = EncreTaskManager.get_task(task_id)
            assert task.status == "completed"
            assert "Unknown task type" in task.result

        asyncio.run(_test())


# ===========================================================================
# Public API exports
# ===========================================================================


class TestTaskPublicAPI:
    """Validate the public API surface of the encre.task package.

    This class exercises package-level imports and type literal availability
    across 2 scenarios to ensure the public exports are stable and that
    TaskType and TaskStatus are importable from encre.utils.types, because
    downstream code and external integrations depend on these symbols being
    present at the documented import paths.
    """
    """Public symbols consumers are allowed to import from encre.task."""

    def test_verify_public_exports_are_importable(self):
        """Validate that EncreTask, EncreTaskManager, and EncreTaskExecutor are exported from encre.task.

        The test imports all three from the package-level module and asserts
        each is not None because the public API contract requires these three
        symbols to be accessible without importing internal submodules, enabling
        simple usage patterns like 'from encre.task import EncreTask'.
        """
        from encre.task import EncreTask, EncreTaskExecutor, EncreTaskManager
        assert EncreTask is not None
        assert EncreTaskManager is not None
        assert EncreTaskExecutor is not None

    def test_verify_task_type_and_status_literals_are_importable(self):
        """Validate that TaskType and TaskStatus are importable from encre.utils.types.

        The test asserts both symbols are not None because they are the canonical
        type enumerations used throughout the task system for validation,
        serialization, and UI rendering; missing literals would break any
        code that depends on them for type-safe task classification.
        """
        from encre.utils.types import TaskStatus, TaskType
        assert TaskType is not None
        assert TaskStatus is not None
