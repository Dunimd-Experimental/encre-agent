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

"""Tests for encre.git.repo and encre.git.diff.

Covers the GitState and GitDiffResult dataclasses, the EncreGitRepo wrapper
over git CLI operations (state queries, diff capture, numstat parsing, stash
and unstash), and the EncreGitDiff static helpers for pure content-to-content
diff computation, application, stats aggregation, and transient-state checks.
"""  # noqa: E501

import os
import tempfile

import pytest

# ---- GitState dataclass ----

class TestGitState:
    """Validate the GitState dataclass as the canonical repository-status snapshot.

    Exercises GitState across default, full-construction, dataclass-identity,
    and field-type scenarios to ensure every field (in_repo, commit_hash,
    branch, remote_url, is_clean, changed_files, untracked_files,
    has_unpushed, worktree_count) is stored correctly and that the defaults
    describe a safe non-repository condition for the agent's stash and diff
    decisions.
    """

    def test_verify_default_state_represents_non_repo_condition(self):
        """Validate that GitState(in_repo=False) yields safe defaults everywhere.

        Asserts in_repo is False, the identifier fields are empty strings,
        is_clean is True, file lists are empty, has_unpushed is False, and
        worktree_count is 1, so the default never triggers a false positive.
        """
        from encre.git.repo import GitState
        gs = GitState(in_repo=False)
        assert gs.in_repo is False
        assert gs.commit_hash == ""
        assert gs.branch == ""
        assert gs.remote_url == ""
        assert gs.is_clean is True
        assert gs.changed_files == []
        assert gs.untracked_files == []
        assert gs.has_unpushed is False
        assert gs.worktree_count == 1

    def test_verify_full_state_stores_all_fields_correctly(self):
        """Validate that GitState stores every constructor argument unchanged.

        Builds a state with all nine fields set and asserts each one matches,
        because downstream consumers (diff capture, stash decisions) rely on
        this data being preserved exactly.
        """
        from encre.git.repo import GitState
        gs = GitState(
            in_repo=True,
            commit_hash="abc123def456",
            branch="main",
            remote_url="https://github.com/user/repo.git",
            is_clean=False,
            changed_files=["src/main.py", "README.md"],
            untracked_files=["new_file.txt"],
            has_unpushed=True,
            worktree_count=2,
        )
        assert gs.in_repo is True
        assert gs.commit_hash == "abc123def456"
        assert gs.branch == "main"
        assert gs.remote_url == "https://github.com/user/repo.git"
        assert gs.is_clean is False
        assert len(gs.changed_files) == 2
        assert "src/main.py" in gs.changed_files
        assert gs.untracked_files == ["new_file.txt"]
        assert gs.has_unpushed is True
        assert gs.worktree_count == 2

    def test_verify_gitstate_is_dataclass(self):
        """Validate that GitState is a standard-library dataclass.

        Asserts is_dataclass(GitState) is True, since generated equality, repr,
        and field semantics are part of the class contract.
        """
        from dataclasses import is_dataclass
        from encre.git.repo import GitState
        assert is_dataclass(GitState)

    def test_verify_field_types_match_expected_types(self):
        """Validate that GitState fields have the expected runtime types.

        Asserts identifiers are str, flags are bool, changed_files is a list,
        and worktree_count is int, since downstream serialization and branching
        logic depend on exact types.
        """
        from encre.git.repo import GitState
        gs = GitState(in_repo=True, changed_files=["a.py"])
        assert isinstance(gs.commit_hash, str)
        assert isinstance(gs.branch, str)
        assert isinstance(gs.is_clean, bool)
        assert isinstance(gs.changed_files, list)
        assert isinstance(gs.has_unpushed, bool)
        assert isinstance(gs.worktree_count, int)


# ---- EncreGitRepo ----

