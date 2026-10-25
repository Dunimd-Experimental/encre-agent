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

"""Plugin factory and manifest for ``ea-tool-test-runner``.

Each Encre Agent tool plugin exposes a :func:`create_plugin` factory
that returns an :class:`EncrePlugin` instance. The loader calls the
factory once per process, reads the returned instance's
:class:`PluginManifest` for metadata (name, version, tags, engine
range), and calls :meth:`_TestRunnerPlugin.get_tools` to obtain the
actual tool objects it should register.

Keeping the manifest in a separate module from the tool object keeps
the ``tool.py`` import graph free of plugin-loader types. This matters
because tests and direct programmatic callers often import the tool
without wanting to pull in :mod:`encre.plugins.types` at all.

The plugin advertises both a tool (``test_run``) and a skill tag
(``tool-test_run``); the tool is the callable object the agent
dispatches on, while the skill tag is a naming handle used by the
intent router and the market UI.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_test_runner.tool import test_run_tool


def create_plugin() -> EncrePlugin:
    """Construct and return the ``ea-tool-test-runner`` plugin instance.

    The factory returns a fresh :class:`_TestRunnerPlugin` on every
    call rather than caching a singleton. The plugin instance carries
    no mutable state beyond the immutable :attr:`manifest` attribute,
    so constructing it repeatedly is cheap and the factory is safe to
    invoke from the market UI as it enumerates installed plugins.

    Returns:
        An :class:`EncrePlugin` instance whose ``manifest`` describes
        the bundled ``test_run`` tool and whose ``get_tools`` returns
        the :data:`test_run_tool` object exactly once.
    """
    return _TestRunnerPlugin()


class _TestRunnerPlugin(EncrePlugin):
    """Encre Agent plugin object for the bundled ``test_run`` tool.

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
    * ``danger_levels={"test_run": "high"}`` declares the ``test_run``
      tool as high-danger at the plugin level. This is a broader
      signal than the ``cost_level="high"`` on the tool object itself
      and is used by permission-aware callers (such as the desktop
      UI's auto-approve toggle) to prompt for confirmation before
      running the potentially expensive test suite.
    * No ``activation_events`` or ``permissions`` are declared because
      the tool only touches the local filesystem and subprocesses; it
      never hooks into session lifecycle events.
    """

    manifest = PluginManifest(
        name="ea-tool-test-runner",
        display_name="Test Runner",
        icon="flask-conical",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: test_run.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "dev"],
        provides_tools=['test_run'],
            danger_levels={"test_run": "high"},
        provides_skills=["tool-test_run"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the tool objects this plugin exposes.

        The Encre Agent tool-dispatch layer calls this once at startup
        and registers each returned object in its global registry.
        Returning the module-level :data:`test_run_tool` directly
        (rather than a fresh construction) keeps tool identity stable
        across the process — the tool registry, telemetry, and
        permission checks all compare tool objects by identity, so
        constructing a new dict per call would break those comparisons.

        Returns:
            A one-element list containing :data:`test_run_tool`.
        """
        return [test_run_tool]
