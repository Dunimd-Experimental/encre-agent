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

"""Process completion registry.

Tracks completion notifications for background shell/agent processes
so the readiness endpoint can report how much work is queued behind
the process completion path. Adapters only read the depth of
:attr:`completion_queue`.

The registry deliberately keeps three separate indexes — running,
finished, and completion_queue — so consumers can answer distinct
questions without polling each other:

* ``running``   — is process X still executing?
* ``finished``  — what was the last known outcome of process X?
* ``completion_queue`` — how many completions are pending delivery?
"""

from __future__ import annotations

import queue


class ProcessRegistry:
    """Process-wide registry of completed background processes."""

    def __init__(self) -> None:
        self.completion_queue: "queue.Queue[object]" = queue.Queue()
        self.running: dict[str, object] = {}
        self.finished: dict[str, object] = {}

    def register(self, process_id: str, handle: object) -> None:
        """Record a newly started process.

        Overwriting an existing entry with the same ``process_id`` is
        intentional: callers should re-register rather than check
        state first, and the last registration wins for any downstream
        lookup.
        """
        self.running[process_id] = handle

    def complete(self, process_id: str, result: object = None) -> None:
        """Move a process to finished and enqueue its completion.

        The completion queue is appended to even when the process was
        never registered — some background processes (e.g. sub-agents
        spawned from a delegated task) never pass through ``register``.
        That lets the readiness endpoint observe work it cannot
        otherwise account for.
        """
        self.running.pop(process_id, None)
        self.finished[process_id] = result
        self.completion_queue.put(result)

    def drain(self) -> list[object]:
        """Remove and return all queued completions.

        Uses ``get_nowait`` in a loop rather than :meth:`queue.Queue.get`
        so this call is O(n) in the queue depth and never blocks;
        the readiness probe runs on a hot path.
        """
        items: list[object] = []
        while True:
            try:
                items.append(self.completion_queue.get_nowait())
            except queue.Empty:
                break
        return items


# Module-level singleton: the readiness endpoint and the process spawn
# helpers must agree on a single queue, so both reach for this object
# rather than instantiating their own.
process_registry = ProcessRegistry()
