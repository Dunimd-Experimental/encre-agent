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

"""The agent loop runner: assembles the loop from mixins and drives the
pluggable phase pipeline.

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
the phase implementations (``encre.loop.phases.*``), the collaborator wiring
(``encre.loop.wiring``), the control surface (``encre.loop.control``), the
cross-phase helpers (``encre.loop.support``) and the manager objects those
modules construct (``StateManager``, ``CommandManager``, ``PlanModeManager``,
``SkillManager``, ``WorkingSetManager``, ``SubAgentRunner`` and the
``loop_stability`` helpers).  This module keeps only the run orchestration:
the pluggable phase pipeline driver -- the loop iterates the
:class:`~encre.loop.phase_registry.PhaseRegistry` built by the wiring and
dispatches on the :class:`~encre.loop.phase_api.PhaseResult` each phase
returns -- plus the turn-lifecycle bookkeeping (turn start events,
checkpoints) and the post-loop finalisation.
"""

import asyncio
import contextlib
import json
import os
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from encre.compact.loop_state.state import LoopTurnState
from encre.compact.loop_state.transition import TurnTransition
from encre.events.lifecycle import (
    CheckpointCreated,
    SessionEnded,
    TurnStarted,
)
from encre.logging_config import get_logger
from encre.loop.control import LoopControlMixin
from encre.loop.phase_api import (
    PhaseAction,
    PhaseContext,
    PhaseOutcome,
    PhaseResult,
    PhaseScope,
)
from encre.loop.phase_registry import PhaseRegistry
from encre.loop.phases.compact import PhaseCompactMixin
from encre.loop.phases.end_of_turn import PhaseEndOfTurnMixin
from encre.loop.phases.execute import PhaseExecuteMixin
from encre.loop.phases.model import PhaseModelMixin
from encre.loop.phases.prompt import PhasePromptMixin
from encre.loop.phases.recovery import PhaseRecoveryMixin
from encre.loop.phases.tools import PhaseToolsMixin
from encre.loop.state import (
    PHASE_COMPLETED_EVENT_TYPE,
    TURN_COMPLETED_EVENT_TYPE,
)
from encre.loop.state import (
    LoopState as LoopRunState,
)
from encre.loop.support import LoopSupportMixin
from encre.loop.turn_ctx import TurnContext, TurnExit
from encre.loop.wiring import _LoopWiringMixin
from encre.utils.types import AgentEvent, create_finish, create_system_message

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
        Per-run :class:`~encre.compact.loop_state.state.LoopTurnState`, rebuilt each run.
    _error_orch:
        The :class:`~encre.loop_error.ErrorOrchestrator` owning all recovery
        decisions and counters.
    """

    def current_context_usage(self) -> dict[str, Any] | None:
        """Recompute the per-request context composition on demand.

        Protocol-layer resume pushes use this when neither the loop cache
        nor the session-persisted breakdown is available (e.g. sessions
        last active before breakdowns started being recorded).  It mirrors
        the prompt-phase measurement for the resting session: the frozen
        system prompt, the active tool payload and the current context
        messages, with the static skill catalogue standing in for
        activation-specific skill prompts.  The next finished turn
        overwrites the estimate with the exact in-request breakdown.
        """
        try:
            from encre.context_compiler import compute_context_usage
            from encre.prompts.system import skills_block

            system_prompt = getattr(self, "_frozen_system_prompt", None) or ""
            tools = None
            if self.backend is not None and self.backend.supports_tool_calling():
                try:
                    tools = self.discovery.get_active_tools_payload(
                        self.session.id, fmt="openai", profile=self._model_profile()
                    )
                except Exception:
                    tools = None
            try:
                _catalogue = skills_block(self._render_skill_catalogue())
                skills_text = _catalogue.content or ""
            except Exception:
                skills_text = ""
            window = 0
            try:
                window = int(self.backend.context_window_size() or 0)
            except Exception:
                window = 0
            return compute_context_usage(
                system_prompt=system_prompt,
                messages=self.session.get_context_messages(),
                tools=tools,
                model=self.config.model,
                context_window=window,
                skills_text=skills_text,
            )
        except Exception:
            logger.debug("[loop] on-demand context usage recompute failed", exc_info=True)
            return None

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

        # Mark this loop as the active loop so context-aware tools (manage,
        # EncreAgentTool) see the correct discovery/registry/session even when
        # nested inside a sub-agent.
        from encre.tools.runtime import (
            reset_active_loop,
            set_active_loop,
        )
        from encre.tools.runtime import (
            reset_workspace as reset_bash_workspace,
        )
        from encre.tools.runtime import (
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
            with contextlib.suppress(Exception):
                await self._device_context_manager.build_catalog()
        try:
            async for ev in self._run_impl(
                prompt,
                system_prompt,
                custom_instructions,
                slash_command_mode=slash_command_mode,
                slash_commands=slash_commands,
            ):
                yield ev
        except GeneratorExit:
            # Consumer stopped pulling after the final event (normal stream
            # close): not a termination cause worth attributing.
            raise
        except BaseException as exc:
            # Abnormal teardown (exception or hard task cancellation): record
            # the failure attribution before propagating.
            self._record_session_end(exc=exc)
            raise
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
                    with contextlib.suppress(Exception):
                        _save()
            # Loop runtime state convergence on abnormal teardown (exception
            # or hard cancel): a normal completion already returned to idle
            # in _run_impl, so this is a no-op on the happy path.  Any stale
            # waiting_*/compacting/running state collapses back to idle.
            _sm = getattr(self, "_loop_sm", None)
            if _sm is not None and _sm.state not in (
                LoopRunState.IDLE,
                LoopRunState.STOPPED,
            ):
                self._transition_loop(LoopRunState.IDLE, {"reason": "teardown"})
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
        """Execute one agent turn; the pluggable phase-pipeline driver.

        For a single user prompt it resets the per-run recovery/loop state,
        then runs the phases registered on ``self.phase_registry``:

        * the **run-scoped** phases execute once, before the turn loop
          (``system_prompt`` -> ``session_start``);
        * every turn iteration then executes the **turn-scoped** pipeline in
          registration order (``pre_model_compact`` -> ``model`` ->
          ``post_stream`` -> ``prepare_tools`` -> ``execute_tools`` ->
          ``secondary_tools`` -> ``post_tool_compact`` -> ``end_of_turn``).

        Phases share state through the :class:`~encre.loop.turn_ctx.TurnContext`
        and return a :class:`~encre.loop.phase_api.PhaseResult` the runner
        dispatches on.  The actions encode the exact control-flow branches
        the original monolith expressed inline:

        * ``END_TURN`` -- skip the remaining phases, start the next turn
          iteration (mirrors ``continue``);
        * ``END_RUN`` -- skip the remaining phases and leave the turn loop
          (mirrors ``break``);
        * ``EXIT`` -- terminate the whole run immediately (mirrors
          :class:`~encre.loop.turn_ctx.TurnExit`: ``return`` from inside the
          monolithic loop, skipping the post-loop finalisation).

        The pipeline is snapshotted when the run starts, so registry
        mutations take effect on the next ``run()``.

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
            prompt,
            system_prompt,
            custom_instructions,
            slash_command_mode,
            slash_commands,
        )

        # Reset per-run recovery state via the unified orchestrator.
        self._error_orch.reset_for_new_turn()
        # Per-run idempotence flag for the terminal ``session_ended``
        # attribution event (see ``_record_session_end``).
        self._session_end_recorded = False
        # Initialize per-run loop state with transition history tracking.
        self._state = LoopTurnState.create(
            turn_count=self.session.turn_count,
            event_bus=self.event_bus,
            session_id=self.session.id or "",
        )
        # Log effective max_turns so we can diagnose unexpected session stops
        logger.info(
            f"[run] _run_impl start turn={self.session.turn_count} "
            f"max_turns={self.config.max_turns} "
            f"backend={self.config.backend_type} model={self.config.model}"
        )
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
        # Reset per-run verification budgets (verify-on-stop nudges, forced
        # reviews, hard-gate escalations): they are bounded per run, not for
        # the lifetime of the loop object.
        self._verify_on_stop_nudges = 0
        self._forced_review_count = 0
        self._verify_hard_gate_count = 0

        # Snapshot the pipeline once per run: registry mutations made while a
        # run is in flight take effect on the next run, never mid-turn.
        registry = self.phase_registry or PhaseRegistry()
        pipeline = registry.pipeline(self.mode)
        run_phases = [p for p in pipeline if p.scope is PhaseScope.RUN]
        turn_phases = [p for p in pipeline if p.scope is PhaseScope.TURN]

        # ── Run-scoped phases: prompt build, session start ──
        _outcome = PhaseOutcome()
        async for _ev in self._drive_phases(run_phases, t, _outcome):
            yield _ev
        if _outcome.action is PhaseAction.EXIT:
            self._record_session_end(hint="run_exit")
            return

        while (
            not self.session.is_max_turns_reached()
            and not self._cancelled()
            and not self._guardrail_halt
            and self.session.turn_count < _MAIN_SESSION_HARD_TURN_CAP
        ):
            # Loop runtime state: a turn start puts the loop back into
            # "running" (a no-op when consecutive turns follow each other).
            self._transition_loop(
                LoopRunState.RUNNING,
                {"turn": self.session.turn_count},
            )
            t.reset_turn()
            t.turn_start = time.time()
            self._compacted_this_turn = False
            self._streaming_tool_results.clear()
            self._streaming_tool_clients.clear()
            # Per-turn recovery bookkeeping: the retry budget is turn-scoped
            # (see RetryBudget) and write tracking drives the post-tools
            # checkpoint recorded after the pipeline below.
            self._writes_this_turn = False
            self.recovery_engine.budget.reset_turn()
            # Fallback bookkeeping is turn-scoped too: models that failed
            # earlier turns must be eligible again.  Clearing here (not only
            # after a clean turn) matters because a turn that ends via EXIT
            # -- e.g. every pool model failing and the turn releasing with an
            # error card -- used to skip the post-turn clear and keep the
            # set poisoned, so every later turn resolved ZERO fallback
            # candidates and the pool silently stopped cycling.
            self._fallback_tried.clear()
            _t_ts = time.time()
            await self.hook_system.emit_turn_start(self.session.turn_count)
            self.event_stream.publish(TurnStarted(turn=self.session.turn_count))
            logger.info(f"[run] emit_turn_start done turn={self.session.turn_count} ({time.time() - _t_ts:.2f}s)")
            _t_ck = time.time()
            self.session.checkpoint(f"turn_{self.session.turn_count}")
            await self.hook_system.emit_checkpoint(f"turn_{self.session.turn_count}")
            self.event_stream.publish(
                CheckpointCreated(
                    turn=self.session.turn_count,
                    label=f"turn_{self.session.turn_count}",
                )
            )
            logger.info(f"[run] emit_checkpoint done turn={self.session.turn_count} ({time.time() - _t_ck:.2f}s)")
            # Emit compact notification if a background compact completed
            if self._compact_notification is not None:
                yield self._compact_notification
                self._compact_notification = None
            # Steering inbox (turn boundary): deliver queued user messages
            # BEFORE the context snapshot below so they flow into this
            # turn's prompt as regular user messages.
            self._inject_drained_messages()
            # Refresh context at the start of every turn so the model
            # sees its own assistant messages and tool results from
            # previous turns -- without this the context stays frozen on
            # the initial user message, causing repeated tool invocations.
            t.context_msgs = self.session.get_context_messages()

            # ── Turn-scoped pipeline: compact -> model -> recovery -> tools
            # -> execute -> secondary tools -> compact -> end-of-turn ──
            _outcome = PhaseOutcome()
            async for _ev in self._drive_phases(turn_phases, t, _outcome):
                yield _ev
            # Phase journal: the turn pipeline has fully unwound.  Recorded
            # for every non-crash path (including error exits and user
            # stops) -- from the journal's point of view the turn is over;
            # only a hard process death leaves the last turn open.
            self._loop_sm.record(
                TURN_COMPLETED_EVENT_TYPE,
                {"turn": self.session.turn_count},
            )
            # Post-tools checkpoint: when this turn performed write-tool
            # side effects, persist one explicit checkpoint (at most once
            # per turn) so a later crash can roll back to a known-good
            # state that includes this turn's writes.
            if self._writes_this_turn:
                self._writes_this_turn = False
                _pt_label = f"post_tools_{self.session.turn_count}"
                self.session.checkpoint(_pt_label)
                await self.hook_system.emit_checkpoint(_pt_label)
                self.event_stream.publish(
                    CheckpointCreated(
                        turn=self.session.turn_count,
                        label=_pt_label,
                    )
                )
            if _outcome.action is PhaseAction.END_TURN:
                # A phase requested the historical ``continue``: skip the
                # remaining phases and start the next turn iteration.
                continue
            if _outcome.action is PhaseAction.END_RUN:
                # A phase requested the historical ``break``: leave the turn
                # loop and run the post-loop finalisation below.
                break
            if _outcome.action is PhaseAction.EXIT:
                # A phase raised the historical ``TurnExit``: terminate the
                # whole run immediately, skipping the post-loop finalisation
                # exactly like the original monolithic method returning from
                # inside its monolithic loop.  Covers the normal stop, the
                # withheld-error stop and the interrupt paths; attribute the
                # end cause before returning.
                _exit_hint = "error" if getattr(t, "error_consumed", False) else (getattr(t, "slot_finish_reason", "") or "turn_exit")
                self._record_session_end(hint=_exit_hint)
                return

            # ``_fallback_tried`` is cleared at TURN START (see the per-turn
            # reset block above) -- that covers abnormal exits too, so no
            # post-turn clearing happens here.

        # ── Defense layer: close any half-finished tool_use block ────────
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
        if self._pending_review_notice is not None:
            # The review died with the turn -- tell the client, or its card
            # keeps offering a decision nobody is waiting for.
            notice = self._pending_review_notice
            self._pending_review_notice = None
            yield create_system_message(
                f"__plan_review__:{json.dumps(notice, ensure_ascii=False)}",
                kind="plan",
            )

        reason = "cancelled" if self._cancelled() else "max_tokens"
        # Failure attribution for this termination path (turn-limit / cancel
        # / guardrail halt / tool-gating break).
        self._record_session_end(hint=reason)
        # Loop runtime state: the run ended (normally / cancelled / turn
        # cap) -- back to idle.  ``stopped`` is reserved for real session
        # termination and is intentionally not produced here.
        self._transition_loop(LoopRunState.IDLE, {"reason": reason})
        if self._state is not None:
            self._state.transitions.record(
                TurnTransition.CANCELLED if reason == "cancelled" else TurnTransition.MAX_TURNS,
                turn=self.session.turn_count,
                detail=reason,
            )
        logger.warning(f"[run] session ending turn={self.session.turn_count} max_turns={self.config.max_turns} reason={reason}")
        await self.hook_system.emit_session_end()
        self.event_stream.publish(
            SessionEnded(
                session_id=self.session.id or "",
                reason=reason,
            )
        )
        # Cache the last computed breakdown on the loop so protocol-layer
        # pushes that run outside the turn (resume, system messages) can
        # still attach the composition to their context_usage frames.  The
        # session-metadata copy also survives restarts, so reopening a
        # session shows its last real composition immediately.
        self.last_context_usage = t.context_usage
        if t.context_usage:
            self.session.metadata["context_usage"] = t.context_usage
        yield create_finish(
            reason,
            usage=t.last_backend_usage,
            context_usage=t.context_usage,
            compacted=self._compacted_this_turn,
        )

    # -- Session end attribution ---------------------------------------------

    @staticmethod
    def _short_detail(text: Any, limit: int = 200) -> str:
        """Collapse *text* to a bounded single-line detail string."""
        if not text:
            return ""
        collapsed = " ".join(str(text).split())
        return collapsed[:limit]

    def _record_session_end(
        self,
        *,
        exc: BaseException | None = None,
        hint: str = "",
    ) -> None:
        """Attribute the session termination and persist it exactly once.

        Converges every termination path (normal stop, turn/cap limit,
        guardrail halt, user abort, released error, hard exception) into one
        writer.  Two artefacts are produced:

        * a ``session_ended`` event on the loop event stream
          (``events.jsonl``) with payload
          ``{reason, detail, tool?, error_class?, failed_tool_count?}``;
        * an ``end_reason`` entry in the session's ``meta.json`` metadata
          (written through the session's existing meta writer, only when the
          session directory already exists).

        Reason derivation priority: context overflow (error classification
        of the terminating exception) -> user abort (cancel event /
        ``CancelledError``) -> guardrail halt (repetitive tool loop) ->
        max turns -> last failed tool -> exception class -> normal stop
        hint.  Must never raise: attribution failures are logged and
        swallowed.
        """
        if getattr(self, "_session_end_recorded", False):
            return
        self._session_end_recorded = True
        payload: dict[str, Any] = {"reason": "", "detail": ""}
        try:
            detail_text: Any = hint
            overflow = False
            if exc is not None:
                detail_text = exc
                if isinstance(exc, Exception):
                    try:
                        from encre.recovery_loop import is_context_overflow

                        overflow = is_context_overflow(exc)
                    except Exception:
                        overflow = False
            if "overflow" in (hint or "").lower():
                overflow = True
            if overflow:
                payload["reason"] = "context_overflow"
                payload["detail"] = self._short_detail(detail_text) or "context overflow"
            elif (exc is not None and isinstance(exc, asyncio.CancelledError)) or (exc is None and self._cancelled()):
                payload["reason"] = "user_abort"
                payload["detail"] = "cancelled by user"
            elif self._guardrail_halt:
                payload["reason"] = "tool_failure"
                payload["detail"] = "guardrail halt: repetitive tool loop"
            elif self.session.is_max_turns_reached() or self.session.turn_count >= _MAIN_SESSION_HARD_TURN_CAP:
                payload["reason"] = "max_turns"
                payload["detail"] = f"turn limit reached (turns={self.session.turn_count})"
            else:
                failed = [tc for tc in self.telemetry.tool_calls if not tc.success] if self.telemetry is not None else []
                if failed:
                    payload["reason"] = "tool_failure"
                    payload["tool"] = failed[-1].tool_name
                    payload["failed_tool_count"] = len(failed)
                    payload["detail"] = self._short_detail(failed[-1].error_message) or "last tool call failed"
                elif exc is not None:
                    payload["reason"] = "model_error"
                    payload["detail"] = self._short_detail(detail_text)
                    try:
                        from encre.backends.retry import classify_error

                        payload["error_class"] = classify_error(exc).value
                    except Exception:
                        payload["error_class"] = type(exc).__name__
                elif hint == "stop" and not self._recent_run_error():
                    payload["reason"] = "completed"
                    payload["detail"] = "run finished normally"
                else:
                    payload["reason"] = "model_error"
                    payload["detail"] = self._short_detail(hint) or "run ended"
        except Exception:
            logger.debug("[run] session end attribution failed", exc_info=True)
            return
        # Persist 1: event stream entry (never breaks the loop).
        try:
            state_sm = getattr(self, "_loop_sm", None)
            if state_sm is not None:
                state_sm.record("session_ended", payload)
        except Exception:
            logger.debug("[run] session_ended event failed", exc_info=True)
        # Persist 2: end_reason in meta.json via the session's own writer.
        try:
            meta_dir = Path(
                "data",
                "sessions",
                getattr(self.session, "id", "") or "unknown",
            )
            if meta_dir.is_dir():
                self.session.metadata["end_reason"] = payload
                self.session._save_meta_file(meta_dir)
        except Exception:
            logger.debug("[run] end_reason meta write failed", exc_info=True)

    def _recent_run_error(self) -> bool:
        """Return True when a recent persisted message carries an error.

        Used to distinguish a normal stop from a released backend error
        (which stores ``errorMessage``/``errorCode`` on the assistant
        message before exiting).
        """
        try:
            for msg in self.session.messages[-5:]:
                if isinstance(msg, dict) and (msg.get("errorMessage") or msg.get("errorCode")):
                    return True
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
        return False

    async def _drive_phases(
        self,
        phases: list[Any],
        t: TurnContext,
        outcome: PhaseOutcome,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Run ``phases`` sequentially while streaming their events.

        Each phase runs to completion inside a worker task while this
        generator streams the events the phase emits to the caller.  The
        rendezvous queue (``maxsize=1``) keeps the pull semantics of the
        original inline ``async for ... yield`` sequence: the pipeline runs
        at most one event ahead of the consumer, so a slow or paused
        consumer (plan approval, backpressured websocket) suspends the
        pipeline exactly as the old generator-based phases did.

        Control flow: the first phase that returns a non-``CONTINUE``
        :class:`~encre.loop.phase_api.PhaseResult` ends the pipeline and its
        action is recorded in ``outcome``.  A phase raising
        :class:`~encre.loop.turn_ctx.TurnExit` is translated into
        :attr:`PhaseAction.EXIT`; any other exception is re-raised to the
        caller **after** the events already emitted have been drained, so
        the consumer never loses events that were produced before the
        failure (identical to the old inline sequence).  When the consumer
        abandons or is cancelled mid-stream, the worker task is cancelled
        and reaped -- no orphan task outlives the pipeline.

        Args:
            phases: The snapshot of the pipeline to execute, in order.
            t: The shared turn context handed to every phase.
            outcome: Mutable accumulator receiving the final action/reason.

        Yields:
            The :class:`~encre.utils.types.AgentEvent` items the phases emit.
        """
        # Allow the model phase to run ahead of the consumer.  With the
        # transport writer decoupled, dispatch is a cheap enqueue; a deep
        # queue absorbs snapshot/context work between events instead of
        # stalling token generation on it.
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)
        errors: list[BaseException] = []

        async def _worker() -> None:
            try:
                for phase in phases:
                    ctx = PhaseContext(loop=self, turn=t, sink=queue.put)
                    result: PhaseResult = await phase.run(ctx)
                    # Phase journal: persist that this phase ran to
                    # completion so a crash mid-turn is detectable on
                    # restart (see StateEventLog.last_incomplete_turn).
                    self._loop_sm.record(
                        PHASE_COMPLETED_EVENT_TYPE,
                        {
                            "turn": self.session.turn_count,
                            "phase": getattr(phase, "name", "") or type(phase).__name__,
                        },
                    )
                    if result.action is not PhaseAction.CONTINUE:
                        outcome.action = result.action
                        outcome.reason = result.reason
                        return
            except TurnExit:
                # Historical monolith ``return``: stop everything, skip the
                # post-loop finalisation -- expressed as the EXIT action.
                outcome.action = PhaseAction.EXIT
            except BaseException as exc:
                # Re-raised on the caller side after the queue is drained.
                errors.append(exc)

        worker = asyncio.create_task(_worker())
        get_op: asyncio.Future[Any] | None = None
        try:
            while True:
                if queue.empty() and worker.done():
                    break
                if get_op is None:
                    get_op = asyncio.ensure_future(queue.get())
                _done, _ = await asyncio.wait(
                    {get_op, worker},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if get_op in _done:
                    yield get_op.result()
                    get_op = None
        finally:
            if get_op is not None and not get_op.done():
                get_op.cancel()
            if not worker.done():
                worker.cancel()
            # Reap the worker so nothing outlives the pipeline; its exception
            # (if any) is re-raised below on the normal completion path.
            with contextlib.suppress(BaseException):
                await worker
        if errors:
            raise errors[0]

    async def _run_sub_agent(
        self,
        prompt: str,
        system_prompt: str = "",
        max_turns: int = 0,
        model: str = "",
        api_key: str = "",
        base_url: str = "",
        tool_policy: str = "all",
        progress_callback: Any = None,
        event_callback: Any = None,
        session_id: str | None = None,
        cache_context: Any = None,
        permission_mode: str = "",
        model_index: int | None = None,
    ) -> dict[str, Any]:
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
            permission_mode=permission_mode,
            model_index=model_index,
        )
