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

"""The agent loop runner: assembles the loop from mixins and orchestrates phases.

``EncreLoop`` is the heart of the Encre backend: it drives a single
conversational agent turn-by-turn, streaming model output and tool calls to the
frontend while managing an extensive set of cross-cutting concerns. In
addition to calling the model backend it orchestrates:

* **Tool execution** -- discovery, permission gating, safety checks, streaming
  pre-execution, and bounded concurrency.
* **Recovery** -- a unified :class:`~encre.loop_error.ErrorOrchestrator` that
  classifies backend/tool errors and decides retry, fallback, compact, or
  graceful-stop actions.
* **Compaction** -- reactive and background context compaction to stay under
  token budgets, with a frontend refresh flag.
* **Plan mode** -- a "plan-first" workflow where write-class tools are
  previewed and require explicit user approval.
* **Skills & sub-agents** -- dynamic skill activation and delegation to
  specialised sub-agent roles (researcher / executor / critic).
* **Stability** -- token-budget "grace" calls, steer injections, and stuck-loop
  detection.
* **Persistence & observability** -- session metadata, OpenTelemetry tracing,
  and milestone summaries.

The loop is intentionally a thin coordinator; fine-grained behaviour lives in
the phase mixins (``encre.loop.phases.*``), the collaborator wiring
(``encre.loop.wiring``), the control surface (``encre.loop.control``), the
cross-phase helpers (``encre.loop.support``) and the manager objects those
modules construct (``StateManager``, ``CommandManager``, ``PlanModeManager``,
``SkillManager``, ``WorkingSetManager``, ``SubAgentRunner`` and the
``loop_stability`` helpers).  This module keeps only the run orchestration:
the per-turn phase sequence, the control-flow translation (``do_continue`` /
``do_break`` / :class:`~encre.loop.turn_ctx.TurnExit`) and the post-loop
finalisation.
"""

import os
import time
from collections.abc import AsyncGenerator
from typing import Any

from encre.events.lifecycle import (
    CheckpointCreated,
    SessionEnded,
    SessionStarted,
    TurnStarted,
)
from encre.logging_config import get_logger
from encre.loop.control import LoopControlMixin
from encre.loop.phases.compact import PhaseCompactMixin
from encre.loop.phases.end_of_turn import PhaseEndOfTurnMixin
from encre.loop.phases.execute import PhaseExecuteMixin
from encre.loop.phases.model import PhaseModelMixin
from encre.loop.phases.prompt import PhasePromptMixin
from encre.loop.phases.recovery import PhaseRecoveryMixin
from encre.loop.phases.tools import PhaseToolsMixin
from encre.loop.support import LoopSupportMixin
from encre.loop.turn_ctx import TurnContext, TurnExit
from encre.loop.wiring import _LoopWiringMixin
from encre.compact.loop_state.state import LoopState
from encre.compact.loop_state.transition import TurnTransition
from encre.loop_stability import build_standing_orders_reminder
from encre.utils.types import AgentEvent, create_finish

logger = get_logger(__name__)

# Hard safety ceiling for the main session.  Config/workspace may set
# ``max_turns`` freely (0 = unlimited), but a runaway loop -- e.g. one that
# defeats stuck-detection with an alternating or non-erroring no-op tool --
# must never run forever.  This is deliberately very high so legitimate
# long-running sessions are never cut short; it exists only to bound an
# otherwise-unbounded loop.  Env override for operators/CI.
_MAIN_SESSION_HARD_TURN_CAP = int(os.environ.get("ENCRE_MAX_TURNS_HARD_CAP", "100000"))


