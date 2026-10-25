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

"""Encre hook system: event & result types.

This module defines the *vocabulary* of the hook system:

    * :data:`HookEventType` -- the closed set of lifecycle events hooks can
      subscribe to (``pre_tool_exec``, ``post_tool_exec``,
      ``on_session_start``, ``pre_model_request`` ...).
    * :data:`HookHandler` -- the async callable signature each handler must
      match: ``(name, context, state) -> HookResult``.
    * :data:`HookResult` -- the dict a handler returns, optionally carrying
      ``block`` / ``block_reason`` / ``modified_input`` / ``extra_context``.
    * :class:`HookStartedEvent`, :class:`HookProgressEvent`,
      :class:`HookResponseEvent` -- observer event records emitted by
      :class:`~encre.hooks.system.EncreHookSystem` for the UI / logging.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

HookEventType = Literal[
    "pre_tool_exec",
    "post_tool_exec",
    "on_session_start",
    "on_session_end",
    "on_turn_start",
    "on_turn_end",
    "on_error",
    "on_permission_request",
    "on_permission_response",
    "pre_compact",
    "post_compact",
    "pre_model_request",
    "post_model_response",
    "on_tool_progress",
    "on_backend_error",
    "on_rate_limit",
    "on_checkpoint",
    "pre_sub_agent",
    "post_sub_agent",
    "on_goal_progress",
    "on_telemetry",
    "pre_bash",
    "on_file_change",
    "on_user_message_persisted",
]

HookResult = dict[str, Any]
HookHandler = Callable[[str, dict[str, Any], dict[str, Any] | None], Awaitable[HookResult]]


@dataclass
class HookStartedEvent:
    """Observer record emitted just before a hook handler runs."""

    hook_id: str
    hook_name: str
    event_type: HookEventType
    timestamp: float = field(default_factory=__import__('time').time)


@dataclass
class HookProgressEvent:
    """Observer record carrying a handler's streamed progress output.

    Captures the handler's combined output plus its raw stdout/stderr so the UI
    or logs can show what the hook produced while running.
    """

    hook_id: str
    hook_name: str
    event_type: HookEventType
    output: str
    stdout: str = ""
    stderr: str = ""


@dataclass
class HookResponseEvent:
    """Observer record emitted after a hook handler completes.

    Carries the handler's returned output, an exit code (0 = success) and a
    coarse ``outcome`` string ("success" / "error") for quick filtering.
    """

    hook_id: str
    hook_name: str
    event_type: HookEventType
    output: str
    exit_code: int = 0
    outcome: str = "success"
