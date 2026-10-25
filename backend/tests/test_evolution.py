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

"""Tests for the self-improvement evolution stack in :mod:`encre.improve.evolution`.

Covers the four subsystems and their configuration: the error/success learner with
persistence, the outcome-driven strategy optimizer with its sample-count gate, the
turn-scoring reflex loop with its degradation detectors, and the meta-cognition layer
that tracks per-domain capability and decides on delegation.
"""


from encre.improve.evolution.config import EvolutionConfig
from encre.improve.evolution.learner import EncreEvolutionLearner, ErrorRecord, SuccessRecord
from encre.improve.evolution.meta import CapabilityProfile, EncreMetaCognition
from encre.improve.evolution.optimizer import EncreStrategyOptimizer
from encre.improve.evolution.reflex import EncreReflexLoop, ReflexResult


class TestEvolutionConfig:
    """Validates the EvolutionConfig record and its factory methods.

    Guards the four enable flags and the create_default()/create_disabled() factories,
    since this record is the single source of truth for which evolution subsystems run.
    """

    def test_verify_default_config_enables_all_subsystems(self):
        """Default EvolutionConfig enables learner, optimizer, reflex, and meta."""
        cfg = EvolutionConfig()
        assert cfg.learner_enabled is True, "Learner must be enabled by default."
        assert cfg.optimizer_enabled is True, "Optimizer must be enabled by default."
        assert cfg.reflex_enabled is True, "Reflex loop must be enabled by default."
        assert cfg.meta_enabled is True, "Meta-cognition must be enabled by default."

    def test_verify_custom_config_overrides_flags(self):
        """Constructor kwargs override the default enable flags."""
        cfg = EvolutionConfig(learner_enabled=False, optimizer_enabled=False)
        assert cfg.learner_enabled is False, "Custom learner_enabled=False must be preserved."
        assert cfg.optimizer_enabled is False, "Custom optimizer_enabled=False must be preserved."

    def test_verify_create_default_produces_non_none_subconfigs(self):
        """create_default() instantiates all four sub-config objects."""
        cfg = EvolutionConfig.create_default()
        assert cfg.learner is not None, "create_default must instantiate a learner config."
        assert cfg.optimizer is not None, "create_default must instantiate an optimizer config."
        assert cfg.reflex is not None, "create_default must instantiate a reflex config."
        assert cfg.meta is not None, "create_default must instantiate a meta config."

    def test_verify_create_disabled_disables_all_subsystems(self):
        """create_disabled() yields a no-op config with subsystem flags turned off."""
        cfg = EvolutionConfig.create_disabled()
        assert cfg.learner_enabled is False, "create_disabled must set learner_enabled=False."
        assert cfg.meta_enabled is False, "create_disabled must set meta_enabled=False."


class TestRecords:
    """Validates the SuccessRecord and ErrorRecord data models and their serialisation.

    Records are the persistence unit for the learner and optimizer, so a lossy
    round-trip would corrupt the on-disk training log.
    """

    def test_verify_success_record_fields(self):
        """SuccessRecord stores every constructor argument verbatim, with reuse_count defaulting to 0."""
        sr = SuccessRecord(tool_name="bash", intent_signature="run tests", param_pattern='{"cmd": "pytest"}', outcome="passed")
        assert sr.tool_name == "bash", "tool_name must be stored verbatim."
        assert sr.intent_signature == "run tests", "intent_signature must be stored verbatim."
        assert sr.reuse_count == 0, "reuse_count must default to 0 on creation."

    def test_verify_error_record_fields(self):
        """ErrorRecord stores its arguments verbatim and defaults to resolved=False."""
        er = ErrorRecord(tool_name="grep", error_type="no_match", error_context="no matches found", correction="use broader pattern")
        assert er.tool_name == "grep", "tool_name must be stored verbatim."
        assert er.error_type == "no_match", "error_type must be stored verbatim."
        assert er.correction == "use broader pattern", "correction must be stored verbatim."
        assert er.resolved is False, "Newly created error records must default to resolved=False."

    def test_verify_record_serialization_roundtrip(self):
        """SuccessRecord.to_dict()/from_dict() preserves field values across a round-trip."""
        sr = SuccessRecord(tool_name="bash", intent_signature="run", param_pattern="{}", outcome="ok")
        d = sr.to_dict()
        assert d["tool_name"] == "bash", "Serialized dict must contain the original tool_name."
        sr2 = SuccessRecord.from_dict(d)
        assert sr2.tool_name == sr.tool_name, "Deserialized record must match the original tool_name."


