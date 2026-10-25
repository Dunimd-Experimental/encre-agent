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

"""Package root for the ``ea-tool-ssh`` plugin.

This package wires together three pieces of the Encre Agent plugin contract:

1. ``ssh_tool`` — the actual tool object that the Encre Agent tool-dispatch
   layer invokes when a caller asks to run a command or move a file over
   SSH/SCP. Lives in :mod:`ea_tool_ssh.tool`.
2. ``create_plugin`` — a factory that returns an :class:`EncrePlugin`
   instance carrying the plugin manifest (name, version, provided tools,
   engines, tags). Lives in :mod:`ea_tool_ssh.plugin`.

Re-exporting both symbols here means the plugin loader can import either
name from the package top level (``from ea_tool_ssh import ssh_tool`` or
``from ea_tool_ssh import create_plugin``) without needing to know about
the internal module layout. The split between ``tool.py`` (the object) and
``plugin.py`` (the manifest) keeps the wiring concerns separate from the
SSH transport logic so either can evolve independently.
"""

from ea_tool_ssh.tool import ssh_tool
from ea_tool_ssh.plugin import create_plugin

__all__ = [ssh_tool, 'create_plugin']
