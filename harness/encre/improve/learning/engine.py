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

"""Learning engine that crystallises repeated tool patterns into skills.

This module implements the proactive half of the learning subsystem.
:class:`LearningEngine` watches the tool calls an agent makes during a run and,
once it believes a pattern is repeated often enough, asks
:class:`~encre.learning.skill_generator.SkillGenerator` to turn that pattern
into an auto-generated skill and register it with the skill store.

How it works
------------
* The engine is fed the names of the tools used in a run via
  :meth:`LearningEngine.analyze_run`. It does not track the call sequence
  itself in detail; it only counts how many tools were involved.
* When the number of distinct tool names in a single run reaches
  :attr:`TOOL_CALL_THRESHOLD`, the engine spawns a background asyncio task that
  performs the (potentially slow) skill generation off the critical path.
* Generated skills are **not** activated directly. They are handed to the
  :class:`~encre.improve.verification.SkillVerifier` as *proposed* artifacts
  and only promoted once they pass verification; a failing artifact is rejected
  with recorded reasons. This prevents a bad run from permanently polluting
  long-term behaviour.
* All in-flight crystallisation tasks are tracked so that :meth:`stop` can
  cancel them and wait for them to finish tearing down.

Caveats
-------
The engine deliberately ignores partial failures: if a generated skill cannot
be proposed or verified, the error is logged and the engine simply forgets the
task. This keeps the learning side-effects from ever blocking or crashing the
main agent loop.
"""


import asyncio
import contextlib
import logging
import secrets
import time

from encre.agent import EncreAgent

from encre.improve.verification import SkillVerifier

logger = logging.getLogger("encre.learning")


