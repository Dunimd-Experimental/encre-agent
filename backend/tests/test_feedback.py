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

"""Tests for encre.feedback -- error correction learner with Jaccard similarity."""

from pathlib import Path

from encre.improve.feedback import CorrectionRecord, EncreFeedbackLearner
from encre.improve.feedback.learner import cut_str

# ---- CorrectionRecord ---------------------------------------------------

class TestCorrectionRecord:
    """Engineered to validate the CorrectionRecord data contract.

    This test class exercises :class:`CorrectionRecord` across full construction,
    serialization, deserialization, partial-deserialization, and round-trip
    scenarios to ensure that the correction record can survive storage cycles
    and still carry the tool name, error type, context, and user-supplied
    fix needed by the feedback learner to bias future attempts.
    """

    def test_verify_correction_record_defaults(self):
        """Validate that CorrectionRecord applies correct default values.

        The test constructs a record with only the required fields and asserts
        the optional counters and stale flag are set to their defaults because
        the learner initializes new records without invoking every setter.
        """
        rec = CorrectionRecord(
            tool_name="bash",
            error_type="execution_error",
            error_context="ls /nonexistent",
            user_correction="use ls /tmp instead",
        )
        assert rec.tool_name == "bash"
        assert rec.error_type == "execution_error"
        assert rec.error_context == "ls /nonexistent"
        assert rec.user_correction == "use ls /tmp instead"
        # trigger_count and missed_count start at zero for a fresh record.
        assert rec.trigger_count == 0
        assert rec.missed_count == 0
        # stale defaults to False so the record is considered active.
        assert rec.stale is False
        # timestamp must be a positive epoch value for ordering.
        assert rec.timestamp > 0

    def test_verify_to_dict_serializes_all_fields(self):
        """Validate that to_dict produces a full payload for storage.

        The test serializes a record and asserts that every semantic field
        appears in the dict because the learner persists and reloads records
        using this dict as the interchange format.
        """
        rec = CorrectionRecord(
            tool_name="file_write",
            error_type="type_error",
            error_context="content must be str",
            user_correction="convert to str first",
        )
        d = rec.to_dict()
        assert d["tool_name"] == "file_write"
        assert d["error_type"] == "type_error"
        assert d["error_context"] == "content must be str"
        assert d["user_correction"] == "convert to str first"
        assert d["trigger_count"] == 0
        assert d["missed_count"] == 0
        assert d["stale"] is False

    def test_verify_from_dict_deserializes_full_record(self):
        """Validate that from_dict reconstructs a record from persisted data.

        The test passes a pre-populated dictionary (including optional fields)
        and asserts every field survives reconstruction because the learner
        loads records from disk on startup and must not lose any metadata.
        """
        data = {
            "tool_name": "bash",
            "error_type": "syntax_error",
            "error_context": "missing semicolon",
            "user_correction": "add semicolon",
            "timestamp": 1234567890.0,
            "trigger_count": 3,
            "missed_count": 1,
            "stale": False,
        }
        rec = CorrectionRecord.from_dict(data)
        assert rec.tool_name == "bash"
        assert rec.error_type == "syntax_error"
        assert rec.error_context == "missing semicolon"
        assert rec.user_correction == "add semicolon"
        assert rec.timestamp == 1234567890.0
        assert rec.trigger_count == 3
        assert rec.missed_count == 1
        assert rec.stale is False

    def test_verify_from_dict_minimal(self):
        """Validate that from_dict tolerates a minimal payload.

        The test passes only the four required fields and asserts the optional
        counters and stale flag are populated with safe defaults because the
        storage format may contain old records written before optional fields
        were introduced.
        """
        data = {
            "tool_name": "edit",
            "error_type": "parse_error",
            "error_context": "bad json",
            "user_correction": "validate json",
        }
        rec = CorrectionRecord.from_dict(data)
        assert rec.tool_name == "edit"
        assert rec.trigger_count == 0
        assert rec.missed_count == 0
        assert rec.stale is False

    def test_verify_roundtrip_preserves_all_fields(self):
        """Validate that to_dict + from_dict is a lossless round trip.

        The test builds a record, mutates trigger_count, serializes, deserializes,
        and asserts every field on the restored record matches the original
        because persistence cycles must never silently drop mutation state.
        """
        original = CorrectionRecord(
            tool_name="grep",
            error_type="regex_error",
            error_context="invalid pattern [a-z",
            user_correction="escape brackets: \\[a-z",
        )
        original.trigger_count = 5
        restored = CorrectionRecord.from_dict(original.to_dict())
        assert restored.tool_name == original.tool_name
        assert restored.error_type == original.error_type
        assert restored.error_context == original.error_context
        assert restored.user_correction == original.user_correction
        assert restored.trigger_count == original.trigger_count


