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

"""Manifest and factory for the ea-skill-byted-mediakit-image system-default skill.

The plugin loader calls ``create_plugin()`` at import time to obtain an
``EncrePlugin`` instance whose ``manifest`` describes what the skill
provides (name, tags, provided skills, dependencies, engine constraints).
Keeping the manifest next to the factory keeps each skill self-contained
and lets CI validate the plugin catalog without loading the skill's
runtime code.

The ``tier="system-default"`` tag marks this skill as one of the base
capabilities shipped with the Encre Agent distribution; ``source=BUNDLED``
means the payload is included in the wheel rather than fetched from the
remote plugin registry at runtime.
"""

from __future__ import annotations

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    """Instantiate and return the skill's plugin object.

    The factory shape (function returning an ``EncrePlugin``) is what the
    loader expects; every ``create_plugin`` symbol across the tree
    follows this contract so the loader can iterate uniformly without
    reflection.
    """
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    """Concrete ``EncrePlugin`` for ``ea-skill-byted-mediakit-image``."""

    manifest = PluginManifest(
        name="ea-skill-byted-mediakit-image",
        display_name="Byted Mediakit Image",
        icon="assets/icon.svg",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: byted-mediakit-image.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "media"],
        provides_skills=["byted-mediakit-image"],
        activation_events=[],
        permissions=[],
    )
