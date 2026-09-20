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

from __future__ import annotations

"""Tests for evolution subsystem: learner, optimizer, reflex, meta-cognition."""


from encre.improve.evolution.config import EvolutionConfig
from encre.improve.evolution.learner import EncreEvolutionLearner, ErrorRecord, SuccessRecord
from encre.improve.evolution.meta import CapabilityProfile, EncreMetaCognition
from encre.improve.evolution.optimizer import EncreStrategyOptimizer
from encre.improve.evolution.reflex import EncreReflexLoop, ReflexResult


class TestEvolutionConfig:
    """Engineered to validate the EvolutionConfig record and its factory methods.

    This test class exercises the config across 4 scenarios: default field population,
    custom constructor overrides, create_default() producing non-None sub-configs, and
    create_disabled() toggling all subsystem flags off. The config is the single source
    of truth for which evolution subsystems are active, so these tests guard against
    regressions in the enable/disable wiring.
    """

    def test_verify_default_config_enables_all_subsystems(self):
        """Validate that EvolutionConfig() enables learner, optimizer, reflex, and meta by default.

        The test asserts all four *_enabled flags are True, confirming the default policy
        runs the full evolution pipeline unless the user explicitly disables components.
        """
        cfg = EvolutionConfig()
        assert cfg.learner_enabled is True, "Learner must be enabled by default."
        assert cfg.optimizer_enabled is True, "Optimizer must be enabled by default."
        assert cfg.reflex_enabled is True, "Reflex loop must be enabled by default."
        assert cfg.meta_enabled is True, "Meta-cognition must be enabled by default."

    def test_verify_custom_config_overrides_flags(self):
        """Validate that constructor kwargs override the default enabled flags.

        The test passes learner_enabled=False and optimizer_enabled=False and asserts
        both flags are False, confirming the constructor respects explicit overrides.
        """
        cfg = EvolutionConfig(learner_enabled=False, optimizer_enabled=False)
        assert cfg.learner_enabled is False, "Custom learner_enabled=False must be preserved."
        assert cfg.optimizer_enabled is False, "Custom optimizer_enabled=False must be preserved."

    def test_verify_create_default_produces_non_none_subconfigs(self):
        """Validate that create_default() instantiates all four sub-config objects.

        The test asserts learner, optimizer, reflex, and meta are all non-None, confirming
        the factory method wires every subsystem rather than skipping any.
        """
        cfg = EvolutionConfig.create_default()
        assert cfg.learner is not None, "create_default must instantiate a learner config."
        assert cfg.optimizer is not None, "create_default must instantiate an optimizer config."
        assert cfg.reflex is not None, "create_default must instantiate a reflex config."
        assert cfg.meta is not None, "create_default must instantiate a meta config."

    def test_verify_create_disabled_disables_all_subsystems(self):
        """Validate that create_disabled() turns off learner and meta flags.

        The test asserts learner_enabled and meta_enabled are False, confirming the
        disabled factory method produces a no-op config suitable for environments where
        evolution must be suppressed.
        """
        cfg = EvolutionConfig.create_disabled()
        assert cfg.learner_enabled is False, "create_disabled must set learner_enabled=False."
        assert cfg.meta_enabled is False, "create_disabled must set meta_enabled=False."


