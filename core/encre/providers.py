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

"""Human-readable provider labels.

Configuration stores providers by their stable slug (``deepseek``,
``azure-openai``, ...).  Users should see a friendly name (``DeepSeek``,
``Azure OpenAI``).  The authoritative slug -> label mapping lives in the
harness provider catalog; this module surfaces it to core code that only has a
provider id in hand.

When the catalog cannot be imported, or an unknown slug is passed, a
best-effort Title-Case fallback keeps the UI readable instead of leaking a raw
slug.
"""

import re
from typing import Any

__all__ = ["get_label", "provider_label", "known_provider_labels"]

# Tokens that should be shouted rather than title-cased.
_ACRONYMS = {
    "ai",
    "api",
    "gpt",
    "glm",
    "llm",
    "mcp",
    "oss",
    "ui",
    "url",
}

# A token that is numeric or a version-ish blob (``4``, ``4o``, ``3.5``,
# ``v1``) is preserved verbatim rather than title-cased.
_VERSION_RE = re.compile(r"^v?\d[\d.]*[a-z]{0,3}$")


def _load_catalog_labels() -> dict[str, str]:
    """Build ``{lowercased_provider_id: label}`` from the harness catalog.

    Returns an empty mapping when the catalog cannot be imported, so callers
    degrade to :func:`_pretty_slug` instead of failing.
    """
    labels: dict[str, str] = {}
    try:
        from encre.backends.catalog import PROVIDERS  # type: ignore
    except Exception:
        return labels
    for entry in PROVIDERS or ():
        if not isinstance(entry, dict):
            continue
        provider_id = str(entry.get("id") or "").strip()
        label = str(entry.get("label") or "").strip()
        if provider_id and label:
            labels[provider_id.lower()] = label
    return labels


_LABELS = _load_catalog_labels()


def known_provider_labels() -> dict[str, str]:
    """Return a copy of the catalog-backed ``id -> label`` mapping."""
    return dict(_LABELS)


def _pretty_slug(slug: str) -> str:
    """Title-case an unknown provider/model slug for display."""
    tokens = re.split(r"[-_/]+", slug.strip())
    rendered: list[str] = []
    for token in tokens:
        if not token:
            continue
        lower = token.lower()
        if lower in _ACRONYMS:
            rendered.append(token.upper())
        elif _VERSION_RE.match(token):
            rendered.append(token)
        else:
            rendered.append(token[:1].upper() + token[1:])
    return " ".join(rendered) or slug.strip()


def get_label(provider: Any) -> str:
    """Return the display label for a provider slug.

    Args:
        provider: A provider id such as ``"deepseek"``.  ``None`` or an empty
            value yields ``""``.

    Returns:
        The catalog label when known, otherwise a Title-Cased fallback.
    """
    if provider is None:
        return ""
    key = str(provider).strip()
    if not key:
        return ""
    label = _LABELS.get(key.lower())
    if label:
        return label
    return _pretty_slug(key)


def provider_label(provider: Any) -> str:
    """Alias for :func:`get_label` for call sites that read better this way."""
    return get_label(provider)
