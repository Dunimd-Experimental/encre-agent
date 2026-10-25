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

"""DAG workflow executor -- the single TaskTree execution engine.

Runs :class:`TaskTree` nodes (from ``encre.swarm.planner``) in dependency
order.  Independent nodes execute concurrently via :func:`asyncio.gather`
(optionally bounded by a concurrency semaphore) and failures propagate
downstream: any node whose dependency failed or was skipped is itself
marked ``skipped`` so every node of the tree shows up in the results.

Optional hooks integrate the multi-agent collaboration channel:

- ``blackboard``: a shared :class:`~encre.swarm.blackboard.EncreBlackboard`;
  the executor publishes the goal and each completed node's result, and
  exposes the instance to node runners via ``context["blackboard"]``.
- ``context["mailboxes"]``: a per-node :class:`~encre.swarm.mailbox.EncreMailbox`
  registry created for every run, so sub-agent runners can send/receive
  point-to-point messages between nodes.
- ``reviewer_gate``: an async-or-sync ``(node, result) -> bool`` callback;
  returning ``False`` fails the node with "Reviewer rejected the output".
- ``progress_cb``: sync-or-async ``(node_id, NodeResult)`` notifications.

Node runners may be plain callables (executed in a worker thread) or async
callables (awaited on the loop).
"""

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from encre.swarm.mailbox import EncreMailbox
from encre.swarm.planner import TaskNode, TaskTree

logger = logging.getLogger("encre.dag.executor")

# Type alias: a node runner receives (node, context) and returns a result
# string, either synchronously or as an awaitable.  ``context`` is a mutable
# dict shared across all nodes in the workflow; the executor injects
# ``context["blackboard"]`` (when a blackboard is configured) and
# ``context["mailboxes"]`` (per-node mailboxes) so runners can coordinate.
NodeRunner = Callable[[TaskNode, dict[str, Any]], Any]

# Type alias: a reviewer gate receives (node, result) and returns -- directly
# or as an awaitable -- whether the output is approved.
ReviewerGate = Callable[..., Any]


class SkipNodeError(Exception):
    """Raise inside a node runner to skip this node (no error logged)."""


@dataclass
class NodeResult:
    """Outcome of a single DAG node execution."""

    node_id: str
    node_name: str
    status: str  # completed | failed | skipped
    result: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    attempts: int = 1


@dataclass
class DagExecutionResult:
    """Aggregated result of a full DAG workflow execution."""

    goal: str
    success: bool
    node_results: dict[str, NodeResult] = field(default_factory=dict)
    total_duration: float = 0.0
    error: str = ""


