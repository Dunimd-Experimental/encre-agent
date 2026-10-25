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

"""Workflow tool -- unified multi-agent orchestration.

A single delegation tool with two execution modes:

* ``light`` (default) -- decomposes the goal into a DAG of sub-tasks and
  executes it through the shared DAG engine (``encre.dag.executor.DagExecutor``).
  Independent tasks run in parallel as sub-agent runs, dependency outputs
  are injected into downstream prompts, failed tasks are retried, and
  failed/skipped dependencies transitively skip their dependents.
* ``team`` -- runs the goal through ``EncreSwarmSession``: each task is
  assigned a specialised role (architect / coder / reviewer / tester /
  researcher / debugger), coder output passes a reviewer gate, results
  are shared through a blackboard, and when two or more results are
  produced a proposal-vote consensus step runs.

Both modes execute every task via the host loop's ``_run_sub_agent``, so
all participants inherit the same infrastructure as a normal ``agent``-tool
sub-agent: depth fencing (no workflow-inside-workflow recursion), live
progress streaming to the frontend, transcript persistence under
``sub_agents/<id>/``, and the safety / tool-policy hooks.
"""

import asyncio
import contextlib
import time
import uuid
from typing import Any

from encre.dag.executor import DagExecutor, NodeResult
from encre.logging_config import get_logger
from encre.swarm import EncreSwarmSession
from encre.swarm.planner import EncreTaskPlanner, TaskNode
from encre.tools.base import build_tool

logger = get_logger(__name__)

# Per-call budgets shared by both modes.
# MAX_PARALLEL_NODES caps the number of concurrent sub-agent nodes per
# workflow call to prevent resource exhaustion when a task tree has many
# independent branches.
MAX_PARALLEL_NODES = 4
# NODE_MAX_TURNS caps the number of agent turns per node to prevent any
# single task from consuming the entire workflow budget.
NODE_MAX_TURNS = 30
# DEFAULT_LIGHT_TIMEOUT_SECONDS is the overall wall-clock budget for light
# mode when the calling model omits a timeout. Chosen to accommodate a
# typical multi-step pipeline with 3-5 tasks.
DEFAULT_LIGHT_TIMEOUT_SECONDS = 1800.0
# DEFAULT_TEAM_TIMEOUT_SECONDS is the overall wall-clock budget for team
# mode when the calling model omits a timeout. Team mode is heavier due
# to role assignment, reviewer gates, and consensus, so the budget is
# doubled compared to light mode.
DEFAULT_TEAM_TIMEOUT_SECONDS = 3600.0


def _consensus_to_dict(cr: Any) -> dict[str, Any]:
    """Best-effort serialisation of a ``ConsensusResult`` for the frontend.

    ConsensusResult may be a dataclass, a dict, or a custom object.
    This function tries ``to_dict()`` first, then falls back to
    extracting the ``outcome`` and ``vote_counts`` attributes. If both
    fail, returns an empty dict rather than raising, so a malformed
    consensus object never crashes the frontend rendering.

    Args:
        cr: The consensus result object from the swarm session.

    Returns:
        A dict with at least ``outcome`` and ``vote_counts`` keys, or
        an empty dict if serialisation failed entirely.
    """
    try:
        if hasattr(cr, "to_dict"):
            return cr.to_dict()
        return {
            "outcome": getattr(cr, "outcome", ""),
            "vote_counts": getattr(cr, "vote_counts", {}),
        }
    except Exception:
        return {}


