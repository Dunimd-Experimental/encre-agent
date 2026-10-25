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

from encre.compact.loop_state.collapse import (
    COLLAPSE_CHUNK_SIZE,
    MAX_STAGED_COLLAPSES,
    MIN_MESSAGES_FOR_COLLAPSE,
    CollapseChunk,
    ContextCollapseState,
    collapse_old_tool_outputs,
    compute_collapse_boundaries,
    count_collapsed,
)
from encre.compact.loop_state.manager import StateManager
from encre.compact.loop_state.state import LoopTurnState
from encre.compact.loop_state.stop_hooks import (
    MAX_STOP_HOOK_BLOCKS,
    execute_stop_hooks,
    should_block_stop,
)
from encre.compact.loop_state.token_budget import (
    DEFAULT_TOKEN_BUDGET,
    TokenBudget,
    create_token_budget,
)
from encre.compact.loop_state.transition import (
    TurnTransition,
    TransitionRecord,
    TransitionHistory,
)

__all__ = [
    "CollapseChunk",
    "ContextCollapseState",
    "collapse_old_tool_outputs",
    "compute_collapse_boundaries",
    "count_collapsed",
    "COLLAPSE_CHUNK_SIZE",
    "MAX_STAGED_COLLAPSES",
    "MIN_MESSAGES_FOR_COLLAPSE",
    "LoopTurnState",
    "StateManager",
    "execute_stop_hooks",
    "should_block_stop",
    "MAX_STOP_HOOK_BLOCKS",
    "TokenBudget",
    "create_token_budget",
    "DEFAULT_TOKEN_BUDGET",
    "TurnTransition",
    "TransitionRecord",
    "TransitionHistory",
]
