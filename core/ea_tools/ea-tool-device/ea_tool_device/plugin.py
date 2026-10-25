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

"""Plugin factory and manifest for ``ea-tool-device``."""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_device.tool import device_tool


def create_plugin() -> EncrePlugin:
    """Construct and return the ``ea-tool-device`` plugin instance."""
    return _DevicePlugin()


class _DevicePlugin(EncrePlugin):
    """Encre Agent plugin object for the bundled ``device`` tool."""

    manifest = PluginManifest(
        name="ea-tool-device",
        display_name="Device",
        icon="monitor",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: device (info, battery, location, network, sensor).",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "system"],
        provides_tools=['device'],
        provides_skills=["tool-device"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the tool objects this plugin exposes."""
        return [device_tool]
