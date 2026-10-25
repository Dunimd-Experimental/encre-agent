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

"""Plugin factory and manifest for ``ea-tool-ssh``.

The Encre Agent plugin contract asks each shipped tool plugin to expose a
``create_plugin()`` factory that returns an :class:`EncrePlugin` instance.
The loader calls the factory exactly once per process, reads the returned
instance's :attr:`EncrePlugin.manifest` for metadata (name, version, tag,
engine range), and asks it for the list of tools via :meth:`get_tools`.

Keeping the manifest here rather than in :mod:`ea_tool_ssh.tool` is
deliberate: the tool module only needs to describe *what a single tool
does*, while the plugin module describes *what this whole package offers
to the market*. That separation lets the tool object be reused outside a
plugin context (for example in unit tests or direct programmatic calls)
without pulling in manifest fields like ``tier``, ``homepage``, or
``engines``.

See also:
    :class:`EncrePlugin` — base class imported from
    ``encre.plugins.types``; defines the ``manifest`` and ``get_tools``
    contract that all Encre Agent plugins must satisfy.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_ssh.tool import ssh_tool


def create_plugin() -> EncrePlugin:
    """Construct and return the ``ea-tool-ssh`` plugin instance.

    The loader calls this factory when it discovers the package through
    the plugin registry. Returning a fresh :class:`_SshPlugin` on every
    call keeps the plugin lightweight — no module-level singleton state,
    no caches — which means the factory is safe to call repeatedly (for
    example by the market UI when it enumerates installed plugins).

    Returns:
        An :class:`EncrePlugin` instance whose ``manifest`` describes the
        bundled SSH/SCP tool and whose ``get_tools`` returns the
        ``ssh`` tool object exactly once.
    """
    return _SshPlugin()


class _SshPlugin(EncrePlugin):
    """Encre Agent plugin object for the bundled SSH/SCP tool.

    The class body carries only a :class:`PluginManifest` — the manifest
    is declared at import time so the loader can read it synchronously
    without instantiating anything. The manifest advertises both a tool
    (``ssh``) and a skill name (``tool-ssh``); the tool is the callable
    object, the skill is a naming tag used by the intent router.

    The ``tier="system-default"`` and ``tags=["system-default"]`` fields
    mark this plugin as part of the Encre Agent core distribution rather
    than a user-installed marketplace entry. Downstream filtering in the
    market UI and the plugin loader both key off these fields.

    Notes:
        No ``activation_events`` or ``permissions`` are declared because
        this tool operates purely on user-provided SSH credentials at
        call time and never needs to hook into session lifecycle events.
    """

    manifest = PluginManifest(
        name="ea-tool-ssh",
        display_name="Ssh",
        icon="terminal",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: ssh.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "cloud"],
        provides_tools=['ssh'],
        provides_skills=["tool-ssh"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the list of tool objects this plugin exposes.

        The Encre Agent tool-dispatch layer calls this once at startup and
        registers each returned object in its global registry. Returning
        the module-level :data:`ssh_tool` directly (rather than a fresh
        construction) keeps tool identity stable across the process — the
        tool registry, telemetry, and permission checks all compare tool
        objects by identity, so constructing a new dict per call would
        break those comparisons.

        Returns:
            A one-element list containing the :data:`ssh_tool` object.
        """
        return [ssh_tool]
