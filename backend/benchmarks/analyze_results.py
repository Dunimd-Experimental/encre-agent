#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
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

from __future__ import annotations

"""Analyze previously collected Encre benchmark results.

This is a thin command-line wrapper around
:func:`encre.improve.eval.benchmark_suite.analyze_benchmark_results`. It reads a
benchmark results JSON file (as produced by ``runner.py``), computes a gap
analysis of where the flagship agent falls short, and prints (and optionally
writes) the analysis as JSON.

It additionally exposes :func:`detect_regressions`, a per-task z-score
regression detector over the historical ``results/runs.jsonl`` stream
appended by the real-execution runner. For every task it compares the latest
run against the historical mean/std of each cost-type metric
(tokens_total/tool_calls/turns/retries/wall_time_seconds); a z-score above 2
or a drop of ``success`` from an all-``True`` history flags a regression,
and flagged cases are appended to ``results/regressions.jsonl``. LLM judge
scores are deliberately excluded from the z-score detection (their semantics
differ from the cost metrics) and are only recorded.

The per-model matrix (:func:`aggregate_by_model`, CLI ``--by-model``) groups
the same ``runs.jsonl`` stream by the ``model.id`` identity block written by
the runner and reports, per model, sample count, success rate, mean cost
metrics, and the failure-pattern split (tool errors vs. invented tool names
vs. malformed args). :func:`build_tier_suggestions` (CLI
``--tier-suggestions``) turns that matrix into the evaluation -> tier
calibration loop: models failing with *form* past light-tier thresholds are
proposed for ``capabilities.tier: light``, consistently-successful light
models are proposed for upgrade. Suggestions are written to
``results/tier_suggestions.jsonl`` and are always advisory -- a human
confirms before anything touches ``config.yaml``.

Usage:
    python analyze_results.py --input results.json [--output analysis.json]
    python analyze_results.py --regressions [--runs results/runs.jsonl]
    python analyze_results.py --by-model [--tier-suggestions] [--runs results/runs.jsonl]
"""

import argparse
import json
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from encre.improve.eval.benchmark_suite import analyze_benchmark_results

# This script intentionally stays thin: all analysis logic lives in
# ``encre.improve.eval.benchmark_suite.analyze_benchmark_results`` so it can be reused
# programmatically. Here we only handle CLI I/O and shape normalization.


# ---------------------------------------------------------------------------
# Regression detection over the runs.jsonl history (Task 7.2)
# ---------------------------------------------------------------------------

#: Cost-type metrics tracked for z-score regressions (higher = more expensive).
_COST_METRICS = ("tokens_total", "tool_calls", "turns", "retries", "wall_time_seconds")
#: z-score above which a cost-type metric is flagged.
_Z_THRESHOLD = 2.0
#: Minimum number of historical samples before detection activates.
_MIN_HISTORY = 3

_RESULTS_DIR = Path(__file__).resolve().parent / "results"
_DEFAULT_RUNS_PATH = _RESULTS_DIR / "runs.jsonl"
_DEFAULT_REGRESSIONS_PATH = _RESULTS_DIR / "regressions.jsonl"


def _is_number(value: Any) -> bool:
    """Return True for real numbers (bools explicitly excluded)."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def load_runs(runs_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load every run record from the ``runs.jsonl`` history.

    Args:
        runs_path: Path to the JSONL file; defaults to
            ``results/runs.jsonl`` beside this script.

    Returns:
        The parsed run records; malformed lines are skipped silently.
    """
    path = Path(runs_path) if runs_path else _DEFAULT_RUNS_PATH
    if not path.exists():
        return []
    runs: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if isinstance(data, dict):
            runs.append(data)
    return runs


