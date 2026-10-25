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

"""Plugin factory and manifest for ``ea-tool-todo``.

Each Encre Agent tool plugin exposes a :func:`create_plugin` factory
that returns an :class:`EncrePlugin` instance. The loader calls the
factory once per process, reads the returned instance's
:class:`PluginManifest` for metadata (name, version, tags, engine
range), and calls :meth:`_TodoPlugin.get_tools` to obtain the actual
tool objects it should register.

Keeping the manifest in a separate module from the tool object keeps
the ``tool.py`` import graph free of plugin-loader types. This matters
because tests and direct programmatic callers often import the tool
without wanting to pull in :mod:`encre.plugins.types` at all.

The plugin advertises both a tool (``todo``) and a skill tag
(``tool-todo``); the tool is the callable object the agent
dispatches on, while the skill tag is a naming handle used by the
intent router and the market UI.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_todo.tool import todo_tool


def create_plugin() -> EncrePlugin:
    """Construct and return the ``ea-tool-todo`` plugin instance.

    The factory returns a fresh :class:`_TodoPlugin` on every call
    rather than caching a singleton. The plugin instance carries no
    mutable state beyond the immutable :attr:`manifest` attribute, so
    constructing it repeatedly is cheap and the factory is safe to
    invoke from the market UI as it enumerates installed plugins.

    Returns:
        An :class:`EncrePlugin` instance whose ``manifest`` describes
        the bundled ``todo`` tool and whose ``get_tools`` returns the
        :data:`todo_tool` object exactly once.
    """
    return _TodoPlugin()


class _TodoPlugin(EncrePlugin):
    """Encre Agent plugin object for the bundled ``todo`` tool.

    The class body carries only a :class:`PluginManifest`. The manifest
    is a plain data object evaluated at import time so the loader can
    read it synchronously without instantiating anything — this keeps
    the import path of this module cheap and side-effect-free.

    Fields worth calling out:

    * ``tier="system-default"`` and ``tags=["system-default"]`` mark
      this plugin as part of the Encre Agent core distribution, not a
      marketplace entry.
    * ``engines={"encre": ">=0.5"}`` is the minimum harness version the
      plugin loader must have before installing this package.
    * No ``danger_levels`` entry is declared here because the ``todo``
      tool only writes to the current session's in-memory plan, which
      is scoped and per-session. It is neither destructive at the
      filesystem level nor does it need permission-aware caller
      prompts.
    * No ``activation_events`` or ``permissions`` are declared because
      the tool never hooks into session lifecycle events; the session
      is discovered per call via the active-loop context.
    """

    manifest = PluginManifest(
        name="ea-tool-todo",
        display_name="Todo",
        icon="list-todo",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: todo.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "office"],
        provides_tools=['todo'],
        provides_skills=["tool-todo"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the tool objects this plugin exposes.

        The Encre Agent tool-dispatch layer calls this once at startup
        and registers each returned object in its global registry.
        Returning the module-level :data:`todo_tool` directly (rather
        than a fresh construction) keeps tool identity stable across
        the process — the tool registry, telemetry, and permission
        checks all compare tool objects by identity, so constructing a
        new dict per call would break those comparisons.

        Returns:
            A one-element list containing :data:`todo_tool`.
        """
        return [todo_tool]
