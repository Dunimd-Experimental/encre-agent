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

"""Tests for the encre.feedback error-correction learner.

Covers the CorrectionRecord serialization contract, the cut_str context
truncation helper, the full EncreFeedbackLearner correction lifecycle
(insert, deduplicate, retrieve, reset, save/load), the Jaccard similarity
scoring that drives correction matching, and the pruning and stale-exclusion
behavior that keeps the feedback store bounded.
"""

from pathlib import Path

from encre.improve.feedback import CorrectionRecord, EncreFeedbackLearner
from encre.improve.feedback.learner import cut_str

# ---- CorrectionRecord ---------------------------------------------------

class TestCorrectionRecord:
    """Validate the CorrectionRecord data contract.

    Exercises :class:`CorrectionRecord` across construction, serialization,
    deserialization, minimal-payload deserialization, and round-trip
    scenarios to ensure a correction record survives a storage cycle while
    still carrying the tool name, error type, context, and user-supplied fix
    that the feedback learner needs to bias future attempts.
    """

    def test_verify_correction_record_defaults(self):
        """Validate that CorrectionRecord applies correct default values.

        Builds a record with only the required fields and asserts the optional
        counters and stale flag fall back to their defaults, because the learner
        creates new records without calling every setter explicitly.
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
        # Fresh records start with zero activity counters.
        assert rec.trigger_count == 0
        assert rec.missed_count == 0
        # A fresh record is active, not stale.
        assert rec.stale is False
        # The timestamp is a real epoch value so records can be ordered.
        assert rec.timestamp > 0

    def test_verify_to_dict_serializes_all_fields(self):
        """Validate that to_dict emits a complete persistence payload.

        Serializes a record and asserts every semantic field is present,
        because the learner uses this dict as its interchange format when
        writing and reading records.
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
        """Validate that from_dict reconstructs a fully populated record.

        Passes a dictionary containing both required and optional fields and
        asserts nothing is lost during reconstruction, because records are
        reloaded from disk on startup and must retain their metadata.
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

        Passes only the four required fields and asserts the optional counters
        and stale flag are filled in with safe defaults, because the store may
        hold records written before optional fields existed.
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
        """Validate that to_dict followed by from_dict is lossless.

        Builds a record, mutates trigger_count, serializes, and deserializes,
        then asserts the restored record matches the original on every field,
        because persistence must never silently drop mutated state.
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
    """Validate the cut_str context-truncation helper.

    Exercises :func:`cut_str` across short input, exact-length input,
    truncation, empty input, and a zero max length to ensure the helper always
    returns a safe string, since truncated contexts are logged and must not
    overflow display buffers.
    """

    def test_verify_cut_str_short_string_untouched(self):
        """Validate that cut_str returns short input unchanged.

        Inputs shorter than max_len need no truncation, so the result must be
        byte-identical to the input.
        """
        assert cut_str("hello", 10) == "hello"

    def test_verify_cut_str_exact_length_untouched(self):
        """Validate that cut_str returns input unchanged at exactly max_len.

        The boundary case len(input) == max_len must not append the ellipsis
        suffix, so the result must equal the input exactly.
        """
        assert cut_str("1234567890", 10) == "1234567890"

    def test_verify_cut_str_truncation_appends_ellipsis(self):
        """Validate that cut_str truncates long input and appends "...".

        Asserts the result stays within the limit and ends with the ellipsis
        suffix, which signals to a reader that content was dropped.
        """
        result = cut_str("this is a very long string that needs cutting", 14)
        assert len(result) <= 14
        assert result.endswith("...")

    def test_verify_cut_str_empty_string(self):
        """Validate that cut_str handles an empty string gracefully.

        An empty context must round-trip unchanged, since the helper cannot
        crash on empty error contexts.
        """
        assert cut_str("", 5) == ""

    def test_verify_cut_str_max_len_zero(self):
        """Validate that cut_str does not crash when max_len is zero.

        A max length of zero produces a negative slice bound, which Python
        resolves as an empty slice rather than raising, so the result must
        still be a string.
        """
        result = cut_str("abc", 0)
        assert isinstance(result, str)


# ---- EncreFeedbackLearner -----------------------------------------------

