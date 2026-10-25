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

"""Process capability seam.

Single implementation for every subprocess power in Encre:
- spawning with window suppression (:mod:`encre.capabilities.process.spawn`)
- background shells (:class:`BackgroundShellManager`)
- persistent terminals (:class:`TerminalSessionManager`)
- path sandboxing + Rust-owned process confinement
  (:mod:`encre.capabilities.process.sandbox`)

OS-level confinement for model-driven shells lives in the Rust core
(``SandboxSession``); :func:`spawn_confined` is the only sanctioned entry.
The Rust ``permission_check`` call path is unaffected: permission
decisions happen in the tools layer before reaching this seam.
"""

from encre.capabilities.process.sandbox import (
    ConfinedProcess,
    PathViolation,
    check_path_safety,
    filter_allowed_env,
    get_sandbox_root,
    get_session_files_dir,
    remap_path,
    remap_tool_path,
    spawn_confined,
)
from encre.capabilities.process.shell_manager import BackgroundShellManager
from encre.capabilities.process.spawn import (
    create_subprocess_exec,
    create_subprocess_run,
    hidden_subprocess_kwargs,
    kill_process_tree,
)
from encre.capabilities.process.terminal_manager import (
    SHELL_LAUNCH,
    TerminalSessionManager,
    marker_cmd,
)

__all__ = [
    "SHELL_LAUNCH",
    "BackgroundShellManager",
    "ConfinedProcess",
    "PathViolation",
    "TerminalSessionManager",
    "check_path_safety",
    "create_subprocess_exec",
    "create_subprocess_run",
    "filter_allowed_env",
    "get_sandbox_root",
    "get_session_files_dir",
    "hidden_subprocess_kwargs",
    "kill_process_tree",
    "marker_cmd",
    "remap_path",
    "remap_tool_path",
    "spawn_confined",
]