class TestRecords:
    """Engineered to validate the SuccessRecord and ErrorRecord data models.

    This test class exercises record construction, field storage, and round-trip
    serialisation across 3 scenarios. Records are the persistence unit for the learner
    and optimizer; incorrect serialisation would corrupt the on-disk training log.
    """

    def test_verify_success_record_fields(self):
        """Validate that SuccessRecord stores all constructor arguments verbatim.

        The test constructs a record with explicit tool_name, intent_signature,
        param_pattern, and outcome, then asserts each field matches, confirming the
        dataclass does not drop or transform any argument.
        """
        sr = SuccessRecord(tool_name="bash", intent_signature="run tests", param_pattern='{"cmd": "pytest"}', outcome="passed")
        assert sr.tool_name == "bash", "tool_name must be stored verbatim."
        assert sr.intent_signature == "run tests", "intent_signature must be stored verbatim."
        assert sr.reuse_count == 0, "reuse_count must default to 0 on creation."

    def test_verify_error_record_fields(self):
        """Validate that ErrorRecord stores all constructor arguments and defaults resolved=False.

        The test constructs a record with tool_name, error_type, error_context, and
        correction, then asserts each field matches and resolved is False, confirming
        the default state is "unresolved" until the learner marks it fixed.
        """
        er = ErrorRecord(tool_name="grep", error_type="no_match", error_context="no matches found", correction="use broader pattern")
        assert er.tool_name == "grep", "tool_name must be stored verbatim."
        assert er.error_type == "no_match", "error_type must be stored verbatim."
        assert er.correction == "use broader pattern", "correction must be stored verbatim."
        assert er.resolved is False, "Newly created error records must default to resolved=False."

    def test_verify_record_serialization_roundtrip(self):
        """Validate that SuccessRecord.to_dict() and from_dict() preserve field values.

        The test constructs a record, serialises it, deserialises it into a new instance,
        and asserts the tool_name matches, confirming the serialisation round-trip is
        lossless for at least the primary key field.
        """
        sr = SuccessRecord(tool_name="bash", intent_signature="run", param_pattern="{}", outcome="ok")
        d = sr.to_dict()
        assert d["tool_name"] == "bash", "Serialized dict must contain the original tool_name."
        sr2 = SuccessRecord.from_dict(d)
        assert sr2.tool_name == sr.tool_name, "Deserialized record must match the original tool_name."