def detect_regressions(
    runs_path: str | Path | None = None,
    regressions_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Detect per-task cost/success regressions over the runs.jsonl history.

    Runs are grouped by ``task_id`` and ordered by ``ts``. For each task the
    latest run is compared against the historical distribution of every
    cost-type metric: with fewer than :data:`_MIN_HISTORY` historical samples
    detection is skipped; otherwise a population z-score above
    :data:`_Z_THRESHOLD` flags the metric (zero-variance history skips the
    metric since z is undefined). Additionally, a latest ``success`` of
    ``False`` after an all-``True`` history is flagged regardless of z.
    Flagged cases are appended (one JSON object per line) to the regressions
    JSONL sink and returned.

    Args:
        runs_path: Optional path to ``runs.jsonl`` (default beside script).
        regressions_path: Optional output path for flagged cases (default
            ``results/regressions.jsonl``).

    Returns:
        The list of regression cases detected in this invocation.
    """
    runs = load_runs(runs_path)
    groups: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        task_id = str(run.get("task_id", ""))
        if not task_id:
            continue
        groups.setdefault(task_id, []).append(run)

    out_path = Path(regressions_path) if regressions_path else _DEFAULT_REGRESSIONS_PATH
    cases: list[dict[str, Any]] = []
    for task_id, group in groups.items():
        group.sort(key=lambda r: (str(r.get("ts", "")), str(r.get("run_id", ""))))
        latest = group[-1]
        history = group[:-1]
        if len(history) < _MIN_HISTORY:
            continue
        latest_metrics = latest.get("metrics") or {}
        flags: list[dict[str, Any]] = []
        for metric in _COST_METRICS:
            latest_value = latest_metrics.get(metric)
            if not _is_number(latest_value):
                continue
            sample = [h.get("metrics", {}).get(metric) for h in history]
            sample = [v for v in sample if _is_number(v)]
            if len(sample) < _MIN_HISTORY:
                continue
            mean = statistics.fmean(sample)
            std = statistics.pstdev(sample)
            if std <= 0.0:
                continue  # z is undefined for zero-variance history
            z = (float(latest_value) - mean) / std
            if z > _Z_THRESHOLD:
                flags.append({
                    "type": "zscore",
                    "metric": metric,
                    "latest": latest_value,
                    "mean": round(mean, 4),
                    "std": round(std, 4),
                    "z": round(z, 4),
                })
        historical_success = [bool((h.get("metrics") or {}).get("success")) for h in history]
        if not bool(latest_metrics.get("success")) and historical_success and all(historical_success):
            flags.append({
                "type": "success_drop",
                "metric": "success",
                "latest": False,
                "historical_all_true": True,
            })
        if not flags:
            continue
        case = {
            "detected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "task_id": task_id,
            "run_id": latest.get("run_id", ""),
            "ts": latest.get("ts", ""),
            "session_id": latest.get("session_id", ""),
            "history_samples": len(history),
            "flags": flags,
        }
        cases.append(case)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")
    return cases


# ---------------------------------------------------------------------------
# Per-model benchmark matrix + tier calibration suggestions (Tasks 9 / 10)
# ---------------------------------------------------------------------------

#: Runs per model required before a tier suggestion is emitted.
_MIN_MODEL_SAMPLES = 3
#: Success rate below which a non-light model is proposed for light tier.
_LIGHT_SUCCESS_CEILING = 0.40
#: Tool-call error rate above which a non-light model is proposed for light tier.
_LIGHT_TOOL_ERROR_FLOOR = 0.35
#: Success rate at or above which a light model is proposed for upgrade.
_UPGRADE_SUCCESS_FLOOR = 0.75

_DEFAULT_MODEL_REPORT_PATH = _RESULTS_DIR / "model_report.json"
_DEFAULT_TIER_SUGGESTIONS_PATH = _RESULTS_DIR / "tier_suggestions.jsonl"


def _model_id(run: dict[str, Any]) -> str:
    """Extract the grouping key from a run's model identity block."""
    model = run.get("model")
    if isinstance(model, dict):
        model_id = str(model.get("id", ""))
        if model_id:
            return model_id
    return "unknown"


def _metric_values(metrics_list: list[dict[str, Any]], key: str) -> list[float]:
    """Collect numeric values of one metric across per-run metric dicts."""
    return [float(m[key]) for m in metrics_list if _is_number(m.get(key))]


def _metric_avg(metrics_list: list[dict[str, Any]], key: str) -> float | None:
    """Mean of one metric over the runs that carry it; ``None`` when none do."""
    values = _metric_values(metrics_list, key)
    return round(sum(values) / len(values), 4) if values else None


def _metric_rate(metrics_list: list[dict[str, Any]], num_key: str, den_key: str) -> float | None:
    """Aggregate ratio ``sum(num_key) / sum(den_key)``; ``None`` when denominator is 0."""
    denominator = sum(_metric_values(metrics_list, den_key))
    if denominator <= 0:
        return None
    return round(sum(_metric_values(metrics_list, num_key)) / denominator, 4)


def aggregate_by_model(runs_path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Aggregate the runs.jsonl history into a per-model failure-pattern matrix.

    Runs are grouped by the ``model.id`` identity block written by the runner
    (older records without the block group under ``"unknown"``). For each
    model the report combines cost averages with the failure-pattern split:
    weak models fail with *form* (invented tool names, broken argument JSON,
    a high tool-error rate) while strong ones fail with *substance*, and the
    two patterns call for different tier settings.

    Args:
        runs_path: Optional path to ``runs.jsonl`` (default beside script).

    Returns:
        Mapping of model id -> stats dict (n_runs, success_rate, cost means,
        tool_error/unknown_tool/malformed_arg rates, resolved tier, and
        termination-reason counts).
    """
    runs = load_runs(runs_path)
    groups: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        groups.setdefault(_model_id(run), []).append(run)

    report: dict[str, dict[str, Any]] = {}
    for model_id, group in sorted(groups.items()):
        group.sort(key=lambda r: (str(r.get("ts", "")), str(r.get("run_id", ""))))
        metrics = [r.get("metrics") or {} for r in group]
        success_flags = [1.0 if bool(m.get("success")) else 0.0 for m in metrics]
        latest_model = group[-1].get("model") or {}
        if not isinstance(latest_model, dict):
            latest_model = {}
        termination: dict[str, int] = {}
        for run in group:
            attribution = run.get("attribution") or {}
            reason = str(attribution.get("reason", "")) or "unknown"
            termination[reason] = termination.get(reason, 0) + 1
        report[model_id] = {
            "n_runs": len(group),
            "success_rate": round(sum(success_flags) / len(success_flags), 4) if success_flags else None,
            "avg_tokens_total": _metric_avg(metrics, "tokens_total"),
            "avg_tool_calls": _metric_avg(metrics, "tool_calls"),
            "avg_turns": _metric_avg(metrics, "turns"),
            "avg_retries": _metric_avg(metrics, "retries"),
            "avg_wall_time_seconds": _metric_avg(metrics, "wall_time_seconds"),
            "tool_error_rate": _metric_rate(metrics, "tool_errors", "tool_calls"),
            "unknown_tool_rate": _metric_rate(metrics, "unknown_tool_calls", "tool_calls"),
            "malformed_arg_rate": _metric_rate(metrics, "malformed_arg_calls", "tool_calls"),
            "resolved_tier": str(latest_model.get("tier", "")),
            "tier_reasons": list(latest_model.get("tier_reasons", []) or []),
            "termination_reasons": termination,
        }
    return report


def build_tier_suggestions(
    runs_path: str | Path | None = None,
    suggestions_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Turn the per-model matrix into suggestion-only tier calibration.

    For every model with at least :data:`_MIN_MODEL_SAMPLES` runs: propose
    ``light`` when the success rate is under :data:`_LIGHT_SUCCESS_CEILING`
    or the tool-error rate exceeds :data:`_LIGHT_TOOL_ERROR_FLOOR` (a model
    failing with *form* benefits from the slimmed prompt, narrowed tool
    visibility, and stricter recovery), and propose an upgrade to
    ``standard`` when a currently-light model succeeds at or above
    :data:`_UPGRADE_SUCCESS_FLOOR`. Suggestions are appended to the
    tier-suggestions JSONL sink and returned; they are never auto-applied --
    a human confirms and edits the model's ``capabilities.tier`` in
    ``config.yaml``.

    Args:
        runs_path: Optional path to ``runs.jsonl`` (default beside script).
        suggestions_path: Optional output path (default
            ``results/tier_suggestions.jsonl``).

    Returns:
        The list of suggestions produced in this invocation.
    """
    report = aggregate_by_model(runs_path)
    out_path = Path(suggestions_path) if suggestions_path else _DEFAULT_TIER_SUGGESTIONS_PATH
    suggestions: list[dict[str, Any]] = []
    for model_id, stats in report.items():
        if model_id == "unknown" or stats["n_runs"] < _MIN_MODEL_SAMPLES:
            continue
        success = stats["success_rate"]
        if success is None:
            continue
        tool_error_rate = stats["tool_error_rate"] or 0.0
        current = stats["resolved_tier"]
        action = suggested = ""
        if current != "light" and (
            success < _LIGHT_SUCCESS_CEILING or tool_error_rate > _LIGHT_TOOL_ERROR_FLOOR
        ):
            action, suggested = "downgrade", "light"
        elif current == "light" and success >= _UPGRADE_SUCCESS_FLOOR and tool_error_rate <= _LIGHT_TOOL_ERROR_FLOOR:
            action, suggested = "upgrade", "standard"
        if not action:
            continue
        case = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "model_id": model_id,
            "current_tier": current,
            "suggested_tier": suggested,
            "action": action,
            "evidence": {
                "n_runs": stats["n_runs"],
                "success_rate": success,
                "tool_error_rate": stats["tool_error_rate"],
                "unknown_tool_rate": stats["unknown_tool_rate"],
                "malformed_arg_rate": stats["malformed_arg_rate"],
            },
            "thresholds": {
                "min_samples": _MIN_MODEL_SAMPLES,
                "light_success_ceiling": _LIGHT_SUCCESS_CEILING,
                "light_tool_error_floor": _LIGHT_TOOL_ERROR_FLOOR,
                "upgrade_success_floor": _UPGRADE_SUCCESS_FLOOR,
            },
            "apply_hint": (
                f"Advisory only: after human review set this model's "
                f"capabilities.tier to '{suggested}' in config.yaml."
            ),
        }
        suggestions.append(case)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")
    return suggestions


def main() -> None:
    """Parse CLI arguments, load benchmark results, and emit the analysis.

    With ``--regressions``, runs the z-score regression detection over the
    ``runs.jsonl`` history. With ``--by-model`` and/or ``--tier-suggestions``,
    runs the per-model matrix aggregation and the suggestion-only tier
    calibration instead. Otherwise, reads the JSON file referenced by
    ``--input`` (only the ``results`` array inside it is used), runs the gap
    analysis, and either writes the rendered analysis to ``--output`` (when
    provided) or prints it to stdout.

    Args:
        (none) - Behavior is driven entirely by ``sys.argv`` via
            :mod:`argparse`.

    Raises:
        FileNotFoundError: If the ``--input`` file does not exist.
        json.JSONDecodeError: If the input file is not valid JSON.
    """
    parser = argparse.ArgumentParser(description="Analyze benchmark results for flagship-agent gaps.")
    parser.add_argument("--input", default="", help="Path to benchmark results JSON (gap analysis; required unless --regressions).")
    parser.add_argument("--output", default="", help="Optional path to write analysis JSON.")
    parser.add_argument("--regressions", action="store_true", help="Run z-score regression detection over the runs.jsonl history.")
    parser.add_argument("--runs", default="", help="Optional path to runs.jsonl (default: results/runs.jsonl beside this script).")
    parser.add_argument("--regressions-output", default="", help="Optional path for flagged cases (default: results/regressions.jsonl).")
    parser.add_argument("--by-model", action="store_true", help="Per-model matrix from runs.jsonl (default: results/model_report.json).")
    parser.add_argument("--tier-suggestions", action="store_true", help="Suggestion-only tier calibration (JSONL sink).")
    parser.add_argument("--model-report-output", default="", help="Optional path for the per-model matrix.")
    parser.add_argument("--tier-suggestions-output", default="", help="Optional path for tier suggestions.")
    args = parser.parse_args()

    if args.regressions:
        cases = detect_regressions(args.runs or None, args.regressions_output or None)
        rendered = json.dumps(
            {"regression_count": len(cases), "regressions": cases},
            ensure_ascii=False, indent=2,
        )
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        print(rendered)
        return

    if args.by_model or args.tier_suggestions:
        payload_out: dict[str, Any] = {}
        if args.by_model:
            report = aggregate_by_model(args.runs or None)
            report_path = Path(args.model_report_output) if args.model_report_output else _DEFAULT_MODEL_REPORT_PATH
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            payload_out["model_report"] = report
        if args.tier_suggestions:
            suggestions = build_tier_suggestions(args.runs or None, args.tier_suggestions_output or None)
            payload_out["tier_suggestion_count"] = len(suggestions)
            payload_out["tier_suggestions"] = suggestions
        payload_out["note"] = "Tier suggestions are advisory; apply only after human review of config.yaml."
        rendered = json.dumps(payload_out, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        print(rendered)
        return

    if not args.input:
        parser.error("--input is required unless --regressions/--by-model/--tier-suggestions is given.")

    # Load the raw payload and tolerate both a bare results array and a
    # wrapping object; fall back to an empty list when the shape is unexpected.
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    results = payload.get("results", []) if isinstance(payload, dict) else []
    # Delegate the actual gap analysis to the shared library function.
    analysis = analyze_benchmark_results(results)
    rendered = json.dumps(analysis, ensure_ascii=False, indent=2)

    if args.output:
        # Persist to disk when an output path was supplied.
        Path(args.output).write_text(rendered, encoding="utf-8")
    # Always echo the analysis to stdout for pipelines/logging.
    print(rendered)


if __name__ == "__main__":
    main()