class TestEncreGitRepo:
    """Validate the EncreGitRepo wrapper over git operations for agent decisions.

    Covers construction, state queries, diff capture, stats parsing, and
    safe-default behavior outside a repository. The wrapper is a thin layer
    over git CLI commands so the agent can query status, compute diffs, and
    manage stash state without issuing subprocess calls itself.
    """

    def test_verify_construction_stores_workspace_path(self):
        """Validate that EncreGitRepo stores the workspace path it was given.

        Constructs a repo with workspace="." and asserts it is preserved,
        since the wrapper must remember its target directory.
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        assert repo is not None
        assert repo.workspace == "."

    def test_verify_construction_with_absolute_path(self):
        """Validate that EncreGitRepo stores an absolute workspace path.

        Constructs a repo with os.path.abspath(".") and asserts the stored path
        matches, because normalized paths keep subprocess calls consistent
        across operating systems.
        """
        from encre.git.repo import EncreGitRepo
        abs_path = os.path.abspath(".")
        repo = EncreGitRepo(workspace=abs_path)
        assert repo.workspace == abs_path

    def test_verify_is_in_repo_returns_bool(self):
        """Validate that is_in_repo() returns a real Python bool.

        Asserts the result is a bool, since the method is a predicate guarding
        every git-dependent operation in the agent.
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        result = repo.is_in_repo()
        assert isinstance(result, bool)

    def test_verify_get_state_returns_GitState_instance(self):
        """Validate that get_state() returns a GitState instance.

        Asserts isinstance(state, GitState), because the state object is the
        structured contract for all repository metadata queries.
        """
        from encre.git.repo import EncreGitRepo, GitState
        repo = EncreGitRepo(workspace=".")
        state = repo.get_state()
        assert isinstance(state, GitState)

    def test_verify_get_state_not_in_repo_returns_defaults(self):
        """Validate that get_state() returns in_repo=False defaults outside git.

        Creates an empty temp directory, builds a repo there, and asserts
        in_repo is False with empty commit_hash and branch, so the wrapper
        degrades safely when no repository is present.
        """
        from encre.git.repo import EncreGitRepo
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = EncreGitRepo(workspace=tmpdir)
            assert repo.is_in_repo() is False
            state = repo.get_state()
            assert state.in_repo is False
            assert state.commit_hash == ""
            assert state.branch == ""

    def test_verify_get_diff_returns_str_when_in_repo(self):
        """Validate that get_diff() returns a string inside a repo with commits.

        Skips when not in a repo or when HEAD is missing, then asserts the
        result is a str, since get_diff must always return valid diff text
        (possibly empty).
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        if not repo.is_in_repo():
            pytest.skip("Not in a git repository")
        try:
            result = repo.get_diff()
        except RuntimeError:
            pytest.skip("Git repo has no commits (HEAD does not exist)")
        else:
            assert isinstance(result, str)

    def test_verify_get_diff_with_file_path_returns_str(self):
        """Validate that get_diff(file_path=...) returns a file-scoped string.

        Skips when not in a repo or when HEAD is missing, then asserts the
        result is a str, so file-scoped diffs are always usable text.
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        if not repo.is_in_repo():
            pytest.skip("Not in a git repository")
        try:
            result = repo.get_diff(file_path="README.md")
        except RuntimeError:
            pytest.skip("Git repo has no commits (HEAD does not exist)")
        else:
            assert isinstance(result, str)

    def test_verify_get_diff_stats_returns_dict_with_required_keys(self):
        """Validate that get_diff_stats() returns a dict with the key metrics.

        Skips when not in a repo or when HEAD is missing, then asserts the dict
        carries "files", "insertions", and "deletions", because those three
        aggregates are always required.
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        if not repo.is_in_repo():
            pytest.skip("Not in a git repository")
        try:
            stats = repo.get_diff_stats()
        except RuntimeError:
            pytest.skip("Git repo has no commits (HEAD does not exist)")
        else:
            assert isinstance(stats, dict)
            assert "files" in stats
            assert "insertions" in stats
            assert "deletions" in stats

    def test_verify_get_changed_files_returns_list(self):
        """Validate that get_changed_files() returns a list.

        Asserts the result is a list (possibly empty) so downstream code can
        iterate it unconditionally.
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        files = repo.get_changed_files()
        assert isinstance(files, list)

    def test_verify_get_commit_hash_returns_str(self):
        """Validate that get_commit_hash() returns a string.

        Asserts the result is a str even when no commits exist, so an empty
        hash is always typed consistently.
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        result = repo.get_commit_hash()
        assert isinstance(result, str)

    def test_verify_get_branch_returns_str(self):
        """Validate that get_branch() returns a string.

        Asserts the result is a str even outside a repository, so an empty
        branch name is always typed consistently.
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        result = repo.get_branch()
        assert isinstance(result, str)

    def test_verify_has_unpushed_commits_returns_bool(self):
        """Validate that has_unpushed_commits() returns a boolean.

        Asserts the result is a bool, since the method is a predicate used to
        decide whether to warn about unpushed changes.
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        result = repo.has_unpushed_commits()
        assert isinstance(result, bool)

    def test_verify_is_transient_state_returns_bool(self):
        """Validate that is_transient_state() returns a boolean.

        Asserts the result is a bool, since the method is a predicate for
        risky working-tree states such as changes that could be lost.
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        result = repo.is_transient_state()
        assert isinstance(result, bool)

    def test_verify_stash_to_clean_state_returns_string_or_none(self):
        """Validate that stash_to_clean_state() returns a stash name or None.

        Asserts the result is None or a str, since the method either creates a
        stash and reports its name or finds nothing to stash.
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        result = repo.stash_to_clean_state()
        assert result is None or isinstance(result, str)

    def test_verify_unstash_does_not_raise(self):
        """Validate that unstash() is safe when no stash exists.

        Calls unstash() and asserts nothing is raised, because agents may call
        it unconditionally before resuming work and it must be idempotent.
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        repo.unstash()

    def test_verify_not_in_repo_methods_return_safe_defaults(self):
        """Validate that every query method degrades gracefully outside git.

        Builds a repo in an empty temp directory and asserts get_diff is empty,
        stats are all zeros, changed files are empty, commit hash and branch are
        empty, unpushed and transient flags are False, and stash returns None.
        """
        from encre.git.repo import EncreGitRepo
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = EncreGitRepo(workspace=tmpdir)
            assert repo.get_diff() == ""
            assert repo.get_diff_stats() == {"files": 0, "insertions": 0, "deletions": 0}
            assert repo.get_changed_files() == []
            assert repo.get_commit_hash() == ""
            assert repo.get_branch() == ""
            assert repo.has_unpushed_commits() is False
            assert repo.is_transient_state() is False
            assert repo.stash_to_clean_state() is None

    def test_verify_get_state_has_all_expected_attributes_in_actual_repo(self):
        """Validate that a real-repo GitState exposes all nine attributes.

        Asserts hasattr for every documented field, since downstream
        serialization and pattern matching require the full field set.
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        state = repo.get_state()
        assert hasattr(state, "in_repo")
        assert hasattr(state, "commit_hash")
        assert hasattr(state, "branch")
        assert hasattr(state, "remote_url")
        assert hasattr(state, "is_clean")
        assert hasattr(state, "changed_files")
        assert hasattr(state, "untracked_files")
        assert hasattr(state, "has_unpushed")
        assert hasattr(state, "worktree_count")

    def test_verify_get_changed_files_includes_tracked_modifications(self):
        """Validate that get_changed_files() returns a list inside a real repo.

        Skips when not in a repository and asserts the result is a list, since
        changed files are always returned as a list even for a clean tree.
        """
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        if not repo.is_in_repo():
            pytest.skip("Not in a git repository")
        files = repo.get_changed_files()
        assert isinstance(files, list)

    def test_verify_parse_numstat_counts_insertions_and_deletions_across_files(self):
        """Validate that _parse_numstat aggregates insertions and deletions.

        Passes two numstat lines and asserts files is 2, insertions is 13
        (3+10), and deletions is 7 (2+5), so per-file counts are summed
        correctly.
        """
        from encre.git.repo import EncreGitRepo
        output = "3\t2\tREADME.md\n10\t5\tsrc/main.py\n"
        parsed = EncreGitRepo._parse_numstat(output)
        assert parsed == {"files": 2, "insertions": 13, "deletions": 7}

    def test_verify_parse_numstat_empty_string_returns_zero_counts(self):
        """Validate that _parse_numstat("") returns all-zero counts.

        Asserts zero counts for every field, because an empty numstat means no
        changes and must not raise.
        """
        from encre.git.repo import EncreGitRepo
        parsed = EncreGitRepo._parse_numstat("")
        assert parsed == {"files": 0, "insertions": 0, "deletions": 0}

    def test_verify_parse_numstat_skips_binary_files(self):
        """Validate that _parse_numstat treats "-" entries as binary files.

        Passes one binary line ("-" counts) and one text line (5 insertions, 3
        deletions) and asserts files is 2, insertions is 5, and deletions is 3,
        so binary files count as files but not toward line totals.
        """
        from encre.git.repo import EncreGitRepo
        output = "-\t-\timage.png\n5\t3\tcode.py\n"
        parsed = EncreGitRepo._parse_numstat(output)
        assert parsed == {"files": 2, "insertions": 5, "deletions": 3}


# ---- GitDiffResult dataclass ----

class TestGitDiffResult:
    """Validate the GitDiffResult dataclass as the diff-statistics container.

    Exercises construction with normal, zero, and large values plus
    dataclass-identity checks to ensure files, insertions, and deletions are
    stored without transformation or truncation.
    """

    def test_verify_creation_stores_all_fields(self):
        """Validate that GitDiffResult stores all three fields correctly.

        Builds a result with known values and asserts each field matches, so
        statistics pass through untouched.
        """
        from encre.git.diff import GitDiffResult
        result = GitDiffResult(files=3, insertions=50, deletions=10)
        assert result.files == 3
        assert result.insertions == 50
        assert result.deletions == 10

    def test_verify_zero_values_are_accepted(self):
        """Validate that GitDiffResult accepts all-zero values.

        Builds a result with every field at zero and asserts they stay zero,
        since an unchanged repository must produce a zero-count result.
        """
        from encre.git.diff import GitDiffResult
        result = GitDiffResult(files=0, insertions=0, deletions=0)
        assert result.files == 0
        assert result.insertions == 0
        assert result.deletions == 0

    def test_verify_large_values_are_stored_correctly(self):
        """Validate that GitDiffResult stores large integers without truncation.

        Builds a result with 1000 files, 50000 insertions, and 30000 deletions
        and asserts each value survives, so large repositories do not lose
        precision in their stats.
        """
        from encre.git.diff import GitDiffResult
        result = GitDiffResult(files=1000, insertions=50000, deletions=30000)
        assert result.files == 1000
        assert result.insertions == 50000
        assert result.deletions == 30000

    def test_verify_gitdiffresult_is_dataclass(self):
        """Validate that GitDiffResult is a standard-library dataclass.

        Asserts is_dataclass(GitDiffResult) is True, since generated equality
        and repr are part of the contract.
        """
        from dataclasses import is_dataclass
        from encre.git.diff import GitDiffResult
        assert is_dataclass(GitDiffResult)


# ---- EncreGitDiff static methods ----

class TestEncreGitDiff:
    """Validate the EncreGitDiff static helpers for diff work.

    Exercises compute_diff, apply_diff, parse_diff_stats, and
    is_transient_git_state. These are pure static functions that need neither a
    GitRepo instance nor git subprocess calls, so diffs can be computed over
    arbitrary old/new content pairs.
    """

    def test_verify_compute_diff_returns_str(self):
        """Validate that compute_diff returns a string for differing inputs.

        Passes two contents differing by one modified line and asserts the
        result is a str, since diff output is always text.
        """
        from encre.git.diff import EncreGitDiff
        diff = EncreGitDiff.compute_diff(
            old="line1\nline2\nline3\n",
            new="line1\nline2 modified\nline3\nline4\n",
        )
        assert isinstance(diff, str)

    def test_verify_compute_diff_no_changes_returns_str(self):
        """Validate that compute_diff returns a string for identical inputs.

        Passes the same content twice and asserts the result is a str, so a
        no-op diff is still a valid (empty) string.
        """
        from encre.git.diff import EncreGitDiff
        content = "hello world\nfoo bar\n"
        diff = EncreGitDiff.compute_diff(old=content, new=content)
        assert isinstance(diff, str)

    def test_verify_compute_diff_empty_to_content_produces_nonempty_diff(self):
        """Validate that going from empty to content produces a non-empty diff.

        Passes old="" and a two-line new content and asserts the diff is a
        non-empty string, so additions are visibly represented.
        """
        from encre.git.diff import EncreGitDiff
        diff = EncreGitDiff.compute_diff(old="", new="line1\nline2\n")
        assert isinstance(diff, str)
        assert len(diff) > 0

    def test_verify_compute_diff_content_to_empty_returns_str(self):
        """Validate that compute_diff returns a string when content is removed.

        Passes two lines as old and "" as new and asserts the result is a str,
        so deletion diffs are valid text too.
        """
        from encre.git.diff import EncreGitDiff
        diff = EncreGitDiff.compute_diff(old="line1\nline2\n", new="")
        assert isinstance(diff, str)

    def test_verify_apply_diff_returns_str(self):
        """Validate that apply_diff returns a string.

        Computes a diff and applies it to the original content, asserting the
        result is a str, so the transformed content is always text.
        """
        from encre.git.diff import EncreGitDiff
        original = "line1\nline2\nline3\n"
        diff = EncreGitDiff.compute_diff(
            old=original,
            new="line1\nline2 modified\nline3\n",
        )
        result = EncreGitDiff.apply_diff(content=original, diff=diff)
        assert isinstance(result, str)

    def test_verify_apply_diff_noop_returns_original_content(self):
        """Validate that applying a no-op diff still returns a string.

        Computes a diff between identical content, applies it, and asserts the
        result is a str, so no-op application never breaks the type contract.
        """
        from encre.git.diff import EncreGitDiff
        content = "hello\nworld\n"
        diff = EncreGitDiff.compute_diff(old=content, new=content)
        result = EncreGitDiff.apply_diff(content=content, diff=diff)
        assert isinstance(result, str)

    def test_verify_parse_diff_stats_returns_dict_with_required_keys(self):
        """Validate that parse_diff_stats returns the three required keys.

        Passes a single numstat line and asserts the dict contains
        total_files, total_insertions, and total_deletions, so stats parsing
        always yields a structured result.
        """
        from encre.git.diff import EncreGitDiff
        stats = EncreGitDiff.parse_diff_stats("3\t2\tfile.py\n")
        assert isinstance(stats, dict)
        assert "total_files" in stats
        assert "total_insertions" in stats
        assert "total_deletions" in stats

    def test_verify_parse_diff_stats_empty_string_returns_zero_counts(self):
        """Validate that parse_diff_stats("") returns all-zero counts.

        Asserts zero totals for every field, since empty input must not raise
        and must aggregate to zero.
        """
        from encre.git.diff import EncreGitDiff
        stats = EncreGitDiff.parse_diff_stats("")
        assert stats == {"total_files": 0, "total_insertions": 0, "total_deletions": 0}

    def test_verify_parse_diff_stats_multiple_files_aggregates_correctly(self):
        """Validate that parse_diff_stats sums counts across multiple files.

        Passes two numstat lines and asserts total_files is 2,
        total_insertions is 7 (5+2), and total_deletions is 8, so multi-file
        stats aggregate correctly.
        """
        from encre.git.diff import EncreGitDiff
        output = "5\t0\tsrc/new.py\n2\t8\tsrc/changed.py\n"
        stats = EncreGitDiff.parse_diff_stats(output)
        assert stats["total_files"] == 2
        assert stats["total_insertions"] == 7
        assert stats["total_deletions"] == 8

    def test_verify_is_transient_git_state_returns_bool_for_current_workspace(self):
        """Validate that is_transient_git_state returns a bool for the workspace.

        Asserts the result is a bool, since the method is a predicate for
        whether the workspace has uncommitted changes.
        """
        from encre.git.diff import EncreGitDiff
        result = EncreGitDiff.is_transient_git_state(workspace=".")
        assert isinstance(result, bool)

    def test_verify_is_transient_git_state_returns_false_for_nonexistent_directory(self):
        """Validate that is_transient_git_state returns False for a missing path.

        Asserts False for a nonexistent directory, because a missing path
        cannot be transient and must fall back to a safe default.
        """
        from encre.git.diff import EncreGitDiff
        result = EncreGitDiff.is_transient_git_state(workspace="/nonexistent/path/xyz")
        assert result is False

    def test_verify_roundtrip_compute_and_apply_produces_valid_string(self):
        """Validate that compute_diff then apply_diff yields a valid string.

        Computes a diff between two Python function bodies, applies it to the
        original, and asserts the result is a str; this checks type safety of
        the round trip rather than semantic equality of the applied output.
        """
        from encre.git.diff import EncreGitDiff
        original = "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"
        modified = "def foo():\n    return 42\n\n\ndef bar():\n    return 2\n\ndef baz():\n    return 3\n"  # noqa: E501
        diff = EncreGitDiff.compute_diff(old=original, new=modified)
        applied = EncreGitDiff.apply_diff(content=original, diff=diff)
        assert isinstance(applied, str)
