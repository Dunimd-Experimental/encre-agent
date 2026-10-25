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

"""Unified task tool for the Encre Agent task system.

This module consolidates the entire task lifecycle — create, list, get,
output, stop, update — into a single ``task`` tool that uses an
``action`` parameter to select the operation. This mirrors the ``manage``
tool pattern: one tool, one package, one uninstall target.

Previously the task lifecycle was split across six separate EA packages
(``ea-tool-task-create``, ``ea-tool-task-list``, etc.), each with its own
uninstall path. The problem: if a user uninstalled ``task_stop``, the agent
could create background tasks but never cancel them — a logical impossibility.

The merged tool preserves all six operations as ``action`` sub-commands:

* ``create`` — spawn a new background sub-task
* ``list`` — enumerate tasks with optional status filter
* ``get`` — one-shot snapshot of a single task's state
* ``output`` — stream task output, optionally blocking for completion
* ``stop`` — cancel a running task (idempotent)
* ``update`` — low-level mutation of status/result/error

Concurrency: the task manager serialises its own writes, so concurrent
``task`` calls from different tool dispatchers are safe. Read actions
(``list``, ``get``, ``output``) are declared concurrency-safe.
"""

import asyncio
import json
from typing import Any

from encre.task.manager import EncreTaskManager
from encre.tools.base import build_tool
from encre.utils.types import TaskStatus


# ── Helper: ensure task_id is provided ──────────────────────────────────

def _require_task_id(kwargs: dict[str, Any]) -> str | None:
    """Return the task_id or an error message string."""
    task_id = kwargs.get("task_id", "")
    if not task_id:
        return None
    return task_id


# ── Action: create ───────────────────────────────────────────────────────

async def _task_create(**kwargs: Any) -> str:
    """Create a new background task and return its ID."""
    name = kwargs.get("name", "")
    description = kwargs.get("description", "")
    task_type = kwargs.get("task_type", "bash")
    prompt = kwargs.get("prompt", "")
    parent_id = kwargs.get("parent_id")

    if not name or not prompt:
        return "Error: both 'name' and 'prompt' are required for create action."

    task_id = EncreTaskManager.create_task(
        name=name,
        description=description,
        task_type=task_type,
        prompt=prompt,
        parent_id=parent_id,
    )
    return f"Task created: {task_id}"


# ── Action: list ─────────────────────────────────────────────────────────

async def _task_list(**kwargs: Any) -> str:
    """Enumerate tasks and return them as a compact multi-line listing."""
    status = kwargs.get("status")
    tasks = EncreTaskManager.list_tasks(status=status)

    if not tasks:
        return "No tasks found."

    status_icon = {
        "pending": "[ ]",
        "running": "[>]",
        "completed": "[x]",
        "failed": "[!]",
        "killed": "[-]",
    }
    lines: list[str] = []
    for task in tasks:
        icon = status_icon.get(task.status, "[?]")
        lines.append(f"{icon} {task.id}: {task.name} ({task.status})")
    return "\n".join(lines)


# ── Action: get ──────────────────────────────────────────────────────────

async def _task_get(**kwargs: Any) -> str:
    """Look up a task by ID and return its snapshot as text."""
    task_id = kwargs.get("task_id", "")
    task = EncreTaskManager.get_task(task_id)
    if task is None:
        return f"Error: Task not found: {task_id}"

    lines = [
        f"ID: {task.id}",
        f"Name: {task.name}",
        f"Type: {task.task_type}",
        f"Status: {task.status}",
        f"Description: {task.description}",
    ]
    if task.result:
        lines.append(f"Result: {task.result[:500]}")
    if task.error:
        lines.append(f"Error: {task.error}")
    if task.parent_id:
        lines.append(f"Parent: {task.parent_id}")
    return "\n".join(lines)


# ── Action: output ───────────────────────────────────────────────────────

async def _task_output(**kwargs: Any) -> str:
    """Read a task's output, optionally blocking until it finishes."""
    task_id = kwargs.get("task_id", "")
    block = kwargs.get("block", True)
    timeout_ms = kwargs.get("timeout", 30000)

    if not task_id:
        return "Error: task_id is required."

    task = EncreTaskManager.get_task(task_id)
    if task is None:
        return f"Error: task '{task_id}' not found."

    # If blocking, poll until terminal status or timeout
    if block and task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
        deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
        while task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.2, remaining))
            task = EncreTaskManager.get_task(task_id)
            if task is None:
                return f"Error: task '{task_id}' disappeared."

    status_icon = {
        TaskStatus.PENDING: "[ ]",
        TaskStatus.RUNNING: "\U0001f504",
        TaskStatus.COMPLETED: "[x]",
        TaskStatus.FAILED: "[!]",
        TaskStatus.CANCELLED: "\U0001f6ab",
    }.get(task.status, "[?]")

    result = {
        "id": task.id,
        "name": task.name,
        "status": task.status.name,
        "icon": status_icon,
        "result": task.result or "",
        "error": task.error or "",
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }

    if task.result and len(task.result) > 5000:
        result["result"] = task.result[:5000] + "\n... (truncated)"
    if task.error and len(task.error) > 2000:
        result["error"] = task.error[:2000] + "\n... (truncated)"

    return json.dumps(result, ensure_ascii=False, indent=2)


# ── Action: stop ─────────────────────────────────────────────────────────