class TestEvolutionLearner:
    """Validates the EncreEvolutionLearner tracking and guidance pipeline.

    Covers error and success ingestion, correction matching, similarity-based
    deduplication, guidance generation, best-parameter retrieval, statistics,
    save/load persistence, and reset. The learner is the memory backbone of the
    evolution subsystem, so these tests pin down its state transitions.
    """

    def setup_method(self):
        """Initialise a fresh learner before each test to prevent state leakage."""
        self.learner = EncreEvolutionLearner()

    def test_verify_record_error_appends_to_error_log(self):
        """record_error appends an ErrorRecord to the internal error log."""
        self.learner.record_error("bash", "timeout", "command timed out", "retry with backoff")
        assert len(self.learner._errors) == 1, "One record_error call must append one error record."
        assert self.learner._errors[0].tool_name == "bash", "Recorded tool_name must match the input."

    def test_verify_record_success_appends_to_success_log(self):
        """record_success appends a SuccessRecord to the internal success log."""
        self.learner.record_success("bash", "run tests", {"cmd": "pytest"}, "passed")
        assert len(self.learner._successes) == 1, "One record_success call must append one success record."
        assert self.learner._successes[0].tool_name == "bash", "Recorded tool_name must match the input."

    def test_verify_record_correction_matches_open_error(self):
        """record_correction resolves an error whose context overlaps the correction context."""
        self.learner.record_error("bash", "timeout", "command timed out when running git status", "")
        self.learner.record_correction("bash", "command timed out when running", "use timeout flag")
        assert self.learner._errors[0].resolved is True, "Matching correction must mark the error as resolved."

    def test_verify_record_correction_no_match_leaves_error_unresolved(self):
        """record_correction does not resolve an unrelated error."""
        self.learner.record_error("bash", "timeout", "command timed out running git", "")
        self.learner.record_correction("bash", "completely different error about permissions", "fix perms")
        assert self.learner._errors[0].resolved is False, "Non-matching correction must not resolve the error."

    def test_verify_similar_error_triggers_deduplication(self):
        """Semantically similar errors merge into one record and increment trigger_count."""
        self.learner.record_error("bash", "timeout", "command timed out", "")
        self.learner.record_error("bash", "timeout", "command timed out running git", "use timeout")
        assert len(self.learner._errors) == 1, "Similar errors must be deduplicated into a single record."
        assert self.learner._errors[0].trigger_count == 1, "Deduplicated record must increment trigger_count."

    def test_verify_mark_error_resolved(self):
        """mark_error_resolved sets resolved=True independently of record_correction."""
        self.learner.record_error("bash", "timeout", "command timed out", "")
        self.learner.mark_error_resolved("bash", "command timed out")
        assert self.learner._errors[0].resolved is True, "mark_error_resolved must set resolved=True."

    def test_verify_get_guidance_returns_string(self):
        """get_guidance always returns a string for a tool with recorded history."""
        self.learner.record_error("bash", "timeout", "command timed out", "use timeout flag")
        self.learner.record_success("bash", "run tests", {"cmd": "pytest"}, "tests passed")
        guidance = self.learner.get_guidance("bash", "run tests with timeout")
        assert isinstance(guidance, str), "get_guidance must return a string."

    def test_verify_get_guidance_empty_for_unknown_tool(self):
        """get_guidance returns an empty string for a tool with no history."""
        guidance = self.learner.get_guidance("unknown_tool", "some context")
        assert guidance == "", "Guidance for an unknown tool must be an empty string."

    def test_verify_get_tool_best_params_returns_recorded_params(self):
        """get_tool_best_params returns the parameters from recorded successes."""
        self.learner.record_success("bash", "run tests", {"cmd": "pytest -v"}, "passed")
        params = self.learner.get_tool_best_params("bash", "run tests")
        assert params is not None, "Best params must exist for a tool with recorded successes."
        assert params["cmd"] == "pytest -v", "Best params must reflect the recorded success parameters."

    def test_verify_get_tool_best_params_none_for_unknown_tool(self):
        """get_tool_best_params returns None when no successes are recorded."""
        params = self.learner.get_tool_best_params("nonexistent", "test")
        assert params is None, "Best params must be None when no successes are recorded."

    def test_verify_get_statistics_counts_errors_and_successes(self):
        """get_statistics reflects the number of recorded errors and successes."""
        self.learner.record_error("bash", "timeout", "err1", "fix1")
        self.learner.record_success("bash", "run", {}, "ok")
        stats = self.learner.get_statistics()
        assert stats["total_errors"] >= 1, "total_errors must reflect recorded errors."
        assert stats["total_successes"] >= 1, "total_successes must reflect recorded successes."

    def test_verify_save_load_persists_and_restores_state(self):
        """save()/load() round-trip error records through a JSON file on disk."""
        import os
        import tempfile
        import shutil
        path = os.path.join(tempfile.mkdtemp(), "learner.json")
        self.learner._storage_path = path
        self.learner.record_error("bash", "timeout", "err", "fix")
        self.learner.save()
        learner2 = EncreEvolutionLearner(storage_path=path)
        assert learner2.load() is True, "load() must return True when the file exists and is valid."
        assert len(learner2._errors) == 1, "Loaded learner must restore the recorded error."
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def test_verify_load_nonexistent_file_returns_false(self):
        """load() returns False instead of raising when the storage file is missing."""
        learner = EncreEvolutionLearner(storage_path="/nonexistent/path/file.json")
        assert learner.load() is False, "load() must return False when the storage file is absent."

    def test_verify_reset_clears_all_records(self):
        """reset() empties both the error and success logs."""
        self.learner.record_error("bash", "timeout", "err", "fix")
        self.learner.record_success("bash", "run", {}, "ok")
        self.learner.reset()
        assert len(self.learner._errors) == 0, "reset() must clear the error log."
        assert len(self.learner._successes) == 0, "reset() must clear the success log."


