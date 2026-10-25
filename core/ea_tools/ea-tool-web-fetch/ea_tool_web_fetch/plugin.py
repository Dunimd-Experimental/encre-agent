#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

"""EA Web Fetch Plugin."""

from __future__ import annotations

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource

from ea_tool_web_fetch.tool import web_fetch_tool


def create_plugin() -> EncrePlugin:
    return _WebFetchPlugin()


class _WebFetchPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-tool-web-fetch",
        display_name="Web Fetch",
        icon="cloud-download",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: web_fetch.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "web"],
        provides_tools=["web_fetch"],
        provides_skills=["tool-web_fetch"],
        activation_events=[],
        permissions=["network"],
    )

    def get_tools(self) -> list:
        return [web_fetch_tool]
