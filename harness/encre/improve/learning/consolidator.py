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

"""Periodic memory consolidation for the learning subsystem.

The learning subsystem accumulates experiences in the agent's memory during a
run. Left unchecked, those short-term memories grow without bound and become
expensive to search. This module provides :class:`MemoryConsolidator`, a thin
background driver that periodically asks the agent's memory system to merge,
deduplicate, and summarise its stored memories (a process the code refers to as
"consolidation").

Design notes
------------
* The consolidator owns its own :mod:`asyncio` task and is fully decoupled
  from the agent loop: it never blocks normal execution because all work
  happens on a timer in a separate task.
* Consolidation is best-effort. If the agent's loop has no ``memory_layers``
  store, the consolidator simply does nothing rather than raising. Failures
  during a consolidation pass are logged and swallowed so a misbehaving memory
  backend cannot crash the host loop.

Public surface
--------------
* ``start`` / ``stop`` -- begin and end the background timer.
* ``consolidate_now`` -- trigger a single pass on demand (useful for tests
  and for callers that want to flush memory at a checkpoint).
"""

import asyncio
import contextlib
import logging

from encre.agent import EncreAgent

logger = logging.getLogger("encre.learning.consolidator")


class MemoryConsolidator:
    """Drives periodic, best-effort memory consolidation on a background timer.

    The consolidator is a self-contained async driver. It holds a reference to
    the owning agent purely so it can reach that agent's loop memory store; it
    holds no other state. Lifecycle is explicit: call :meth:`start` to spin up
    the timer task and :meth:`stop` to cancel it cleanly.

    Attributes
    ----------
    _agent:
        The agent whose layered memory store will be consolidated. Accessed
        lazily via ``getattr`` so that an agent lacking a loop or a store is
        handled gracefully.
    _interval:
        Seconds to wait between consolidation passes. Defaults to 3600 (one
        hour).
    _running:
        Boolean flag gating the background loop. Flipped to ``False`` by
        :meth:`stop` to request a graceful exit.
    _task:
        The :class:`asyncio.Task` running :meth:`_loop`, or ``None`` when the
        consolidator is idle.
    """

    def __init__(self, agent: EncreAgent, interval: int = 3600) -> None:
        """Initialise the consolidator with its agent and timer interval.

        Args:
            agent: The agent instance whose loop memory store will be
                consolidated. The reference is stored but not touched until a
                consolidation pass actually runs.
            interval: Seconds to sleep between automatic consolidation passes.
                Defaults to 3600. A smaller value consolidates more often at
                the cost of more store calls.

        Returns:
            None.
        """
        self._agent = agent
        self._interval = interval
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background consolidation loop.

        Sets the running flag and schedules :meth:`_loop` as a new asyncio
        task. Safe to call once; calling it again while already running would
        create a second task and is not guarded against by this method.

        Returns:
            None.
        """
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Memory consolidator started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        """Stop the background loop and await its cancellation.

        Flips the running flag so the loop exits at its next check, cancels the
        task, and waits for the cancellation to propagate. ``CancelledError``
        raised while awaiting the task is expected and suppressed. Idempotent:
        calling it when no task is active is a no-op aside from the log line.

        Returns:
            None.
        """
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Memory consolidator stopped")

    async def _loop(self) -> None:
        """Background timer that triggers consolidation until stopped.

        Cycles through sleeping for the configured interval and then running a
        single consolidation pass. The ``self._running`` re-check after the
        sleep is a small race guard: if :meth:`stop` flipped the flag while we
        were sleeping, we skip the pending pass and exit rather than doing one
        last consolidation right after being asked to stop.

        Returns:
            None.
        """
        while self._running:
            # Wait for the next scheduled tick before doing any work.
            await asyncio.sleep(self._interval)
            # Re-check the flag: stop() may have been called during the sleep.
            if not self._running:
                break
            await self._consolidate()

    async def consolidate_now(self) -> None:
        """Force a single consolidation pass immediately.

        Bypasses the timer and runs :meth:`_consolidate` once. Useful for
        callers that want to flush memory at a checkpoint (for example, before
        a long pause) or for unit tests that should not wait on the interval.

        Returns:
            None.
        """
        await self._consolidate()

    async def _consolidate(self) -> None:
        """Run one consolidation pass against the layered memory store.

        Retrieval and consolidation now live on the **single store**,
        :class:`~encre.memdir.layers.MemoryLayerStore` (whose
        :meth:`~MemoryLayerStore.consolidate` sweeps the short-term buffer).
        The consolidator reaches it through the owning agent's loop
        (``agent.loop.memory_layers``); a missing store -- an agent built
        without a loop or with the memory subsystem unavailable -- is treated
        as "nothing to do".  Any exception from the underlying store is logged
        and swallowed so a failing consolidation never disrupts the host loop.

        Returns:
            None.
        """
        layer_store = getattr(getattr(self._agent, "loop", None), "memory_layers", None)
        if layer_store is None:
            return

        try:
            # The store's consolidate is synchronous; only call it when the
            # backend actually offers the maintenance pass.
            if hasattr(layer_store, "consolidate") and callable(layer_store.consolidate):
                result = layer_store.consolidate()
                logger.info("Memory consolidation completed: %s", result)
        except Exception as e:
            logger.warning("Memory consolidation failed: %s", e)
