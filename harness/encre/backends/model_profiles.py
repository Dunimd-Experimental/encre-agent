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

"""Per-model behaviour profiles (model capability tiering).

Encre aggregates models from many providers, so the fleet is heterogeneous:
frontier models thrive under the full harness behaviour while small/weak
models drown in the same 14k+ token system prompt and 80-tool surface.
:class:`ModeProfile` tunes *session mode*; this module tunes *per model* --
the two are orthogonal and compose (fan-out takes the stricter of the two).

A :class:`ModelProfile` is resolved once per model identity from:

1. an explicit user override -- the active model's ``capabilities["tier"]``
   (users know their own model; this always wins);
2. :class:`~encre.backends.registry.ModelInfo` capability signals
   (no tool calling, tiny context window -> light);
3. otherwise ``standard`` -- i.e. exactly today's behaviour.

Name-marker heuristics were removed deliberately: marker vocabularies
(``-mini``/``-flash``/``7b``...) are per-vendor naming accidents, not
capability facts -- a frontier flagship can carry a light-sounding name
(``deepseek-flash``) while a genuinely small model may not, and any static
vocabulary goes stale as vendors rename their tiers.

Design constraints (mirrors :mod:`encre.backends.adaptive`): resolution
**never raises** and unknown models never degrade -- a mis-tiered unknown
model would silently change behaviour for every user of an aggregating
gateway, so the fail-open default is the historical full behaviour.

Profiles only change *behavioural load* (prompt weight, parallel fan-out).
The tool-visibility filter (``core_tool_patterns``) applies only when the
user explicitly tiers the model via ``capabilities.tier = "weak"``;
heuristic light tiers (name markers, small context) keep the full visible
registry.  Profiles never gate tool *execution*: a model that still manages
to call a hidden tool succeeds (registration IS activation).
"""


import fnmatch
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from encre.backends.registry import ModelInfo


class ModelTier(str, Enum):
    """Behavioural tier of one model within the aggregated fleet."""

    STRONG = "strong"
    STANDARD = "standard"
    LIGHT = "light"


# Heavy behavioural blocks removed for light models (block names as used by
# EncrePromptBuilder).  The kept-core is deliberately: identity,
# mandatory_constraints, task_completion, tool_catalog, tool_usage, safety,
# environment, permission, language -- the rules even the weakest model must
# not lose.
_LIGHT_DROP_BLOCKS: tuple[str, ...] = (
    "memory_discipline",
    "task_management",
    "post_execution_validation",
    "slash_commands",
    "skills",
)

# Blocks whose full text is replaced by ``blocks/slim_<name>.prompt`` for
# light models (same position in the assembly, much shorter text).
_LIGHT_SLIM_BLOCKS: tuple[str, ...] = (
    "tool_execution",
    "output_format",
)

# Tools a light model still sees.  fnmatch patterns over the active tool
# names; everything else stays registered and executable, it is just not
# listed in the payload / catalog.  ``manage`` keeps discovery reachable so
# the hidden surface remains one tool call away even for a light model.
_LIGHT_CORE_TOOL_PATTERNS: tuple[str, ...] = (
    "file_read",
    "file_write",
    "file_edit",
    "bash",
    "web_search",
    "manage",
    "memory_*",
)

# Short behavioural preamble for light models: forces the single-most
# important compensations (one step at a time, short answers, only listed
# tools).
_LIGHT_PROMPT_GAIN = (
    "WORK STYLE: resolve one step at a time. Call a single tool when the next "
    "step depends on its result; batch only lookups that are truly independent, "
    "and if a batch leaves you unsure which result answers which call, go back "
    "to one call per step. Prefer the tools listed in Available Tools. Keep "
    "answers short and concrete. Never describe an action instead of performing "
    "it."
)

# Context windows at or below this size cannot comfortably carry the full
# prompt + large tool surface -> light.
_SMALL_CONTEXT_WINDOW = 32768


