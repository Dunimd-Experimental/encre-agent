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

"""Plugin manifest and factory for the ``ea-tool-question`` tool.

The plugin loader imports this module and calls ``create_plugin()``
once per session. The manifest is the source of truth for what the
desktop market and ``encre-plugin verify`` render to users; the
``get_tools()`` method is what the tool registry iterates to expose
the ``question`` tool to the model.

The question tool is marked ``always_available=True`` on the tool
object (see ``tool.py``), which means the frontend surfaces it even
when other tools are disabled. The plugin-level manifest still keeps
the standard ``BUNDLED`` / ``system-default`` shape so the tool
discovers through the same path as every other Encre Agent
system-default tool.
"""

from __future__ import annotations

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_question.tool import question_tool


def create_plugin() -> EncrePlugin:
    """Return a fresh ``EncrePlugin`` instance exposing the question tool.

    Uses the factory pattern so each session has its own plugin object.
    The manifest is a shared class attribute; only the returned
    ``_QuestionPlugin`` instance is fresh per call.

    Returns:
        A new ``_QuestionPlugin`` instance.
    """
    return _QuestionPlugin()


class _QuestionPlugin(EncrePlugin):
    """Internal plugin container for the question tool.

    The underscore prefix signals that callers should go through
    ``create_plugin()``; the class identity is intentionally an
    implementation detail that can be refactored without changing
    import paths.

    Manifest invariants:
        * ``source=BUNDLED`` -- ships with the Encre Agent distribution
          rather than being user-installed from the market, so the
          desktop does not offer to uninstall it.
        * ``tier="system-default"`` -- visible in the default UI tier
          without extra configuration.
        * ``engines={"encre": ">=0.5"}`` -- advertises the minimum
          Encre Agent version that understands this plugin's API
          surface, so older clients can refuse to load rather than
          half-installing a broken integration.
    """

    manifest = PluginManifest(
        name="ea-tool-question",
        display_name="Question",
        icon="circle-help",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: question.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "dev"],
        provides_tools=['question'],
        provides_skills=["tool-question"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the list of tools this plugin contributes to the agent.

        Returning a fresh list each call keeps the contract honest if
        any caller mutates the returned list -- the plugin's state is
        unaffected.

        Returns:
            A one-element list containing the ``question_tool``
            instance.
        """
        return [question_tool]
