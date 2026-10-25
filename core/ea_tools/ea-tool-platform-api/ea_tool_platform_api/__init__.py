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

"""Package entry point for the ``ea-tool-platform-api`` plugin.

Re-exports the three ``EncreTool`` instances backed by OpenAI-compatible
platform APIs, plus the ``create_plugin`` factory used by the plugin
loader:

* ``file_api_tool`` -- stage JSONL input files and manage uploads.
* ``batch_api_tool`` -- create and poll asynchronous batch jobs.
* ``fine_tune_api_tool`` -- start and list fine-tuning jobs.

The factory is not called at import time so this package can be
imported cheaply by tooling and test harnesses without touching the
runtime backend. Callers should go through ``create_plugin`` rather
than importing the underscore-prefixed class directly.
"""

from __future__ import annotations

from ea_tool_platform_api.tool import batch_api_tool, file_api_tool, fine_tune_api_tool
from ea_tool_platform_api.plugin import create_plugin

__all__ = [batch_api_tool, file_api_tool, fine_tune_api_tool, 'create_plugin']