@dataclass(frozen=True)
class ModelProfile:
    """Resolved per-model behavioural load settings.

    All defaults equal the pre-profile (full) behaviour, so a standard-tier
    resolution can never change what the harness sends to a model.

    Attributes:
        tier: The resolved behavioural tier.
        drop_prompt_blocks: System-prompt block names removed entirely.
        slim_prompt_blocks: Block names replaced by their ``slim_*`` text.
        prompt_gain: Short preamble injected for this tier ("" = none).
        core_tool_patterns: When non-empty, only tools matching these fnmatch
            patterns remain visible in the payload / catalog.
        max_visible_tools: Hard cap on visible tools (None = no cap),
            applied after the core-pattern filter.
        parallel_fan_out: Per-model cap on parallel tool fan-out, applied as
            ``min(ModeProfile.parallel_fan_out, this)`` (None = no cap).
        reasons: Signal name -> explanation for each degradation applied;
            audit trail mirroring ``CapabilityPlan.reasons``.
    """

    tier: ModelTier = ModelTier.STANDARD
    drop_prompt_blocks: tuple[str, ...] = ()
    slim_prompt_blocks: tuple[str, ...] = ()
    prompt_gain: str = ""
    core_tool_patterns: tuple[str, ...] = ()
    max_visible_tools: int | None = None
    parallel_fan_out: int | None = None
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def is_light(self) -> bool:
        return self.tier is ModelTier.LIGHT

    def filter_tool_names(self, names: list[str]) -> list[str]:
        """Restrict the visible tool names according to this profile.

        A no-op (returns the input list unchanged) when the profile carries
        no visibility restriction, so standard/strong models keep the
        full registered surface.
        """
        if not self.core_tool_patterns and self.max_visible_tools is None:
            return names
        if self.core_tool_patterns:
            kept = [n for n in names if any(fnmatch.fnmatch(n, pat) for pat in self.core_tool_patterns)]
        else:
            kept = list(names)
        if self.max_visible_tools is not None:
            kept = kept[: self.max_visible_tools]
        # A model with no matching tool at all is a config mistake, not a
        # reason to strand it: fall back to the unrestricted list.
        return kept or names

    def fan_out_for(self, mode_fan_out: int) -> int:
        """Combine with the mode profile's fan-out (stricter wins)."""
        if self.parallel_fan_out is None:
            return mode_fan_out
        return max(1, min(mode_fan_out, self.parallel_fan_out))

    @property
    def cache_key(self) -> tuple[Any, ...]:
        """Stable identity for payload/prompt caches."""
        return (
            self.tier.value,
            self.core_tool_patterns,
            self.max_visible_tools,
            self.parallel_fan_out,
            self.drop_prompt_blocks,
            self.slim_prompt_blocks,
            self.prompt_gain,
        )


# Canonical instances ---------------------------------------------------------

STANDARD_PROFILE = ModelProfile(tier=ModelTier.STANDARD)

LIGHT_PROFILE = ModelProfile(
    tier=ModelTier.LIGHT,
    drop_prompt_blocks=_LIGHT_DROP_BLOCKS,
    slim_prompt_blocks=_LIGHT_SLIM_BLOCKS,
    prompt_gain=_LIGHT_PROMPT_GAIN,
    core_tool_patterns=_LIGHT_CORE_TOOL_PATTERNS,
    parallel_fan_out=1,
)

# Strong tier currently behaves exactly like standard (full load); it exists
# as the anchor for future amplifications (deeper thinking budgets, richer
# delegation prompts) resolved through the same plumbing.
STRONG_PROFILE = ModelProfile(tier=ModelTier.STRONG)

# Heuristic-only light tier: keeps the prompt-load reductions but drops the
# tool-visibility filter.  The core-pattern filter hides every plugin tool
# (browser, ea-tool-*, MCP contributions) from otherwise capable setups whose
# model name merely carries a heuristic marker or whose configured context
# window is small -- silently stripping tools from the payload contradicts
# the "registration IS activation" contract.  The full core-pattern filter
# only applies when the user explicitly tiers the model via
# ``capabilities.tier = "weak"``.
_LIGHT_HEURISTIC_PROFILE = replace(LIGHT_PROFILE, core_tool_patterns=())

_TIER_PROFILES: dict[ModelTier, ModelProfile] = {
    ModelTier.STRONG: STRONG_PROFILE,
    ModelTier.STANDARD: STANDARD_PROFILE,
    ModelTier.LIGHT: LIGHT_PROFILE,
}


