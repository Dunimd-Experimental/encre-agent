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

"""Tests for the pluggable phase pipeline (P1.1).

The loop under test is the real ``EncreLoop`` -- genuine wiring, genuine
session, genuine event backbone, genuine phase adapters -- with only the
model backend swapped for a scripted ``BaseBackend`` subclass that streams a
canned text-only response through the real async-generator streaming path.
The data root is redirected to ``tmp_path`` via ``ENCRE_DATA_DIR`` so no
session, job or telemetry state can leak into the real workspace.

The suite checks five invariants: the default wiring order, same-name
in-place replacement, safe and reversible unregistration, mode-based
pipeline filtering, and a minimal pipeline completing one real turn.
"""

from unittest.mock import patch

import pytest

from encre.backends.base import BaseBackend
from encre.config import EncreConfig
from encre.loop import EncreLoop
from encre.loop.phase_api import (
    LoopPhase,
    PhaseContext,
    PhaseProtocol,
    PhaseResult,
    PhaseScope,
)
from encre.loop.phase_registry import PhaseRegistry
from encre.loop.phases import (
    EndOfTurnPhase,
    ExecuteToolsPhase,
    ModelPhase,
    PostStreamPhase,
    PreModelCompactPhase,
    PrepareToolsPhase,
    SystemPromptPhase,
)
from encre.modes.mode_profiles import AgentMode
from encre.session import EncreSession
from encre.utils.types import BackendFinish, BackendText, Finish

# The exact call order the runner used to hard-code in ``_run_impl``.
_EXPECTED_ORDER = [
    "system_prompt",
    "session_start",
    "pre_model_compact",
    "model",
    "post_stream",
    "prepare_tools",
    "execute_tools",
    "secondary_tools",
    "post_tool_compact",
    "end_of_turn",
]

_PROMPT = "Summarise the pluggable phase pipeline in one paragraph."

# Deliberately > 300 chars so the stub-response detector never flags the
# canned response and the turn completes through the normal text-only path.
_SCRIPTED_TEXT = (
    "The phase pipeline is the load-bearing seam of the agent loop: every "
    "concern the former monolith expressed inline now lives behind a small "
    "protocol, and the runner only iterates the registry snapshot taken at "
    "the start of each run. Run-scoped phases execute once before the turn "
    "loop, turn-scoped phases execute on every iteration, and the control "
    "flow actions each phase returns encode the historical continue, break "
    "and TurnExit branches. This scripted response is deliberately long so "
    "the stub detector never mistakes it for an acknowledgement without "
    "action, and so the turn completes through the normal text-only path."
)


class _ScriptedBackend(BaseBackend):
    """Real ``BaseBackend`` subclass streaming one canned text-only response.

    Subclassing :class:`~encre.backends.base.BaseBackend` (rather than
    patching with a ``MagicMock``) keeps the model phase on the genuine
    streaming path: it consumes an async generator of backend events exactly
    as it does with a live provider.
    """

    def __init__(self, text: str) -> None:
        """Store the canned response text and reset the call counter."""
        self._text = text
        self.chat_calls = 0

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = True,
        enable_caching: bool = False,
        cache_edits_state=None,
    ):
        """Stream the canned text, then a clean ``stop`` finish event.

        The single ``yield`` pair mirrors the shape of a live provider: one
        text chunk followed by a terminal finish carrying usage numbers.
        Request arguments are accepted and ignored so the loop can pass its
        full parameter set unchanged.
        """
        self.chat_calls += 1
        yield BackendText(text=self._text)
        yield BackendFinish(
            reason="stop", usage={"input_tokens": 120, "output_tokens": 80},
        )

    def supports_tool_calling(self) -> bool:
        """Report tool calling as available so the tools phases stay live."""
        return True

    def context_window_size(self) -> int:
        """Advertise a 128k window so no compaction threshold is tripped."""
        return 128000

    def supports_prompt_caching(self) -> bool:
        """Disable prompt caching to match the fixture's config flag."""
        return False


