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

"""Tests for the benchmark evaluation suite: scoring, diagnosis, and summarization.

These tests validate how raw benchmark task results are scored against
required tokens, classified into failure buckets, and aggregated into
summaries and analyses."""

from encre.improve.eval.benchmark_suite import (
    analyze_benchmark_results,
    classify_benchmark_result,
    normalize_task,
    score_task_output,
    summarize_benchmark_results,
)


class TestBenchmarkScoring:
    """Validate the scoring engine for benchmark task outputs.

    Exercises the end-to-end scoring pipeline: token matching against
    ``must_include``/``any_of``/``must_exclude`` constraints, coverage
    calculation, and forbidden-text detection. The scorer must reject
    partial matches and attribute failures to missing or forbidden
    content so downstream diagnosis can surface the right gap.
    """

    def test_score_task_output_supports_any_of_and_excludes(self):
        """Validate that scoring accepts a response when must_include, any_of, and must_exclude constraints are all satisfied.

        The task requires both "python" and "release", accepts either
        "october" or "2024", and forbids "hallucinated". A response
        containing all required tokens, one any_of token, and no
        forbidden text must pass with full coverage and an empty
        forbidden list.
        """
        task = normalize_task({
            "id": "research-1",
            "must_include": ["python", "release"],
            "any_of": ["october", "2024"],
            "must_exclude": ["hallucinated"],
        })
        result = score_task_output(task, "Python 3.13 release landed in October 2024.")
        # Full coverage is expected because every required token matched
        # and no forbidden token was present in the response.
        assert result["passed"] is True
        assert result["coverage"] == 1.0
        assert result["forbidden_found"] == []

    def test_score_task_output_marks_missing_tokens(self):
        """Validate that scoring reports missing tokens and marks the task failed when must_include constraints are not met.

        The task requires "loop", "retry", and "test" but the response
        only contains "loop". The scorer must list the two absent tokens
        in ``missing`` and set ``passed`` to False so downstream tools
        can identify which constraints went unfulfilled.
        """
        task = normalize_task({
            "id": "coding-1",
            "must_include": ["loop", "retry", "test"],
        })
        result = score_task_output(task, "Loop behavior is described, but verification is absent.")
        # Both "retry" and "test" are absent from the response, so they
        # must appear in the missing list and the overall pass flag must
        # be False.
        assert result["passed"] is False
        assert "retry" in result["missing"]
        assert "test" in result["missing"]


class TestBenchmarkDiagnosis:
    """Validate the failure-bucket diagnosis engine for benchmark results.

    Ensures raw run metrics (tool-call counts, turn counts, coverage,
    finish reasons) are mapped to semantically meaningful diagnosis
    buckets. Correct bucket assignment lets engineers prioritize fix
    areas -- e.g. tool_selection_gap versus long_horizon_drift --
    without inspecting individual traces.
    """

    def test_classify_benchmark_result_detects_tool_selection_gap(self):
        """Validate that a task with preferred tools but zero invocations is diagnosed as a tool_selection_gap.

        Simulates a failing task where the agent never called any tools
        despite the task declaring preferred tools. Zero tool calls in
        this scenario indicate the agent failed to reach the execution
        stage or did not recognize the need for tool use, which the
        diagnosis engine should flag as a tool-selection deficiency.
        """
        task = normalize_task({
            "id": "coding-2",
            "preferred_tools": ["file_read", "grep"],
            "must_include": ["file", "function"],
        })
        result = {
            "passed": False,
            "finish_reason": "stop",
            "final_text": "You should inspect the project.",
            "tool_calls": 0,
            "tool_error_count": 0,
            "turn_count": 2,
            "stuck_event_count": 0,
            "score": {"coverage": 0.25, "missing": ["file", "function"]},
            "tool_names": [],
            "task_stage": "discover",
            "task_stage_history": ["discover"],
        }
        diagnosis = classify_benchmark_result(task, result)
        # Zero tool calls when preferred tools are declared means the
        # agent did not attempt tool selection, which maps to
        # tool_selection_gap.
        assert diagnosis["bucket"] == "tool_selection_gap"

    def test_classify_benchmark_result_detects_long_horizon_drift(self):
        """Validate that exceeding max turns with low coverage is diagnosed as long_horizon_drift.

        The result shows 11 turns against a max of 8, one stuck event,
        and only 50% coverage. This pattern -- many turns without
        reaching the target -- indicates the agent drifted through the
        horizon without converging on a complete answer, which the
        diagnosis engine should bucket as long_horizon_drift.
        """
        task = normalize_task({
            "id": "long-1",
            "max_turn_count": 8,
            "must_include": ["summary"],
        })
        result = {
            "passed": False,
            "finish_reason": "stop",
            "final_text": "Partial notes only.",
            "tool_calls": 6,
            "tool_error_count": 0,
            "turn_count": 11,
            "stuck_event_count": 1,
            "score": {"coverage": 0.5, "missing": ["summary"]},
            "tool_names": ["file_read"],
            "task_stage": "report",
            "task_stage_history": ["discover", "plan", "execute"],
        }
        diagnosis = classify_benchmark_result(task, result)
        # Surpassing the turn limit with incomplete coverage signals
        # the agent lost focus over a long horizon rather than failing
        # fast.
        assert diagnosis["bucket"] == "long_horizon_drift"


