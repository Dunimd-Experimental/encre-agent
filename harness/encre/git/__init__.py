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

"""Public API for the ``encre.git`` package: repository state inspection and diffing.

This package bundles Encre's git helpers used by the agent to reason about a
working tree before and after making changes. It exposes two collaborating
pieces:

* :class:`~encre.git.diff.EncreGitDiff` -- a stateless helper that computes,
  applies, and parses git-style (unified / numstat) diffs. The heavy lifting of
  diff computation and application is delegated to a Rust-backed native module
  for speed; this class is the small, typed Python-facing surface.
* :class:`~encre.git.repo.EncreGitRepo` -- a wrapper around a local repository
  that snapshots its state (branch, head, status) into a :class:`GitState`.

Re-exports keep the common names importable directly from ``encre.git``::

    from encre.git import EncreGitDiff, EncreGitRepo, GitDiffResult, GitState
"""

# Re-export the diff engine and its result container.
from encre.git.diff import EncreGitDiff, GitDiffResult
# Re-export the repository wrapper and its snapshot state model.
from encre.git.repo import EncreGitRepo, GitState

__all__ = ["EncreGitDiff", "EncreGitRepo", "GitDiffResult", "GitState"]
