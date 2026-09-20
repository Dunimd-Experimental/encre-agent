#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

"""Ordered, mode-filterable registry of the agent loop's phases (P1.1).

The registry is the swap point of the pluggable loop: the runner no longer
hard-codes the phase sequence, it iterates
:meth:`PhaseRegistry.pipeline`.  Registration order is execution order;
re-registering an existing name **replaces** the previous phase while
keeping its position (dict insertion-order semantics) -- that is the
documented customisation mechanism.  ``unregister`` removes a phase entirely
(making e.g. the secondary-tools or compaction phases optional) and returns
whether anything was removed, so removal is reversible and side-effect
clean.
"""

from __future__ import annotations

from encre.logging_config import get_logger
from encre.loop.phase_api import PhaseProtocol
from encre.modes.mode_profiles import AgentMode

logger = get_logger(__name__)


class PhaseRegistry:
    """Holds the loop's phases keyed by their unique ``name``.

    The backing ``dict`` preserves insertion order (execution order) and
    keeps the original position when a name is re-registered, so replacing a
    built-in phase never disturbs the rest of the pipeline.
    """

    def __init__(self) -> None:
        self._phases: dict[str, PhaseProtocol] = {}

    def register(self, phase: PhaseProtocol) -> None:
        """Register ``phase`` under its ``name``.

        Registering a name that already exists replaces the previous phase
        **in place** (same pipeline position).  This is the documented swap
        mechanism: embedders override e.g. ``"model"`` with their own phase
        by simply registering one with that name.

        Args:
            phase: The phase to register (anything satisfying
                :class:`~encre.loop.phase_api.PhaseProtocol`).

        Raises:
            ValueError: If the phase has no non-empty ``name``.
        """
        name = str(getattr(phase, "name", "") or "")
        if not name:
            raise ValueError("phase.name must be a non-empty string")
        replaced = name in self._phases
        self._phases[name] = phase
        if replaced:
            logger.info("[phase_registry] replaced phase %r in place", name)

    def unregister(self, name: str) -> bool:
        """Remove the phase registered under ``name``.

        Args:
            name: The registry key to remove.

        Returns:
            ``True`` if a phase was removed, ``False`` if the name was not
            registered (removing twice is a safe no-op).
        """
        removed = self._phases.pop(name, None) is not None
        if removed:
            logger.info("[phase_registry] unregistered phase %r", name)
        return removed

    def get(self, name: str) -> PhaseProtocol | None:
        """Return the phase registered under ``name`` (``None`` if absent)."""
        return self._phases.get(name)

    def pipeline(self, mode: AgentMode | None) -> list[PhaseProtocol]:
        """Return the phases active for ``mode``, in registration order.

        A phase with ``modes is None`` is active in every mode; otherwise it
        runs only when ``mode`` is a member of its ``modes`` frozenset.

        Args:
            mode: The loop's capability profile mode (``None`` keeps only
                the mode-agnostic phases).

        Returns:
            The ordered phase list for one pipeline execution.
        """
        return [
            phase for phase in self._phases.values()
            if phase.modes is None or mode in phase.modes
        ]

    def names(self) -> list[str]:
        """Return all registered phase names in registration order."""
        return list(self._phases.keys())
