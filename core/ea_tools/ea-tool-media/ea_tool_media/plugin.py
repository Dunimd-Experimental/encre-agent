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

"""Plugin definition for the *media* system-default tool.

The plugin loader probes the distribution for a top-level
:func:`create_plugin` callable. When called, the factory returns a
fresh instance of :class:`_MediaPlugin`, whose ``manifest`` class
attribute declares the plugin's identity and whose :meth:`get_tools`
overrides the base class to expose :data:`media_tool`.

The manifest declares a ``danger_levels`` override for ``media`` at
``"high"`` because the tool can rewrite media files on disk (trim,
convert, compress) and a runaway ffmpeg invocation could chew through
a large amount of disk space. The desktop UI uses that field to gate
destructive actions behind an extra confirmation prompt.

The tool is also registered as a "skill" (via ``provides_skills``)
so the desktop UI can surface it as a skill entry in addition to a
tool entry -- this is a naming convention rather than a functional
difference.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_media.tool import media_tool


def create_plugin() -> EncrePlugin:
    """Construct a fresh instance of this plugin.

    Called by the plugin loader exactly once per process. Keeping the
    factory as a function rather than exporting a class lets the
    loader control instantiation timing and lets tests inject a
    ``create_plugin`` stub without mutating module-level singletons.
    """
    return _MediaPlugin()


class _MediaPlugin(EncrePlugin):
    """Concrete plugin exposing the ``media`` tool.

    The class is intentionally a plain ``EncrePlugin`` subclass. The
    ``manifest`` class attribute carries all declarative metadata,
    and :meth:`get_tools` is overridden so the loader sees the built
    :data:`media_tool` instance rather than an empty tool list.
    """

    manifest = PluginManifest(
        name="ea-tool-media",
        display_name="Media",
        icon="film",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: media.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "media"],
        provides_tools=['media'],
            danger_levels={"media": "high"},
        provides_skills=["tool-media"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the tools this plugin registers with the harness.

        Returning a fresh list (rather than a module-level tuple)
        lets a caller mutate the returned list without corrupting
        other plugins' registries.
        """
        return [media_tool]
