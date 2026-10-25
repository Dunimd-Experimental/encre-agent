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

"""Plugin definition for the *audio* system-default skill.

The plugin loader probes the distribution for a top-level
:func:`create_plugin` callable. When called, the factory returns a
fresh instance of :class:`_SkillPlugin`, whose ``manifest`` class
attribute declares the plugin's identity and the skills it advertises
to the harness.

The manifest is what the desktop market UI, the CLI, and the runtime
activation engine read. The ``provides_skills`` field registers the
``audio`` skill name with the harness's skills registry, which is what
makes the skill available to the agent at runtime.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    """Construct a fresh instance of this plugin.

    Called by the plugin loader exactly once per process. Keeping the
    factory as a function rather than exporting a class lets the
    loader control instantiation timing and lets tests inject a
    ``create_plugin`` stub without mutating module-level singletons.
    """
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    """Concrete plugin exposing the ``audio`` skill.

    The class is intentionally a plain ``EncrePlugin`` subclass with no
    per-instance state: the ``manifest`` class attribute carries all
    declarative metadata, and the loader only needs to read it. Keeping
    the class private (``_`` prefix) signals to callers that they
    should use :func:`create_plugin` rather than importing the class
    directly, which preserves the loader's ability to stub the factory.
    """

    manifest = PluginManifest(
        name="ea-skill-audio",
        display_name="Audio",
        icon="audio-lines",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: audio.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "media"],
        provides_skills=["audio"],
        activation_events=[],
        permissions=[],
    )
