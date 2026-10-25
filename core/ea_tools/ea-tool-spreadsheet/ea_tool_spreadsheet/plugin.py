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

"""Plugin manifest and factory for the ``ea-tool-spreadsheet`` tool.

The plugin loader imports this module and calls ``create_plugin()``
once per session. The manifest is the source of truth for what the
desktop market and ``encre-plugin verify`` render to users; the
``get_tools()`` method is what the tool registry iterates to expose
the ``spreadsheet`` tool to the model.

``danger_levels`` is set to ``{"spreadsheet": "high"}`` because this
tool can overwrite an existing spreadsheet in place. Reads are safe;
writes truncate and replace the file, which is why the tool's
``is_destructive`` flag only fires on the ``write`` action rather
than on every invocation. The two flags work together: the
``danger_levels`` value tells the desktop market to render a
strong warning on the tool card, and the ``is_destructive`` lambda
tells the tool registry which specific invocations should prompt
for confirmation.
"""

from __future__ import annotations

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_spreadsheet.tool import spreadsheet_tool


def create_plugin() -> EncrePlugin:
    """Return a fresh ``EncrePlugin`` instance exposing the spreadsheet tool.

    Uses the factory pattern so each session has its own plugin object.
    The manifest is a shared class attribute; only the returned
    ``_SpreadsheetPlugin`` instance is fresh per call.

    Returns:
        A new ``_SpreadsheetPlugin`` instance.
    """
    return _SpreadsheetPlugin()


class _SpreadsheetPlugin(EncrePlugin):
    """Internal plugin container for the spreadsheet tool.

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
        * ``danger_levels={"spreadsheet": "high"}`` -- declared at
          the plugin level so the desktop market can render a
          warning badge on the tool card. See the module docstring
          above for the interaction between this field and the
          tool-level ``is_destructive`` lambda.
    """

    manifest = PluginManifest(
        name="ea-tool-spreadsheet",
        display_name="Spreadsheet",
        icon="table",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: spreadsheet.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "office"],
        provides_tools=['spreadsheet'],
            danger_levels={"spreadsheet": "high"},
        provides_skills=["tool-spreadsheet"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the list of tools this plugin contributes to the agent.

        Returning a fresh list each call keeps the contract honest if
        any caller mutates the returned list -- the plugin's state is
        unaffected.

        Returns:
            A one-element list containing the ``spreadsheet_tool``
            instance.
        """
        return [spreadsheet_tool]