class DagExecutor:
    """Async DAG workflow executor.

    Usage::

        executor = DagExecutor(runner=my_async_node_runner)
        result = await executor.run(task_tree, context={})
    """

    def __init__(
        self,
        runner: NodeRunner,
        max_retries: int = 2,
        retry_delay: float = 1.0,
        fail_fast: bool = False,
        max_concurrent: int = 0,
        blackboard: Any = None,
        reviewer_gate: ReviewerGate | None = None,
    ) -> None:
        self._runner = runner
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._fail_fast = fail_fast
        self._semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent > 0 else None
        self._blackboard = blackboard
        self._reviewer_gate = reviewer_gate
        self._node_results: dict[str, NodeResult] = {}
        self._context: dict[str, Any] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._completed: set[str] = set()
        self._failed: set[str] = set()
        self._skipped: set[str] = set()
        self._tree: TaskTree | None = None
        self._cancelled = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Request cooperative cancellation.

        Stops the scheduling loop before the next batch of ready nodes;
        nodes already in flight finish their current attempt.  Unresolved
        nodes are then reported as ``skipped`` in the final result.
        """
        self._cancelled = True

    async def run(
        self,
        tree: TaskTree,
        context: dict[str, Any] | None = None,
        progress_cb: Callable[[str, NodeResult], Any] | None = None,
    ) -> DagExecutionResult:
        """Execute a ``TaskTree`` from root to leaves.

        Args:
            tree: The DAG to execute.
            context: Shared mutable dict passed to every node runner.
            progress_cb: Optional sync-or-async callback fired after each
                node reaches a terminal state (receives node_id, NodeResult).

        Returns:
            Aggregated ``DagExecutionResult``.
        """
        self._tree = tree
        self._context = context or {}
        self._node_results.clear()
        self._completed.clear()
        self._failed.clear()
        self._skipped.clear()
        self._cancelled = False

        if not tree.nodes:
            return self._build_result(success=True)

        # Inter-agent collaboration channel: publish the goal on the shared
        # blackboard, expose it to runners, and create one point-to-point
        # mailbox per node so sub-agents can coordinate.
        if self._blackboard is not None:
            self._blackboard.put("__orchestrator__", "goal", tree.goal)
            self._context["blackboard"] = self._blackboard
        self._context["mailboxes"] = {
            nid: EncreMailbox(owner_id=nid) for nid in tree.nodes
        }

        # Validate DAG -- check for missing dependencies and cycles
        validation_error = self._validate(tree)
        if validation_error:
            return self._build_result(success=False, error=validation_error)

        started = time.monotonic()

        # Seed the queue with entry nodes
        enqueued: set[str] = set()
        for nid in tree.entry_nodes:
            if nid in tree.nodes:
                self._queue.put_nowait(nid)
                enqueued.add(nid)

        in_flight: set[str] = set()

        try:
            while not self._cancelled:
                # Dequeue as many ready nodes as possible
                ready_nodes: list[str] = []
                while not self._queue.empty():
                    try:
                        nid = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if (nid not in enqueued or nid in self._completed
                            or nid in self._failed or nid in self._skipped):
                        continue
                    ready_nodes.append(nid)

                if ready_nodes:
                    # Execute ready nodes concurrently (bounded by the
                    # optional concurrency semaphore).
                    tasks = {nid: self._run_node(nid, tree.nodes[nid]) for nid in ready_nodes}
                    in_flight.update(ready_nodes)
                    enqueued.update(ready_nodes)

                    completed = await asyncio.gather(*tasks.values(), return_exceptions=True)

                    for nid, outcome in zip(tasks.keys(), completed, strict=False):
                        in_flight.discard(nid)
                        if not isinstance(outcome, NodeResult):
                            outcome = NodeResult(
                                node_id=nid,
                                node_name=tree.nodes[nid].name,
                                status="failed",
                                error=str(outcome),
                            )
                        self._node_results[nid] = outcome
                        if outcome.status == "completed":
                            self._completed.add(nid)
                        elif outcome.status == "failed":
                            self._failed.add(nid)
                        elif outcome.status == "skipped":
                            self._skipped.add(nid)
                        await self._notify(progress_cb, nid, outcome)

                    if self._fail_fast and self._failed:
                        break

                # Schedule to a fixpoint: enqueue newly-ready nodes and
                # transitively mark nodes with failed/skipped dependencies
                # as skipped so they appear in the results.
                while await self._schedule_pass(tree, enqueued, progress_cb):
                    pass

                if self._queue.empty() and not in_flight:
                    break
        except asyncio.CancelledError:
            # The run() coroutine itself was cancelled (e.g. by a timeout):
            # mark in-flight nodes so none is left dangling in "running".
            for nid in in_flight:
                node = tree.nodes.get(nid)
                if node is not None and node.status == "running":
                    node.status = "cancelled"
                    node.error = "Cancelled by executor"
            raise

        # Finalization: after a cooperative cancel() or a fail_fast abort,
        # mark every node that never ran as skipped so the result set always
        # covers the full tree.
        if self._cancelled or (self._fail_fast and self._failed):
            note = "Cancelled by executor" if self._cancelled else "Aborted by fail_fast"
            for nid, node in tree.nodes.items():
                if nid in self._node_results:
                    continue
                node.status = "skipped"
                node.error = note
                self._node_results[nid] = NodeResult(
                    node_id=nid,
                    node_name=node.name,
                    status="skipped",
                    error=note,
                )
                self._skipped.add(nid)

        total_duration = time.monotonic() - started
        success = len(self._failed) == 0 and len(self._node_results) == len(tree.nodes)
        return self._build_result(
            success=success,
            duration=total_duration,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _notify(self, cb: Callable[..., Any] | None, *args: Any) -> None:
        """Invoke a sync-or-async callback, swallowing its failures.

        Progress callbacks must never take down the execution engine.
        """
        if cb is None:
            return
        try:
            outcome = cb(*args)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            logger.warning("[dag] progress callback failed", exc_info=True)

    async def _run_node(self, nid: str, node: TaskNode) -> NodeResult:
        """Run one node, bounded by the concurrency semaphore when set."""
        if self._semaphore is None:
            return await self._execute_node(nid, node)
        async with self._semaphore:
            return await self._execute_node(nid, node)

    async def _execute_node(self, nid: str, node: TaskNode) -> NodeResult:
        """Run a single node with retry logic, the reviewer gate, and a
        blackboard write on success."""
        node.status = "running"
        started = time.monotonic()
        last_error = ""
        for attempt in range(1, self._max_retries + 2):
            try:
                outcome = self._runner(node, self._context)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                result = outcome or ""
                node.result = result
                node.error = ""
                finished = time.monotonic()

                # Reviewer gate (optional): a rejected output fails the node.
                if self._reviewer_gate is not None:
                    approved = self._reviewer_gate(node, result)
                    if inspect.isawaitable(approved):
                        approved = await approved
                    if not approved:
                        node.status = "failed"
                        node.error = "Reviewer rejected the output"
                        return NodeResult(
                            node_id=nid,
                            node_name=node.name,
                            status="failed",
                            result=result,
                            error="Reviewer rejected the output",
                            started_at=started,
                            finished_at=finished,
                            attempts=attempt,
                        )

                node.status = "completed"
                if self._blackboard is not None:
                    self._blackboard.put(f"task:{nid}", "result", result, owner=nid)
                return NodeResult(
                    node_id=nid,
                    node_name=node.name,
                    status="completed",
                    result=result,
                    started_at=started,
                    finished_at=finished,
                    attempts=attempt,
                )
            except SkipNodeError:
                node.status = "skipped"
                return NodeResult(
                    node_id=nid,
                    node_name=node.name,
                    status="skipped",
                    started_at=started,
                    finished_at=time.monotonic(),
                    attempts=attempt,
                )
            except Exception as e:
                last_error = str(e)
                if attempt <= self._max_retries:
                    logger.info("[dag] retrying node=%s attempt=%d/%d error=%s",
                                nid, attempt, self._max_retries, last_error)
                    await asyncio.sleep(self._retry_delay * (2 ** (attempt - 1)))
                continue

        node.status = "failed"
        node.error = last_error
        return NodeResult(
            node_id=nid,
            node_name=node.name,
            status="failed",
            error=last_error,
            started_at=started,
            finished_at=time.monotonic(),
            attempts=self._max_retries + 1,
        )

    async def _schedule_pass(
        self,
        tree: TaskTree,
        enqueued: set[str],
        progress_cb: Callable[[str, NodeResult], Any] | None,
    ) -> bool:
        """One scheduling pass over unresolved nodes.

        Enqueues nodes whose dependencies are all completed and marks nodes
        with failed or skipped dependencies as ``skipped``.  Returns True
        when any node changed state; the caller loops until this returns
        False so skips propagate transitively.
        """
        changed = False
        for nid, node in tree.nodes.items():
            if (nid in enqueued or nid in self._completed
                    or nid in self._failed or nid in self._skipped):
                continue
            failed_deps = [d for d in node.dependencies if d in self._failed]
            skipped_deps = [d for d in node.dependencies if d in self._skipped]
            if failed_deps or skipped_deps:
                if failed_deps:
                    reason = f"dependency failed: {failed_deps[0]}"
                else:
                    reason = f"dependency skipped: {skipped_deps[0]}"
                node.status = "skipped"
                node.error = reason
                skip_result = NodeResult(
                    node_id=nid,
                    node_name=node.name,
                    status="skipped",
                    error=reason,
                )
                self._node_results[nid] = skip_result
                self._skipped.add(nid)
                enqueued.add(nid)
                await self._notify(progress_cb, nid, skip_result)
                changed = True
                continue
            if self._deps_met(node):
                enqueued.add(nid)
                self._queue.put_nowait(nid)
                changed = True
        return changed

    def _deps_met(self, node: TaskNode) -> bool:
        """Check whether all dependencies of *node* are completed."""
        if not node.dependencies:
            return True
        tree = self._tree
        if tree is None:
            return False
        return all(
            dep in tree.nodes and (
                dep in self._completed or tree.nodes[dep].status == "completed"
            )
            for dep in node.dependencies
        )

    def _validate(self, tree: TaskTree) -> str | None:
        """Validate the DAG: check for missing deps and obvious cycles."""
        all_ids = set(tree.nodes.keys())
        for nid, node in tree.nodes.items():
            for dep in node.dependencies:
                if dep not in all_ids:
                    return f"node {nid!r} depends on unknown node {dep!r}"
        # Quick cycle check via DFS
        visited: set[str] = set()
        stack: set[str] = set()

        def _has_cycle(nid: str) -> bool:
            visited.add(nid)
            stack.add(nid)
            for dep in tree.nodes[nid].dependencies:
                if dep not in visited:
                    if _has_cycle(dep):
                        return True
                elif dep in stack:
                    return True
            stack.discard(nid)
            return False

        for nid in all_ids:
            if nid not in visited and _has_cycle(nid):
                return f"cycle detected involving node {nid!r}"
        return None

    def _build_result(
        self,
        success: bool,
        error: str = "",
        duration: float = 0.0,
    ) -> DagExecutionResult:
        goal = self._tree.goal if self._tree else ""
        return DagExecutionResult(
            goal=goal,
            success=success,
            node_results=dict(self._node_results),
            total_duration=duration,
            error=error,
        )
