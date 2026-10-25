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

"""Lazy dependency installation for platform feature groups.

Platform adapters declare their third-party requirements as *feature
groups* (e.g. ``platform.matrix``).  :func:`feature_missing` reports
which packages in a group are not importable yet, and :func:`ensure` /
:func:`ensure_and_bind` install the group on demand and publish the
imported symbols back into the caller's module globals.

Deferring imports to call time is essential: the harness must remain
importable on a fresh Python interpreter that has only the core
requirements installed, and importing every platform library at module
load would crash startup on any machine missing one of the extras.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Feature group -> PyPI distribution names. Top-level importable module
# names are derived automatically by normalising the distribution name
# (see :func:`_module_name`).
FEATURE_PACKAGES: dict[str, list[str]] = {
    "platform.matrix": ["mautrix", "Markdown", "aiosqlite", "asyncpg", "aiohttp-socks"],
    "platform.slack": ["slack_bolt", "slack_sdk"],
    "platform.teams": ["microsoft-teams-apps"],
    "platform.telegram": ["python-telegram-bot"],
    "platform.discord": ["discord.py", "PyNaCl"],
    "platform.dingtalk": ["dingtalk-stream"],
    "platform.feishu": ["lark-oapi"],
    "platform.qqbot": ["cryptography", "qrcode", "pilk"],
    "platform.google_chat": ["google-api-python-client", "google-auth-oauthlib"],
    "platform.sms": ["twilio"],
}


def _module_name(distribution: str) -> str:
    """Map a distribution name to its importable top-level module name.

    Some distributions ship under a different import name than their
    PyPI name (``python-telegram-bot`` -> ``telegram``, ``discord.py``
    -> ``discord``). The alias table handles those; everything else
    falls back to the generic ``-`` -> ``_`` normalisation.

    Extras in brackets are stripped because ``foo[bar]`` refers to a
    distribution variant, not a separate import.
    """
    base = distribution.split("[", 1)[0]
    aliases = {
        "mautrix": "mautrix",
        "markdown": "markdown",
        "aiosqlite": "aiosqlite",
        "asyncpg": "asyncpg",
        "aiohttp-socks": "aiohttp_socks",
        "slack_bolt": "slack_bolt",
        "slack_sdk": "slack_sdk",
        "microsoft-teams-apps": "microsoft_teams",
        "python-telegram-bot": "telegram",
        "discord.py": "discord",
        "pynacl": "nacl",
        "dingtalk-stream": "dingtalk_stream",
        "lark-oapi": "lark_oapi",
        "qrcode": "qrcode",
        "pilk": "pilk",
        "twilio": "twilio",
        "cryptography": "cryptography",
        "google-api-python-client": "googleapiclient",
        "google-auth-oauthlib": "google_auth_oauthlib",
    }
    return aliases.get(base.lower(), base.replace("-", "_"))


def _is_available(distribution: str) -> bool:
    """Return ``True`` if the distribution's top-level module is importable.

    Uses :func:`importlib.util.find_spec` rather than a bare
    ``import`` so that checking availability does not execute the
    module's code — the module may have side effects (network setup,
    logging) that we do not want during a health probe.
    """
    try:
        return importlib.util.find_spec(_module_name(distribution)) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def feature_missing(feature: str) -> tuple[str, ...]:
    """Return the package specs in *feature* that are not yet installed.

    Returns an empty tuple when the feature group is unknown, so
    callers can treat both "everything installed" and "no such group"
    as ``True`` when checking ``not feature_missing(...)``.
    """
    specs = FEATURE_PACKAGES.get(feature)
    if not specs:
        return ()
    return tuple(spec for spec in specs if not _is_available(spec))


def ensure(feature: str, prompt: bool = True) -> bool:
    """Install every missing package in *feature*. Returns success.

    Runs ``pip install`` as a subprocess with stdout/stderr silenced so
    the user's terminal is not polluted by pip's progress bar during
    normal operation. Install failures are logged and reported as
    ``False`` rather than raised because callers already branch on
    success and do not expect exceptions from a feature probe.

    Args:
        feature: Feature group name (e.g. ``"platform.matrix"``).
        prompt: Kept for interface parity; installs are non-interactive
            here (the caller decides whether to prompt before invoking).

    Returns:
        ``True`` when the feature ends up importable.
    """
    missing = feature_missing(feature)
    if not missing:
        return True
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *missing],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("lazy_deps: failed to install %s: %s", feature, exc)
        return False
    return not feature_missing(feature)


def ensure_and_bind(
    feature: str,
    import_fn: Callable[[], dict[str, Any] | None],
    target_globals: dict[str, Any],
    prompt: bool = False,
) -> bool:
    """Ensure *feature* is importable, then bind imports into module globals.

    The import callback pattern (rather than passing a plain ``import``
    statement) lets adapter modules write:

        ensure_and_bind("platform.matrix", _do_import, globals())

    The mapping is merged into the caller's globals so the adapter can
    reference the imported symbols as if they had been imported at the
    top of the module.

    Args:
        feature: Feature group name.
        import_fn: Zero-arg callable returning a ``name -> object``
            mapping, or ``None`` to skip binding.
        target_globals: The caller's ``globals()`` to update on success.
        prompt: Kept for interface parity.

    Returns:
        ``True`` if the symbols were imported and bound.
    """
    if not ensure(feature, prompt=prompt):
        return False
    try:
        mapping = import_fn()
    except Exception as exc:
        logger.warning("lazy_deps: import callback for %s failed: %s", feature, exc)
        return False
    if mapping:
        target_globals.update(mapping)
    return True
