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

"""Prompt template base classes.

Provides :class:`EncreBasePrompt` (an abstract interface for prompt
templates) and :class:`EncrePromptTemplate` (a general-purpose concrete
implementation backed by the layered
:class:`~encre.prompts.system.EncrePromptBuilder`).

Subclasses such as ``EncreCodingPrompt`` specialize behaviour by passing a
fixed ``specialty`` to the builder.
"""

from abc import ABC, abstractmethod
from typing import Any

from encre.prompts.loader import PromptLoader
from encre.prompts.system import EncrePromptBuilder
from encre.utils.types import PermissionMode

_loader = PromptLoader()


class EncreBasePrompt(ABC):
    """Abstract base for prompt templates."""

    @abstractmethod
    def build_system_prompt(
        self,
        mode: PermissionMode,
        tools: list[dict[str, Any]] | None = None,
        custom_instructions: str = "",
        intents: list[str] | None = None,
        workspace_root: str = "",
        workspace_name: str = "",
        project_summary: str = "",
        language_preference: str = "auto",
        app_language: str = "zh",
        session_id: str = "",
        slash_command_mode: str = "",
        slash_commands: list[dict[str, Any]] | None = None,
        skill_summary: str = "",
        active_command: dict[str, Any] | None = None,
        model: str = "",
        model_profile: Any = None,
        include_specialty: bool = True,
    ) -> str: ...

    @abstractmethod
    def build_tool_instructions(self, tool_names: list[str]) -> str: ...

    @abstractmethod
    def build_with_restrictions(
        self,
        mode: PermissionMode,
        tools: list[dict[str, Any]] | None = None,
        custom_instructions: str = "",
        intents: list[str] | None = None,
        workspace_root: str = "",
        workspace_name: str = "",
        project_summary: str = "",
        language_preference: str = "auto",
        app_language: str = "zh",
        session_id: str = "",
        slash_command_mode: str = "",
        slash_commands: list[dict[str, Any]] | None = None,
        skill_summary: str = "",
        active_command: dict[str, Any] | None = None,
        model: str = "",
        model_profile: Any = None,
        include_specialty: bool = True,
    ) -> tuple[str, dict[str, Any]]: ...


class EncrePromptTemplate(EncreBasePrompt):
    """General-purpose prompt template using the layered builder."""

    def __init__(self, builder: EncrePromptBuilder | None = None, specialty: str = "general") -> None:
        """Initialize the template.

        Args:
            builder: Optional pre-configured prompt builder.  A new
                :class:`~encre.prompts.system.EncrePromptBuilder` is created
                when omitted.
            specialty: Default specialty label forwarded to the builder.
        """
        self._builder = builder or EncrePromptBuilder()
        self._specialty = specialty

    def build_system_prompt(
        self,
        mode: PermissionMode,
        tools: list[dict[str, Any]] | None = None,
        custom_instructions: str = "",
        intents: list[str] | None = None,
        workspace_root: str = "",
        workspace_name: str = "",
        project_summary: str = "",
        language_preference: str = "auto",
        app_language: str = "zh",
        session_id: str = "",
        slash_command_mode: str = "",
        slash_commands: list[dict[str, Any]] | None = None,
        skill_summary: str = "",
        active_command: dict[str, Any] | None = None,
        model: str = "",
        model_profile: Any = None,
        include_specialty: bool = True,
    ) -> str:
        """Build the system prompt from session context.

        Delegates to
        :meth:`~encre.prompts.system.EncrePromptBuilder.build` using the
        template's configured ``specialty``.
        """
        return self._builder.build(
            mode=mode,
            tools=tools,
            specialty=self._specialty,
            custom_instructions=custom_instructions,
            intents=intents,
            workspace_root=workspace_root,
            workspace_name=workspace_name,
            project_summary=project_summary,
            language_preference=language_preference,
            app_language=app_language,
            session_id=session_id,
            slash_command_mode=slash_command_mode,
            slash_commands=slash_commands,
            skill_summary=skill_summary,
            active_command=active_command,
            model=model,
            model_profile=model_profile,
            include_specialty=include_specialty,
        )

    def build_tool_instructions(self, tool_names: list[str]) -> str:
        """Render a human-readable list of available tools.

        Args:
            tool_names: Names of tools exposed to the model.

        Returns:
            A prompt fragment describing the tools, or a notice when no tools
            are available.
        """
        if not tool_names:
            return _loader.load("no_tools")
        tools_list = "\n".join(f"- {name}" for name in tool_names)
        return _loader.load_with_context("tool_instructions", tools_list=tools_list)

    @property
    def builder(self) -> EncrePromptBuilder:
        """The underlying prompt builder instance."""
        return self._builder

    @property
    def specialty(self) -> str:
        """The specialty label this template forces into the builder.

        Public because the loop supplies the domain-specialty section as a
        per-turn context block (``include_specialty=False``) and must select the
        same section the template would have.
        """
        return self._specialty

    def build_with_restrictions(
        self,
        mode: PermissionMode,
        tools: list[dict[str, Any]] | None = None,
        custom_instructions: str = "",
        intents: list[str] | None = None,
        workspace_root: str = "",
        workspace_name: str = "",
        project_summary: str = "",
        language_preference: str = "auto",
        app_language: str = "zh",
        session_id: str = "",
        slash_command_mode: str = "",
        slash_commands: list[dict[str, Any]] | None = None,
        skill_summary: str = "",
        active_command: dict[str, Any] | None = None,
        model: str = "",
        model_profile: Any = None,
        include_specialty: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        """Build the system prompt with active restrictions metadata.

        Returns ``(prompt, restrictions)`` where ``restrictions`` is a dict
        describing which constraints the caller should enforce at the code
        level (e.g., restricted tools in plan/spec mode, safety level).
        """
        return self._builder.build_with_restrictions(
            mode=mode,
            tools=tools,
            specialty=self._specialty,
            custom_instructions=custom_instructions,
            intents=intents,
            workspace_root=workspace_root,
            workspace_name=workspace_name,
            project_summary=project_summary,
            language_preference=language_preference,
            app_language=app_language,
            session_id=session_id,
            slash_command_mode=slash_command_mode,
            slash_commands=slash_commands,
            skill_summary=skill_summary,
            active_command=active_command,
            model=model,
            model_profile=model_profile,
            include_specialty=include_specialty,
        )
