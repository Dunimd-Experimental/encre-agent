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

"""Kernel ports: the only seam the turn engine may depend on.

The loop is the kernel of the harness.  Historically it imported concrete
subsystems directly (``encre.agent`` for registries, ``encre.improve`` for
feedback, ``encre.memdir`` for memory, ``encre.device`` for context), which
made the kernel impossible to embed, test in isolation, or reason about.

A *port* is a structural contract (``typing.Protocol``) owned by the kernel.
Adapters live in the feature packages and are injected at wiring time.  The
rule is one-directional: **features import ports, the kernel imports only
ports and contracts**.  No adapter import may appear under ``encre.loop``.

These protocols are intentionally narrow.  They describe the minimum a
collaborator must do to be usable by the kernel; richer capability interfaces
remain on the concrete classes.  ``runtime_checkable`` allows adapters to be
validated at wiring time without nominal inheritance.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from encre.contracts.decisions import PolicyDecision
from encre.contracts.events import EventEnvelope
from encre.contracts.tools import ToolSpec

__all__ = [
    "ClockPort",
    "ContextPort",
    "EventPort",
    "MemoryPort",
    "ModelPort",
    "PolicyPort",
    "StorePort",
    "ToolPort",
]


@runtime_checkable
class ModelPort(Protocol):
    """A streaming model backend."""

    def stream(self, request: Any) -> AsyncIterator[Any]:
        """Yield model events for ``request`` (deltas, tool calls, usage)."""
        ...

    def context_window_size(self) -> int:
        """Return the model's context window in tokens."""
        ...

    def supports_tool_calling(self) -> bool:
        """Return whether the model can emit tool calls."""
        ...


@runtime_checkable
class ToolPort(Protocol):
    """A tool executor plus its catalogue."""

    def list_specs(self) -> list[ToolSpec]:
        """Return every registered tool's contract."""
        ...

    async def execute(self, call: Any, *, timeout_s: float | None = None) -> Any:
        """Execute one tool call and return its result."""
        ...


@runtime_checkable
class ContextPort(Protocol):
    """The single context assembler."""

    def build(self, request: Any) -> Any:
        """Assemble the model-facing context for ``request``."""
        ...


@runtime_checkable
class MemoryPort(Protocol):
    """Recall and persistence of long-term memory."""

    async def recall(self, query: str, *, limit: int = 10) -> list[Any]:
        """Return the most relevant memory entries for ``query``."""
        ...

    async def remember(self, entry: Any) -> str:
        """Persist ``entry`` and return its id."""
        ...


@runtime_checkable
class EventPort(Protocol):
    """The append-only event sink."""

    def publish(self, envelope: EventEnvelope) -> None:
        """Append ``envelope`` to the backbone."""
        ...


@runtime_checkable
class PolicyPort(Protocol):
    """The single policy decision point."""

    def decide(self, action: str, *, context: dict[str, Any] | None = None) -> PolicyDecision:
        """Decide whether ``action`` may proceed."""
        ...


@runtime_checkable
class StorePort(Protocol):
    """Byte-oriented persistence used by sessions and checkpoints."""

    async def read(self, key: str) -> bytes | None:
        """Return the stored bytes for ``key``, or ``None``."""
        ...

    async def write(self, key: str, data: bytes) -> None:
        """Store ``data`` under ``key``."""
        ...


@runtime_checkable
class ClockPort(Protocol):
    """Injectable time source (makes timeout/retry tests deterministic)."""

    def now(self) -> float:
        """Return the current monotonic time in seconds."""
        ...
