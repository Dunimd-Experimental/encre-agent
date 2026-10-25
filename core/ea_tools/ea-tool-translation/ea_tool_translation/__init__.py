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

"""Public surface of the ``ea-tool-translation`` plugin package.

This package implements the ``translation`` system-default tool exposed by
Encre Agent. The public contract is intentionally tiny so that the plugin
loader and any downstream importers have a stable API to reach:

* ``translation_tool`` — the concrete tool descriptor built by
  :mod:`ea_tool_translation.tool`. It is handed to the tool registry during
  plugin activation.
* ``create_plugin`` — a factory called by the plugin loader to obtain a
  fresh :class:`~encre.plugins.types.EncrePlugin` instance. Callers should
  not instantiate the plugin class directly; going through the factory
  keeps the loader decoupled from the internal class name.

Both names are re-exported through ``__all__`` so that ``from
ea_tool_translation import *`` behaves predictably when the package is
loaded through ``importlib.import_module``.
"""

from __future__ import annotations

from ea_tool_translation.plugin import create_plugin
from ea_tool_translation.tool import translation_tool

__all__ = ["translation_tool", "create_plugin"]
