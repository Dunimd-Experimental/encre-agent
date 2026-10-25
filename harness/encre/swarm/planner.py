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

# Hierarchical task decomposition for swarms.
#
# ``EncreTaskPlanner`` turns a high-level goal into a ``TaskTree`` (a DAG of
# ``TaskNode``s with dependencies and role assignments).  Simple/recognised
# goals use a fast rule-based path (known workflow patterns or a single task);
# richer goals can be driven by an LLM via ``plan_with_llm`` /
# ``plan_from_json``.  The produced tree is consumed by ``DagExecutor``
# (``encre.dag.executor``), the single TaskTree execution engine.
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from encre.logging_config import get_logger
from encre.prompts.loader import PromptLoader

logger = get_logger(__name__)


@dataclass
class TaskNode:
    """A single node in a swarm ``TaskTree`` (one unit of planned work).

    Attributes:
        id: Unique node identifier within the tree.
        name: Short task title.
        description: Detailed instruction for the assigned agent.
        assigned_role: Role name (see ``encre.swarm.roles``) that should run it.
        dependencies: Ids of nodes that must complete first.
        priority: Scheduling priority among ready nodes (higher = sooner).
        status: Lifecycle state (``pending``/``ready``/``running``/
            ``completed``/``failed``/``skipped``).
        result / error: Captured output / failure detail after execution.
        metadata: Free-form caller bag.
    """
    id: str
    name: str
    description: str
    assigned_role: str = "general"
    dependencies: list[str] = field(default_factory=list)
    priority: int = 0
    status: str = "pending"  # pending | ready | running | completed | failed | skipped
    result: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskTree:
    goal: str
    nodes: dict[str, TaskNode]
    entry_nodes: list[str]
    exit_nodes: list[str]

    def get_ready_nodes(self) -> list[TaskNode]:
        ready: list[TaskNode] = []
        for node in self.nodes.values():
            if node.status != "pending":
                continue
            if all(
                self.nodes[dep].status == "completed"
                for dep in node.dependencies
                if dep in self.nodes
            ):
                ready.append(node)
        ready.sort(key=lambda n: (-n.priority, n.name))
        return ready

    def all_done(self) -> bool:
        return all(
            n.status in ("completed", "skipped")
            for n in self.nodes.values()
        )

    def has_failure(self) -> bool:
        return any(n.status == "failed" for n in self.nodes.values())