async def _workflow_execute(**kwargs: Any) -> Any:
    """Execute a complex goal as a coordinated multi-agent workflow.

    The execution flow is:

    1. **Planning**: Decomposes the goal into a TaskTree using the shared
       planner (``EncreTaskPlanner``).
    2. **Execution**: Dispatches to either ``_run_light`` (DAG executor)
       or ``_run_team`` (swarm session) based on the ``mode`` parameter.
    3. **Reporting**: Streams progress events and per-node live transcripts
       to the frontend in real time via the ``progress_callback``.

    Args:
        kwargs: Keyword arguments containing:
            - ``goal``: The complete goal to accomplish (required).
            - ``mode``: "light" or "team" (optional, default "light").
            - ``timeout_seconds``: Overall wall-clock budget (optional).
            - ``progress_callback``: Async callback for streaming progress
              events to the frontend.

    Returns:
        A dict with ``content`` (human-readable summary) and ``messages``
        (list of message dicts for the frontend). On failure, returns an
        error message in ``content`` and an empty ``messages`` list.

    Raises:
        No exceptions are raised; all errors are caught and returned as
        error messages in the result dict. This ensures the tool never
        crashes the parent agent loop.
    """
    goal = str(kwargs.get("goal", "")).strip()
    mode = str(kwargs.get("mode", "light") or "light").strip().lower()
    if mode not in ("light", "team"):
        # Invalid mode defaults to "light" rather than erroring, so the
        # tool degrades gracefully when the model passes an unexpected value.
        mode = "light"
    progress_callback = kwargs.get("progress_callback")

    # Optional overall wall-clock budget. The calling model decides it from
    # task complexity; when omitted each mode falls back to its default.
    try:
        timeout_seconds = float(kwargs.get("timeout_seconds") or 0)
    except (TypeError, ValueError):
        timeout_seconds = 0.0

    from encre.tools.runtime import _resolve_loop

    loop = _resolve_loop()
    if loop is None:
        return {"content": "Error: No active agent loop found", "messages": []}
    if not goal:
        return {"content": "Error: goal is required", "messages": []}

    # Depth fence: a sub-agent (depth > 0) must not spawn a workflow, which
    # would itself spawn sub-agents -- the runtime only allows one level of
    # delegation.  This mirrors the ``agent`` tool's hard fence and is also
    # enforced by the pre-tool policy hook in ``tools.runtime``.
    if getattr(loop, "sub_agent_depth", 0) > 0:
        return {
            "content": (
                "Error: workflow orchestration cannot be invoked from a "
                "sub-agent. The runtime only allows one level of delegation. "
                "Complete the assigned task with your own tools and return "
                "the result."
            ),
            "messages": [],
        }

    logger.info("[workflow] mode=%s goal=%s", mode, goal[:200])

    # ------------------------------------------------------------------
    # Shared live-transcript machinery. Each node gets its own slot keyed
    # by task id; on every update we re-emit the merged snapshot with task
    # dividers so the frontend renders one card per node -- the same UX as
    # the parallel path of the ``agent`` tool.
    # ------------------------------------------------------------------
    # node_meta tracks per-task metadata (name, role, status) for rendering
    # task dividers in the frontend.
    node_meta: dict[str, dict[str, str]] = {}
    # latest stores the most recent streamed messages for each task id.
    # When a task's transcript updates, we re-emit the merged snapshot so
    # the frontend can render one card per node with live updates.
    latest: dict[str, list[dict[str, Any]]] = {}

    async def _emit(payload: list[dict[str, Any]]) -> None:
        """Emit a progress payload to the frontend via the callback.

        Wrapped in ``contextlib.suppress`` so a frontend disconnect or
        callback error never crashes the workflow execution.
        """
        if progress_callback is not None:
            with contextlib.suppress(Exception):
                await progress_callback(payload)

    def _merged_messages() -> list[dict[str, Any]]:
        """Flatten every node's transcript with dividers, in start order.

        The frontend expects a single flat list of messages. This function
        constructs that list by iterating over ``node_meta`` in insertion
        order (Python dicts preserve insertion order since 3.7) and prepending
        a task divider for each node. The divider includes the task name,
        role, and status so the frontend can render a card header.

        Returns:
            A flat list of message dicts, each either a task divider or
            a message from a task's transcript.
        """
        combined: list[dict[str, Any]] = []
        for tid, meta in node_meta.items():
            combined.append({
                "role": "assistant",
                "content": meta.get("task_name", tid),
                "mode": "task_divider",
                "task_id": tid,
                "task_name": meta.get("task_name", tid),
                "role": meta.get("role", ""),
                "task_status": meta.get("status", "running"),
                "tool_calls": [],
                "segments": [],
                "created_at": time.time(),
            })
            combined.extend(latest.get(tid, []))
        return combined

    def _node_progress_factory(node: Any) -> Any:
        """Provide the per-node live callback consumed by both modes.

        Each node gets its own callback that updates the shared ``node_meta``
        and ``latest`` dicts, then re-emits the merged snapshot so the
        frontend sees live updates for all tasks simultaneously.

        Args:
            node: The task node being executed. Must have ``id`` and
                ``name`` attributes.

        Returns:
            An async callback function that accepts a list of message dicts
            and updates the shared state.
        """
        node_meta[node.id] = {
            "task_name": node.name,
            "role": getattr(node, "assigned_role", "") or "",
            "status": "running",
        }

        async def _cb(messages: list[dict[str, Any]]) -> None:
            latest[node.id] = list(messages or [])
            await _emit(_merged_messages())

        return _cb

    # ------------------------------------------------------------------
    # Mode A: light -- plain sub-agent pipeline over the task DAG.
    # ------------------------------------------------------------------
    async def _run_light() -> dict[str, Any]:
        """Execute the workflow in light mode using the DAG executor.

        Light mode is simpler and faster than team mode: it treats each
        task as an independent sub-agent run with dependency-aware
        parallelism, but without role assignment, reviewer gates, or
        consensus. Use this mode for straightforward pipelines where
        role specialisation and cross-agent verification are not needed.

        Returns:
            A dict with ``content`` (status summary) and ``messages``
            (merged transcripts from all tasks).
        """
        planner = EncreTaskPlanner()
        tree = planner.plan(goal)

        # Generate a unique workflow ID for tracking and logging.
        workflow_id = f"wf_{uuid.uuid4().hex[:10]}"
        all_messages: list[dict[str, Any]] = []

        # Emit the workflow_started event so the frontend can render a
        # workflow card with the total task count and task IDs.
        await _emit([{
            "role": "workflow",
            "type": "workflow_started",
            "workflow_id": workflow_id,
            "goal": tree.goal,
            "total_tasks": len(tree.nodes),
            "task_ids": list(tree.nodes.keys()),
        }])

        async def _run_task_node(node: TaskNode, context: dict[str, Any]) -> str:
            """Execute a single task node as a sub-agent run.

            This function is called by the DAG executor for each task node.
            It constructs the task prompt, injects dependency outputs,
            and delegates to the host loop's ``_run_sub_agent``.

            The prompt is carefully crafted to tell the sub-agent to:
            - Work ONLY on this task (not restate the plan)
            - Produce the concrete deliverable
            - Report the result with an explicit completion status
            - Reference files with file:line format

            Dependency outputs are prepended to the prompt so downstream
            tasks have access to upstream results. This enables the DAG
            to chain tasks together through their outputs.

            Args:
                node: The task node to execute.
                context: A dict mapping task IDs to their results,
                    shared across all tasks in the DAG.

            Returns:
                The task result as a string.

            Raises:
                RuntimeError: If the task result is prefixed with "FAILED:",
                    indicating the sub-agent reported failure. The DAG executor
                    catches this and retries the task.
            """
            await _emit([{
                "role": "workflow",
                "type": "workflow_task",
                "workflow_id": workflow_id,
                "task_id": node.id,
                "task_name": node.name,
                "status": "started",
            }])
            node_cb = _node_progress_factory(node)

            # Collect dependency outputs to inject into the prompt.
            dep_parts: list[str] = []
            for dep_id in node.dependencies:
                dep = tree.nodes.get(dep_id)
                if dep is not None and dep.result:
                    # Truncate to 3000 chars to avoid overwhelming the prompt.
                    dep_parts.append(f"Dependency [{dep.name}] output:\n{dep.result[:3000]}")
            dep_context = "\n\n".join(dep_parts)

            # Construct the task prompt with the node description and
            # dependency context. The prompt explicitly tells the sub-agent
            # to work ONLY on this task and report completion status.
            prompt = f"## Task: {node.name}\n\n{node.description}\n\n"
            if dep_context:
                prompt += f"{dep_context}\n\n"
            prompt += (
                "You are executing one subtask of a larger workflow. Do NOT "
                "restate the plan. Work ONLY on this task, produce the "
                "concrete deliverable, and report the result with an "
                "explicit completion status. Reference files with "
                "file:line format."
            )

            sub_result = await loop._run_sub_agent(
                prompt=prompt,
                max_turns=NODE_MAX_TURNS,
                progress_callback=node_cb,
            )
            msgs = sub_result.get("messages", []) if isinstance(sub_result, dict) else []
            content = sub_result.get("content", "") if isinstance(sub_result, dict) else str(sub_result)
            all_messages.extend(msgs)
            context[node.id] = content

            # Preserve the in-band failure convention: content prefixed
            # with "FAILED:" marks the node as failed (the engine retries).
            if content.startswith("FAILED:"):
                raise RuntimeError(content[7:])
            return content

        async def _on_progress(nid: str, node_result: NodeResult) -> None:
            """Update the task status and emit a progress event.

            Called by the DAG executor whenever a task completes, fails,
            or is skipped. Updates the shared ``node_meta`` and emits
            a workflow_task event so the frontend can update the task card.
            """
            meta = node_meta.get(nid)
            if meta is not None and node_result.status in ("completed", "failed", "skipped"):
                meta["status"] = node_result.status
            await _emit([{
                "role": "workflow",
                "type": "workflow_task",
                "workflow_id": workflow_id,
                "task_id": nid,
                "task_name": node_result.node_name,
                "status": node_result.status,
            }])

        def _status_lines(title: str, subtitle: str = "") -> str:
            """Format a status summary with per-task results.

            Produces a markdown-formatted summary with:
            - A title (e.g., "Workflow Results: <goal>")
            - An optional subtitle
            - A list of tasks with status icons:
              - [x] completed
              - [!] failed
              - [>] running/pending
            """
            lines = [f"## {title}", ""]
            if subtitle:
                lines += [subtitle, ""]
            for _nid, node in tree.nodes.items():
                icon = "[x]" if node.status == "completed" else "[!]" if node.status == "failed" else "[>]"
                # Truncate results to 200 chars to keep the summary readable.
                lines.append(f"- {icon} **{node.name}**: {node.result[:200] if node.result else node.status}")
            return "\n".join(lines)

        executor = DagExecutor(
            runner=_run_task_node,
            max_retries=2,
            retry_delay=1.0,
            max_concurrent=MAX_PARALLEL_NODES,
        )
        overall_timeout = timeout_seconds if timeout_seconds > 0 else DEFAULT_LIGHT_TIMEOUT_SECONDS
        try:
            await asyncio.wait_for(
                executor.run(tree, context={}, progress_cb=_on_progress),
                timeout=overall_timeout,
            )
        except asyncio.TimeoutError:
            # Timeout: cancel the executor and return partial results.
            executor.cancel()
            logger.warning("[workflow] timed out after %.0fs", overall_timeout)
            await _emit([{
                "role": "workflow",
                "type": "workflow_completed",
                "workflow_id": workflow_id,
                "goal": tree.goal,
                "success": False,
                "error": f"timed out after {int(overall_timeout)}s",
                "completed_count": sum(1 for n in tree.nodes.values() if n.status == "completed"),
                "failed_count": sum(1 for n in tree.nodes.values() if n.status == "failed"),
                "skipped_count": sum(1 for n in tree.nodes.values() if n.status == "skipped"),
            }])
            return {
                "content": _status_lines(
                    f"Workflow timed out: {tree.goal}",
                    f"Stopped after {int(overall_timeout)}s; partial results below.",
                ),
                "messages": _merged_messages() or all_messages,
            }
        except asyncio.CancelledError:
            # User stopped the workflow (per-tool stop button cancels the
            # tool task). Preserve the nodes' streamed transcripts and
            # per-node status so the tool card keeps the partial work.
            logger.info("[workflow] interrupted by user; %d nodes had started", len(node_meta))
            return {
                "content": _status_lines(
                    f"Workflow interrupted: {tree.goal}",
                    "Stopped by user before completion.",
                ),
                "messages": _merged_messages() or all_messages,
            }
        except Exception as exc:
            executor.cancel()
            logger.exception("[workflow] light execution failed")
            return {
                "content": f"Workflow failed: {exc}\n\n{_status_lines(f'Partial status: {tree.goal}')}",
                "messages": _merged_messages() or all_messages,
            }

        success = all(n.status == "completed" for n in tree.nodes.values())
        await _emit([{
            "role": "workflow",
            "type": "workflow_completed",
            "workflow_id": workflow_id,
            "goal": tree.goal,
            "success": success,
            "completed_count": sum(1 for n in tree.nodes.values() if n.status == "completed"),
            "failed_count": sum(1 for n in tree.nodes.values() if n.status == "failed"),
            "skipped_count": sum(1 for n in tree.nodes.values() if n.status == "skipped"),
        }])

        return {
            "content": _status_lines(f"Workflow Results: {tree.goal}"),
            "messages": _merged_messages() or all_messages,
        }

    # ------------------------------------------------------------------
    # Mode B: team -- role-specialised swarm with review gate + consensus.
    # ------------------------------------------------------------------
    async def _run_team() -> dict[str, Any]:
        """Execute the workflow in team mode using the swarm session.

        Team mode is more sophisticated than light mode: it assigns
        specialised roles to each task (architect, coder, reviewer, etc.),
        gates coder output through a reviewer, shares context via a
        blackboard, and runs a proposal-vote consensus step when multiple
        tasks produce results. Use this mode when role specialisation
        and cross-agent verification are important (e.g., design +
        implement + review + test).

        Returns:
            A dict with ``content`` (status summary) and ``messages``
            (merged transcripts from all tasks).
        """
        workflow_id = f"swarm_{uuid.uuid4().hex[:10]}"

        async def _runner(
            prompt: str,
            *,
            system_prompt: str = "",
            max_turns: int = NODE_MAX_TURNS,
            progress_callback: Any = None,
        ) -> dict[str, Any]:
            """Delegate a swarm task to the host loop's ``_run_sub_agent``.

            This function is called by the swarm session for each task.
            It passes through to the host loop's ``_run_sub_agent`` which
            handles the actual sub-agent execution, including depth fencing,
            live progress streaming, and transcript persistence.
            """
            return await loop._run_sub_agent(
                prompt=prompt,
                system_prompt=system_prompt,
                max_turns=max_turns,
                progress_callback=progress_callback,
            )

        async def _on_event(event: Any) -> None:
            """Map a ``SwarmEvent`` onto the frontend progress-callback protocol.

            The swarm session emits various event types (planning,
            task_started, task_completed, task_failed, consensus, etc.).
            This function translates each event into the frontend's
            progress-callback protocol and emits it via ``_emit``.

            Task-terminal events (started, completed, failed) also trigger
            a re-emit of the merged messages so the frontend can update
            the task card statuses immediately.
            """
            msg: dict[str, Any] = {"role": "swarm", "workflow_id": workflow_id}
            et = event.type
            if et == "planning":
                msg.update({
                    "type": "workflow_started",
                    "goal": goal,
                    "progress": event.progress,
                })
            elif et == "task_started":
                meta = node_meta.get(event.task_id)
                if meta is not None:
                    meta["status"] = "running"
                msg.update({
                    "type": "workflow_task",
                    "task_id": event.task_id,
                    "task_name": event.task_name,
                    "role": event.role,
                    "status": "started",
                })
            elif et == "task_completed":
                meta = node_meta.get(event.task_id)
                if meta is not None:
                    meta["status"] = "completed"
                msg.update({
                    "type": "workflow_task",
                    "task_id": event.task_id,
                    "task_name": event.task_name,
                    "role": event.role,
                    "status": "completed",
                    # Truncate result to 500 chars to keep events compact.
                    "result": (event.result or "")[:500],
                })
            elif et == "task_failed":
                meta = node_meta.get(event.task_id)
                if meta is not None:
                    meta["status"] = "failed"
                msg.update({
                    "type": "workflow_task",
                    "task_id": event.task_id,
                    "task_name": event.task_name,
                    "status": "failed",
                    "error": event.error,
                })
            elif et == "consensus":
                cr = _consensus_to_dict(event.consensus)
                msg.update({
                    "type": "swarm_consensus",
                    "consensus": cr,
                })
            elif et == "team_finished":
                msg.update({
                    "type": "workflow_completed",
                    "summary": event.result,
                    "progress": event.progress,
                    "success": True,
                })
            elif et == "error":
                msg.update({
                    "type": "workflow_completed",
                    "error": event.error,
                    "success": False,
                })
            else:
                return
            await _emit([msg])
            # Task-terminal events change divider statuses -- refresh the
            # merged node view so completed/failed cards flip immediately.
            if et in ("task_started", "task_completed", "task_failed"):
                await _emit(_merged_messages())

        session = EncreSwarmSession(
            agent=None,
            goal=goal,
            timeout_seconds=timeout_seconds if timeout_seconds > 0 else DEFAULT_TEAM_TIMEOUT_SECONDS,
            sub_agent_runner=_runner,
        )

        try:
            result = await session.execute(
                goal=goal,
                on_event=_on_event,
                node_progress_factory=_node_progress_factory,
            )
        except asyncio.CancelledError:
            # User stopped the workflow mid-team-run. Preserve the
            # teammates' streamed transcripts and per-node status.
            logger.info("[workflow] team interrupted by user; %d nodes had started", len(node_meta))
            lines = [f"## Workflow interrupted: {goal}", "", "Stopped by user before completion."]
            for tid, meta in node_meta.items():
                lines.append(f"- [{meta.get('status', 'running')}] {meta.get('task_name', tid)}")
            return {"content": "\n".join(lines), "messages": _merged_messages()}
        except Exception as exc:
            logger.exception("[workflow] team execution failed")
            return {"content": f"Workflow failed: {exc}", "messages": _merged_messages()}

        # Format the consolidated result for the parent model.
        lines = [f"## Workflow Result (team mode): {goal}\n"]
        lines.append(result.summary)
        lines.append("")
        lines.append(
            f"Tasks: {result.completed_tasks}/{result.total_tasks} succeeded, "
            f"{result.failed_tasks} failed in {result.elapsed_seconds:.1f}s"
        )
        if result.results:
            lines.append("\n### Per-task results:")
            for tid, res in result.results.items():
                lines.append(f"- [{tid}] {res[:300]}")
        if result.consensus is not None:
            cr = _consensus_to_dict(result.consensus)
            lines.append(f"\n### Consensus: {cr.get('outcome', 'n/a')}")
            lines.append(f"Votes: {cr.get('vote_counts', {})}")
            if cr.get("selected_task_id"):
                lines.append(f"Selected proposal: [{cr['selected_task_id']}]")
            if cr.get("rationale"):
                lines.append(cr["rationale"])
        return {"content": "\n".join(lines), "messages": _merged_messages()}

    if mode == "team":
        return await _run_team()
    return await _run_light()


