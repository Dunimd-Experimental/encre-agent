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

"""Package entry point for the ``ea-tool-workflow`` plugin.

This ``__init__.py`` is the top-level import surface for the plugin.
It re-exports both the tool implementation (:data:`workflow_tool`,
defined in :mod:`ea_tool_workflow.tool`) and the plugin factory
(:func:`create_plugin`, defined in :mod:`ea_tool_workflow.plugin`)
so the Encre Agent plugin loader can use a single, stable import path
regardless of how the internal layout of the package is organised.

Keeping both re-exports in ``__init__.py`` — rather than making
callers import from ``.tool`` and ``.plugin`` directly — decouples
the loader contract from the internal file layout: the plugin's own
modules can be reorganised, renamed, or split, as long as the
``workflow_tool`` and ``create_plugin`` symbols are still exported
from this package.
"""

from __future__ import annotations

from ea_tool_workflow.tool import workflow_tool
from ea_tool_workflow.plugin import create_plugin

__all__ = [workflow_tool, 'create_plugin']
