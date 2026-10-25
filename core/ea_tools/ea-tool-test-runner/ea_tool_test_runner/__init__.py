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

"""Package root for the ``ea-tool-test-runner`` plugin.

This plugin exposes the ``test_run`` tool, which drives the project's
native test runner (pytest, vitest, jest, or cargo) as a subprocess
and returns a structured JSON report of per-test outcomes.

Re-exporting both symbols from this package top level lets the plugin
loader and any downstream user call either ``from ea_tool_test_runner
import test_run_tool`` (the callable) or ``from ea_tool_test_runner
import create_plugin`` (the factory) without knowing about the
internal module layout.
"""

from ea_tool_test_runner.tool import test_run_tool
from ea_tool_test_runner.plugin import create_plugin

__all__ = [test_run_tool, 'create_plugin']
