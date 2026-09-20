#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class WorkflowTaskStatus(Enum):
    """Represents the current state of a workflow task node."""

    PENDING = auto()
    """Initial state; dependencies not yet checked."""

    READY = auto()
    """All dependencies satisfied; awaiting execution."""

    RUNNING = auto()
    """Currently being executed."""

    COMPLETED = auto()
    """Execution finished successfully."""

    FAILED = auto()
    """Execution finished with an unrecoverable error."""

    SKIPPED = auto()
    """Skipped due to a predecessor failure or conditional logic."""

    BLOCKED = auto()
    """Blocked because a dependency has failed or been skipped."""


@dataclass
class WorkflowTask:
    """A single node in a DAG workflow.

    Each task may depend on the completion of other tasks (identified by their
    ``id`` fields in ``dependencies``).  The execution engine evaluates
    dependencies before running a task and automatically sets its status to
    ``READY`` when all dependencies are satisfied.

    The ``execute`` field should be an async callable that performs the actual
    work.  If ``execute`` is ``None`` the task is treated as a no-op leaf node
    that transitions directly to ``COMPLETED``.
    """

    id: str
    """Unique identifier for this task within the workflow."""

    name: str
    """Human-readable display name."""

    description: str
    """Detailed description of what this task does."""

    dependencies: list[str] = field(default_factory=list)
    """List of task IDs that must complete before this task may run."""

    status: WorkflowTaskStatus = WorkflowTaskStatus.PENDING
    """Current lifecycle status."""

    priority: int = 0
    """Relative priority (higher values = higher priority)."""

    max_retries: int = 0
    """Maximum number of automatic retries on failure."""

    timeout: float | None = None
    """Optional execution timeout in seconds. ``None`` means no timeout."""

    result: str = ""
    """Arbitrary string result produced by the task."""

    error: str = ""
    """Error message if the task failed."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Extensible metadata dictionary for arbitrary key-value pairs."""

    execute: Coroutine[Any, Any, Any] | None = None
    """The async work to perform.  If ``None`` the task completes immediately."""

    retry_count: int = 0
    """Number of times this task has been retried (internal bookkeeping)."""

    created_at: float = field(default_factory=time.time)
    """Timestamp (Unix epoch) when the task was created."""

    started_at: float | None = None
    """Timestamp when execution started."""

    completed_at: float | None = None
    """Timestamp when execution completed (success or failure)."""

    def __hash__(self) -> int:
        # Tasks are keyed by id so they hash consistently in sets/dicts.
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WorkflowTask):
            return NotImplemented
        return self.id == other.id

    def to_dict(self) -> dict[str, Any]:
        """Serialise the task (minus the executable callable) to a plain dict."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "dependencies": list(self.dependencies),
            "status": self.status.name,
            "priority": self.priority,
            "max_retries": self.max_retries,
            "timeout": self.timeout,
            "result": self.result,
            "error": self.error,
            "metadata": dict(self.metadata),
            "retry_count": self.retry_count,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowTask:
        """Deserialise a task from a dict (``execute`` remains ``None``)."""
        status_str = data.get("status", "PENDING")
        # Resolve the status name back to the enum, falling back to PENDING.
        status = WorkflowTaskStatus[status_str] if isinstance(status_str, str) else WorkflowTaskStatus.PENDING
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            description=data.get("description", ""),
            dependencies=list(data.get("dependencies", [])),
            status=status,
            priority=data.get("priority", 0),
            max_retries=data.get("max_retries", 0),
            timeout=data.get("timeout"),
            result=data.get("result", ""),
            error=data.get("error", ""),
            metadata=dict(data.get("metadata", {})),
            retry_count=data.get("retry_count", 0),
            created_at=data.get("created_at", 0.0),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
        )


def make_ready_predicate(task: WorkflowTask, completed_ids: set[str]) -> bool:
    """Return ``True`` if *task* should be considered ready.

    A task is ready when:
    - Its status is ``PENDING``, and
    - All its dependencies are in *completed_ids*.
    """
    if task.status != WorkflowTaskStatus.PENDING:
        return False
    # Only PENDING tasks with every dependency completed are considered ready.
    return all(dep_id in completed_ids for dep_id in task.dependencies)
