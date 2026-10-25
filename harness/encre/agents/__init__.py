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

"""Public entry points for the built-in sub-agent roster.

This package re-exports :func:`get_builtin_sub_agents` so that callers can do
``from encre.agents import get_builtin_sub_agents`` without reaching into the
``builtin`` submodule.  Keeping the export surface narrow makes it easy to
tell at a glance which symbols are part of the stable agents API.
"""

from encre.agents.builtin import get_builtin_sub_agents

__all__ = [
    "get_builtin_sub_agents",
]
