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

"""Recovery phase: post-stream decisions, text-only finish gates, turn tail.

Extracted verbatim from the original monolithic ``_run_impl``.  Contains
``_phase_post_stream`` -- the unified error orchestrator's post-stream
decision (slot escalation, max-tokens recovery, empty/truncated retry),
the text-only finish path (spec/plan review output, anti-stub retry,
auto-continue, verify-on-stop, forced review, hard verification gate,
Plan-Do-Review advancement) and the assistant message materialisation
(OpenAI-format tool_calls + segments) ahead of the tool phase.

The turn tail lives in :mod:`encre.loop.phases.end_of_turn`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from encre.compact.loop_state.transition import TurnTransition
from encre.features import adapters as _features
from encre.logging_config import get_logger
from encre.loop.phase_api import LoopPhase, PhaseContext, PhaseResult
from encre.loop.phases.end_of_turn import _cleanup_terminal_sessions
from encre.loop.turn_ctx import TurnContext, TurnExit
from encre.loop_error import PostStreamAction
from encre.loop_stability import (
    MAX_DELIBERATION_RETRIES,
    MAX_STUB_RESPONSE_RETRIES,
    BudgetState,
    adjudicate_deliberation,
    build_auto_continue_message,
    build_deliberation_retry_message,
    build_empty_retry_message,
    build_stub_retry_message,
    build_tombstone_messages,
    build_truncated_retry_message,
    is_deliberation_loop,
    is_empty_response,
    is_stub_response,
    is_truncated_tool_call,
)
from encre.recovery_loop import (
    ESCALATED_MAX_TOKENS,
    MAX_OUTPUT_TOKENS_RECOVERY_LIMIT,
    build_max_tokens_recovery_message,
    build_slot_escalation_message,
)
from encre.utils.types import create_finish

logger = get_logger(__name__)


class PhaseRecoveryMixin:
    """Post-stream recovery and turn finalisation."""

    async def _phase_post_stream(self, t: TurnContext) -> AsyncGenerator:
        """Handle the post-stream orchestrator decision and text-only exits.

        Args:
            t: The shared turn context.

        Yields:
            Recovery/system/finish events; may raise :class:`TurnExit` to
            terminate the run (mirrors the original ``return`` statements).
        """
        post_decision = self._error_orch.handle_post_stream(
            finish_reason=t.slot_finish_reason,
            is_empty=is_empty_response(t.text_parts, t.tool_call_buffers, t.thinking_parts) if not t.error_consumed else False,
            is_truncated=bool(t.tool_call_buffers) and is_truncated_tool_call(t.tool_call_buffers),
            tool_call_buffers=t.tool_call_buffers,
            text_parts=t.text_parts,
            thinking_parts=t.thinking_parts,
            config=self.config,
            session=self.session,
            turn_count=self.session.turn_count,
        )

        if t.error_consumed:
            yield create_finish("stop")
            raise TurnExit()

        if post_decision.action == PostStreamAction.CONTINUE:
            if post_decision.detail == "slot escalation":
                self._error_orch._slot_escalated = True
                if self._state is not None:
                    self._state.transitions.record(
                        TurnTransition.SLOT_ESCALATION,
                        turn=self.session.turn_count,
                        detail=f"{self.config.default_slot_tokens} -> {self.config.max_tokens}",
                    )
                logger.info("[slot] escalating from %d -> %d turn=%d",
                            self.config.default_slot_tokens, self.config.max_tokens, self.session.turn_count)
                # Internal loop-control nudge: goes into the session context
                # (the model must see it) but never onto the wire -- a yielded
                # SystemMessage renders as a client-facing bubble.
                self.session.add_message(
                    "system", build_slot_escalation_message(),
                    is_synthetic=True,
                )
            elif "max_tokens" in post_decision.detail:
                self._max_output_tokens_override = ESCALATED_MAX_TOKENS
                if t.tool_call_buffers:
                    _tombstones = build_tombstone_messages(
                        t.tool_call_buffers,
                        "max_tokens truncation",
                        reasoning_content="".join(t.thinking_parts),
                    )
                    for _ts in _tombstones:
                        self.session.add_message(_ts["role"], _ts.get("content", ""),
                            tool_call_id=_ts.get("tool_call_id"), name=_ts.get("name"),
                            is_error=_ts.get("is_error", False),
                            tool_calls=_ts.get("tool_calls"),
                            reasoning_content=_ts.get("reasoning_content"))
                    t.tool_call_buffers.clear()
                if self._state is not None:
                    self._state.transitions.record(
                        TurnTransition.MAX_OUTPUT_TOKENS,
                        turn=self.session.turn_count,
                        detail=post_decision.detail,
                    )
                logger.info("[max_tokens] recovery %s turn=%d", post_decision.detail, self.session.turn_count)
                # Same as slot escalation: model-context-only nudge, never
                # rendered in the client timeline.
                self.session.add_message(
                    "system", build_max_tokens_recovery_message(),
                    is_synthetic=True,
                )
            elif "empty" in post_decision.detail:
                if self._state is not None:
                    self._state.transitions.record(
                        TurnTransition.EMPTY_RESPONSE,
                        turn=self.session.turn_count,
                        detail=post_decision.detail,
                    )
                logger.warning("[run] empty response, %s turn=%d", post_decision.detail, self.session.turn_count)
                retry_count = self._error_orch._empty_response_retry_count
                self.session.add_message(
                    "system", build_empty_retry_message(retry_count),
                    is_synthetic=True,
                )
            elif "truncated" in post_decision.detail:
                _first_tc = next(iter(t.tool_call_buffers.values()))
                _args_preview = str(_first_tc.get("arguments", ""))[:200]
                _tc_name = _first_tc.get("name", "unknown")
                if self._state is not None:
                    self._state.transitions.record(
                        TurnTransition.TRUNCATED_TOOL_CALL,
                        turn=self.session.turn_count,
                        detail=post_decision.detail + f" tool={_tc_name}",
                    )
                logger.warning("[run] truncated tool call '%s', %s turn=%d",
                               _tc_name, post_decision.detail, self.session.turn_count)
                t.tool_call_buffers.clear()
                self.session.add_message(
                    "system", build_truncated_retry_message(_tc_name, _args_preview),
                    is_synthetic=True,
                )
            t.do_continue = True
            return

        if ((post_decision.action == PostStreamAction.STOP or
             (t.slot_finish_reason in ("max_tokens", "length") and
              not self._error_orch._slot_escalated and
              self._error_orch._max_output_tokens_recovery_count >= MAX_OUTPUT_TOKENS_RECOVERY_LIMIT)) and
            is_empty_response(t.text_parts, t.tool_call_buffers, t.thinking_parts)):
            # Empty response exhausted
            logger.warning("[run] empty response retries exhausted turn=%d", self.session.turn_count)
            self.session.add_message("assistant",
                "(No response generated. Please try rephrasing your request.)")
            yield create_finish("stop")
            raise TurnExit()

        # Reset stub counter when the model actually does work (calls tools)
        if t.tool_call_buffers:
            self._stub_response_retry_count = 0
            self._auto_continue_consecutive = 0
            self._auto_continue_last_output = 0

        if t.text_parts and not t.tool_call_buffers:
            full_text = "".join(t.text_parts)

            # Merge into the previous assistant that had tool_calls, so that
            # tool-calling turns don't create a second assistant message in
            # the session.  Scan backwards -- if we find an assistant with
            # tool_calls before any user message, it belongs to the same
            # logical response from the user's perspective.
            merged = False
            for i in range(len(self.session.messages) - 1, -1, -1):
                m = self.session.messages[i]
                if m.get("role") == "user":
                    break
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    existing = m.get("content") or ""
                    m["content"] = (existing + "\n\n" + full_text) if existing else full_text
                    if t.thinking_parts:
                        existing_r = m.get("reasoning_content", "") or ""
                        m["reasoning_content"] = existing_r + "".join(t.thinking_parts)
                    if t.thinking_blocks:
                        # Keep block signatures replayable across the merge
                        # (see the block-level note in the commit path).
                        existing_blocks = list(m.get("reasoning_blocks") or [])
                        m["reasoning_blocks"] = existing_blocks + [
                            b for b in t.thinking_blocks if b.get("text") or b.get("signature")
                        ]
                    if t.backend_usage:
                        m["usage"] = t.backend_usage
                    # Preserve segment ordering
                    new_segs = []
                    if t.thinking_parts:
                        new_segs.append({"kind": "thinking", "text": "".join(t.thinking_parts)})
                    if full_text:
                        new_segs.append({"kind": "text", "text": full_text})
                    if new_segs:
                        existing_segs = m.get("segments", [])
                        m["segments"] = existing_segs + new_segs
                    self.session.mark_messages_dirty()
                    merged = True
                    self._clear_pending_stream()
                    break

            if not merged and not (t.slash_command_mode in ("spec", "plan") and full_text.strip()):
                txt_kwargs: dict[str, Any] = {}
                if t.thinking_parts:
                    txt_kwargs["reasoning_content"] = "".join(t.thinking_parts)
                if t.thinking_blocks:
                    txt_kwargs["reasoning_blocks"] = [
                        b for b in t.thinking_blocks if b.get("text") or b.get("signature")
                    ]
                if t.backend_usage:
                    txt_kwargs["usage"] = t.backend_usage
                segs = []
                if t.thinking_parts:
                    segs.append({"kind": "thinking", "text": "".join(t.thinking_parts)})
                if full_text:
                    segs.append({"kind": "text", "text": full_text})
                if segs:
                    txt_kwargs["segments"] = segs
                self.session.add_message("assistant", full_text, **txt_kwargs)
                self._clear_pending_stream()

            await self.hook_system.emit_session_end()
            await _cleanup_terminal_sessions()
            logger.debug("Agent finished (text-only response, %s chars)", len(full_text))


            # ── Anti-stub: if the model returned a brief acknowledgment
            # without any tool calls or prior tool work, nudge it to
            # actually act.  This catches the #1 task-delivery failure
            # mode: "I'll help you with that." / "Done!" with no work.
            # Only triggers when there was no prior tool work to merge
            # into (``merged`` is False) and not in plan/spec mode
            # (where text-only IS the expected deliverable).
            _is_stub = (
                not merged
                and t.slash_command_mode not in ("plan", "spec")
                and not t.checkpoint_injected_this_run
                and is_stub_response(t.text_parts, t.tool_call_buffers, t.thinking_parts, t.prompt)
            )
            if _is_stub:
                _stub_count = getattr(self, "_stub_response_retry_count", 0)
                if _stub_count < MAX_STUB_RESPONSE_RETRIES:
                    self._stub_response_retry_count = _stub_count + 1
                    self.session.add_message(
                        "system", build_stub_retry_message(_stub_count + 1),
                        is_synthetic=True,
                    )
                    if self._state is not None:
                        self._state.transitions.record(
                            TurnTransition.STUB_RESPONSE,
                            turn=self.session.turn_count,
                            detail=f"retry={_stub_count + 1}/{MAX_STUB_RESPONSE_RETRIES}",
                        )
                    logger.info(
                        "[run] stub response detected, retrying turn=%d retry=%d/%d",
                        self.session.turn_count, _stub_count + 1, MAX_STUB_RESPONSE_RETRIES,
                    )
                    t.do_continue = True
                    return
            else:
                # Reset stub counter on any non-stub response so the
                # next task starts fresh.
                self._stub_response_retry_count = 0

            # ── Anti-deliberation: the mirror failure of a stub.  The
            # model produced a very long internal monologue (possibly
            # thinking-only) that re-decides the same question over and
            # over, with no tool calls and no committed answer.  Nudge it
            # to either act or ask one concise question.  Same gating as
            # the stub check (no merge, no plan/spec mode, no checkpoint).
            if not _is_stub and not merged and t.slash_command_mode not in ("plan", "spec") and not t.checkpoint_injected_this_run:
                if is_deliberation_loop(t.text_parts, t.tool_call_buffers, t.thinking_parts):
                    # Confirm the heuristic with a cheap LLM adjudication
                    # call so verbose legitimate answers are not nudged.
                    _adjudicated_loop = await adjudicate_deliberation(
                        self.backend, t.text_parts, t.thinking_parts,
                    )
                    if not _adjudicated_loop:
                        logger.info(
                            "[run] deliberation heuristic overruled by LLM adjudication turn=%d",
                            self.session.turn_count,
                        )
                    else:
                        _dl_count = getattr(self, "_deliberation_retry_count", 0)
                        if _dl_count < MAX_DELIBERATION_RETRIES:
                            self._deliberation_retry_count = _dl_count + 1
                            self.session.add_message(
                                "system", build_deliberation_retry_message(_dl_count + 1),
                                is_synthetic=True,
                            )
                            if self._state is not None:
                                self._state.transitions.record(
                                    TurnTransition.STUB_RESPONSE,
                                    turn=self.session.turn_count,
                                    detail=f"deliberation retry={_dl_count + 1}/{MAX_DELIBERATION_RETRIES}",
                                )
                            logger.info(
                                "[run] deliberation loop detected, retrying turn=%d retry=%d/%d",
                                self.session.turn_count, _dl_count + 1, MAX_DELIBERATION_RETRIES,
                            )
                            t.do_continue = True
                            return
                else:
                    # Reset deliberation counter on any committed response.
                    self._deliberation_retry_count = 0

            # ── Auto-continue: when budget remains, nudge the model to
            # keep going instead of stopping early.  Mirrors Claude Code's
            # token-budget auto-continue (query/tokenBudget.ts).
            #
            # Token budget is a *resource* signal; the Plan-Do-Review engine
            # carries the *task* signal.  Both must agree before the loop
            # keeps spending: a completed plan never triggers another
            # auto-continue, and a dead plan (every remaining step
            # FAILED/BLOCKED) ends the run with a truthful blocked report
            # instead of burning budget against an unrunnable plan.
            _pdr_plan_complete = (
                self._pdr_active and self._pdr.plan.is_complete
            )
            _auto_continue = False
            _progress_context = ""
            if (
                self._profile.auto_continue
                and self.config.token_budget > 0
                and not t.tool_call_buffers
                and not t.checkpoint_injected_this_run
                and t.backend_usage
                and not _pdr_plan_complete
            ):
                self._budget_state.add_usage(
                    t.backend_usage.get("output_tokens", 0)
                    + t.backend_usage.get("input_tokens", 0)
                )
                self.session.metadata[BudgetState.META_KEY] = self._budget_state.checkpoint()
                # Dead-plan gate: with an active plan whose remaining steps
                # are all FAILED/BLOCKED there is nothing runnable left --
                # continuing would only re-read the same dead plan.
                if self._pdr_active:
                    _plan = self._pdr.plan
                    _open_steps = [
                        s for s in _plan.steps
                        if s.status in (
                            _features.StepStatus.PENDING,
                            _features.StepStatus.IN_PROGRESS,
                        )
                    ]
                    if not _open_steps:
                        _stuck = [
                            f"{s.id}: {s.description[:120]} ({s.status.name})"
                            for s in _plan.steps
                            if s.status in (
                                _features.StepStatus.FAILED,
                                _features.StepStatus.BLOCKED,
                            )
                        ]
                        logger.error(
                            "[run] dead plan (%d stuck step(s)) turn=%d -- blocked finish",
                            len(_stuck), self.session.turn_count,
                        )
                        yield create_finish(
                            "stop", usage=t.backend_usage, context_usage=t.context_usage,
                            error=(
                                "plan_dead: the active plan has no runnable steps "
                                f"left -- {len(_stuck)} step(s) failed or blocked: "
                                + "; ".join(_stuck[:5])
                            ),
                            error_code="plan_dead",
                            error_category="plan",
                        )
                        raise TurnExit()
                    _progress_context = self._pdr.get_context()
                else:
                    # No active plan: surface the real run state (verification
                    # ledger + tools that errored this run) so the continuation
                    # nudge carries facts, not placeholders.
                    _unverified_n = len(self._verif_ledger.unverified_files())
                    _error_tools = sorted(self._error_tool_names)
                    if _unverified_n or _error_tools:
                        _progress_context = (
                            f"Run state: {_unverified_n} edited file(s) still lack "
                            f"passing verification; tools that errored this run: "
                            f"{', '.join(_error_tools) if _error_tools else 'none'}."
                        )
                # Diminishing-returns guard: track consecutive auto-continues
                # with tiny output.  If the model keeps "continuing" without
                # producing meaningful output, stop (it is going in circles).
                _out_tokens = t.backend_usage.get("output_tokens", 0)
                if (
                    self._auto_continue_consecutive >= self._profile.auto_continue_min_continues
                    and _out_tokens < self._profile.auto_continue_min_delta
                    and self._auto_continue_last_output < self._profile.auto_continue_min_delta
                ):
                    logger.warning(
                        "[run] auto-continue diminishing returns (output=%d) turn=%d -- stopping",
                        _out_tokens, self.session.turn_count,
                    )
                    _auto_continue = False
                else:
                    if (
                        not self._budget_state.is_exhausted
                        and self._budget_state.used_tokens > 0
                    ):
                        _auto_continue = True
                        self._auto_continue_consecutive += 1
                        self._auto_continue_last_output = _out_tokens
                if _auto_continue:
                    if self._state is not None:
                        self._state.transitions.record(
                            TurnTransition.AUTO_CONTINUE,
                            turn=self.session.turn_count,
                            detail=f"token={self._budget_state.used_tokens}/{self._budget_state.max_tokens}",
                        )
                    logger.info(
                        "[run] auto-continue turn=%s token=%d/%d",
                        self.session.turn_count,
                        self._budget_state.used_tokens,
                        self._budget_state.max_tokens,
                    )
                    self.session.add_message(
                        "system",
                        build_auto_continue_message(progress_context=_progress_context),
                        is_synthetic=True,
                    )
                    t.do_continue = True
                    return
            elif _pdr_plan_complete and self._profile.auto_continue:
                logger.info(
                    "[run] plan complete -- auto-continue suppressed turn=%d",
                    self.session.turn_count,
                )

            # Plan/spec mode suspends the verification ladder while the
            # artifacts are still being drafted -- there is nothing to verify
            # yet.  An approval installs a standing goal, and the
            # implementation pass that follows is exactly where the ladder
            # matters, so it is re-enabled for it (see verify_ladder_allowed).
            _pm = getattr(self, "_plan_mode", None)
            _verify_ok = (
                _pm.verify_ladder_allowed(t.slash_command_mode)
                if _pm is not None
                else t.slash_command_mode not in ("plan", "spec")
            )

            if not _auto_continue:
                # ── Verify-on-stop (bounded nudge, Hermes port) ─────
                # If this turn's text-only finish happened after editing
                # code that was never verified (or whose verification
                # failed), nudge the model to verify instead of silently
                # ending.  Evidence-driven via the verification ledger:
                # it distinguishes "checked and failed" (report that) from
                # "never checked".  Bounded by the profile's
                # verify_on_stop_nudges budget (GENERAL=2) so it can never
                # deadlock.
                _unverified = self._verif_ledger.unverified_files()
                if (
                    _verify_ok
                    and _unverified
                    and not t.checkpoint_injected_this_run
                    and self._verify_on_stop_nudges < self._profile.verify_on_stop_nudges
                ):
                    self._verify_on_stop_nudges += 1
                    _failed = self._verif_ledger.has_failed_evidence()
                    self.session.add_message(
                        "system", self._verif_ledger.build_nudge_message(failed=_failed),
                        is_synthetic=True,
                    )
                    logger.info(
                        "[run] verify-on-stop nudge %d/%d for %d files turn=%d failed=%s",
                        self._verify_on_stop_nudges, self._profile.verify_on_stop_nudges,
                        len(_unverified), self.session.turn_count, _failed,
                    )
                    if self._state is not None:
                        self._state.transitions.record(
                            TurnTransition.TEXT_ONLY,
                            turn=self.session.turn_count,
                            detail="verify_on_stop",
                        )
                    t.do_continue = True
                    return

                # ── Forced review escalation ─────────────────────────
                # If verify nudges were exhausted but a verification still
                # failed, do not silently finish: demand a critical review
                # (code-review skill / critic sub-agent) as the review stage
                # of the implement -> verify -> review -> fix loop.  Bounded
                # by _MAX_FORCED_REVIEWS so it can never deadlock.
                if (
                    _verify_ok
                    and _unverified
                    and self._verif_ledger.has_failed_evidence()
                    and not t.checkpoint_injected_this_run
                    and self._forced_review_count < self._profile.forced_reviews
                ):
                    self._forced_review_count += 1
                    self.session.add_message(
                        "system",
                        self._verif_ledger.build_forced_review_message(
                            list(self._verif_ledger.unverified_files())
                        ),
                        is_synthetic=True,
                    )
                    logger.info(
                        "[run] forced review escalation %d/%d for %d files turn=%d",
                        self._forced_review_count, self._profile.forced_reviews,
                        len(_unverified), self.session.turn_count,
                    )
                    if self._state is not None:
                        self._state.transitions.record(
                            TurnTransition.TEXT_ONLY,
                            turn=self.session.turn_count,
                            detail="forced_review",
                        )
                    t.do_continue = True
                    return

                # ── Hard verification gate ────────────────────────────
                # The final gate before a text-only finish is allowed.
                # After verify-on-stop nudges and the forced-review stage
                # are exhausted, unverified code must NOT silently become
                # a successful stop: keep escalating the demand (project-
                # level regression: build + typecheck + lint + tests).
                # Bounded by _MAX_VERIFY_HARD_GATE so a model that
                # refuses to fix can never wedge the loop forever; when
                # the bound is reached we finish with an explicit
                # "verification_blocked" error instead of a clean stop.
                if (
                    _verify_ok
                    and _unverified
                    and not t.checkpoint_injected_this_run
                    and self._verify_hard_gate_count < self._profile.verify_hard_gate
                ):
                    self._verify_hard_gate_count += 1
                    self.session.add_message(
                        "system",
                        self._verif_ledger.build_hard_gate_message(
                            list(self._verif_ledger.unverified_files()),
                            remaining=self._profile.verify_hard_gate - self._verify_hard_gate_count,
                        ),
                        is_synthetic=True,
                    )
                    logger.warning(
                        "[run] hard verification gate %d/%d for %d files turn=%d",
                        self._verify_hard_gate_count, self._profile.verify_hard_gate,
                        len(_unverified), self.session.turn_count,
                    )
                    if self._state is not None:
                        self._state.transitions.record(
                            TurnTransition.TEXT_ONLY,
                            turn=self.session.turn_count,
                            detail="hard_verify_gate",
                        )
                    t.do_continue = True
                    return

                # If the hard gate budget is exhausted and code is still
                # unverified, do NOT yield a clean stop -- surface it as a
                # blocked finish so the caller knows delivery is gated.
                if (
                    _verify_ok
                    and _unverified
                    and not t.checkpoint_injected_this_run
                    and self._profile.verify_budget_total() > 0
                ):
                    if self._state is not None:
                        self._state.transitions.record(
                            TurnTransition.TEXT_ONLY,
                            turn=self.session.turn_count,
                            detail="verification_blocked",
                        )
                    logger.error(
                        "[run] verification gate blocked %d files after %d hard-gate turns -- blocked finish",
                        len(_unverified), self._verify_hard_gate_count,
                    )
                    yield create_finish(
                        "stop", usage=t.backend_usage, context_usage=t.context_usage,
                        error=(
                            "verification_blocked: changes remain unverified "
                            f"({len(_unverified)} file(s)). Run the project build, "
                            "typecheck, lint, and tests until they pass before delivery."
                        ),
                        error_code="verification_blocked",
                        error_category="verification",
                    )
                    raise TurnExit()

                if self._state is not None:
                    self._state.transitions.record(
                        TurnTransition.TEXT_ONLY,
                        turn=self.session.turn_count,
                    )
                # ── Standing goal gate (plan/spec) ────────────────────
                # An approved plan/spec is the session's objective, not a
                # one-shot approval.  While checklist items remain, a clean
                # text-only finish is not an ending: the turn is pushed to the
                # next outstanding item.  The mode is left only when every item
                # is done, when the drive stalls with no progress left to make,
                # or when the user exits the mode by hand.
                _goal = None
                if _pm is not None and _pm.goal_implementing:
                    _pm.sync_goal_plan_items(getattr(self.session, "plan_items", None))
                    _goal = _pm.active_goal
                    _pending = _pm.pending_goal_items()
                    _total = _goal.total if _goal is not None else 0
                    _done = _goal.done_count() if _goal is not None else 0

                    if not _pending:
                        self.session.add_message(
                            "system", _pm.goal_complete_message(), is_synthetic=True,
                        )
                        _pm.complete_goal()
                        self.set_mode("")
                        if self._state is not None:
                            self._state.transitions.record(
                                TurnTransition.TEXT_ONLY,
                                turn=self.session.turn_count,
                                detail="goal_complete",
                            )
                        logger.info(
                            "[goal] complete (%d/%d) -- leaving mode turn=%d",
                            _done, _total, self.session.turn_count,
                        )
                        yield create_finish("stop", usage=t.backend_usage, context_usage=t.context_usage)
                        raise TurnExit()

                    if _pm.note_goal_push():
                        self.session.add_message(
                            "system", _pm.goal_continue_message(), is_synthetic=True,
                        )
                        if self._state is not None:
                            self._state.transitions.record(
                                TurnTransition.AUTO_CONTINUE,
                                turn=self.session.turn_count,
                                detail="goal_push",
                            )
                        logger.info(
                            "[goal] push (%d/%d done, %d outstanding) turn=%d",
                            _done, _total, len(_pending), self.session.turn_count,
                        )
                        t.do_continue = True
                        return

                    if self._state is not None:
                        self._state.transitions.record(
                            TurnTransition.TEXT_ONLY,
                            turn=self.session.turn_count,
                            detail="goal_incomplete",
                        )
                    logger.error(
                        "[goal] stalled with %d item(s) outstanding -- blocked finish turn=%d",
                        len(_pending), self.session.turn_count,
                    )
                    yield create_finish(
                        "stop", usage=t.backend_usage, context_usage=t.context_usage,
                        error=_pm.goal_incomplete_message(),
                        error_code="goal_incomplete",
                        error_category="goal",
                    )
                    raise TurnExit()

                # Plan-Do-Review step advancement (WORKSPACE): when the
                # turn finished cleanly, mark the current step done (or
                # failed if verification evidence shows broken state) and
                # start the next step so the plan makes visible progress
                # across turns.  A standing goal owns this drive when one is
                # active, so the two never push the same turn.
                if self._pdr_active and _goal is None:
                    _step_failed = self._verif_ledger.has_failed_evidence()
                    if _step_failed:
                        _failed_files = self._verif_ledger.unverified_files()
                        self._pdr.mark_step_failed(
                            error=f"verification failed for {len(_failed_files)} file(s): {', '.join(_failed_files[:5])}"
                        )
                    else:
                        self._pdr.mark_step_complete(summary="turn completed cleanly")
                        # Wire the Review phase of Plan-Do-Review: the
                        # step's tool-call log is graded for error
                        # density / repeated / empty results.  A bad grade
                        # rolls the step back to a retry instead of
                        # advancing to the next one, closing the loop that
                        # was previously Plan+Do only.
                        _review = self._pdr.lightweight_review()
                        if _review in (
                            _features.ReviewGrade.FAIL,
                            _features.ReviewGrade.NEEDS_RETRY,
                        ):
                            self._pdr.mark_step_failed(error=f"review grade: {_review.name}")
                    # Advance only when the current step actually reached
                    # COMPLETED. mark_step_failed may keep it IN_PROGRESS
                    # (retry budget) -- advancing then would skip the retry.
                    _cur = self._pdr.plan.current_step
                    if _cur is not None and _cur.status == _features.StepStatus.COMPLETED:
                        _next = self._pdr.start_next_step()
                        if _next is not None and _next.status == _features.StepStatus.IN_PROGRESS:
                            logger.info(
                                "[run] Plan-Do-Review step %d/%d completed, advancing to next step",
                                self._pdr.plan.current_step_index + 1, len(self._pdr.plan.steps),
                            )
                            self.session.add_message(
                                "system",
                                f"[PLAN STEP {self._pdr.plan.current_step_index + 1}] "
                                f"Continue the plan. Next step: {_next.description} "
                                f"(success criteria: {_next.success_criteria}).",
                                is_synthetic=True,
                            )
                            t.do_continue = True
                            return
                yield create_finish("stop", usage=t.backend_usage, context_usage=t.context_usage)
                # Main session: text-only ends this run. User sends next message.
                # Sub-agent: text-only completes the sub-agent task.
                raise TurnExit()

        t.assistant_content = "".join(t.text_parts) if t.text_parts else ""

        # Build OpenAI-format tool_calls from buffers.
        # We also attach the synthetic client-facing id (used for
        # streaming events) under a non-protocol ``_client_id`` key
        # so the renderer can correlate tool_results delivered via
        # ``client_id`` (in tool_progress / tool_result events) with
        # the same tc after a session restore.  Without this, restore
        # uses the backend ``id`` (tc["id"]) and streaming updates use
        # the client_id — the two halves never meet, so
        # subAgentMessages never lands on the right tc.
        t.assistant_tool_calls = []
        for idx in sorted(t.tool_call_buffers.keys()):
            tc = t.tool_call_buffers[idx]
            client_id = f"call_{t.tool_id_token}_{idx}"
            protocol_id = tc["id"] or client_id
            tc["id"] = protocol_id
            entry: dict[str, Any] = {
                "id": protocol_id,
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                },
            }
            # Only attach client_id when it differs from the
            # backend id (e.g. toolu_xxx) -- otherwise it would
            # be redundant and we keep the persisted shape slim.
            if client_id != entry["id"]:
                entry["_client_id"] = client_id
            t.assistant_tool_calls.append(entry)

        msg_kwargs: dict[str, Any] = {}
        if t.assistant_tool_calls:
            msg_kwargs["tool_calls"] = t.assistant_tool_calls
        if t.backend_usage:
            msg_kwargs["usage"] = t.backend_usage
        if t.thinking_parts:
            msg_kwargs["reasoning_content"] = "".join(t.thinking_parts)
        if t.thinking_blocks:
            # Native-thinking protocols (Anthropic Messages API) replay
            # thinking blocks with their server-issued signatures; a
            # mismatched or missing signature is a hard 400.  The plain
            # ``reasoning_content`` concatenation loses the block
            # boundaries, so the block list travels alongside it.
            msg_kwargs["reasoning_blocks"] = [
                b for b in t.thinking_blocks if b.get("text") or b.get("signature")
            ]
        # Build segments from streaming order
        segs = []
        if t.thinking_parts:
            segs.append({"kind": "thinking", "text": "".join(t.thinking_parts)})
        if t.assistant_content:
            segs.append({"kind": "text", "text": t.assistant_content})
        for tc in t.assistant_tool_calls:
            segs.append({"kind": "tool", "tool_id": tc["id"]})
        if segs:
            msg_kwargs["segments"] = segs
        self.session.add_message("assistant", t.assistant_content or None, **msg_kwargs)
        self._clear_pending_stream()


class PostStreamPhase(LoopPhase):
    """Turn-scoped phase: post-stream recovery + assistant materialisation.

    Wraps :meth:`PhaseRecoveryMixin._phase_post_stream` -- the exact code the
    runner used to call third inside every turn iteration.  This is the
    phase that owns the historical ``t.do_continue`` checkpoints (slot
    escalation, empty/stub retry, auto-continue, verify nudges, PDR step
    advancement): when it completes with that flag set, the pipeline skips
    the remaining phases and starts the next turn iteration -- expressed
    here as :attr:`PhaseAction.END_TURN`.
    """

    name = "post_stream"

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        t = ctx.turn
        await self._stream_through(ctx, self._loop._phase_post_stream(t))
        if t.do_continue:
            t.do_continue = False
            return PhaseResult.end_turn("post_stream recovery requested a fresh turn")
        return PhaseResult.continue_()
