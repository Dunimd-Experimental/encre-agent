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

"""
EA byted-ark-trainer skill plugin.

This module defines the Encre Agent plugin manifest and factory for the
ByteDance Volcano Ark model training skill. The plugin registers the
``byted-ark-trainer`` skill, which provides CLI-based tooling for managing
training jobs, foundation models, inference endpoints, and evaluation
pipelines on the Volcengine ARK platform.

The ``_SkillPlugin`` class is intentionally prefixed with an underscore
to signal that it is an internal implementation detail; consumers should
always call ``create_plugin()`` instead of instantiating the class directly.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    """
    Factory function that constructs and returns a new ``_SkillPlugin`` instance.

    This is the single entry point used by the Encre Agent plugin loader
    to instantiate this skill. The loader calls ``create_plugin()`` and
    registers the returned ``EncrePlugin`` instance together with its
    ``PluginManifest`` metadata.

    Returns:
        A fully-initialized ``_SkillPlugin`` instance with its class-level
        ``manifest`` attribute populated.
    """
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    """
    Internal plugin implementation for the ``ea-skill-byted-ark-trainer`` skill.

    Inherits from :class:`EncrePlugin` and declares a ``PluginManifest`` at
    the class level. The manifest tells the plugin loader about the skill's
    name, version, dependencies, and which skills it provides.

    The class is named with a leading underscore to discourage direct
    subclassing or instantiation outside of ``create_plugin()``.
    """

    manifest = PluginManifest(
        name="ea-skill-byted-ark-trainer",
        display_name="Byted Ark Trainer",
        icon="assets/icon.svg",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: byted-ark-trainer.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "ai"],
        provides_skills=["byted-ark-trainer"],
        activation_events=[],
        permissions=[],
    )
