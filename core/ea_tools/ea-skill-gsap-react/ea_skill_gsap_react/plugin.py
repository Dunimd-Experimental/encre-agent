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

"""Plugin definition for the ``ea-skill-gsap-react`` skill.

This module provides the plugin manifest and factory function that the Encre
Agent plugin system uses to register the "gsap-react" skill. The skill itself
is packaged as data (SKILL.md, helper scripts, etc.) under the ``skills/``
directory and is discovered at runtime by the Encre Agent Harness when a session
activates this plugin. The manifest declared below serves as the contract between
this plugin and the harness: it declares metadata, dependency requirements, and
activation behaviour that the plugin registry uses for discovery, UI rendering,
and policy enforcement.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    """Return a new instance of the gsap-react skill plugin.

    This factory function is the entry point that the Encre Agent plugin loader
    calls to instantiate the plugin. It creates and returns a
    :class:`_SkillPlugin` object whose ``manifest`` attribute describes the
    skill's identity, version, source tier, and engine compatibility constraints.

    Returns:
        An :class:`EncrePlugin` instance configured with the gsap-react manifest.
    """
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    """Encre Agent plugin implementation for the gsap-react skill.

    Inherits from :class:`EncrePlugin` and exposes a single ``manifest`` class
    attribute. The manifest is the primary contract between this plugin and the
    Encre Agent harness. It declares the skill name, display name, icon path,
    version, description, source tier (system-default), engine version
    requirements, tags for filtering, the list of skill identifiers provided,
    activation events (empty because this skill is manually invoked), and
    permissions (empty because this skill provides GSAP React integration
    guidance without requiring system-level permissions).
    """

    manifest = PluginManifest(
        name="ea-skill-gsap-react",
        display_name="Gsap React",
        icon="assets/icon.svg",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: gsap-react.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "web"],
        provides_skills=["gsap-react"],
        activation_events=[],
        permissions=[],
    )
