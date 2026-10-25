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

"""Structured streaming events -- the agent-to-gateway delivery contract.

This module defines a typed event vocabulary that names *what happened* during
an agent turn without prescribing *how* it is delivered. The gateway's stream
consumer is the single sink for these events, while the platform adapter decides
how each event is rendered to the end user.

Design constraints:
    * Every event is a plain frozen (immutable) dataclass -- no behavior, no
      platform knowledge, and no I/O. This keeps construction cheap and makes
      the objects safe to pass across thread/async boundaries into the consumer
      queue.
    * Events are intentionally decoupled from the lower-level ``AgentEvent``
      union emitted by the runtime; the gateway translates between the two.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Union


# -- Message (assistant text) events -------------------------------------------


@dataclass(frozen=True)
class MessageChunk:
    """A delta of streamed assistant text.

    ``text`` is the incremental content as it arrives from the model token by
    token. The stream consumer accumulates these chunks and progressively
    renders the growing message to the platform.

    Attributes:
        text: The incremental piece of assistant text for this chunk.
    """

    text: str


@dataclass(frozen=True)
class MessageStop:
    """Marks the end of the current assistant message segment.

    A turn can emit several message segments (e.g. text, then a tool, then more
    text). Each segment boundary produces a ``MessageStop``; only the very last
    one of the whole turn carries ``final=True``.

    Attributes:
        final: True only for the terminal stop of the entire turn, signaling
            the consumer that it may finalize delivery.
    """

    final: bool = False


@dataclass(frozen=True)
class Commentary:
    """A complete interim assistant message emitted between tool iterations.

    Unlike ``MessageChunk`` this is already-complete text rather than an
    incremental delta. The consumer renders it as its own standalone message so
    it reads as a distinct beat (e.g. "Let me check the repository--) before the
    next tool call.

    Attributes:
        text: The full, already-finalized commentary text.
    """

    text: str


# -- Tool-call events ----------------------------------------------------------


@dataclass(frozen=True)
class ToolCallChunk:
    """Signals that a tool invocation started (or changed in-progress state).

    Carries the raw facts about the call -- its name, a short argument preview
    for compact display, and the full arguments dict -- so the gateway can
    decide how much detail to present without re-parsing the call.

    Attributes:
        tool_name: The identifier of the invoked tool.
        preview: A short, human-friendly preview of the arguments, or None.
        args: The complete argument mapping passed to the tool, or None.
        index: The zero-based index of this tool call within the turn, used to
            correlate chunks with their matching ``ToolCallFinished``.
    """

    tool_name: str
    preview: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    index: int = 0


@dataclass(frozen=True)
class ToolCallFinished:
    """Signals that a tool invocation completed.

    Pairs with a preceding ``ToolCallChunk`` of the same ``index`` so the
    gateway can close out any in-progress tool UI.

    Attributes:
        tool_name: The identifier of the finished tool.
        duration: Wall-clock seconds the tool took to run.
        ok: True if the tool returned without raising an exception.
        index: The zero-based index matching the originating ``ToolCallChunk``.
    """

    tool_name: str
    duration: float = 0.0
    ok: bool = True
    index: int = 0


# -- Gateway control / lifecycle events ----------------------------------------


@dataclass(frozen=True)
class LongToolHint:
    """A one-shot onboarding nudge emitted when a tool runs too long.

    Sent a single time per slow tool so the user understands the delay rather
    than assuming the agent is stuck.

    Attributes:
        tool_name: The identifier of the slow-running tool.
        duration: The elapsed seconds that triggered the hint.
    """

    tool_name: str = ""
    duration: float = 0.0


@dataclass(frozen=True)
class GatewayNotice:
    """A gateway-originated control message (restart, online, long-run notice).

    These are emitted by the gateway/infrastructure rather than the agent model.
    ``kind`` is a stable string the adapter can switch on to pick a
    platform-specific presentation; ``text`` is the human-readable default used
    when an adapter has no specialized handling.

    Attributes:
        kind: A stable category string identifying the notice type.
        text: A default human-readable message rendered when no platform-
            specific treatment exists.
        extra: An open dict for kind-specific supplementary data.
    """

    kind: str
    text: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


# -- Union type ----------------------------------------------------------------

StreamEvent = Union[
    MessageChunk,
    MessageStop,
    Commentary,
    ToolCallChunk,
    ToolCallFinished,
    LongToolHint,
    GatewayNotice,
]


__all__ = [
    "MessageChunk",
    "MessageStop",
    "Commentary",
    "ToolCallChunk",
    "ToolCallFinished",
    "LongToolHint",
    "GatewayNotice",
    "StreamEvent",
]
