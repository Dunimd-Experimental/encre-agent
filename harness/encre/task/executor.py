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

# Executor for the simple task layer.
#
# ``EncreTaskExecutor`` runs an ``EncreTask`` created via ``EncreTaskManager``.
# Depending on ``task_type`` it shells out to bash, spins up a nested
# ``EncreLoop`` sub-agent, or interprets the prompt as a newline-separated list
# of shell commands (a mini-workflow).  Status and results are written back
# through the manager.

from encre.task.manager import EncreTaskManager
from encre.task.types import EncreTask


class EncreTaskExecutor:
    """Runs individual ``EncreTask`` records to completion.

    The executor is a thin async dispatcher: it marks the task ``running``,
    selects an execution strategy based on ``task.task_type`` (``bash``,
    ``agent``, or ``workflow``), records the outcome, and returns the textual
    result.  Unexpected exceptions are captured into the task's ``error`` field
    rather than propagated.
    """
    def __init__(self) -> None:
        self._manager = EncreTaskManager()

    async def execute_task(self, task_id: str) -> str:
        """Execute the task identified by *task_id*.

        Returns the task's output on success or an error string on failure /
        when the task does not exist.  Side effects: updates the task's
        status/result/error via the manager.
        """
        task = self._manager.get_task(task_id)
        if task is None:
            return f"Error: Task not found: {task_id}"

        self._manager.update_task(task_id, status="running")

        try:
            if task.task_type == "bash":
                result = await self._execute_bash(task)
            elif task.task_type == "agent":
                result = await self._execute_agent(task)
            elif task.task_type == "workflow":
                result = await self._execute_workflow(task)
            else:
                result = f"Error: Unknown task type: {task.task_type}"

            self._manager.update_task(task_id, status="completed", result=result)
            return result
        except Exception as e:
            error_msg = str(e)
            self._manager.update_task(task_id, status="failed", error=error_msg)
            return f"Error: {error_msg}"

    async def _execute_bash(self, task: EncreTask) -> str:
        """Run the task prompt as a shell command via ``asyncio`` subprocess.

        Captures combined stdout/stderr (decoded with replacement for invalid
        bytes) and returns it as the task result.  A 120s communication timeout
        bounds runaway commands.
        """
        import asyncio
        import subprocess

        from encre.tools._encoding import decode_bytes
        from encre.capabilities.process import hidden_subprocess_kwargs
        kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        kwargs.update(hidden_subprocess_kwargs())

        proc = await asyncio.create_subprocess_shell(
            task.prompt,
            **kwargs,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        output = decode_bytes(stdout)
        if stderr:
            output += "\n" + decode_bytes(stderr)
        return output

    async def _execute_agent(self, task: EncreTask) -> str:
        """Execute the task prompt inside a fresh ``EncreLoop`` sub-agent.

        Builds an ``EncreConfig`` (optionally overridden from ``task.metadata``
        keys ``model``/``api_key``/``base_url``/``max_tokens``), seeds a system
        message describing the subtask, and delegates to the loop's internal
        sub-agent runner.  Returns the sub-agent's final text.
        """
        from encre.config import EncreConfig
        from encre.loop import EncreLoop
        from encre.session import EncreSession

        config = EncreConfig()
        if task.metadata:
            for key in ("model", "api_key", "base_url", "max_tokens"):
                if key in task.metadata:
                    setattr(config, key, task.metadata[key])

        session = EncreSession(config)
        session.add_message(
            "system",
            f"You are executing a subtask: {task.description or task.name}",
        )
        loop = EncreLoop(config, session)
        return await loop._run_sub_agent(task.prompt, [])

    async def _execute_workflow(self, task: EncreTask) -> str:
        """Treat the prompt as a newline-separated shell script.

        Each non-empty line is executed as a separate bash step (via
        ``_execute_bash`` over a throwaway ``EncreTask``) and the per-step
        output is joined with separators, producing a single combined result.
        """
        steps = task.prompt.split("\n")
        results: list[str] = []
        for step in steps:
            step = step.strip()
            if not step:
                continue
            result = await self._execute_bash(
                EncreTask(
                    id="",
                    name="step",
                    description="",
                    task_type="bash",
                    prompt=step,
                )
            )
            results.append(f"$ {step}\n{result}")
        return "\n---\n".join(results)
