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

"""Plugin definition for the *media-api* system-default tools.

The plugin loader probes the distribution for a top-level
:func:`create_plugin` callable. When called, the factory returns a
fresh instance of :class:`_MediaApiPlugin`, whose ``manifest`` class
attribute declares the plugin's identity and whose :meth:`get_tools`
overrides the base class to expose all four media-API tools.

Unlike single-tool plugins, this plugin bundles four related
capabilities (transcribe, translate, embeddings, moderation) under
one manifest because they all draw on the same multimodal backend.
Grouping them lets the desktop UI show them as one entry while the
agent sees four individually-callable tools.

Each tool is also registered as a "skill" (via ``provides_skills``)
so the desktop UI can surface it as a skill entry in addition to a
tool entry -- this is a naming convention rather than a functional
difference.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_media_api.tool import create_embeddings_tool, create_moderation_tool, transcribe_audio_tool, translate_audio_tool


def create_plugin() -> EncrePlugin:
    """Construct a fresh instance of this plugin.

    Called by the plugin loader exactly once per process. Keeping the
    factory as a function rather than exporting a class lets the
    loader control instantiation timing and lets tests inject a
    ``create_plugin`` stub without mutating module-level singletons.
    """
    return _MediaApiPlugin()


class _MediaApiPlugin(EncrePlugin):
    """Concrete plugin exposing the four media-API tools.

    The class is intentionally a plain ``EncrePlugin`` subclass. The
    ``manifest`` class attribute carries all declarative metadata,
    and :meth:`get_tools` is overridden so the loader sees all four
    built tool instances rather than an empty tool list.
    """

    manifest = PluginManifest(
        name="ea-tool-media-api",
        display_name="Media API",
        icon="brain",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: create_embeddings, create_moderation, transcribe_audio, translate_audio.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default"],
        provides_tools=['create_embeddings', 'create_moderation', 'transcribe_audio', 'translate_audio'],
        provides_skills=["tool-create_embeddings", "tool-create_moderation", "tool-transcribe_audio", "tool-translate_audio"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the tools this plugin registers with the harness.

        Returning a fresh list (rather than a module-level tuple)
        lets a caller mutate the returned list without corrupting
        other plugins' registries.
        """
        return [create_embeddings_tool, create_moderation_tool, transcribe_audio_tool, translate_audio_tool]
