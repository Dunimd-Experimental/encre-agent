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

"""Plugin descriptor for ``ea-tool-translation``.

The Encre Agent plugin loader discovers this module when scanning the
``ea_tools`` directory, then calls :func:`create_plugin` to obtain a
:class:`_TranslationPlugin` instance. The instance contributes the
``translation`` tool to the tool registry and advertises the matching
``tool-translation`` skill entry point.

The manifest lives on the class as a class-level attribute so that it is
inspected without instantiating the plugin when the market UI enumerates
available system-default plugins. Fields on
:class:`~encre.plugins.types.PluginManifest` are validated by the loader
before the plugin is activated; a malformed entry here will cause the
plugin to be skipped rather than crash the whole session.

Because ``source`` is set to :attr:`PluginSource.BUNDLED` and ``tier``
to ``"system-default"``, the plugin is always installed alongside the
core distribution and cannot be disabled by end users without forking
the repo. That is deliberate: translation is a core workflow primitive.
"""

from __future__ import annotations

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_translation.tool import translation_tool


def create_plugin() -> EncrePlugin:
    """Build a fresh translation plugin instance for the loader.

    Kept as a factory rather than a module-level singleton so that the
    loader can own the lifecycle and so that test fixtures can construct
    isolated instances without leaking state between them.

    Returns:
        A new :class:`_TranslationPlugin` ready to be registered with
        the Encre Agent plugin registry.
    """
    return _TranslationPlugin()


class _TranslationPlugin(EncrePlugin):
    """Concrete plugin that wires the ``translation`` tool into the harness.

    Underscore-prefixed so external code goes through :func:`create_plugin`
    instead of bypassing the loader. The class does not override
    lifecycle hooks beyond :meth:`get_tools` because the tool descriptor
    is fully self-contained — no activation or deactivation side effects
    are needed.
    """

    manifest = PluginManifest(
        name="ea-tool-translation",
        display_name="Translation",
        icon="languages",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: translation.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "writing"],
        provides_tools=['translation'],
        provides_skills=["tool-translation"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the tool descriptors this plugin contributes.

        The list is intentionally short: this plugin exposes a single
        translation tool whose ``action`` field multiplexes translate /
        detect / languages operations. Splitting those into separate
        tools would pollute the schema surface for a marginal gain.

        Returns:
            A single-element list containing the module-level
            :data:`translation_tool` descriptor.
        """
        return [translation_tool]
