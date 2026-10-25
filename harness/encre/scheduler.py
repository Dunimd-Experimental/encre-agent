#!/usr/bin/env python3

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

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any

from encre.crypto import decrypt, encrypt
from encre.logging_config import get_logger

logger = get_logger("encre.scheduler")


class ScheduleType(Enum):
    ONE_SHOT = auto()
    RECURRING = auto()


class JobState(Enum):
    PENDING = auto()
    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()


@dataclass
class CronSchedule:
    """5-field cron expression: minute hour dom month dow"""

    minute: str = "*"
    hour: str = "*"
    day_of_month: str = "*"
    month: str = "*"
    day_of_week: str = "*"

    @classmethod
    def parse(cls, expr: str) -> "CronSchedule":
        fields = expr.strip().split()
        if len(fields) != 5:
            raise ValueError(f"Invalid cron expression: {expr}. Expected 5 fields.")
        return cls(*fields)

    def to_expression(self) -> str:
        return f"{self.minute} {self.hour} {self.day_of_month} {self.month} {self.day_of_week}"

    def _match_field(self, value: int, field: str) -> bool:
        """Check if a numeric value matches a cron field."""
        for part in field.split(","):
            part = part.strip()
            if part == "*":
                return True
            if "/" in part:
                base_str, step_str = part.split("/")
                step = int(step_str)
                if base_str == "*":
                    if value % step == 0:
                        return True
                else:
                    base = int(base_str)
                    if value >= base and (value - base) % step == 0:
                        return True
            elif "-" in part:
                lo_s, hi_s = part.split("-")
                lo, hi = int(lo_s), int(hi_s)
                if lo <= value <= hi:
                    return True
            else:
                try:
                    if int(part) == value:
                        return True
                except ValueError:
                    logger.debug("exception suppressed", exc_info=True)
        return False

    @staticmethod
    def _normalize_dow(field: str) -> str:
        """Convert named days to numbers. Cron: 0=Sun, 1=Mon, ..., 6=Sat, 7=Sun."""
        _dow_map = {"sun": "0", "mon": "1", "tue": "2", "wed": "3", "thu": "4", "fri": "5", "sat": "6"}
        result = field.lower()
        for name, num in _dow_map.items():
            result = result.replace(name, num)
        return result

    @staticmethod
    def _weekday_cron(year: int, month: int, day: int) -> int:
        """Return cron-style weekday (0=Sun, 1=Mon, ..., 6=Sat)."""
        from datetime import datetime

        py_wday = datetime(year, month, day).weekday()  # 0=Mon, 6=Sun
        return (py_wday + 1) % 7  # Convert to 0=Sun

    def next_fire(self, from_ts: float | None = None) -> float | None:
        """Calculate the next fire time from the given timestamp.
        Returns None if no match within the next year.
        """
        if from_ts is None:
            from_ts = time.time()
        t = time.localtime(from_ts)
        current = list(t[:5])  # (year, month, day, hour, min)
        current[4] += 1  # increment minute

        # Pre-normalize DOW field -- convert named days to numbers once
        normalized_dow = self._normalize_dow(self.day_of_week)

        max_iter = 525600  # 1 year in minutes
        for _ in range(max_iter):
            year, month, day, hour, minute = current
            # Normalize
            if minute >= 60:
                minute = 0
                hour += 1
            if hour >= 24:
                hour = 0
                day += 1
            days_in_month = [
                31,
                29 if (year % 400 == 0 or (year % 4 == 0 and year % 100 != 0)) else 28,
                31,
                30,
                31,
                30,
                31,
                31,
                30,
                31,
                30,
                31,
            ]
            if day > days_in_month[month - 1]:
                day = 1
                month += 1
            if month > 12:
                month = 1
                year += 1

            current = [year, month, day, hour, minute]

            if (
                self._match_field(minute, self.minute)
                and self._match_field(hour, self.hour)
                and self._match_field(day, self.day_of_month)
                and self._match_field(month, self.month)
                and self._match_field(
                    self._weekday_cron(year, month, day),
                    normalized_dow,
                )
            ):
                from datetime import datetime

                dt = datetime(year, month, day, hour, minute)
                return dt.timestamp()

            current[4] += 1  # next minute
        return None


@dataclass
class JobExecution:
    """A single execution record for a job.

    The execution itself lives as a regular sub-agent session under
    ``<data_dir>/sub_agents/<session_id>/`` -- exactly the same place
    where the in-chat ``agent`` tool stores its sub-agent sessions. The
    scheduler only keeps a lightweight reference here so the
    automation history list stays cheap to render. The full messages
    snapshot must be loaded from the sub-agent session on demand via
    :func:`EncreSession.load_from_dir`.
    """

    time: float
    state: str
    result: str
    session_id: str | None = None
    fail_count: int = 0
    name: str = ""
    job_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": self.time,
            "state": self.state,
            "result": self.result,
            "session_id": self.session_id,
            "fail_count": self.fail_count,
            "name": self.name,
            "job_id": self.job_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "JobExecution":
        return cls(
            time=d["time"],
            state=d["state"],
            result=d.get("result", ""),
            session_id=d.get("session_id"),
            fail_count=d.get("fail_count", 0),
            name=d.get("name", ""),
            job_id=d.get("job_id", ""),
        )


