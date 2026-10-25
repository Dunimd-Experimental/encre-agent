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

"""Agent "soul" (identity) system package.

Exposes :class:`EncreSoulSystem` and the :class:`SoulFiles` container that
manage the agent's encrypted identity, personality, and user-knowledge files.

The soul system maintains three markdown files that define the agent's
core identity and its evolving relationship with the user:

- ``SOUL.md`` — the agent's personality, values, and behavioural principles.
- ``IDENTITY.md`` — the agent's self-description, capabilities, and background.
- ``USER.md`` — the agent's growing knowledge about the user's preferences,
  communication style, and past context.

All files are stored encrypted on disk (AES-256-GCM) and loaded at startup.
The :class:`EncreSoulSystem` class provides methods to load, update, and
build prompt sections from these files for injection into the agent's
system context.
"""

from encre.soul.system import EncreSoulSystem, SoulFiles

__all__ = [
    "EncreSoulSystem",
    "SoulFiles",
]
