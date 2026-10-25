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

"""Skill enumeration helper for the OpenAI-compatible API surface.

Exposes the loaded skill catalogue in the shape the ``/v1/skills``
endpoint returns. Delegates to the harness skill registry when
importable.

The import of the skill registry is deferred to call time so this
module can be imported from the API layer even in environments where
the full harness is not installed (e.g. minimal test harnesses).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _coerce_entry(skill: Any) -> dict[str, Any]:
    """Normalise a skill object into a JSON-serialisable dict.

    Handles three shapes the registry is known to return:

    1. A plain dict — copied as-is, only the ``object`` field is
       defaulted so callers always see an OpenAI-shaped envelope.
    2. An object with ``name``/``id``/``description`` attributes —
       the common shape of a loaded ``Skill`` class instance.
    3. Anything else — stringified as the ``id`` fallback so the
       endpoint still produces valid JSON rather than raising.

    The ``enabled`` field defaults to ``True`` (inverting the
    ``disabled`` attribute) so a skill object that omits both fields
    is treated as visible.
    """
    if isinstance(skill, dict):
        entry = dict(skill)
    else:
        entry = {
            "id": getattr(skill, "name", None)
            or getattr(skill, "id", None)
            or str(skill),
            "name": getattr(skill, "name", None),
            "description": getattr(skill, "description", None),
            "enabled": not getattr(skill, "disabled", False),
        }
    entry.setdefault("object", "skill")
    return entry


def _find_all_skills(skip_disabled: bool = False, workspace_path: str | None = None) -> list[dict[str, Any]]:
    """Return every known skill as a list of dicts.

    The registry import is wrapped in a broad ``except`` because this
    is called from a network endpoint — a broken registry (e.g. an
    in-progress plugin install) should produce an empty list rather
    than a 500 response.

    Args:
        skip_disabled: Drop disabled skills from the result when
            ``True``. Default ``False`` because the endpoint is
            expected to expose the full catalogue by default and let
            the client filter.
        workspace_path: Optional workspace to scope project skills
            to. Retained for interface parity; the shared registry
            currently resolves workspace scope itself.

    Returns:
        A list of normalised skill dictionaries (empty when unavailable).
    """
    try:
        from encre.skills.registry import get_shared_skill_registry

        registry = get_shared_skill_registry()
        raw = registry.list_skills() if hasattr(registry, "list_skills") else []
        skills = [_coerce_entry(s) for s in raw]
    except Exception as exc:  # pragma: no cover - registry optional at runtime
        logger.debug("skills_tool: no skill registry available: %s", exc)
        return []
    if skip_disabled:
        skills = [s for s in skills if s.get("enabled", True)]
    return skills


def _sort_skills(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return *skills* sorted by id/name for stable API output.

    Stable sort matters for the API contract: clients may diff
    successive responses and see spurious moves if the registry's
    iteration order shifts. Non-list inputs are treated as empty
    because a partially-initialised registry occasionally returns
    ``None``.
    """
    if not isinstance(skills, list):
        return []

    def _key(item: dict[str, Any]) -> str:
        return str(item.get("id") or item.get("name") or "")

    return sorted(skills, key=_key)