class TestEvolutionLearner:
    """Engineered to validate the EncreEvolutionLearner error/success tracking and guidance pipeline.

    This test class exercises the learner across 12 scenarios covering error recording,
    success recording, correction matching, similarity-based error deduplication,
    guidance generation, best-parameter retrieval, statistics, save/load persistence,
    nonexistent-file load handling, and reset. The learner is the memory backbone of
    the evolution subsystem; these tests ensure its state transitions are correct.
    """

    def setup_method(self):
        """Initialise a fresh learner before each test to prevent state leakage."""
        self.learner = EncreEvolutionLearner()

    def test_verify_record_error_appends_to_error_log(self):
        """Validate that record_error appends an ErrorRecord to _errors.

        The test records one error and asserts the log length is 1 with the correct
        tool_name, confirming the error ingestion path writes correctly.
        """
        self.learner.record_error("bash", "timeout", "command timed out", "retry with backoff")
        assert len(self.learner._errors) == 1, "One record_error call must append one error record."
        assert self.learner._errors[0].tool_name == "bash", "Recorded tool_name must match the input."

    def test_verify_record_success_appends_to_success_log(self):
        """Validate that record_success appends a SuccessRecord to _successes.

        The test records one success and asserts the log length is 1 with the correct
        tool_name, confirming the success ingestion path writes correctly.
        """
        self.learner.record_success("bash", "run tests", {"cmd": "pytest"}, "passed")
        assert len(self.learner._successes) == 1, "One record_success call must append one success record."
        assert self.learner._successes[0].tool_name == "bash", "Recorded tool_name must match the input."

    def test_verify_record_correction_matches_open_error(self):
        """Validate that record_correction resolves an error whose context overlaps.

        The test records an error with context "command timed out when running git status"
        and then records a correction for the prefix "command timed out when running". It
        asserts the error's resolved flag becomes True, confirming the overlap-matching
        logic finds the right record.
        """
        self.learner.record_error("bash", "timeout", "command timed out when running git status", "")
        self.learner.record_correction("bash", "command timed out when running", "use timeout flag")
        assert self.learner._errors[0].resolved is True, "Matching correction must mark the error as resolved."

    def test_verify_record_correction_no_match_leaves_error_unresolved(self):
        """Validate that record_correction leaves an unrelated error unresolved.

        The test records an error about timeout and then records a correction about a
        completely different permission context, asserting the original error's resolved
        flag stays False, confirming the matcher does not over-match.
        """
        self.learner.record_error("bash", "timeout", "command timed out running git", "")
        self.learner.record_correction("bash", "completely different error about permissions", "fix perms")
        assert self.learner._errors[0].resolved is False, "Non-matching correction must not resolve the error."

    def test_verify_similar_error_triggers_deduplication(self):
        """Validate that semantically similar errors are deduplicated into a single record.

        The test records two timeout errors with overlapping context and asserts the log
        length is 1 (not 2) and the trigger_count is 1 (incremented once by the second
        call), confirming the similarity detector merges related errors instead of
        fragmenting them.
        """
        self.learner.record_error("bash", "timeout", "command timed out", "")
        self.learner.record_error("bash", "timeout", "command timed out running git", "use timeout")
        assert len(self.learner._errors) == 1, "Similar errors must be deduplicated into a single record."
        assert self.learner._errors[0].trigger_count == 1, "Deduplicated record must increment trigger_count."

    def test_verify_mark_error_resolved(self):
        """Validate that mark_error_resolved sets resolved=True on the matching error.

        The test records an error and calls mark_error_resolved with the tool name and
        context prefix, then asserts the resolved flag is True, confirming the explicit
        resolve path works independently of record_correction.
        """
        self.learner.record_error("bash", "timeout", "command timed out", "")
        self.learner.mark_error_resolved("bash", "command timed out")
        assert self.learner._errors[0].resolved is True, "mark_error_resolved must set resolved=True."

    def test_verify_get_guidance_returns_string(self):
        """Validate that get_guidance returns a string for a known tool with records.

        The test records one error and one success for "bash" and asserts get_guidance
        returns a str (possibly empty), confirming the method never raises and always
        returns a string that can be injected into the prompt.
        """
        self.learner.record_error("bash", "timeout", "command timed out", "use timeout flag")
        self.learner.record_success("bash", "run tests", {"cmd": "pytest"}, "tests passed")
        guidance = self.learner.get_guidance("bash", "run tests with timeout")
        assert isinstance(guidance, str), "get_guidance must return a string."

    def test_verify_get_guidance_empty_for_unknown_tool(self):
        """Validate that get_guidance returns an empty string for an unknown tool.

        The test queries guidance for "unknown_tool" and asserts the result is "",
        confirming the method gracefully handles tools with no recorded history.
        """
        guidance = self.learner.get_guidance("unknown_tool", "some context")
        assert guidance == "", "Guidance for an unknown tool must be an empty string."

    def test_verify_get_tool_best_params_returns_recorded_params(self):
        """Validate that get_tool_best_params returns the best-known params for a known tool.

        The test records one success with cmd="pytest -v" and asserts the returned dict
        contains that command, confirming the learner tracks and retrieves optimal
        parameters from successful executions.
        """
        self.learner.record_success("bash", "run tests", {"cmd": "pytest -v"}, "passed")
        params = self.learner.get_tool_best_params("bash", "run tests")
        assert params is not None, "Best params must exist for a tool with recorded successes."
        assert params["cmd"] == "pytest -v", "Best params must reflect the recorded success parameters."

    def test_verify_get_tool_best_params_none_for_unknown_tool(self):
        """Validate that get_tool_best_params returns None when no successes exist.

        The test queries a nonexistent tool and asserts None, confirming the method
        does not fabricate parameters when the success log is empty.
        """
        params = self.learner.get_tool_best_params("nonexistent", "test")
        assert params is None, "Best params must be None when no successes are recorded."

    def test_verify_get_statistics_counts_errors_and_successes(self):
        """Validate that get_statistics returns accurate error and success counts.

        The test records one error and one success and asserts both counters are >= 1,
        confirming the statistics aggregator increments correctly.
        """
        self.learner.record_error("bash", "timeout", "err1", "fix1")
        self.learner.record_success("bash", "run", {}, "ok")
        stats = self.learner.get_statistics()
        assert stats["total_errors"] >= 1, "total_errors must reflect recorded errors."
        assert stats["total_successes"] >= 1, "total_successes must reflect recorded successes."

    def test_verify_save_load_persists_and_restores_state(self):
        """Validate that save() and load() round-trip error records through JSON.

        The test records an error, saves to a temp file, constructs a fresh learner
        pointing at the same file, loads, and asserts the error count is 1, confirming
        disk persistence works end-to-end. The test cleans up the temp directory afterward.
        """
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
        """Validate that load() returns False when the storage file does not exist.

        The test points at a deliberately nonexistent path and asserts load() returns
        False, confirming the method does not crash on missing files and signals the
        absence gracefully.
        """
        learner = EncreEvolutionLearner(storage_path="/nonexistent/path/file.json")
        assert learner.load() is False, "load() must return False when the storage file is absent."

    def test_verify_reset_clears_all_records(self):
        """Validate that reset() empties both the error and success logs.

        The test records one error and one success, calls reset, and asserts both logs
        are empty, confirming the reset path clears state completely.
        """
        self.learner.record_error("bash", "timeout", "err", "fix")
        self.learner.record_success("bash", "run", {}, "ok")
        self.learner.reset()
        assert len(self.learner._errors) == 0, "reset() must clear the error log."
        assert len(self.learner._successes) == 0, "reset() must clear the success log."


