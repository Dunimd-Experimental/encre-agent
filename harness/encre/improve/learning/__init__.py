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

"""Learning subsystem for Encre.

This package turns the agent's repeated behaviour into durable, reusable
knowledge so that future runs start from a richer baseline instead of raw
experience. It is composed of three collaborating components:

* :class:`~encre.learning.engine.LearningEngine` -- observes a running
  agent loop, records how tools are combined into patterns, and decides
  when a frequently repeated pattern is worth crystallising into a skill.
* :class:`~encre.learning.skill_generator.SkillGenerator` -- given a
  detected tool pattern, synthesises a structured skill definition (its
  trigger, the steps, and metadata) and registers it with the skill store.
* :class:`~encre.learning.consolidator.MemoryConsolidator` -- periodically
  drives the agent's memory system to merge, deduplicate, and summarise
  stored memories so that long-running sessions do not grow without bound.

The public surface of the package is intentionally small: only the three
classes above are re-exported through ``__all__``. Downstream code should
import them from this package rather than from their concrete modules.
"""

from encre.improve.learning.consolidator import LearningConsolidator
from encre.improve.learning.engine import LearningEngine
from encre.improve.learning.skill_generator import SkillGenerator

__all__ = [
    "LearningEngine",
    "MemoryConsolidator",
    "SkillGenerator",
]