class LearningEngine:
    """Spawns skill-crystallisation tasks from frequent tool use.

    The engine is a thin orchestrator: it decides *when* a run is worth
    learning from, and delegates the actual skill creation to
    :class:`~encre.learning.skill_generator.SkillGenerator`. It owns an agent
    reference (used only to construct the generator) and a small bookkeeping
    structure that tracks the background tasks it has launched.

    Attributes
    ----------
    TOOL_CALL_THRESHOLD:
        Minimum number of distinct tool names a run must use before the engine
        considers the pattern worth crystallising into a skill. Set to 5.
    _agent:
        The agent whose skills will be generated and registered.
    _running:
        Lifecycle flag; the engine only acts on runs while this is ``True``.
    _tasks:
        Live list of :class:`asyncio.Task` objects spawned for crystallisation.
        Entries remove themselves when their task completes.
    _verifier:
        The :class:`SkillVerifier` gate that proposed skills must pass before
        they become active. Created lazily from the owning agent when not
        injected.
    """

    # Below this many distinct tools a run is treated as too small to learn from.
    TOOL_CALL_THRESHOLD = 5

    def __init__(
        self,
        agent: EncreAgent,
        *,
        verifier: SkillVerifier | None = None,
    ) -> None:
        """Initialise the engine with its owning agent and verification gate.

        Args:
            agent: The agent used to build skill generators. The reference is
                stored and later handed to :class:`SkillGenerator`.
            verifier: Optional verification gate. When omitted one is created
                lazily for the agent on first use.

        Returns:
            None.
        """
        self._agent = agent
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._verifier = verifier

    def _get_verifier(self) -> SkillVerifier:
        """Return the verification gate, creating one for the agent if needed.

        The gate is bound to the owning agent's loop so that promoted skills
        are written into procedural memory (Task 7.4).  Binding is best-effort
        and idempotent; a memory fault can never block skill promotion.
        """
        if self._verifier is None:
            self._verifier = SkillVerifier(self._agent)
        self._bind_procedural_memory(self._verifier)
        return self._verifier

    def _bind_procedural_memory(self, verifier: SkillVerifier) -> None:
        """Bind ``verifier`` to the agent loop's procedural-memory bridge.

        Resolved duck-typed (``agent.loop.attach_skill_verifier``) so this
        module stays independent of the loop package.  The loop owns the
        actual write into :class:`~encre.memdir.layers.MemoryLayerStore`; here
        we only connect the gate's ``on_promoted`` seam.  Never raises.
        """
        attach = getattr(
            getattr(self._agent, "loop", None), "attach_skill_verifier", None,
        )
        if not callable(attach):
            return
        try:
            attach(verifier)
        except Exception:
            logger.warning(
                "failed to bind skill verifier to procedural memory", exc_info=True,
            )

    async def start(self) -> None:
        """Mark the engine as running and migrate any legacy auto-generated skills.

        Migration is best-effort and idempotent: pre-verification artifacts are
        marked ``proposed`` so they cannot be injected until they pass the gate.

        Returns:
            None.
        """
        self._running = True
        # Best-effort: migration must never prevent the engine from starting.
        with contextlib.suppress(Exception):
            self._get_verifier().migrate_legacy()
        logger.info("Learning engine started")

    async def stop(self) -> None:
        """Shut the engine down, cancelling all in-flight crystallisation.

        Cancels every tracked task, waits for them to finish (exceptions are
        collected and ignored so a failing skill generation cannot block
        shutdown), then clears the task list. Idempotent after the first call
        because ``_running`` is ``False`` and the task list is empty.

        Returns:
            None.
        """
        self._running = False
        for task in self._tasks:
            task.cancel()
        # Gather with return_exceptions so a cancelled/failed task does not
        # propagate an exception out of shutdown.
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Learning engine stopped")

    async def analyze_run(
        self,
        tool_names: list[str],
        prompt: str,
        *,
        run_id: str = "",
    ) -> None:
        """Consider a finished run and perhaps queue a crystallisation task.

        The engine ignores runs while stopped or while the run did not involve
        enough distinct tools. When the run clears the threshold, a background
        task is created for :meth:`_crystallize` and appended to the tracked
        task list; a done-callback later prunes it once finished.

        Args:
            tool_names: Names of the tools used during the run. Used only to
                measure how "rich" the run was against
                :attr:`TOOL_CALL_THRESHOLD`.
            prompt: The original user prompt for the run, passed through to the
                generator so the generated skill can capture intent.
            run_id: Optional identifier for the originating run; recorded on the
                proposed artifact for provenance. A synthetic id is minted when
                omitted.

        Returns:
            None.
        """
        if not self._running:
            return
        if len(tool_names) < self.TOOL_CALL_THRESHOLD:
            return
        if not run_id:
            run_id = f"run-{int(time.time())}-{secrets.token_hex(3)}"
        # Offload the (potentially slow) generation to the event loop so the
        # caller's analysis path is never blocked.
        task = asyncio.create_task(self._crystallize(tool_names, prompt, run_id))
        self._tasks.append(task)
        # When the task finishes, drop it from the list if still present.
        task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)

    async def _crystallize(self, tool_names: list[str], prompt: str, run_id: str = "") -> None:
        """Generate, propose and verify a skill from the observed tool pattern.

        Builds a :class:`SkillGenerator` wired to the verification gate, asks it
        to synthesise a skill definition from the tool names and prompt, records
        it as a proposed artifact, then runs verification. A passing artifact is
        promoted to active; a failing one is rejected with recorded reasons. A
        ``None`` result from the generator means nothing worth saving was found,
        so we silently stop.

        Args:
            tool_names: Tool names that formed the pattern to learn.
            prompt: The originating prompt, preserved as skill context.
            run_id: Identifier of the originating run, stored on the artifact.

        Returns:
            None.

        Raises:
            Any exception raised by the generator or verifier is allowed to
            propagate to the task wrapper; the engine does not catch it here,
            so callers awaiting the task (or the done-callback pruner) should
            treat failures as non-fatal.
        """
        # Imported lazily to avoid a circular import at module load time.
        from encre.improve.learning.skill_generator import SkillGenerator

        verifier = self._get_verifier()
        generator = SkillGenerator(self._agent, verifier=verifier)
        skill_def = generator.generate(tool_names, prompt)
        if skill_def is None:
            return
        artifact = await generator.register(skill_def, run_id=run_id)
        if artifact is None:
            return
        # Verification decides promote vs reject; unverified output never
        # reaches the main prompt.
        verifier.verify(artifact)