# ---- cut_str helper -----------------------------------------------------

class TestCutStr:
    """Engineered to validate the cut_str truncation utility.

    This test class exercises :func:`cut_str` across short, exact-length,
    truncation, empty-string, and zero-max-length scenarios to ensure the
    helper produces safe, append-style truncated strings used when logging
    correction context without overflowing display buffers.
    """

    def test_verify_cut_str_short_string_untouched(self):
        """Validate that cut_str returns the input unchanged when under the limit.

        The test asserts the result equals the original string because no
        truncation is needed when the input is shorter than max_len.
        """
        assert cut_str("hello", 10) == "hello"

    def test_verify_cut_str_exact_length_untouched(self):
        """Validate that cut_str returns the input unchanged at exactly max_len.

        The test asserts byte-exact equality when len(input) == max_len because
        the boundary case must not insert the ellipsis suffix.
        """
        assert cut_str("1234567890", 10) == "1234567890"

    def test_verify_cut_str_truncation_appends_ellipsis(self):
        """Validate that cut_str truncates long strings and appends '...'.

        The test asserts the result length stays within the limit and ends
        with '...' because the suffix signals to the reader that content was
        dropped, which is important for log readability.
        """
        result = cut_str("this is a very long string that needs cutting", 14)
        assert len(result) <= 14
        assert result.endswith("...")

    def test_verify_cut_str_empty_string(self):
        """Validate that cut_str handles an empty string gracefully.

        The test asserts the empty input is returned unchanged because the
        helper must not crash on edge-case inputs from empty error contexts.
        """
        assert cut_str("", 5) == ""

    def test_verify_cut_str_max_len_zero(self):
        """Validate that cut_str does not crash when max_len is zero.

        The test asserts the result is still a string because a max_len of
        zero produces a negative slice bound, which Python handles by yielding
        an empty slice rather than raising.
        """
        result = cut_str("abc", 0)
        assert isinstance(result, str)


# ---- EncreFeedbackLearner -----------------------------------------------

