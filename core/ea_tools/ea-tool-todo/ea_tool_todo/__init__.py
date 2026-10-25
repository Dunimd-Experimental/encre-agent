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

"""Package root for the ``ea-tool-todo`` plugin.

This plugin exposes the ``todo`` tool, which maintains the current
session's structured task list — the same list that is rendered as a
live plan card on the frontend. Unlike the sibling ``task_*`` tools
(which manage background sub-tasks delegated to the harness scheduler),
``todo`` is the model's own per-session plan: it is synchronous,
session-scoped, and mutated entirely through this single tool.

Re-exporting both symbols from this package top level lets the plugin
loader and any downstream user call either ``from ea_tool_todo
import todo_tool`` (the callable) or ``from ea_tool_todo
import create_plugin`` (the factory) without knowing about the
internal module layout.
"""

from ea_tool_todo.tool import todo_tool
from ea_tool_todo.plugin import create_plugin

__all__ = [todo_tool, 'create_plugin']
