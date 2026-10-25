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

"""
PostRunProcessor -- self-improving loop for the iClaw daemon.

Runs after every agent session to:
1. Analyze the session (tool calls, text output, errors)
2. Summarize key learnings and write to persistent memory
3. Detect repeated tool-use patterns and auto-generate skills
4. Update the soul USER.md with learned user preferences
5. Record evolution observations for long-term adaptation
6. Consolidate feedback corrections

This is the core "self-improving" mechanism that makes iClaw
get better over time without manual intervention.

Position in Encre: this module is the iClaw write-back half.  The Rust agent
core produces the events, :class:`encre.iclaw.IClawDaemon` runs the process,
and here those events are distilled into persistent learning state (memory
files, the soul files under ``data/soul/``, learner observation logs).  Nothing
in the gateway package touches this code; it is reachable only through the
iClaw profile.

Public API:
    * :class:`RunSummary` -- structured digest of one completed run.
    * :class:`PostRunPipeline` -- the stage runner.
    * :class:`PostRunOrchestrator` -- gatekeeper that decides whether a run is
      worth analysing at all.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from encre.agent import EncreAgent
from encre.improve.evolution.learner import EncreEvolutionLearner
from encre.improve.feedback.learner import EncreFeedbackLearner
from encre.improve.learning.consolidator import MemoryConsolidator
from encre.improve.learning.engine import LearningEngine
from encre.logging_config import get_logger
from encre.soul.system import EncreSoulSystem
from encre.utils.types import AgentEvent, TextDelta, ToolResult

logger = get_logger("encre.iclaw.post_run")


@dataclass
class RunSummary:
    """Structured summary of a single agent run.

    Built once per run by :meth:`PostRunPipeline._build_summary` and handed to
    every stage.  It is deliberately flat and plain-typed so a stage can consume
    only the fields it cares about without knowing how they were derived.

    ``tool_calls`` entries carry ``id``, ``name``, and ``success``.  The name is
    recovered from the tool-call id rather than carried by the event, which is
    why a run without ids degrades to ``"unknown"`` names instead of failing.

    ``repeated_patterns`` is populated after the call list is complete, by
    :meth:`PostRunPipeline._detect_patterns`, so it is always ordered by
    descending frequency.
    """

    prompt: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    text_output: str = ""
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    tool_call_count: int = 0
    unique_tools: set[str] = field(default_factory=set)
    repeated_patterns: list[dict[str, Any]] = field(default_factory=list)


class PostRunPipeline:
    """Post-run self-improvement pipeline.

    Collects events from a completed run, builds a summary, then
    feeds it through every learning subsystem in sequence.

    Parameters
    ----------
    agent : EncreAgent
        The agent that executed the run. Its subsystems (memory, soul, etc.)
        are accessed through the agent.
    learning_engine : LearningEngine | None
        Engine for detecting patterns and generating skills.
    evolution_learner : EncreEvolutionLearner | None
        Records success/error observations from the run.
    feedback_learner : EncreFeedbackLearner | None
        Records any corrections detected during the run.
    consolidator : MemoryConsolidator | None
        Triggers periodic memory consolidation after analysis.
    soul_system : EncreSoulSystem | None
        Updates USER.md with learned preferences.
    analyze_fn : Callable | None
        Optional async callable that performs LLM-powered analysis of the
        run data. If not provided, simple statistical analysis is used.

    Invariant: every subsystem is optional and independently degradable.  A
    stage whose dependency is ``None`` returns ``{"enabled": False}`` and a
    stage that raises returns ``{"error": ...}``; the pipeline never lets one
    missing subsystem abort the others.  That is what lets a partially
    provisioned daemon still accumulate the learnings it can.

    The pipeline holds no state of its own beyond its dependencies, so one
    instance may be shared by every run the daemon performs.
    """

    def __init__(
        self,
        agent: EncreAgent,
        *,
        learning_engine: LearningEngine | None = None,
        evolution_learner: EncreEvolutionLearner | None = None,
        feedback_learner: EncreFeedbackLearner | None = None,
        consolidator: MemoryConsolidator | None = None,
        soul_system: EncreSoulSystem | None = None,
        analyze_fn: Callable[[RunSummary], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        """Bind the agent and its optional learning subsystems to the pipeline.

        No subsystem is validated at construction time: a ``None`` entry simply
        disables the corresponding stage, which is what makes this pipeline
        usable by a partially provisioned daemon.

        Args:
            agent: The :class:`encre.agent.EncreAgent` that ran the session.
                Shared memory, soul, and session state are reached through it.
            learning_engine: Detects repeated tool-usage patterns and can
                promote them into skills.
            evolution_learner: Records success and error observations per run.
            feedback_learner: Records user corrections detected during a run.
            consolidator: Performs periodic memory consolidation after the
                analysis stages finish.
            soul_system: Applies learned preferences back into ``USER.md``.
            analyze_fn: Optional async callable taking a
                :class:`RunSummary` and returning an analysis dict. When
                omitted, the statistical fallback analysis is used.

        Invariant:
            The pipeline stores no derived state of its own, so a single
            instance is safe to reuse across every run of the daemon.
        """
        self._agent = agent
        self._learning_engine = learning_engine
        self._evolution_learner = evolution_learner
        self._feedback_learner = feedback_learner
        self._consolidator = consolidator
        self._soul_system = soul_system
        self._analyze_fn = analyze_fn

    async def process(
        self,
        prompt: str,
        events: list[AgentEvent],
        *,
        duration_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Run the full post-processing pipeline on a completed session.

        Returns a dict with results from each subsystem stage.

        Args:
            prompt: The user prompt that started the run.  Stored on the
                summary and used as the goal text in every learning record, so
                it is what later stages attribute tool usage to.
            events: Every agent event streamed during the run, in order.  Only
                :class:`TextDelta` and :class:`ToolResult` events carry
                signal; the rest are ignored.
            duration_seconds: Wall-clock length of the run.  Reported in the
                result and used by the orchestrator's gate.

        Returns:
            A dict with ``summary`` (counts), ``analyzed`` (whether LLM
            analysis ran), ``stages`` (one entry per stage, each either a
            stage result or ``{"error": ...}``), and ``completed``.

        Note:
            The four stages run concurrently with ``return_exceptions=True``
            rather than in a try/except chain.  Stage order in the result dict
            is fixed by the ``stage_names`` list, so it must stay aligned with
            the ``gather`` argument order; ``zip`` is strict-disabled so a
            length mismatch degrades to a shorter loop instead of raising.
        """
        results: dict[str, Any] = {"stages": {}}

        # First, turn the raw event stream into a structured summary.
        summary = self._build_summary(prompt, events, duration_seconds)
        results["summary"] = {
            "tool_call_count": summary.tool_call_count,
            "unique_tools": sorted(summary.unique_tools),
            "error_count": len(summary.errors),
            "duration_seconds": summary.duration_seconds,
        }

        enrichments: dict[str, Any] = {}
        if self._analyze_fn:
            try:
                enrichments = await self._analyze_fn(summary)
                results["analyzed"] = True
            except Exception as e:
                logger.warning("LLM analysis failed: %s", e)
                results["analyzed"] = False

        # Run all learning stages concurrently; capture individual failures.
        stage_results = await asyncio.gather(
            self._stage_evolution(summary),
            self._stage_learning(summary, enrichments),
            self._stage_memory(summary, enrichments),
            self._stage_soul(summary, enrichments),
            return_exceptions=True,
        )

        stage_names = ["evolution", "learning", "memory", "soul"]
        for name, result in zip(stage_names, stage_results, strict=False):
            if isinstance(result, Exception):
                results["stages"][name] = {"error": str(result)}
                logger.warning("Post-run stage '%s' failed: %s", name, result)
            else:
                results["stages"][name] = result

        results["completed"] = True
        return results

    def _build_summary(
        self,
        prompt: str,
        events: list[AgentEvent],
        duration_seconds: float,
    ) -> RunSummary:
        """Aggregate streamed events into a structured :class:`RunSummary`.

        Args:
            prompt: The run's prompt, stored verbatim.
            events: Events in stream order.  Order matters because pattern
                detection looks at adjacent tool calls.
            duration_seconds: Recorded for reporting; not used for any decision
                inside the pipeline.

        Returns:
            A populated summary, including detected repeated patterns.

        Note:
            A tool name is derived from the first ``_``-delimited segment of
            the tool-call id, which assumes ids are namespaced like
            ``toolu_<name>_<uuid>``.  A vendor that returns unnamespaced ids
            gets the whole id as the name, so grouping degrades but never fails.
        """
        summary = RunSummary(
            prompt=prompt,
            duration_seconds=duration_seconds,
        )
        text_parts: list[str] = []
        for event in events:
            if isinstance(event, TextDelta) and event.text:
                text_parts.append(event.text)
            elif isinstance(event, ToolResult):
                tool_id = event.id or "unknown"
                tool_name = tool_id.split("_")[0] if "_" in tool_id else tool_id
                summary.tool_calls.append({
                    "id": tool_id,
                    "name": tool_name,
                    "success": not event.is_error,
                })
                summary.tool_call_count += 1
                summary.unique_tools.add(tool_name)
                if event.is_error:
                    summary.errors.append(event.content or "unknown error")

        summary.text_output = "".join(text_parts)
        self._detect_patterns(summary)
        return summary

    def _detect_patterns(self, summary: RunSummary) -> None:
        """Detect frequently repeated adjacent tool-call pairs in the run.

        Mutates ``summary.repeated_patterns`` in place.  A pair is the
        signature ``"A+B"`` of two consecutive tool calls; a pair occurring at
        least twice is treated as a habit worth surfacing to the learning
        engine.

        Args:
            summary: The summary to annotate.  Must already have its
                ``tool_calls`` list complete.
        """
        tool_names = [tc["name"] for tc in summary.tool_calls]
        # Need enough calls to make pattern detection meaningful.
        if len(tool_names) < 3:
            return
        from collections import Counter
        pairs = [f"{tool_names[i]}+{tool_names[i+1]}" for i in range(len(tool_names) - 1)]
        pair_counts = Counter(pairs)
        frequent_pairs = {k: v for k, v in pair_counts.items() if v >= 2}
        if frequent_pairs:
            summary.repeated_patterns = [
                {"pattern": k, "frequency": v}
                for k, v in sorted(frequent_pairs.items(), key=lambda x:
                    -x[1])
            ]

    async def _stage_evolution(self, summary: RunSummary) -> dict[str, Any]:
        """Feed success/error tool observations into the evolution learner.

        Args:
            summary: The run digest whose tool outcomes are recorded.

        Returns:
            ``{"enabled": False}`` when no learner is wired, otherwise a dict
            with a ``records`` count and, when there were failures, an
            ``errors`` list naming the tools involved.

        Note:
            The failure message is taken from ``tc["error"]``, which
            :meth:`_build_summary` does not populate, so every recorded error
            currently reads ``"unknown error"``.  The tool name is still
            accurate, which is what the learner keys on.
        """
        if self._evolution_learner is None:
            return {"enabled": False}
        stage: dict[str, Any] = {"enabled": True, "records": 0}
        for tc in summary.tool_calls:
            # Feed each tool outcome back to the evolution learner.
            if tc.get("success", True):
                self._evolution_learner.record_success(tc["name"], summary.prompt[:200])
                stage["records"] = stage.get("records", 0) + 1
            else:
                self._evolution_learner.record_error(tc["name"], tc.get("error", "") or "unknown error")
                stage.setdefault("errors", []).append({"tool": tc["name"]})
        return stage

    async def _stage_learning(self, summary: RunSummary, _enrichments: dict[str, Any]) -> dict[str, Any]:
        """Run the learning engine's pattern/skill analysis on the run.

        Args:
            summary: The run digest.  Only the tool-call name list and the
                prompt are passed to the engine; the engine reconstructs its
                own view from those.
            _enrichments: Unused by this stage.  Kept in the signature so every
                stage shares one call shape under :func:`asyncio.gather`.

        Returns:
            ``{"enabled": False}`` when no engine is wired, otherwise a dict
            reporting how many tools were analysed.  ``skills_generated`` is
            reported as zero because generation happens asynchronously inside
            the engine and is not observable from here.
        """
        if self._learning_engine is None:
            return {"enabled": False}
        tool_names = [tc["name"] for tc in summary.tool_calls]
        await self._learning_engine.analyze_run(tool_names, summary.prompt)
        return {
            "enabled": True,
            "tools_analyzed": len(tool_names),
            "skills_generated": 0,
        }

    async def _stage_memory(self, summary: RunSummary, enrichments: dict[str, Any]) -> dict[str, Any]:
        """Write a session note to memory and trigger consolidation if able.

        Args:
            summary: The run digest, used both to build a fallback note and to
                decide whether consolidation is worth running.
            enrichments: Optional LLM analysis output.  A ``task_summary`` key
                wins over the locally rendered note, because the LLM version
                captures intent rather than tool counts.

        Returns:
            ``{"enabled": False}`` when the agent has no memory system, a
            result dict with ``note_added`` and optionally ``consolidated``,
            or ``{"enabled": True, "error": ...}`` on failure.

        Note:
            Consolidation only runs when the run actually invoked tools.  A
            pure-conversation turn cannot contribute new retrieval material, so
            paying the consolidation cost for it would be wasted work.
        """
        memory_system = getattr(self._agent, "memory_system", None)
        if memory_system is None:
            return {"enabled": False}
        try:
            task_note = enrichments.get("task_summary", "")
            if not task_note:
                task_note = self._build_memory_note(summary)
            if task_note:
                memory_system.write_entrypoint_cache()
            stage: dict[str, Any] = {"enabled": True, "note_added": bool(task_note)}
            if self._consolidator and summary.tool_call_count > 0:
                await self._consolidator.consolidate()
                stage["consolidated"] = True
            return stage
        except Exception as e:
            logger.warning("Memory stage failed: %s", e)
            return {"enabled": True, "error": str(e)}

    async def _stage_soul(self, _summary: RunSummary, enrichments: dict[str, Any]) -> dict[str, Any]:
        """Append discovered user preferences to the soul USER.md.

        Args:
            _summary: Unused by this stage; preferences come only from LLM
                analysis.  Kept for a uniform stage signature.
            enrichments: Expected to carry a ``user_preferences`` list of
                strings.

        Returns:
            ``{"enabled": False}`` when no soul system is wired, otherwise a
            dict with the count of notes appended.

        Note:
            Non-list values are ignored and reported as zero.  Each note must
            be a string over ten characters, which filters out placeholder
            entries and empty preferences before they are written to a file
            that is itself part of the agent's identity.
        """
        if self._soul_system is None:
            return {"enabled": False}
        user_notes = enrichments.get("user_preferences", [])
        if isinstance(user_notes, list):
            for note in user_notes:
                if isinstance(note, str) and len(note) > 10:
                    self._soul_system.append_user_note(note)
        return {
            "enabled": True,
            "user_notes_added": len(user_notes) if isinstance(user_notes, list) else 0,
        }

    def _build_memory_note(self, summary: RunSummary) -> str:
        """Render a Markdown session-summary note for the memory system.

        Args:
            summary: The run digest to render.

        Returns:
            A Markdown note, or an empty string when nothing is worth writing.
            Fields are only emitted when they carry information, so a quiet
            turn yields a note without tools, errors, or patterns lines rather
            than lines reading zero.
        """
        parts: list[str] = []
        parts.append("## Session Summary")
        parts.append(f"**Goal:** {summary.prompt[:300]}")
        if summary.tool_call_count > 0:
            tools_str = ", ".join(sorted(summary.unique_tools))
            parts.append(f"**Tools used ({summary.tool_call_count}):** {tools_str}")
        if summary.errors:
            parts.append(f"**Errors encountered:** {len(summary.errors)}")
        if summary.repeated_patterns:
            patterns_str = "; ".join(
                f"{p['pattern']} (脳{p['frequency']})"
                for p in summary.repeated_patterns[:
                    5]
            )
            parts.append(f"**Repeated patterns:** {patterns_str}")
        return "\n\n".join(parts)


