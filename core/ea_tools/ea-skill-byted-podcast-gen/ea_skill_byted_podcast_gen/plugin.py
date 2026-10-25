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

"""EA byted-podcast-gen skill plugin.

Defines the plugin factory and the ``PluginManifest`` describing the
bundled byted-podcast-gen skill so the Encre Agent plugin loader can
discover, tier and gate it correctly.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    """Construct the byted-podcast-gen plugin instance.

    The plugin system calls this factory once per load and expects a fully
    initialised ``EncrePlugin`` subclass instance back. Returning a new
    ``_SkillPlugin`` keeps the module import side-effect free: manifest
    metadata is attached at class definition time, so instantiating the
    subclass is all the wiring the loader needs.
    """
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    """Bundled skill plugin exposing the ``byted-podcast-gen`` skill.

    The ``manifest`` class attribute is read by the loader to register
    skill identifiers, declare tiering (``system-default``) and the engine
    version floor required before this plugin is activated.
    """

    manifest = PluginManifest(
        name="ea-skill-byted-podcast-gen",
        display_name="Byted Podcast Gen",
        icon="assets/icon.svg",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: byted-podcast-gen.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "media"],
        provides_skills=["byted-podcast-gen"],
        activation_events=[],
        permissions=[],
    )
