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

"""Plugin descriptor for ``ea-tool-workflow``.

The Encre Agent plugin loader discovers this module when scanning the
``ea_tools`` directory and calls :func:`create_plugin` to obtain a
:class:`_WorkflowPlugin` instance. The instance contributes the
``workflow`` tool to the tool registry and advertises the matching
``tool-workflow`` skill entry point.

The manifest lives on the class as a class-level attribute so that it
is inspected without instantiating the plugin when the market UI
enumerates available system-default plugins. Fields on
:class:`~encre.plugins.types.PluginManifest` are validated by the
loader before the plugin is activated; a malformed entry here will
cause the plugin to be skipped rather than crash the whole session.

Because ``source`` is set to :attr:`PluginSource.BUNDLED` and ``tier``
to ``"system-default"``, this plugin is always installed alongside the
core distribution and cannot be disabled by end users without forking
the repo. That is deliberate: multi-agent orchestration is a core
workflow primitive for Encre Agent.
"""

from __future__ import annotations

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_workflow.tool import workflow_tool


def create_plugin() -> EncrePlugin:
    """Build a fresh workflow plugin instance for the loader.

    Kept as a factory rather than a module-level singleton so that
    the loader can own the lifecycle and so that test fixtures can
    construct isolated instances without leaking state between them.

    Returns:
        A new :class:`_WorkflowPlugin` ready to be registered with
        the Encre Agent plugin registry.
    """
    return _WorkflowPlugin()


class _WorkflowPlugin(EncrePlugin):
    """Concrete plugin that wires the ``workflow`` tool into the harness.

    Underscore-prefixed so external code goes through :func:`create_plugin`
    instead of bypassing the loader. The class does not override
    lifecycle hooks beyond :meth:`get_tools` because the tool descriptor
    is fully self-contained — no activation or deactivation side effects
    are needed.
    """

    manifest = PluginManifest(
        # Unique plugin identifier. Must match the PyPI distribution
        # name (``ea-tool-workflow``) so the market index can
        # correlate an installed plugin with its catalog entry.
        name="ea-tool-workflow",
        display_name="Workflow",
        # Lucide icon name used by the desktop plugin marketplace UI.
        icon="workflow",
        # SemVer version of the plugin. Bumped on every published
        # release; the desktop UI uses this to sort and filter.
        version="0.5.0",
        # Short description shown in the marketplace listing. Kept to
        # one sentence so it fits in the marketplace card.
        description="EA (Encre Agent) system-default tools: workflow.",
        # Author of the plugin. Uses the standard "Name <email>" format
        # so it round-trips with PyPI's author metadata.
        author="Dunimd Team <dunimd@outlook.com>",
        # SPDX license identifier. Must match the license file shipped
        # with the plugin distribution; CI validates this.
        license="Apache-2.0",
        # Project homepage. Displayed in the marketplace detail view;
        # also used by the desktop UI's "open on GitHub" affordance.
        homepage="https://github.com/mf2023/Encre",
        # Whether this plugin is bundled with Encre Agent itself
        # (``PluginSource.BUNDLED``) or was downloaded from the market
        # (``PluginSource.INSTALLED``). Bundled plugins are shipped
        # inside the Encre Agent wheel; installed plugins live in the
        # user's data directory.
        source=PluginSource.BUNDLED,
        # Human-readable tier label for the marketplace UI.
        # "system-default" means it is part of the base Encre Agent
        # distribution and cannot be uninstalled without restoring
        # defaults; "user" would be a downloaded plugin.
        tier="system-default",
        # Other plugins this one depends on. Empty for this plugin
        # because the workflow tool is self-contained.
        dependencies=[],
        # Minimum Encre Agent engine version required. The loader
        # refuses to load a plugin whose engine constraint is not
        # satisfied by the running Encre Agent version.
        engines={"encre": ">=0.5"},
        # Free-form tags used for marketplace filtering and search.
        tags=["system-default", "dev"],
        # Tool names this plugin registers with the shared tool
        # registry. Each name must resolve to a real tool module
        # somewhere on the search path; the loader validates this at
        # install time and refuses to load the plugin if any listed
        # tool cannot be found.
        provides_tools=['workflow'],
        # Skill names this plugin registers with the shared skill
        # registry. Each name must resolve to a real skill module
        # somewhere on the search path; the loader validates this at
        # install time and refuses to load the plugin if any listed
        # skill cannot be found.
        provides_skills=["tool-workflow"],
        # Events that should trigger automatic activation of this
        # plugin. Empty here because the tool is always loaded as
        # part of the bundled set — it does not need to react to
        # external events.
        activation_events=[],
        # Permissions the plugin requests at runtime. Empty because
        # the workflow tool only reads files that the user has explicitly
        # pointed the agent at; it does not need extra filesystem,
        # network, or system access.
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the tool descriptors this plugin contributes.

        This plugin exposes a single ``workflow`` tool whose ``mode``
        field multiplexes between the ``light`` DAG pipeline and the
        ``team`` swarm-with-consensus flow. Splitting those into two
        tools would double the schema surface for what is essentially
        the same orchestration primitive with a richer policy.

        Returns:
            A single-element list containing the module-level
            :data:`workflow_tool` descriptor.
        """
        return [workflow_tool]
