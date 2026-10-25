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

"""Unified context-compression pipeline.

Replaces the ad-hoc step1/step1a/step1b/step2 inline in loop.py with
a single :class:`CompactionPipeline`.  Stages run synchronously in
pipeline order, each feeding its transformed message list into the next,
so budget/collapse/microcompact/snip effects actually reach the API call.
Autocompact itself is triggered as a synchronous step in the caller.

Evidence markers contributed by the loop (verification evidence, plan
state) can be passed via ``protected_texts`` and survive the
deterministic stages verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.compact.pipeline")


@dataclass
class StageResult:
    name: str
    msgs_before: int = 0
    msgs_after: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    did_work: bool = False
    detail: str = ""


#: Stages that only *decide* whether a later, separate compaction should run.
#: They never rewrite the message list, so ``did_work`` on them must not be
#: read as "the messages changed".
_DECISION_ONLY_STAGES = frozenset({"autocompact"})


@dataclass
class PipelineReport:
    stages: list[StageResult] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    needs_compact: bool = False
    compact_trigger_detail: str = ""

    def add_stage(self, stage: StageResult) -> None:
        self.stages.append(stage)

    @property
    def total_tokens_saved(self) -> int:
        return sum(s.tokens_before - s.tokens_after for s in self.stages if s.did_work)

    @property
    def changed(self) -> bool:
        """Whether any rewriting stage actually modified the message list.

        This is the authoritative "did the pipeline alter the conversation?"
        signal.  ``messages`` is always *some* list -- :meth:`CompactionPipeline.run`
        builds new list objects at every stage boundary -- so callers must
        never infer change from object identity; they must ask this property.
        Reading it as identity is what made the loop rewrite the session
        branch (and rebind ``session.messages``) on every single turn even
        when nothing was compacted.

        The decision-only stages (:data:`_DECISION_ONLY_STAGES`) are excluded:
        ``autocompact`` merely reports that a separate compaction *should* run
        and leaves the list untouched.
        """
        return any(
            s.did_work for s in self.stages if s.name not in _DECISION_ONLY_STAGES
        )


class CompactionPipeline:
    """Unified compression pipeline.

    Usage::

        pipeline = CompactionPipeline(compact_engine)
        report = await pipeline.run(messages, backend=backend, config=config)
        if report.needs_compact:
            # schedule async compact task (caller's responsibility)
    """

    def __init__(self, compact_engine: Any = None) -> None:
        # Share the caller's CompactEngine: circuit-breaker counters,
        # cooldowns and the pre-compact cache live on the engine, so a
        # privately-constructed second instance would split-brain those
        # against the loop's own engine.  When omitted, an engine is
        # lazily constructed for standalone use.
        self._compact_engine: Any = compact_engine

    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        backend: Any,
        config: Any,
        extra_tokens: int = 0,
        compact_engine: Any = None,
        protected_texts: list[str] | None = None,
    ) -> PipelineReport:
        """Run the compression pipeline.

        ``extra_tokens`` is the fixed per-request overhead (system prompt +
        tool schemas) added to the autocompact trigger check so it compares
        the REAL request size against the window, not just the messages.

        ``compact_engine`` optionally injects the caller's existing
        :class:`~encre.compact.engine.CompactEngine` so the pipeline stages
        share its state with the rest of the loop.

        ``protected_texts`` carries core-contributed evidence markers
        (verification evidence, plan state): tool results containing any of
        them survive microcompact verbatim.
        """
        if compact_engine is not None:
            self._compact_engine = compact_engine
        report = PipelineReport(messages=list(messages))
        window = backend.context_window_size() if backend else 128_000
        est_tokens = _count_tokens(messages)

        # Stage 0: tool result budget
        if getattr(config, "enable_tool_result_budget", True):
            s, messages = await self._stage_budget(messages, config, est_tokens)
            report.add_stage(s)
            if s.did_work and s.tokens_after < est_tokens:
                est_tokens = s.tokens_after

        # Stage 1: context collapse (deterministic, LLM-free)
        if getattr(config, "enable_context_collapse", True):
            s, messages = self._stage_collapse(messages, est_tokens)
            report.add_stage(s)
            if s.did_work:
                est_tokens = s.tokens_after

        # Stage 2: microcompact
        s, messages = await self._stage_microcompact(
            messages, window, est_tokens, protected_texts=protected_texts,
        )
        report.add_stage(s)
        if s.did_work:
            est_tokens = s.tokens_after

        # Stage 3: snip
        if getattr(config, "enable_snip_compact", True):
            s, messages = await self._stage_snip(messages, window, est_tokens)
            report.add_stage(s)
            if s.did_work:
                est_tokens = s.tokens_after
        else:
            report.add_stage(StageResult(name="snip", did_work=False, detail="disabled"))

        # Stage 4: autocompact check (trigger only, actual compact runs async)
        # Use the real output slot budget (config.max_tokens) rather than the
        # hard-coded 32k default so the trigger matches what the backend will
        # actually reserve for output.
        slot_budget = getattr(config, "max_tokens", None) or getattr(config, "max_output_tokens", 32_768)
        s = self._stage_compact_check(
            messages, window, est_tokens,
            max_output_tokens=slot_budget, extra_tokens=extra_tokens,
        )
        report.add_stage(s)
        if s.did_work:
            report.needs_compact = True
            report.compact_trigger_detail = s.detail

        report.messages = messages
        return report

    async def _stage_budget(
        self,
        messages: list[dict[str, Any]],
        config: Any,
        est_tokens: int,
    ) -> tuple[StageResult, list[dict[str, Any]]]:
        from encre.tool_output_store import apply_tool_result_budget

        max_per = getattr(config, "max_tool_result_chars", 20_000)
        max_agg = getattr(config, "aggregate_tool_budget", 500_000)
        new_msgs = apply_tool_result_budget(
            messages,
            max_per_result=max_per,
            max_aggregate=max_agg,
        )
        new_tokens = _count_tokens(new_msgs)
        return (
            StageResult(
                name="tool_result_budget",
                msgs_before=len(messages),
                msgs_after=len(new_msgs),
                tokens_before=est_tokens,
                tokens_after=new_tokens,
                did_work=(new_tokens < est_tokens),
                detail=f"per={max_per} agg={max_agg}",
            ),
            new_msgs,
        )

    def _stage_collapse(
        self,
        messages: list[dict[str, Any]],
        est_tokens: int,
    ) -> tuple[StageResult, list[dict[str, Any]]]:
        try:
            from encre.compact.loop_state.collapse import collapse_old_tool_outputs, count_collapsed

            collapsed = collapse_old_tool_outputs(messages)
            n_col = count_collapsed(collapsed)
            if n_col:
                new_tokens = _count_tokens(collapsed)
                return (
                    StageResult(
                        name="context_collapse",
                        msgs_before=len(messages),
                        msgs_after=len(collapsed),
                        tokens_before=est_tokens,
                        tokens_after=new_tokens,
                        did_work=True,
                        detail=f"stubbed {n_col} old tool outputs",
                    ),
                    collapsed,
                )
            return (
                StageResult(
                    name="context_collapse",
                    msgs_before=len(messages),
                    msgs_after=len(messages),
                    tokens_before=est_tokens,
                    tokens_after=est_tokens,
                    did_work=False,
                ),
                messages,
            )
        except Exception as exc:
            logger.warning("[pipeline] context_collapse failed: %s", exc)
            return (
                StageResult(name="context_collapse", did_work=False, detail=f"error: {exc}"),
                messages,
            )

    async def _stage_microcompact(
        self,
        messages: list[dict[str, Any]],
        window: int,
        est_tokens: int,
        protected_texts: list[str] | None = None,
    ) -> tuple[StageResult, list[dict[str, Any]]]:
        engine = self._get_compact_engine()
        if engine.should_microcompact(messages, window):
            micro = await engine.microcompact(
                messages, window, protected_texts=protected_texts,
            )
            # microcompact truncates content in place without dropping
            # messages, so message count is not a work signal -- compare
            # token estimates before/after instead.
            new_tokens = _count_tokens(micro)
            if new_tokens < est_tokens:
                return (
                    StageResult(
                        name="microcompact",
                        msgs_before=len(messages),
                        msgs_after=len(micro),
                        tokens_before=est_tokens,
                        tokens_after=new_tokens,
                        did_work=True,
                    ),
                    micro,
                )
        return (
            StageResult(
                name="microcompact",
                msgs_before=len(messages),
                msgs_after=len(messages),
                tokens_before=est_tokens,
                tokens_after=est_tokens,
                did_work=False,
            ),
            messages,
        )

    async def _stage_snip(
        self,
        messages: list[dict[str, Any]],
        window: int,
        est_tokens: int,
    ) -> tuple[StageResult, list[dict[str, Any]]]:
        try:
            from encre.compact.engine import EncreSnipStrategy

            snipper = EncreSnipStrategy()
            if await snipper.should_compact(messages, window):
                snipped = await snipper.compact(messages, window)
                new_tokens = _count_tokens(snipped)
                return (
                    StageResult(
                        name="snip",
                        msgs_before=len(messages),
                        msgs_after=len(snipped),
                        tokens_before=est_tokens,
                        tokens_after=new_tokens,
                        did_work=True,
                        detail=f"msgs {len(messages)}->{len(snipped)}",
                    ),
                    snipped,
                )
            return (StageResult(name="snip", did_work=False), messages)
        except Exception as exc:
            logger.warning("[pipeline] snip failed: %s", exc)
            return (
                StageResult(name="snip", did_work=False, detail=f"error: {exc}"),
                messages,
            )

    def _stage_compact_check(
        self,
        messages: list[dict[str, Any]],
        window: int,
        est_tokens: int,
        max_output_tokens: int = 32_768,
        extra_tokens: int = 0,
    ) -> StageResult:
        engine = self._get_compact_engine()
        if engine.should_compact(
            messages, window,
            max_output_tokens=max_output_tokens, extra_tokens=extra_tokens,
        ):
            return StageResult(
                name="autocompact",
                did_work=True,
                detail=f"triggered tokens={est_tokens}+{extra_tokens} window={window}",
            )
        return StageResult(name="autocompact", did_work=False)

    def _get_compact_engine(self):
        if self._compact_engine is None:
            from encre.compact.engine import CompactEngine

            self._compact_engine = CompactEngine()
        return self._compact_engine


def _count_tokens(messages: list[dict[str, Any]]) -> int:
    try:
        from encre.utils.tokens import count_message_tokens

        return count_message_tokens(messages or [])
    except Exception:
        return sum(len(str(m.get("content", ""))) for m in (messages or [])) // 4
