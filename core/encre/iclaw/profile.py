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

"""iClaw launcher profile: self-improvement add-ons layered over the server.

The core server launcher already owns the runtime -- sessions, router, WS
transport, scheduler, hooks, safety, plugins. This profile deliberately does
not re-create any of that; it layers only what is unique to iClaw on top of
:class:`encre.server.app.EncreServer`:

- learning / evolution / reflex / metacognition / feedback subsystems
- memory consolidator and goal runner
- the post-run self-improvement pipeline
- housekeeping loops (cleanup, heartbeat, reflex scan)
- the channel submit pipeline (hooks + compaction + learning feed)

Data flow: a channel prompt arrives at :meth:`IclawProfile.submit` or
:meth:`IclawProfile.submit_stream`, which delegates execution to
:class:`encre.protocol.session_router.EventRouter` (the same router the
gateway and desktop path use), observes the event stream for tool activity,
and then feeds a :class:`encre.iclaw.post_run.PostRunPipeline` so the run is
reflected back into the agent's memory, evolution, and feedback state.

Public API (mirrors the established iClaw engine surface so callers are
unchanged):

- :meth:`IclawProfile.start` / :meth:`IclawProfile.stop`
- :meth:`IclawProfile.submit` / :meth:`IclawProfile.submit_stream`
- :meth:`IclawProfile.run_goal` / :meth:`IclawProfile.run_swarm`
- :meth:`IclawProfile.schedule_job` / :meth:`IclawProfile.cancel_job`
- :meth:`IclawProfile.get_stats` / :meth:`IclawProfile.get_health`
"""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from encre.agent import EncreAgent
from encre.compact.engine import EncreCompactEngine
from encre.compact.engine import EncreMultiStagePipeline
from encre.config import EncreConfig, get_data_dir
from encre.goal import (
    EncreGoalRunner,
    GoalDefinition,
    GoalEvent,
    GoalResult,
    GoalStatus,
)
from encre.iclaw.post_run import PostRunOrchestrator, PostRunPipeline, RunSummary
from encre.improve.evolution.config import EvolutionConfig  # noqa: F401  (re-exported for profile consumers)
from encre.improve.evolution.learner import EncreEvolutionLearner
from encre.improve.evolution.meta import EncreMetaCognition
from encre.improve.evolution.reflex import EncreReflexLoop
from encre.improve.feedback.learner import EncreFeedbackLearner
from encre.improve.learning.consolidator import MemoryConsolidator
from encre.improve.learning.engine import LearningEngine
from encre.protocol.session_router import EventRouter
from encre.scheduler import EncreScheduler, ScheduledJob
from encre.swarm.blackboard import EncreBlackboard
from encre.swarm.planner import EncreTaskPlanner
from encre.swarm.roles import AgentRole, RoleRegistry  # noqa: F401  (AgentRole re-exported)
from encre.swarm.session import EncreSwarmSession
from encre.utils.types import (
    AgentEvent,
    Finish,
    ToolResult,
)

logger = logging.getLogger("encre.iclaw")


@dataclass
class DaemonStats:
    """Counters collected while the iClaw profile is running.

    Every field is monotonically increasing except ``started_at``, which is a
    timestamp. Values are mutated from the event loop only, and
    :meth:`IclawProfile.get_stats` returns a fresh dict each call so a caller
    can never observe a half-updated snapshot.

    Attributes:
        started_at: Epoch seconds when :meth:`IclawProfile.start` ran; ``0.0``
            means the profile has never been started.
        sessions_created: Submissions accepted by the profile, whether they
            finished or not.
        sessions_completed: Submissions that yielded a :class:`Finish` event.
        jobs_executed: Scheduled jobs registered (not executions).
        skills_generated: Skills written by the learning stage of the
            post-run pipeline.
        consolidation_runs: Memory consolidation cycles completed.
        swarm_tasks_executed: Swarm executions started.
        compact_runs: Context compactions actually applied to a prompt.
        evolution_observations: Tool results observed by the evolution
            learner during a run.
        reflex_evaluations: Cycles the reflex scan loop completed.
        feedback_records: User corrections recorded by the feedback learner.
        errors: Count of failures caught inside the background loops.
    """

    started_at: float = 0.0
    sessions_created: int = 0
    sessions_completed: int = 0
    jobs_executed: int = 0
    skills_generated: int = 0
    consolidation_runs: int = 0
    swarm_tasks_executed: int = 0
    compact_runs: int = 0
    evolution_observations: int = 0
    reflex_evaluations: int = 0
    feedback_records: int = 0
    errors: int = 0


