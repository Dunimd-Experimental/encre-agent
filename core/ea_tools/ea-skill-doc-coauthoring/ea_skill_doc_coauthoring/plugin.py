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

"""Doc Coauthoring skill plugin for Encre Agent.

This module declares a *system-default* Encre plugin that advertises the
``doc-coauthoring`` skill. The host does not import skill implementations
dynamically here; instead it reads the :class:`PluginManifest` embedded on
the plugin class and uses the ``provides_skills`` list to route incoming
requests to the skill payloads that live under this package's ``skills/``
directory.

Kept deliberately free of I/O at import time: the manifest is a static
data structure so that scanning the plugin catalog is a pure attribute
read, not a filesystem traversal.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    """Instantiate the doc-coauthoring plugin.

    Returns a fresh :class:`_SkillPlugin` instance each call so that the
    host can register the plugin per-session without sharing mutable state
    between independent Encre Agent processes.

    :returns: A configured :class:`EncrePlugin` carrying this package's
        manifest.
    """
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    """Doc Coauthoring plugin implementation.

    All behavioural metadata is expressed declaratively through the
    class-level ``manifest``. The runtime derives discovery keys, engine
    compatibility, and permission checks from that object, so the class
    body intentionally stays free of per-instance state.

    Key manifest fields used by the loader:

    * ``source=PluginSource.BUNDLED`` marks this plugin as shipped inside
      the Encre Agent distribution rather than user-installed, which
      affects upgrade semantics.
    * ``tier="system-default"`` groups it with the pre-installed skill
      set surfaced in the default UI.
    * ``provides_skills`` enumerates skill ids that the skill registry
      will resolve to the ``skills/doc-coauthoring`` resource folder.
    """

    manifest = PluginManifest(
        name="ea-skill-doc-coauthoring",
        display_name="Doc Coauthoring",
        icon="users",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: doc-coauthoring.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "writing"],
        provides_skills=["doc-coauthoring"],
        activation_events=[],
        permissions=[],
    )
