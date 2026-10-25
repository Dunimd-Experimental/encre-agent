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

"""Encre Agent tool system package.

This module is the public entry point for the Encre Agent tool system,
providing access to the core abstractions used throughout the harness:

- :class:`EncreTool` — the base class that all tools must subclass to
  define their schema, input validation, and execution logic.
- :class:`ToolRegistry` — the central registry that tracks all available
  tools, supports lookup by name, and provides filtering by capability.
- :class:`ToolSet` and :data:`TOOLSETS` — predefined groupings of tools
  that can be resolved as a unit, useful for enabling or disabling
  collections of related tools together (e.g., all browser tools, all
  file-system tools).
- :func:`resolve_toolset` — resolves a named toolset to its concrete
  tool definitions.
- :func:`list_available_sets` — returns the names of all registered
  toolsets, useful for CLI and UI discovery.

The tool system is designed around three concerns:

1. **Schema definition** — each tool declares its JSON Schema, which is
   used for input validation, LLM prompt construction, and UI rendering.
2. **Execution isolation** — tool execution is wrapped in a sandboxed
   context that captures errors, tracks timing, and enforces timeouts.
3. **Registry indirection** — tools are looked up by name through the
   registry rather than imported directly, allowing plugins to add
   tools dynamically without modifying the harness code.

Typical usage::

    from encre.tools import ToolRegistry, EncreTool

    registry = ToolRegistry()
    tool = registry.get("bash")  # Returns an EncreTool instance

    result = await tool.execute(input={"command": "ls -la"})
"""

from __future__ import annotations

from encre.tools.base import EncreTool
from encre.tools.registry import ToolRegistry
from encre.tools.toolset import (
    TOOLSETS,
    ToolSet,
    list_available_sets,
    resolve_toolset,
)

__all__ = [
    "EncreTool",
    "ToolRegistry",
    "ToolSet",
    "TOOLSETS",
    "resolve_toolset",
    "list_available_sets",
]