class TestEncreFeedbackLearner:
    """Validate the EncreFeedbackLearner correction lifecycle.

    Exercises :class:`EncreFeedbackLearner` across initial state, new
    insertion, similar-context merging, tool isolation, error-type isolation,
    feedback retrieval, reset, and file-backed save/load to ensure the learner
    stabilizes recurring errors without leaking state across tools.
    """

    def setup_method(self):
        """Create a fresh learner before each test to prevent state leakage."""
        self.learner = EncreFeedbackLearner()

    def test_verify_initial_state_is_empty(self):
        """Validate that a fresh learner starts with no records.

        Asserts both counters read zero after construction, so results cannot
        be contaminated by state left behind by an earlier test.
        """
        assert self.learner.record_count == 0
        assert self.learner.active_count == 0

    def test_verify_record_correction_new_inserts_record(self):
        """Validate that record_correction inserts a new record.

        Records a single correction and asserts both record_count and
        active_count reach one, so the correction becomes available for later
        similarity matching.
        """
        self.learner.record_correction(
            "bash", "execution_error",
            "ls /nonexistent", "use ls /tmp"
        )
        assert self.learner.record_count == 1
        assert self.learner.active_count == 1

    def test_verify_record_correction_duplicate_increments_trigger(self):
        """Validate that a similar correction merges into an existing record.

        Records two closely related corrections for the same tool and error
        type and asserts the count stays at one, because the learner matches
        on similarity and should bump trigger_count rather than create a
        duplicate that dilutes the signal.
        """
        self.learner.record_correction(
            "bash", "execution_error",
            "ls /nonexistent", "use ls /tmp"
        )
        self.learner.record_correction(
            "bash", "execution_error",
            "ls /nonexistent path", "use ls /tmp instead"
        )
        # The near-identical context should merge into the existing record.
        assert self.learner.record_count == 1
        assert self.learner.active_count == 1

    def test_verify_record_correction_different_tool_creates_separate_record(self):
        """Validate that corrections for different tools are kept separate.

        Records corrections for two different tools and asserts the count is
        two, since tool isolation stops bash fixes contaminating edit feedback.
        """
        self.learner.record_correction("bash", "execution_error", "ctx1", "fix1")
        self.learner.record_correction("edit", "type_error", "ctx2", "fix2")
        assert self.learner.record_count == 2

    def test_verify_record_correction_different_error_type_creates_separate_record(self):
        """Validate that corrections for different error types are kept separate.

        Records two corrections for the same tool but different error types and
        asserts the count is two, because execution and syntax errors need
        distinct remediation strategies.
        """
        self.learner.record_correction("bash", "execution_error", "ctx1", "fix1")
        self.learner.record_correction("bash", "syntax_error", "ctx2", "fix2")
        assert self.learner.record_count == 2

    def test_verify_get_relevant_feedback_empty_when_no_records(self):
        """Validate that get_relevant_feedback is empty for an empty learner.

        Asserts an empty string is returned when nothing has been recorded, so
        callers never receive garbage or an exception on a cold learner.
        """
        result = self.learner.get_relevant_feedback("bash", "some context")
        assert result == ""

    def test_verify_get_relevant_feedback_returns_context_when_matched(self):
        """Validate that get_relevant_feedback surfaces matching corrections.

        Records two similar bash execution errors and asserts the feedback is
        non-empty for a related query, so prior fixes help the agent avoid
        repeating the same mistake.
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
        """Validate that get_relevant_feedback is empty for another tool.

        Records a bash correction then queries grep and asserts the result is
        empty, so feedback never leaks across tools.
        """
        self.learner.record_correction(
            "bash", "execution_error",
            "command not found", "use correct command"
        )
        result = self.learner.get_relevant_feedback("grep", "pattern error")
        assert result == ""

    def test_verify_reset_clears_all_records(self):
        """Validate that reset empties the learner and resets its counters.

        Inserts two corrections, confirms the count, then resets and asserts
        both counters drop to zero, giving operators a clean way to discard
        stale learning between runs.
        """
        self.learner.record_correction("bash", "e", "c", "f")
        self.learner.record_correction("edit", "e", "c", "f")
        assert self.learner.record_count == 2
        self.learner.reset()
        assert self.learner.record_count == 0
        assert self.learner.active_count == 0

    def test_verify_save_and_load_persists_records(self, tmp_path: Path):
        """Validate that save and load round-trip records through a JSON file.

        Inserts two corrections, persists them, loads them into a fresh learner
        pointed at the same file, and asserts two records are recovered, since
        persistence is what survives a process restart.
        """
        storage = str(tmp_path / "feedback.json")
        learner = EncreFeedbackLearner(storage_path=storage)
        learner.record_correction("bash", "execution_error", "ctx", "fix")
        learner.record_correction("edit", "type_error", "ctx2", "fix2")
        learner.save()

        # Reload the store into a brand new learner instance.
        learner2 = EncreFeedbackLearner(storage_path=storage)
        loaded = learner2.load()
        assert loaded is True
        assert learner2.record_count == 2

    def test_verify_load_nonexistent_file_returns_false(self):
        """Validate that load returns False for a missing storage file.

        Points the learner at a nonexistent path and asserts load returns
        False, so a missing store does not crash or silently create an empty
        file.
        """
        learner = EncreFeedbackLearner(storage_path="/nonexistent/path/file.json")
        result = learner.load()
        assert result is False

    def test_verify_load_without_storage_path_returns_false(self):
        """Validate that load returns False when no storage path is set.

        Builds a learner without a storage_path and asserts load returns False,
        because a learner with no persistence target cannot satisfy a load.
        """
        learner = EncreFeedbackLearner()  # no storage_path configured
        result = learner.load()
        assert result is False

    def test_verify_save_without_storage_path_does_not_raise(self):
        """Validate that save is a no-op when no storage path is set.

        Records a correction and calls save, asserting nothing is raised, since
        callers may invoke save defensively after every mutation even when
        persistence is not configured.
        """
        learner = EncreFeedbackLearner()  # no storage_path configured
        learner.record_correction("bash", "e", "c", "f")
        # Saving without a storage path must be a silent no-op.
        learner.save()

    def test_verify_save_with_invalid_json_file_returns_false_on_load(self, tmp_path: Path):
        """Validate that load tolerates a corrupt JSON file.

        Writes plain text into the storage path and asserts load returns False,
        because disk corruption must not crash the learner; the store is simply
        treated as empty.
        """
        storage = str(tmp_path / "bad.json")
        storage_file = Path(storage)
        storage_file.write_text("this is not json", encoding="utf-8")
        learner = EncreFeedbackLearner(storage_path=storage)
        result = learner.load()
        assert result is False


# ---- Jaccard Similarity -------------------------------------------------

class TestContextSimilarity:
    """Validate the Jaccard similarity scoring used for correction matching.

    Exercises :meth:`EncreFeedbackLearner._context_similarity` across identical
    strings, disjoint strings, partial overlap, empty inputs, case-insensitive
    input, and length-bonus behavior to ensure the matching signal is stable
    and discriminative.
    """

    def setup_method(self):
        """Create a fresh learner before each test to prevent state leakage."""
        self.learner = EncreFeedbackLearner()

    def test_verify_identical_strings_score_high(self):
        """Validate that identical strings score near 1.0.

        Asserts sim > 0.9, since an exact match must be treated as maximally
        relevant by the correction matcher.
        """
        sim = self.learner._context_similarity("hello world", "hello world")
        assert sim > 0.9

    def test_verify_completely_different_strings_score_zero(self):
        """Validate that disjoint token sets score exactly 0.0.

        Asserts sim == 0.0, so unrelated contexts never accidentally match a
        similar-looking correction.
        """
        sim = self.learner._context_similarity("foo bar baz", "x y z")
        assert sim == 0.0

    def test_verify_partial_overlap_yields_intermediate_score(self):
        """Validate that partial token overlap yields a moderate score.

        Asserts the score lands inside (0.4, 1.0), because the shared tokens
        ("command", "not", "found") should produce a meaningful but not maximal
        similarity signal.
        """
        sim = self.learner._context_similarity(
            "command not found ls",
            "command not found cat",
        )
        # "command", "not", and "found" overlap while "ls" and "cat" do not.
        assert 0.4 < sim < 1.0

    def test_verify_empty_strings_score_zero(self):
        """Validate that any empty-side comparison scores 0.0.

        Checks all three empty-input permutations and asserts zero similarity,
        since an empty context carries no signal and must match nothing.
        """
        assert self.learner._context_similarity("", "abc") == 0.0
        assert self.learner._context_similarity("abc", "") == 0.0
        assert self.learner._context_similarity("", "") == 0.0

    def test_verify_case_insensitive_matching(self):
        """Validate that case differences do not reduce similarity.

        Asserts sim > 0.9, because the tokenizer normalizes case before the
        Jaccard overlap, so "HELLO World" and "hello world" are one context.
        """
        sim = self.learner._context_similarity("HELLO World", "hello world")
        assert sim > 0.9

    def test_verify_length_bonus_favors_long_matching_tokens(self):
        """Validate that longer matching tokens get a slight similarity boost.

        Compares two identical-string pairs, one with a long token and one with
        a single-character token, and asserts the long-token pair scores at
        least as high, since longer matches are more discriminative and should
        not be penalized.
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
    """Validate the learner's record cap and stale-record exclusion.

    Exercises the internal pruning path when the record count exceeds
    MAX_RECORDS and the active_count filter that excludes stale entries, to
    ensure the feedback store stays bounded and its metrics report only
    actionable corrections.
    """

    def test_verify_prune_at_max_records(self):
        """Validate that the learner prunes once MAX_RECORDS is exceeded.

        Inserts MAX_RECORDS + 10 corrections and asserts the final count never
        exceeds MAX_RECORDS, so memory usage stays bounded by dropping the
        oldest entries.
        """
        learner = EncreFeedbackLearner()
        # Deliberately overflow the record cap to exercise pruning.
        for i in range(learner.MAX_RECORDS + 10):
            learner.record_correction(
                f"tool_{i % 5}", f"error_{i % 3}",
                f"context_{i}", f"fix_{i}",
            )
        assert learner.record_count <= learner.MAX_RECORDS

    def test_verify_active_count_excludes_stale_records(self):
        """Validate that active_count ignores stale records.

        Inserts two records, marks one stale, and asserts active_count is one
        while record_count stays two, so stale entries are retained for audit
        but excluded from similarity search.
        """
        learner = EncreFeedbackLearner()
        learner.record_correction("bash", "e", "c", "f")
        learner.record_correction("edit", "e", "c2", "f2")
        # Manually flag the first record as stale.
        learner._records[0].stale = True
        assert learner.active_count == 1
        assert learner.record_count == 2
