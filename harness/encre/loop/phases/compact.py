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

"""Compaction phase: pre-API compression, milestone summaries, post-tool trim.

Extracted verbatim from the original monolithic ``_run_impl``.  Contains:

* ``_phase_pre_model_compact`` -- the turn-start token budget check, the
  unified compression pipeline (budget -> collapse -> microcompact ->
  snip -> autocompact -> milestone), the synchronous autocompact with
  race guard, and P1 milestone summarisation.
* ``_phase_post_tool_compact`` -- the post-tool compression check that runs
  after tool execution, mirroring Hermes' post-response compression.
"""

import contextlib
import copy
from collections.abc import AsyncGenerator

from encre.logging_config import get_logger
from encre.loop.phase_api import (
    LoopPhase,
    PhaseContext,
    PhaseResult,
)
from encre.loop.state import LoopState as LoopRunState
from encre.loop.turn_ctx import TurnContext
from encre.loop_stability import should_post_tool_compact
from encre.session import compact_notice_anchor
from encre.utils.tokens import count_message_tokens, estimate_request_overhead
from encre.utils.types import CompactFailed, CompactNotification, CompactStarting

logger = get_logger(__name__)


class PreModelCompactPhase(LoopPhase):
    """Turn-scoped phase: pre-API-call compaction (Claude Code style).

    Wraps :meth:`PhaseCompactMixin._phase_pre_model_compact` -- the exact
    code the runner used to call first inside every turn iteration.
    """

    name = "pre_model_compact"

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        await self._stream_through(ctx, self._loop._phase_pre_model_compact(ctx.turn))
        return PhaseResult.continue_()


class PostToolCompactPhase(LoopPhase):
    """Turn-scoped phase: post-tool compression check.

    Wraps :meth:`PhaseCompactMixin._phase_post_tool_compact` -- the exact
    code the runner used to call after tool execution.
    """

    name = "post_tool_compact"

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        await self._stream_through(ctx, self._loop._phase_post_tool_compact(ctx.turn))
        return PhaseResult.continue_()


