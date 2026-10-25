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

"""Resolve the runtime workspace root for arkclaw / openclaw / other *claw* runtimes.

Every script in this skill is expected to operate against a single on-disk
"workspace" directory that holds ``evolution-data/``, sessions, and other
persisted state.  The directory differs per host installation, so this module
centralises the lookup and exposes it to the sibling scripts via
:func:`resolve_workspace_root`.

Resolution order (first match wins):

1. ``CLAW_WORKSPACE`` environment variable — an explicit, unambiguous override
   intended for CI and multi-tenant deployments.
2. A scan of ``~/`` for hidden directories named ``.*claw*`` that already
   contain a ``workspace`` subdirectory.  When several candidates exist the
   ranking prefers one that already has data (see :func:`_scan_home`).
3. ``~/.arkclaw/workspace`` — the production default for fresh installs; this
   path is returned even if it does not exist yet, so callers can safely create
   it.
"""

import os

# Exported so operators can inspect/override the configured knob in one place.
ENV_VAR = "CLAW_WORKSPACE"

# Final fallback used when neither the env var nor a discovered directory
# matched; intentionally not expanded here so it stays a short constant.
_FALLBACK = "~/.arkclaw/workspace"


def _scan_home():
    """Discover and rank ``~/.Xclaw/workspace`` candidates.

    A candidate is any hidden home-directory entry whose name both starts with
    ``.`` and contains ``claw`` (case-insensitive), *and* which already has a
    ``workspace`` subdirectory.  Returning ``None`` lets the caller fall back
    to :data:`_FALLBACK` without special-casing IO errors: if ``~`` is missing
    or unreadable we simply report "nothing found".

    Returns:
        Absolute path of the best candidate, or ``None`` if none was found.
    """
    home = os.path.expanduser("~")
    if not os.path.isdir(home):
        return None
    try:
        entries = os.listdir(home)
    except OSError:
        # Unreadable home is not fatal — the caller falls back to the default.
        return None
    found = []
    for name in entries:
        if not (name.startswith(".") and "claw" in name.lower()):
            continue
        ws = os.path.join(home, name, "workspace")
        if not os.path.isdir(ws):
            continue
        # ``has_data`` distinguishes "already in use" from "empty skeleton":
        # reusing an empty directory would silently lose no data, but picking
        # a directory that already holds evolution-data is always correct.
        has_data = os.path.isdir(os.path.join(ws, "evolution-data"))
        found.append((has_data, name, ws))
    if not found:
        return None

    def sort_key(item):
        has_data, name, _ws = item
        # Three-way runtime priority: arkclaw (primary product), openclaw
        # (legacy alias), anything else.  Lower number sorts first.
        priority = 0 if name == ".arkclaw" else (1 if name == ".openclaw" else 2)
        return (0 if has_data else 1, priority, name)

    found.sort(key=sort_key)
    return found[0][2]


def resolve_workspace_root():
    """Return the absolute path to the runtime workspace root.

    Combines the three resolution strategies documented at module level.  The
    environment override is always expanded with :func:`os.path.expanduser` so
    callers may supply ``~/...`` style paths without preprocessing.
    """
    override = os.environ.get(ENV_VAR)
    if override:
        return os.path.expanduser(override)
    found = _scan_home()
    if found:
        return found
    return os.path.expanduser(_FALLBACK)


def resolve_runtime_home():
    """Return the parent directory of the workspace (e.g. ``~/.arkclaw``).

    Runtime homes hold configuration files that sit one level above the
    workspace (for example ``config.yaml``), so sibling scripts use this to
    locate them without duplicating the resolution logic.
    """
    return os.path.dirname(resolve_workspace_root())


def claw_exclude_dirs():
    """Return the set of ``~/.X`` directory names containing ``claw``.

    Used by directory-walking helpers so they can skip runtime-managed
    locations and never recurse into the very directory they are about to
    scan.  ``.arkclaw`` and ``.openclaw`` are always included even when they
    do not currently exist, because they are the canonical paths that a future
    install would create.
    """
    home = os.path.expanduser("~")
    out = set()
    try:
        for name in os.listdir(home):
            if name.startswith(".") and "claw" in name.lower():
                out.add(name)
    except OSError:
        pass
    out.update({".arkclaw", ".openclaw"})
    return out


if __name__ == "__main__":
    # Diagnostic entry point: prints the resolved layout as JSON so an operator
    # can confirm which branch of the resolution order was taken.
    import json

    print(
        json.dumps(
            {
                "workspace_root": resolve_workspace_root(),
                "runtime_home": resolve_runtime_home(),
                "claw_exclude_dirs": sorted(claw_exclude_dirs()),
                "env_var": ENV_VAR,
            },
            indent=2,
        )
    )