class EncreLoop(
    _LoopWiringMixin,
    LoopControlMixin,
    LoopSupportMixin,
    PhasePromptMixin,
    PhaseCompactMixin,
    PhaseModelMixin,
    PhaseRecoveryMixin,
    PhaseEndOfTurnMixin,
    PhaseToolsMixin,
    PhaseExecuteMixin,
):
    """Turn-by-turn conversational agent loop with full recovery and tooling.

    ``EncreLoop`` owns a session and a configured backend and runs the agent:
    it builds the prompt context, streams model output and tool calls, executes
    tools with safety/permission gating, and applies recovery/compaction
    strategies when things go wrong. It also exposes the plan-mode, skill, and
    sub-agent surfaces that the desktop UI drives through method calls.

    Rather than implementing every concern inline, the loop composes a set of
    manager objects (state, commands, plan mode, skills, working set, sub-agent
    runner) and a unified error orchestrator. Many public attributes are thin
    properties that forward to those managers so legacy call sites keep working.

    Attributes
    ----------
    config, session:
        The immutable-ish configuration and the persistent session this loop
        drives.
    backend:
        The lazily created model backend (``None`` only before construction in
        ``__init__`` completes).
    _child_loops:
        Set of sub-loop references spawned by this loop, cancelled together when
        the user stops the parent.
    _state:
        Per-run :class:`~encre.loop_state.state.LoopState`, rebuilt each run.
    _error_orch:
        The :class:`~encre.loop_error.ErrorOrchestrator` owning all recovery
        decisions and counters.
    """

    async def run(
        self,
        prompt: str,
        system_prompt: str | None = None,
        custom_instructions: str = "",
        slash_command_mode: str = "",
        slash_commands: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Run a single agent turn and stream its events to the caller.

        Sets this loop as the "active loop" for context-aware tools (so nested
        sub-agents see the correct registry/session/workspace), delegates to
        :meth:`_run_impl`, and -- whether the run finishes normally or is torn
        down by an exception or a hard cancel -- guarantees that any orphaned
        tool-use left by an interrupted tool execution is finalized so the
        persisted session history stays self-consistent.

        Args:
            prompt: The user prompt for this turn.
            system_prompt: Optional override for the system prompt.
            custom_instructions: Extra instructions appended to the context.
            slash_command_mode: Optional initial slash-command mode.
            slash_commands: Optional slash-command definitions.

        Yields:
            :class:`~encre.utils.types.AgentEvent` items (text deltas, tool
            events, finish, etc.).

        Raises:
            Propagates unexpected exceptions after the cleanup ``finally`` block
            has run.
        """
        if self.backend is None:
            logger.warning("Agent run requested but no backend configured")
            yield create_finish("error", error="No backend configured. Send a 'configure' message first.")
            return

        # Mark this loop as the active loop so context-aware tools (find_tool,
        # EncreAgentTool) see the correct discovery/registry/session even when
        # nested inside a sub-agent.
        from encre.tools.runtime import (
            reset_active_loop,
            reset_workspace as reset_bash_workspace,
            set_active_loop,
            set_workspace as set_bash_workspace,
        )
        _loop_token = set_active_loop(self)
        # Inject the workspace path into the bash tool so the Rust
        # sandbox_execute can apply Landlock (Linux) or path isolation
        # (other platforms) automatically.
        _ws = getattr(self.config, "workspace", "") or ""
        _bash_ws_token = set_bash_workspace(_ws if _ws else None)
        # Build the device catalog on first run (lazy init, uses disk cache).
        if self._device_context_manager is not None:
            try:
                await self._device_context_manager.build_catalog()
            except Exception:
                pass
        try:
            async for ev in self._run_impl(
                prompt, system_prompt, custom_instructions,
                slash_command_mode=slash_command_mode,
                slash_commands=slash_commands,
            ):
                yield ev
        finally:
            # Defense layer (exception/asyncio-cancel path): even if the run
            # was torn down by an exception or hard task cancellation -- which
            # skips the normal convergence point in _run_impl -- close any
            # orphan tool_use so the persisted history stays self-consistent.
            # Idempotent with the _run_impl call, so a second pass adds nothing.
            try:
                self._finalize_cancelled_turn()
            except Exception:
                logger.warning("[run] finalize in finally failed", exc_info=True)
            # Persist evolution/learning state so learned experience survives
            # restarts.  Best-effort: never blocks the run teardown.
            for _comp in (self.learner, self.feedback):
                _save = getattr(_comp, "save", None)
                if _save is not None:
                    try:
                        _save()
                    except Exception:
                        pass
            reset_bash_workspace(_bash_ws_token)
            reset_active_loop(_loop_token)

    async def _run_impl(
        self,
        prompt: str,
        system_prompt: str | None = None,
        custom_instructions: str = "",
        slash_command_mode: str = "",
        slash_commands: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Execute one agent turn; the phase-orchestration driver.

        For a single user prompt it resets the per-run recovery/loop state,
        builds the system prompt (prompt phase), then iterates the main turn
        loop through the fixed phase sequence::

            pre-model compact -> model streaming -> post-stream recovery
            -> tool preparation -> tool execution -> secondary tools
            -> post-tool compact -> end-of-turn bookkeeping

        Phases share state through the :class:`~encre.loop.turn_ctx.TurnContext`
        and signal control flow the same way the original monolith did:

        * ``t.do_continue`` -- skip the rest of the turn (mirrors ``continue``);
        * ``t.do_break`` -- leave the turn loop (mirrors ``break``);
        * :class:`TurnExit` -- terminate the whole run immediately (mirrors
          ``return`` from inside the monolithic loop, skipping the post-loop
          finalisation).

        Args:
            prompt: The user prompt for this turn.
            system_prompt: Optional override; when ``None`` the full system
                prompt is built and cached.
            custom_instructions: Extra instructions appended to the context.
            slash_command_mode: Optional initial slash-command mode.
            slash_commands: Optional slash-command definitions.

        Yields:
            :class:`~encre.utils.types.AgentEvent` items.
        """
        t = TurnContext(
            prompt, system_prompt, custom_instructions,
            slash_command_mode, slash_commands,
        )

        # Reset per-run recovery state via the unified orchestrator.
        self._error_orch.reset_for_new_turn()
        # Initialize per-run loop state with transition history tracking.
        self._state = LoopState.create(turn_count=self.session.turn_count)
        # Log effective max_turns so we can diagnose unexpected session stops
        logger.info("[run] _run_impl start turn=%s max_turns=%s backend=%s model=%s",
                    self.session.turn_count, self.config.max_turns,
                    self.config.backend_type, self.config.model)
        # Main session: force unlimited turns so no config/workspace override
        # can cap it.  Sub-agents (depth > 0) keep their own max_turns so they
        # can still terminate naturally via text-only response.
        if self.sub_agent_depth == 0:
            self.config.max_turns = 0
        # Clear any stale cancel/pause state from a previous run so new
        # messages are not immediately rejected after a user cancellation.
        self._cancel_event.clear()
        self._recent_tool_names.clear()
        self._error_tool_names.clear()

        # 鈹€鈹€ Prompt phase: classify intent, build/cache the system prompt,
        # assemble every enrichment block and update the session's system +
        # user messages. 鈹€鈹€
        await self._build_turn_system_prompt(t)

        _t_hook = time.time()
        await self.hook_system.emit_session_start()
        self.event_stream.publish(SessionStarted(session_id=self.session.id or ""))
        logger.info("[run] emit_session_start done (%.2fs)", time.time() - _t_hook)

        # Standing-orders reminder: loaded once per ``run()`` and merged into
        # the LAST user message before the first model call of a fresh user
        # turn (main agent only).  Re-arms the binding pre-action constraints
        # (recall-first, clarify, checkpoint) right where the model begins
        # responding; the full gates still live in the system prompt.
        t.standing_orders_text = (
            build_standing_orders_reminder() if not t.skip_enrichment else ""
        )
        t.standing_orders_injected = False

        # Checkpoint hard-gate state: injected once per run() when the number
        # of consecutive tool-call steps (since the last user message) crosses
        # the threshold AND the user has not explicitly authorised a hands-off
        # run.  The injected message hands control back to the user -- the
        # code-level mirror of Gate 4 in mandatory_constraints.prompt.
        t.checkpoint_injected_this_run = False
        t.checkpoint_text = ""

        # Sanitize session messages on every run -- old sessions loaded from disk
        # may contain broken tool_call groups (from crashes) that cause 400 errors.
        # Only sanitize active branch context; other branches remain untouched.
        active_branch_id = self.session.active_branch_id
        if active_branch_id not in self._sanitized_branches:
            self.session.replace_branch_messages(
                active_branch_id, self.compact_engine.sanitize(t.context_msgs),
            )
            self._sanitized_branches.add(active_branch_id)
            t.context_msgs = self.session.get_context_messages()
        t.active_branch_id = active_branch_id

        while (not self.session.is_max_turns_reached()
               and not self._cancelled()
               and not self._guardrail_halt
               and self.session.turn_count < _MAIN_SESSION_HARD_TURN_CAP):
            t.reset_turn()
            t.turn_start = time.time()
            self._compacted_this_turn = False
            self._streaming_tool_results.clear()
            _t_ts = time.time()
            await self.hook_system.emit_turn_start(self.session.turn_count)
            self.event_stream.publish(TurnStarted(turn=self.session.turn_count))
            logger.info("[run] emit_turn_start done turn=%s (%.2fs)", self.session.turn_count, time.time() - _t_ts)
            _t_ck = time.time()
            self.session.checkpoint(f"turn_{self.session.turn_count}")
            await self.hook_system.emit_checkpoint(f"turn_{self.session.turn_count}")
            self.event_stream.publish(CheckpointCreated(
                turn=self.session.turn_count, label=f"turn_{self.session.turn_count}",
            ))
            logger.info("[run] emit_checkpoint done turn=%s (%.2fs)", self.session.turn_count, time.time() - _t_ck)
            # Emit compact notification if a background compact completed
            if self._compact_notification is not None:
                yield self._compact_notification
                self._compact_notification = None
            # Refresh context at the start of every turn so the model
            # sees its own assistant messages and tool results from
            # previous turns -- without this the context stays frozen on
            # the initial user message, causing repeated tool invocations.
            t.context_msgs = self.session.get_context_messages()

            try:
                # 鈹€鈹€ Phase: pre-API-call compaction (Claude Code style) 鈹€鈹€
                async for _ev in self._phase_pre_model_compact(t):
                    yield _ev

                # 鈹€鈹€ Phase: model streaming + fallback/retry recovery 鈹€鈹€
                async for _ev in self._phase_model(t):
                    yield _ev

                # 鈹€鈹€ Phase: post-stream recovery + assistant materialisation 鈹€鈹€
                async for _ev in self._phase_post_stream(t):
                    yield _ev
                if t.do_continue:
                    t.do_continue = False
                    continue

                # 鈹€鈹€ Phase: prepare + gate the primary tool calls 鈹€鈹€
                async for _ev in self._phase_prepare_tools(t):
                    yield _ev
                if t.do_break:
                    t.do_break = False
                    break

                # 鈹€鈹€ Phase: execute tools (safe parallel / unsafe sequential) 鈹€鈹€
                async for _ev in self._phase_execute_tools(t):
                    yield _ev

                # 鈹€鈹€ Phase: secondary tool calls requested by executed tools 鈹€鈹€
                async for _ev in self._phase_secondary_tools(t):
                    yield _ev

                # 鈹€鈹€ Phase: post-tool compression check 鈹€鈹€
                async for _ev in self._phase_post_tool_compact(t):
                    yield _ev

                # 鈹€鈹€ Phase: end-of-turn bookkeeping (grace call, guardrail,
                # telemetry, evolution, hooks, rollback) 鈹€鈹€
                await self._phase_end_of_turn(t)
                if t.do_break:
                    t.do_break = False
                    break
            except TurnExit:
                # A phase requested the historical ``return``: terminate the
                # whole run immediately, skipping the post-loop finalisation
                # exactly like the original monolithic method returning from
                # inside its main loop.
                return

        # 鈹€鈹€ Defense layer: close any half-finished tool_use block 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
        # A cancel (user pause / abnormal exit) can break the
        # assistant.tool_calls -> tool_result pairing mid-turn: the assistant
        # message was already persisted (line ~3352) declaring N tool calls,
        # but only some got results before the loop broke out. That leaves an
        # orphan tool_use in session state -- which breaks history display,
        # rollback, and the next API call regardless of the sanitize gateway.
        # Fix the session state HERE (the single convergence point after the
        # while loop) so the persisted history is always self-consistent, not
        # just papered over at request time. Sanitize (loop.py:2594) remains
        # as a belt-and-suspenders safety net for any path that slips through.
        self._finalize_cancelled_turn()

        reason = "cancelled" if self._cancelled() else "max_tokens"
        if self._state is not None:
            self._state.transitions.record(
                TurnTransition.CANCELLED if reason == "cancelled" else TurnTransition.MAX_TURNS,
                turn=self.session.turn_count,
                detail=reason,
            )
        logger.warning("[run] session ending turn=%s max_turns=%s reason=%s",
                       self.session.turn_count, self.config.max_turns, reason)
        await self.hook_system.emit_session_end()
        self.event_stream.publish(SessionEnded(
            session_id=self.session.id or "", reason=reason,
        ))
        yield create_finish(
            reason,
            usage=t.last_backend_usage,
            compacted=self._compacted_this_turn,
        )

    async def _run_sub_agent(self, prompt: str,
                              system_prompt: str = "", max_turns: int = 0,
                              model: str = "", api_key: str = "",
                              base_url: str = "",
                              tool_policy: str = "all",
                              progress_callback: Any = None,
                              event_callback: Any = None,
                              session_id: str | None = None,
                              cache_context: Any = None) -> dict[str, Any]:
        return await self._sub_agent_runner.run(
            prompt=prompt,
            system_prompt=system_prompt,
            max_turns=max_turns,
            model=model,
            api_key=api_key,
            base_url=base_url,
            tool_policy=tool_policy,
            progress_callback=progress_callback,
            event_callback=event_callback,
            session_id=session_id,
            cache_context=cache_context,
        )