class TestStrategyOptimizer:
    """Validates the EncreStrategyOptimizer outcome tracking and suggestion logic.

    Covers success/failure recording, the minimum-sample gate on suggestions,
    fallback generation, per-tool statistics, and reset. The optimizer recommends
    parameters from outcome history, so the gating and aggregation matter most.
    """

    def setup_method(self):
        """Initialise a fresh optimizer before each test to prevent state leakage."""
        self.optimizer = EncreStrategyOptimizer()

    def test_verify_record_outcome_success_increments_sample_count(self):
        """record_outcome(success=True) increments the tool's sample counter."""
        self.optimizer.record_outcome("bash", {"cmd": "ls -la"}, success=True, latency_ms=100)
        stats = self.optimizer.get_statistics()
        assert "bash" in stats, "Statistics must contain an entry for the recorded tool."
        assert stats["bash"]["total_samples"] == 1, "total_samples must be 1 after one success."

    def test_verify_record_outcome_failure_increments_sample_count(self):
        """record_outcome(success=False) is counted alongside successes in the histogram."""
        self.optimizer.record_outcome("bash", {"cmd": "rm -rf /"}, success=False)
        stats = self.optimizer.get_statistics()
        assert stats["bash"]["total_samples"] == 1, "total_samples must be 1 after one failure."

    def test_verify_suggest_strategy_returns_hint_after_sufficient_samples(self):
        """suggest_strategy yields a hint dict once enough successful samples exist."""
        for _ in range(5):
            self.optimizer.record_outcome("bash", {"cmd": "ls -la"}, success=True)
        suggestion = self.optimizer.suggest_strategy("bash", "list files")
        assert suggestion is not None, "Suggestion must be produced after sufficient samples."
        assert "_strategy_hint" in suggestion, "Suggestion must contain a _strategy_hint key."

    def test_verify_suggest_strategy_returns_none_with_insufficient_samples(self):
        """suggest_strategy returns None below the minimum sample threshold."""
        self.optimizer.record_outcome("bash", {"cmd": "ls"}, success=True)
        suggestion = self.optimizer.suggest_strategy("bash", "list")
        assert suggestion is None, "Suggestion must be None when sample count is below the threshold."

    def test_verify_suggest_strategy_returns_none_for_unknown_tool(self):
        """suggest_strategy returns None for a tool with no recorded outcomes."""
        assert self.optimizer.suggest_strategy("nonexistent", "test") is None, \
            "Suggestion must be None for an unknown tool."

    def test_verify_get_fallback_produces_hint_after_sufficient_samples(self):
        """get_fallback yields a hint dict once the sample threshold is met."""
        for _ in range(5):
            self.optimizer.record_outcome("bash", {"cmd": "ls -la"}, success=True)
        for _ in range(5):
            self.optimizer.record_outcome("bash", {"cmd": "pwd"}, success=True)
        fallback = self.optimizer.get_fallback("bash", {"cmd": "ls -la"})
        assert fallback is not None, "Fallback must be produced after sufficient samples."
        assert "_fallback_hint" in fallback, "Fallback must contain a _fallback_hint key."

    def test_verify_get_statistics_aggregates_multiple_tools(self):
        """get_statistics keeps a histogram entry per recorded tool."""
        self.optimizer.record_outcome("bash", {"cmd": "ls"}, success=True)
        self.optimizer.record_outcome("grep", {"pattern": "foo"}, success=False)
        stats = self.optimizer.get_statistics()
        assert "bash" in stats, "Statistics must contain an entry for bash."
        assert "grep" in stats, "Statistics must contain an entry for grep."

    def test_verify_reset_clears_all_outcome_history(self):
        """reset() empties the outcome history so suggestions become unavailable."""
        self.optimizer.record_outcome("bash", {"cmd": "ls"}, success=True)
        self.optimizer.reset()
        assert self.optimizer.suggest_strategy("bash", "test") is None, \
            "Suggestion must be None after reset clears all history."