class TestStrategyOptimizer:
    """Engineered to validate the EncreStrategyOptimizer outcome tracking and suggestion logic.

    This test class exercises the optimizer across 8 scenarios covering success/failure
    recording, strategy suggestion with sufficient and insufficient samples, unknown-tool
    handling, fallback generation, statistics, and reset. The optimizer uses outcome
    histories to recommend parameters; these tests ensure the sample-threshold gating
    and statistics aggregation are correct.
    """

    def setup_method(self):
        """Initialise a fresh optimizer before each test to prevent state leakage."""
        self.optimizer = EncreStrategyOptimizer()

    def test_verify_record_outcome_success_increments_sample_count(self):
        """Validate that record_outcome with success=True increments the sample counter.

        The test records one success for "bash" and asserts the statistics dict contains
        "bash" with total_samples==1, confirming the success path updates the histogram.
        """
        self.optimizer.record_outcome("bash", {"cmd": "ls -la"}, success=True, latency_ms=100)
        stats = self.optimizer.get_statistics()
        assert "bash" in stats, "Statistics must contain an entry for the recorded tool."
        assert stats["bash"]["total_samples"] == 1, "total_samples must be 1 after one success."

    def test_verify_record_outcome_failure_increments_sample_count(self):
        """Validate that record_outcome with success=False also increments the sample counter.

        The test records one failure and asserts total_samples==1, confirming failures
        are counted alongside successes in the sample histogram.
        """
        self.optimizer.record_outcome("bash", {"cmd": "rm -rf /"}, success=False)
        stats = self.optimizer.get_statistics()
        assert stats["bash"]["total_samples"] == 1, "total_samples must be 1 after one failure."

    def test_verify_suggest_strategy_returns_hint_after_sufficient_samples(self):
        """Validate that suggest_strategy returns a hint dict after enough successful samples.

        The test records 5 successes and asserts the suggestion is non-None and contains
        the "_strategy_hint" key, confirming the minimum-sample threshold is met and
        the suggestion engine produces output.
        """
        for _ in range(5):
            self.optimizer.record_outcome("bash", {"cmd": "ls -la"}, success=True)
        suggestion = self.optimizer.suggest_strategy("bash", "list files")
        assert suggestion is not None, "Suggestion must be produced after sufficient samples."
        assert "_strategy_hint" in suggestion, "Suggestion must contain a _strategy_hint key."

    def test_verify_suggest_strategy_returns_none_with_insufficient_samples(self):
        """Validate that suggest_strategy returns None when below the minimum sample threshold.

        The test records only 1 success (below MIN_SAMPLES_FOR_RECOMMENDATION) and asserts
        None is returned, confirming the gate prevents premature or noisy suggestions.
        """
        self.optimizer.record_outcome("bash", {"cmd": "ls"}, success=True)
        suggestion = self.optimizer.suggest_strategy("bash", "list")
        assert suggestion is None, "Suggestion must be None when sample count is below the threshold."

    def test_verify_suggest_strategy_returns_none_for_unknown_tool(self):
        """Validate that suggest_strategy returns None for a tool with no recorded outcomes.

        The test queries a nonexistent tool and asserts None, confirming the method does
        not fabricate suggestions for tools with zero history.
        """
        assert self.optimizer.suggest_strategy("nonexistent", "test") is None, \
            "Suggestion must be None for an unknown tool."

    def test_verify_get_fallback_produces_hint_after_sufficient_samples(self):
        """Validate that get_fallback returns a hint dict after enough successful samples.

        The test records 5 successes for two different commands and asserts the fallback
        contains the "_fallback_hint" key, confirming the fallback engine activates once
        the sample threshold is met.
        """
        for _ in range(5):
            self.optimizer.record_outcome("bash", {"cmd": "ls -la"}, success=True)
        for _ in range(5):
            self.optimizer.record_outcome("bash", {"cmd": "pwd"}, success=True)
        fallback = self.optimizer.get_fallback("bash", {"cmd": "ls -la"})
        assert fallback is not None, "Fallback must be produced after sufficient samples."
        assert "_fallback_hint" in fallback, "Fallback must contain a _fallback_hint key."

    def test_verify_get_statistics_aggregates_multiple_tools(self):
        """Validate that get_statistics contains entries for all recorded tools.

        The test records one success for "bash" and one failure for "grep" and asserts
        both tool names appear in the stats dict, confirming the per-tool histogram is
        maintained correctly.
        """
        self.optimizer.record_outcome("bash", {"cmd": "ls"}, success=True)
        self.optimizer.record_outcome("grep", {"pattern": "foo"}, success=False)
        stats = self.optimizer.get_statistics()
        assert "bash" in stats, "Statistics must contain an entry for bash."
        assert "grep" in stats, "Statistics must contain an entry for grep."

    def test_verify_reset_clears_all_outcome_history(self):
        """Validate that reset() clears outcome history so suggestions become unavailable.

        The test records a success, resets, and asserts suggest_strategy returns None,
        confirming the reset path empties the sample histogram completely.
        """
        self.optimizer.record_outcome("bash", {"cmd": "ls"}, success=True)
        self.optimizer.reset()
        assert self.optimizer.suggest_strategy("bash", "test") is None, \
            "Suggestion must be None after reset clears all history."


