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

"""Token budget auto-continue: keep the agent running until a budget
target is met.

Mirrors Claude Code's ``+500k`` token budget feature in
``src/query/tokenBudget.ts``.  Users specify a target (e.g. "+500k")
and the loop automatically continues until the cumulative output
token count reaches that target.
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Default budget: 0 means no budget tracking (unlimited)
DEFAULT_TOKEN_BUDGET = 0


@dataclass
class TokenBudget:
    """Tracks cumulative output token usage against a target."""

    target: int = 0
    used: int = 0

    @property
    def is_active(self) -> bool:
        return self.target > 0

    @property
    def remaining(self) -> int:
        if not self.is_active:
            return 0
        return max(0, self.target - self.used)

    @property
    def is_exhausted(self) -> bool:
        return self.is_active and self.remaining <= 0

    def add_usage(self, tokens: int) -> None:
        """Record tokens consumed in the current turn."""
        self.used += tokens
        if self.is_active:
            logger.debug(
                "[budget] used=%d/%d remaining=%d",
                self.used, self.target, self.remaining,
            )

    def should_continue(self, tokens_last_turn: int) -> bool:
        """Return True if the loop should continue for another turn.

        Continues if:
        - Budget is active and not yet exhausted
        - The last turn produced output (model is still working)
        """
        if not self.is_active:
            return False
        if self.is_exhausted:
            return False
        if tokens_last_turn <= 0:
            # No output — model stopped on its own
            return False
        return True

    def build_budget_hint(self) -> str:
        """Build a system message hint about the remaining budget."""
        if not self.is_active:
            return ""
        return (
            f"[Token budget: {self.remaining:,} tokens remaining "
            f"({self.used:,} used of {self.target:,})]"
        )

    def reset(self) -> None:
        self.used = 0


def create_token_budget(target: int = 0) -> TokenBudget:
    """Create a token budget tracker.

    *target* is the total output token budget.  Use 0 for unlimited.
    """
    return TokenBudget(target=target)
