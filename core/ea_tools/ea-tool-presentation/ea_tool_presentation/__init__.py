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

"""Package entry point for the ``ea-tool-presentation`` plugin.

Re-exports the ``EncreTool`` instance used to read, create, and edit
PowerPoint (``.pptx``) presentations, plus the ``create_plugin``
factory used by the plugin loader.

The factory is not called at import time so that importing this
package remains side-effect free -- the actual ``python-pptx`` import
happens lazily inside the tool's action handlers, so a machine without
``python-pptx`` installed can still ``import ea_tool_presentation``.
"""

from __future__ import annotations

from ea_tool_presentation.tool import presentation_tool
from ea_tool_presentation.plugin import create_plugin

__all__ = [presentation_tool, 'create_plugin']