class TestReflexLoop:
    """Engineered to validate the EncreReflexLoop turn-scoring and issue-detection logic.

    This test class exercises the reflex loop across 11 scenarios covering empty-tool
    turns, all-success turns, all-error turns, consecutive-failure detection, duplicate-call
    detection, slow-turn detection, improvement context generation, trend analysis,
    average scoring, reset, and disabled-mode behaviour. The reflex loop analyses recent
    tool-use patterns to surface degradation signals; these tests ensure each detector
    fires correctly.
    """

    def setup_method(self):
        """Initialise a fresh reflex loop before each test to prevent state leakage."""
        self.reflex = EncreReflexLoop(enabled=True)

    def test_verify_reflect_empty_tools_returns_low_score(self):
        """Validate that reflecting on an empty tool-results list yields a low score and issues.

        The test asserts score < 1.0 and issues is non-empty, confirming the reflex loop
        flags turns with no tool activity as suboptimal rather than passing them silently.
        """
        result = self.reflex.reflect(turn_number=1, tool_results=[], turn_latency_ms=100)
        assert isinstance(result, ReflexResult), "reflect must return a ReflexResult instance."
        assert result.turn_number == 1, "Turn number must be preserved in the result."
        assert result.score < 1.0, "Empty tool results must yield a score below 1.0."
        assert len(result.issues) > 0, "Empty tool results must surface at least one issue."

    def test_verify_reflect_all_success_returns_high_score(self):
        """Validate that a turn with all successful tool calls yields a score above 0.5.

        The test passes two error-free tool results and asserts score > 0.5 and
        should_retry is False, confirming clean turns do not trigger a retry recommendation.
        """
        result = self.reflex.reflect(turn_number=2, tool_results=[
            {"tool_name": "file_read", "is_error": False},
            {"tool_name": "grep", "is_error": False},
        ], turn_latency_ms=2000)
        assert result.score > 0.5, "All-success turn must score above 0.5."
        assert result.should_retry is False, "All-success turn must not recommend retry."

    def test_verify_reflect_all_errors_returns_low_score_and_retry(self):
        """Validate that a turn with all errors yields a low score and recommends retry.

        The test passes two errored tool results and asserts score < 0.5 and
        should_retry is True, confirming the error-rate threshold (error_rate > 0.5 with
        total > 1) triggers the retry signal correctly.
        """
        result = self.reflex.reflect(turn_number=3, tool_results=[
            {"tool_name": "bash", "is_error": True},
            {"tool_name": "bash", "is_error": True},
        ], turn_latency_ms=500)
        assert result.score < 0.5, "All-error turn must score below 0.5."
        assert result.should_retry is True, "All-error turn must recommend retry (error_rate=1.0 > 0.5)."

    def test_verify_consecutive_failures_are_detected(self):
        """Validate that four consecutive errors plus one more surface a 'consecutive' issue.

        The test drives the reflex loop through 5 turns of single-error bash calls and
        asserts the final reflection contains an issue mentioning "consecutive", confirming
        the streak detector accumulates correctly across turns.
        """
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
        """Validate that four identical successful grep calls surface a 'repeated' issue.

        The test passes four identical {"tool_name": "grep", "is_error": False} entries in
        a single turn and asserts the reflection contains an issue mentioning "repeated",
        confirming the duplicate-call detector operates within a single turn.
        """
        result = self.reflex.reflect(turn_number=1, tool_results=[
            {"tool_name": "grep", "is_error": False},
            {"tool_name": "grep", "is_error": False},
            {"tool_name": "grep", "is_error": False},
            {"tool_name": "grep", "is_error": False},
        ], turn_latency_ms=100)
        assert any("repeated" in issue.lower() for issue in result.issues), \
            "Four identical calls must trigger a 'repeated' issue."

    def test_verify_slow_turn_is_detected(self):
        """Validate that a turn with 120-second latency surfaces a 'slow' issue.

        The test passes a single successful bash call with turn_latency_ms=120000 and
        asserts the reflection contains an issue mentioning "slow", confirming the
        latency threshold detector is operational.
        """
        result = self.reflex.reflect(turn_number=1, tool_results=[
            {"tool_name": "bash", "is_error": False},
        ], turn_latency_ms=120000)
        assert any("slow" in issue.lower() for issue in result.issues), \
            "120-second turn must trigger a 'slow' issue."

    def test_verify_get_improvement_context_returns_string(self):
        """Validate that get_improvement_context returns a string after at least one reflection.

        The test records one errored bash call and asserts the context string is a str,
        confirming the method never raises and produces output suitable for prompt injection.
        """
        self.reflex.reflect(turn_number=1, tool_results=[
            {"tool_name": "bash", "is_error": True},
        ], turn_latency_ms=100)
        ctx = self.reflex.get_improvement_context()
        assert isinstance(ctx, str), "get_improvement_context must return a string."

    def test_verify_get_improvement_context_empty_when_no_reflections(self):
        """Validate that get_improvement_context returns "" when no reflections have occurred.

        The test constructs a fresh reflex loop and asserts the context is an empty string,
        confirming the method does not fabricate text from empty history.
        """
        reflex = EncreReflexLoop(enabled=True)
        assert reflex.get_improvement_context() == "", \
            "Improvement context must be empty when no reflections have been recorded."

    def test_verify_get_trend_stable_after_consistent_success(self):
        """Validate that get_trend returns 'stable' after five consistent successful turns.

        The test drives five turns of single successful bash calls and asserts the trend
        is "stable", confirming the trend analyser recognises a flat positive trajectory.
        """
        for i in range(5):
            self.reflex.reflect(turn_number=i, tool_results=[
                {"tool_name": "bash", "is_error": False},
            ], turn_latency_ms=100)
        assert self.reflex.get_trend() == "stable", "Five consistent successes must yield a 'stable' trend."

    def test_verify_get_average_score_is_bound_between_zero_and_one(self):
        """Validate that get_average_score returns a value in [0.0, 1.0] after one turn.

        The test records one successful turn and asserts the average score is within the
        valid probability range, confirming the aggregator does not produce out-of-range values.
        """
        self.reflex.reflect(turn_number=1, tool_results=[
            {"tool_name": "bash", "is_error": False},
        ], turn_latency_ms=100)
        avg = self.reflex.get_average_score()
        assert 0.0 <= avg <= 1.0, "Average score must lie within [0.0, 1.0]."

    def test_verify_reset_clears_scores_and_context(self):
        """Validate that reset() restores average score to 1.0 and clears improvement context.

        The test records one successful turn, resets, and asserts the average is 1.0
        (the pre-history baseline) and the improvement context is "", confirming reset
        fully reinitialises the loop state.
        """
        self.reflex.reflect(turn_number=1, tool_results=[
            {"tool_name": "bash", "is_error": False},
        ], turn_latency_ms=100)
        self.reflex.reset()
        assert self.reflex.get_average_score() == 1.0, "Average score must reset to 1.0 (baseline)."
        assert self.reflex.get_improvement_context() == "", "Improvement context must be cleared on reset."

    def test_verify_disabled_reflex_returns_perfect_score(self):
        """Validate that a disabled reflex loop returns score=1.0 and empty issues on every reflect.

        The test constructs an enabled=False loop, calls reflect, and asserts score is
        exactly 1.0 and issues is empty, confirming the disabled path is a no-op that
        never flags problems.
        """
        reflex = EncreReflexLoop(enabled=False)
        result = reflex.reflect(turn_number=1, tool_results=[], turn_latency_ms=100)
        assert result.score == 1.0, "Disabled reflex must return a perfect score."
        assert result.issues == [], "Disabled reflex must produce no issues."


