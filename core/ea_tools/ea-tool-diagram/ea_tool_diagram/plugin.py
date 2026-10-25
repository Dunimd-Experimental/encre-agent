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

"""Plugin registration for the ea-tool-diagram tool.

This module defines the ``_DiagramPlugin`` class, which extends
``EncrePlugin`` to declare its manifest metadata and expose the
``diagram`` tool to the Encre Agent runtime.

The ``create_plugin`` factory is the entry point called by the plugin
loader. It returns a fully configured plugin instance without requiring
the caller to know about the internal ``_DiagramPlugin`` class.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_diagram.tool import diagram_tool


def create_plugin() -> EncrePlugin:
    """Instantiate and return the diagram plugin.

    The plugin loader calls this factory to obtain a configured
    ``EncrePlugin`` instance. Wrapping the class in a factory isolates
    the constructor from import-time side effects and gives the loader
    a single, stable entry point regardless of how the internal class
    is structured.

    Returns:
        A configured ``_DiagramPlugin`` instance ready for
        registration with the Encre Agent plugin system.
    """
    return _DiagramPlugin()


class _DiagramPlugin(EncrePlugin):
    """Encre plugin that provides the ``diagram`` tool.

    This class is the implementation counterpart to ``create_plugin``.
    It inherits from ``EncrePlugin`` and supplies:

    * A ``manifest`` (class-level ``PluginManifest``) that describes the
      plugin's identity, capabilities, dependency graph, and safety
      classification.
    * A ``get_tools`` method that returns the list of tool objects this
      plugin contributes to the agent runtime.

    The leading underscore signals that this class is an internal
    implementation detail; consumers should use ``create_plugin``
    instead of constructing ``_DiagramPlugin`` directly.
    """

    manifest = PluginManifest(
        name="ea-tool-diagram",
        display_name="Diagram",
        icon="git-fork",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: diagram.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "dev"],
        provides_tools=['diagram'],
        provides_skills=["tool-diagram"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the tool objects provided by this plugin.

        The Encre Agent plugin system calls this method during plugin
        initialization to register the tools with the agent's tool
        registry. The returned ``diagram_tool`` is the fully
        configured tool definition (name, schema, executor) created
        in ``tool.py``.

        Returns:
            A list containing the single ``diagram_tool`` tool
            instance.
        """
        return [diagram_tool]
