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

"""Evolution subsystem for Encre.

Brings self-improving behaviour to the agent:

* :mod:`encre.evolution.learner` -- records successful tool usages and
  errors, then supplies guidance for future calls.
* :mod:`encre.evolution.optimizer` -- learns per-tool strategy preferences.
* :mod:`encre.evolution.reflex` -- a reflexion-style self-correction loop.
* :mod:`encre.evolution.meta` -- metacognitive capability tracking.
* :mod:`encre.evolution.plan_do_review` -- hierarchical task planning.
* :mod:`encre.evolution.config` -- aggregate configuration object.

:class:`EvolutionConfig` wires the individual components together and
toggles each one on or off.
"""

from encre.improve.evolution.config import EvolutionConfig
from encre.improve.evolution.event_store import EventStore, StoredEvent
from encre.improve.evolution.learner import EncreEvolutionLearner, ErrorRecord, SuccessRecord
from encre.improve.evolution.meta import CapabilityProfile, EncreMetaCognition
from encre.improve.evolution.optimizer import EncreStrategyOptimizer, ToolStrategy
from encre.improve.evolution.plan_do_review import (
    PlanDoReviewEngine,
    ReviewGrade,
    RuntimePlan,
    StepNode,
    StepStatus,
)
from encre.improve.evolution.reflex import EncreReflexLoop, ReflexResult
from encre.improve.evolution.reviewer import BackgroundReviewer, ReviewSuggestion

__all__ = [
    "CapabilityProfile",
    "EncreEvolutionLearner",
    "EncreMetaCognition",
    "EncreReflexLoop",
    "EncreStrategyOptimizer",
    "ErrorRecord",
    "EvolutionConfig",
    "PlanDoReviewEngine",
    "ReflexResult",
    "ReviewGrade",
    "RuntimePlan",
    "StepNode",
    "StepStatus",
    "SuccessRecord",
    "ToolStrategy",
    "decompose_task",
    "should_plan",
]
