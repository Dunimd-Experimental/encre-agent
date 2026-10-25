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

"""Built-in evaluation tasks for measuring agent quality.

Each task tests a specific capability: tool calling, file operations,
web search, code editing, multi-turn reasoning, etc.

Add new tasks here and they will be available via ``BUILTIN_TASKS``.
"""

from encre.improve.eval.runner import EvalTask

# Curated set of ready-to-run benchmark tasks covering core agent skills.
BUILTIN_TASKS: list[EvalTask] = [
    EvalTask(
        name="file_write_read",
        prompt="Create a file named /tmp/eval_test.txt containing the text "
               "'Hello from Encre eval', then read it back.",
        success_criteria="File /tmp/eval_test.txt exists with expected content",
        expected_output_patterns=["Hello from Encre eval"],
        required_tools=["file_write", "file_read"],
    ),
    EvalTask(
        name="web_search_basic",
        prompt="Search the web for 'Python 3.13 release date' and summarize.",
        success_criteria="Found recent information about Python 3.13",
        required_tools=["web_search"],
        timeout=30,
    ),
    EvalTask(
        name="code_edit_simple",
        prompt="Create a Python file /tmp/hello.py that prints 'hello world', "
               "then change it to print 'hello encre' instead.",
        success_criteria="File was created and edited successfully",
        expected_output_patterns=["hello encre"],
        required_tools=["file_write", "file_edit"],
    ),
    EvalTask(
        name="grep_search",
        prompt="Search for 'def main' in all Python files under current directory. "
               "Return the file paths and line numbers.",
        success_criteria="Found Python files containing 'def main'",
        required_tools=["grep"],
        timeout=30,
    ),
    EvalTask(
        name="tool_error_recovery",
        prompt="Try to delete a non-existent file /tmp/nonexistent_xyz_2024.txt, "
               "then handle the error gracefully and report what happened.",
        success_criteria="Error was handled without crashing",
        expected_output_patterns=["not found", "exist", "error"],
    ),
    # ── Evidence-based tasks (SWE-bench style) ─────────────────────────
    # These pass ONLY when the delivered artifact actually works: files must
    # exist with expected content, and verification commands must exit 0.
    EvalTask(
        name="ev_build_python_module",
        prompt="Create a Python module at /tmp/encre_eval/pkg/__init__.py and "
               "/tmp/encre_eval/pkg/mathx.py exposing a function add(a, b) that "
               "returns a + b.",
        success_criteria="Module with add() exists and imports",
        required_tools=["file_write"],
        file_assertions={
            "pkg/mathx.py": "def add(a, b)",
            "pkg/__init__.py": "",
        },
        verify_commands=[
            "python3 -c \"import sys; sys.path.insert(0, '.'); from pkg.mathx import add; assert add(2, 3) == 5\"",
        ],
        verify_cwd="/tmp/encre_eval",
    ),
    EvalTask(
        name="ev_write_and_run_script",
        prompt="Write /tmp/encre_eval/hello.py that prints 'HELLO_ENCRE_OK' to "
               "stdout, then run it and report the output.",
        success_criteria="Script prints the expected marker when run",
        required_tools=["file_write", "bash"],
        file_assertions={"hello.py": "HELLO_ENCRE_OK"},
        verify_commands=["python3 hello.py | grep -q HELLO_ENCRE_OK"],
        verify_cwd="/tmp/encre_eval",
    ),
    EvalTask(
        name="ev_fix_failing_test",
        prompt="In /tmp/encre_eval, there is a file fixme.py with a broken "
               "function that should return the square of its argument. Fix it "
               "so that running `python3 -c \"import fixme; print(fixme.square(7))\"` "
               "prints 49.",
        success_criteria="fixme.square(7) returns 49",
        required_tools=["file_read", "file_edit"],
        verify_commands=["python3 -c \"import fixme; assert fixme.square(7) == 49\" && echo PASS"],
        verify_cwd="/tmp/encre_eval",
    ),
]

# Tasks that assert real delivery via filesystem/command evidence.
EVIDENCE_TASKS = [t for t in BUILTIN_TASKS if (t.verify_commands or t.file_assertions)]

LIGHT_TASKS = [t for t in BUILTIN_TASKS if t.timeout <= 60]
# Quick tasks (<= 60s timeout) suitable for fast smoke testing.
