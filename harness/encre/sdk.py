#!/usr/bin/env python3

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

"""The Encre embedding SDK: one object that owns a fully wired agent runtime.

`EncreAgent` is the desktop/gateway entry point; it also loads plugins, MCP
servers, soul/profile memory, slash commands and the learning engine.  An
*embedding* caller -- a script, a CI job, the headless JSONL transport, a
notebook -- wants the agent runtime without that surface, and without having
to know that a working loop needs a dozen collaborators wired in the right
order.

:class:`EncreSDK` is that surface.  It owns an
:class:`~encre.session.EncreSession` and a
:class:`~encre.tools.registry.ToolRegistry`, builds the
:class:`~encre.loop.runner.EncreLoop` around them (forwarding every
collaborator it was given, so nothing is constructed twice), and exposes
:meth:`EncreSDK.run` as an async stream of
:data:`~encre.utils.types.AgentEvent`.

Collaborators are *injected*, never mocked: a caller that wants a real
memory store passes a real one, and a caller that wants none gets the loop's
own default.  Duplicating the loop's constructor defaults here would be a
second source of truth, so this module deliberately forwards ``None`` and
lets the wiring decide.

Typical use::

    config = EncreConfig(model="gpt-5.6", backend_type="openai")
    sdk = EncreSDK(EncreSession(config))
    try:
        async for event in sdk.run("summarise this repo"):
            ...
    finally:
        await sdk.aclose()

For a batch caller that only wants the final answer, :meth:`EncreSDK.collect`
drains the same stream into a :class:`SDKRunResult`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable
from dataclasses import dataclass, field
from typing import Any

from encre.config import EncreConfig
from encre.events import EventSourcedBus
from encre.logging_config import get_logger
from encre.loop import EncreLoop
from encre.modes.mode_profiles import AgentMode
from encre.session import EncreSession
from encre.tools.base import EncreTool
from encre.tools.registry import ToolRegistry
from encre.utils.types import (
    AgentEvent,
    BackendError,
    Finish,
    TextDelta,
    ThinkingDelta,
    ToolCallStart,
)

logger = get_logger(__name__)


@dataclass
class SDKRunResult:
    """The drained outcome of one :meth:`EncreSDK.collect` call.

    Attributes:
        text: The assistant's streamed text, concatenated.
        thinking: The streamed reasoning text, concatenated.
        finish_reason: The ``Finish`` reason (``"end_turn"``, ``"error"``,
            ``"cancelled"``, ...); empty when the run yielded no ``Finish``.
        usage: Token usage reported by the backend, when available.
        tool_calls: Tool names invoked during the run, in call order.
        error: The backend error message when the run failed, else ``None``.
        compacted: Whether the context was compacted during the run.
    """

    text: str = ""
    thinking: str = ""
    finish_reason: str = ""
    usage: dict[str, Any] | None = None
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None
    compacted: bool = False


def _coerce_tool_registry(
    tools: ToolRegistry | Iterable[EncreTool] | None,
) -> ToolRegistry:
    """Normalise the ``tools`` argument to a :class:`ToolRegistry`.

    Args:
        tools: An existing registry (used as-is), any iterable of
            :class:`~encre.tools.base.EncreTool` (registered in order), or
            ``None`` for an empty registry.

    Returns:
        The registry to hand to the loop.

    Raises:
        TypeError: If ``tools`` is neither a registry, an iterable of tools,
            nor ``None``.
    """
    if tools is None:
        return ToolRegistry()
    if isinstance(tools, ToolRegistry):
        return tools
    if isinstance(tools, Iterable):
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        return registry
    raise TypeError(
        "tools must be a ToolRegistry, an iterable of EncreTool, or None; "
        f"got {type(tools).__name__}"
    )


class EncreSDK:
    """A fully wired agent runtime with a minimal embedding surface.

    Attributes:
        session: The session this runtime drives.
        config: The session's configuration (the loop reads it live, so
            mutating it between runs is honoured).
        tool_registry: The tools the model may call.
        loop: The assembled :class:`~encre.loop.runner.EncreLoop`.
    """

    def __init__(
        self,
        session: EncreSession,
        tools: ToolRegistry | Iterable[EncreTool] | None = None,
        *,
        mode: AgentMode | str | None = None,
        register_builtins: bool = True,
        hook_system: Any = None,
        safety: Any = None,
        memory_system: Any = None,
        profile_system: Any = None,
        soul_system: Any = None,
        skill_registry: Any = None,
        telemetry: Any = None,
        evolution: Any = None,
        recovery: Any = None,
        feedback: Any = None,
    ) -> None:
        """Assemble the runtime around ``session``.

        Every collaborator keyword is forwarded verbatim to
        :class:`~encre.loop.runner.EncreLoop`; passing ``None`` (the default)
        leaves that collaborator to the loop's own default rather than
        substituting a stub, so the SDK never becomes a second source of
        wiring defaults.

        Args:
            session: The session to drive.  Owns the configuration.
            tools: A tool registry, an iterable of tools, or ``None``.
            mode: The capability profile to run under; defaults to the
                loop's ``GENERAL``.
            register_builtins: Register the harness's six builtin tools
                (file read/write/edit, bash, web fetch/search) into the
                registry.  The builtin set is part of the harness, not a
                plugin, so this defaults to ``True``.
            hook_system: Optional hook system.
            safety: Optional safety engine.
            memory_system: Optional memory system.
            profile_system: Optional profile system.
            soul_system: Optional soul/persona system.
            skill_registry: Optional skill registry.
            telemetry: Optional telemetry sink.
            evolution: Optional evolution configuration.
            recovery: Optional error recovery engine.
            feedback: Optional feedback learner.

        Raises:
            TypeError: If ``tools`` is of an unsupported type.
        """
        self.session = session
        self.config: EncreConfig = session.config
        self.tool_registry = _coerce_tool_registry(tools)
        self.loop = EncreLoop(
            self.config,
            session,
            self.tool_registry,
            mode=mode,
            hook_system=hook_system,
            safety=safety,
            memory_system=memory_system,
            profile_system=profile_system,
            soul_system=soul_system,
            skill_registry=skill_registry,
            telemetry=telemetry,
            evolution=evolution,
            recovery=recovery,
            feedback=feedback,
        )
        if register_builtins:
            # The six builtin tools are harness primitives, so an embedded
            # runtime registers them the same way the desktop agent does.
            from encre.tools.builtin import register_builtin_tools

            register_builtin_tools(self.tool_registry)
        # Tools that delegate (sub-agent spawning, manage) resolve the loop
        # through this module-global; without it they would see no parent.
        from encre.tools.runtime import set_parent_loop

        set_parent_loop(self.loop)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: EncreConfig | None = None,
        tools: ToolRegistry | Iterable[EncreTool] | None = None,
        **kwargs: Any,
    ) -> EncreSDK:
        """Build an SDK around a fresh session for ``config``.

        Args:
            config: The configuration to run under; a default
                :class:`~encre.config.EncreConfig` is created when omitted.
            tools: Same as :meth:`__init__`.
            **kwargs: Forwarded to :meth:`__init__`.

        Returns:
            A ready-to-run :class:`EncreSDK`.
        """
        resolved = config or EncreConfig()
        return cls(EncreSession(resolved), tools, **kwargs)

    # ------------------------------------------------------------------
    # Event backbone
    # ------------------------------------------------------------------

    @property
    def event_bus(self) -> EventSourcedBus:
        """The loop's single append-only event backbone.

        Returns:
            The :class:`~encre.events.EventSourcedBus` the loop publishes to.
        """
        return self.loop.event_bus

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def run(
        self,
        prompt: str,
        system_prompt: str | None = None,
        *,
        custom_instructions: str = "",
        slash_command_mode: str | None = None,
        slash_commands: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Stream one agent turn.

        Returns the loop's own async generator rather than wrapping it, so
        consumer-side cancellation and backpressure propagate exactly as they
        do for the desktop transport.

        Args:
            prompt: The user prompt for this turn.
            system_prompt: Optional system-prompt override.
            custom_instructions: Extra instructions appended to the context.
            slash_command_mode: Initial slash-command mode; defaults to the
                configuration's current mode.
            slash_commands: Slash-command definitions offered to the model.

        Returns:
            An async iterator of :data:`~encre.utils.types.AgentEvent`.
        """
        mode = (
            slash_command_mode
            if slash_command_mode is not None
            else getattr(self.config, "slash_command_mode", "") or ""
        )
        return self.loop.run(
            prompt,
            system_prompt,
            custom_instructions=custom_instructions,
            slash_command_mode=mode,
            slash_commands=slash_commands,
        )

    async def collect(
        self,
        prompt: str,
        system_prompt: str | None = None,
        *,
        custom_instructions: str = "",
        slash_command_mode: str | None = None,
        slash_commands: list[dict[str, Any]] | None = None,
    ) -> SDKRunResult:
        """Drain one turn into a :class:`SDKRunResult`.

        Intended for batch callers (scripts, CI) that want the answer rather
        than the stream.  The underlying events are not retained, so this is
        memory-safe on long turns.

        Args:
            prompt: The user prompt for this turn.
            system_prompt: Optional system-prompt override.
            custom_instructions: Extra instructions appended to the context.
            slash_command_mode: Initial slash-command mode.
            slash_commands: Slash-command definitions offered to the model.

        Returns:
            The drained :class:`SDKRunResult`.
        """
        text: list[str] = []
        thinking: list[str] = []
        tool_calls: list[str] = []
        result = SDKRunResult()

        async for event in self.run(
            prompt,
            system_prompt,
            custom_instructions=custom_instructions,
            slash_command_mode=slash_command_mode,
            slash_commands=slash_commands,
        ):
            if isinstance(event, TextDelta):
                text.append(event.text)
            elif isinstance(event, ThinkingDelta):
                thinking.append(event.text)
            elif isinstance(event, ToolCallStart):
                tool_calls.append(event.name)
            elif isinstance(event, BackendError):
                result.error = event.error
            elif isinstance(event, Finish):
                result.finish_reason = str(event.reason)
                result.usage = event.usage
                result.compacted = event.compacted
                if event.error:
                    result.error = event.error

        result.text = "".join(text)
        result.thinking = "".join(thinking)
        result.tool_calls = tool_calls
        return result

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Ask the running turn to stop at its next checkpoint.

        Cancels the whole agent tree: sub-agent loops spawned by this loop are
        cancelled too, so one call stops everything.

        Returns:
            None.
        """
        self.loop.cancel()

    async def aclose(self) -> None:
        """Release the backend's resources (HTTP clients, model memory, ...).

        Safe to call more than once.  Collaborators injected by the caller
        (memory stores, telemetry sinks) are owned by the caller and are
        deliberately not closed here.

        Returns:
            None.
        """
        await self.loop.aclose()


__all__ = [
    "EncreSDK",
    "SDKRunResult",
]
