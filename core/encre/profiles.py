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

"""Multiple-runtime ("profile") discovery and resolution.

Encre normally runs against a single data directory.  When *multiplexing* is
enabled the HTTP API serves several runtimes over one port and selects between
them by URL prefix; each runtime keeps its own sessions, credentials, and
workspace state.

Directory convention (kept intentionally simple and self-describing):

- The **default** profile is the data root itself, so a single-profile install
  sees exactly the same paths with or without this module.
- A **named** profile lives in ``<profiles_root>/<name>``.

``profiles_root`` defaults to ``<data_root>/profiles`` and can be relocated
with ``ENCRE_PROFILES_DIR``.  The active profile name comes from
``ENCRE_PROFILE`` and defaults to ``"default"``.

Names are validated because they become path segments; allowing ``..`` or a
separator would let a URL prefix escape the profiles root.
"""

import os
import re
from pathlib import Path

__all__ = [
    "DEFAULT_PROFILE_NAME",
    "get_active_profile_name",
    "set_active_profile_name",
    "get_profile_dir",
    "profiles_to_serve",
    "list_profile_names",
    "profile_exists",
    "validate_profile_name",
]

DEFAULT_PROFILE_NAME = "default"

_PROFILE_ENV = "ENCRE_PROFILE"
_PROFILES_ROOT_ENV = "ENCRE_PROFILES_DIR"

# A single path segment: leading alphanumeric, then alphanumerics plus
# dot/underscore/hyphen.  Explicitly rejects empty, ``.``, ``..``, and anything
# holding a path separator.
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _data_root() -> Path:
    """Return the process data root without forcing its creation."""
    from encre.paths import get_data_dir

    return Path(get_data_dir(ensure=False))


def _profiles_root() -> Path:
    """Return the directory that holds named profiles."""
    override = os.environ.get(_PROFILES_ROOT_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    return _data_root() / "profiles"


def validate_profile_name(name: str) -> str:
    """Validate and return a profile name suitable for use as a path segment.

    Args:
        name: The candidate profile name.

    Returns:
        The stripped, validated name.

    Raises:
        ValueError: If the name is empty, ``"default"`` is not special-cased by
            callers, contains a path separator, or fails the segment pattern.
    """
    candidate = (name or "").strip()
    if not candidate:
        raise ValueError("Profile name must be non-empty")
    if not _PROFILE_NAME_RE.match(candidate):
        raise ValueError(
            f"Invalid profile name {name!r}: must match "
            "[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
        )
    return candidate


def get_active_profile_name() -> str:
    """Return the active profile name from ``ENCRE_PROFILE`` (default profile)."""
    raw = (os.environ.get(_PROFILE_ENV) or "").strip()
    return raw or DEFAULT_PROFILE_NAME


def set_active_profile_name(name: str) -> None:
    """Set the active profile name in ``ENCRE_PROFILE`` for this process.

    Args:
        name: The profile name to activate.  ``"default"`` clears the override.
    """
    if name == DEFAULT_PROFILE_NAME:
        os.environ.pop(_PROFILE_ENV, None)
        return
    os.environ[_PROFILE_ENV] = validate_profile_name(name)


def get_profile_dir(profile: str | None = None, *, create: bool = False) -> Path:
    """Resolve the data directory for *profile*.

    Args:
        profile: Profile name, or ``None`` for the active profile.
        create: When ``True``, create the directory (named profiles only) before
            returning.

    Returns:
        The profile's data directory.  The default profile maps to the data
        root; a named profile maps to ``<profiles_root>/<name>``.
    """
    name = (profile or get_active_profile_name()).strip() or DEFAULT_PROFILE_NAME
    if name == DEFAULT_PROFILE_NAME:
        root = _data_root()
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root

    valid = validate_profile_name(name)
    directory = _profiles_root() / valid
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def list_profile_names() -> list[str]:
    """Return every available profile name, default first.

    Only directories whose names pass :func:`validate_profile_name` are
    considered profiles.
    """
    names = [DEFAULT_PROFILE_NAME]
    root = _profiles_root()
    try:
        if root.is_dir():
            for entry in sorted(root.iterdir(), key=lambda p: p.name):
                if entry.is_dir() and _PROFILE_NAME_RE.match(entry.name):
                    names.append(entry.name)
    except OSError:
        pass
    return names


def profile_exists(name: str) -> bool:
    """Return whether *name* is the default or an existing named profile dir."""
    candidate = (name or "").strip()
    if not candidate:
        return False
    if candidate == DEFAULT_PROFILE_NAME:
        return True
    if not _PROFILE_NAME_RE.match(candidate):
        return False
    return (_profiles_root() / candidate).is_dir()


def profiles_to_serve(multiplex: bool = False) -> list[tuple[str, Path]]:
    """Return the ``(name, data_dir)`` pairs the API should expose.

    Args:
        multiplex: When ``False``, only the active profile is returned.  When
            ``True``, the default profile plus every named profile directory is
            returned, with the active profile guaranteed to be present.

    Returns:
        A list of ``(profile_name, data_directory)`` tuples, default first.
    """
    if not multiplex:
        name = get_active_profile_name()
        return [(name, get_profile_dir(name))]

    pairs: list[tuple[str, Path]] = [(DEFAULT_PROFILE_NAME, _data_root())]
    seen = {DEFAULT_PROFILE_NAME}
    root = _profiles_root()
    try:
        if root.is_dir():
            for entry in sorted(root.iterdir(), key=lambda p: p.name):
                if entry.is_dir() and _PROFILE_NAME_RE.match(entry.name) and entry.name not in seen:
                    pairs.append((entry.name, entry))
                    seen.add(entry.name)
    except OSError:
        pass

    active = get_active_profile_name()
    if active not in seen:
        try:
            pairs.append((active, get_profile_dir(active)))
        except ValueError:
            pass
    return pairs
