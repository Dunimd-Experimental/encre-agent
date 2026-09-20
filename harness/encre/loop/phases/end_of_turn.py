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

"""End-of-turn phase: the turn tail of the former monolithic ``_run_impl``.

Extracted verbatim from the recovery phase module.  Contains the budget
grace call, the turn counter, repetitive tool-call detection + guardrail
ladder, telemetry, evolution reflection, turn-end hooks, rollback commit
and terminal cleanup.
"""

import asyncio
import time
from collections import Counter
from typing import Any

from encre.events.lifecycle import TurnEnded
from encre.logging_config import get_logger
from encre.loop.phase_api import LoopPhase, PhaseContext, PhaseResult
from encre.loop.turn_ctx import TurnContext
from encre.memdir.judgment import Experience, SignificanceJudge
from encre.compact.loop_state.transition import TurnTransition
from encre.loop_stability import BudgetState, build_grace_message
from encre.capabilities.process import TerminalSessionManager
from encre.utils.loop_helpers import (
    _GUARDRAIL_BLOCK_AFTER,
    _GUARDRAIL_HALT_AFTER,
    _GUARDRAIL_WARN_AFTER,
    _NO_PROGRESS_AFTER,
    _STUCK_LOOP_THRESHOLD,
    _VERIFY_TOOL_NAMES,
    _canonical_tool_signature,
    _is_idempotent_tool,
    _result_digest,
    _summarize_verify_result,
)
from encre.utils.tokens import estimate_tokens

logger = get_logger(__name__)


async def _cleanup_terminal_sessions() -> None:
    """Kill all persistent terminal sessions from the finished turn."""
    try:
        await TerminalSessionManager.instance().cleanup_all()
    except Exception:
        pass


