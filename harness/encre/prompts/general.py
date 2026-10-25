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

"""General-purpose prompt template.

Defines :class:`EncreGeneralPrompt`, a
:class:`~encre.prompts.base.EncrePromptTemplate` specialized for generic,
non-domain conversations.  It delegates assembly to the shared
:class:`~encre.prompts.system.EncrePromptBuilder` with the ``"general"``
specialty.
"""

from typing import Any

from encre.prompts.base import EncrePromptTemplate
from encre.prompts.system import EncrePromptBuilder
from encre.utils.types import PermissionMode


class EncreGeneralPrompt(EncrePromptTemplate):
    """General-purpose prompt template.

    Builds the system prompt for non-specialized conversations by delegating
    to the layered :class:`~encre.prompts.system.EncrePromptBuilder`.
    """

    def __init__(self, builder: EncrePromptBuilder | None = None, specialty: str = "general") -> None:
        """Initialize the template.

        Args:
            builder: Optional pre-configured prompt builder.  A new one is
                created when omitted.
            specialty: Specialty label forwarded to the builder (``"general"``).
        """
        super().__init__(builder=builder, specialty=specialty)

    def build_system_prompt(
        self,
        mode: PermissionMode = "default",
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
        """Build the general system prompt from session context.

        Forwards every argument to
        :meth:`~encre.prompts.system.EncrePromptBuilder.build` with the
        ``"general"`` specialty so the shared block-assembly logic is reused.
        """
        return self._builder.build(
            mode=mode,
            tools=tools,
            specialty="general",
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
