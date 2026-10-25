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

"""Flat runtime overlay for general (UI) settings.

The settings panel of the Electron desktop renders a single flat form, and
the gateway reads a single flat dict, so this package presents general
settings as exactly that: a ``dict[str, Any]``. It does not define its own
storage. Every read and write is delegated to :mod:`encre.config_store`,
which owns the real on-disk layout at ``<data_dir>/config/ui.json`` plus the
per-group files it borrows from.

Splitting the concern matters because two independent stores writing the
same key is an unresolvable precedence race: whichever writes last wins at
runtime and the value silently disagrees with its own source of truth. The
store enforces one home per key; this module is only the shape-compatible
facade sitting on top of it.

Layer position: this is the UI-facing end of the settings path. The desktop
renderer and :class:`encre.gateway.run.GatewayRunner` call
:func:`load_settings` / :func:`save_settings` and never see a filesystem
path; the server-side config handlers go straight to
:mod:`encre.config_store` for typed, group-aware access.

Public API:
    :func:`load_settings` -- the flat settings view.
    :func:`save_settings` -- persist the flat overlay.
    :func:`is_general_setting` -- membership test against the overlay keys.
    :func:`load_custom_slash_commands` / :func:`save_custom_slash_commands`
        -- custom slash-command list helpers.
"""

import json
from typing import Any

from encre import config_store

#: Keys that have overlay semantics: the settings panel may change them at
#: runtime and the overlay value takes precedence over whatever was
#: persisted in the configuration files. Bound directly to the store's
#: canonical set rather than duplicated here -- the two definitions can then
#: never drift, and adding a key to the store automatically widens the
#: overlay.
_GENERAL_SETTINGS_KEYS = config_store.UI_SETTING_KEYS


def load_settings() -> dict[str, Any]:
    """Return the flat settings view.

    The shape is what every general-settings consumer expects from a single
    ``dict`` lookup:

    1. the UI overlay file itself, including non-schema keys such as
       ``custom_slash_commands`` and the ``gateway_*`` flags;
    2. flattened ``adapter_<platform>_<field>`` credentials from
       ``config/adapters.json`` -- the gateway resolves platform tokens from
       exactly these keys;
    3. ``permission_settings`` from ``config/runtime.json``.

    Items 2 and 3 are *borrowed*: their one real home is their group file,
    and they appear here only to keep the flat-dict contract intact. Saving
    a view produced by this function therefore does not store them, see
    :func:`save_settings`.

    Returns:
        A fresh dict. Mutating it has no effect on disk until it is passed
        back to :func:`save_settings`.
    """
    return config_store.overlay_view()


def save_settings(settings: dict[str, Any]) -> None:
    """Persist the flat overlay, leaving borrowed keys to their owners.

    A :func:`load_settings` value fed back in here carries adapter
    credentials and ``permission_settings`` as read-only passengers. Those
    are written from their typed models to ``config/adapters.json`` and
    ``config/runtime.json``, so this path filters them out rather than
    copying them a second time. Writing the copies would recreate the
    duplicate-store hazard this module exists to avoid.

    Keys with overlay semantics -- see :data:`_GENERAL_SETTINGS_KEYS` -- are
    always accepted.

    Args:
        settings: The flat settings dict to persist.

    Raises:
        Exception: Propagated from :mod:`encre.config_store` on I/O or
            permission failure; callers that must not abort on settings I/O
            wrap this themselves.
    """
    config_store.save_overlay(settings)


def is_general_setting(key: str) -> bool:
    """Return ``True`` when *key* is a general (overlay) setting.

    Callers use this to route a settings-panel write to the right owner: an
    overlay key belongs here, while an ``adapter_*`` key belongs to the
    gateway's adapter config and must not be written into the overlay.

    Args:
        key: A settings key as it appears in :func:`load_settings`.

    Returns:
        ``True`` if the key has overlay semantics.
    """
    return key in _GENERAL_SETTINGS_KEYS


def load_custom_slash_commands() -> list[dict]:
    """Return the user-defined slash commands.

    Stored under the ``custom_slash_commands`` key, which may hold either a
    JSON string (the shape the desktop persists) or an already-decoded list.
    The gateway and the CLI expand these at message parse time into extra
    command entries alongside the built-in ones.

    Every degraded shape maps to ``[]``: a missing key, a malformed JSON
    string, a non-list payload, or a failure reading settings. Slash-command
    loading runs on the message-handling path, so a bad value must never
    block a message.

    Returns:
        The command list, possibly empty. Each item is a dict carrying at
        least ``command`` and ``prompt``.
    """
    try:
        settings = load_settings()
        raw = settings.get("custom_slash_commands", "[]")
        if isinstance(raw, str):
            return json.loads(raw)
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def save_custom_slash_commands(commands: list[dict]) -> None:
    """Persist the user-defined slash commands.

    Serialised with ``ensure_ascii=False`` because commands frequently carry
    non-ASCII labels and trigger text; escaping them would make the stored
    key unsearchable and harder to inspect for an operator editing the file.

    Failures are swallowed on purpose: this is invoked from a UI submit
    handler, and raising out of it would surface as an unhandled error toast
    without telling the user that the in-memory commands are still live.

    Args:
        commands: The command list to store.
    """
    try:
        settings = load_settings()
        settings["custom_slash_commands"] = json.dumps(commands, ensure_ascii=False)
        save_settings(settings)
    except Exception:
        pass