class TestEncreFeedbackLearner:
    """Engineered to validate the EncreFeedbackLearner correction lifecycle.

    This test class exercises :class:`EncreFeedbackLearner` across initial state,
    new-correction insertion, duplicate-context update, multi-tool isolation,
    multi-error-type isolation, feedback retrieval, reset, and file-backed
    save/load scenarios to ensure the learner stabilizes repeated errors and
    surfaces relevant corrections without leaking cross-tool state.
    """

    def setup_method(self):
        """Create a fresh learner before each test to avoid cross-test leakage."""
        self.learner = EncreFeedbackLearner()

    def test_verify_initial_state_is_empty(self):
        """Validate that a fresh learner starts with zero records.

        The test asserts record_count and active_count are both zero because
        the learner must expose a clean slate after construction so that
        earlier test contamination cannot leak into later assertions.
        """
        assert self.learner.record_count == 0
        assert self.learner.active_count == 0

    def test_verify_record_correction_new_inserts_record(self):
        """Validate that record_correction inserts a new record and updates counts.

        The test records a single correction and asserts both record_count and
        active_count rise to one because the learner must register fresh
        corrections so they become available for future similarity matching.
        """
        self.learner.record_correction(
            "bash", "execution_error",
            "ls /nonexistent", "use ls /tmp"
        )
        assert self.learner.record_count == 1
        assert self.learner.active_count == 1

    def test_verify_record_correction_duplicate_increments_trigger(self):
        """Validate that a similar correction updates an existing record.

        The test records two closely related corrections against the same tool
        and error type and asserts the record count stays at one because the
        learner matches on similarity and should increment trigger_count rather
        than spawn duplicate entries that dilute signal.
        """
        self.learner.record_correction(
            "bash", "execution_error",
            "ls /nonexistent", "use ls /tmp"
        )
        self.learner.record_correction(
            "bash", "execution_error",
            "ls /nonexistent path", "use ls /tmp instead"
        )
        # Similar context should match and update existing record
        assert self.learner.record_count == 1
        assert self.learner.active_count == 1

    def test_verify_record_correction_different_tool_creates_separate_record(self):
        """Validate that corrections against different tools are stored separately.

        The test records two corrections for different tools and asserts
        record_count equals two because tool isolation prevents bash-specific
        fixes from contaminating edit-specific feedback.
        """
        self.learner.record_correction("bash", "execution_error", "ctx1", "fix1")
        self.learner.record_correction("edit", "type_error", "ctx2", "fix2")
        assert self.learner.record_count == 2

    def test_verify_record_correction_different_error_type_creates_separate_record(self):
        """Validate that corrections for different error types are stored separately.

        The test records two corrections against the same tool but different
        error types and asserts record_count equals two because execution
        errors and syntax errors have distinct remediation strategies.
        """
        self.learner.record_correction("bash", "execution_error", "ctx1", "fix1")
        self.learner.record_correction("bash", "syntax_error", "ctx2", "fix2")
        assert self.learner.record_count == 2

    def test_verify_get_relevant_feedback_empty_when_no_records(self):
        """Validate that get_relevant_feedback returns empty for an empty learner.

        The test asserts an empty string is returned because callers must not
        receive garbage or raise when no corrections have been recorded yet.
        """
        result = self.learner.get_relevant_feedback("bash", "some context")
        assert result == ""

    def test_verify_get_relevant_feedback_returns_context_when_matched(self):
        """Validate that get_relevant_feedback surfaces matching corrections.

        The test records two similar bash execution errors and asserts the
        feedback string is non-empty for a related query because the learner
        must surface prior fixes to help the agent avoid repeating mistakes.
        """
        self.learner.record_correction(
            "bash", "execution_error",
            "command not found ls /badpath", "use correct path"
        )
        self.learner.record_correction(
            "bash", "execution_error",
            "command not found cat /badpath", "check path exists"
        )
        result = self.learner.get_relevant_feedback("bash", "command not found ls")
        assert "Previous errors" in result or result != ""

    def test_verify_get_relevant_feedback_empty_when_no_match(self):
        """Validate that get_relevant_feedback returns empty for unrelated tool queries.

        The test records a bash correction and then queries for grep, asserting
        the result is empty because cross-tool feedback must not leak.
        """
        self.learner.record_correction(
            "bash", "execution_error",
            "command not found", "use correct command"
        )
        result = self.learner.get_relevant_feedback("grep", "pattern error")
        assert result == ""

    def test_verify_reset_clears_all_records(self):
        """Validate that reset empties the learner and resets counters.

        The test inserts two corrections, asserts record_count is two, then
        calls reset and asserts both counters drop to zero because operators
        need a clean way to discard stale learning between runs.
        """
        self.learner.record_correction("bash", "e", "c", "f")
        self.learner.record_correction("edit", "e", "c", "f")
        assert self.learner.record_count == 2
        self.learner.reset()
        assert self.learner.record_count == 0
        assert self.learner.active_count == 0

    def test_verify_save_and_load_persists_records(self, tmp_path: Path):
        """Validate that save/load round-trips records through a JSON file.

        The test inserts two corrections, persists to a temp path, constructs
        a new learner pointed at the same file, loads, and asserts the loaded
        learner reports two records because persistence is the mechanism that
        survives process restarts.
        """
        storage = str(tmp_path / "feedback.json")
        learner = EncreFeedbackLearner(storage_path=storage)
        learner.record_correction("bash", "execution_error", "ctx", "fix")
        learner.record_correction("edit", "type_error", "ctx2", "fix2")
        learner.save()

        # Load into a new learner
        learner2 = EncreFeedbackLearner(storage_path=storage)
        loaded = learner2.load()
        assert loaded is True
        assert learner2.record_count == 2

    def test_verify_load_nonexistent_file_returns_false(self):
        """Validate that load returns False when the storage file does not exist.

        The test points at a nonexistent path and asserts load returns False
        because the learner must not crash or silently create empty files on
        a missing-store read.
        """
        learner = EncreFeedbackLearner(storage_path="/nonexistent/path/file.json")
        result = learner.load()
        assert result is False

    def test_verify_load_without_storage_path_returns_false(self):
        """Validate that load returns False when no storage path is configured.

        The test constructs a learner with no storage_path and asserts load
        returns False because a learner without a persistence target cannot
        fulfill a load contract.
        """
        learner = EncreFeedbackLearner()  # no storage_path
        result = learner.load()
        assert result is False

    def test_verify_save_without_storage_path_does_not_raise(self):
        """Validate that save is a no-op when no storage path is configured.

        The test records a correction and calls save, asserting no exception
        is raised because callers may invoke save defensively after every
        mutation even when persistence is not configured.
        """
        learner = EncreFeedbackLearner()  # no storage_path
        learner.record_correction("bash", "e", "c", "f")
        # Should not raise
        learner.save()

    def test_verify_save_with_invalid_json_file_returns_false_on_load(self, tmp_path: Path):
        """Validate that load tolerates a corrupt JSON file gracefully.

        The test writes raw text into the storage path and asserts load returns
        False because the learner must not crash on disk corruption; it should
        treat the store as empty and continue.
        """
        storage = str(tmp_path / "bad.json")
        storage_file = Path(storage)
        storage_file.write_text("this is not json", encoding="utf-8")
        learner = EncreFeedbackLearner(storage_path=storage)
        result = learner.load()
        assert result is False