class TestBenchmarkSummary:
    """Validate the aggregation and analysis pipeline for benchmark result sets.

    Ensures raw per-task results are grouped correctly by track,
    category, and difficulty; that pass rates and latency metrics are
    computed accurately; and that the top-level analysis surfaces the
    slowest tasks and prioritized fix buckets in the expected order.
    Accurate aggregation is essential for reporting dashboards and
    regression alerts.
    """

    def test_summarize_benchmark_results_groups_tracks_and_buckets(self):
        """Validate that summary aggregation correctly groups by track, category, difficulty, and failure bucket.

        Feeds two heterogeneous results -- one passing on claude_code
        and one failing on manus with a tool_selection_gap -- and
        asserts the summary reports the correct task count, failure
        bucket count, per-track pass rate, per-category failure count,
        per-difficulty failure count, and per-track first-token-latency
        average.
        """
        results = [
            {
                "id": "a",
                "track": "claude_code",
                "category": "coding_fix",
                "difficulty": "medium",
                "tags": ["coding", "verification"],
                "passed": True,
                "duration_ms": 100,
                "tool_calls": 2,
                "turn_count": 3,
                "prompt_build_ms": 20,
                "first_model_event_ms": 40,
                "model_total_ms": 80,
                "stuck_event_count": 0,
                "delegate_count": 0,
                "tool_error_count": 0,
                "score": {"coverage": 1.0},
                "diagnosis": {"bucket": "passed"},
            },
            {
                "id": "b",
                "track": "manus",
                "category": "research_synthesis",
                "difficulty": "hard",
                "tags": ["research"],
                "passed": False,
                "duration_ms": 200,
                "tool_calls": 0,
                "turn_count": 4,
                "prompt_build_ms": 30,
                "first_model_event_ms": 90,
                "model_total_ms": 150,
                "stuck_event_count": 1,
                "delegate_count": 1,
                "tool_error_count": 1,
                "score": {"coverage": 0.25},
                "diagnosis": {"bucket": "tool_selection_gap"},
            },
        ]
        summary = summarize_benchmark_results(results)
        # Two tasks were supplied so the total count must be exactly 2.
        assert summary["task_count"] == 2
        # Exactly one task fell into the tool_selection_gap bucket.
        assert summary["failure_buckets"]["tool_selection_gap"] == 1
        # The claude_code task passed, so its pass rate must be 1.0.
        assert summary["by_track"]["claude_code"]["pass_rate"] == 1.0
        # research_synthesis has one failing task.
        assert summary["by_category"]["research_synthesis"]["failed"] == 1
        # hard-difficulty has one failing task.
        assert summary["by_difficulty"]["hard"]["failed"] == 1
        # The manus track's first-model-event latency is the raw value 90 ms.
        assert summary["by_track"]["manus"]["avg_first_model_event_ms"] == 90.0

    def test_analyze_benchmark_results_surfaces_priorities(self):
        """Validate that analysis ranks slowest tasks first and surfaces prioritized fix buckets.

        Supplies two failing tasks -- one slow runtime-or-budget failure
        and one tool-selection gap -- and asserts that the slowest-task
        list and first-token-latency list both place the slower task
        ("slow-1") at index 0, and that the first prioritized fix
        corresponds to one of the two known failure buckets.
        """
        results = [
            {
                "id": "slow-1",
                "track": "manus",
                "category": "research_synthesis",
                "difficulty": "hard",
                "tags": ["research"],
                "passed": False,
                "duration_ms": 1200,
                "tool_calls": 1,
                "turn_count": 6,
                "first_model_event_ms": 500,
                "stuck_event_count": 2,
                "tool_error_count": 1,
                "delegate_count": 0,
                "score": {"coverage": 0.25},
                "diagnosis": {"bucket": "runtime_or_budget_failure", "signals": ["finish_reason=max_tokens"]},
            },
            {
                "id": "gap-1",
                "track": "claude_code",
                "category": "coding_fix",
                "difficulty": "medium",
                "tags": ["coding"],
                "passed": False,
                "duration_ms": 800,
                "tool_calls": 0,
                "turn_count": 3,
                "first_model_event_ms": 120,
                "stuck_event_count": 0,
                "tool_error_count": 0,
                "delegate_count": 0,
                "score": {"coverage": 0.2},
                "diagnosis": {"bucket": "tool_selection_gap", "signals": ["no tool calls despite preferred tools"]},
            },
        ]
        analysis = analyze_benchmark_results(results, top_n=2)
        # "slow-1" has the highest duration and first-token latency, so
        # it must appear first in both ranked lists.
        assert analysis["top_slowest_tasks"][0]["id"] == "slow-1"
        assert analysis["top_first_token_latency_tasks"][0]["id"] == "slow-1"
        # The highest-priority fix must be one of the two failure buckets present.
        assert analysis["prioritized_fixes"][0]["bucket"] in {"runtime_or_budget_failure", "tool_selection_gap"}
