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
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from encre.logging_config import get_logger
from encre.scheduler import EncreScheduler
from encre.workflow.engine import (
    WorkflowCompleted,
    WorkflowEngine,
    WorkflowEvent,
    WorkflowStarted,
)
from encre.workflow.graph import DAGGraph

logger = get_logger("encre.workflow.scheduler")


class WorkflowState(Enum):
    """Lifecycle state of a scheduled workflow."""

    PENDING = auto()
    """Scheduled but not yet started."""

    RUNNING = auto()
    """Workflow is currently being executed."""

    COMPLETED = auto()
    """Workflow finished successfully."""

    FAILED = auto()
    """Workflow finished with unrecoverable errors."""

    CANCELLED = auto()
    """Workflow was cancelled before or during execution."""


@dataclass
class WorkflowRecord:
    """Persistent metadata for a scheduled workflow run."""

    id: str
    name: str
    description: str = ""
    state: WorkflowState = WorkflowState.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    total_tasks: int = 0
    completed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    blocked_count: int = 0
    error: str = ""
    job_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "state": self.state.name,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "total_tasks": self.total_tasks,
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "blocked_count": self.blocked_count,
            "error": self.error,
            "job_id": self.job_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowRecord:
        state_str = data.get("state", "PENDING")
        state = WorkflowState[state_str] if isinstance(state_str, str) else WorkflowState.PENDING
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            description=data.get("description", ""),
            state=state,
            created_at=data.get("created_at", 0.0),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            total_tasks=data.get("total_tasks", 0),
            completed_count=data.get("completed_count", 0),
            failed_count=data.get("failed_count", 0),
            skipped_count=data.get("skipped_count", 0),
            blocked_count=data.get("blocked_count", 0),
            error=data.get("error", ""),
            job_id=data.get("job_id", ""),
            metadata=dict(data.get("metadata", {})),
        )


