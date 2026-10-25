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

"""Helpers for benchmark task scoring, diagnosis, and reporting."""

from collections import Counter, defaultdict
from typing import Any


def normalize_task(task: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized benchmark task dictionary."""
    normalized = dict(task)
    normalized.setdefault("id", "")
    normalized.setdefault("track", "general")
    normalized.setdefault("category", "general")
    normalized.setdefault("difficulty", "medium")
    normalized.setdefault("prompt", "")
    normalized.setdefault("expected_stage", "")
    normalized.setdefault("preferred_tools", [])
    normalized.setdefault("must_include", [])
    normalized.setdefault("any_of", [])
    normalized.setdefault("must_exclude", [])
    normalized.setdefault("tags", [])
    normalized.setdefault("max_turn_count", 0)
    normalized.setdefault("max_tool_calls", 0)
    return normalized


def score_task_output(task: dict[str, Any], text: str) -> dict[str, Any]:
    """Score output using simple token expectations.

    Supported task keys:
    - ``must_include``: all tokens must appear
    - ``any_of``: at least one token must appear, if provided
    - ``must_exclude``: no token may appear
    """
    normalized = normalize_task(task)
    text_lower = (text or "").lower()
    required = [str(x).lower() for x in normalized["must_include"]]
    any_of = [str(x).lower() for x in normalized["any_of"]]
    forbidden = [str(x).lower() for x in normalized["must_exclude"]]

    matched = [token for token in required if token in text_lower]
    missing = [token for token in required if token not in matched]
    any_of_matched = [token for token in any_of if token in text_lower]
    forbidden_found = [token for token in forbidden if token in text_lower]

    coverage_checks = 0
    coverage_points = 0.0
    if required:
        coverage_checks += len(required)
        coverage_points += len(matched)
    if any_of:
        coverage_checks += 1
        coverage_points += 1.0 if any_of_matched else 0.0
    if forbidden:
        coverage_checks += 1
        coverage_points += 1.0 if not forbidden_found else 0.0

    coverage = coverage_points / coverage_checks if coverage_checks else 1.0
    # A task passes only when every required token is present, an any_of
    # token (if any) matched, and nothing forbidden was produced.
    passed = not missing and (not any_of or bool(any_of_matched)) and not forbidden_found
    return {
        "passed": passed,
        "coverage": coverage,
        "matched": matched,
        "required": required,
        "missing": missing,
        "any_of": any_of,
        "any_of_matched": any_of_matched,
        "must_exclude": forbidden,
        "forbidden_found": forbidden_found,
    }


def classify_benchmark_result(task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Assign a primary failure bucket to a benchmark result."""
    normalized = normalize_task(task)
    score = result.get("score", {}) or {}
    finish_reason = str(result.get("finish_reason", "") or "")
    final_text = str(result.get("final_text", "") or "").strip()
    tool_calls = int(result.get("tool_calls", 0) or 0)
    tool_error_count = int(result.get("tool_error_count", 0) or 0)
    turn_count = int(result.get("turn_count", 0) or 0)
    stuck_event_count = int(result.get("stuck_event_count", 0) or 0)
    stage = str(result.get("task_stage", "") or "")
    stage_history = [str(x).lower() for x in result.get("task_stage_history", []) or []]
    expected_stage = str(normalized.get("expected_stage", "") or "").lower()
    preferred_tools = [str(x) for x in normalized.get("preferred_tools", []) or []]
    used_tools = [str(x) for x in result.get("tool_names", []) or []]
    max_turn_count = int(normalized.get("max_turn_count", 0) or 0)
    max_tool_calls = int(normalized.get("max_tool_calls", 0) or 0)

    # Default to "passed"; each subsequent check may re-classify the failure
    # into a more specific bucket, most severe first.
    bucket = "passed"
    signals: list[str] = []

    if result.get("passed"):
        return {"bucket": bucket, "signals": signals}

    if not final_text:
        bucket = "no_final_answer"
        signals.append("assistant produced no final text")
    elif finish_reason in {"error", "max_tokens", "budget_exceeded", "cancelled"}:
        bucket = "runtime_or_budget_failure"
        signals.append(f"finish_reason={finish_reason}")
    elif preferred_tools and tool_calls == 0:
        bucket = "tool_selection_gap"
        signals.append("no tool calls despite preferred tools")
    elif preferred_tools:
        missing_tools = [name for name in preferred_tools if name not in used_tools]
        if missing_tools and score.get("coverage", 0.0) < 0.75:
            bucket = "tool_selection_gap"
            signals.append(f"missing preferred tools: {', '.join(missing_tools)}")
    if bucket == "passed" and tool_error_count > 0 and score.get("coverage", 0.0) < 1.0:
        bucket = "error_recovery_gap"
        signals.append(f"tool errors observed: {tool_error_count}")
    if bucket == "passed" and expected_stage:
        observed = set(stage_history + ([stage.lower()] if stage else []))
        if expected_stage not in observed:
            bucket = "workflow_control_gap"
            signals.append(f"expected stage '{expected_stage}' was not reached")
    if bucket == "passed" and max_tool_calls and tool_calls > max_tool_calls:
        bucket = "tool_overuse"
        signals.append(f"tool_calls={tool_calls} exceeded budget={max_tool_calls}")
    if bucket == "passed" and max_turn_count and turn_count > max_turn_count:
        bucket = "long_horizon_drift"
        signals.append(f"turn_count={turn_count} exceeded budget={max_turn_count}")
    if bucket == "passed" and stuck_event_count > 0:
        bucket = "long_horizon_drift"
        signals.append(f"stuck_event_count={stuck_event_count}")
    if bucket == "passed":
        coverage = float(score.get("coverage", 0.0) or 0.0)
        if coverage < 0.35:
            bucket = "task_understanding_gap"
            signals.append(f"low output coverage={coverage:.2f}")
        else:
            bucket = "output_incomplete"
            signals.append(f"missing tokens={len(score.get('missing', []))}")

    return {"bucket": bucket, "signals": signals}


def summarize_benchmark_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate benchmark results into a report-friendly summary."""
    # Pre-compute the overall pass count and the average output coverage.
    total = len(results)
    passed = sum(1 for item in results if item.get("passed"))
    by_category: dict[str, dict[str, Any]] = {}
    by_track: dict[str, dict[str, Any]] = {}
    by_difficulty: dict[str, dict[str, Any]] = {}
    bucket_counts = Counter(
        str((item.get("diagnosis", {}) or {}).get("bucket", "unknown")) for item in results
    )
    tag_counts: Counter[str] = Counter()
    avg_coverage = 0.0
    if total:
        avg_coverage = sum(
            float((item.get("score", {}) or {}).get("coverage", 0.0) or 0.0)
            for item in results
        ) / total

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    grouped_tracks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    grouped_difficulties: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        grouped[str(item.get("category", "general"))].append(item)
        grouped_tracks[str(item.get("track", "general"))].append(item)
        grouped_difficulties[str(item.get("difficulty", "medium"))].append(item)
        for tag in item.get("tags", []) or []:
            tag_counts[str(tag)] += 1

    def _group_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute aggregate pass-rate and latency stats for a group of tasks."""
        count = len(items)
        ok = sum(1 for row in items if row.get("passed"))
        avg_first_model_event_ms = (
            sum(float(row.get("first_model_event_ms", 0) or 0) for row in items) / count
        ) if count else 0.0
        avg_model_total_ms = (
            sum(float(row.get("model_total_ms", 0) or 0) for row in items) / count
        ) if count else 0.0
        avg_prompt_build_ms = (
            sum(float(row.get("prompt_build_ms", 0) or 0) for row in items) / count
        ) if count else 0.0
        return {
            "task_count": count,
            "passed": ok,
            "failed": count - ok,
            "pass_rate": (ok / count) if count else 0.0,
            "avg_duration_ms": (
                sum(float(row.get("duration_ms", 0) or 0) for row in items) / count
            ) if count else 0.0,
            "avg_tool_calls": (
                sum(int(row.get("tool_calls", 0) or 0) for row in items) / count
            ) if count else 0.0,
            "avg_turn_count": (
                sum(int(row.get("turn_count", 0) or 0) for row in items) / count
            ) if count else 0.0,
            "avg_prompt_build_ms": avg_prompt_build_ms,
            "avg_first_model_event_ms": avg_first_model_event_ms,
            "avg_model_total_ms": avg_model_total_ms,
        }

    for category, items in grouped.items():
        by_category[category] = _group_summary(items)
    for track, items in grouped_tracks.items():
        by_track[track] = _group_summary(items)
    for difficulty, items in grouped_difficulties.items():
        by_difficulty[difficulty] = _group_summary(items)

    return {
        "task_count": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": (passed / total) if total else 0.0,
        "avg_duration_ms": (
            sum(float(item.get("duration_ms", 0) or 0) for item in results) / total
        ) if total else 0.0,
        "avg_tool_calls": (
            sum(int(item.get("tool_calls", 0) or 0) for item in results) / total
        ) if total else 0.0,
        "avg_turn_count": (
            sum(int(item.get("turn_count", 0) or 0) for item in results) / total
        ) if total else 0.0,
        "avg_output_coverage": avg_coverage,
        "total_stuck_events": sum(int(item.get("stuck_event_count", 0) or 0) for item in results),
        "total_delegations": sum(int(item.get("delegate_count", 0) or 0) for item in results),
        "total_tool_errors": sum(int(item.get("tool_error_count", 0) or 0) for item in results),
        "failure_buckets": dict(bucket_counts),
        "by_category": by_category,
        "by_track": by_track,
        "by_difficulty": by_difficulty,
        "top_tags": tag_counts.most_common(10),
    }


def analyze_benchmark_results(results: list[dict[str, Any]], top_n: int = 5) -> dict[str, Any]:
    """Generate actionable analysis from benchmark results."""
    summary = summarize_benchmark_results(results)
    failures = [item for item in results if not item.get("passed")]

    def _sort_top(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        """Return the ``top_n`` rows ranked by *key* (descending), trimmed."""
        ranked = sorted(rows, key=lambda row: float(row.get(key, 0) or 0), reverse=True)
        return [
            {
                "id": row.get("id", ""),
                "track": row.get("track", "general"),
                "category": row.get("category", "general"),
                "difficulty": row.get("difficulty", "medium"),
                key: row.get(key, 0),
                "diagnosis": (row.get("diagnosis", {}) or {}).get("bucket", "unknown"),
            }
            for row in ranked[:top_n]
        ]

    weakest_categories = sorted(
        (
            {"name": name, **stats}
            for name, stats in summary.get("by_category", {}).items()
        ),
        key=lambda item: (float(item.get("pass_rate", 0.0)), -int(item.get("task_count", 0))),
    )[:top_n]
    weakest_tracks = sorted(
        (
            {"name": name, **stats}
            for name, stats in summary.get("by_track", {}).items()
        ),
        key=lambda item: (float(item.get("pass_rate", 0.0)), -int(item.get("task_count", 0))),
    )[:top_n]
    weakest_difficulties = sorted(
        (
            {"name": name, **stats}
            for name, stats in summary.get("by_difficulty", {}).items()
        ),
        key=lambda item: (float(item.get("pass_rate", 0.0)), -int(item.get("task_count", 0))),
    )[:top_n]

    bucket_advice = {
        "tool_selection_gap": "Prioritize tool-selection strategy and tool-discovery prompts to reduce pure-text answers when a tool should be used.",
        "error_recovery_gap": "Strengthen tool error retry, fallback, and self-repair prompts after errors.",
        "workflow_control_gap": "Add stage control and sub-task progression to avoid getting stuck in discover/plan and never reaching execute/report.",
        "long_horizon_drift": "Prioritize compressing ineffective turns, add stuck recovery and memory summaries to control long-range drift.",
        "tool_overuse": "Add tool budgets and stopping criteria to avoid searching for the sake of searching or reading for the sake of reading.",
        "runtime_or_budget_failure": "Check first token, total model time, and budget exhaustion points; handle provider/MCP/context bloat.",
        "task_understanding_gap": "Strengthen task parsing, success-criteria extraction, and output structure constraints.",
        "output_incomplete": "Add a final answer checklist to ensure the final answer covers all task requirements.",
        "no_final_answer": "Strengthen termination conditions and wrap-up strategy to avoid doing process work without delivering results.",
    }

    failure_buckets = summary.get("failure_buckets", {})
    prioritized_fixes = []
    for bucket, count in sorted(failure_buckets.items(), key=lambda item: item[1], reverse=True):
        if bucket == "passed":
            continue
        prioritized_fixes.append({
            "bucket": bucket,
            "count": count,
            "recommendation": bucket_advice.get(bucket, "Add targeted strategies and validation cases for this failure category."),
        })

    return {
        "summary": summary,
        "weakest_categories": weakest_categories,
        "weakest_tracks": weakest_tracks,
        "weakest_difficulties": weakest_difficulties,
        "top_slowest_tasks": _sort_top(results, "duration_ms"),
        "top_first_token_latency_tasks": _sort_top(results, "first_model_event_ms"),
        "top_tool_error_tasks": _sort_top(results, "tool_error_count"),
        "top_turn_heavy_tasks": _sort_top(results, "turn_count"),
        "top_tool_heavy_tasks": _sort_top(results, "tool_calls"),
        "top_stuck_tasks": _sort_top(results, "stuck_event_count"),
        "failed_tasks": [
            {
                "id": row.get("id", ""),
                "track": row.get("track", "general"),
                "category": row.get("category", "general"),
                "difficulty": row.get("difficulty", "medium"),
                "bucket": (row.get("diagnosis", {}) or {}).get("bucket", "unknown"),
                "signals": (row.get("diagnosis", {}) or {}).get("signals", []),
            }
            for row in failures[: max(top_n * 2, top_n)]
        ],
        "prioritized_fixes": prioritized_fixes[:top_n],
    }
