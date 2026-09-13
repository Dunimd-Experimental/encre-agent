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

"""Compaction phase: pre-API compression, milestone summaries, post-tool trim.

Extracted verbatim from the original monolithic ``_run_impl``.  Contains:

* ``_phase_pre_model_compact`` -- the turn-start token budget check, the
  unified compression pipeline (budget 鈫?collapse 鈫?microcompact 鈫?snip 鈫?  autocompact 鈫?milestone), the synchronous autocompact with race guard,
  and P1 milestone summarisation.
* ``_phase_post_tool_compact`` -- the post-tool compression check that runs
  after tool execution, mirroring Hermes' post-response compression.
"""

from collections.abc import AsyncGenerator
from typing import Any

from encre.logging_config import get_logger
from encre.loop.turn_ctx import TurnContext
from encre.loop_stability import should_post_tool_compact
from encre.utils.tokens import count_message_tokens, estimate_request_overhead
from encre.utils.types import CompactFailed, CompactNotification, CompactStarting

logger = get_logger(__name__)


class PhaseCompactMixin:
    """Context compaction for one ``run()`` invocation."""

    async def _phase_pre_model_compact(
        self, t: TurnContext,
    ) -> AsyncGenerator[Any, None]:
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

        # 鈹€鈹€ Unified compression pipeline 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
        # Replaces the ad-hoc step1/step1a/step1b/step2 with a single
        # pipeline that runs stages in order: budget 鈫?collapse 鈫?        # microcompact 鈫?snip 鈫?autocompact (async) 鈫?milestone.
        try:
            # Include the fixed per-request overhead (system prompt + tool
            # schemas) in the autocompact trigger so it compares the REAL
            # request size against the window, not just the message list.
            _overhead = estimate_request_overhead(
                t.system_prompt, getattr(t, "backend_tools", None),
            )
            pipeline_report = await self._compaction_pipeline.run(
                context_msgs,
                backend=self.backend,
                config=self.config,
                extra_tokens=_overhead,
            )
            if pipeline_report.messages is not context_msgs:
                self.session.replace_branch_messages(
                    self.session.active_branch_id, pipeline_report.messages,
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

            # 鈹€鈹€ Synchronous autocompact (was async background) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
            # Instead of running the compact as a detached background task
            # while the model continues to generate output (which causes
            # confusing "thinking" text from the user's perspective), we
            # now run compact synchronously: signal the frontend, pause,
            # compact, then resume.  This keeps the model from generating
            # during compression and provides a clear UI indicator.
            if pipeline_report.needs_compact:
                self._compact_epoch += 1
                logger.info(
                    "[compact] starting turn=%d tokens=%dk window=%dk",
                    self.session.turn_count, est_tokens // 1000, window // 1000,
                )

                # Signal frontend that compression is starting
                yield CompactStarting()

                # Record the highest seq_in_branch at snapshot time
                # so we can detect messages added while compact ran.
                snap_max_seq = max(
                    (m.get("seq_in_branch", -1) for m in context_msgs),
                    default=-1,
                )
                self.session.set_compact_archive(context_msgs)
                await self.hook_system.emit_pre_compact(len(context_msgs), est_tokens)
                compacted = await self.compact_engine.compact(
                    context_msgs, backend=self.backend,
                    turn_count=self.session.turn_count,
                    system_prompt=t.system_prompt or "",
                    enable_caching=self.config.enable_prompt_caching,
                    session_id=self.session.id or "",
                )
                if compacted is not None:
                    # 鈹€鈹€ Race guard 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
                    # If new messages (assistant reply, tool results, etc.)
                    # were added to this branch while compact was running,
                    # preserve them by appending after the compacted result.
                    current_max_seq = self.session._branch_last_seq.get(
                        self.session.active_branch_id, -1,
                    )
                    if current_max_seq > snap_max_seq:
                        new_msgs = sorted(
                            [m for m in self.session.messages
                             if m.get("branch_id") == self.session.active_branch_id
                             and isinstance(m.get("seq_in_branch"), int)
                             and m["seq_in_branch"] > snap_max_seq],
                            key=lambda m: m["seq_in_branch"],
                        )
                        if new_msgs:
                            compacted = compacted + new_msgs
                            logger.info(
                                "[compact] race guard: appended %d msgs "
                                "(seq %d..%d) added after snapshot",
                                len(new_msgs), snap_max_seq + 1,
                                current_max_seq,
                            )
                    self.session.replace_branch_messages(self.session.active_branch_id, compacted)
                    self._compacted_this_turn = True
                    self._update_user_requirements(compacted)
                    new_tokens = count_message_tokens(compacted)
                    yield CompactNotification(
                        old_count=len(context_msgs),
                        new_count=len(compacted),
                        old_tokens=est_tokens,
                        new_tokens=new_tokens,
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
                    yield CompactFailed(reason="no compaction applied")

        except Exception as _pe:
            logger.warning("[pipeline] failed turn=%d: %s", self.session.turn_count, _pe)
            # If a CompactStarting was already sent, dismiss the indicator;
            # when none was sent this is a harmless no-op on the frontend.
            yield CompactFailed(reason=str(_pe))

        # P1: milestone summarisation.  Every MILESTONE_INTERVAL turns
        # we write a compact "milestone" into session metadata.
        try:
            await self._maybe_write_milestone(context_msgs)
        except Exception as _m_err:
            logger.warning("[milestone] failed turn=%d: %s", self.session.turn_count, _m_err)

    async def _phase_post_tool_compact(self, t: TurnContext) -> AsyncGenerator[Any, None]:
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
                # Signal the frontend before compressing.
                yield CompactStarting()
                self.session.set_compact_archive(_post_tool_msgs)
                _post_compacted = await self.compact_engine.compact(
                    _post_tool_msgs, backend=self.backend,
                    turn_count=self.session.turn_count,
                    system_prompt=t.system_prompt or "",
                    enable_caching=self.config.enable_prompt_caching,
                    session_id=self.session.id or "",
                )
                if _post_compacted is not None:
                    self.session.replace_branch_messages(
                        self.session.active_branch_id, _post_compacted
                    )
                    self._compacted_this_turn = True
                    yield CompactNotification(
                        old_count=len(_post_tool_msgs),
                        new_count=len(_post_compacted),
                        old_tokens=_pre_tokens,
                        new_tokens=count_message_tokens(_post_compacted),
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
                    yield CompactFailed(reason="no compaction applied")
            except Exception as _ptc_err:
                logger.warning("[run] post-tool compact failed: %s", _ptc_err)
                yield CompactFailed(reason=str(_ptc_err))