class TestReflexLoop:
    """Validates the EncreReflexLoop turn scoring and issue detection.

    Covers empty, all-success, and all-error turns, consecutive-failure and
    duplicate-call detection, slow-turn detection, improvement context, trend
    analysis, average scoring, reset, and the disabled no-op path. The loop
    surfaces degradation signals from recent tool-use patterns.
    """

    def setup_method(self):
        """Initialise a fresh reflex loop before each test to prevent state leakage."""
        self.reflex = EncreReflexLoop(enabled=True)

    def test_verify_reflect_empty_tools_returns_low_score(self):
        """Reflecting on a turn with no tool results yields a low score and at least one issue."""
        result = self.reflex.reflect(turn_number=1, tool_results=[], turn_latency_ms=100)
        assert isinstance(result, ReflexResult), "reflect must return a ReflexResult instance."
        assert result.turn_number == 1, "Turn number must be preserved in the result."
        assert result.score < 1.0, "Empty tool results must yield a score below 1.0."
        assert len(result.issues) > 0, "Empty tool results must surface at least one issue."

    def test_verify_reflect_all_success_returns_high_score(self):
        """A turn whose tool calls all succeed scores above 0.5 and does not request a retry."""
        result = self.reflex.reflect(turn_number=2, tool_results=[
            {"tool_name": "file_read", "is_error": False},
            {"tool_name": "grep", "is_error": False},
        ], turn_latency_ms=2000)
        assert result.score > 0.5, "All-success turn must score above 0.5."
        assert result.should_retry is False, "All-success turn must not recommend retry."

    def test_verify_reflect_all_errors_returns_low_score_and_retry(self):
        """A turn whose tool calls all fail scores below 0.5 and recommends a retry."""
        result = self.reflex.reflect(turn_number=3, tool_results=[
            {"tool_name": "bash", "is_error": True},
            {"tool_name": "bash", "is_error": True},
        ], turn_latency_ms=500)
        assert result.score < 0.5, "All-error turn must score below 0.5."
        assert result.should_retry is True, "All-error turn must recommend retry (error_rate=1.0 > 0.5)."

    def test_verify_consecutive_failures_are_detected(self):
        """Four consecutive error turns plus one more surface a 'consecutive' issue."""
        for i in range(4):
            self.reflex.reflect(turn_number=i, tool_results=[
                {"tool_name": "bash", "is_error": True},
            ], turn_latency_ms=100)
        result = self.reflex.reflect(turn_number=5, tool_results=[
            {"tool_name": "bash", "is_error": True},
        ], turn_latency_ms=100)
        assert any("consecutive" in issue.lower() for issue in result.issues), \
            "Four+ consecutive errors must trigger a 'consecutive' issue."

    def test_verify_duplicate_calls_are_detected(self):
        """Four identical calls within one turn surface a 'repeated' issue."""
        result = self.reflex.reflect(turn_number=1, tool_results=[
            {"tool_name": "grep", "is_error": False},
            {"tool_name": "grep", "is_error": False},
            {"tool_name": "grep", "is_error": False},
            {"tool_name": "grep", "is_error": False},
        ], turn_latency_ms=100)
        assert any("repeated" in issue.lower() for issue in result.issues), \
            "Four identical calls must trigger a 'repeated' issue."

    def test_verify_slow_turn_is_detected(self):
        """A 120-second turn latency surfaces a 'slow' issue."""
        result = self.reflex.reflect(turn_number=1, tool_results=[
            {"tool_name": "bash", "is_error": False},
        ], turn_latency_ms=120000)
        assert any("slow" in issue.lower() for issue in result.issues), \
            "120-second turn must trigger a 'slow' issue."

    def test_verify_get_improvement_context_returns_string(self):
        """get_improvement_context returns a string once at least one reflection exists."""
        self.reflex.reflect(turn_number=1, tool_results=[
            {"tool_name": "bash", "is_error": True},
        ], turn_latency_ms=100)
        ctx = self.reflex.get_improvement_context()
        assert isinstance(ctx, str), "get_improvement_context must return a string."

    def test_verify_get_improvement_context_empty_when_no_reflections(self):
        """get_improvement_context returns '' when no reflections have been recorded."""
        reflex = EncreReflexLoop(enabled=True)
        assert reflex.get_improvement_context() == "", \
            "Improvement context must be empty when no reflections have been recorded."

    def test_verify_get_trend_stable_after_consistent_success(self):
        """get_trend reports 'stable' after five consistent successful turns."""
        for i in range(5):
            self.reflex.reflect(turn_number=i, tool_results=[
                {"tool_name": "bash", "is_error": False},
            ], turn_latency_ms=100)
        assert self.reflex.get_trend() == "stable", "Five consistent successes must yield a 'stable' trend."

    def test_verify_get_average_score_is_bound_between_zero_and_one(self):
        """get_average_score stays within the valid [0.0, 1.0] range."""
        self.reflex.reflect(turn_number=1, tool_results=[
            {"tool_name": "bash", "is_error": False},
        ], turn_latency_ms=100)
        avg = self.reflex.get_average_score()
        assert 0.0 <= avg <= 1.0, "Average score must lie within [0.0, 1.0]."

    def test_verify_reset_clears_scores_and_context(self):
        """reset() restores the average score to the 1.0 baseline and clears the context."""
        self.reflex.reflect(turn_number=1, tool_results=[
            {"tool_name": "bash", "is_error": False},
        ], turn_latency_ms=100)
        self.reflex.reset()
        assert self.reflex.get_average_score() == 1.0, "Average score must reset to 1.0 (baseline)."
        assert self.reflex.get_improvement_context() == "", "Improvement context must be cleared on reset."

    def test_verify_disabled_reflex_returns_perfect_score(self):
        """A disabled reflex loop returns score=1.0 and no issues from every reflect()."""
        reflex = EncreReflexLoop(enabled=False)
        result = reflex.reflect(turn_number=1, tool_results=[], turn_latency_ms=100)
        assert result.score == 1.0, "Disabled reflex must return a perfect score."
        assert result.issues == [], "Disabled reflex must produce no issues."


