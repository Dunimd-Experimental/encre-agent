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

"""Skills system package.

Exposes the :class:`EncreSkillRegistry` and the
:class:`BundledSkillDefinition` data structure used to register, resolve, and
activate agent skills (reusable prompt bundles).

A skill is a self-contained prompt template that the agent can invoke to
perform a specialised task.  Skills are loaded from ``SKILL.md`` files on
disk or registered programmatically at startup.  The registry resolves
conflicts between same-named skills using a source-priority hierarchy
(managed > user > project > bundled), so that a user-provided skill always
overrides a bundled one of the same name.
"""

from encre.skills.registry import EncreSkillRegistry
from encre.skills.types import BundledSkillDefinition

__all__ = [
    "BundledSkillDefinition",
    "EncreSkillRegistry",
]