def _merge_reasons(profile: ModelProfile, reasons: dict[str, str]) -> ModelProfile:
    """Return *profile* carrying *reasons* (frozen dataclass copy)."""
    if not reasons:
        return profile
    return ModelProfile(
        tier=profile.tier,
        drop_prompt_blocks=profile.drop_prompt_blocks,
        slim_prompt_blocks=profile.slim_prompt_blocks,
        prompt_gain=profile.prompt_gain,
        core_tool_patterns=profile.core_tool_patterns,
        max_visible_tools=profile.max_visible_tools,
        parallel_fan_out=profile.parallel_fan_out,
        reasons={**profile.reasons, **reasons},
    )


def _tier_from_capabilities(capabilities: dict[str, Any] | None) -> ModelTier | None:
    """Read the explicit user override from a model's capabilities dict.

    ``capabilities["tier"]`` accepts strong/standard/light (aliases: full,
    default, weak).  Invalid values are ignored (heuristics decide).
    """
    if not capabilities:
        return None
    raw = str(capabilities.get("tier", "") or "").strip().lower()
    if not raw:
        return None
    aliases = {
        "full": "standard",
        "default": "standard",
        "weak": "light",
        "small": "light",
    }
    raw = aliases.get(raw, raw)
    try:
        return ModelTier(raw)
    except ValueError:
        return None


def resolve_model_profile(
    model_info: ModelInfo | None,
    *,
    model_id: str = "",  # noqa: ARG001 - name heuristics removed; call-surface symmetry
    backend_type: str = "",  # noqa: ARG001 - unused today; call-surface symmetry
    capabilities: dict[str, Any] | None = None,
) -> ModelProfile:
    """Resolve the behavioural :class:`ModelProfile` for one model.

    Args:
        model_info: Registry metadata for the model, or ``None`` when the
            model is unknown (never degrades behaviour on this alone).
        model_id: Raw configured/exposed model name.  No longer used for
            tiering: name-marker vocabularies are per-vendor naming
            accidents, not capability facts.  Kept for call-surface
            compatibility.
        backend_type: Backend type key (unused today; kept for signal
            symmetry with ``compute_plan``).
        capabilities: Active model config capabilities dict; a
            ``tier`` entry is an explicit user override and always wins.

    Returns:
        A :class:`ModelProfile`.  This function never raises.
    """
    try:
        reasons: dict[str, str] = {}

        explicit = _tier_from_capabilities(capabilities)
        if explicit is not None:
            reasons["tier"] = f"explicit capabilities.tier={explicit.value}"
            return _merge_reasons(_TIER_PROFILES[explicit], reasons)

        # Objective signals only -- no name probing.  Tier resolves from
        # explicit user configuration, the measurable context window, and
        # the hard supports_tools fact.  Unknown models never degrade.
        tier = ModelTier.STANDARD

        if model_info is not None:
            ctx = int(getattr(model_info, "context_window", 0) or 0)
            if 0 < ctx <= _SMALL_CONTEXT_WINDOW:
                reasons["context"] = f"context window {ctx} <= {_SMALL_CONTEXT_WINDOW}"
                tier = ModelTier.LIGHT
            if not bool(getattr(model_info, "supports_tools", True)):
                # No native tool calling: the heavy tool-discipline blocks
                # are pure load -- adaptive.py already swaps in the text
                # tool-intent fallback for this model.
                reasons["tools"] = "model does not support tool calling"
                tier = ModelTier.LIGHT

        if tier is ModelTier.LIGHT:
            # Explicit user tiering keeps the full light behaviour (core
            # tool filter included); heuristic-only signals never shrink
            # the visible tool surface.
            if explicit is not None:
                return _merge_reasons(LIGHT_PROFILE, reasons)
            return _merge_reasons(_LIGHT_HEURISTIC_PROFILE, reasons)
        if tier is ModelTier.STRONG:
            return _merge_reasons(STRONG_PROFILE, reasons)
        return STANDARD_PROFILE
    except Exception:
        # Resolution must never break a turn.
        return STANDARD_PROFILE


__all__ = [
    "LIGHT_PROFILE",
    "STANDARD_PROFILE",
    "STRONG_PROFILE",
    "ModelProfile",
    "ModelTier",
    "resolve_model_profile",
]
