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

"""Module: ea_tool_archive.plugin

Plugin manifest and factory for the ``ea-tool-archive`` system-default plugin.

The harness discovers plugins by finding a ``create_plugin`` callable inside
each packaged plugin directory and calling it to obtain an opaque
:class:`EncrePlugin` handle. This module registers the ``archive`` tool and
advertises it as a bundled, system-default plugin.
"""

from __future__ import annotations

from pathlib import Path

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_archive.tool import archive_tool

# Absolute path of this plugin's package directory. The harness resolves
# icon and asset references relative to this location, so we compute it
# once at import time rather than on every manifest lookup.
_PLUGIN_DIR = Path(__file__).resolve().parent


def create_plugin() -> EncrePlugin:
    """Build and return a fresh :class:`EncrePlugin` instance.

    We construct a new :class:`_ArchivePlugin` on each call (rather than
    caching a module-level singleton) so concurrent test runs and any
    harness that reloads the plugin package never share mutable state.
    The ``_PLUGIN_DIR`` constant above is module-scoped and read-only, so
    it is safe to reference from every instance.
    """
    return _ArchivePlugin()


class _ArchivePlugin(EncrePlugin):
    """Concrete plugin class for the ``archive`` compression tool.

    The :attr:`manifest` class attribute is a static :class:`PluginManifest`
    describing identity, packaging metadata, and the tools/skills this plugin
    contributes. The harness reads this manifest when building the plugin
    registry and does not re-instantiate it. Behaviour lives in
    :meth:`get_tools`, which the runtime consults when a session asks for
    available tools.
    """

    manifest = PluginManifest(
        # Stable internal identifier; appears in marketplace indexes,
        # config snippets, and enable/disable toggles.
        name="ea-tool-archive",
        # Human-friendly label shown in UI plugin pickers.
        display_name="Archive",
        icon="archive",
        version="0.5.0",
        description="EA (Encre Agent) system-default archive tool.",
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
        tags=["system-default", "data"],
        provides_tools=["archive"],
        provides_skills=["tool-archive"],
        activation_events=[],
        # Archive I/O touches the local filesystem so we request the
        # filesystem:write capability for extract/create paths.
        permissions=['filesystem:write'],
    )

    def get_tools(self) -> list:
        """Return the list of tool objects this plugin contributes.

        The harness merges tools from every loaded plugin into a single
        registry keyed by name. We deliberately return a fresh list each
        call (rather than a cached tuple) so callers cannot mutate a
        shared reference and accidentally hide the tool from future lookups.
        """
        return [archive_tool]
