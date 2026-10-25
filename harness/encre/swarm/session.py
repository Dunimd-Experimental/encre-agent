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

# High-level orchestration entry point for multi-agent swarms.
#
# ``EncreSwarmSession`` is the user-facing API: given a goal it plans a task
# tree and runs it through the single DAG engine (``encre.dag.executor``),
# with role-configured teammates, a shared blackboard, per-node mailboxes for
# point-to-point messaging, and an optional reviewer gate on coder output.
# Progress and lifecycle events are surfaced through ``SwarmEvent`` callbacks
# / a streaming generator.
import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from encre.logging_config import get_logger
from encre.swarm.blackboard import EncreBlackboard
from encre.swarm.planner import EncreTaskPlanner, TaskNode, TaskTree
from encre.swarm.roles import (
    ROLE_ARCHITECT,
    ROLE_CODER,
    ROLE_DEBUGGER,
    ROLE_REVIEWER,
    ROLE_TESTER,
    RoleRegistry,
)

logger = get_logger("encre.swarm")

# Sub-agent turn budget for each swarm teammate. Kept in sync with the
# workflow tool's NODE_MAX_TURNS so both delegation paths give participants
# the same working room.
NODE_MAX_TURNS = 30


@dataclass
class SwarmEvent:
    """Events emitted during swarm execution."""
    type: str  # planning | task_started | task_completed | task_failed | task_skipped | consensus | team_finished | error
    task_id: str = ""
    task_name: str = ""
    role: str = ""
    result: str = ""
    error: str = ""
    progress: float = 0.0
    consensus: Any = None  # ConsensusResult, set only on "consensus" events
    timestamp: float = field(default_factory=time.time)


@dataclass
class ConsensusResult:
    """Outcome of the proposal-vote consensus step.

    Produced after execution when two or more tasks completed: an
    independent reviewer panel (up to three voters) evaluates every
    completed proposal and each voter picks the strongest one.  A strict
    majority elects a winner; a tie or total abstention yields
    ``no_consensus``.  ``to_dict`` is the frontend serialisation.
    """
    outcome: str  # consensus | no_consensus
    selected_task_id: str = ""
    vote_counts: dict[str, int] = field(default_factory=dict)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "selected_task_id": self.selected_task_id,
            "vote_counts": dict(self.vote_counts),
            "rationale": self.rationale,
        }


@dataclass
class SwarmResult:
    """Final result of a swarm execution."""
    goal: str
    task_tree: TaskTree | None = None
    results: dict[str, str] = field(default_factory=dict)  # task_id -> result or error
    blackboard: dict[str, Any] = field(default_factory=dict)
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    skipped_tasks: int = 0
    elapsed_seconds: float = 0.0
    summary: str = ""
    consensus: ConsensusResult | None = None


