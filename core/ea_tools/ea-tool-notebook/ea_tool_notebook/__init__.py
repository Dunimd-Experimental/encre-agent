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

"""Package entry point for the ``ea-tool-notebook`` plugin.

This package hosts the *notebook* tool that ships with Encre Agent.
Two public symbols are exported:

- :data:`notebook_tool` -- the built :class:`EncreTool` instance
  that agents invoke to drive a Jupyter-style notebook session with
  a persistent kernel.
- :func:`create_plugin` -- the plugin factory the loader uses to
  obtain a live :class:`EncrePlugin` instance whose ``get_tools``
  returns :data:`notebook_tool`.

Both are re-exported here so callers can do
``from ea_tool_notebook import notebook_tool, create_plugin`` without
knowing the internal module layout.
"""

from ea_tool_notebook.tool import notebook_tool
from ea_tool_notebook.plugin import create_plugin

__all__ = [notebook_tool, 'create_plugin']