@pytest.fixture
def loop(tmp_path, monkeypatch) -> EncreLoop:
    """A real loop: scripted backend, data root isolated to ``tmp_path``."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    config = EncreConfig(
        model="scripted-model",
        backend_type="openai",
        permission_mode="default",
        max_turns=10,
        max_tokens=4096,
        log_level="ERROR",
        enable_prompt_caching=False,
    )
    session = EncreSession(config)
    with patch("encre.loop.create_backend") as mock_create_backend:
        mock_create_backend.return_value = _ScriptedBackend(_SCRIPTED_TEXT)
        return EncreLoop(config=config, session=session)


async def _run_turn(loop: EncreLoop, prompt: str = _PROMPT) -> list:
    """Drain one full ``loop.run()`` and return the streamed events."""
    events: list = []
    async for event in loop.run(prompt):
        events.append(event)
    return events


def _finishes(events: list) -> list[Finish]:
    """Filter the streamed events down to their terminal ``Finish`` events."""
    return [event for event in events if isinstance(event, Finish)]


def _streamed_text(events: list) -> str:
    """Concatenate every ``text`` attribute across the streamed events."""
    return "".join(e.text for e in events if hasattr(e, "text"))


class _MarkerPhase:
    """Minimal structural phase (satisfies ``PhaseProtocol``) for registry
    tests that need custom ``modes``."""

    def __init__(self, name: str, modes: frozenset | None = None) -> None:
        """Record the phase name, its optional mode restriction and its scope."""
        self.name = name
        self.modes = modes
        self.scope = PhaseScope.TURN

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        """Always continue; the phase exists only to be counted and filtered."""
        return PhaseResult.continue_()


class _RecordingSwap(LoopPhase):
    """Swap phase: records its own execution, then delegates to the built-in
    phase it replaced, so the turn must still complete with identical
    semantics."""

    def __init__(self, loop, name: str, inner, calls: list) -> None:
        """Bind the replaced built-in phase and the list that records calls."""
        super().__init__(loop)
        self.name = name
        self._inner = inner
        self._calls = calls

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        """Record that this swap ran, then forward to the built-in phase."""
        self._calls.append(self.name)
        return await self._inner.run(ctx)


# ----------------------------------------------------------------------
# 1 -- default wiring: the exact historical order, all active in GENERAL
# ----------------------------------------------------------------------


def test_default_general_phase_order(loop):
    """The wiring registers the built-ins in the hard-coded call order."""
    assert loop.phase_registry.names() == _EXPECTED_ORDER
    assert [p.name for p in loop.phase_registry.pipeline(AgentMode.GENERAL)] \
        == _EXPECTED_ORDER
    # Every built-in phase is mode-agnostic (modes=None).
    assert all(p.modes is None for p in loop.phase_registry.pipeline(AgentMode.GENERAL))


# ----------------------------------------------------------------------
# 2 -- same-name registration swaps the built-in in place
# ----------------------------------------------------------------------


async def test_same_name_registration_swaps_builtin_in_place(loop):
    """Re-registering 'post_stream' replaces the built-in at its position and
    the custom phase really executes during a live turn."""
    registry = loop.phase_registry
    builtin = registry.get("post_stream")
    calls: list[str] = []
    swap = _RecordingSwap(loop, "post_stream", builtin, calls)
    registry.register(swap)

    # The swap keeps the pipeline position (names unchanged) and get()
    # returns the replacement.
    assert registry.names() == _EXPECTED_ORDER
    assert registry.get("post_stream") is swap

    events = await _run_turn(loop)
    # The custom phase really executed (the swap took effect) ...
    assert calls == ["post_stream"]
    # ... and delegation preserved the turn semantics: clean stop finish with
    # the scripted text streamed through the real model phase.
    assert [f.reason for f in _finishes(events)] == ["stop"]
    assert _SCRIPTED_TEXT in _streamed_text(events)
    assert loop.backend.chat_calls == 1


# ----------------------------------------------------------------------
# 3 -- unregistering an optional phase: loop still completes a turn
# ----------------------------------------------------------------------


async def test_unregister_optional_phase_still_completes_turn(loop):
    """Removing 'secondary_tools' is reversible and the loop completes a full
    turn without it."""
    registry = loop.phase_registry
    secondary = registry.get("secondary_tools")
    assert registry.unregister("secondary_tools") is True
    assert registry.unregister("secondary_tools") is False  # safe no-op
    assert "secondary_tools" not in registry.names()

    events = await _run_turn(loop)
    assert [f.reason for f in _finishes(events)] == ["stop"]
    assert loop.backend.chat_calls == 1

    # Removal is reversible: re-registering restores the same phase object.
    registry.register(secondary)
    assert registry.get("secondary_tools") is secondary
    assert "secondary_tools" in registry.names()


# ----------------------------------------------------------------------
# 4 -- pipeline(mode) filters by the phase's modes frozenset
# ----------------------------------------------------------------------


def test_pipeline_filters_by_mode():
    """Only phases whose ``modes`` contains the mode (or ``modes is None``)
    are returned, in registration order."""
    registry = PhaseRegistry()
    everywhere = _MarkerPhase("everywhere")
    general_only = _MarkerPhase("general_only", modes=frozenset({AgentMode.GENERAL}))
    workspace_only = _MarkerPhase("workspace_only", modes=frozenset({AgentMode.WORKSPACE}))
    for phase in (everywhere, general_only, workspace_only):
        registry.register(phase)

    # Structural conformance of a plain class to the protocol.
    assert isinstance(everywhere, PhaseProtocol)

    assert [p.name for p in registry.pipeline(AgentMode.GENERAL)] \
        == ["everywhere", "general_only"]
    assert [p.name for p in registry.pipeline(AgentMode.WORKSPACE)] \
        == ["everywhere", "workspace_only"]
    assert [p.name for p in registry.pipeline(AgentMode.AUTOMATION)] == ["everywhere"]
    assert [p.name for p in registry.pipeline(None)] == ["everywhere"]
    assert registry.names() == ["everywhere", "general_only", "workspace_only"]


# ----------------------------------------------------------------------
# 5 -- minimal pipeline completes a real turn end-to-end
# ----------------------------------------------------------------------


async def test_minimal_pipeline_completes_a_turn(loop):
    """A fresh registry holding only the core phases drives one complete
    turn: prompt -> compaction -> model -> recovery -> tools -> end-of-turn."""
    registry = PhaseRegistry()
    for phase in (
        SystemPromptPhase(loop),
        PreModelCompactPhase(loop),
        ModelPhase(loop),
        PostStreamPhase(loop),
        PrepareToolsPhase(loop),
        ExecuteToolsPhase(loop),
        EndOfTurnPhase(loop),
    ):
        registry.register(phase)
    loop.phase_registry = registry

    events = await _run_turn(loop)
    finishes = _finishes(events)
    assert [f.reason for f in finishes] == ["stop"]
    # The text streamed through the real model phase over the scripted
    # backend, exactly one model call for the text-only turn.
    assert _SCRIPTED_TEXT in _streamed_text(events)
    assert loop.backend.chat_calls == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-header"]))
