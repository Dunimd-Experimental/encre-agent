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

"""The pluggable phase contract of the agent loop (P1.1).

The former hard-coded call sequence in
:meth:`~encre.loop.runner.EncreLoop._run_impl` is driven by a
:class:`~encre.loop.phase_registry.PhaseRegistry` instead.  This module
defines the contract every phase implements:

* :class:`PhaseScope` -- does the phase run once per ``run()`` (``RUN``) or
  once per turn iteration (``TURN``)?  The split mirrors the real control
  flow: the system prompt / session start are run-scoped, the model / tool /
  recovery / end-of-turn phases are turn-scoped.
* :class:`PhaseAction` / :class:`PhaseResult` -- the control-flow outcome of
  one phase.  The actions encode the exact branches the runner used to
  inline: ``CONTINUE`` (next phase), ``END_TURN`` (the historical
  ``t.do_continue`` -> ``continue``), ``END_RUN`` (the historical
  ``t.do_break`` -> ``break``) and ``EXIT`` (the historical
  :class:`~encre.loop.turn_ctx.TurnExit` -> ``return``).
* :class:`PhaseContext` -- what a phase sees: the shared
  :class:`~encre.loop.turn_ctx.TurnContext`, an ``emit`` sink for streaming
  events, and typed read-only accessors for the collaborators phases
  actually use.
* :class:`PhaseProtocol` -- the structural type the registry stores.

Honesty note: phases genuinely mutate loop-level state (guardrail counters,
budget state, prompt caches, the PDR engine, ...).  :class:`PhaseContext`
therefore exposes the live loop explicitly instead of pretending a
read-only facade exists; the typed accessors document the common,
read-mostly surface.
"""


import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from encre.loop.turn_ctx import TurnContext
from encre.modes.mode_profiles import AgentMode, ModeProfile


class PhaseScope(Enum):
    """When a phase executes inside one ``run()`` invocation.

    ``RUN`` phases execute exactly once per ``run()`` before the turn loop;
    ``TURN`` phases execute on every iteration of the turn loop.
    """

    RUN = "run"
    TURN = "turn"


class PhaseAction(Enum):
    """Control-flow action a phase asks the runner to take.

    The actions map one-to-one onto the branches the original monolithic
    ``_run_impl`` expressed with ``t.do_continue`` / ``t.do_break`` /
    :class:`~encre.loop.turn_ctx.TurnExit`.
    """

    CONTINUE = "continue"
    END_TURN = "end_turn"
    END_RUN = "end_run"
    EXIT = "exit"


@dataclass(frozen=True)
class PhaseResult:
    """The outcome of one phase execution.

    Attributes:
        action: The control-flow action for the runner.
        reason: Optional human-readable explanation (logged / surfaced by
            embedders driving the pipeline manually).
    """

    action: PhaseAction = PhaseAction.CONTINUE
    reason: str = ""

    @classmethod
    def continue_(cls, reason: str = "") -> "PhaseResult":
        """Proceed to the next phase in the pipeline."""
        return cls(action=PhaseAction.CONTINUE, reason=reason)

    @classmethod
    def end_turn(cls, reason: str = "") -> "PhaseResult":
        """Skip the remaining phases and start the next turn iteration.

        Mirrors the historical ``t.do_continue`` -> ``continue``.
        """
        return cls(action=PhaseAction.END_TURN, reason=reason)

    @classmethod
    def end_run(cls, reason: str = "") -> "PhaseResult":
        """Skip the remaining phases and leave the turn loop.

        Mirrors the historical ``t.do_break`` -> ``break``; the post-loop
        finalisation still runs.
        """
        return cls(action=PhaseAction.END_RUN, reason=reason)

    @classmethod
    def exit(cls, reason: str = "") -> "PhaseResult":
        """Terminate the whole run immediately.

        Mirrors the historical :class:`~encre.loop.turn_ctx.TurnExit`:
        the run returns at once, skipping the post-loop finalisation.
        """
        return cls(action=PhaseAction.EXIT, reason=reason)