class PhaseEndOfTurnMixin:
    """Turn tail bookkeeping for one agent turn."""

    async def _phase_end_of_turn(self, t: TurnContext) -> None:
        """Turn tail: grace call, guardrail ladder, telemetry, bookkeeping.

        Args:
            t: The shared turn context.

        Returns:
            None.  Sets ``t.do_break`` when the guardrail ladder halts the
            session (mirrors the original ``break`` statement).
        """
        # 鈹€鈹€ Budget grace call 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
        # When the token budget is exhausted, give the model one final
        # call to wrap up.  Mirrors Hermes agent's budget grace call.
        if t.backend_usage:
            self._budget_state.add_usage(
                t.backend_usage.get("output_tokens", 0)
                + t.backend_usage.get("input_tokens", 0)
            )
            self.session.metadata[BudgetState.META_KEY] = self._budget_state.checkpoint()
        if self._budget_state.is_exhausted and self._budget_state.can_grace:
            self._budget_state.use_grace()
            if self._state is not None:
                self._state.transitions.record(
                    TurnTransition.BUDGET_GRACE,
                    turn=self.session.turn_count,
                    detail=f"used={self._budget_state.used_tokens}/{self._budget_state.max_tokens}",
                )
            logger.info("[run] budget exhausted, using grace call turn=%d", self.session.turn_count)
            self.session.add_message("user", build_grace_message(), is_synthetic=True)
            self._budget_state.grace_enabled = False

        # Don't yield an assistant_boundary here -- doing so makes the frontend
        # split the response into separate bubbles after every tool-calling
        # turn.  All model output within a single user turn (including post-tool
        # follow-ups) stays in one assistant message on both session and UI.

        self.session.turn_count += 1
        turn_latency = (time.time() - t.turn_start) * 1000

        # 鈹€鈹€ Repetitive tool-call loop detection + guardrail ladder 鈹€鈹€
        # Detect when the model is genuinely stuck: same tool+args across
        # consecutive turns.  Different queries with the same tool name
        # (e.g. web_search with different queries) are fine.
        # Escalation (port of Hermes tool_guardrails.py):
        #   warn  (>= _GUARDRAIL_WARN_AFTER)  -> inject guidance
        #   block (>= _GUARDRAIL_BLOCK_AFTER) -> feed a synthetic error
        #   halt  (>= _GUARDRAIL_HALT_AFTER)  -> hard-stop the turn
        turn_sigs: list[str] = []
        _canonical = _canonical_tool_signature
        _turn_sigs_set: set[str] = set()
        _turn_sig_names: dict[str, str] = {}
        for tc in t.assistant_tool_calls:
            func = tc.get("function", {})
            name = func.get("name", "")
            args_raw = func.get("arguments", "")
            # Normalize whitespace so formatting differences (newlines vs
            # single-line JSON, extra indentation) don't hide a true repeat.
            args_key = ("".join((args_raw or "").split()))[:80]
            turn_sigs.append(f"{name}:{args_key}")
            # Canonical-hash fingerprint for the guardrail ladder.  Only
            # consecutive-turn repeats accumulate: a signature that did not
            # appear on the previous turn resets to 1, so legitimate but
            # scattered use of a common tool never triggers a false halt.
            sig = _canonical(name, args_raw)
            _turn_sigs_set.add(sig)
            _turn_sig_names[sig] = name
            if sig in self._guardrail_prev_sigs:
                self._guardrail_call_counts[sig] = self._guardrail_call_counts.get(sig, 1) + 1
            else:
                self._guardrail_call_counts[sig] = 1
        self._guardrail_prev_sigs = _turn_sigs_set
        if turn_sigs:
            self._recent_tool_names.append(tuple(turn_sigs))
            if len(self._recent_tool_names) > 20:
                self._recent_tool_names.pop(0)

            # No-progress detection for idempotent read-only tools: when
            # the same tool returns a byte-identical result on repeat, the
            # model is re-reading the same data (a stall), even if the args
            # differ cosmetically.
            for p in t.prepared:
                pname = p.get("name", "")
                # A successful verify/lint/test tool records passing evidence
                # in the verification ledger (the change has been checked).
                if pname in _VERIFY_TOOL_NAMES and not p.get("skip"):
                    _vpassed = not p.get("is_error", False)
                    self._verif_ledger.record_verification(
                        pname, _vpassed,
                        summary=_summarize_verify_result(pname, str(p.get("result", "")), _vpassed),
                    )
                # Plan-Do-Review (WORKSPACE): record the tool call against
                # the current step so lightweight review can judge step
                # completion from real tool outcomes, not narrative.
                if self._pdr_active and not p.get("skip"):
                    self._pdr.record_tool_call(
                        turn=self.session.turn_count,
                        tool_name=pname,
                        args=p.get("args") or {},
                        result=str(p.get("result", "")),
                        is_error=bool(p.get("is_error", False)),
                    )
                if _is_idempotent_tool(pname) and not p.get("is_error"):
                    pdigest = _result_digest(str(p.get("result", "")))
                    _dkey = f"{pname}:{pdigest}"
                    if self._guardrail_no_progress_digests.get(pname) == pdigest:
                        self._guardrail_no_progress_counts[_dkey] = \
                            self._guardrail_no_progress_counts.get(_dkey, 0) + 1
                    else:
                        self._guardrail_no_progress_counts[_dkey] = 1
                    self._guardrail_no_progress_digests[pname] = pdigest
                    if self._guardrail_no_progress_counts.get(_dkey, 0) >= _NO_PROGRESS_AFTER:
                        logger.warning(
                            "[run] idempotent tool %s returned identical result %dx "
                            "turn=%d -- no progress, injecting guidance",
                            pname, self._guardrail_no_progress_counts[_dkey],
                            self.session.turn_count,
                        )
                        p["result"] = (
                            f"{p.get('result', '')}\n\n[NO-PROGRESS] `{pname}` returned "
                            "an identical result on repeated calls with no state change. "
                            "Stop re-reading the same data and choose a different action."
                        )

            # Detect repetitive tool-loops even when tools are failing:
            # a model stuck re-invoking the same tool+args (often the
            # exact failing call) is the most common runaway scenario,
            # and _record_stuck_event only injects guidance -- it never
            # aborts -- so firing on error turns is safe and desirable.
            if len(self._recent_tool_names) >= _STUCK_LOOP_THRESHOLD:
                # Count each distinct signature across the window so that an
                # alternating pattern (A,B,A,B,...) is caught as stuck, not
                # just a strictly-consecutive repeat.
                recent = self._recent_tool_names[-_STUCK_LOOP_THRESHOLD:]
                _counts = Counter(recent)
                _repeated_sig, _repeated_count = _counts.most_common(1)[0]
                if _repeated_count >= _STUCK_LOOP_THRESHOLD:
                    logger.warning(
                        "[run] repetitive tool-loop: %s x%d turn=%d",
                        _repeated_sig, _repeated_count, self.session.turn_count,
                    )
                    self._record_stuck_event(_repeated_sig)
                    self._set_task_stage("discover", reason="stuck loop recovery")
                    self._refresh_working_set(t.prompt, t.prepared)

            # Guardrail escalation on the most-repeated canonical signature.
            if self._guardrail_call_counts:
                _top_sig, _top_count = max(
                    self._guardrail_call_counts.items(), key=lambda kv: kv[1],
                )
                if _top_count >= _GUARDRAIL_HALT_AFTER and not self._guardrail_halt:
                    self._guardrail_halt = True
                    logger.error(
                        "[run] guardrail HALT: %s called %dx -- stopping turn %d",
                        _top_sig, _top_count, self.session.turn_count,
                    )
                    self._record_stuck_event((_top_sig,))
                    self._set_task_stage("discover", reason="guardrail halt")
                    t.do_break = True
                    return
                if _top_count >= _GUARDRAIL_BLOCK_AFTER:
                    # Feed a synthetic block result so the model sees the call
                    # was refused rather than silently looping again.
                    _blocked_name = _turn_sig_names.get(_top_sig, "")
                    for p in t.prepared:
                        if _blocked_name and p.get("name") == _blocked_name:
                            p["result"] = (
                                f"[GUARDRAIL-BLOCK] `{_blocked_name}` has been called "
                                f"{_top_count} times in a loop and is blocked. Stop invoking "
                                "this identical call; choose a different tool or action."
                            )
                    logger.warning(
                        "[run] guardrail BLOCK: %s called %dx turn=%d",
                        _top_sig, _top_count, self.session.turn_count,
                    )
                elif _top_count >= _GUARDRAIL_WARN_AFTER:
                    logger.warning(
                        "[run] guardrail WARN: %s called %dx turn=%d",
                        _top_sig, _top_count, self.session.turn_count,
                    )
                    self._record_stuck_event((_top_sig,))

        if not t.backend_usage:
            _input_est = estimate_tokens(t.prompt or "")
            _output_est = estimate_tokens(t.assistant_content or "")
            t.backend_usage = {"input_tokens": _input_est, "output_tokens": _output_est}
        self.telemetry.record_turn(
            turn_number=self.session.turn_count,
            event_count=t.turn_events,
            latency_ms=turn_latency,
            token_usage=t.backend_usage,
            model=self.config.model,
            channel=self._state_mgr.get("channel", "normal"),
        )

        # Evolution: reflex + meta-cognition
        tool_outcomes: list[dict[str, Any]] = [
            {
                "tool_name": p.get("name", ""),
                "is_error": bool(p.get("is_error") or p.get("skip") and p.get("error")),
                "semantic_type": p.get("semantics", {}).get("semantic_type", ""),
            }
            for p in t.prepared
        ]
        self._maybe_record_turn_summary(t.prompt, t.prepared, tool_outcomes)
        self._refresh_working_set(t.prompt, t.prepared)
        self.reflex.reflect(
            turn_number=self.session.turn_count,
            tool_results=tool_outcomes,
            turn_latency_ms=turn_latency,
        )
        self.meta.assess_turn(
            prompt=t.prompt,
            tool_results=tool_outcomes,
        )

        await self.hook_system.emit_turn_end(self.session.turn_count)
        self.event_stream.publish(TurnEnded(
            turn=self.session.turn_count, event_count=t.turn_events,
        ))
        # Trigger background review every N turns (fire-and-forget)
        if self.reviewer is not None:
            asyncio.create_task(self.reviewer.on_turn_end(self))
        # Significance judgment -- the ONLY path into memory.  This phase
        # never writes memory itself: it collects this turn's experiences,
        # hands them to the judge as a background task, and lets the judge
        # publish `memory_candidate` events on the event backbone.  Anything
        # below the threshold is dropped silently (no event, no write).
        self._dispatch_significance_judgment(t)
        # Behaviour -> safety-policy reflow.  Corrections are recorded at three
        # call sites (execute / tools / tool executor); this single turn-tail
        # hook covers all of them without touching any of those sites.
        self._dispatch_safety_reflow()
        self.rollback.commit(self.session, f"turn_{self.session.turn_count}")

        # Clean up persistent terminal sessions from this turn.
        await _cleanup_terminal_sessions()

        # Record successful turn completion as NEXT_TURN
        if self._state is not None:
            self._state.transitions.record(
                TurnTransition.NEXT_TURN,
                turn=self.session.turn_count,
            )

    # -- Significance judgment (the only memory entry point) ----------------

    def _resolve_event_bus(self) -> Any:
        """Return the event backbone, if one is wired into the loop.

        The append-only :class:`~encre.events.ledger.EventSourcedBus` is
        being introduced into the loop wiring; different call sites name the
        attribute differently, so probe the known names.  Returns ``None``
        when no backbone is available, in which case judgment is skipped
        entirely (memory is never written through any other path).
        """
        for name in ("event_bus", "ledger_bus", "events_bus"):
            bus = getattr(self, name, None)
            if bus is not None:
                return bus
        return None

    def _collect_turn_experiences(self, t: TurnContext) -> list[Experience]:
        """Collect this turn's judgeable experiences.

        Covers the user's message for the turn (where corrections and
        emphasis live) and every tool result (where errors, surprises and
        key outcomes live).  Raw stream noise is not collected.

        Args:
            t: The shared turn context.

        Returns:
            The experiences to hand to the significance judge.
        """
        session_id = getattr(self.session, "id", "") or ""
        experiences: list[Experience] = []
        # ``t.prompt`` is run-scoped and therefore re-seen on every turn of
        # the same run.  Judge it only once per distinct prompt: otherwise a
        # neutral prompt would be mistaken for a "repeated pattern" on the
        # second turn and falsely clear the significance threshold.
        if t.prompt and t.prompt != getattr(self, "_last_judged_prompt", None):
            self._last_judged_prompt = t.prompt
            experiences.append(Experience(
                kind="user_message",
                content=t.prompt,
                session_id=session_id,
                metadata={"source": "turn_prompt"},
                ts=t.turn_start or time.time(),
            ))
        for p in t.prepared:
            result = str(p.get("result", "") or "")
            if not result:
                continue
            refs = [
                str(r) for r in (
                    p.get("tool_call_id") or p.get("id") or ""
                ) if r
            ]
            experiences.append(Experience(
                kind="tool_result",
                content=result,
                session_id=session_id,
                refs=refs,
                metadata={
                    "tool_name": str(p.get("name", "") or ""),
                    "is_error": bool(p.get("is_error", False)),
                    "source": "tool_result",
                },
            ))
        return experiences

    def _dispatch_significance_judgment(self, t: TurnContext) -> None:
        """Send this turn's experiences to the judge without blocking.

        Judgment runs as a background task (``asyncio.create_task`` inside
        the judge), so the turn tail never waits on it.  Candidates that
        clear the threshold are published by the judge as ``memory_candidate``
        events on the backbone; nothing is persisted here.

        Args:
            t: The shared turn context.

        Returns:
            None.
        """
        bus = self._resolve_event_bus()
        if bus is None:
            return
        judge = getattr(self, "_significance_judge", None)
        if judge is None:
            # The ``strong_association`` signal reads existing memory through
            # the single retrieval path (associative recall over the layer
            # store).  When the memory subsystem is unavailable the engine is
            # ``None`` and the signal stays inactive.
            judge = SignificanceJudge(
                recall_engine=getattr(self, "recall_engine", None),
            )
            self._significance_judge = judge
        experiences = self._collect_turn_experiences(t)
        if not experiences:
            return
        try:
            judge.schedule_judgment(experiences, bus)
        except Exception:
            logger.debug(
                "[end_of_turn] significance judgment dispatch failed",
                exc_info=True,
            )

    def _dispatch_safety_reflow(self) -> None:
        """Compile newly learned corrections into permission policies.

        The feedback learner records user corrections at three call sites
        (``loop/phases/execute.py``, ``loop/phases/tools.py`` and
        ``tools/executor.py``); this single turn-tail hook deliberately covers
        all of them.  It runs the learned-policy reflow only when the learner's
        record count changed since the previous run, so an unchanged turn costs
        one integer comparison and never rewrites the policy document -- i.e.
        at most one reflow per turn.  On a change the reflow compiles the
        records, persists the encrypted policy document and pushes the entries
        into the Rust permission engine
        (``apply`` = compile -> persist -> sync), which is what turns a
        correction into a real authorization decision.

        A failure here must never affect turn teardown, so everything is caught
        and logged.  The record count is advanced only after a successful
        reflow, so a transient fault is retried on the next turn.

        Returns:
            None.
        """
        reflow = getattr(self, "_policy_reflow", None)
        if reflow is None:
            return
        learner = getattr(self, "feedback", None)
        count = getattr(learner, "record_count", None)
        if not isinstance(count, int):
            return
        if count == getattr(self, "_last_reflow_record_count", -1):
            return
        try:
            reflow.apply(session_id=getattr(self.session, "id", "") or "")
        except Exception:
            logger.debug(
                "[end_of_turn] safety-policy reflow failed", exc_info=True,
            )
            return
        self._last_reflow_record_count = count


class EndOfTurnPhase(LoopPhase):
    """Turn-scoped phase: end-of-turn bookkeeping.

    Wraps :meth:`PhaseEndOfTurnMixin._phase_end_of_turn` -- the exact code
    the runner used to call last inside every turn iteration (grace call,
    guardrail ladder, telemetry, evolution, hooks, rollback).  This is the
    phase that owns the historical ``t.do_break`` checkpoint after the turn
    tail (guardrail halt): when it completes with that flag set, the
    pipeline leaves the turn loop -- expressed here as
    :attr:`PhaseAction.END_RUN`.
    """

    name = "end_of_turn"

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        t = ctx.turn
        await self._loop._phase_end_of_turn(t)
        if t.do_break:
            t.do_break = False
            return PhaseResult.end_run("end-of-turn guardrail halted the session")
        return PhaseResult.continue_()