# ---- Jaccard Similarity -------------------------------------------------

class TestContextSimilarity:
    """Engineered to validate the Jaccard similarity scoring used by the learner.

    This test class exercises :meth:`EncreFeedbackLearner._context_similarity`
    across identical strings, disjoint strings, partial overlap, empty inputs,
    case-insensitive matching, and length-bonus semantics to ensure the
    similarity signal used for correction matching is stable and discriminative.
    """

    def setup_method(self):
        """Create a fresh learner before each test to avoid state leakage."""
        self.learner = EncreFeedbackLearner()

    def test_verify_identical_strings_score_high(self):
        """Validate that identical strings receive a similarity score near 1.0.

        The test asserts sim > 0.9 because exact matches should be treated as
        maximally relevant by the correction matcher.
        """
        sim = self.learner._context_similarity("hello world", "hello world")
        assert sim > 0.9

    def test_verify_completely_different_strings_score_zero(self):
        """Validate that disjoint token sets receive a similarity score of 0.0.

        The test asserts sim == 0.0 because completely unrelated contexts must
        not accidentally match similar-looking corrections.
        """
        sim = self.learner._context_similarity("foo bar baz", "x y z")
        assert sim == 0.0

    def test_verify_partial_overlap_yields_intermediate_score(self):
        """Validate that partially overlapping strings receive a moderate score.

        The test asserts the score falls in the (0.4, 1.0) range because the
        shared tokens ('command', 'not', 'found') should produce a meaningful
        but not maximal similarity signal.
        """
        sim = self.learner._context_similarity(
            "command not found ls",
            "command not found cat",
        )
        # "command", "not", "found" overlap, "ls" vs "cat" don't
        assert 0.4 < sim < 1.0

    def test_verify_empty_strings_score_zero(self):
        """Validate that any comparison involving an empty string scores 0.0.

        The test asserts similarity is 0.0 in all three empty-input permutations
        because an empty context provides no signal and must not match anything.
        """
        assert self.learner._context_similarity("", "abc") == 0.0
        assert self.learner._context_similarity("abc", "") == 0.0
        assert self.learner._context_similarity("", "") == 0.0

    def test_verify_case_insensitive_matching(self):
        """Validate that casing differences do not reduce similarity.

        The test asserts sim > 0.9 because the tokenizer should normalize case
        before computing Jaccard overlap so that 'HELLO World' and 'hello world'
        are treated as the same context.
        """
        sim = self.learner._context_similarity("HELLO World", "hello world")
        assert sim > 0.9

    def test_verify_length_bonus_favors_long_matching_tokens(self):
        """Validate that longer matching tokens receive a slight similarity boost.

        The test compares similarity on identical long-token strings against
        identical short-token strings and asserts the long-token case scores
        at least as highly because longer token matches are more discriminative
        and should not be penalized relative to single-character tokens.
        """
        sim_with_long = self.learner._context_similarity(
            "verylongtoken common",
            "verylongtoken common",
        )
        sim_without = self.learner._context_similarity(
            "x common",
            "x common",
        )
        assert sim_with_long >= sim_without


# ---- Pruning and Decay --------------------------------------------------

class TestPruning:
    """Engineered to validate the learner's record cap and stale-exclusion logic.

    This test class exercises the learner's internal pruning behavior when the
    record count exceeds MAX_RECORDS and the active_count exclusion of stale
    entries to ensure the feedback store remains bounded and observable metrics
    reflect only actionable corrections.
    """

    def test_verify_prune_at_max_records(self):
        """Validate that the learner prunes records when MAX_RECORDS is exceeded.

        The test inserts MAX_RECORDS + 10 corrections and asserts the final
        record_count does not exceed MAX_RECORDS because the learner must
        bound memory usage by dropping the oldest entries once capacity is
        reached.
        """
        learner = EncreFeedbackLearner()
        # Add records beyond MAX_RECORDS
        for i in range(learner.MAX_RECORDS + 10):
            learner.record_correction(
                f"tool_{i % 5}", f"error_{i % 3}",
                f"context_{i}", f"fix_{i}",
            )
        assert learner.record_count <= learner.MAX_RECORDS

    def test_verify_active_count_excludes_stale_records(self):
        """Validate that active_count ignores records marked as stale.

        The test inserts two records, marks one stale, and asserts active_count
        is one while record_count remains two because stale entries are retained
        for auditability but excluded from similarity searches.
        """
        learner = EncreFeedbackLearner()
        learner.record_correction("bash", "e", "c", "f")
        learner.record_correction("edit", "e", "c2", "f2")
        # Force first record to become stale
        learner._records[0].stale = True
        assert learner.active_count == 1
        assert learner.record_count == 2
