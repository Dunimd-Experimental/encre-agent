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

"""Plugin manifest and factory for the ``ea-tool-notify`` tool.

The Encre Agent plugin system discovers this file, imports it, and calls
``create_plugin()`` to obtain a fresh ``EncrePlugin`` instance. The manifest
attached to the returned class is what the desktop market and the CLI render
to users; the ``get_tools()`` method is what the agent's tool registry iterates
over to expose callable tools to the model.

Keeping the manifest declared at class level (rather than inside
``create_plugin``) means the metadata lives in one obvious place that static
tooling such as ``encre-plugin verify`` can introspect without instantiating
the plugin. The factory itself is deliberately tiny: it just news up a
``_NotifyPlugin`` so callers never touch the private class directly.
"""

from __future__ import annotations

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_notify.tool import notify_tool


def create_plugin() -> EncrePlugin:
    """Return a fresh ``EncrePlugin`` instance exposing the notify tool.

    The factory pattern (rather than a module-level singleton) matches the
    convention used by the plugin loader: each session instantiates its own
    plugin so that per-session state, if any is added later, does not leak
    between sessions.

    Returns:
        A new ``_NotifyPlugin`` instance. It carries the shared
        ``PluginManifest`` as a class attribute and re-uses the module-level
        ``notify_tool`` singletons rather than rebuilding them per session.
    """
    return _NotifyPlugin()


class _NotifyPlugin(EncrePlugin):
    """Internal plugin container for the notify tool.

    The class is intentionally prefixed with an underscore because callers
    should go through ``create_plugin()``; the underscore signals that the
    class identity is an implementation detail that can move between files
    or be split later without breaking import paths.

    Manifest invariants:
        * ``source=BUNDLED`` -- this tool ships inside the Encre Agent
          distribution rather than being user-installed from the market, so
          the desktop never offers to "uninstall" it.
        * ``tier="system-default"`` -- surfaced in the default UI tier so
          the tool is visible without the user flipping any switches.
        * ``engines={"encre": ">=0.5"}`` -- the manifest advertises the
          minimum Encre Agent version that understands this plugin's API
          surface, so older clients can refuse to load it rather than
          half-installing a broken integration.
    """

    manifest = PluginManifest(
        name="ea-tool-notify",
        display_name="Notify",
        icon="bell",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: notify.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "comms"],
        provides_tools=['notify'],
        provides_skills=["tool-notify"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the list of tools this plugin contributes to the agent.

        The tool registry concatenates the results from every plugin's
        ``get_tools()`` and de-duplicates by ``tool.name``. Returning a
        fresh list each call (rather than a module-level constant) keeps
        the contract honest if a caller mutates the returned list for any
        reason -- the plugin's state is not affected.

        Returns:
            A one-element list containing the ``notify_tool`` instance.
        """
        return [notify_tool]