class IclawProfile:
    """iClaw add-on profile attached to a running :class:`EncreServer`.

    Owns only the self-improvement and autonomy subsystems; transport,
    sessions, and scheduling come from the server. Presents the same public
    surface callers already rely on (submit / run_goal / run_swarm /
    schedule_job / stats), so existing call sites keep working unchanged.

    Invariants:

    - All subsystem attributes are ``None`` until :meth:`start` has run, and
      every accessor therefore checks for ``None`` before delegating. A
      profile built but never started degrades to unavailable rather than
      raising.
    - ``_running`` is the single authority on liveness. Background loops test
      it and exit, so :meth:`stop` never has to wait for a loop to finish a
      full interval.
    - Subsystem state is created once in :meth:`start` and released once in
      :meth:`stop`; calling :meth:`start` twice is a no-op.

    Lifecycle: construct with the server's agent, router, and scheduler ->
    :meth:`start` (assemble subsystems, spawn background loops) -> serve ->
    :meth:`stop` (cancel loops, stop the consolidator and learning engine).

    Attributes:
        stats: Live :class:`DaemonStats` counters; read through
            :meth:`get_stats` rather than directly so uptime is computed.
        agent: The server's agent; the post-run pipeline and analyze callable
            read its backend and registries.
        router: The :class:`encre.protocol.session_router.EventRouter` that
            actually executes prompts.
        scheduler: The :class:`encre.scheduler.EncreScheduler` used for
            scheduled jobs.
        is_running: Whether the profile has been started.
        session_manager: The server-owned session manager, exposed for
            compatibility with callers expecting a direct attribute.

    Args:
        agent: The server agent that subsystems improve.
        router: Router used for prompt submission.
        scheduler: Scheduler used for job operations.
        max_concurrent: Cap on concurrent sessions; also bounds swarm width.
        consolidation_interval: Seconds between memory consolidations. A value
            of ``0`` disables the consolidator.
        enable_compact: Build a context compaction engine.
        enable_evolution: Build the evolution learner.
        enable_reflex: Build the reflex loop and its scan task.
        enable_metacognition: Build the metacognition module.
        enable_feedback: Build the feedback learner.
        enable_swarm: Build the swarm session and blackboard.
        enable_hooks: Emit turn-start/turn-end events to the agent's hook
            system from :meth:`submit_stream`.
    """

    def __init__(
        self,
        agent: EncreAgent,
        *,
        router: EventRouter,
        scheduler: EncreScheduler,
        max_concurrent: int = 20,
        consolidation_interval: int = 3600,
        enable_compact: bool = True,
        enable_evolution: bool = True,
        enable_reflex: bool = True,
        enable_metacognition: bool = True,
        enable_feedback: bool = True,
        enable_swarm: bool = True,
        enable_hooks: bool = True,
    ) -> None:
        """Capture server collaborators and pre-allocate every subsystem slot.

        Construction allocates no subsystems and spawns no tasks; it only stores
        the injected collaborators, records the feature toggles, and sets every
        subsystem attribute to ``None``. Pre-allocating the ``None`` slots is
        what lets the public accessors degrade to "unavailable" instead of
        raising ``AttributeError`` before :meth:`start` runs, and it means
        :meth:`stop` can walk the same slots without an ``if hasattr`` guard.

        Args:
            agent: The server's :class:`encre.agent.EncreAgent`; subsystems
                build against its registries and backend.
            router: The :class:`encre.protocol.session_router.EventRouter`
                that executes prompts and owns the session manager.
            scheduler: The :class:`encre.scheduler.EncreScheduler` used by
                :meth:`schedule_job`.
            max_concurrent: Cap on concurrent sessions; swarm fan-out is
                bounded by the same value.
            consolidation_interval: Seconds between memory consolidations. A
                value of ``0`` or below suppresses the consolidator entirely.
            enable_compact: Build the context compaction engine.
            enable_evolution: Build the evolution learner.
            enable_reflex: Build the reflex loop and its scan task.
            enable_metacognition: Build the metacognition module.
            enable_feedback: Build the feedback learner.
            enable_swarm: Build the swarm session, blackboard, and planner.
            enable_hooks: Emit turn-start/turn-end events to the agent's hook
                system from :meth:`submit_stream`.

        Invariant:
            On return ``self._running`` is ``False``, ``self.stats`` is a fresh
            :class:`DaemonStats`, and every subsystem attribute is ``None``.
        """
        self._agent = agent
        self._router = router
        self._scheduler = scheduler
        self._max_concurrent = max_concurrent
        self._consolidation_interval = consolidation_interval
        self._running = False

        # -- Compact engine -------------------------------------------------
        self._compact_engine: EncreCompactEngine | None = None
        self._enable_compact = enable_compact
        self._compact_max_tokens = 128000

        # -- Hook pipeline --------------------------------------------------
        self._enable_hooks = enable_hooks

        # -- Learning subsystems --------------------------------------------
        self._learning_engine: LearningEngine | None = None
        self._consolidator: MemoryConsolidator | None = None
        self._evolution_learner: EncreEvolutionLearner | None = None
        self._reflex_loop: EncreReflexLoop | None = None
        self._meta_cognition: EncreMetaCognition | None = None
        self._feedback_learner: EncreFeedbackLearner | None = None

        # -- Swarm / multi-agent subsystems ---------------------------------
        self._swarm_session: EncreSwarmSession | None = None
        self._blackboard: EncreBlackboard | None = None
        self._task_planner: EncreTaskPlanner | None = None
        self._role_registry: RoleRegistry | None = None

        # -- Goal runner ----------------------------------------------------
        self._goal_runner: EncreGoalRunner | None = None

        # -- Lifecycle ------------------------------------------------------
        self._cleanup_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._reflex_scan_task: asyncio.Task[None] | None = None

        # -- Stats ----------------------------------------------------------
        self.stats = DaemonStats()

        # -- Post-run pipeline ----------------------------------------------
        self._post_run: PostRunOrchestrator | None = None

    @property
    def is_running(self) -> bool:
        """Return ``True`` while the profile is running.

        Returns:
            Whether :meth:`start` has completed and :meth:`stop` has not.
        """
        return self._running

    @property
    def session_manager(self):
        """Return the server-owned session manager (compat accessor).

        The profile does not own sessions; it forwards to the router's
        manager so callers that read the attribute directly keep
        working.

        Returns:
            The :class:`encre.server.session_manager.SessionManager`.
        """
        return self._router.session_manager

    # -- Start / Stop -------------------------------------------------------

    async def start(self) -> None:
        """Initialise the iClaw-unique subsystems and background loops.

        Transport, sessions, and scheduler come from the server launcher;
        only the self-improvement add-ons are assembled here.

        Idempotent: a second call while already running returns immediately.
        Subsystems that need a file store create their parent directories
        with ``exist_ok=True`` so a fresh install and an existing install
        both work. The reflex scan, cleanup, and heartbeat loops are spawned
        with ``asyncio.create_task`` rather than awaited, because they run
        for the lifetime of the daemon.

        Raises:
            Exception: Any subsystem constructor error propagates; a failed
                :meth:`start` leaves the profile in a half-initialised state,
                so the caller should not serve traffic afterwards.
        """
        if self._running:
            return
        self._running = True
        self.stats.started_at = time.time()
        agent = self._agent
        data_dir = get_data_dir()

        # -- Compact engine -------------------------------------------------
        if self._enable_compact:
            self._compact_engine = EncreCompactEngine(
                strategy=EncreMultiStagePipeline(),
            )

        # -- Learning engine ------------------------------------------------
        self._learning_engine = LearningEngine(agent)
        await self._learning_engine.start()

        # -- Evolution learner ----------------------------------------------
        # Persistence is file-based JSON under the data dir; the learner is the
        # only writer of that file.
        evo_path = data_dir / "evolution" / "state.json"
        evo_path.parent.mkdir(parents=True, exist_ok=True)
        self._evolution_learner = EncreEvolutionLearner(storage_path=str(evo_path))

        # -- Reflex loop ----------------------------------------------------
        self._reflex_loop = EncreReflexLoop(enabled=True)
        self._reflex_scan_task = asyncio.create_task(self._reflex_scan_loop())

        # -- Metacognition --------------------------------------------------
        self._meta_cognition = EncreMetaCognition()

        # -- Feedback learner -----------------------------------------------
        fb_path = data_dir / "feedback" / "corrections.json"
        fb_path.parent.mkdir(parents=True, exist_ok=True)
        self._feedback_learner = EncreFeedbackLearner(storage_path=str(fb_path))

        # -- Memory consolidator --------------------------------------------
        # A non-positive interval disables consolidation entirely, which is how
        # an operator turns off periodic memory rewriting.
        if self._consolidation_interval > 0:
            self._consolidator = MemoryConsolidator(
                agent,
                interval=self._consolidation_interval,
            )
            await self._consolidator.start()

        # -- Goal runner ----------------------------------------------------
        # Optional collaborators are read with getattr so an agent built without
        # memory or skills still gets a working goal runner.
        self._goal_runner = EncreGoalRunner(
            config=agent.config,
            tool_registry=agent.tool_registry,
            hook_system=agent.hook_system,
            safety=agent.safety,
            memory_system=getattr(agent, "memory_system", None),
            skill_registry=getattr(agent, "skill_registry", None),
            telemetry=getattr(agent, "telemetry", None),
        )

        # -- Swarm subsystems -----------------------------------------------
        # Swarm width is capped below the session cap so a swarm cannot consume
        # every concurrency slot the gateway also needs.
        self._blackboard = EncreBlackboard()
        self._task_planner = EncreTaskPlanner()
        self._role_registry = RoleRegistry()
        self._swarm_session = EncreSwarmSession(
            agent=agent,
            max_concurrent=min(10, self._max_concurrent),
        )

        # -- Post-run self-improvement pipeline -----------------------------
        soul_system = getattr(agent, "soul_system", None)
        pipeline = PostRunPipeline(
            agent,
            learning_engine=self._learning_engine,
            evolution_learner=self._evolution_learner,
            feedback_learner=self._feedback_learner,
            consolidator=self._consolidator,
            soul_system=soul_system,
            analyze_fn=self._make_analyze_fn(),
        )
        self._post_run = PostRunOrchestrator(pipeline)

        # -- Background housekeeping ----------------------------------------
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        logger.info(
            "iClaw profile ready -- consolidation=%ds evolution=%s reflex=%s "
            "metacognition=%s feedback=%s swarm=%s",
            self._consolidation_interval,
            self._evolution_learner is not None,
            self._reflex_loop is not None,
            self._meta_cognition is not None,
            self._feedback_learner is not None,
            self._swarm_session is not None,
        )

    async def stop(self) -> None:
        """Shut down the profile's subsystems and background tasks.

        Cancel the loops first and await them so no cleanup callback runs
        after the flag is cleared, then stop the long-lived subsystems in
        reverse of their start order. Clearing ``_running`` before cancelling
        means a loop that wakes up concurrently observes the flag and exits
        by itself instead of racing the cancel.

        Subsystem handles are cleared so a subsequent :meth:`start` rebuilds
        them instead of reusing a stopped object.

        Raises:
            Exception: Nothing is re-raised; a subsystem that refuses to stop
                is left for process exit to clean up.
        """
        if not self._running:
            return
        self._running = False
        logger.info("Shutting down iClaw profile...")

        for task in (self._cleanup_task, self._heartbeat_task, self._reflex_scan_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if self._consolidator:
            await self._consolidator.stop()
            self._consolidator = None
        if self._learning_engine:
            await self._learning_engine.stop()
            self._learning_engine = None

        logger.info("iClaw profile stopped")

    # -- Submit pipeline ------------------------------------------------------

    async def submit(
        self,
        channel_name: str,
        prompt: str,
        *,
        session_id: str | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """Submit a one-shot prompt to a channel and return the full text.

        Thin delegation to the router: no hooks, compaction, or post-run
        pipeline are applied here. Callers that want the full self-improvement
        treatment should use :meth:`submit_stream` instead.

        Args:
            channel_name: Originating channel; recorded on the session so the
                session list can badge its source.
            prompt: The user prompt.
            session_id: Existing session to resume, or ``None`` for a new one.
            system_prompt: Optional system-prompt override.

        Returns:
            The concatenated assistant text for the turn.
        """
        result = await self._router.submit(
            channel_name, prompt,
            session_id=session_id,
            system_prompt=system_prompt,
        )
        self.stats.sessions_created += 1
        return result

    async def submit_stream(
        self,
        channel_name: str,
        prompt: str,
        *,
        session_id: str | None = None,
        system_prompt: str | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Stream an agent run, wrapped with the self-improvement pipeline.

        Wraps the router stream with four additions: a pre-turn hook, prompt
        compaction for oversized inputs, in-stream observation of tool and
        finish events, and a post-run pipeline invocation after the stream is
        exhausted.

        Everything added is best-effort and swallows its own errors, so the
        caller always sees the agent's real events and a real terminal event.

        Args:
            channel_name: Originating channel.
            prompt: The user prompt.
            session_id: Existing session to resume, or ``None`` for a new one.
            system_prompt: Optional system-prompt override.

        Yields:
            The :class:`encre.utils.types.AgentEvent` values from the agent,
            in order and unmodified.

        Raises:
            Exception: Not caught; an error from the router propagates so the
                caller can report it.
        """
        self.stats.sessions_created += 1

        # -- Hook: pre-turn -------------------------------------------------
        # Fired before any model work so a hook can veto or rewrite the turn.
        # Gated on the profile toggle so enable_hooks=False really suppresses
        # hook emission instead of only being accepted as a dead argument.
        hook_system = getattr(self._agent, "hook_system", None) if self._enable_hooks else None
        if hook_system:
            with contextlib.suppress(Exception):
                await hook_system.emit_turn_start(turn=0, prompt=prompt)

        # -- Compact: check if context needs compaction ---------------------
        # Only very long prompts are compacted; short prompts would pay for a
        # compaction round trip they do not need.
        if self._compact_engine and len(prompt) > 50000:
            try:
                messages = [{"role": "user", "content": prompt}]
                if await self._compact_engine.should_compact(messages, self._compact_max_tokens):
                    compressed = await self._compact_engine.compact(messages, self._compact_max_tokens)
                    if compressed and len(compressed) > 0:
                        prompt = compressed[0].get("content", prompt)
                        self.stats.compact_runs += 1
                        logger.debug("Context compacted: %d chars -> %d chars",
                                     len(prompt), len(compressed[0].get("content", "")))
            except Exception:
                pass

        collected_events: list[AgentEvent] = []
        tool_names: list[str] = []
        stream_start = time.monotonic()
        async for event in self._router.submit_stream(
            channel_name, prompt,
            session_id=session_id,
            system_prompt=system_prompt,
        ):
            if isinstance(event, ToolResult):
                tool_names.append(getattr(event, "id", "unknown"))
                if self._evolution_learner:
                    self.stats.evolution_observations += 1
            elif isinstance(event, Finish):
                self.stats.sessions_completed += 1

            collected_events.append(event)
            yield event

        stream_duration = time.monotonic() - stream_start

        # -- Post-run self-improvement pipeline -----------------------------
        # Runs after the terminal event so the caller has already received the
        # answer; failures here must not surface as an error to the user.
        if self._post_run is not None:
            try:
                pr_result = await self._post_run.collect_and_process(
                    prompt, collected_events,
                    duration_seconds=stream_duration,
                )
                if pr_result.get("completed"):
                    self.stats.skills_generated = pr_result.get("stages", {}).get("learning", {}).get(
                        "skills_generated", self.stats.skills_generated
                    )
            except Exception:
                pass

        # -- Hook: post-turn ------------------------------------------------
        if hook_system:
            with contextlib.suppress(Exception):
                await hook_system.emit_turn_end(turn=0, event_count=len(tool_names))

    # -- Goal execution -------------------------------------------------------

    async def run_goal(
        self,
        description: str,
        success_criteria: str,
        *,
        max_attempts: int = 20,
        timeout_seconds: int = 3600,
        on_progress: Callable[[GoalEvent], None] | None = None,
    ) -> GoalResult:
        """Run a goal-driven autonomous session via the goal runner.

        Degrades to a failed :class:`GoalResult` rather than raising when the
        goal runner was never built, so a caller can always treat the return
        value as the outcome.

        Args:
            description: What the agent should accomplish.
            success_criteria: How the runner judges completion.
            max_attempts: Upper bound on execution attempts.
            timeout_seconds: Wall-clock budget for the whole goal.
            on_progress: Optional callback receiving each
                :class:`encre.goal.GoalEvent`.

        Returns:
            The :class:`encre.goal.GoalResult` describing the outcome.
        """
        if self._goal_runner is None:
            return GoalResult(
                status=GoalStatus.FAILED,
                summary="Goal runner not available",
            )
        goal = GoalDefinition(
            description=description,
            success_criteria=success_criteria,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
        )
        return await self._goal_runner.run(goal, on_attempt=on_progress)

    # -- Swarm / multi-agent operations ---------------------------------------

    async def run_swarm(
        self,
        goal: str,
        *,
        max_concurrent: int = 5,
        _enable_reviewer: bool = True,
        _timeout_seconds: float = 3600.0,
        on_event: Callable[[Any], None] | None = None,
    ) -> Any:
        """Execute a goal across the swarm, returning its result dict.

        Args:
            goal: The goal description handed to the planner.
            max_concurrent: Upper bound on parallel worker agents.
            _enable_reviewer: Reserved for a reviewer stage.
            _timeout_seconds: Reserved for a per-swarm timeout.
            on_event: Optional callback for swarm progress events.

        Returns:
            The swarm result dict, or an ``{"error": ...}`` dict when the
            swarm session is unavailable.
        """
        if self._swarm_session is None:
            return {"error": "Swarm session not available"}
        result = await self._swarm_session.execute(
            goal=goal,
            max_concurrent=max_concurrent,
            on_event=on_event,
        )
        self.stats.swarm_tasks_executed += 1
        return result

    async def run_swarm_stream(
        self,
        goal: str,
    ) -> AsyncGenerator[Any, None]:
        """Stream a swarm goal execution, yielding swarm events.

        Yields nothing at all when the swarm session is unavailable, so a
        caller can iterate unconditionally.

        Args:
            goal: The goal description handed to the planner.

        Yields:
            Swarm progress events.
        """
        if self._swarm_session is None:
            return
        async for event in self._swarm_session.execute_streaming(goal=goal):
            yield event
            self.stats.swarm_tasks_executed += 1

    def blackboard_put(self, namespace: str, key: str, value: Any, owner: str = "") -> int | None:
        """Write a value to the swarm blackboard; return its slot id.

        Args:
            namespace: Logical partition for the entry.
            key: Entry key inside the namespace.
            value: The value to store.
            owner: Optional owner tag for conflict handling.

        Returns:
            The blackboard slot id, or ``None`` when no blackboard exists.
        """
        if self._blackboard is None:
            return None
        return self._blackboard.put(namespace, key, value, owner)

    def blackboard_get(self, namespace: str, key: str) -> Any:
        """Read the first value stored for a blackboard key, or ``None``.

        The blackboard stores every write per key, so only the first entry is
        returned: callers treat the first writer as authoritative.

        Args:
            namespace: Logical partition to read from.
            key: Entry key to read.

        Returns:
            The first value for the key, or ``None`` when absent or when no
            blackboard exists.
        """
        if self._blackboard is None:
            return None
        result = self._blackboard.get(namespace, key)
        if result:
            return result[0]
        return None

    # -- Scheduled job operations ---------------------------------------------

    def schedule_job(
        self,
        name: str,
        prompt: str,
        cron: str = "",
        fire_at: float | None = None,
        max_failures: int = 3,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Schedule a recurring/one-shot job via the server scheduler.

        Args:
            name: Job name, used as its identifier.
            prompt: The prompt the job submits when it fires.
            cron: Cron expression for recurring jobs; empty for one-shots.
            fire_at: Epoch seconds for a one-shot fire time.
            max_failures: Failures tolerated before the job is disabled.
            metadata: Arbitrary per-job metadata.

        Returns:
            The scheduled job id.
        """
        job_id = self._scheduler.schedule(
            name=name,
            prompt=prompt,
            cron=cron,
            fire_at=fire_at,
            max_failures=max_failures,
            metadata=metadata,
        )
        self.stats.jobs_executed += 1
        return job_id

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a scheduled job by id.

        Args:
            job_id: The job id returned by :meth:`schedule_job`.

        Returns:
            ``True`` when a job with that id was removed.
        """
        return self._scheduler.cancel(job_id)

    def list_jobs(self) -> list[ScheduledJob]:
        """Return all currently scheduled jobs.

        Returns:
            The list of :class:`encre.scheduler.ScheduledJob` entries.
        """
        return self._scheduler.list_jobs()

    # -- Stats / Health -------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return a snapshot of running counters and uptime stats.

        Builds a fresh dict each call, so it is safe to pass to a WebSocket
        frame without holding a reference into the live counters.

        Returns:
            A dict of counter values plus computed ``uptime_seconds``,
            ``uptime_human``, and the router's ``active_sessions``.
        """
        now = time.time()
        uptime = now - self.stats.started_at if self.stats.started_at > 0 else 0
        return {
            "uptime_seconds": uptime,
            "uptime_human": self._format_duration(uptime),
            "sessions_created": self.stats.sessions_created,
            "sessions_completed": self.stats.sessions_completed,
            "jobs_executed": self.stats.jobs_executed,
            "skills_generated": self.stats.skills_generated,
            "consolidation_runs": self.stats.consolidation_runs,
            "swarm_tasks_executed": self.stats.swarm_tasks_executed,
            "compact_runs": self.stats.compact_runs,
            "evolution_observations": self.stats.evolution_observations,
            "reflex_evaluations": self.stats.reflex_evaluations,
            "feedback_records": self.stats.feedback_records,
            "errors": self.stats.errors,
            "active_sessions": self._router.session_manager.active_count,
        }

    def get_health(self) -> dict[str, Any]:
        """Return a lightweight health/status summary of the profile.

        Cheap enough to call from a health endpoint on every poll: it touches
        only in-memory flags and counter attributes.

        Returns:
            A dict with ``status``, ``running``, ``uptime_seconds``,
            ``active_sessions``, ``scheduled_jobs``, and a ``subsystems`` map
            of boolean availability flags.
        """
        return {
            "status": "ok" if self._running else "stopped",
            "running": self._running,
            "uptime_seconds": time.time() - self.stats.started_at if self.stats.started_at > 0 else 0,
            "active_sessions": self._router.session_manager.active_count,
            "scheduled_jobs": len(self.list_jobs()),
            "subsystems": {
                "evolution": self._evolution_learner is not None,
                "reflex": self._reflex_loop is not None,
                "metacognition": self._meta_cognition is not None,
                "feedback": self._feedback_learner is not None,
                "swarm": self._swarm_session is not None,
                "scheduler": self._scheduler is not None,
                "learning": self._learning_engine is not None,
                "consolidator": self._consolidator is not None,
                "goals": self._goal_runner is not None,
            },
        }

    # -- Internal loops -------------------------------------------------------

    async def _reflex_scan_loop(self) -> None:
        """Background loop that periodically marks reflex evaluation runs.

        Sleeps first so a freshly started profile does not burn a reflex cycle
        before any session has happened, and re-checks ``_running`` after the
        sleep so :meth:`stop` can interrupt it immediately.
        """
        while self._running and self._reflex_loop:
            await asyncio.sleep(900)
            if not self._running:
                break
            if self._evolution_learner:
                self.stats.reflex_evaluations += 1

    async def _cleanup_loop(self, session_manager=None) -> None:
        """Background loop that periodically evicts idle sessions.

        Re-checks ``_running`` after every sleep so shutdown is responsive,
        and counts an evict failure as an error rather than dying: a single
        bad session must not kill the whole daemon.

        Args:
            session_manager: Optional manager override, mainly for tests;
                defaults to the router's session manager.
        """
        mgr = session_manager or self._router.session_manager
        while self._running:
            await asyncio.sleep(600)
            if not self._running:
                break
            try:
                removed = await mgr.cleanup_idle()
                if removed > 0:
                    logger.info("Cleaned up %d idle session(s)", removed)
            except Exception:
                self.stats.errors += 1

    async def _heartbeat_loop(self) -> None:
        """Background loop that logs periodic daemon heartbeat stats.

        Debug-level only, so a production deploy stays quiet while an operator
        who needs liveness detail can turn logging down.
        """
        while self._running:
            await asyncio.sleep(300)
            if not self._running:
                break
            logger.debug(
                "iClaw heartbeat -- sessions=%d jobs=%d evolution=%d",
                self._router.session_manager.active_count,
                len(self.list_jobs()),
                self.stats.evolution_observations,
            )

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format a duration in seconds as a compact ``Nh Mm Ss`` string.

        Zero-valued high-order units are omitted so short durations render as
        ``42s`` rather than ``0h 0m 42s``.

        Args:
            seconds: Duration to format.

        Returns:
            The formatted string.
        """
        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        parts.append(f"{secs}s")
        return " ".join(parts)

    @staticmethod
    def _extract_json_object(text: str) -> str | None:
        """Return the first brace-balanced ``{...}`` slice of ``text``.

        Scans from the first ``{`` and tracks brace depth while skipping over
        string literals and their escapes, so a brace that appears inside a
        string value cannot unbalance the count, and a second object appended
        after the first is never folded into the slice the way a
        first-open/last-close pair would. This is what lets a model reply that
        wraps its JSON in prose -- or emits two objects -- still yield the
        intended payload.

        Args:
            text: The raw model reply, which may contain prose, code fences,
                and one or more JSON-looking objects.

        Returns:
            The substring from the first ``{`` to its matching ``}``
            inclusive, or ``None`` when no balanced object exists.
        """
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                # Inside a string literal only the closing quote (or an
                # escape) is significant; braces are ordinary characters.
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        return None

    # -- LLM-powered run analysis ---------------------------------------------

    def _make_analyze_fn(self) -> Callable[[RunSummary], Awaitable[dict[str, Any]]]:
        """Build the LLM-powered run-analysis callable for the post-run pipeline.

        The returned coroutine asks the agent's own backend to read a finished
        run and return structured learnings. Every failure mode -- no tool
        calls at all, no backend attached, a model reply without JSON, or a
        malformed payload -- degrades to an empty dict, because analysis is a
        nice-to-have and must never fail the run it reflects on.

        Returns:
            The analyze callable passed into :class:`PostRunPipeline`.
        """
        agent = self._agent

        async def _analyze(summary: RunSummary) -> dict[str, Any]:
            """Summarise one finished run by asking the agent's own backend.

            Runs a single LLM turn over the truncated run digest and parses the
            reply into the four learning fields the post-run pipeline consumes.
            Every degenerate input -- no tool calls, no backend attached, a
            reply with no JSON object, or a parse failure -- yields ``{}``, so a
            bad analysis can never be reported as a failed run.

            Args:
                summary: The :class:`RunSummary` digest of the completed run;
                    only its prompt, tool set, repeated patterns, and text
                    preview reach the model, each truncated to bound prompt
                    size.

            Returns:
                A dict with                 ``task_summary``, ``user_preferences``,
                ``skill_candidates``, and ``key_insights``, or an empty dict when
                nothing could be extracted.

            Note:
                The payload is recovered by scanning for the first
                brace-balanced object rather than decoding the whole reply,
                because models routinely wrap JSON in prose or code fences.
                Braces inside string values and any trailing second object are
                handled by :meth:`_extract_json_object`, so a naive
                first-open/last-close slice can no longer truncate the payload.
            """
            if not summary.tool_calls:
                return {}
            prompt_for_llm = (
                f"Analyze this agent session and extract key learnings:\n\n"
                f"User goal: {summary.prompt[:500]}\n\n"
                f"Tools used ({summary.tool_call_count}): "
                f"{', '.join(sorted(summary.unique_tools))}\n\n"
                f"Repeated patterns: {summary.repeated_patterns[:5] if summary.repeated_patterns else 'none'}\n\n"
                f"Response preview: {summary.text_output[:300]}\n\n"
                f"Extract the following as JSON:\n"
                f'{{"task_summary": "one-sentence summary of what was accomplished",\n'
                f' "user_preferences": ["any user preferences or patterns observed as strings, or empty list"],\n'
                f' "skill_candidates": ["reusable workflows worth saving as skills, or empty list"],\n'
                f' "key_insights": ["any important learnings, or empty list"]}}'
            )
            try:
                backend = agent.loop.backend
                if backend is None:
                    return {}
                result = await backend.chat(
                    messages=[{"role": "user", "content": prompt_for_llm}]
                )
                text = ""
                async for part in result:
                    if hasattr(part, "text") and part.text:
                        text += part.text
                # Models frequently wrap the JSON in prose or code fences, so
                # the first balanced object is sliced out rather than parsing
                # the whole reply.
                snippet = self._extract_json_object(text)
                if snippet is not None:
                    parsed = json.loads(snippet)
                    return {
                        "task_summary": parsed.get("task_summary", ""),
                        "user_preferences": parsed.get("user_preferences", []),
                        "skill_candidates": parsed.get("skill_candidates", []),
                        "key_insights": parsed.get("key_insights", []),
                    }
            except Exception:
                pass
            return {}

        return _analyze


def _make_agent_factory(main_agent: EncreAgent) -> Callable[[dict[str, Any] | None], EncreAgent]:
    """Build a factory that spawns agents sharing the main agent's registry.

    Shared registries (tools/hooks/memory/safety/plugins) come from the
    main agent; per-job model overrides are applied from ``agent_config``.

    Sharing rather than duplicating the registries is what keeps spawned
    workers consistent with the parent: a skill or tool the parent learned is
    immediately visible to any worker created afterwards.

    Args:
        main_agent: The parent agent whose registries are reused.

    Returns:
        A factory taking an optional per-agent config override dict.
    """
    def _factory(agent_config: dict[str, Any] | None = None) -> EncreAgent:
        """Materialise one worker agent from the parent's registries.

        Starts from a dataclasses copy of the parent config, applies only the
        whitelisted overrides, and reuses the parent's registry objects by
        reference. Sharing by reference -- rather than cloning -- is what keeps
        a worker created after the parent learns a new skill or tool already
        able to see it.

        Args:
            agent_config: Optional override dict. Only ``backend_type``,
                ``api_key``, ``base_url``, ``model_id``, ``max_tokens``,
                ``workspace``, and ``permission_mode`` are honoured; any other
                key is ignored so a caller cannot wholesale replace the config
                object.

        Returns:
            A new :class:`encre.agent.EncreAgent` bound to the parent's tool,
            hook, memory, safety, and plugin registries.

        Note:
            ``model_id`` updates both ``config.model`` and
            ``config.target_model_ids`` so routing and selection agree.
        """
        config = replace(main_agent.config)
        if agent_config:
            # Only whitelisted overrides are applied so a caller cannot
            # accidentally replace the whole config object.
            if agent_config.get("backend_type"):
                config.backend_type = agent_config["backend_type"]
            if agent_config.get("api_key"):
                config.api_key = agent_config["api_key"]
            if agent_config.get("base_url"):
                config.base_url = agent_config["base_url"]
            if agent_config.get("model_id"):
                config.model = agent_config["model_id"]
                config.target_model_ids = [agent_config["model_id"]]
            if agent_config.get("max_tokens"):
                config.max_tokens = agent_config["max_tokens"]
            if agent_config.get("workspace"):
                config.workspace = agent_config["workspace"]
            # Same permission preset the safety engine dispatches on for the
            # regular modes; absent key keeps the parent's permission mode.
            if agent_config.get("permission_mode"):
                config.permission_mode = agent_config["permission_mode"]
        return EncreAgent(
            config=config,
            tool_registry=main_agent.tool_registry,
            hook_system=main_agent.hook_system,
            memory_system=main_agent.memory_system,
            safety=main_agent.safety,
            plugin_registry=main_agent.plugin_registry,
        )
    return _factory
