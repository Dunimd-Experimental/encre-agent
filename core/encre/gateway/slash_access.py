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

"""Canonical normalisation of access-control id lists.

Gateway adapters receive the same logical allowlist -- a set of platform user
ids, chat ids, or role ids -- through config in several shapes.  A
``config.yaml`` fragment may carry it as a comma-separated string, a JSON string
the frontend round-tripped, a YAML list, a scalar, or a mapping keyed by id.
Every gate (slash-command authorisation, exec-approval admin lists, per-channel
allowlists) must agree on what that value means, otherwise a user can be
authorised in one code path and denied in another for the same config.

This module is the single place that collapses those shapes.  It is
deliberately dependency-free and side-effect-free so it can be imported from
adapter import time without pulling in the gateway runtime.
"""

import re
from typing import Any, Iterable

__all__ = [
    "_coerce_id_list",
    "coerce_id_list",
    "is_id_allowed",
]

# A stored list may be written with commas, semicolons, or whitespace.
_ID_SPLIT_RE = re.compile(r"[,\s;]+")

# Guard against a malformed config that nests containers deeply (or points at
# an unbounded iterator) wedging adapter startup.  Well-formed lists are tiny;
# anything near these caps is a configuration error, so we stop rather than
# expand.
_MAX_DEPTH = 4
_MAX_IDS = 100_000


def _coerce_id_list(value: Any) -> set[str]:
    """Normalise any stored access list to a set of non-empty string ids.

    The accepted shapes cover every producer in the codebase:

    - ``None`` / empty string -> empty set (a disabled allowlist).
    - A string -> split on commas, semicolons, and whitespace.  This absorbs
      the ``"a,b"`` form the CLI writes, the ``"a b"`` form a YAML flow scalar
      produces, and a JSON string that decoded to a bare scalar.
    - ``bytes`` -> decoded as UTF-8, then treated as a string.
    - ``bool`` -> ignored (``True``/``False`` are toggles, never ids).
    - Integers (and integral floats) -> their decimal string.  Platform ids are
      sometimes numeric in YAML, and ``123`` must match the ``"123"`` that
      arrives from an API payload.
    - A mapping -> its *keys* are treated as ids.  Config allowlists are
      conventionally written as ``{user_id: {...}}``; keying off the mapping
      body would silently drop the id.
    - Any other iterable (list/tuple/set/generator) -> each element is coerced
      recursively, so nested lists and mixed-type lists flatten correctly.

    Whitespace around each id is stripped and empties are dropped, so a trailing
    comma never produces a phantom ``""`` member that could match an empty user
    id at the gate.

    Args:
        value: The stored access-list value in any accepted shape.

    Returns:
        A new :class:`set` of non-empty, stripped string ids.  Never the input
        object, so callers may mutate the result freely.
    """
    result: set[str] = set()
    _collect_ids(value, result, 0)
    return result


def _collect_ids(value: Any, out: set[str], depth: int) -> None:
    """Recursive worker for :func:`_coerce_id_list` sharing one output set."""
    if value is None or len(out) >= _MAX_IDS:
        return
    if depth > _MAX_DEPTH:
        # Too deeply nested to be a real allowlist; coerce the leaf verbatim so
        # a pathological value is still represented instead of silently lost.
        text = str(value).strip()
        if text:
            out.add(text)
        return

    if isinstance(value, str):
        for part in _ID_SPLIT_RE.split(value):
            part = part.strip()
            if part:
                out.add(part)
                if len(out) >= _MAX_IDS:
                    return
        return

    if isinstance(value, (bytes, bytearray)):
        try:
            _collect_ids(bytes(value).decode("utf-8"), out, depth + 1)
        except Exception:
            pass
        return

    # ``bool`` is a subclass of ``int`` -- exclude it before the int branch.
    if isinstance(value, bool):
        return

    if isinstance(value, int):
        out.add(str(value))
        return

    if isinstance(value, float):
        out.add(str(int(value)) if value.is_integer() else str(value))
        return

    if isinstance(value, dict):
        for key in value.keys():
            _collect_ids(key, out, depth + 1)
            if len(out) >= _MAX_IDS:
                return
        return

    if isinstance(value, Iterable):
        for item in value:
            _collect_ids(item, out, depth + 1)
            if len(out) >= _MAX_IDS:
                return
        return

    text = str(value).strip()
    if text:
        out.add(text)


def coerce_id_list(value: Any) -> set[str]:
    """Public alias for :func:`_coerce_id_list` for new call sites.

    The underscored name is kept because it is the historical import used by
    adapters; this alias exists so new code does not have to import a private
    symbol.
    """
    return _coerce_id_list(value)


def is_id_allowed(candidate: Any, allowed: Any) -> bool:
    """Return whether *candidate* is present in the *allowed* access list.

    ``allowed`` may be any shape :func:`_coerce_id_list` accepts; an already
    built ``set``/``frozenset`` is used directly to avoid re-normalising on
    every message.  Comparison is done on the stripped string form so ``123``
    from YAML matches ``"123"`` from an API payload.

    Args:
        candidate: The id to test (user id, chat id, role id, ...).
        allowed: The allowlist in any accepted stored shape.

    Returns:
        ``True`` when *candidate* is non-empty and present in the allowlist.
    """
    if candidate is None:
        return False
    needle = str(candidate).strip()
    if not needle:
        return False
    if isinstance(allowed, (set, frozenset)):
        return needle in allowed
    return needle in _coerce_id_list(allowed)