@dataclass
class ScheduledJob:
    """A job scheduled for future execution.

    Supports both simple prompt-based execution and DAG workflow
    execution.  When ``dag_definition`` is set, the scheduler uses
    :class:`encre.dag.executor.DagExecutor` to run the workflow
    instead of submitting the prompt directly to the agent.
    """

    id: str
    name: str
    prompt: str
    schedule_type: ScheduleType
    cron: CronSchedule | None = None
    fire_at: float | None = None  # absolute timestamp for one-shot
    state: JobState = JobState.PENDING
    created_at: float = field(default_factory=time.time)
    last_fired: float | None = None
    last_result: str | None = None
    fail_count: int = 0
    max_failures: int = 3
    suspended: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    model_index: int = -1
    session_id: str | None = None
    executions: list[JobExecution] = field(default_factory=list)
    _agent_config: dict[str, Any] | None = None
    # DAG workflow fields -- when set, the job executes a multi-step
    # DAG instead of a single prompt.  ``dag_definition`` is a JSON
    # string compatible with ``EncreTaskPlanner.plan_from_json()``.
    dag_definition: str = ""
    push_gateways: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prompt": self.prompt,
            "schedule_type": self.schedule_type.name,
            "cron": self.cron.to_expression() if self.cron else None,
            "fire_at": self.fire_at,
            "state": self.state.name,
            "suspended": self.suspended,
            "created_at": self.created_at,
            "last_fired": self.last_fired,
            "last_result": self.last_result,
            "fail_count": self.fail_count,
            "max_failures": self.max_failures,
            "metadata": self.metadata,
            "model_index": self.model_index,
            "session_id": self.session_id,
            "agent_config": self._agent_config,
            "dag_definition": self.dag_definition,
            "push_gateways": list(self.push_gateways),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledJob":
        job = cls(
            id=data["id"],
            name=data["name"],
            prompt=data["prompt"],
            schedule_type=ScheduleType[data["schedule_type"]],
            cron=CronSchedule.parse(data["cron"]) if data.get("cron") else None,
            fire_at=data.get("fire_at"),
            state=JobState[data.get("state", "PENDING")],
            created_at=data.get("created_at", 0),
            last_fired=data.get("last_fired"),
            last_result=data.get("last_result"),
            fail_count=data.get("fail_count", 0),
            max_failures=data.get("max_failures", 3),
            suspended=data.get("suspended", False),
            metadata=data.get("metadata", {}),
            model_index=data.get("model_index", -1),
            dag_definition=data.get("dag_definition", ""),
            push_gateways=data.get("push_gateways", []),
        )
        job.session_id = data.get("session_id")
        job._agent_config = data.get("agent_config")
        # Executions are now stored in a separate global history file.
        # The legacy ``executions`` field is only used for one-time migration
        # when _load() detects it.
        job.executions = [JobExecution.from_dict(execution) for execution in (data.get("executions") or [])]
        return job


@dataclass
class WakeIntent:
    """Core -> Harness contract for a commitment wake.

    The Core layer (resident runtime) detects a due commitment in the memory
    graph and emits one intent per notification. The envelope is fully
    self-contained: everything the executor needs rides in ``prompt`` -- the
    harness reads no global state, runs once, and keeps nothing afterwards.
    When the scheduler sinks into Rust, this is the exact shape the native
    watcher will emit.
    """

    commitment_id: str
    content: str
    deadline_ts: float
    notify_stage: int
    max_reminds: int
    prompt: str
    push_gateways: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "commitment_id": self.commitment_id,
            "content": self.content,
            "deadline_ts": self.deadline_ts,
            "notify_stage": self.notify_stage,
            "max_reminds": self.max_reminds,
            "push_gateways": list(self.push_gateways),
        }


def _build_wake_prompt(
    commitment_id: str,
    content: str,
    deadline_ts: float,
    notify_stage: int,
    max_reminds: int,
) -> str:
    """Assemble the self-contained wake prompt for one commitment notification.

    Stage 1 is the pre-deadline warning; later stages are at/after the
    deadline. The prompt instructs the executor to verify the outcome, close
    the node when done, and report concisely -- in the user's language.
    """
    deadline_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(deadline_ts))
    stage_text = "pre-deadline warning" if notify_stage <= 1 else "deadline reached or overdue"
    return (
        "You were woken by the resident commitment watcher. A commitment has "
        "reached its notification point.\n\n"
        f"Commitment (id: {commitment_id}): {content}\n"
        f"Deadline: {deadline_text}\n"
        f"Notification: {notify_stage}/{max_reminds} ({stage_text})\n\n"
        "Tasks:\n"
        "1. Verify whether this commitment has been fulfilled "
        "(use memory_search and any relevant tools to check).\n"
        "2. If it is fulfilled, close it: "
        f'memory_update(ref="{commitment_id}", commitment_status="done").\n'
        "3. If it is not fulfilled, do whatever is practical to help right "
        "now (gather what is missing, draft the next step, prepare a "
        "concrete plan), then report what the user must know and do next.\n\n"
        "Report concisely, in the user's language."
    )


