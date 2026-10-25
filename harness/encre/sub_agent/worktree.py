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

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WorktreeIsolation:
    """Isolates a sub-agent in a temporary worktree.

    Creates a copy of the relevant workspace area so the sub-agent can
    read/write without affecting the parent's working directory.  On
    completion, changed files can be synced back.

    Two strategies:
    1. **Copy**: recursive copy of workspace files into a temp dir.
       Simple, no git dependency.  Used by default.
    2. **Git worktree**: ``git worktree add`` for the full repo.
       Preserves git history in the sub-agent.  Used when the workspace
       is a git repo and ``use_git_worktree=True``.
    """

    workspace_root: str
    temp_dir: str = ""
    use_git_worktree: bool = False
    _copied_paths: list[str] = field(default_factory=list, repr=False)
    _original_cwd: str = ""

    async def __aenter__(self) -> "WorktreeIsolation":
        self._original_cwd = os.getcwd()
        if self.use_git_worktree:
            self.temp_dir = self._create_git_worktree()
        else:
            self.temp_dir = self._create_copy_worktree()
        os.chdir(self.temp_dir)
        return self

    async def __aexit__(self, *args: Any) -> None:
        os.chdir(self._original_cwd)

    def sync_back(self, target_dir: str | None = None) -> list[str]:
        """Copy changed files from the temp dir back to the workspace.

        Returns a list of file paths that were updated.
        """
        target = target_dir or self.workspace_root
        if not self.temp_dir or not os.path.isdir(self.temp_dir):
            return []
        changed: list[str] = []
        for root, _dirs, files in os.walk(self.temp_dir):
            for fname in files:
                src = os.path.join(root, fname)
                rel = os.path.relpath(src, self.temp_dir)
                dst = os.path.join(target, rel)
                if os.path.isfile(dst):
                    with open(src, "rb") as f_src, open(dst, "rb") as f_dst:
                        if f_src.read() == f_dst.read():
                            continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                changed.append(rel)
        return changed

    def cleanup(self) -> None:
        if self.temp_dir and os.path.isdir(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = ""

    def _create_copy_worktree(self) -> str:
        td = tempfile.mkdtemp(prefix="encre_sub_agent_")
        root = Path(self.workspace_root)
        if root.is_dir():
            for entry in root.iterdir():
                if entry.name.startswith((".", "__pycache__", "node_modules", ".git")):
                    continue
                dst = Path(td) / entry.name
                if entry.is_dir():
                    shutil.copytree(str(entry), str(dst), symlinks=True, ignore_dangling_symlinks=True)
                else:
                    shutil.copy2(str(entry), str(dst))
                self._copied_paths.append(entry.name)
        return td

    def _create_git_worktree(self) -> str:
        import subprocess
        td = tempfile.mkdtemp(prefix="encre_sub_agent_")
        branch = f"_sub_agent_{os.urandom(4).hex()}"
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, td],
            cwd=self.workspace_root,
            capture_output=True,
            timeout=30,
        )
        return td
