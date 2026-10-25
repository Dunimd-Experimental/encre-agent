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

"""EA byted-seedance-video-generate skill plugin.

Declares the plugin factory and the ``PluginManifest`` that the Encre
Agent plugin loader uses to register, tier and gate the Seedance video
generation skill.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    """Construct the byted-seedance-video-generate plugin instance.

    The loader calls this factory once per load. Manifest metadata is
    attached at class definition time, so simply instantiating
    ``_SkillPlugin`` performs all the wiring the loader needs.
    """
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    """Bundled skill plugin exposing the ``byted-seedance-video-generate`` skill.

    ``provides_skills`` is what the loader matches against when a user
    asks for the skill by name; ``engines`` declares the minimum Encre
    version so older installs decline to load it.
    """

    manifest = PluginManifest(
        name="ea-skill-byted-seedance-video-generate",
        display_name="Byted Seedance Video Generate",
        icon="assets/icon.svg",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: byted-seedance-video-generate.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "media"],
        provides_skills=["byted-seedance-video-generate"],
        activation_events=[],
        permissions=[],
    )