class EncreSwarmSession:
    """High-level swarm execution session attached to a EncreAgent.

    Usage:
        agent = EncreAgent(config)
        session = EncreSwarmSession(agent, goal="Build a TODO API")
        result = await session.execute()
    """

    DEFAULT_ROLES: ClassVar[list] = [
        ROLE_ARCHITECT,
        ROLE_CODER,
        ROLE_REVIEWER,
        ROLE_TESTER,
        ROLE_DEBUGGER,
    ]

    def __init__(
        self,
        agent: Any,
        goal: str = "",
        max_concurrent: int = 5,
        enable_reviewer: bool = True,
        timeout_seconds: float = 3600.0,
        sub_agent_runner: Callable | None = None,
    ) -> None:
        self._agent = agent
        self._goal = goal
        self._max_concurrent = max_concurrent
        self._enable_reviewer = enable_reviewer
        self._timeout = timeout_seconds
        self._sub_agent_runner = sub_agent_runner

    async def execute(
        self,
        goal: str = "",
        max_concurrent: int = 0,
        on_event: Callable[[SwarmEvent], None] | None = None,
        node_progress_factory: Callable[[TaskNode], Any] | None = None,
    ) -> SwarmResult:
        """Run the full swarm workflow for *goal*.

        Two phases: (1) plan the goal into a ``TaskTree``; (2) execute the
        tree through the shared DAG engine, feeding ``SwarmEvent``s to
        *on_event* and accumulating per-task results.  Returns a consolidated
        ``SwarmResult`` with counts, the blackboard snapshot, and a summary.
        Raises ``ValueError`` when no goal is supplied.

        ``node_progress_factory`` optionally provides a per-node progress
        callback (called with the ``TaskNode`` when the node starts); the
        node's sub-agent live transcript streams through it, so hosts can
        render each teammate's activity in real time.
        """
        goal = goal or self._goal
        max_concurrent = max_concurrent or self._max_concurrent

        if not goal:
            raise ValueError("goal is required for swarm execution")

        start_time = time.time()
        result = SwarmResult(goal=goal)

        # Phase 1: Planning -- decompose goal into task tree
        if on_event:
            on_event(SwarmEvent(type="planning", progress=0.0))

        planner = EncreTaskPlanner()
        # Model-driven planning is the primary path; the agent's backend is
        # the real model behind this swarm.  Without it the planner falls
        # back to its explicit no-backend skeleton -- never a fake plan.
        task_tree = await planner.decompose(
            goal, backend=getattr(self._agent, "backend", None),
        )
        result.task_tree = task_tree

        # Phase 2: Execution through the single DAG engine.  Each node runs
        # as a role-configured teammate; dependency outputs and the shared
        # blackboard are injected into every node's prompt, and each node
        # gets a point-to-point mailbox for agent-to-agent messages.
        blackboard = EncreBlackboard()
        role_registry = RoleRegistry()
        for role in self.DEFAULT_ROLES:
            role_registry.register(role)

        completed = 0
        failed = 0
        skipped = 0
        total = max(1, len(task_tree.nodes))
        finished = 0

        # Imported lazily: ``encre.dag.executor`` imports ``encre.swarm``
        # submodules at module level, so a top-level import here would form
        # a dag <-> swarm circular import at package init.
        from encre.dag.executor import DagExecutor, NodeResult

        def _make_event(node_result: NodeResult) -> SwarmEvent:
            nonlocal finished
            finished += 1
            node = task_tree.nodes.get(node_result.node_id)
            ev_type = {
                "completed": "task_completed",
                "failed": "task_failed",
            }.get(node_result.status, "task_skipped")
            return SwarmEvent(
                type=ev_type,
                task_id=node_result.node_id,
                task_name=node_result.node_name,
                role=node.assigned_role if node is not None else "",
                result=node_result.result,
                error=node_result.error,
                progress=finished / total,
            )

        def _on_progress(nid: str, node_result: NodeResult) -> None:
            nonlocal completed, failed, skipped
            if on_event:
                on_event(_make_event(node_result))
            if node_result.status == "completed":
                completed += 1
                result.results[nid] = node_result.result
            elif node_result.status == "failed":
                failed += 1
                result.results[nid] = node_result.error
            else:
                skipped += 1

        async def _run_node(node: TaskNode, _context: dict[str, Any]) -> str:
            # Per-node collaboration context is rebuilt locally via
            # _build_context(); the executor-provided bag is unused here.
            role = role_registry.get(node.assigned_role)
            if on_event:
                # Notify the frontend that this node started executing so the
                # UI can flip its card to "running" before the first token.
                on_event(SwarmEvent(
                    type="task_started",
                    task_id=node.id,
                    task_name=node.name,
                    role=node.assigned_role,
                ))
            node_cb = node_progress_factory(node) if node_progress_factory else None
            context_text = self._build_context(node, task_tree, blackboard)
            return await self._run_agent(node, role, context_text, progress_callback=node_cb)

        async def _reviewer_gate(node: TaskNode, node_result: str) -> bool:
            # Reviewer gate: only coder output is reviewed; everything else
            # passes through untouched.
            if role_registry.get(node.assigned_role).name != "coder":
                return True
            return await self._reviewer_check(node, node_result, role_registry)

        executor = DagExecutor(
            runner=_run_node,
            max_retries=0,
            retry_delay=0.0,
            max_concurrent=max_concurrent,
            blackboard=blackboard,
            reviewer_gate=_reviewer_gate if self._enable_reviewer else None,
        )

        try:
            await asyncio.wait_for(
                executor.run(task_tree, progress_cb=_on_progress),
                timeout=self._timeout,
            )
        except TimeoutError:
            executor.cancel()
            result.summary = f"Swarm timed out after {self._timeout}s"
        except Exception as e:
            executor.cancel()
            result.summary = f"Swarm error: {e}"
            if on_event:
                on_event(SwarmEvent(type="error", error=str(e)))

        # Proposal-vote consensus: when two or more tasks completed cleanly,
        # an independent reviewer panel votes on the strongest proposal.
        # Skipped entirely when the run timed out or errored (result.summary
        # was already set by those branches) -- no extra model calls after an
        # aborted execution.
        if completed >= 2 and not result.summary and task_tree is not None:
            proposals = [
                (nid, node.name, node.result or "")
                for nid, node in task_tree.nodes.items()
                if node.status == "completed" and (node.result or "").strip()
            ]
            if len(proposals) >= 2:
                try:
                    consensus = await asyncio.wait_for(
                        self._run_consensus(proposals, role_registry),
                        timeout=420.0,
                    )
                except TimeoutError:
                    logger.warning("[swarm] consensus step timed out; skipping")
                    consensus = None
                except Exception:
                    logger.warning("[swarm] consensus step failed", exc_info=True)
                    consensus = None
                if consensus is not None:
                    result.consensus = consensus
                    if on_event:
                        on_event(SwarmEvent(
                            type="consensus",
                            result=consensus.outcome,
                            progress=min(1.0, completed / total),
                            consensus=consensus,
                        ))

        # Collect blackboard state
        result.blackboard = blackboard.get_all("__orchestrator__") or {}

        result.total_tasks = len(task_tree.nodes) if task_tree else 0
        result.completed_tasks = completed
        result.failed_tasks = failed
        result.skipped_tasks = skipped
        result.elapsed_seconds = time.time() - start_time

        if not result.summary:
            result.summary = (
                f"Swarm completed: {completed}/{result.total_tasks} tasks succeeded, "
                f"{failed} failed in {result.elapsed_seconds:.1f}s"
            )

        if on_event:
            on_event(SwarmEvent(
                type="team_finished",
                progress=1.0,
                result=result.summary,
            ))

        return result

    async def execute_streaming(
        self,
        goal: str = "",
    ) -> AsyncGenerator[SwarmEvent]:
        """Execute swarm and yield events as they happen.

        Events are forwarded through an in-memory queue from the ``on_event``
        callback while the run is still in flight, so consumers observe
        ``planning``/``task_started``/... live rather than after completion.
        Raises whatever :meth:`execute` raised after the stream ends.
        """
        queue: asyncio.Queue[SwarmEvent | None] = asyncio.Queue()

        def _on_event(event: SwarmEvent) -> None:
            queue.put_nowait(event)

        async def _run() -> SwarmResult:
            try:
                return await self.execute(goal=goal, on_event=_on_event)
            finally:
                queue.put_nowait(None)  # stream terminator

        run_task = asyncio.create_task(_run())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            # Propagate execution failures (e.g. missing goal) to the caller.
            await run_task

    # ------------------------------------------------------------------
    # Consensus
    # ------------------------------------------------------------------

    async def _run_consensus(
        self,
        proposals: list[tuple[str, str, str]],
        role_registry: RoleRegistry,
    ) -> ConsensusResult:
        """Run the proposal-vote consensus over completed task outputs.

        ``proposals`` is a list of ``(task_id, task_name, result_text)``.
        Up to three independent reviewer-panel voters each read every
        proposal and reply with the id of the strongest one; a strict
        majority elects the winner, a tie or total abstention yields
        ``no_consensus``.  Every voter call is a real sub-agent run through
        the host loop (or a standalone agent when unattached).
        """
        reviewer_role = role_registry.get("reviewer")
        valid_ids = [pid for pid, _, _ in proposals]
        listing = "\n\n".join(
            f"### Proposal [{pid}] (task: {name})\n{text[:4000]}"
            for pid, name, text in proposals
        )

        async def _vote(voter_idx: int) -> str:
            prompt = (
                f"You are voter #{voter_idx + 1} on a review panel. Several "
                "independent proposals produced outputs for the same goal. "
                "Evaluate them on correctness, completeness, and "
                "actionability, then decide which single proposal is the "
                "strongest overall.\n\n"
                f"{listing}\n\n"
                "Reply with ONLY the id of the winning proposal in square "
                f"brackets, e.g. [{valid_ids[0]}]. No other text."
            )
            try:
                reply = await asyncio.wait_for(
                    self._run_simple_agent(reviewer_role, prompt),
                    timeout=120.0,
                )
            except Exception:
                logger.warning("[swarm] consensus voter #%d failed", voter_idx + 1, exc_info=True)
                return ""
            for pid in valid_ids:
                if pid in reply:
                    return pid
            return ""  # abstain: unparseable reply

        voter_count = min(len(proposals), 3)
        votes = await asyncio.gather(*(_vote(i) for i in range(voter_count)))
        vote_counts: dict[str, int] = {}
        for vid in votes:
            if vid:
                vote_counts[vid] = vote_counts.get(vid, 0) + 1

        if not vote_counts:
            return ConsensusResult(outcome="no_consensus", vote_counts={})

        top_votes = max(vote_counts.values())
        winners = [pid for pid, n in vote_counts.items() if n == top_votes]
        if len(winners) > 1:
            return ConsensusResult(outcome="no_consensus", vote_counts=vote_counts)

        winner = winners[0]
        return ConsensusResult(
            outcome="consensus",
            selected_task_id=winner,
            vote_counts=vote_counts,
            rationale=(
                f"Selected proposal [{winner}] with {top_votes}/{voter_count} "
                "review-panel votes."
            ),
        )

    # ------------------------------------------------------------------
    # Node execution helpers
    # ------------------------------------------------------------------

    async def _run_agent(self, node: TaskNode, role: Any, context: str,
                         progress_callback: Any = None) -> str:
        """Execute a single node's prompt.

        When a ``sub_agent_runner`` is configured (the normal case when the
        swarm is driven from the host loop), the node runs through the loop's
        ``_run_sub_agent`` so it inherits depth fencing (no
        swarm-inside-swarm recursion), live progress streaming to the
        frontend, transcript persistence under ``sub_agents/<id>/``, and the
        safety / tool-policy hooks.  Otherwise a fresh role-configured
        ``EncreAgent`` is spawned directly (the unit-test / standalone path).

        ``progress_callback`` (when given) receives the sub-agent's live
        message snapshots so the host can stream the teammate's activity.

        Returns the concatenated text output.  A ``Finish`` with reason
        ``"error"`` appends a marker so the caller knows the agent aborted.
        """
        from encre.utils.types import Finish, TextDelta

        system_prompt = role.system_prompt_override or ""
        full_prompt = (
            f"{node.name}: {node.description}\n\n{context}\n\n"
            "---\n"
            "You are executing a subtask. Do NOT restate the full plan. "
            "Work ONLY on your assigned task. "
            "Reference files with file:line format. "
            "Report completion status explicitly."
        )

        if self._sub_agent_runner is not None:
            try:
                result = await self._sub_agent_runner(
                    full_prompt,
                    system_prompt=system_prompt,
                    max_turns=NODE_MAX_TURNS,
                    progress_callback=progress_callback,
                )
            except Exception as exc:
                return f"[Error during execution: {exc}]"
            if isinstance(result, dict):
                return result.get("content", "") or ""
            return str(result)

        # Fallback: spawn a fresh role-configured EncreAgent directly.
        from encre.agent import EncreAgent
        from encre.config import EncreConfig

        config = EncreConfig(
            max_turns=NODE_MAX_TURNS,
            permission_mode=role.permission_mode,
        )
        if role.model_override:
            config.model = role.model_override
        if role.backend_type_override:
            config.backend_type = role.backend_type_override
        agent = EncreAgent(config=config)
        parts: list[str] = []
        async for event in agent.run(full_prompt, system_prompt=system_prompt or None):
            if isinstance(event, TextDelta) and event.text:
                parts.append(event.text)
            elif isinstance(event, Finish) and event.reason == "error":
                parts.append("\n[Error during execution]")
        return "".join(parts)

    async def _reviewer_check(
        self,
        coder_node: TaskNode,
        result: str,
        role_registry: RoleRegistry,
    ) -> bool:
        """Run the reviewer role over a coder node's output.

        Approves on timeout so a hung reviewer never blocks the swarm.
        """
        reviewer_role = role_registry.get("reviewer")
        review_prompt = (
            f"Review the output of task '{coder_node.name}'. "
            f"Output:\n```\n{result[:5000]}\n```\n"
            "Does this look correct and production-ready? Reply ONLY with 'APPROVED' or 'REJECTED: <reason>'."
        )
        try:
            reviewer_result = await asyncio.wait_for(
                self._run_simple_agent(reviewer_role, review_prompt),
                timeout=120.0,
            )
            return "APPROVED" in reviewer_result.upper() and "REJECTED" not in reviewer_result.upper()
        except TimeoutError:
            return True  # Timeout: approve by default

    async def _run_simple_agent(self, role: Any, prompt: str) -> str:
        """Run a brief one-shot agent for the reviewer gate."""
        system_prompt = role.system_prompt_override or ""
        if self._sub_agent_runner is not None:
            try:
                result = await self._sub_agent_runner(
                    prompt, system_prompt=system_prompt, max_turns=5,
                )
            except Exception as exc:
                return f"[Error: {exc}]"
            if isinstance(result, dict):
                return result.get("content", "") or ""
            return str(result)
        from encre.agent import EncreAgent
        from encre.config import EncreConfig
        from encre.utils.types import TextDelta

        config = EncreConfig(max_turns=5, permission_mode="auto")
        agent = EncreAgent(config=config)
        parts: list[str] = []
        async for event in agent.run(prompt, system_prompt=system_prompt or None):
            if isinstance(event, TextDelta) and event.text:
                parts.append(event.text)
        return "".join(parts)

    def _build_context(
        self,
        node: TaskNode,
        task_tree: TaskTree,
        blackboard: EncreBlackboard,
    ) -> str:
        """Build the prompt context: goal, dependency outputs, blackboard."""
        parts: list[str] = [f"Goal: {task_tree.goal}\n"]
        for dep_id in node.dependencies:
            dep = task_tree.nodes.get(dep_id)
            if dep and dep.result:
                parts.append(f"Dependency [{dep.name}] output:\n{dep.result[:3000]}")
        blackboard_context = blackboard.get_all_visible()
        if blackboard_context:
            parts.append(f"Shared context:\n{blackboard_context}")
        return "\n".join(parts)
