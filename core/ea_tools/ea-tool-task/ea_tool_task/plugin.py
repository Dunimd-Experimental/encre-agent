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

"""Plugin factory and manifest for ``ea-tool-task``.

This plugin consolidates the entire task lifecycle — create, list, get,
output, stop, update — into a single ``task`` tool. Previously these were
six separate packages that could be independently uninstalled, creating
logical impossibilities (e.g. creating tasks but never being able to stop
them). The merged package ensures the lifecycle is atomic: install or
uninstall the whole thing.
"""

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_task.tool import task_tool


def create_plugin() -> EncrePlugin:
    """Construct and return the ``ea-tool-task`` plugin instance.

    Returns:
        An :class:`EncrePlugin` instance whose ``manifest`` describes
        the bundled ``task`` tool and whose ``get_tools`` returns the
        :data:`task_tool` object exactly once.
    """
    return _TaskPlugin()


class _TaskPlugin(EncrePlugin):
    """Encre Agent plugin object for the bundled ``task`` tool.

    The class body carries only a :class:`PluginManifest`. The manifest
    is a plain data object evaluated at import time so the loader can
    read it synchronously without instantiating anything.
    """

    manifest = PluginManifest(
        name="ea-tool-task",
        display_name="Task",
        icon="play-circle",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: task (create, list, get, output, stop, update).",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "dev"],
        provides_tools=['task'],
        provides_skills=["tool-task"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the tool objects this plugin exposes.

        Returns:
            A one-element list containing :data:`task_tool`.
        """
        return [task_tool]
