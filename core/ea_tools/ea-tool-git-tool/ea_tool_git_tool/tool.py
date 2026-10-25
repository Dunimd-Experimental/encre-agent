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

"""Module: builtin/git_tool.py

Git tool implementation for the Encre tool system.
"""
import asyncio
import shlex
from typing import Any

from encre.tools.base import build_tool
from encre.tools._encoding import decode_bytes


async def _git_execute(**kwargs: Any) -> str:
    """Git execute.

    Args:
        kwargs: Description of the kwargs parameter.
    """
    command = kwargs.get("command", "status")
    repo_path = kwargs.get("repo_path", ".")
    args = kwargs.get("args", "")

    cmd_parts = ["git", "-C", repo_path, command]

    if args:
        cmd_parts.extend(shlex.split(args))

    try:
        from encre.capabilities.process import (
            hidden_subprocess_kwargs as _hidden,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_hidden(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=120
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return "Error: Git command timed out after 120 seconds"
        output = decode_bytes(stdout) if stdout else ""
        if stderr:
            err_text = decode_bytes(stderr)
            if err_text:
                output += "\n" + err_text
        if proc.returncode and proc.returncode != 0:
            output += f"\nGit command exited with code {proc.returncode}"
        return output or "(no output)"
    except FileNotFoundError:
        return "Error: Git CLI not found. Is Git installed and in PATH?"
    except Exception as e:
        return f"Error executing git command: {e}"


git_tool = build_tool(
    name="git",
    description=(
        "Run Git CLI commands against a local repository to inspect history, manage "
        "branches, stage and commit changes, or sync with remotes. "
        "Use this for routine version-control tasks such as viewing status/diff/log, "
        "creating branches, committing, pushing, pulling, stashing, or cloning. "
        "Do NOT use this for GitHub-specific actions (issues, PRs, releases) - use the "
        "github tool instead; and avoid it for large binary asset history. "
        "Tips: pass raw subcommand flags via `args` (e.g. \"--oneline -n 10\"); specify "
        "`repo_path` when operating outside the working directory. "
        "Pitfalls: commands run with a 120s timeout, so long clones or pushes may be "
        "killed; destructive ops (force push, reset) are not guarded here - confirm "
        "intent before invoking."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["status", "diff", "log", "branch", "commit", "add", "push", "pull", "stash", "checkout", "clone"],
                "description": "The Git subcommand to execute (e.g. \"status\" for working-tree state, \"log\" for commit history, \"commit\" to record staged changes).",
            },
            "repo_path": {
                "type": "string",
                "description": "Filesystem path to the target Git repository; defaults to the current working directory if omitted.",
            },
            "args": {
                "type": "string",
                "description": "Extra flags or arguments appended to the subcommand, shell-split with shlex (e.g. \"--oneline -n 20\", \"-m \\\"fix: typo\\\"\").",
            },
        },
        "required": ["command"],
    },
    execute=_git_execute,
    intents=["coding"],
    category="code_intel",
    semantic_type="exec",
    is_destructive=True,
    is_concurrency_safe=lambda data: data.get("command") in ("status", "diff", "log", "branch", "stash"),
)
