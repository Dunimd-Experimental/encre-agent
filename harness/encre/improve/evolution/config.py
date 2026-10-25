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

"""Configuration object that wires together the evolution subsystems.

:class:`EvolutionConfig` is a plain dataclass holding the learner,
optimizer, reflex loop and metacognition components plus boolean flags that
enable or disable each one.  The :meth:`create_default` and
:meth:`create_disabled` factories build fully-wired or fully-disabled
instances respectively.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class EvolutionConfig:
    """Aggregate configuration for the evolution subsystem.

    Holds the four evolution components and a per-component enable flag so
    callers can selectively activate only the parts they need.
    """
    learner: Any = None
    optimizer: Any = None
    reflex: Any = None
    meta: Any = None
    reviewer: Any = None
    event_store: Any = None
    learner_enabled: bool = True
    optimizer_enabled: bool = True
    reflex_enabled: bool = True
    meta_enabled: bool = True
    reviewer_enabled: bool = True
    event_store_enabled: bool = True

    @classmethod
    def create_default(cls) -> "EvolutionConfig":
        """Build a fully-enabled config with components wired to disk storage."""
        from encre.config import get_data_dir
        from encre.improve.evolution.event_store import EventStore
        from encre.improve.evolution.learner import EncreEvolutionLearner
        from encre.improve.evolution.meta import EncreMetaCognition
        from encre.improve.evolution.optimizer import EncreStrategyOptimizer
        from encre.improve.evolution.reflex import EncreReflexLoop
        from encre.improve.evolution.reviewer import BackgroundReviewer

        return cls(
            learner=EncreEvolutionLearner(storage_path=str(get_data_dir() / "evolution" / "state.json")),
            optimizer=EncreStrategyOptimizer(),
            reflex=EncreReflexLoop(enabled=True),
            meta=EncreMetaCognition(),
            reviewer=BackgroundReviewer(),
            event_store=EventStore(),
        )

    @classmethod
    def create_disabled(cls) -> "EvolutionConfig":
        """Build a config where every evolution component is disabled."""
        from encre.config import get_data_dir
        from encre.improve.evolution.learner import EncreEvolutionLearner
        from encre.improve.evolution.meta import EncreMetaCognition
        from encre.improve.evolution.optimizer import EncreStrategyOptimizer
        from encre.improve.evolution.reflex import EncreReflexLoop

        return cls(
            learner=EncreEvolutionLearner(storage_path=str(get_data_dir() / "evolution" / "state.json")),
            optimizer=EncreStrategyOptimizer(),
            reflex=EncreReflexLoop(enabled=False),
            meta=EncreMetaCognition(),
            learner_enabled=False,
            optimizer_enabled=False,
            reflex_enabled=False,
            meta_enabled=False,
            reviewer_enabled=False,
            event_store_enabled=False,
        )


__all__ = ["EvolutionConfig"]
