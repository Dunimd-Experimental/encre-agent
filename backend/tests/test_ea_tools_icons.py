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

"""Icon contract for the official vendor packages (core/ea_tools/*).

Every official package declares its own glyph in ``[tool.ea] icon`` — a file it
ships (``assets/icon.svg``) or a lucide name — because the desktop renders
exactly what the package states and substitutes nothing.  These checks keep that
declaration honest at the source: a dangling path or a malformed value shows up
as an empty tile for the user, and nothing in the runtime says why.

Skipped when the source tree is absent (a packaged install ships installed
plugin directories, not ``core/ea_tools``).
"""

import re
from pathlib import Path

import pytest

EA_TOOLS = Path(__file__).resolve().parents[2] / "core" / "ea_tools"
IMAGE_SUFFIXES = {".svg", ".png", ".webp", ".gif", ".jpg", ".jpeg", ".ico"}
LUCIDE_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

_candidates = list(EA_TOOLS.iterdir()) if EA_TOOLS.is_dir() else []
packages = sorted(p for p in _candidates if p.is_dir() and (p / "pyproject.toml").is_file())

pytestmark = pytest.mark.skipif(not packages, reason="no official EA packages in this checkout")


def _icon_value(pkg: Path) -> str:
    """The single ``icon`` value declared under ``[tool.ea]``."""
    text = (pkg / "pyproject.toml").read_text(encoding="utf-8")
    found = re.findall(r'^icon = "(.*)"$', text, re.MULTILINE)
    assert len(found) == 1, f"{pkg.name}: expected one [tool.ea] icon, got {found}"
    return found[0]


@pytest.mark.parametrize("pkg", packages, ids=lambda p: p.name)
def test_verify_package_declares_resolvable_icon(pkg):
    """A declared icon is either a shipped image that exists or a lucide name."""
    value = _icon_value(pkg)
    assert value, f"{pkg.name}: icon is empty"
    if "/" in value or Path(value).suffix.lower() in IMAGE_SUFFIXES:
        target = pkg / value
        assert target.is_file(), f"{pkg.name}: declared icon file missing: {value}"
        assert target.suffix.lower() in IMAGE_SUFFIXES, f"{pkg.name}: icon file is not an image: {value}"
        return
    assert LUCIDE_NAME.match(value), f"{pkg.name}: icon is neither a path nor a lucide name: {value}"


def test_verify_no_unreferenced_icon_files():
    """No package ships an ``assets/icon.svg`` it does not declare."""
    declared = {pkg.name for pkg in packages if _icon_value(pkg) == "assets/icon.svg"}
    shipped = [pkg for pkg in packages if (pkg / "assets" / "icon.svg").is_file()]
    orphans = sorted(pkg.name for pkg in shipped if pkg.name not in declared)
    assert not orphans, f"packages shipping an undeclared assets/icon.svg: {orphans}"
