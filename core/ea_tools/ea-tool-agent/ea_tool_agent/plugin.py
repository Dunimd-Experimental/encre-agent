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

"""Module: ea_tool_agent.plugin

Plugin manifest and factory for the ``ea-tool-agent`` system-default plugin.

The harness locates plugins by discovering a ``create_plugin`` callable inside
each packaged plugin directory. This module registers the ``agent`` tool --
the sub-agent delegation primitive -- and advertises it as a system-default,
bundled plugin so it is always available in every Encre Agent session.
"""

from __future__ import annotations

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_agent.tool import agent_tool


def create_plugin() -> EncrePlugin:
    """Build and return a fresh :class:`EncrePlugin` instance.

    The harness calls this factory once per loaded plugin and treats the
    returned object as an opaque handle for the plugin's manifest, tools,
    and skills. We construct an :class:`_AgentPlugin` on each call rather
    than caching a module-level singleton so concurrent test runs (and any
    harness that reloads the plugin package) never share mutable state.
    """
    return _AgentPlugin()


class _AgentPlugin(EncrePlugin):
    """Concrete plugin class for the ``agent`` sub-agent delegation tool.

    The :attr:`manifest` class attribute is a static :class:`PluginManifest`
    describing identity, packaging metadata, and the tools/skills this plugin
    contributes. The harness reads this manifest when building the plugin
    registry and does not re-instantiate it. All mutating behaviour lives in
    the overridden :meth:`get_tools`, which is what the runtime consults when
    a session asks for available tools.
    """

    manifest = PluginManifest(
        # Stable internal identifier. The hyphenated form is what appears in
        # marketplace indexes, config snippets, and enable/disable toggles.
        name="ea-tool-agent",
        # Human-friendly label shown in UI plugin pickers.
        display_name="Agent",
        icon="assets/icon.svg",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: agent.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        # BUNDLED marks this plugin as shipped inside the runtime distribution
        # rather than installed by the user, so the market UI hides it from
        # install/uninstall flows while still listing it in the active set.
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "ai"],
        provides_tools=['agent'],
        provides_skills=["tool-agent"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the list of tool objects this plugin contributes.

        The harness merges tools from every loaded plugin into a single
        registry keyed by name. We deliberately return a fresh list each
        call (rather than a cached tuple) so callers cannot mutate a
        shared reference and accidentally hide the tool from future lookups.
        """
        return [agent_tool]