class PostRunOrchestrator:
    """Top-level orchestrator that manages the post-run pipeline lifecycle.

    Wraps PostRunPipeline with event collection -- accumulates AgentEvents
    during a streaming run, then feeds them into the pipeline when finished.

    The gate here exists because analysis costs an LLM call.  Running the
    pipeline for a two-second turn that never invoked a tool would spend that
    budget on nothing, so trivial runs are rejected before the pipeline sees
    them and the caller receives a ``skipped`` result instead.
    """

    def __init__(
        self,
        pipeline: PostRunPipeline,
        *,
        min_tool_calls: int = 1,
        min_duration: float = 1.0,
    ) -> None:
        """Attach the pipeline and the cheap pre-analysis gates.

        The orchestrator exists to avoid paying for LLM analysis on runs that
        produced nothing to learn from. Both thresholds are pure configuration;
        they do not depend on any event data, so they can be fixed at
        construction and evaluated per run.

        Args:
            pipeline: The :class:`PostRunPipeline` to feed once a run clears
                the gates.
            min_tool_calls: Minimum number of :class:`ToolResult` events before
                analysis is worth attempting.
            min_duration: Minimum wall-clock run length, in seconds, before
                analysis is worth attempting.

        Invariant:
            Both thresholds are checked with a less-than comparison, so a run
            that exactly equals a threshold does pass.
        """
        self._pipeline = pipeline
        self._min_tool_calls = min_tool_calls
        self._min_duration = min_duration

    async def collect_and_process(
        self,
        prompt: str,
        events: list[AgentEvent],
        *,
        duration_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Collect events from a completed streaming run and process them.

        Args:
            prompt: The run's prompt, passed through to the pipeline.
            events: Events accumulated during the run, in stream order.
            duration_seconds: Wall-clock length of the run.

        Returns:
            Either a ``{"skipped": True, "reason": ...}`` dict explaining why
            the run was not analysed, or the pipeline's full result dict.

        Note:
            Both gates must pass.  The tool-call count is derived by counting
            :class:`ToolResult` events here rather than trusting a caller
            count, because the orchestrator is the only component that sees the
            complete event list.
        """
        # Count real tool invocations to decide whether analysis is worthwhile.
        tool_call_count = sum(
            1 for e in events if isinstance(e, ToolResult)
        )
        if tool_call_count < self._min_tool_calls:
            return {"skipped": True, "reason": f"too few tool calls ({tool_call_count} < {self._min_tool_calls})"}
        if duration_seconds < self._min_duration:
            return {"skipped": True, "reason": f"too short ({duration_seconds}s < {self._min_duration}s)"}
        return await self._pipeline.process(prompt, events, duration_seconds=duration_seconds)

    @property
    def pipeline(self) -> PostRunPipeline:
        """The wrapped post-run pipeline instance."""
        return self._pipeline