class CommitmentWatcher:
    """Resident watcher that wakes the agent when commitments fall due.

    Core-layer component (the resident side): it runs no model calls of its
    own. Due/expired commitments are decided by the pure-math
    :func:`encre.memdir.layers.iter_due_commitments`; each due node becomes a
    :class:`WakeIntent` dispatched through the scheduler's standard one-shot
    pipeline, so execution, history, progress streaming and gateway push all
    reuse the existing automation path unchanged.
    """

    def __init__(
        self,
        store: Any,
        scheduler: "EncreScheduler",
        push_gateways: "list[str] | Callable[[], list[str]] | None" = None,
        agent_config: dict[str, Any] | None = None,
        check_interval_seconds: float = 60.0,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        # Static list or a provider callable -- a callable lets the Core
        # resolve the currently-connected gateways at dispatch time, so
        # adapters that come online later still receive wakes.
        if callable(push_gateways):
            self._push_gateways_fn = push_gateways
        else:
            resolved = list(push_gateways or [])
            self._push_gateways_fn = lambda: resolved
        self._agent_config = agent_config
        self._check_interval = max(5.0, float(check_interval_seconds))
        self._running = False
        self._task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        """Start the background scan loop."""
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the background scan loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        """Periodically scan the memory graph for due commitments."""
        while self._running:
            try:
                self.scan()
            except Exception as e:
                logger.exception("[commitment_watcher] scan crashed: {}", e)
            await asyncio.sleep(self._check_interval)

    def scan(self) -> list[WakeIntent]:
        """One scan pass: dispatch due wakes, expire drained commitments.

        Stage metadata is written BEFORE dispatch so a crash between the two
        can only skip one wake, never spam duplicates.
        """
        from encre.memdir.layers import (
            COMMITMENT_STAGE_KEY,
            COMMITMENT_STATUS_EXPIRED,
            COMMITMENT_STATUS_KEY,
            iter_due_commitments,
        )

        now = time.time()
        entries = self._store.long_term() + self._store.buffer()
        due, expired = iter_due_commitments(entries, now)

        intents: list[WakeIntent] = []
        for entry, next_stage in due:
            intent = self._build_intent(entry, next_stage)
            if intent is None:
                continue
            self._store.update_metadata(entry.id, {COMMITMENT_STAGE_KEY: next_stage})
            self._dispatch(intent)
            intents.append(intent)
            logger.info(
                "[commitment_watcher] wake dispatched: commitment={} stage={}/{}",
                entry.id,
                next_stage,
                intent.max_reminds,
            )
        for entry_id in expired:
            self._store.update_metadata(entry_id, {COMMITMENT_STATUS_KEY: COMMITMENT_STATUS_EXPIRED})
            logger.info("[commitment_watcher] commitment={} expired", entry_id)
        return intents

    def _build_intent(self, entry: Any, next_stage: int) -> WakeIntent | None:
        """Wrap one due memory node into a WakeIntent (the Harness contract)."""
        meta = entry.metadata or {}
        try:
            deadline_ts = float(meta.get("deadline_ts") or 0.0)
        except (TypeError, ValueError):
            return None
        if deadline_ts <= 0.0:
            return None
        try:
            max_reminds = max(1, int(meta.get("max_reminds") or 2))
        except (TypeError, ValueError):
            max_reminds = 2
        return WakeIntent(
            commitment_id=str(entry.id),
            content=str(entry.content or ""),
            deadline_ts=deadline_ts,
            notify_stage=next_stage,
            max_reminds=max_reminds,
            prompt=_build_wake_prompt(
                str(entry.id),
                str(entry.content or ""),
                deadline_ts,
                next_stage,
                max_reminds,
            ),
            push_gateways=list(self._push_gateways_fn() or []),
        )

    def _dispatch(self, intent: WakeIntent) -> None:
        """Hand the intent to the scheduler's standard one-shot pipeline."""
        self._scheduler.schedule(
            name=f"commitment wake {intent.commitment_id[:8]}",
            prompt=intent.prompt,
            fire_at=time.time() + 1.0,
            max_failures=1,
            metadata={
                "origin": "commitment_wake",
                "commitment_id": intent.commitment_id,
                "notify_stage": intent.notify_stage,
                "wake": intent.to_dict(),
            },
            agent_config=self._agent_config,
            push_gateways=list(intent.push_gateways),
        )


class EncreScheduler:
    """Persistent cron-based job scheduler for autonomous agent workflows.

    Jobs survive restarts when `durable_path` is set.
    Supports one-shot reminders and recurring cron jobs.

    Usage:
        sched = EncreScheduler()  # persists to ~/.dunimd/encre/jobs.json
        job_id = sched.schedule(
            name="daily PR review",
            prompt="Review all open PRs and report issues",
            cron="0 9 * * 1-5",
        )
        await sched.start(agent)  # starts background polling
    """

    def __init__(
        self,
        durable_path: str = "",
        poll_interval_seconds: float = 30.0,
    ) -> None:
        self._jobs: dict[str, ScheduledJob] = {}
        if not durable_path:
            from encre.config import get_data_dir

            durable_path = str(get_data_dir() / "jobs.json")
        self._durable_path = Path(durable_path)
        self._history_path = self._durable_path.with_name("automation_history.json")
        self._executions: list[JobExecution] = []
        self._poll_interval = poll_interval_seconds
        # Wall-clock moment this scheduler session began. Recurring jobs only
        # fire for occurrences that fall within this session; occurrences that
        # passed while the process was offline are skipped (never fired late).
        self._started_at: float = time.time()
        self._running = False
        self._task: asyncio.Task[Any] | None = None
        # Background tasks for in-flight automation jobs. Automations are
        # unrestricted: any number may run concurrently (bounded only by the
        # machine), so each due job is launched as its own task and tracked
        # here to keep a strong reference and surface any crash.
        self._job_tasks: set[asyncio.Task[Any]] = set()
        self._agent_factory: Callable[[dict[str, Any] | None], Any] | None = None
        self._on_complete: Callable[[ScheduledJob], None] | None = None
        self._on_progress: Callable[[ScheduledJob, str, dict[str, Any]], Awaitable[None]] | None = None

        self._load()

    def schedule(
        self,
        name: str,
        prompt: str,
        cron: str = "",
        fire_at: float | None = None,
        max_failures: int = 3,
        metadata: dict[str, Any] | None = None,
        agent_config: dict[str, Any] | None = None,
        model_index: int = -1,
        push_gateways: list[str] | None = None,
    ) -> str:
        """Schedule a new job. Returns the job ID."""
        import uuid

        job_id = uuid.uuid4().hex[:12]

        if cron:
            schedule_type = ScheduleType.RECURRING
            cron_obj = CronSchedule.parse(cron)
        elif fire_at:
            schedule_type = ScheduleType.ONE_SHOT
            cron_obj = None
        else:
            # Default: fire immediately once
            schedule_type = ScheduleType.ONE_SHOT
            cron_obj = None
            fire_at = time.time() + 1

        job = ScheduledJob(
            id=job_id,
            name=name,
            prompt=prompt,
            schedule_type=schedule_type,
            cron=cron_obj,
            fire_at=fire_at,
            max_failures=max_failures,
            metadata=metadata or {},
            model_index=model_index,
            push_gateways=push_gateways or [],
        )
        job._agent_config = agent_config
        self._jobs[job_id] = job
        if self._durable_path:
            self._save()
        return job_id

    def schedule_workflow(
        self,
        name: str,
        dag_definition: str,
        cron: str = "",
        fire_at: float | None = None,
        max_failures: int = 3,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Schedule a DAG workflow job. Returns the job ID.

        Args:
            name: Human-readable name for this workflow.
            dag_definition: JSON string compatible with
                ``EncreTaskPlanner.plan_from_json()``.  Contains
                ``tasks``, ``entry_tasks``, and ``exit_tasks``.
            cron: Optional cron expression for recurring execution.
            fire_at: Optional absolute timestamp for one-shot execution.
            max_failures: Maximum consecutive failures before suspension.
            metadata: Optional metadata dict.

        Returns:
            The newly-created job ID.
        """
        import uuid

        job_id = uuid.uuid4().hex[:12]

        if cron:
            schedule_type = ScheduleType.RECURRING
            cron_obj = CronSchedule.parse(cron)
        elif fire_at:
            schedule_type = ScheduleType.ONE_SHOT
            cron_obj = None
        else:
            schedule_type = ScheduleType.ONE_SHOT
            cron_obj = None
            fire_at = time.time() + 1

        job = ScheduledJob(
            id=job_id,
            name=name,
            prompt="",  # DAG workflows do not use the prompt directly
            schedule_type=schedule_type,
            cron=cron_obj,
            fire_at=fire_at,
            max_failures=max_failures,
            metadata=metadata or {},
            dag_definition=dag_definition,
        )
        self._jobs[job_id] = job
        if self._durable_path:
            self._save()
        return job_id

    def cancel(self, job_id: str) -> bool:
        """Cancel a pending or recurring job."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.state = JobState.CANCELLED
        if self._durable_path:
            self._save()
        return True

    def toggle_job(self, job_id: str) -> bool:
        """Suspend or resume a job. Returns True if now running (resumed), False if suspended."""
        job = self._jobs.get(job_id)
        if job is None or job.state == JobState.CANCELLED:
            return False
        job.suspended = not job.suspended
        if self._durable_path:
            self._save()
        return not job.suspended

    def update_job(
        self,
        job_id: str,
        *,
        name: str,
        prompt: str,
        cron: str,
        tag: str,
        model_index: int = -1,
        agent_config: dict[str, Any] | None = None,
        push_gateways: list[str] | None = None,
    ) -> bool:
        """Update a job's properties. Returns True on success."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.name = name
        job.prompt = prompt
        job.cron = CronSchedule.parse(cron) if cron else None
        job.metadata["tag"] = tag
        job.model_index = model_index
        job._agent_config = agent_config
        job.fail_count = 0
        job.state = JobState.PENDING
        if push_gateways is not None:
            job.push_gateways = push_gateways
        if self._durable_path:
            self._save()
        return True

    def delete_job(self, job_id: str) -> bool:
        """Remove a job configuration. Execution history is preserved."""
        if job_id not in self._jobs:
            return False
        del self._jobs[job_id]
        if self._durable_path:
            self._save()
        return True

    def delete_job_execution_by_session_id(self, session_id: str) -> bool:
        """Delete any execution record with the given session_id.

        Returns True if an execution was found and removed.
        """
        for i, exec_entry in enumerate(self._executions):
            if exec_entry.session_id == session_id:
                del self._executions[i]
                if self._durable_path:
                    self._save_history()
                return True
        return False

    def delete_job_execution(self, entry_id: str) -> str | None:
        """Delete a single execution record by entry_id (format: job_id_timestamp).

        Returns the session_id of the deleted execution if found, None otherwise.
        The caller should clean up the sub-agent session directory from disk.
        """
        try:
            job_id, time_str = entry_id.rsplit("_", 1)
            exec_time = float(time_str)
        except (ValueError, IndexError):
            return None
        for i, exec_entry in enumerate(self._executions):
            if exec_entry.job_id == job_id and exec_entry.time is not None and abs(exec_entry.time - exec_time) < 0.001:
                sid = exec_entry.session_id
                del self._executions[i]
                if self._durable_path:
                    self._save_history()
                return sid
        return None

    def rename_job_execution(self, entry_id: str, new_name: str) -> bool:
        """Rename a single execution record by entry_id (format: job_id_timestamp).

        Returns True if the entry was found and renamed, False otherwise.
        """
        try:
            job_id, time_str = entry_id.rsplit("_", 1)
            exec_time = float(time_str)
        except (ValueError, IndexError):
            return False
        for exec_entry in self._executions:
            if exec_entry.job_id == job_id and exec_entry.time is not None and abs(exec_entry.time - exec_time) < 0.001:
                exec_entry.name = new_name.strip()[:64]
                if self._durable_path:
                    self._save_history()
                return True
        return False

    def get_execution_history(self) -> list[JobExecution]:
        """Return all execution records sorted by time (newest first)."""
        return sorted(self._executions, key=lambda e: e.time or 0, reverse=True)

    def cancel_all(self) -> int:
        """Cancel all jobs. Returns count of cancelled jobs."""
        count = 0
        for job in self._jobs.values():
            if job.state in (JobState.PENDING, JobState.RUNNING):
                job.state = JobState.CANCELLED
                count += 1
        if self._durable_path and count > 0:
            self._save()
        return count

    def get_job(self, job_id: str) -> ScheduledJob | None:
        return self._jobs.get(job_id)

    def list_jobs(self, state: JobState | None = None, include_finished: bool = False) -> list[ScheduledJob]:
        """List jobs, optionally filtered by state.

        By default, CANCELLED jobs and completed/failed ONE_SHOT jobs
        are excluded to avoid cluttering the UI with stale entries.
        """
        jobs = list(self._jobs.values())
        if state is not None:
            jobs = [j for j in jobs if j.state == state]
        elif not include_finished:
            jobs = [
                j
                for j in jobs
                if j.state != JobState.CANCELLED
                and not (j.schedule_type == ScheduleType.ONE_SHOT and j.state in (JobState.COMPLETED, JobState.FAILED))
            ]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def on_job_complete(self, callback: Callable[[ScheduledJob], None]) -> None:
        """Register a callback for job completion."""
        self._on_complete = callback

    def on_job_progress(self, callback: Callable[[ScheduledJob, str, dict[str, Any]], Awaitable[None]]) -> None:
        """Register an async callback for real-time job execution progress.

        Called for each streaming event during ``agent.run()`` with
        the job, event type name (e.g. ``"text_delta"``), and event data dict.
        The callback is awaited so events are broadcast in order.
        """
        self._on_progress = callback

    async def start(self, agent_factory: Callable[[dict[str, Any] | None], Any]) -> None:
        """Start the background scheduler loop.

        Args:
            agent_factory: A callable(config_dict) that returns a fresh EncreAgent instance.
                          Called for each job execution with the job's stored agent_config.
        """
        self._agent_factory = agent_factory
        self._started_at = time.time()
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # Cancel any automation jobs still in flight so shutdown is clean.
        if self._job_tasks:
            for t in list(self._job_tasks):
                t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*self._job_tasks, return_exceptions=True)
            self._job_tasks.clear()

    async def _loop(self) -> None:
        """Main scheduler loop -- polls for due jobs."""
        while self._running:
            try:
                now = time.time()
                due_jobs: list[ScheduledJob] = []

                for job in self._jobs.values():
                    if job.state not in (JobState.PENDING,):
                        continue
                    if job.suspended:
                        continue
                    if job.schedule_type == ScheduleType.ONE_SHOT:
                        if job.fire_at and now >= job.fire_at:
                            due_jobs.append(job)
                        else:
                            pass
                    elif job.schedule_type == ScheduleType.RECURRING and job.cron:
                        # First occurrence: compute next_fire from the fixed
                        # creation timestamp so the result is stable across
                        # poll cycles.  Using the current poll time (now)
                        # would make next_fire keep shifting forward (e.g.
                        # "every minute" would return now+60s on every call,
                        # making now >= next_fire *always* false -- the job
                        # would never fire).
                        next_fire = job.cron.next_fire(job.created_at) if job.last_fired is None else job.cron.next_fire(job.last_fired)
                        if next_fire and now >= next_fire:
                            if next_fire >= self._started_at:
                                # The scheduled occurrence fell within this
                                # scheduler session, so the process was alive
                                # at that time -- fire it (normal catch-up
                                # within the same session, e.g. a poll tick
                                # a few seconds late).
                                due_jobs.append(job)
                            else:
                                # The occurrence already passed while the
                                # process was offline (e.g. the app was
                                # started after the scheduled time). Never
                                # fire a missed occurrence on startup: advance
                                # last_fired past it so the job is armed for
                                # the next future occurrence instead.
                                logger.info(
                                    "[scheduler] recurring job {} (name={}) skipped a missed"
                                    " occurrence at {} (scheduler was offline);"
                                    " will wait for the next scheduled time",
                                    job.id,
                                    job.name,
                                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(next_fire)),
                                )
                                job.last_fired = next_fire
                        else:
                            logger.debug(
                                "[scheduler] recurring job {} not due yet: next_fire={} now={} last_fired={}",
                                job.id,
                                next_fire,
                                now,
                                job.last_fired,
                            )
                    else:
                        logger.debug("[scheduler] job {} skipped: type={} cron={}", job.id, job.schedule_type, job.cron)

                logger.debug("[scheduler] poll: {} jobs, {} due", len(self._jobs), len(due_jobs))

                for job in due_jobs:
                    logger.info("[scheduler] executing job {} (name={})", job.id, job.name)
                    # Automations are unrestricted: launch each due job as an
                    # independent background task so any number can run in
                    # parallel (limited only by the machine), instead of
                    # serializing them one-after-another behind ``await``.
                    self._spawn_job(job)

                if self._durable_path:
                    self._save()

                await asyncio.sleep(self._poll_interval)
            except Exception as e:
                logger.exception("[scheduler] _loop iteration crashed: {}", e)
                await asyncio.sleep(self._poll_interval)

    def _spawn_job(self, job: ScheduledJob) -> None:
        """Launch a job as an independent, unrestricted background task.

        Automations run in parallel with no artificial concurrency cap: the
        only limit is what the machine can handle. The task is tracked so it
        keeps a strong reference (otherwise it could be garbage-collected
        mid-run) and so any unhandled exception is logged instead of lost.
        """
        task = asyncio.create_task(self._execute_job(job))
        self._job_tasks.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            self._job_tasks.discard(t)
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.error("[scheduler] job {} crashed: {}", job.id, exc)

        task.add_done_callback(_done)

    async def _execute_job(self, job: ScheduledJob) -> None:
        """Execute a single scheduled job.

        Every job execution is ALWAYS a sub-agent -- the same ``agent``
        tool path used by the main conversation. The parent ``EncreAgent``
        built by ``agent_factory`` is just a config/transport holder;
        the actual work is delegated to :meth:`EncreLoop._run_sub_agent`,
        which:

        * creates a fresh ``EncreAgent`` (depth 1),
        * tags it with ``channel = "sub_agent"``,
        * persists it under ``<data_dir>/sub_agents/<session_id>/``,
        * returns ``{"content", "messages", "session_id"}``.

        DAG jobs follow the same rule: each node is run via
        :meth:`EncreLoop._run_sub_agent` and produces its own sub-agent
        session.
        """
        if self._agent_factory is None:
            logger.warning("[scheduler] _agent_factory is None, cannot execute job {}", job.id)
            return

        logger.debug(
            "[scheduler] _execute_job {} (name={}) starting, state={}, cron={}, last_fired={}",
            job.id,
            job.name,
            job.state,
            job.cron.to_expression() if job.cron else None,
            job.last_fired,
        )

        job.state = JobState.RUNNING
        job.last_fired = time.time()

        # Lightweight execution record. The full transcript is owned by
        # the sub-agent session that _run_sub_agent will create.
        exec_entry = JobExecution(
            time=time.time(),
            state="RUNNING",
            result="",
            session_id=uuid.uuid4().hex,
            fail_count=0,
            name=job.name,
            job_id=job.id,
        )
        self._executions.append(exec_entry)
        job.session_id = exec_entry.session_id
        if self._durable_path:
            self._save()
            self._save_history()

        # Notify frontend immediately that the job is now RUNNING so the
        # automation panel can update its state (status → RUNNING, history
        # entry appears) without waiting for execution to finish.
        if self._on_complete:
            try:
                self._on_complete(job)
            except Exception:
                logger.debug("[scheduler] start-state broadcast failed for job {}", job.id, exc_info=True)

        # Build an event translator that turns raw AgentEvents into the
        # existing automation_stream_event wire format. This is the only
        # place that knows the wire format -- everything else speaks the
        # sub-agent protocol.
        async def _translate_event(event: Any) -> None:
            from encre.utils.types import (
                TextDelta,
                ThinkingDelta,
                ToolCallDelta,
                ToolCallEnd,
                ToolCallStart,
                ToolProgress,
                ToolResult,
            )

            event_data: dict[str, Any] = {}
            event_name = ""
            if isinstance(event, TextDelta):
                event_name = "text_delta"
                event_data = {"text": event.text}
            elif isinstance(event, ThinkingDelta):
                event_name = "thinking_delta"
                event_data = {"text": event.text}
            elif isinstance(event, ToolCallStart):
                event_name = "tool_call_start"
                event_data = {"id": event.id, "name": event.name}
            elif isinstance(event, ToolCallDelta):
                event_name = "tool_call_delta"
                event_data = {"id": event.id, "key": event.key, "value": event.value}
            elif isinstance(event, ToolCallEnd):
                event_name = "tool_call_end"
                event_data = {"id": event.id}
            elif isinstance(event, ToolProgress):
                event_name = "tool_progress"
                event_data = {
                    "id": event.id,
                    "tool_name": event.tool_name,
                    "status": event.status,
                    "sub_agent_messages": event.sub_agent_messages,
                }
            elif isinstance(event, ToolResult):
                event_name = "tool_result"
                content = event.content
                if len(content) > 100000:
                    content = content[:100000] + "\n... (truncated)"
                event_data = {
                    "id": event.id,
                    "content": content,
                    "is_error": event.is_error,
                    "sub_agent_messages": event.sub_agent_messages,
                }
            else:
                return
            if self._on_progress is not None:
                try:
                    await self._on_progress(job, event_name, event_data)
                except Exception:
                    logger.warning("[scheduler] progress callback failed for '{}'", event_name, exc_info=True)

        try:
            # Parent agent is a transport/config holder only. _run_sub_agent
            # will create a fresh sub-EncreAgent internally. This call is
            # INSIDE the try block so that factory failures (e.g. invalid
            # model config causing rebuild_backend to raise) are caught by
            # the exception handler below, which correctly resets job state
            # to PENDING/FAILED instead of crashing the entire _loop().
            parent_agent = self._agent_factory(job._agent_config)

            # Notify frontend that execution has started. session_id is
            # filled in once the sub-agent is created.
            if self._on_progress:
                try:
                    await self._on_progress(
                        job,
                        "start",
                        {
                            "id": job.id,
                            "name": job.name,
                            "prompt": job.prompt,
                            "session_id": exec_entry.session_id,
                        },
                    )
                except Exception:
                    logger.warning("[scheduler] progress callback failed for 'start' event", exc_info=True)

            if job.dag_definition:
                # DAG path: each node is a sub-agent run.
                node_session_ids = await self._execute_dag_job(
                    job,
                    parent_agent,
                    _translate_event,
                )
                exec_entry.session_id = node_session_ids[0] if node_session_ids else None
                # The wrapper session is the first node; remaining nodes
                # are reachable from its context but are also listed
                # below for the history view.
                job.session_id = exec_entry.session_id
                if job.state == JobState.RUNNING:
                    job.state = JobState.COMPLETED
                    job.fail_count = 0
                    exec_entry.state = "COMPLETED"
                else:
                    exec_entry.state = "FAILED" if job.state == JobState.FAILED else "PENDING"
                exec_entry.result = (job.last_result or "")[:5000]
            else:
                # Non-DAG path: a single sub-agent run. The sub-agent
                # owns the session -- we just consume its return value.
                sub_result = await parent_agent.loop._run_sub_agent(
                    prompt=job.prompt,
                    system_prompt=None,
                    tool_policy="all",
                    progress_callback=lambda messages: self._emit_execution_snapshot(
                        job,
                        exec_entry,
                        messages,
                    ),
                    event_callback=_translate_event,
                    session_id=exec_entry.session_id,
                )

                session_id = sub_result.get("session_id")
                exec_entry.session_id = session_id
                job.session_id = session_id

                final_content = sub_result.get("content") or ""
                # A sub-agent can finish with an in-band failure
                # ("Error: Sub-agent failed" from Finish(reason="error")).
                # Treating that as COMPLETED lied to the history view --
                # surface it as a real FAILED execution instead.
                sub_failed = bool(sub_result.get("error")) or final_content.startswith("Error:")
                if sub_failed:
                    logger.warning(
                        "[scheduler] sub-agent reported failure: job={} name={} result={:.200s}", job.id, job.name, final_content
                    )
                    job.fail_count += 1
                    job.last_result = final_content[:2000]
                    exec_entry.state = "FAILED"
                    exec_entry.result = final_content[:5000]
                    exec_entry.fail_count = job.fail_count
                    if job.fail_count >= job.max_failures:
                        job.state = JobState.FAILED
                    else:
                        job.state = JobState.PENDING
                else:
                    job.last_result = final_content[:2000]
                    job.state = JobState.COMPLETED
                    job.fail_count = 0
                    job.last_fired = time.time()  # → mark successful execution
                    exec_entry.state = "COMPLETED"
                    exec_entry.result = final_content[:5000]

        except Exception as e:
            logger.exception("[scheduler] job execution failed: job={} name={} error={}", job.id, job.name, e)
            job.fail_count += 1
            job.last_result = f"Error: {e}"
            # This execution is terminal even when the recurring job itself
            # remains pending for a later retry.
            exec_entry.state = "FAILED"
            exec_entry.result = job.last_result
            exec_entry.fail_count = job.fail_count
            if job.fail_count >= job.max_failures:
                job.state = JobState.FAILED
            else:
                job.state = JobState.PENDING

        # Notify frontend that execution has finished
        if self._on_progress:
            try:
                # Classify the raw result text into the unified error taxonomy
                # (e.g. rate_limit for a 429, network_timeout for timeouts) so
                # the UI shows a meaningful code instead of a bare placeholder.
                err_code = ""
                if exec_entry.state == "FAILED":
                    try:
                        from encre.errors import classify_error_code

                        err_code = classify_error_code(job.last_result or "").value
                    except Exception:
                        err_code = "AUTOMATION_EXECUTION_FAILED"
                await self._on_progress(
                    job,
                    "finish",
                    {
                        "state": exec_entry.state,
                        "result": (job.last_result or "")[:2000],
                        "error_code": err_code,
                    },
                )
            except Exception:
                logger.warning("[scheduler] progress callback failed for 'finish' event", exc_info=True)

        if self._on_complete:
            try:
                self._on_complete(job)
            except Exception:
                logger.warning("[scheduler] on_complete callback failed for job {}", job.id, exc_info=True)

        if job.schedule_type == ScheduleType.RECURRING:
            job.state = JobState.PENDING  # reset for next cycle after notification
        # ONE_SHOT stays COMPLETED/FAILED
        if self._durable_path:
            self._save()
            self._save_history()

    async def _emit_execution_snapshot(
        self,
        job: ScheduledJob,
        execution: JobExecution,
        messages: list[dict[str, Any]],
    ) -> None:
        """Publish the current canonical sub-agent transcript to clients."""
        if self._on_progress is None:
            return
        await self._on_progress(
            job,
            "snapshot",
            {
                "session_id": execution.session_id,
                "messages": messages,
            },
        )

    async def _execute_dag_job(
        self,
        job: ScheduledJob,
        parent_agent: Any,
        event_callback: Any = None,
    ) -> list[str]:
        """Execute a scheduled DAG workflow job.

        Each DAG node is run via :meth:`EncreLoop._run_sub_agent` so that
        every node produces a real sub-agent session persisted under
        ``<data_dir>/sub_agents/<session_id>/``. The returned list is
        the ordered session_ids of the nodes, in execution order.
        """
        from encre.dag.executor import DagExecutor
        from encre.swarm.planner import EncreTaskPlanner

        if not job.dag_definition.strip():
            raise ValueError("dag_definition is empty")

        planner = EncreTaskPlanner()
        task_tree = planner.plan_from_json(job.name, job.dag_definition)

        if not task_tree.nodes:
            raise ValueError("dag_definition produced an empty task tree")

        node_session_ids: list[str] = []
        node_summaries: list[str] = []

        def _dag_node_runner(node, context):
            """Run a single DAG node as a sub-agent.

            ``DagExecutor`` invokes runners synchronously, so we bridge
            to the asyncio loop via :func:`asyncio.run_coroutine_threadsafe`
            (or :func:`asyncio.run` if no loop is running).
            """
            nonlocal node_session_ids, node_summaries
            prompt = node.description or node.name
            coro = self._run_dag_node_sub_agent(
                parent_agent=parent_agent,
                prompt=prompt,
                node_name=node.name,
                event_callback=event_callback,
            )
            try:
                loop = asyncio.get_running_loop()
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                result = future.result()
            except RuntimeError:
                # No running loop -- execute in a fresh one.
                result = asyncio.run(coro)
            session_id, content = result
            if session_id:
                node_session_ids.append(session_id)
            context[node.id] = content
            node_summaries.append(f"[{node.name}] session={session_id or '-'} text_len={len(content)}")
            node.result = content
            return content

        executor = DagExecutor(
            runner=_dag_node_runner,
            max_retries=1,
            retry_delay=1.0,
        )

        dag_result = await executor.run(
            task_tree,
            context={},
        )

        # Build a human-readable summary for the job record.
        summary_lines = [
            f"DAG workflow completed: success={dag_result.success}",
            f"Total duration: {dag_result.total_duration:.2f}s",
            f"Sub-agent sessions: {len(node_session_ids)}",
        ]
        for _nid, nr in dag_result.node_results.items():
            summary_lines.append(f"  [{nr.status}] {nr.node_name} ({nr.attempts} attempt(s), {nr.finished_at - nr.started_at:.1f}s)")
            if nr.error:
                summary_lines.append(f"    error: {nr.error}")

        job.last_result = "\n".join(summary_lines)[:5000]
        if not dag_result.success:
            job.fail_count += 1
            if job.fail_count >= job.max_failures:
                job.state = JobState.FAILED
            else:
                job.state = JobState.PENDING
        return node_session_ids

    async def _run_dag_node_sub_agent(
        self,
        parent_agent: Any,
        prompt: str,
        node_name: str,
        event_callback: Any = None,
    ) -> tuple[str | None, str]:
        """Run a single DAG node via ``_run_sub_agent``.

        Returns ``(session_id, content)``. The session_id is the
        sub-agent's persisted session, the content is the assistant
        text extracted from the sub-agent's messages.
        """
        result = await parent_agent.loop._run_sub_agent(
            prompt=prompt,
            system_prompt=f"You are executing a single step of a scheduled workflow. Step: {node_name}",
            tool_policy="all",
            progress_callback=None,
            event_callback=event_callback,
        )
        session_id = result.get("session_id")
        content = result.get("content") or ""
        return session_id, content

    def _save(self) -> None:
        if not self._durable_path:
            return
        self._durable_path.parent.mkdir(parents=True, exist_ok=True)
        data = [j.to_dict() for j in self._jobs.values()]
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        try:
            payload = encrypt(payload)
        except Exception:
            # Fail-closed: skip the durable save rather than write plaintext.
            logger.error("scheduler: encryption failed; skipping jobs save", exc_info=True)
            return
        # Atomic write: write to temp file then rename
        tmp_path = self._durable_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        tmp_path.replace(self._durable_path)

    def _save_history(self) -> None:
        if not self._history_path:
            return
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        data = [e.to_dict() for e in self._executions]
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        try:
            payload = encrypt(payload)
        except Exception:
            # Fail-closed: skip the durable save rather than write plaintext.
            logger.error("scheduler: encryption failed; skipping history save", exc_info=True)
            return
        tmp_path = self._history_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        tmp_path.replace(self._history_path)

    def _load(self) -> None:
        if not self._durable_path or not self._durable_path.exists():
            return
        try:
            with open(self._durable_path, encoding="utf-8") as f:
                raw = f.read().strip()
            if not raw:
                # Empty/corrupted file - reset to a valid empty list.
                self._jobs = {}
                return
            if raw and not raw.startswith("["):
                with contextlib.suppress(Exception):
                    raw = decrypt(raw)
            data = json.loads(raw)
            for item in data:
                job = ScheduledJob.from_dict(item)
                # Recover jobs stuck in RUNNING state after a crash/restart.
                # Recurring jobs should return to PENDING so they can fire again.
                if job.state == JobState.RUNNING:
                    if job.schedule_type == ScheduleType.RECURRING:
                        job.state = JobState.PENDING
                    else:
                        job.state = JobState.FAILED
                        job.fail_count += 1
                self._jobs[job.id] = job
                # Migrate legacy executions stored inside the job into the
                # global history store. This preserves history when upgrading
                # from older versions and removes duplication.
                for exec_entry in job.executions:
                    if exec_entry.job_id == "":
                        exec_entry.job_id = job.id
                    if exec_entry.name == "":
                        exec_entry.name = job.name
                    # Avoid adding the same execution twice by checking
                    # the job_id + time composite key.
                    if not any(
                        e.job_id == exec_entry.job_id
                        and e.time is not None
                        and exec_entry.time is not None
                        and abs(e.time - exec_entry.time) < 0.001
                        for e in self._executions
                    ):
                        self._executions.append(exec_entry)
                job.executions = []
            # Also load the dedicated history file if it exists.
            if self._history_path.exists():
                with open(self._history_path, encoding="utf-8") as f:
                    raw_history = f.read().strip()
                if raw_history:
                    if raw_history and not raw_history.startswith("["):
                        with contextlib.suppress(Exception):
                            raw_history = decrypt(raw_history)
                    history_data = json.loads(raw_history)
                    for item in history_data:
                        exec_entry = JobExecution.from_dict(item)
                        # The dedicated history file is authoritative. It can
                        # contain a user-renamed execution that has the same
                        # identity as a legacy record migrated from jobs.json.
                        # Replace that legacy record instead of discarding the
                        # newer, user-edited one.
                        for i, existing in enumerate(self._executions):
                            if (
                                existing.job_id == exec_entry.job_id
                                and existing.time is not None
                                and exec_entry.time is not None
                                and abs(existing.time - exec_entry.time) < 0.001
                            ):
                                self._executions[i] = exec_entry
                                break
                        else:
                            self._executions.append(exec_entry)
            # Reconcile executions left in RUNNING state by a crash or
            # unclean shutdown. A live scheduler always transitions an
            # execution to COMPLETED or FAILED, so any RUNNING record at
            # load time was interrupted and never finished -- marking it
            # FAILED keeps the history timeline from showing an eternal
            # spinner for a run that can no longer complete.
            for exec_entry in self._executions:
                if exec_entry.state == "RUNNING":
                    exec_entry.state = "FAILED"
            # Persist the migrated history so the legacy executions are not
            # lost even if the old job file is overwritten.
            if self._durable_path and self._executions:
                self._save_history()
                self._save()
        except json.JSONDecodeError as e:
            logger.warning(f"Could not parse durable job store {self._durable_path}: {e} - resetting to empty")
            self._jobs = {}
        except KeyError as e:
            logger.warning(f"Missing key in durable job store entry: {e}")
        except Exception as e:
            logger.error(f"Failed to load durable job store {self._durable_path}: {e}", exc_info=True)
