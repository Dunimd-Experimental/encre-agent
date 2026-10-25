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

"""Package entry point for the ``ea-tool-notify`` plugin.

This package re-exports the two symbols the Encre Agent plugin loader needs
to discover and register the tool:

* ``notify_tool`` -- the fully-built ``EncreTool`` instance that the agent
  dispatches calls against.
* ``create_plugin`` -- a zero-argument factory that returns a new
  ``EncrePlugin`` instance. The plugin loader calls this once per session so
  that each session gets an isolated plugin object rather than sharing a
  single global instance.

Keeping both symbols on the package root lets callers ``from ea_tool_notify
import create_plugin`` without having to reach into ``.plugin`` and ``.tool``
by name. The plugin factory is *not* called at import time on purpose:
importing the package must be side-effect free so that test harnesses and
``python -m compileall`` can touch it cheaply.
"""

from __future__ import annotations

from ea_tool_notify.tool import notify_tool
from ea_tool_notify.plugin import create_plugin

__all__ = [notify_tool, 'create_plugin']
