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

"""Package entry point for the ``ea-tool-pdf`` plugin.

Re-exports the two symbols the Encre Agent plugin loader needs to discover
and register the tool:

* ``pdf_tool`` -- the fully-built ``EncreTool`` instance used to extract
  text and metadata from PDF files.
* ``create_plugin`` -- a factory that returns a fresh ``EncrePlugin``
  instance per session.

The factory is not called at import time so that importing this package
remains side-effect free for test harnesses and ``python -m compileall``.
Callers who want to reach the plugin class directly should still go
through ``create_plugin`` rather than importing the underscore-prefixed
class from ``ea_tool_pdf.plugin``.
"""

from __future__ import annotations

from ea_tool_pdf.tool import pdf_tool
from ea_tool_pdf.plugin import create_plugin

__all__ = [pdf_tool, 'create_plugin']
