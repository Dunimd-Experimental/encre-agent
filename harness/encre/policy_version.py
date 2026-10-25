#!/usr/bin/env python3

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

"""Policy version registry: the single home for shipped policy artefacts.

Every telemetry record, loop event line and benchmark run row is stamped
with :data:`POLICY_VERSIONS` (human-readable versions) plus
:data:`POLICY_VERSIONS_HASH` (content fingerprint) so behaviour changes in
the safety-relevant policy artefacts are traceable in persisted data.

The four tracked artefacts and what their content hash covers:

* ``permission_rules``   -- repr of the shipped dataclass defaults
  (:class:`encre.config.PermissionRule`, :class:`encre.config.NetworkPolicyConfig`,
  :class:`encre.config.PermissionsConfig`), i.e. the built-in rule semantics
  a fresh install starts from (user config is runtime data, not policy).
* ``system_prompt``      -- the assembly policy constants of
  :mod:`encre.context_compiler` (Task 6 converged system-prompt assembly
  into that module): section priority, budget ratios, steering/truncation
  tunables.
* ``classifier_prompt``  -- the text of the two autosafety classifier
  prompts (:mod:`encre.prompts.loader` category ``autosafety``), the same
  files :class:`encre.autosafety.EncreAutoSafetyClassifier` loads.
* ``compaction_params``  -- the inline tunables of the unified compression
  pipeline (budget -> collapse -> microcompact -> snip -> autocompact ->
  milestone) in ``encre/loop/phases/compact.py``.  There are no named
  constants at the call sites, so the canonical values are mirrored here;
  keep this mapping in sync when editing those call sites.

:data:`POLICY_VERSIONS_HASH` is the sha256 (first 12 hex chars) of the
newline-joined ``key<US>content`` canonical text over all four artefacts,
computed once at module import.  Every content extractor is failure-safe:
an artefact that cannot be read hashes as empty text (deterministic), so a
broken environment degrades to a stable value instead of crashing imports.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

__all__ = [
    "POLICY_VERSIONS",
    "POLICY_VERSIONS_HASH",
    "policy_versions_stamped",
]

#: Human-readable version per policy artefact.  Bump when an artefact
#: changes intentionally; the content hash catches accidental drift.
POLICY_VERSIONS: dict[str, str] = {
    "permission_rules": "1.0.0",
    "system_prompt": "1.0.0",
    "classifier_prompt": "1.0.0",
    "compaction_params": "1.0.0",
}

#: Separator between the artefact key and its canonical content (unit
#: separator -- never appears in the hashed texts).
_KEY_SEP = "\x1f"
#: Separator between artefact entries in the canonical text.
_ENTRY_SEP = "\n"


def _permission_rules_canon() -> str:
    """Canonical text of the shipped permission-rule defaults."""
    from encre.config import NetworkPolicyConfig, PermissionRule, PermissionsConfig
    return repr((PermissionRule(), NetworkPolicyConfig(), PermissionsConfig()))


def _system_prompt_canon() -> str:
    """Canonical text of the system-prompt assembly policy (Task 6 form)."""
    from encre.context_compiler import (
        DEFAULT_TOTAL_BUDGET_TOKENS,
        MIN_COMPILE_BUDGET_TOKENS,
        SECTION_BUDGET_RATIOS,
        STEERING_INJECTION_BUDGET_TOKENS,
        STEERING_MESSAGE_MIN_BUDGET_TOKENS,
        TRUNCATION_MARKER,
        ContextSection,
    )
    canon: dict[str, Any] = {
        "section_priority": {section.name: int(section) for section in ContextSection},
        "budget_ratios": {
            section.name: SECTION_BUDGET_RATIOS[section] for section in ContextSection
        },
        "default_total_budget_tokens": DEFAULT_TOTAL_BUDGET_TOKENS,
        "min_compile_budget_tokens": MIN_COMPILE_BUDGET_TOKENS,
        "steering_injection_budget_tokens": STEERING_INJECTION_BUDGET_TOKENS,
        "steering_message_min_budget_tokens": STEERING_MESSAGE_MIN_BUDGET_TOKENS,
        "truncation_marker": TRUNCATION_MARKER,
    }
    return repr(canon)


def _classifier_prompt_canon() -> str:
    """Canonical text of the shipped autosafety classifier prompts."""
    from encre.prompts.loader import PromptLoader
    loader = PromptLoader()
    parts: list[str] = []
    for name in ("safety_classifier", "safety_context_eval"):
        try:
            parts.append(loader.load(name, category="autosafety") or "")
        except Exception:
            parts.append("")  # deterministic fallback: absent artefact
    return "\n\x1e\n".join(parts)


def _compaction_params_canon() -> str:
    """Canonical text of the compaction pipeline tunables.

    Mirrors the inline values in ``encre/loop/phases/compact.py`` (same
    source to maintain by hand -- there are no named constants there).
    """
    canon: dict[str, Any] = {
        "pipeline_stages": [
            "budget", "collapse", "microcompact", "snip", "autocompact",
            "milestone",
        ],
        "autocompact_min_headroom_tokens": 4000,
        "autocompact_headroom_ratio": 0.15,
        "keep_window_recent_turns": 8,
    }
    return repr(canon)


#: Content extractors per artefact, in stable (sorted) order at hash time.
_CANON_BUILDERS: dict[str, Callable[[], str]] = {
    "permission_rules": _permission_rules_canon,
    "system_prompt": _system_prompt_canon,
    "classifier_prompt": _classifier_prompt_canon,
    "compaction_params": _compaction_params_canon,
}


def _canonical_text() -> str:
    """Build the hash input: one ``key<US>content`` line per artefact."""
    lines: list[str] = []
    for key in sorted(_CANON_BUILDERS):
        try:
            content = _CANON_BUILDERS[key]()
        except Exception:
            content = ""  # deterministic fallback: unreadable artefact
        lines.append(f"{key}{_KEY_SEP}{content}")
    return _ENTRY_SEP.join(lines)


def _content_hash(text: str) -> str:
    """sha256 of *text*, first 12 hex characters."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


#: Content fingerprint of the four policy artefacts (sha256, first 12 hex
#: chars of the canonical text).  Computed once at module import.
POLICY_VERSIONS_HASH: str = _content_hash(_canonical_text())


def policy_versions_stamped() -> dict[str, str]:
    """Return a fresh :data:`POLICY_VERSIONS` copy with the hash included.

    Callers embedding the registry into persisted records get a private
    dict each time, so in-place consumers (e.g. the telemetry secret
    redactor) can never mutate shared state.
    """
    stamped = dict(POLICY_VERSIONS)
    stamped["hash"] = POLICY_VERSIONS_HASH
    return stamped
