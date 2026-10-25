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

"""Plugin manifest and factory for the platform-API tool bundle.

The bundle groups three OpenAI-compatible APIs (Files, Batch, Fine-tuning)
into a single plugin because they form one workflow: ``file_api upload``
produces a file id, that id feeds either a ``batch_api`` or
``fine_tune_api`` call. Splitting them into three plugins would force
the desktop market to explain how the three fit together, and the agent
would have to discover the connection across plugin boundaries.

The manifest advertises all three tools at the plugin level so the
desktop market and the CLI can render a single entry instead of three.
Each tool is also exposed via its own ``tool-*`` skill name so agents
can bind to them independently.
"""

from __future__ import annotations

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_platform_api.tool import batch_api_tool, file_api_tool, fine_tune_api_tool


def create_plugin() -> EncrePlugin:
    """Return a fresh ``EncrePlugin`` instance exposing the platform-API tools.

    Uses the factory pattern so each session gets its own plugin object.
    The manifest and tool instances are shared class attributes; only
    the returned ``_PlatformApiPlugin`` instance is fresh per call.

    Returns:
        A new ``_PlatformApiPlugin`` instance.
    """
    return _PlatformApiPlugin()


class _PlatformApiPlugin(EncrePlugin):
    """Internal plugin container for the platform-API tools.

    The underscore prefix signals that callers should use
    ``create_plugin()``; the class identity is intentionally an
    implementation detail that can be refactored without changing the
    import contract.

    Manifest invariants:
        * ``source=BUNDLED`` -- ships with the Encre Agent distribution
          rather than being user-installed from the market, so the
          desktop does not offer to uninstall it.
        * ``tier="system-default"`` -- visible in the default UI tier
          without extra configuration.
        * ``engines={"encre": ">=0.5"}`` -- the manifest advertises the
          minimum Encre Agent version that understands this plugin's
          API surface, so older clients can refuse to load rather than
          half-installing a broken integration.
        * ``provides_tools`` lists three entries (``batch_api``,
          ``file_api``, ``fine_tuning_api``) but
          ``provides_skills`` uses the snake_case naming scheme of the
          Encre Agent skill registry; note that the manifest entry is
          ``fine_tuning_api`` while the Python symbol is
          ``fine_tune_api_tool`` -- the two names intentionally diverge
          because the manifest name is the user-facing identifier.
    """

    manifest = PluginManifest(
        name="ea-tool-platform-api",
        display_name="Platform API",
        icon="server-cog",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: batch_api, file_api, fine_tuning_api.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "dev"],
        provides_tools=['batch_api', 'file_api', 'fine_tuning_api'],
        provides_skills=["tool-batch_api", "tool-file_api", "tool-fine_tuning_api"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the three platform-API tools contributed by this plugin.

        The tool registry concatenates results from every plugin's
        ``get_tools()`` and de-duplicates by ``tool.name``. Returning
        a fresh list each call keeps the contract honest if any caller
        mutates the returned list -- the plugin's state is unaffected.

        Returns:
            A three-element list containing the file, batch, and
            fine-tuning tool instances.
        """
        return [batch_api_tool, file_api_tool, fine_tune_api_tool]
