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

"""Built-in sub-agent role templates for Encre's multi-agent workflows.

Each entry is an :class:`encre.config.SubAgentConfig` that pairs a name and a
short description with a system prompt loaded from the "skills" category of
the prompt library.  The system prompts live as standalone Markdown files in
the harness's prompt tree; :class:`PromptLoader` resolves them by name so the
role definitions here stay declarative.

Most roles are ``hidden`` (not directly invocable by the user) and exist so
the planner / orchestrator / advisor logic can spawn a focused agent on
demand.  A handful expose a ``tool_policy`` (``readonly`` / ``no_writes`` /
``all``) that restricts the sub-agent's tool set at the executor layer —
this is what keeps the Explore role from ever being able to write a file
even if the LLM tries.

Every built-in role also ships ``permission_mode="bypass"`` (full access,
no approval prompts) and leaves ``model_index`` unset, so at spawn time the
sub-agent inherits the model the main agent is currently running.  Note the
two layers are independent: ``permission_mode`` governs the approval engine
while ``tool_policy`` governs which tools are even reachable — bypassing
approvals does not widen Explore's read-only tool set.

The roster is grouped by mode: general-mode workers (coder, researcher,
critic), workspace-mode designers (architect, planner), the plan/spec
writer, the auto-agent (lobster) family (automation / monitor / executor /
scheduler), and the Claude Code-style role templates (Explore / Plan /
general-purpose).  Adding a new role means appending a new
:class:`SubAgentConfig` here and dropping the matching prompt file into the
skills category.
"""

from encre.config import SubAgentConfig
from encre.prompts.loader import PromptLoader

_loader = PromptLoader()


def get_builtin_sub_agents() -> list[SubAgentConfig]:
    """Return the list of predefined sub-agent templates.

    The roster is grouped (by comment) into general-mode workers, workspace
    architects/planners, plan/spec writers, the auto-agent (lobster) family
    (automation / monitor / executor / scheduler), and Claude Code-style role
    templates (Explore / Plan / general-purpose).  The returned configs are
    deep enough to be handed straight to ``EncreAgent`` construction: each
    one is a fresh :class:`SubAgentConfig` with a fully-rendered
    ``system_prompt``, so callers do not need to call :meth:`PromptLoader.load`
    themselves.

    The prompts are loaded eagerly at call time rather than at import time so
    that the harness can be imported in environments where the prompt tree is
    not yet on the filesystem (e.g. a PyInstaller frozen bundle before its
    ``_internal`` staging is in place).  Each call re-reads the files, which
    keeps the overhead low (the loader caches by filename).
    """
    return [
        # Every role below: permission_mode="bypass" (no approval prompts) and
        # model_index unset (inherits the main agent's active model at spawn).
        SubAgentConfig(
            name="coder",
            description="Focused implementation agent for writing code",
            system_prompt=_loader.load("coder", category="skills"),
            hidden=True,
            permission_mode="bypass",
        ),
        SubAgentConfig(
            name="researcher",
            description="Thorough research agent for gathering and analyzing information",
            system_prompt=_loader.load("researcher", category="skills"),
            hidden=True,
            permission_mode="bypass",
        ),
        SubAgentConfig(
            name="critic",
            description="Quality auditor for reviewing code and other work",
            system_prompt=_loader.load("critic", category="skills"),
            hidden=True,
            permission_mode="bypass",
        ),
        # ── Workspace mode ────────────────────────────────────
        SubAgentConfig(
            name="architect",
            description="System designer for planning architecture before implementation",
            system_prompt=_loader.load("architect", category="skills"),
            hidden=True,
            permission_mode="bypass",
        ),
        SubAgentConfig(
            name="planner",
            description="Task breakdown specialist for decomposing goals into actionable steps",
            system_prompt=_loader.load("planner", category="skills"),
            hidden=True,
            permission_mode="bypass",
        ),
        # ── Plan/Spec mode ────────────────────────────────────
        SubAgentConfig(
            name="spec-writer",
            description="Requirements and specification specialist for writing precise specs",
            system_prompt=_loader.load("spec_writer", category="skills"),
            hidden=True,
            permission_mode="bypass",
        ),
        # ── Auto agent (lobster) mode ─────────────────────────
        SubAgentConfig(
            name="automation",
            description="Autonomous workflow orchestrator for monitoring, execution, and scheduling",
            system_prompt=_loader.load("automation", category="skills"),
            hidden=True,
            permission_mode="bypass",
        ),
        SubAgentConfig(
            name="monitor",
            description="Event and state watcher for detecting changes and triggers",
            system_prompt=_loader.load("monitor", category="skills"),
            hidden=True,
            permission_mode="bypass",
        ),
        SubAgentConfig(
            name="executor",
            description="Focused task completion agent for executing well-defined tasks",
            system_prompt=_loader.load("executor", category="skills"),
            hidden=True,
            permission_mode="bypass",
        ),
        SubAgentConfig(
            name="scheduler",
            description="Task and time orchestrator for managing schedules and retries",
            system_prompt=_loader.load("scheduler", category="skills"),
            hidden=True,
            permission_mode="bypass",
        ),
        # ── Claude Code-style role templates ─────────────────
        # The three Claude Code-style roles are deliberately exposed with
        # restrictive ``tool_policy`` values: the executor enforces the
        # policy so an LLM that tries to write through Explore is blocked
        # before the tool is even invoked.  The bypass permission mode only
        # skips approval prompts — it does not widen the tool set.
        SubAgentConfig(
            name="Explore",
            description=(
                "Read-only investigator. Gathers information about a codebase "
                "without modifying anything. Can search, read, and use read-only "
                "shell. Cannot write, install, or run mutating commands. Intended "
                "for parallel read-only research only."
            ),
            system_prompt=_loader.load("explore", category="skills"),
            hidden=True,
            permission_mode="bypass",
            tool_policy="readonly",
        ),
        SubAgentConfig(
            name="Plan",
            description=(
                "Read + analyze sub-agent. Produces a detailed execution plan. "
                "Can read, search, ask the user questions, and use read-only shell. "
                "Cannot write or run mutating commands. Intended for parallel "
                "planning research only."
            ),
            system_prompt=_loader.load("plan_agent", category="skills"),
            hidden=True,
            permission_mode="bypass",
            tool_policy="no_writes",
        ),
        SubAgentConfig(
            name="general-purpose",
            description=(
                "Fully capable sub-agent with the same tool set as the main agent. "
                "Intended for parallel fan-out of independent workstreams only; "
                "not a substitute for handling tasks directly."
            ),
            system_prompt=_loader.load("general_purpose", category="skills"),
            hidden=True,
            permission_mode="bypass",
            tool_policy="all",
        ),
    ]
