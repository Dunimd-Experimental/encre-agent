#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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

"""Hardcoded builtin tools of the harness.

These six tools (file-read / file-write / file-edit / terminal / web-search /
web-fetch) are the mandatory core toolset every Encre agent has, regardless
of which plugins are installed.  They were previously shipped as separate
``ea-tool-*`` EA packages; inlining them removes the circular dependency
(EA packages depending on ``encre-harness``) and makes a bare
``pip install encre-harness`` a working agent with the standard primitives.
"""

from __future__ import annotations

from typing import Any


def register_builtin_tools(tool_registry: Any) -> list[str]:
    """Register all six builtin tools into *tool_registry*.

    Idempotent: tools already present (same name) are skipped.  Returns
    the list of tool names registered by this call.
    """
    from encre.tools.builtin.file_edit import file_edit_tool
    from encre.tools.builtin.file_read import file_read_tool
    from encre.tools.builtin.file_write import file_write_tool
    from encre.tools.builtin.terminal import terminal_tool
    from encre.tools.builtin.web_fetch import web_fetch_tool
    from encre.tools.builtin.web_search import web_search_tool

    registered: list[str] = []
    existing = (
        set(tool_registry.list_tools())
        if hasattr(tool_registry, "list_tools")
        else set()
    )
    for tool in (
        file_read_tool, file_write_tool, file_edit_tool,
        terminal_tool, web_search_tool, web_fetch_tool,
    ):
        name = getattr(tool, "name", "")
        if name and name in existing:
            continue
        tool_registry.register(tool)
        registered.append(name)
    return registered