async def _task_stop(**kwargs: Any) -> str:
    """Cancel a background task by task ID (idempotent)."""
    task_id = kwargs.get("task_id", "")
    if not task_id:
        return "Error: task_id is required."

    task = EncreTaskManager.get_task(task_id)
    if task is None:
        return f"Error: task '{task_id}' not found."

    if task.status == TaskStatus.COMPLETED:
        return f"Task '{task_id}' already completed."
    if task.status == TaskStatus.CANCELLED:
        return f"Task '{task_id}' already cancelled."

    EncreTaskManager.update_task(task_id, status=TaskStatus.CANCELLED, error="Stopped by user request")
    return f"Task '{task_id}' stopped."


# ── Action: update ───────────────────────────────────────────────────────

async def _task_update(**kwargs: Any) -> str:
    """Patch one or more fields on an existing task (low-level mutation)."""
    task_id = kwargs.get("task_id", "")
    status = kwargs.get("status")
    result = kwargs.get("result")
    error = kwargs.get("error")

    success = EncreTaskManager.update_task(
        task_id=task_id,
        status=status,
        result=result,
        error=error,
    )
    if success:
        return f"Task {task_id} updated successfully."
    return f"Error: Task not found: {task_id}"


# ── Unified dispatcher ───────────────────────────────────────────────────

async def _task_execute(**kwargs: Any) -> str:
    """Dispatch the task action and return the result."""
    action = kwargs.get("action", "")
    if not action:
        return "Error: 'action' is required. Valid actions: create, list, get, output, stop, update."

    dispatch = {
        "create": _task_create,
        "list": _task_list,
        "get": _task_get,
        "output": _task_output,
        "stop": _task_stop,
        "update": _task_update,
    }

    handler = dispatch.get(action)
    if handler is None:
        return f"Error: unknown action '{action}'. Valid actions: {', '.join(dispatch.keys())}."

    return await handler(**kwargs)


# ── Tool definition ──────────────────────────────────────────────────────

task_tool = build_tool(
    name="task",
    description=(
        "Manage background sub-tasks through the unified task tool.\n\n"
        "Use the 'action' parameter to select the operation:\n"
        "  - create: spawn a new background sub-task (bash, agent, or workflow)\n"
        "  - list: enumerate all tasks with optional status filter\n"
        "  - get: one-shot snapshot of a single task's state (result truncated to 500 chars)\n"
        "  - output: stream task output with optional blocking wait (result up to 5000 chars)\n"
        "  - stop: cancel a running task (idempotent — already-terminal tasks return friendly messages)\n"
        "  - update: low-level mutation of status/result/error\n\n"
        "WHEN to use: long-running work that would block the main conversation "
        "(builds, test suites, research delegations); parallelizable sub-problems "
        "you want to fan out to sub-agents.\n"
        "WHEN NOT to use: for short synchronous actions just use the relevant tool "
        "directly; for the model's own task tracking use the todo tool.\n"
        "TIPS: 'create' returns a task ID you need for all other actions; 'output' "
        "with block=true (default) waits for completion; 'stop' is safe to retry.\n"
        "PITFALLS: 'output' is the way to wait for and retrieve results — 'get' "
        "truncates the result field; prefer 'stop' over 'update' for cancellations."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "get", "output", "stop", "update"],
                "description": "The task operation to perform.",
            },
            # Common params
            "task_id": {
                "type": "string",
                "description": "The unique ID of the task (required for get, output, stop, update; from create or list output).",
            },
            # Create params
            "name": {
                "type": "string",
                "description": "[create] Short human-readable name for the task (shown in task listings). Required.",
            },
            "description": {
                "type": "string",
                "description": "[create] Longer description of what the task should accomplish.",
            },
            "task_type": {
                "type": "string",
                "enum": ["bash", "agent", "workflow"],
                "description": "[create] Execution backend: 'bash' runs a shell command, 'agent' delegates to a sub-agent LLM, 'workflow' runs a defined multi-step workflow. Defaults to 'bash'.",
            },
            "prompt": {
                "type": "string",
                "description": "[create] The instructions/command for the task: shell command for bash, the natural-language brief for agent, or the workflow spec for workflow. Required.",
            },
            "parent_id": {
                "type": "string",
                "description": "[create] Optional ID of a parent task to nest this one under, building a task hierarchy.",
            },
            # List params
            "status": {
                "type": "string",
                "enum": ["pending", "running", "completed", "failed", "killed"],
                "description": "[list] Filter by status.",
            },
            # Output params
            "block": {
                "type": "boolean",
                "description": "[output] Wait for task completion (default: true). Set false for non-blocking poll.",
                "default": True,
            },
            "timeout": {
                "type": "integer",
                "description": "[output] Max wait time in milliseconds (default: 30000).",
                "default": 30000,
            },
            # Update params
            "result": {
                "type": "string",
                "description": "[update] Task output/result to store. Populate when marking a task 'completed'.",
            },
            "error": {
                "type": "string",
                "description": "[update] Error message describing why the task failed. Populate when marking a task 'failed'.",
            },
        },
        "required": ["action"],
    },
    execute=_task_execute,
    intents=["general", "coding", "data", "research"],
    category="task",
    semantic_type="write",
    is_destructive=True,
    is_readonly=lambda args: args.get("action") in ("list", "get", "output"),
    is_concurrency_safe=lambda args: args.get("action") in ("list", "get", "output"),
)