class TestMetaCognition:
    """Validates the EncreMetaCognition capability profile and delegation logic.

    Covers profile construction and defaults, score updates, turn assessment and
    domain routing, weakness reporting, the delegation decision, self-awareness
    context, delegation recording, and reset. Per-domain proficiency here drives
    whether a task should be handed to a sub-agent.
    """

    def setup_method(self):
        """Initialise a fresh meta-cognition instance before each test to prevent state leakage."""
        self.meta = EncreMetaCognition()

    def test_verify_capability_profile_default_values(self):
        """A fresh CapabilityProfile starts neutral: score 0.5, no confidence, no samples."""
        profile = CapabilityProfile(domain="python")
        assert profile.domain == "python", "Domain must be stored verbatim."
        assert profile.score == 0.5, "Default score must be 0.5 (neutral)."
        assert profile.confidence == 0.0, "Default confidence must be 0.0 (no evidence yet)."
        assert profile.sample_count == 0, "Default sample_count must be 0."

    def test_verify_capability_profile_score_increases_on_success(self):
        """update(success=True) raises the profile score above the 0.5 baseline."""
        profile = CapabilityProfile(domain="python")
        profile.update(success=True, difficulty=0.5)
        assert profile.sample_count == 1, "Sample count must increment after one update."
        assert profile.score > 0.5, "Score must increase above the neutral baseline after a success."

    def test_verify_assess_turn_updates_profile_for_done_domain(self):
        """assess_turn routes tool use to the right domain and raises its profile score."""
        self.meta.assess_turn("write a python function to read a file", [
            {"tool_name": "file_read", "is_error": False},
        ])
        profile = self.meta.get_profile("file_operations")
        assert isinstance(profile, dict), "get_profile must return a dict for a known domain."
        assert profile["score"] > 0.5, "Successful file-operation turn must raise the profile score."

    def test_verify_get_profile_unknown_domain_returns_default_dict(self):
        """get_profile returns a well-formed fallback dict with zero confidence for unknown domains."""
        result = self.meta.get_profile("unknown_domain")
        assert isinstance(result, dict), "Unknown domain must return a dict, not None."
        assert result["confidence"] == 0.0, "Unknown domain must have zero confidence."

    def test_verify_get_all_profiles_returns_dict(self):
        """get_profile() with no arguments returns the aggregated profile store."""
        self.meta.assess_turn("run tests with pytest", [
            {"tool_name": "bash", "is_error": False},
        ])
        all_profiles = self.meta.get_profile()
        assert isinstance(all_profiles, dict), "get_profile() must return a dict."

    def test_verify_get_weakness_report_returns_list(self):
        """get_weakness_report returns a list once enough low-scoring samples accumulate."""
        for _ in range(25):
            self.meta.assess_turn("use bash to run a broken command", [
                {"tool_name": "bash", "is_error": True},
            ])
        report = self.meta.get_weakness_report()
        assert isinstance(report, list), "Weakness report must be a list."

    def test_verify_should_delegate_returns_false_without_confidence(self):
        """should_delegate withholds delegation until a profile has confidence."""
        should, _reason = self.meta.should_delegate("design a system architecture")
        assert should is False, "Delegation must be withheld when confidence is zero."

    def test_verify_get_self_awareness_context_returns_string(self):
        """get_self_awareness_context always returns a prompt-injectable string."""
        ctx = self.meta.get_self_awareness_context()
        assert isinstance(ctx, str), "get_self_awareness_context must return a string."

    def test_verify_record_delegation_does_not_raise(self):
        """record_delegation completes without raising even on an empty profile store."""
        self.meta.record_delegation("complex task", "sub_agent", True)

    def test_verify_reset_clears_all_profiles(self):
        """reset() empties the capability profile store."""
        self.meta.assess_turn("run tests", [{"tool_name": "bash", "is_error": False}])
        self.meta.reset()
        assert self.meta.get_profile() == {}, "reset() must clear all capability profiles."
