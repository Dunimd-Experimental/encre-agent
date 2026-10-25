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

"""Plugin factory and manifest for ``ea-tool-cron``."""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_cron.tool import cron_tool


def create_plugin() -> EncrePlugin:
    """Construct and return the ``ea-tool-cron`` plugin instance."""
    return _CronPlugin()


class _CronPlugin(EncrePlugin):
    """Encre Agent plugin object for the bundled ``cron`` tool."""

    manifest = PluginManifest(
        name="ea-tool-cron",
        display_name="Cron",
        icon="alarm-clock",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: cron (create, list, delete).",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "dev"],
        provides_tools=['cron'],
        provides_skills=["tool-cron"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the tool objects this plugin exposes."""
        return [cron_tool]
