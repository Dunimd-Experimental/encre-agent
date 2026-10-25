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

"""Auto-discovery helper for bundled EA tool packages.

Discovery is directory-driven: the :mod:`encre.plugins.ea_scan` scanner walks
``ea_tools/`` and makes every ``ea-tool-*`` package importable (prepending its
directory to ``sys.path`` when needed) — in development and inside PyInstaller
frozen bundles alike.  No pip install step is required and no package name is
hardcoded here.
"""
from __future__ import annotations

from encre.plugins.ea_scan import iter_ea_packages


def ensure_ea_tools_installed() -> dict[str, bool]:
    """Make every bundled EA package importable, without importing it.

    Returns dict mapping module_name → importable (bool).  Scanning the
    ``ea_tools/`` tree appends each package directory to ``sys.path`` (see
    :func:`encre.plugins.ea_scan._ensure_importable`); that is all this
    function needs to do.

    It used to import every package here, and ``discover_all`` imported the
    same modules again to build their plugin factories — two full passes over
    hundreds of GIL-bound modules before the UI could settle. Discovery owns
    the single import pass, and on a warm cache it imports nothing at all
    (see :mod:`encre.plugins.discovery_cache`).
    """
    from encre.plugins.ea_scan import _ensure_importable

    results: dict[str, bool] = {}
    for pkg in iter_ea_packages():
        # Directory/sys.path probe only; never executes package code.
        results[pkg.module_name] = bool(_ensure_importable(pkg.module_name, pkg.pkg_dir))
    return results


# Kept for backward compatibility with callers that expect a module-level
# MANDATORY_PACKAGES mapping.  The authoritative source is the directory scan.
def mandatory_packages() -> dict[str, str]:
    """Return module → tier for every bundled mandatory EA package."""
    return {
        pkg.module_name: pkg.tier
        for pkg in iter_ea_packages()
        if pkg.tier == "mandatory"
    }