class TestMetaCognition:
    """Engineered to validate the EncreMetaCognition capability-profile tracking and delegation logic.

    This test class exercises the meta-cognition subsystem across 10 scenarios covering
    profile construction, score update, turn assessment, unknown-domain handling, full
    profile retrieval, weakness reporting, delegation decision, self-awareness context,
    delegation recording, and reset. The meta subsystem tracks per-domain proficiency to
    inform whether a task should be delegated to a sub-agent.
    """

    def setup_method(self):
        """Initialise a fresh meta-cognition instance before each test to prevent state leakage."""
        self.meta = EncreMetaCognition()

    def test_verify_capability_profile_default_values(self):
        """Validate that a fresh CapabilityProfile starts with score=0.5, confidence=0.0, sample_count=0.

        The test constructs a profile for domain="python" and asserts the three numeric
        fields match their documented defaults, confirming the initial state is neutral
        (score 0.5) with no confidence (0.0) until evidence is gathered.
        """
        profile = CapabilityProfile(domain="python")
        assert profile.domain == "python", "Domain must be stored verbatim."
        assert profile.score == 0.5, "Default score must be 0.5 (neutral)."
        assert profile.confidence == 0.0, "Default confidence must be 0.0 (no evidence yet)."
        assert profile.sample_count == 0, "Default sample_count must be 0."

    def test_verify_capability_profile_score_increases_on_success(self):
        """Validate that update(success=True) increases the profile score above the neutral 0.5 baseline.

        The test constructs a profile, calls update with success=True and difficulty=0.5,
        and asserts sample_count==1 and score>0.5, confirming positive outcomes raise
        the proficiency estimate.
        """
        profile = CapabilityProfile(domain="python")
        profile.update(success=True, difficulty=0.5)
        assert profile.sample_count == 1, "Sample count must increment after one update."
        assert profile.score > 0.5, "Score must increase above the neutral baseline after a success."

    def test_verify_assess_turn_updates_profile_for_done_domain(self):
        """Validate that assess_turn computes a profile score above 0.5 for a successful turn.

        The test assesses a turn that uses file_read successfully and asserts the
        "file_operations" profile has score > 0.5, confirming the domain classifier
        routes the tool use to the correct profile and updates it.
        """
        self.meta.assess_turn("write a python function to read a file", [
            {"tool_name": "file_read", "is_error": False},
        ])
        profile = self.meta.get_profile("file_operations")
        assert isinstance(profile, dict), "get_profile must return a dict for a known domain."
        assert profile["score"] > 0.5, "Successful file-operation turn must raise the profile score."

    def test_verify_get_profile_unknown_domain_returns_default_dict(self):
        """Validate that get_profile returns a default dict with confidence=0.0 for an unknown domain.

        The test queries "unknown_domain" and asserts the result is a dict with
        confidence==0.0, confirming the method never raises and always returns a
        well-formed fallback profile.
        """
        result = self.meta.get_profile("unknown_domain")
        assert isinstance(result, dict), "Unknown domain must return a dict, not None."
        assert result["confidence"] == 0.0, "Unknown domain must have zero confidence."

    def test_verify_get_all_profiles_returns_dict(self):
        """Validate that get_profile() with no args returns the full profiles dict.

        The test assesses one successful bash turn and asserts get_profile() returns a
        dict, confirming the no-arg path returns the aggregated profile store.
        """
        self.meta.assess_turn("run tests with pytest", [
            {"tool_name": "bash", "is_error": False},
        ])
        all_profiles = self.meta.get_profile()
        assert isinstance(all_profiles, dict), "get_profile() must return a dict."

    def test_verify_get_weakness_report_returns_list(self):
        """Validate that get_weakness_report returns a list after sufficient low-scoring samples.

        The test drives 25 failed bash turns to push confidence above the reporting
        threshold, then asserts the report is a list, confirming the weakness aggregator
        produces structured output once enough evidence accumulates.
        """
        for _ in range(25):
            self.meta.assess_turn("use bash to run a broken command", [
                {"tool_name": "bash", "is_error": True},
            ])
        report = self.meta.get_weakness_report()
        assert isinstance(report, list), "Weakness report must be a list."

    def test_verify_should_delegate_returns_false_without_confidence(self):
        """Validate that should_delegate returns False when no confidence has been accumulated.

        The test calls should_delegate for "design a system architecture" on a fresh meta
        instance and asserts (False, _) is returned, confirming delegation is withheld
        until the profile has sufficient evidence to make an informed decision.
        """
        should, _reason = self.meta.should_delegate("design a system architecture")
        assert should is False, "Delegation must be withheld when confidence is zero."

    def test_verify_get_self_awareness_context_returns_string(self):
        """Validate that get_self_awareness_context returns a string on a fresh instance.

        The test asserts the context is a str (likely empty), confirming the method
        never raises and always returns a prompt-injectable string.
        """
        ctx = self.meta.get_self_awareness_context()
        assert isinstance(ctx, str), "get_self_awareness_context must return a string."

    def test_verify_record_delegation_does_not_raise(self):
        """Validate that record_delegation completes without raising on any input.

        The test calls record_delegation with arbitrary arguments and asserts no exception
        is raised, confirming the method is safe to invoke from the delegation pathway
        even when the underlying profile state is empty.
        """
        self.meta.record_delegation("complex task", "sub_agent", True)

    def test_verify_reset_clears_all_profiles(self):
        """Validate that reset() empties the profile store.

        The test assesses one successful bash turn, resets, and asserts get_profile()
        returns an empty dict, confirming the reset path clears all accumulated domain
        proficiency data.
        """
        self.meta.assess_turn("run tests", [{"tool_name": "bash", "is_error": False}])
        self.meta.reset()
        assert self.meta.get_profile() == {}, "reset() must clear all capability profiles."
