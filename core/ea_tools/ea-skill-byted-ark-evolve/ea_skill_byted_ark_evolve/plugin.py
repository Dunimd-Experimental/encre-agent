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

"""Plugin definition for the *byted-ark-evolve* EA skill.

The Encre Agent plugin system discovers skill packages by importing this module
and calling :func:`create_plugin`.  The factory returns an instance of
:class:`_SkillPlugin`, whose behaviour is fully described by the class-level
``manifest`` attribute: it advertises the skill's identity, packaging metadata
and the list of skills it contributes to the host agent.

The manifest is intentionally declared as a class attribute (rather than built
inside ``__init__``) so it can be inspected by tooling without instantiating
the plugin — this is how the plugin market builds its offline index.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    """Instantiate the byted-ark-evolve skill plugin.

    This is the single public entry point that the host plugin loader calls.
    It is kept stateless on purpose: every behavioural detail lives in the
    ``_SkillPlugin.manifest`` class attribute, so each call simply yields a
    fresh, correctly configured instance.

    Returns:
        A configured :class:`EncrePlugin` instance ready for registration.
    """
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    """Concrete plugin type for the *byted-ark-evolve* skill bundle.

    No ``__init__`` override is required because :class:`EncrePlugin` already
    wires up lifecycle hooks from ``manifest``.  Declaring the manifest here
    keeps this class self-contained and trivially diffable when the skill is
    bumped to a new version.
    """

    # ``tier="system-default"`` marks this as bundled with the core install and
    # always enabled, which is what the market UI uses to grey out uninstall.
    # ``provides_skills`` maps the packaged directory name to the runtime skill
    # identifier that other components address by name.
    manifest = PluginManifest(
        name="ea-skill-byted-ark-evolve",
        display_name="Byted Ark Evolve",
        icon="assets/icon.svg",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: byted-ark-evolve.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "ai"],
        provides_skills=["byted-ark-evolve"],
        activation_events=[],
        permissions=[],
    )
