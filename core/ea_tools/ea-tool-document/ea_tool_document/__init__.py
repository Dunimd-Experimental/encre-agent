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

"""Package initializer for the ea-tool-document plugin.

This module serves as the public entry point for the document plugin.
It re-exports the key symbols (the tool object and the plugin
factory) from their respective submodules so that consumers can
import them directly from the package root without traversing into
internal modules.

Example usage::

    from ea_tool_document import document_tool, create_plugin
"""

from ea_tool_document.tool import document_tool
from ea_tool_document.plugin import create_plugin

# __all__ defines the public API surface exposed by "from package import *".
__all__ = ["document_tool", "create_plugin"]