class WorkflowScheduler:
    """Enhanced scheduler that integrates DAG workflows with the existing
    cron-based :class:`EncreScheduler`.

    The :class:`WorkflowScheduler` manages workflow lifecycle -- scheduling,
    execution (via :class:`WorkflowEngine`), progress tracking, and
    cancellation.  It delegates time-based triggering to ``EncreScheduler``
    while orchestrating the DAG execution itself.

    Usage::

        dag = DAGGraph()
        dag.add_node(task_a)
        dag.add_node(task_b)
        dag.add_edge("task_a", "task_b")

        sched = WorkflowScheduler()
        wf_id = sched.schedule_workflow(
            graph=dag,
            name="My Workflow",
            cron="0 9 * * *",
        )
        await sched.start()  # begins background polling
    """

    def __init__(
        self,
        cron_scheduler: EncreScheduler | None = None,
    ) -> None:
        self._cron_scheduler = cron_scheduler or EncreScheduler()
        self._engine = WorkflowEngine()
        self._workflows: dict[str, WorkflowRecord] = {}
        self._graphs: dict[str, DAGGraph] = {}
        self._on_workflow_complete: Callable[[WorkflowRecord], None] | None = None
        self._background_tasks: set = set()

        # Wire up cron scheduler to notify us when a workflow job fires
        self._cron_scheduler.on_job_complete(self._on_cron_job_complete)

    # 鈹€鈹€ Workflow scheduling 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def schedule_workflow(
        self,
        graph: DAGGraph,
        name: str,
        description: str = "",
        *,
        cron: str = "",
        fire_at: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Schedule a DAG workflow for execution.

        Args:
            graph: The DAG to execute when the schedule fires.
            name: Human-readable name for the workflow.
            description: Optional description.
            cron: Optional 5-field cron expression for recurring execution.
            fire_at: Optional absolute Unix timestamp for one-shot execution.
            metadata: Optional metadata dictionary.

        Returns:
            The workflow ID (string).

        One of *cron* or *fire_at* should be provided.  If neither is given
        the workflow fires once immediately (one second from now).
        """
        workflow_id = uuid.uuid4().hex[:12]
        # Build the persistent record and stash the graph for later execution.

        record = WorkflowRecord(
            id=workflow_id,
            name=name,
            description=description,
            metadata=metadata or {},
        )
        self._workflows[workflow_id] = record
        self._graphs[workflow_id] = graph

        # Schedule via the underlying cron scheduler
        # Use a unique prompt that references the workflow ID so the cron
        # scheduler can trigger the right workflow.
        job_id = self._cron_scheduler.schedule(
            name=f"workflow:{name}",
            prompt=f"__workflow__:{workflow_id}",
            cron=cron,
            fire_at=fire_at,
            metadata={"workflow_id": workflow_id},
        )
        record.job_id = job_id

        logger.info(
            "[workflow_scheduler] scheduled workflow '%s' (%s) as job '%s'",
            name, workflow_id, job_id,
        )
        return workflow_id

    def cancel_workflow(self, workflow_id: str) -> bool:
        """Cancel a pending or running workflow.

        Returns ``True`` if the workflow was successfully cancelled.
        """
        record = self._workflows.get(workflow_id)
        if record is None:
            return False

        if record.state == WorkflowState.RUNNING:
            # Cancel the engine
            # Request the engine to stop accepting new tasks mid-run.
            import asyncio
            self._background_tasks.add(asyncio.create_task(self._engine.cancel()))

        # Cancel the underlying cron job
        if record.job_id:
            self._cron_scheduler.cancel(record.job_id)

        record.state = WorkflowState.CANCELLED
        logger.info(
            "[workflow_scheduler] cancelled workflow '%s' (%s)",
            record.name, workflow_id,
        )
        return True

    def get_workflow_status(self, workflow_id: str) -> WorkflowRecord | None:
        """Return the status record for a workflow, or ``None``."""
        return self._workflows.get(workflow_id)

    def list_workflows(
        self,
        state: WorkflowState | None = None,
    ) -> list[WorkflowRecord]:
        """List all workflows, optionally filtered by state."""
        records = list(self._workflows.values())
        if state is not None:
            records = [r for r in records if r.state == state]
        return sorted(records, key=lambda r: r.created_at, reverse=True)

    def get_workflow_graph(self, workflow_id: str) -> DAGGraph | None:
        """Return the DAG graph for a workflow, or ``None``."""
        return self._graphs.get(workflow_id)

    # 鈹€鈹€ Lifecycle hooks 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def on_workflow_complete(
        self, callback: Callable[[WorkflowRecord], None]
    ) -> None:
        """Register a callback invoked when a workflow run completes."""
        self._on_workflow_complete = callback

    # 鈹€鈹€ Engine integration (called when cron fires) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    async def execute_workflow(
        self,
        workflow_id: str,
        task_executors: dict[str, Callable[[], Any]] | None = None,
    ) -> None:
        """Immediately execute a scheduled workflow.

        This is the entry point called when the cron scheduler fires.
        The *task_executors* dict maps task IDs to async callables that
        will be assigned to the corresponding task nodes before execution.
        """
        record = self._workflows.get(workflow_id)
        graph = self._graphs.get(workflow_id)
        if record is None or graph is None:
            logger.error("[workflow_scheduler] unknown workflow '%s'", workflow_id)
            return

        # Update record state
        record.state = WorkflowState.RUNNING
        record.started_at = time.time()

        # Assign custom executors if provided
        if task_executors:
            for task in graph:
                executor = task_executors.get(task.id)
                if executor is not None:
                    task.execute = executor  # type: ignore[assignment]

        try:
            # Drive the DAG to completion, forwarding live events to the record.
            await self._engine.run_workflow(
                graph,
                workflow_id=workflow_id,
                on_event=lambda ev: self._on_workflow_event(record, ev),
            )

            # After execution, update the record from the graph state
            self._sync_record_from_graph(record, graph)

            if record.failed_count > 0:
                record.state = WorkflowState.FAILED
            else:
                record.state = WorkflowState.COMPLETED

            record.completed_at = time.time()

        except Exception as exc:
            record.state = WorkflowState.FAILED
            record.error = f"{type(exc).__name__}: {exc}"
            record.completed_at = time.time()
            logger.error(
                "[workflow_scheduler] workflow '%s' raised: %s",
                workflow_id, record.error, exc_info=True,
            )

        if self._on_workflow_complete:
            try:
                self._on_workflow_complete(record)
            except Exception:
                logger.warning(
                    "[workflow_scheduler] on_workflow_complete callback failed",
                    exc_info=True,
                )

    # 鈹€鈹€ Background loop 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    async def start(self) -> None:
        """Start the underlying cron scheduler.

        Must be called after workflows are registered.  Delegates to the
        ``EncreScheduler.start()`` method.
        """
        # The cron scheduler needs an agent factory; we provide a minimal
        # one that will discover and execute the right workflow.
        # Hand our stub agent factory to the cron loop so jobs can fire.
        await self._cron_scheduler.start(self._make_agent_factory())

    async def stop(self) -> None:
        """Stop the underlying cron scheduler."""
        await self._cron_scheduler.stop()

    # 鈹€鈹€ Internal helpers 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def _on_workflow_event(
        self, record: WorkflowRecord, event: WorkflowEvent
    ) -> None:
        """Update the workflow record from an engine event."""
        if isinstance(event, WorkflowStarted):
            record.total_tasks = event.total_tasks
        elif isinstance(event, WorkflowCompleted):
            record.completed_count = event.completed_count
            record.failed_count = event.failed_count
            record.skipped_count = event.skipped_count
            record.blocked_count = event.blocked_count

    def _sync_record_from_graph(
        self, record: WorkflowRecord, graph: DAGGraph
    ) -> None:
        """Synchronise the workflow record counters from final graph state."""
        statuses = [t.status for t in graph]
        from encre.workflow.task import WorkflowTaskStatus
        record.total_tasks = len(statuses)
        record.completed_count = sum(1 for s in statuses if s == WorkflowTaskStatus.COMPLETED)
        record.failed_count = sum(1 for s in statuses if s == WorkflowTaskStatus.FAILED)
        record.skipped_count = sum(
            1 for s in statuses if s in (WorkflowTaskStatus.SKIPPED, WorkflowTaskStatus.BLOCKED)
        )
        record.blocked_count = sum(1 for s in statuses if s == WorkflowTaskStatus.BLOCKED)

    def _on_cron_job_complete(self, job: Any) -> None:
        """Callback from the cron scheduler when a job fires.

        Extracts the workflow ID from the job's metadata and executes
        the corresponding DAG.
        """
        wf_id = job.metadata.get("workflow_id") if hasattr(job, "metadata") else None
        if wf_id and wf_id in self._workflows:
            import asyncio
            self._background_tasks.add(asyncio.create_task(self.execute_workflow(wf_id)))

    def _make_agent_factory(self) -> Callable[[dict[str, Any] | None], Any]:
        """Return a dummy agent factory for the cron scheduler.

        The cron scheduler requires a callable that returns an agent-like
        object.  We provide a minimal stub that allows the cron loop to
        progress without a real agent -- actual workflow execution is
        triggered via ``_on_cron_job_complete``.
        """
        class _WorkflowAgent:
            def __init__(self) -> None:
                import uuid
                from types import SimpleNamespace
                self.session = SimpleNamespace()
                self.session.id = str(uuid.uuid4())

            def add_message(self, role: str, content: str) -> None:
                pass

            async def run(self, _prompt: str) -> Any:
                return
                yield  # make this a generator for compatibility

        def _factory(_config: dict[str, Any] | None = None) -> _WorkflowAgent:
            return _WorkflowAgent()

        return _factory