class EncreTaskPlanner:
    """LLM-driven hierarchical task decomposition.

    Takes a high-level goal, returns a TaskTree (DAG) ready for execution.
    Includes a rule-based fallback (no LLM call) for simple goals.
    """

    def __init__(self) -> None:
        self._known_patterns: dict[str, list[dict[str, Any]]] = {
            "build": [
                {"name": "Design architecture", "role": "architect", "priority": 10},
                {"name": "Implement core logic", "role": "coder", "deps": [0], "priority": 5},
                {"name": "Write tests", "role": "tester", "deps": [1], "priority": 5},
                {"name": "Code review", "role": "reviewer", "deps": [1], "priority": 3},
                {"name": "Integrate & verify", "role": "coder", "deps": [2, 3], "priority": 1},
            ],
            "debug": [
                {"name": "Reproduce the issue", "role": "debugger", "priority": 10},
                {"name": "Identify root cause", "role": "debugger", "deps": [0], "priority": 5},
                {"name": "Implement fix", "role": "coder", "deps": [1], "priority": 5},
                {"name": "Verify fix", "role": "tester", "deps": [2], "priority": 3},
            ],
            "research": [
                {"name": "Gather information", "role": "researcher", "priority": 10},
                {"name": "Analyze findings", "role": "researcher", "deps": [0], "priority": 5},
                {"name": "Synthesize report", "role": "general", "deps": [1], "priority": 3},
                {"name": "Review & refine", "role": "reviewer", "deps": [2], "priority": 1},
            ],
            "refactor": [
                {"name": "Analyze current structure", "role": "architect", "priority": 10},
                {"name": "Plan refactoring", "role": "architect", "deps": [0], "priority": 5},
                {"name": "Execute refactoring", "role": "coder", "deps": [1], "priority": 5},
                {"name": "Run tests", "role": "tester", "deps": [2], "priority": 3},
                {"name": "Review changes", "role": "reviewer", "deps": [2, 3], "priority": 1},
            ],
        }

    async def decompose(
        self, goal: str, backend: Any = None, context: str = "",
    ) -> TaskTree:
        """Decompose a goal into a task tree (async).

        LLM planning is the primary path whenever a backend is supplied:
        the model sees the real goal and produces a real DAG.  The keyword
        templates in :meth:`plan` are demoted to the explicit no-backend
        fallback -- they exist so a backend-less host still gets a sane
        skeleton, never to impersonate an LLM plan.  When a backend was
        supplied but planning failed, the fallback is a single honest
        ``Execute`` node (with a warning), not a template masquerading as a
        plan.

        Args:
            goal: The high-level goal text.
            backend: Optional LLM backend for model-driven planning.
            context: Optional shared context passed to the planning prompt.

        Returns:
            The planned ``TaskTree``.
        """
        if backend is not None:
            tree = await self.plan_with_backend(goal, backend, context=context)
            if tree is not None:
                return tree
            logger.warning(
                "[swarm] LLM planning failed for goal=%r -- falling back to single-node execution",
                goal[:120],
            )
            return self._simple_decompose(goal)
        return self.plan(goal)

    async def plan_with_backend(
        self, goal: str, backend: Any, context: str = "",
    ) -> TaskTree | None:
        """Model-driven planning: one bounded thinking-off LLM call.

        Builds the structured planning prompt via :meth:`plan_with_llm`,
        calls the backend (thinking disabled: planning must be deterministic
        output, not deliberation), and parses the JSON answer through
        :meth:`plan_from_json`.  Fail-closed: a missing backend, a failed
        call, an unparsable answer or an empty task list yields ``None`` --
        the caller degrades to the single-node fallback instead of running
        a fabricated plan.

        Args:
            goal: The high-level goal text.
            backend: The LLM backend (thinking-disabled-cloned internally).
            context: Optional shared context for the planning prompt.

        Returns:
            A ``TaskTree`` on success, else ``None``.
        """
        if backend is None:
            return None
        from encre.utils.backend_clone import thinking_off_clone
        from encre.utils.types import BackendFinish, BackendText

        scorer = thinking_off_clone(backend)
        if scorer is None:
            return None
        prompt = self.plan_with_llm(goal, context)
        full_text = ""
        try:
            async for event in scorer.chat(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000, stream=False,
            ):
                if isinstance(event, BackendText):
                    full_text += event.text
                elif isinstance(event, BackendFinish):
                    break
        except Exception:
            logger.warning("[swarm] planning call failed", exc_info=True)
            return None
        payload = full_text.strip()
        if payload.startswith("```"):
            payload = payload.strip("`")
            if payload.startswith("json"):
                payload = payload[4:]
            payload = payload.strip()
        try:
            tree = self.plan_from_json(goal, payload)
        except Exception:
            logger.warning("[swarm] planning answer unparsable; failing closed")
            return None
        if not tree.nodes:
            logger.warning("[swarm] planning produced zero tasks; failing closed")
            return None
        return tree

    def plan(self, goal: str, _context: str = "") -> TaskTree:
        """No-backend fallback: recognise a known workflow pattern or wrap
        the goal in a single ``Execute`` node.

        This path never calls a model; it exists for backend-less hosts.
        Backend-equipped callers must go through :meth:`decompose` /
        :meth:`plan_with_backend` instead.

        Args:
            goal: The high-level goal text.
            _context: Reserved; unused by the rule-based path.

        Returns:
            The planned ``TaskTree``.
        """
        pattern_key = _detect_pattern(goal)
        if pattern_key and pattern_key in self._known_patterns:
            return self._build_from_pattern(goal, pattern_key)

        return self._simple_decompose(goal)

    def plan_with_llm(
        self,
        goal: str,
        context: str,
        plan_prompt: str | None = None,
    ) -> str:
        """Returns a structured prompt for an LLM to generate a task plan.
        The LLM's JSON response can be parsed by plan_from_json().
        """
        if plan_prompt is None:
            plan_prompt = _DEFAULT_PLAN_PROMPT
        return plan_prompt.format(goal=goal, context=context)

    @staticmethod
    def plan_from_json(goal: str, json_str: str) -> TaskTree:
        data = json.loads(json_str)
        nodes: dict[str, TaskNode] = {}
        for item in data.get("tasks", []):
            nid = item.get("id", str(uuid.uuid4())[:8])
            nodes[nid] = TaskNode(
                id=nid,
                name=item.get("name", "Unnamed task"),
                description=item.get("description", ""),
                assigned_role=item.get("role", "general"),
                dependencies=item.get("dependencies", []),
                priority=item.get("priority", 5),
            )
        entry_ids = data.get("entry_tasks", [])
        exit_ids = data.get("exit_tasks", [])
        if not entry_ids and nodes:
            entry_ids = [
                nid for nid, n in nodes.items()
                if not n.dependencies
            ][:1]
        if not exit_ids and nodes:
            all_ids = set(nodes.keys())
            dep_set: set[str] = set()
            for n in nodes.values():
                dep_set.update(n.dependencies)
            exit_ids = list(all_ids - dep_set) or [list(nodes.keys())[-1]]
        return TaskTree(goal=goal, nodes=nodes, entry_nodes=entry_ids, exit_nodes=exit_ids)

    def _build_from_pattern(self, goal: str, pattern_key: str) -> TaskTree:
        pattern = self._known_patterns[pattern_key]
        nodes: dict[str, TaskNode] = {}
        id_map: dict[int, str] = {}
        for i, step in enumerate(pattern):
            nid = str(uuid.uuid4())[:8]
            id_map[i] = nid
            nodes[nid] = TaskNode(
                id=nid,
                name=step["name"],
                description=f"{step['name']} for: {goal}",
                assigned_role=step.get("role", "general"),
                dependencies=[id_map[d] for d in step.get("deps", []) if d in id_map],
                priority=step.get("priority", 5),
            )
        entry_ids = [nid for nid, n in nodes.items() if not n.dependencies]
        all_ids = set(nodes.keys())
        dep_set: set[str] = set()
        for n in nodes.values():
            dep_set.update(n.dependencies)
        exit_ids = list(all_ids - dep_set) or [list(nodes.keys())[-1]]
        return TaskTree(goal=goal, nodes=nodes, entry_nodes=entry_ids, exit_nodes=exit_ids)

    @staticmethod
    def _simple_decompose(goal: str) -> TaskTree:
        """Fallback: wrap the whole goal in one ``general`` role task node."""
        nid = str(uuid.uuid4())[:8]
        node = TaskNode(
            id=nid,
            name="Execute",
            description=goal,
            assigned_role="general",
        )
        return TaskTree(
            goal=goal,
            nodes={nid: node},
            entry_nodes=[nid],
            exit_nodes=[nid],
        )


def _detect_pattern(goal: str) -> str | None:
    """Map a goal string to a known workflow pattern key, or ``None``.

    Uses keyword matching (lower-cased) for ``build``, ``debug``, ``research``,
    and ``refactor`` families so the planner can pick a pre-baked
    ``_known_patterns`` DAG instead of invoking the LLM.
    """
    g = goal.lower()
    if any(kw in g for kw in ("build", "create", "make", "implement", "write a", "develop")):
        return "build"
    if any(kw in g for kw in ("debug", "fix", "bug", "error", "issue", "broken", "wrong")):
        return "debug"
    if any(kw in g for kw in ("research", "investigate", "explore", "study", "analyze", "understand")):
        return "research"
    if any(kw in g for kw in ("refactor", "clean up", "restructure", "reorganize", "improve")):
        return "refactor"
    return None


_loader = PromptLoader()

_DEFAULT_PLAN_PROMPT = _loader.load("planner", category="swarm")