@dataclass
class PhaseOutcome:
    """Mutable accumulator a pipeline driver fills while iterating phases.

    The runner (and any embedder driving a pipeline manually) passes one
    outcome per pipeline execution; the final action of the last phase that
    requested a non-default action lands here.
    """

    action: PhaseAction = PhaseAction.CONTINUE
    reason: str = ""


@dataclass
class PhaseContext:
    """Everything a phase needs, handed to :meth:`PhaseProtocol.run`.

    Attributes:
        loop: The live ``EncreLoop``.  Deliberately the real, mutable loop:
            phases read collaborators from it and mutate loop-level state
            (guardrail counters, budget state, prompt caches, the PDR
            engine, ...).  A read-only facade would be a lie; the mutability
            is documented here instead of hidden.
        turn: The shared :class:`~encre.loop.turn_ctx.TurnContext` for the
            current run / turn.
        sink: Async callable receiving every :class:`~encre.utils.types.AgentEvent`
            the phase streams.  ``None`` (embedders driving phases directly)
            discards events.
    """

    loop: Any
    turn: TurnContext
    sink: Callable[[Any], Awaitable[None]] | None = None

    async def emit(self, event: Any) -> None:
        """Stream one agent event to the pipeline sink (no-op when unset)."""
        if self.sink is not None:
            await self.sink(event)

    # ── Typed read-mostly accessors over the collaborators phases use ──
    @property
    def mode(self) -> AgentMode:
        """The loop's capability profile mode."""
        return self.loop.mode

    @property
    def profile(self) -> ModeProfile:
        """The resolved per-mode tuning profile."""
        return self.loop._profile

    @property
    def config(self):
        """The loop configuration."""
        return self.loop.config

    @property
    def session(self):
        """The persistent session this loop drives."""
        return self.loop.session

    @property
    def backend(self):
        """The lazily created model backend."""
        return self.loop.backend

    @property
    def discovery(self):
        """The tool discovery facade."""
        return self.loop.discovery

    @property
    def event_bus(self):
        """The append-only event backbone."""
        return self.loop.event_bus

    @property
    def event_stream(self):
        """The typed event fan-out projected over the backbone."""
        return self.loop.event_stream

    @property
    def memory_layers(self):
        """The layered memory store (may be ``None``)."""
        return self.loop.memory_layers

    @property
    def hook_system(self):
        """The lifecycle hook system."""
        return self.loop.hook_system

    @property
    def tool_registry(self):
        """The tool registry backing discovery."""
        return self.loop.tool_registry

    @property
    def sub_agent_depth(self) -> int:
        """Nesting depth of this loop (0 = top-level agent)."""
        return self.loop.sub_agent_depth


@runtime_checkable
class PhaseProtocol(Protocol):
    """The structural contract every pipeline phase implements.

    Attributes:
        name: Unique registry key.  Re-registering the same name replaces
            the previous phase in place (the swap mechanism).
        modes: The modes the phase is active in.  ``None`` means all modes.
        scope: Whether the phase runs once per ``run()`` or once per turn.
    """

    name: str
    modes: frozenset[AgentMode] | None
    scope: PhaseScope

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        """Execute the phase and return its control-flow outcome."""
        ...


class LoopPhase:
    """Base class for phases that delegate to a method on the live loop.

    Holds the loop reference and the stream-forwarding helper shared by the
    built-in phase adapters.  The loop reference is typed :class:`Any` on
    purpose: importing :class:`~encre.loop.runner.EncreLoop` here would
    create a circular import (the runner composes the phase mixins), and the
    adapters are constructed by the wiring with the real loop anyway.
    """

    name: str = ""
    modes: frozenset[AgentMode] | None = None
    scope: PhaseScope = PhaseScope.TURN

    def __init__(self, loop: Any) -> None:
        self._loop = loop

    async def _stream_through(
        self, ctx: PhaseContext, stream: AsyncIterator[Any],
    ) -> None:
        """Forward every event of ``stream`` through the context sink.

        The stream is closed deterministically afterwards (including on
        cancellation) so the underlying async generator's ``finally``
        bookkeeping runs instead of waiting for garbage collection.
        """
        try:
            async for event in stream:
                await ctx.emit(event)
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        """Execute the phase; concrete adapters override this."""
        raise NotImplementedError
