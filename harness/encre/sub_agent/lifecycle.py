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

import asyncio
import enum
import time
from dataclasses import dataclass, field
from typing import Any


class SubAgentMode(str, enum.Enum):
    """Lifecycle modes for sub-agent execution.

    Mirrors Claude Code's sync / async / auto-background / worktree patterns.
    """

    SYNC = "sync"
    ASYNC = "async"
    BACKGROUND = "background"
    ISOLATED = "isolated"


@dataclass
class BackgroundTaskInfo:
    """Status information for a background sub-agent task."""

    session_id: str
    agent_name: str
    prompt: str
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class BackgroundSubAgentTracker:
    """Tracks background (fire-and-forget) sub-agent tasks.

    Provides query and cleanup for sub-agents running in ``async`` or
    ``background`` mode.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTaskInfo] = {}
        self._futures: dict[str, asyncio.Task[Any]] = {}

    def register(self, session_id: str, agent_name: str, prompt: str) -> BackgroundTaskInfo:
        info = BackgroundTaskInfo(session_id=session_id, agent_name=agent_name, prompt=prompt)
        self._tasks[session_id] = info
        return info

    def attach_task(self, session_id: str, task: asyncio.Task[Any]) -> None:
        self._futures[session_id] = task

    def complete(self, session_id: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        info = self._tasks.get(session_id)
        if info is None:
            return
        info.status = "error" if error else "completed"
        info.completed_at = time.time()
        info.result = result
        info.error = error

    def get_status(self, session_id: str) -> BackgroundTaskInfo | None:
        return self._tasks.get(session_id)

    def list_active(self) -> list[BackgroundTaskInfo]:
        return [t for t in self._tasks.values() if t.status == "running"]

    def cleanup(self, max_age: float = 3600.0) -> None:
        now = time.time()
        stale = [
            sid for sid, info in self._tasks.items()
            if info.status != "running"
            and info.completed_at is not None
            and (now - info.completed_at) > max_age
        ]
        for sid in stale:
            self._tasks.pop(sid, None)
            fut = self._futures.pop(sid, None)
            if fut is not None and not fut.done():
                fut.cancel()


def detect_background_mode(prompt: str, agent_name: str = "") -> bool:
    """Heuristic: should this task run in background mode?

    Returns True for long-running task patterns:
    - Large file analysis
    - Deep search tasks
    - Agent names like "researcher", "explore" with large prompts
    """
    if len(prompt) > 5000:
        return True
    long_running_agents = {"researcher", "explore", "architect", "critic"}
    if agent_name.lower() in long_running_agents and len(prompt) > 2000:
        return True
    return False
