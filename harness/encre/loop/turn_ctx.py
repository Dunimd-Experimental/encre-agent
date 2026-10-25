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

"""Per-run turn context shared by the loop phases.

Splitting the monolithic ``_run_impl`` body into phase methods requires the
former method locals to be reachable across phases.  :class:`TurnContext`
carries exactly those values: run-scoped parameters (prompt, mode flags,
run-scoped injections) and turn-scoped streaming state (text/tool-call
buffers, backend messages, usage).  Phase methods read and mutate the
context instead of passing dozens of positional arguments.

:class:`TurnExit` reproduces the historical ``return`` statements of the
monolith: a phase raises it to terminate the whole run immediately,
skipping the post-loop finalisation exactly like the original ``return``.
"""

from typing import Any
from uuid import uuid4


class TurnExit(Exception):
    """Signal the run loop to stop immediately (mirrors ``return``).

    Raising this from a phase skips the post-while finalisation of
    ``_run_impl`` -- identical to the original monolithic method returning
    from inside its main loop.
    """


class TurnContext:
    """Mutable state shared by all phases of one ``run()`` invocation.

    Run-scoped fields are set once by ``_run_impl`` before the main loop;
    turn-scoped fields are reset every turn via :meth:`reset_turn`.
    """

    __slots__ = (
        # ── Run-scoped parameters ──
        "prompt",
        "system_prompt",
        "custom_instructions",
        "slash_command_mode",
        "slash_commands",
        "intents",
        "register",
        "skip_enrichment",
        "skill_prompt",
        "standing_orders_text",
        "standing_orders_injected",
        "checkpoint_injected_this_run",
        "checkpoint_text",
        "active_branch_id",
        "last_backend_usage",
        "context_usage",
        "tool_id_token",
        # ── Turn-scoped state ──
        "context_msgs",
        "window",
        "est_tokens",
        "tools",
        "backend_tools",
        "backend_messages",
        "pre_model",
        "model_params_override",
        "steer_msgs",
        "steer_text",
        "injected_system_entries",
        "slot_budget",
        "context_window",
        "thinking_prefill",
        "request_overhead_tokens",
        "text_parts",
        "thinking_parts",
        "thinking_blocks",
        "_sig_buf",
        "tool_call_buffers",
        "extra_thinking",
        "extra_text",
        "extra_buffers",
        "tool_seen",
        "in_extra",
        "think_buf",
        "in_think",
        "backend_usage",
        "slot_finish_reason",
        "error_consumed",
        "llm_span",
        "response_text",
        "turn_events",
        "turn_start",
        "assistant_content",
        "assistant_tool_calls",
        "prepared",
        "spec_approved",
        # ── Control flow flags ──
        "do_continue",
        "do_break",
    )

    def __init__(
        self,
        prompt: str,
        system_prompt: str | None,
        custom_instructions: str,
        slash_command_mode: str,
        slash_commands: list[dict[str, Any]] | None,
    ) -> None:
        # ── Run-scoped parameters ──
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.custom_instructions = custom_instructions
        self.slash_command_mode = slash_command_mode
        self.slash_commands = slash_commands
        self.intents: list[str] = []
        # Response register for this run: chat / answer / work.  Defaults to
        # "work" so a path that never classifies still runs under the strictest
        # (delivering) behavioural frame rather than the conversational one.
        self.register: str = "work"
        self.skip_enrichment = False
        self.skill_prompt = ""
        # Per-request context-usage breakdown (system prompt / messages /
        # tools by connector kind / skills), computed by the prompt phase
        # after every compile.  Run-scoped: each prompt-phase run overwrites.
        self.context_usage: dict[str, Any] | None = None
        self.standing_orders_text = ""
        self.standing_orders_injected = False
        self.checkpoint_injected_this_run = False
        self.checkpoint_text = ""
        self.active_branch_id = ""
        self.last_backend_usage: dict[str, Any] | None = None
        # Opaque per-run token embedded in the client-facing tool-call ids
        # (``call_{token}_{idx}``).  A fresh run gets a fresh token, so ids can
        # never repeat across user turns -- unlike the old ``turn_count`` base,
        # which resets on session reload and is recomputed by message edits.
        # It rotates once per *completed* turn (:meth:`rotate_tool_id_token`),
        # which keeps a retried/discarded attempt inside one turn on its
        # original id instead of orphaning the card the deltas already drew.
        self.tool_id_token = uuid4().hex[:10]
        # ── Turn-scoped state ──
        self.reset_turn()

    def rotate_tool_id_token(self) -> None:
        """Start a new tool-call id namespace (called when a turn completes)."""
        self.tool_id_token = uuid4().hex[:10]

    def reset_turn(self) -> None:
        """Reset every turn-scoped field (called at the top of each turn)."""
        self.context_msgs: list[dict[str, Any]] = []
        self.window = 0
        self.est_tokens = 0
        self.tools: list[dict[str, Any]] | None = None
        self.backend_tools: list[dict[str, Any]] | None = None
        self.backend_messages: list[dict[str, Any]] = []
        self.pre_model: dict[str, Any] | None = None
        # Plugin model-request engineering: temperature/max_tokens/tool_choice
        # overrides harvested from the pre_model_request hook (turn-scoped).
        self.model_params_override: dict[str, Any] | None = None
        self.steer_msgs: list[str] = []
        self.steer_text = ""
        self.injected_system_entries: list[dict[str, str]] = []
        self.slot_budget = 0
        self.context_window = 0
        self.request_overhead_tokens = 0
        self.thinking_prefill = ""
        self.text_parts: list[str] = []
        self.thinking_parts: list[str] = []
        # Block-level thinking capture for native-thinking protocols
        # (Anthropic Messages API, Gemini): each entry is one thinking
        # block as the server emitted it, signature included.  The plain
        # ``thinking_parts`` concatenation loses block boundaries, and the
        # signature is required verbatim when the block is replayed on a
        # later request -- a mismatched or missing signature is a 400.
        self.thinking_blocks: list[dict[str, str]] = []
        self._sig_buf: str = ""
        self.tool_call_buffers: dict[int, dict[str, Any]] = {}
        self.extra_thinking: list[str] = []
        self.extra_text: list[str] = []
        self.extra_buffers: dict[int, dict[str, Any]] = {}
        self.tool_seen = False
        self.in_extra = False
        self.think_buf = ""
        self.in_think = False
        self.backend_usage: dict[str, Any] | None = None
        self.slot_finish_reason = "stop"
        self.error_consumed = False
        self.llm_span: Any = None
        self.response_text = ""
        self.turn_events = 0
        self.turn_start = 0.0
        self.assistant_content = ""
        self.assistant_tool_calls: list[dict[str, Any]] = []
        self.prepared: list[dict[str, Any]] = []
        self.spec_approved = True
        # ── Control flow flags ──
        self.do_continue = False
        self.do_break = False