class PhaseCompactMixin:
    """Context compaction for one ``run()`` invocation."""

    async def force_compact(self) -> dict[str, int] | None:
        """Compact the session context outside of a turn (gateway ``/compact``).

        Runs the same engine as the automatic pre-model phase against the
        current context view.  Returns before/after token and message counts,
        or ``None`` when there is nothing to compact.  Callers must ensure
        the session is idle -- this touches the same view state the in-turn
        phase owns.
        """
        context_msgs = self.session.get_context_messages()
        if not context_msgs:
            return None
        snap_max_seq = max(
            (m.get("seq_in_branch", -1) for m in context_msgs), default=-1,
        )
        compacted = await self.compact_engine.compact(
            copy.deepcopy(context_msgs), backend=self.backend,
            turn_count=self.session.turn_count,
            system_prompt="",
            enable_caching=self.config.enable_prompt_caching,
            session_id=self.session.id or "",
        )
        if compacted is None:
            return None
        self.session.apply_compacted_view(
            self.session.active_branch_id, compacted, snap_max_seq,
        )
        self._update_user_requirements(compacted)
        return {
            "old_tokens": count_message_tokens(context_msgs),
            "new_tokens": count_message_tokens(compacted),
            "old_count": len(context_msgs),
            "new_count": len(compacted),
        }

    async def _phase_pre_model_compact(
        self, t: TurnContext,
    ) -> AsyncGenerator:
        """Pre-API-call compaction (Claude Code style).

        BEFORE the model sees the messages, check the token budget against
        the model's actual context window and run the unified compression
        pipeline.  Yields ``CompactStarting`` / ``CompactNotification`` when
        a synchronous autocompact runs.

        Args:
            t: The shared turn context.

        Yields:
            ``CompactStarting`` before compression and ``CompactNotification``
            after a successful compaction.
        """
        context_msgs = t.context_msgs
        # This is pure arithmetic -- no fixed turn count.  The compact agent
        # uses the SAME backend as the main loop to produce a structured
        # summary that preserves task intent, key decisions, files, errors,
        # and current state.
        window = self.backend.context_window_size()
        est_tokens = count_message_tokens(context_msgs)
        t.window = window
        t.est_tokens = est_tokens
        logger.info(
            "[run] turn={turn} msgs={msgs} tokens={est_k}dk/{window_k}dk ({pct:.0f}%)",
            turn=self.session.turn_count, msgs=len(context_msgs),
            est_k=est_tokens // 1000, window_k=window // 1000,
            pct=100 * est_tokens / window if window else 0,
        )

        # ── Unified compression pipeline ───────────────────────────
        # Replaces the ad-hoc step1/step1a/step1b/step2 with a single
        # pipeline that runs stages in order: budget → collapse →
        # microcompact → snip → autocompact (async) → milestone.
        _notice_id = ""
        try:
            # Include the fixed per-request overhead (system prompt + tool
            # schemas) in the autocompact trigger so it compares the REAL
            # request size against the window, not just the message list.
            _overhead = estimate_request_overhead(
                t.system_prompt, getattr(t, "backend_tools", None),
            )
            # The pipeline may rewrite message dicts in place (e.g.
            # microcompact truncating tool outputs).  Feed it deep copies so
            # the persisted branch history is NEVER touched -- compression is
            # a view-layer operation only.
            pipeline_input = copy.deepcopy(context_msgs)
            # Core-owned evidence markers: with preserve_verify_evidence the
            # verification/plan/budget facts must survive microcompact even
            # on the pipeline path (the async autocompact below receives the
            # same points as a [compact-directive]).
            _pipeline_protected: list[str] = []
            if getattr(self._profile, "preserve_verify_evidence", False):
                with contextlib.suppress(Exception):
                    _pipeline_protected.extend(self._verif_ledger.preservation_notes())
                if self._pdr_active:
                    with contextlib.suppress(Exception):
                        _pctx = self._pdr.get_context()
                        if _pctx:
                            _pipeline_protected.append(str(_pctx))
            pipeline_report = await self._compaction_pipeline.run(
                pipeline_input,
                backend=self.backend,
                config=self.config,
                extra_tokens=_overhead,
                protected_texts=_pipeline_protected or None,
            )
            if pipeline_report.changed:
                # Install the compressed list as the model-context overlay:
                # the persisted branch history stays append-only and intact.
                upto = max(
                    (m.get("seq_in_branch", -1) for m in context_msgs),
                    default=-1,
                )
                self.session.apply_compacted_view(
                    self.session.active_branch_id, pipeline_report.messages, upto,
                )
                self._compacted_this_turn = True
                context_msgs = self.session.get_context_messages()
                t.context_msgs = context_msgs
                est_tokens = count_message_tokens(context_msgs)
                t.est_tokens = est_tokens

            # Log any stages that did work
            for s in pipeline_report.stages:
                if s.did_work:
                    logger.info(
                        "[pipeline] %s turn=%d msgs %d->%d tokens %dk->%dk %s",
                        s.name, self.session.turn_count,
                        s.msgs_before, s.msgs_after,
                        s.tokens_before // 1000, s.tokens_after // 1000,
                        s.detail or "",
                    )

            # ── Synchronous autocompact (was async background) ─────
            # Instead of running the compact as a detached background task
            # while the model continues to generate output (which causes
            # confusing "thinking" text from the user's perspective), we
            # now run compact synchronously: signal the frontend, pause,
            # compact, then resume.  This keeps the model from generating
            # during compression and provides a clear UI indicator.
            #
            # Benefit pre-check: _build_compacted always keeps the system
            # message, the first-user anchor and the last turns verbatim.
            # When nearly all tokens live in that keep-window (few turns
            # or one giant early message), compaction cannot shrink the
            # context meaningfully -- skip it instead of burning a summary
            # LLM call and emitting misleading CompactStarting events.
            #
            # EXCEPT when the context is at/over the window itself: that is
            # exactly the state a model switch to a SMALLER window creates
            # (1M -> 128K with a 200K context).  Skipping there would leave
            # the next request guaranteed to overflow, so the compaction
            # must run (the engine's budget fallback still truncates to
            # fit when the summary path cannot free enough).
            _floor_tokens = 0
            for _m in context_msgs:
                if _m.get("role") == "system":
                    _floor_tokens += count_message_tokens([_m])
            if context_msgs:
                _floor_tokens += count_message_tokens([context_msgs[0]])
                _floor_tokens += count_message_tokens(context_msgs[-8:])
            _headroom = est_tokens - _floor_tokens
            _over_window = window > 0 and est_tokens >= window
            if (
                pipeline_report.needs_compact
                and _headroom < max(4000, int(est_tokens * 0.15))
                and not _over_window
            ):
                logger.info(
                    "[compact] skipping turn=%d -- removable headroom %dk "
                    "too small (floor %dk / est %dk); keep-window dominates",
                    self.session.turn_count, max(_headroom, 0) // 1000,
                    _floor_tokens // 1000, est_tokens // 1000,
                )
                pipeline_report.needs_compact = False
            elif pipeline_report.needs_compact and _over_window:
                logger.warning(
                    "[compact] turn=%d est %dk >= window %dk -- compacting "
                    "regardless of headroom %dk (window shrank or context "
                    "overflowed)",
                    self.session.turn_count, est_tokens // 1000,
                    window // 1000, max(_headroom, 0) // 1000,
                )

            if pipeline_report.needs_compact:
                self._compact_epoch += 1
                logger.info(
                    "[compact] starting turn=%d tokens=%dk window=%dk",
                    self.session.turn_count, est_tokens // 1000, window // 1000,
                )
                # Enter the compacting runtime state for the duration of the
                # synchronous autocompact; back to running once it settles.
                self._transition_loop(
                    LoopRunState.COMPACTING,
                    {"phase": "pre_model_compact", "turn": self.session.turn_count},
                )

                # Notice identity + anchor: shared by the live
                # CompactStarting indicator and the persisted session
                # notice recorded on success, so the frontend flips the
                # SAME timeline line from "compressing" to "compacted".
                _notice_id, _anchor_id = compact_notice_anchor(context_msgs)
                # Record the highest seq_in_branch at snapshot time
                # so we can detect messages added while compact ran.
                snap_max_seq = max(
                    (m.get("seq_in_branch", -1) for m in context_msgs),
                    default=-1,
                )
                # Signal frontend that compression is starting
                yield CompactStarting(notice_id=_notice_id, anchor_id=_anchor_id)
                # Complementary compression engineering: the pre_compact
                # hook lets plugins contribute ``preserve_points`` inside
                # ``modified_input`` -- must-keep facts the built-in engine
                # MUST digest into its summary.  The points JOIN the built-in
                # pipeline as a directive message; the built-in engine still
                # owns the strategy.  Neither side replaces the other, and a
                # plugin that blocks the compaction still wins outright.
                _pre_compact = await self.hook_system.emit_pre_compact(
                    len(context_msgs), est_tokens,
                )
                if isinstance(_pre_compact, dict) and _pre_compact.get("block"):
                    logger.info(
                        "[compact] plugin vetoed compaction turn=%d",
                        self.session.turn_count,
                    )
                    t.turn_events += 1
                    return
                _preserve_points: list[str] = []
                if isinstance(_pre_compact, dict):
                    _mi = _pre_compact.get("modified_input")
                    if isinstance(_mi, dict):
                        _cand = _mi.get("preserve_points")
                        if isinstance(_cand, list):
                            _preserve_points = [str(p).strip() for p in _cand if str(p).strip()]
                # Core-owned evidence preservation: when the profile demands
                # it, the loop contributes its own preserve points BEFORE the
                # plugin ones -- verification evidence, the active plan state
                # and the budget state are facts the post-compaction verify
                # gates still need, and no plugin should be required to
                # report them.  Plugin points are appended (increment), never
                # replaced.
                if getattr(self._profile, "preserve_verify_evidence", False):
                    _core_points: list[str] = []
                    with contextlib.suppress(Exception):
                        _core_points.extend(self._verif_ledger.preservation_notes())
                    if self._pdr_active:
                        with contextlib.suppress(Exception):
                            _ctx = self._pdr.get_context()
                            if _ctx:
                                _core_points.append(
                                    "Active plan state at compaction time:\n" + str(_ctx)
                                )
                    with contextlib.suppress(Exception):
                        _bs = self._budget_state
                        _core_points.append(
                            f"Token budget state: {_bs.used_tokens}/{_bs.max_tokens} used."
                        )
                    _preserve_points = [*_core_points, *_preserve_points]
                if _preserve_points:
                    context_msgs = [*context_msgs, {
                        "role": "user",
                        "content": (
                            "[compact-directive] The following points MUST be "
                            "preserved verbatim in the compacted summary:\n- "
                            + "\n- ".join(_preserve_points)
                        ),
                    }]
                # Deep-copy the input: the compact engine may rewrite message
                # dicts while building the summary -- the persisted history
                # must stay untouched.
                compacted = await self.compact_engine.compact(
                    copy.deepcopy(context_msgs), backend=self.backend,
                    turn_count=self.session.turn_count,
                    system_prompt=t.system_prompt or "",
                    enable_caching=self.config.enable_prompt_caching,
                    session_id=self.session.id or "",
                )
                if compacted is not None:
                    # Install the compaction result as the model-context
                    # overlay.  The persisted branch history is untouched;
                    # messages added while the compact ran (seq >
                    # snap_max_seq) are appended automatically by the
                    # view+tail composition -- no race guard needed.
                    self.session.apply_compacted_view(
                        self.session.active_branch_id, compacted, snap_max_seq,
                    )
                    self._compacted_this_turn = True
                    self._update_user_requirements(compacted)
                    new_tokens = count_message_tokens(compacted)
                    # Persist the compaction as a UI notice (recorded like a
                    # tool call: survives reload, rendered inline at the
                    # anchor).  Recorded BEFORE the notification so any
                    # refresh the client does on receipt already carries it.
                    self.session.record_ui_notice(
                        "compact",
                        {
                            "old_tokens": est_tokens,
                            "new_tokens": new_tokens,
                            "old_count": len(context_msgs),
                            "new_count": len(compacted),
                        },
                        notice_id=_notice_id,
                        anchor_id=_anchor_id,
                    )
                    yield CompactNotification(
                        old_count=len(context_msgs),
                        new_count=len(compacted),
                        old_tokens=est_tokens,
                        new_tokens=new_tokens,
                        notice_id=_notice_id,
                    )
                    # Refresh context so the model sees the compacted messages
                    context_msgs = self.session.get_context_messages()
                    t.context_msgs = context_msgs
                    est_tokens = new_tokens
                    t.est_tokens = est_tokens
                    logger.info(
                        "[compact] done turn=%d msgs %d->%d tokens %dk->%dk",
                        self.session.turn_count, len(context_msgs),
                        len(compacted), est_tokens // 1000, new_tokens // 1000,
                    )
                else:
                    logger.info("[compact] returned None -- no compaction applied")
                    # Dismiss the "compressing" indicator: nothing changed.
                    yield CompactFailed(reason="no compaction applied", notice_id=_notice_id)
                # Compaction settled (success or no-op): resume running.
                self._transition_loop(
                    LoopRunState.RUNNING,
                    {"phase": "pre_model_compact", "turn": self.session.turn_count},
                )

        except Exception as _pe:
            logger.warning("[pipeline] failed turn=%d: %s", self.session.turn_count, _pe)
            # Never leave the runtime state stuck in compacting when the
            # pipeline blows up mid-compaction; running -> running is an
            # idempotent no-op when we never entered compacting.
            self._transition_loop(
                LoopRunState.RUNNING,
                {"phase": "pre_model_compact", "recovered": True},
            )
            # If a CompactStarting was already sent, dismiss the indicator;
            # when none was sent this is a harmless no-op on the frontend.
            yield CompactFailed(reason=str(_pe), notice_id=_notice_id)

        # P1: milestone summarisation.  Every MILESTONE_INTERVAL turns
        # we write a compact "milestone" into session metadata.
        try:
            await self._maybe_write_milestone(context_msgs)
        except Exception as _m_err:
            logger.warning("[milestone] failed turn=%d: %s", self.session.turn_count, _m_err)

    async def _phase_post_tool_compact(self, t: TurnContext) -> AsyncGenerator:
        """Post-tool compression check.

        After tool execution, check if the context has grown too large
        and compact before the next iteration.  Mirrors Hermes agent's
        post-response compression in conversation_loop.py.

        Yields ``CompactStarting`` before compression and
        ``CompactNotification`` after a successful compaction so the
        frontend shows the compression indicator instead of appearing to
        stall silently.

        Args:
            t: The shared turn context.

        Yields:
            ``CompactStarting`` / ``CompactNotification`` events.
        """
        _post_tool_msgs = self.session.get_context_messages()
        if should_post_tool_compact(
            _post_tool_msgs,
            self.backend.context_window_size(),
            self.config.max_tokens,
            extra_tokens=estimate_request_overhead(
                t.system_prompt, getattr(t, "backend_tools", None),
            ),
        ):
            logger.info(
                "[run] post-tool compression triggered turn=%d msgs=%d",
                self.session.turn_count, len(_post_tool_msgs),
            )
            try:
                # Authoritative synchronous pass: bump the epoch so any
                # in-flight background compaction discards its result.
                self._compact_epoch += 1
                _pre_tokens = count_message_tokens(_post_tool_msgs)
                # Notice identity + anchor shared by the live indicator and
                # the persisted notice (same line, two states).
                _notice_id, _anchor_id = compact_notice_anchor(_post_tool_msgs)
                # Enter compacting for the duration of the post-tool
                # compression; back to running once it settles.
                self._transition_loop(
                    LoopRunState.COMPACTING,
                    {"phase": "post_tool_compact", "turn": self.session.turn_count},
                )
                # Signal the frontend before compressing.
                yield CompactStarting(notice_id=_notice_id, anchor_id=_anchor_id)
                _post_compacted = await self.compact_engine.compact(
                    copy.deepcopy(_post_tool_msgs), backend=self.backend,
                    turn_count=self.session.turn_count,
                    system_prompt=t.system_prompt or "",
                    enable_caching=self.config.enable_prompt_caching,
                    session_id=self.session.id or "",
                )
                if _post_compacted is not None:
                    # View overlay, not branch replacement: the persisted
                    # history stays intact; only the model's context shrinks.
                    _upto = max(
                        (m.get("seq_in_branch", -1) for m in _post_tool_msgs),
                        default=-1,
                    )
                    self.session.apply_compacted_view(
                        self.session.active_branch_id, _post_compacted, _upto,
                    )
                    self._compacted_this_turn = True
                    # Persist the compaction as a UI notice BEFORE the
                    # notification, so the refreshed message list the client
                    # loads on receipt already carries the divider.
                    self.session.record_ui_notice(
                        "compact",
                        {
                            "old_tokens": _pre_tokens,
                            "new_tokens": count_message_tokens(_post_compacted),
                            "old_count": len(_post_tool_msgs),
                            "new_count": len(_post_compacted),
                        },
                        notice_id=_notice_id,
                        anchor_id=_anchor_id,
                    )
                    yield CompactNotification(
                        old_count=len(_post_tool_msgs),
                        new_count=len(_post_compacted),
                        old_tokens=_pre_tokens,
                        new_tokens=count_message_tokens(_post_compacted),
                        notice_id=_notice_id,
                    )
                    logger.info("[run] post-tool compact succeeded turn=%d", self.session.turn_count)
                    # Refresh memory so next turn sees any new memories written this turn
                    if self.memory_system is not None:
                        try:
                            self.memory_system.refresh()
                        except Exception:
                            logger.warning("[run] post-tool memory refresh failed", exc_info=True)
                else:
                    # Compaction was a no-op: dismiss the indicator.
                    yield CompactFailed(reason="no compaction applied", notice_id=_notice_id)
                # Compaction settled (success or no-op): resume running.
                self._transition_loop(
                    LoopRunState.RUNNING,
                    {"phase": "post_tool_compact", "turn": self.session.turn_count},
                )
            except Exception as _ptc_err:
                logger.warning("[run] post-tool compact failed: %s", _ptc_err)
                # Never leave the runtime state stuck in compacting.
                self._transition_loop(
                    LoopRunState.RUNNING,
                    {"phase": "post_tool_compact", "recovered": True},
                )
                yield CompactFailed(reason=str(_ptc_err), notice_id=_notice_id)
