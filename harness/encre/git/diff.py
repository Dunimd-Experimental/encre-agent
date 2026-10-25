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

"""Git-style diffing helpers for the Encre agent.

This module wraps the operations the agent needs to describe and replay changes
to text files: computing a unified diff between two blobs, applying a diff back
onto text, parsing ``git diff --numstat`` output into aggregate counts, and
detecting in-progress (transient) git operations.

Implementation notes:
    * Diff computation and application are delegated to a Rust-backed native
      module (``encre.native``) for speed; this module is a thin, typed Python
      facade over those primitives plus pure-Python parsing/inspection helpers.
    * Nothing here shells out to the ``git`` CLI; numstat parsing and transient
      state detection are filesystem/string operations only.
"""

import os
from dataclasses import dataclass

# Diff computation/application is delegated to the Rust-backed native module
# for speed; these wrappers expose a small, typed Python-facing surface.
from encre.native import apply_diff as _native_apply_diff
from encre.native import compute_diff as _native_compute_diff


@dataclass
class GitDiffResult:
    """Aggregate statistics describing a git diff.

    Attributes:
        files: Number of files that changed.
        insertions: Total number of inserted (added) lines.
        deletions: Total number of deleted (removed) lines.
    """
    files: int
    insertions: int
    deletions: int


class EncreGitDiff:
    """Stateless helper for computing, applying, and parsing git-style diffs."""

    @staticmethod
    def compute_diff(old: str, new: str) -> str:
        """Compute a unified diff between two text blobs via the native backend.

        Args:
            old: Original text content.
            new: Updated text content.

        Returns:
            A unified diff string describing how to turn ``old`` into ``new``.
        """
        return _native_compute_diff(old, new)

    @staticmethod
    def apply_diff(content: str, diff: str) -> str:
        """Apply a unified diff to text content via the native backend.

        Args:
            content: The original text to patch.
            diff: The unified diff to apply.

        Returns:
            The patched text content.
        """
        return _native_apply_diff(content, diff)

    @staticmethod
    def parse_diff_stats(diff_output: str) -> dict[str, int]:
        """Parse ``git diff --numstat`` output into aggregate counts.

        Each numstat line is tab-separated as ``<added>\\t<deleted>\\t<path>``,
        where a binary file is represented by ``-`` in the count columns.

        Args:
            diff_output: Raw numstat output text.

        Returns:
            A dict with ``total_files``, ``total_insertions`` and
            ``total_deletions`` keys.
        """
        # Running totals accumulated across every parsed numstat line.
        files = 0
        insertions = 0
        deletions = 0
        # Iterate each non-empty line of the numstat output.
        for line in diff_output.strip().split("\n"):
            if not line.strip():
                continue
            # numstat columns are tab-separated: added, deleted, path.
            parts = line.split("\t")
            if len(parts) >= 2:
                files += 1
                try:
                    # A "-" marks a binary file, counted as zero changed lines.
                    add = int(parts[0]) if parts[0] != "-" else 0
                    dlt = int(parts[1]) if parts[1] != "-" else 0
                    insertions += add
                    deletions += dlt
                except ValueError:
                    # Skip malformed count columns rather than failing.
                    pass
        return {"total_files": files, "total_insertions": insertions, "total_deletions": deletions}

    @staticmethod
    def is_transient_git_state(workspace: str) -> bool:
        """Return True if the repo is mid-operation (merge/rebase/etc.).

        Detects in-progress git operations by checking for marker files/dirs
        inside ``.git``. Useful for avoiding actions during unstable states.

        Args:
            workspace: Path to the repository working tree.

        Returns:
            True when a transient git state is detected, otherwise False.
        """
        # Locate the repository's .git metadata directory.
        git_dir = os.path.join(workspace, ".git")
        if not os.path.exists(git_dir):
            return False
        # Marker files/directories that indicate an in-progress operation.
        transient_names = [
            "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
            "BISECT_START", "rebase-merge", "rebase-apply",
        ]
        # True if any transient marker currently exists.
        return any(os.path.exists(os.path.join(git_dir, name)) for name in transient_names)
