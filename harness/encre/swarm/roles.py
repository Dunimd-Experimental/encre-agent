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

# Predefined agent roles for the swarm.
#
# A role bundles a name, description, system-prompt override, allowed-tool
# list, permission mode, specialty, and priority.  ``RoleRegistry`` holds the
# built-in roles and can select one heuristically from a task description via
# keyword matching.  The orchestrator assigns these roles to planner-generated
# tasks so each task is handled by a suitably specialised agent.

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentRole:
    """Configuration describing one specialisation of swarm agent.

    Attributes:
        name: Stable role identifier (e.g. ``"coder"``).
        description: Human-readable purpose.
        system_prompt_override: Prepended system prompt for the role.
        allowed_tools: Tool names the role may invoke.
        permission_mode: Permission policy applied to the role's agent.
        specialty: Coarse capability tag (``general`` / ``coding`` / ...).
        priority: Higher numbers are scheduled first among ready tasks.
    """
    name: str
    description: str
    system_prompt_override: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    permission_mode: str = "auto"
    specialty: str = "general"
    priority: int = 5
    model_override: str = ""
    backend_type_override: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "system_prompt_override": self.system_prompt_override,
            "allowed_tools": self.allowed_tools,
            "permission_mode": self.permission_mode,
            "specialty": self.specialty,
            "priority": self.priority,
            "model_override": self.model_override,
            "backend_type_override": self.backend_type_override,
        }


# ── Predefined roles ─────────────────────────────────────────────

ROLE_ARCHITECT = AgentRole(
    name="architect",
    description="Designs system architecture, component structure, and data flow. Does NOT write implementation code.",
    system_prompt_override=(
        "You are a software ARCHITECT. Your job is to DESIGN -- not implement. "
        "Produce clear architecture documents, component diagrams (as text), data flow descriptions, "
        "API contracts, and technology choices with rationale. "
        "Read existing code to understand the current state, then propose structural changes. "
        "Do NOT write production code -- that is the coder's job."
    ),
    allowed_tools=["file_read", "grep", "glob", "web_search"],
    permission_mode="auto",
    specialty="general",
    priority=10,
)

ROLE_CODER = AgentRole(
    name="coder",
    description="Implements code according to the architect's design. Writes, edits, and tests code.",
    system_prompt_override=(
        "You are a SOFTWARE ENGINEER. Your job is to IMPLEMENT code according to the architecture design. "
        "Write clean, well-structured, production-quality code. Follow the architect's specifications. "
        "Use file_read/write/edit tools to modify code. Run tests after changes. "
        "If the architecture is unclear, ask for clarification -- do NOT guess."
    ),
    allowed_tools=["file_read", "file_write", "file_edit", "bash", "grep", "glob", "lsp"],
    permission_mode="accept_edits",
    specialty="coding",
    priority=5,
)

ROLE_REVIEWER = AgentRole(
    name="reviewer",
    description="Reviews code for correctness, style, security, and adherence to the architecture.",
    system_prompt_override=(
        "You are a CODE REVIEWER. Your job is to REVIEW code -- not write it. "
        "Check for: correctness, security vulnerabilities, performance issues, style violations, "
        "architectural compliance, test coverage, error handling. "
        "Use grep/glob to find relevant code. Use file_read to examine changes. "
        "Provide structured feedback: critical issues first, then suggestions."
    ),
    allowed_tools=["file_read", "grep", "glob", "lsp", "bash"],
    permission_mode="auto",
    specialty="coding",
    priority=7,
)

ROLE_TESTER = AgentRole(
    name="tester",
    description="Writes and runs tests. Verifies functionality, finds edge cases, measures coverage.",
    system_prompt_override=(
        "You are a QA TESTER. Your job is to TEST code thoroughly. "
        "Write unit tests, integration tests, and edge case tests. "
        "Run the test suite and report results. If tests fail, report exact failures. "
        "Also do manual verification: run the code, try different inputs, check outputs."
    ),
    allowed_tools=["file_read", "file_write", "file_edit", "bash", "grep", "glob"],
    permission_mode="accept_edits",
    specialty="coding",
    priority=5,
)

ROLE_RESEARCHER = AgentRole(
    name="researcher",
    description="Researches topics, gathers information from web and codebase, synthesizes findings.",
    system_prompt_override=(
        "You are a RESEARCHER. Your job is to GATHER and SYNTHESIZE information. "
        "Search the web, read documentation, explore the codebase. "
        "Produce well-structured research notes with citations. "
        "Do NOT implement anything -- just report findings."
    ),
    allowed_tools=["web_search", "web_fetch", "file_read", "grep", "glob"],
    permission_mode="dont_ask",
    specialty="research",
    priority=8,
)

ROLE_DEBUGGER = AgentRole(
    name="debugger",
    description="Diagnoses and fixes bugs. Traces execution, analyzes logs, identifies root causes.",
    system_prompt_override=(
        "You are a DEBUGGER. Your job is to FIND and FIX bugs. "
        "Reproduce the issue first. Use grep/glob to trace code paths. "
        "Read logs and error messages carefully. Identify the ROOT cause, not symptoms. "
        "Propose a minimal fix. If the fix is complex, hand off to the coder."
    ),
    allowed_tools=["file_read", "file_edit", "bash", "grep", "glob", "lsp"],
    permission_mode="accept_edits",
    specialty="coding",
    priority=7,
)

ROLE_GENERAL = AgentRole(
    name="general",
    description="General-purpose agent. Handles tasks that don't fit specialized roles.",
    system_prompt_override="",
    allowed_tools=[],
    permission_mode="default",
    specialty="general",
    priority=1,
)

# ── Registry ─────────────────────────────────────────────────────

class RoleRegistry:
    """In-memory registry of available agent roles.

    Seeds itself with the built-in roles on construction.  Provides lookup by
    name (falling back to ``ROLE_GENERAL``) and a keyword-based
    ``get_for_task`` selector used to map a free-text task description onto the
    most appropriate predefined role.
    """
    def __init__(self) -> None:
        self._roles: dict[str, AgentRole] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        for role in [ROLE_ARCHITECT, ROLE_CODER, ROLE_REVIEWER, ROLE_TESTER,
                      ROLE_RESEARCHER, ROLE_DEBUGGER, ROLE_GENERAL]:
            self._roles[role.name] = role

    def register(self, role: AgentRole) -> None:
        self._roles[role.name] = role

    def get(self, name: str) -> AgentRole:
        return self._roles.get(name, ROLE_GENERAL)

    def list_roles(self) -> list[str]:
        return list(self._roles.keys())

    def get_for_task(self, task_description: str) -> AgentRole:
        desc = task_description.lower()
        if any(kw in desc for kw in ("design", "architecture", "structure", "plan")):
            return self._roles.get("architect", ROLE_GENERAL)
        if any(kw in desc for kw in ("implement", "code", "write", "build", "develop")):
            return self._roles.get("coder", ROLE_GENERAL)
        if any(kw in desc for kw in ("review", "check", "audit", "inspect")):
            return self._roles.get("reviewer", ROLE_GENERAL)
        if any(kw in desc for kw in ("test", "verify", "validate", "qa")):
            return self._roles.get("tester", ROLE_GENERAL)
        if any(kw in desc for kw in ("research", "find", "search", "investigate")):
            return self._roles.get("researcher", ROLE_GENERAL)
        if any(kw in desc for kw in ("debug", "fix bug", "diagnose", "troubleshoot")):
            return self._roles.get("debugger", ROLE_GENERAL)
        return ROLE_GENERAL