workflow_tool = build_tool(
    name="workflow",
    description=(
        "Orchestrate a complex, multi-step goal as a coordinated multi-agent "
        "workflow.\n\n"
        "WHAT: decomposes the goal into a DAG of sub-tasks and executes it as "
        "sub-agent runs with dependency-aware parallelism, retries, and live "
        "progress streaming. Two modes: 'light' (default) runs each task as a "
        "plain capable sub-agent with dependency-output injection; 'team' "
        "additionally assigns specialised roles (architect / coder / reviewer "
        "/ tester / debugger), gates coder output through a reviewer, shares "
        "context via a blackboard, and runs a proposal-vote consensus step "
        "when two or more agents produce results.\n"
        "WHEN to use: multi-step goals that need decomposition -- building a "
        "full app, researching a topic from several angles, refactoring a "
        "codebase. Prefer 'team' when role specialisation and cross-agent "
        "verification matter (e.g. design + implement + review + test); "
        "prefer 'light' for straightforward sequential/parallel pipelines.\n"
        "WHEN NOT to use: for a single focused task use the 'agent' tool; for "
        "independent parallel sub-tasks with no dependencies or review gate "
        "use the 'agent' tool with a `tasks` array; do not use either for "
        "trivial work you can do yourself in 1-2 steps.\n"
        "TIPS: state the desired end result and acceptance criteria "
        "explicitly; a precise goal yields a cleaner decomposition and "
        "better role assignment in 'team' mode.\n"
        "PITFALLS: workflows cannot be spawned from inside a sub-agent (one "
        "level of delegation only); 'team' mode is heavier than 'light' -- "
        "don't use it for a 2-step task.\n"
        "IMPORTANT: The goal MUST be written in English -- all participants "
        "think, reason, and respond in English for reliable state matching "
        "and output parsing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "The complete goal to accomplish. MUST be written in "
                    "English -- all participants think and respond in English "
                    "for reliable parsing. Be specific about the desired end "
                    "result, scope, and any acceptance criteria so the "
                    "planner can decompose correctly and assign the right "
                    "roles."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["light", "team"],
                "description": (
                    "Execution mode (optional, default 'light'). 'light': "
                    "plain sub-agent pipeline over the task DAG. 'team': "
                    "role-specialised teammates with reviewer gate, shared "
                    "blackboard, and consensus."
                ),
            },
            "timeout_seconds": {
                "type": "number",
                "description": (
                    "Overall wall-clock budget in seconds for the whole "
                    "workflow. YOU decide this from goal size: small "
                    "pipelines 300-900, multi-step builds 1800-3600. On "
                    "timeout the run stops and partial per-node results are "
                    "returned. Defaults: light 1800, team 3600 (optional)."
                ),
            },
        },
        "required": ["goal"],
    },
    execute=_workflow_execute,
    intents=["general", "coding", "system"],
    category="delegation",
    semantic_type="orchestrate",
)
