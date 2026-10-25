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

"""Credential validation utilities.

The harness has many places where a user-supplied value is compared against
the shape of a real secret before it is sent to an upstream service: when
loading ``config.yaml``, when picking up a token from an environment
variable, and when a CLI subcommand echoes a masked preview. Rather than
each of those sites inventing its own rule, they call
:func:`has_usable_secret` here and share one definition of "looks like a
real secret".

A value is considered usable when it passes four cheap heuristics:

1. It is a non-empty ``str`` (``None`` and non-string values are rejected
   immediately — a common source of subtle bugs otherwise).
2. Its length is at least ``min_length`` characters (default 16). This
   keeps the check from mistaking a short demo string such as ``sk-test``
   for a real credential.
3. It does not match a well-known placeholder pattern. The placeholder
   regex covers the strings people copy-paste from documentation
   (``changeme``, ``your-api-key``, ``sk-1234``, ``xxxxxxxx``, etc.) so
   that a config template can be loaded without crashing the app or,
   worse, being sent to a real API as-is.
4. It contains at least one digit or one non-alphanumeric character. A
   20-character string of pure lowercase letters is almost never a real
   API key, so this cheap character-class check filters out random
   prose.

The function is intentionally permissive — it will accept strings that
later turn out to be wrong (a misconfigured but plausibly-shaped secret
passes every check). The purpose is only to reject the obviously fake
ones so callers can produce a helpful error message instead of a
confusing ``401 Unauthorized`` from a remote service.
"""

from __future__ import annotations

import re

# Case-insensitive regex that matches well-known placeholder strings that
# appear in tutorials, .env.example files, and framework templates. The
# match is anchored at both ends and strips surrounding whitespace first
# (in the caller), so ``changeme!`` in a config is treated the same as
# ``changeme`` — trailing punctuation would otherwise let it slip through.
# The ``sk-[a-f0-9]{4}`` branch catches the OpenAI-style demo prefix, and
# the trailing ``[a-f0-9]{8}`` branch is a defensive backstop for any
# hex-looking stub that is not already covered.
_PLACEHOLDER_RE = re.compile(
    r"(?i)^"
    r"(?:changeme|change_me|changeme!|changeme123|"
    r"api[_-]?key|apikey|your[_-]?api[_-]?key|"
    r"test|testing|example|placeholder|dummy|sample|"
    r"x{4,}|-+$|\*+$|_+$|"
    r"secret|token|your[_-]?token|"
    r"encre[_-]?key|your[_-]?key|"
    r"sk-[a-f0-9]{4}|"
    r"[a-f0-9]{8})$"
)


def has_usable_secret(value: str | None, min_length: int = 16) -> bool:
    """Return ``True`` if *value* looks like a real secret, not a placeholder.

    See the module docstring for the full rationale and design choices.
    The four checks run in order of increasing cost so the common
    failures (``None``, empty, too short) short-circuit before the regex
    match is attempted.

    Args:
        value: The candidate secret. ``None`` and non-``str`` values are
            rejected silently rather than raising, because callers often
            pass the value straight out of a config dict that may omit
            the key entirely.
        min_length: Minimum accepted string length. Kept as a parameter
            rather than a module constant so stricter call sites (for
            example, an OpenAI API key which is 51+ characters) can pass
            a higher bound without forking this function.

    Returns:
        ``True`` when all four heuristics pass; ``False`` otherwise.
        Callers should treat ``False`` as "the user did not configure a
        real credential" and produce a friendly error message, not as
        "the credential is invalid" — the latter would imply the user
        typed something wrong when in fact they may have never filled
        in the config at all.
    """
    if not value or not isinstance(value, str):
        return False
    if len(value) < min_length:
        return False
    if _PLACEHOLDER_RE.match(value.strip()):
        return False
    has_digit = any(c.isdigit() for c in value)
    has_special = any(not c.isalnum() for c in value)
    return has_digit or has_special
