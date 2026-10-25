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

"""
Telegram Bot API chat-identifier parsing and normalization helpers.

Telegram's Bot API accepts a ``chat_id`` in two distinct forms:

- **Numeric ID**: an ``int`` value. Private DMs use positive integers (e.g.
  ``123456789``), while channels and supergroups use negative integers with
  the ``-100`` prefix (e.g. ``-1001234567890``).
- **Username string**: an ``@username`` handle for public channels and
  public groups (e.g. ``@encre_bot``).

Earlier adapter code coerced every ``chat_id`` with ``int()``, which raises
``ValueError`` on the username form and silently crashes when a user types
``@somechannel`` into a delivery target. Normalizing at this module boundary
lets numeric IDs pass through as ``int`` while usernames pass through
unchanged as ``str``, and both are valid Bot API values.

This module is imported by :class:`ea_connector_telegram.adapter`
and provides:

Public API:
    - :func:`normalize_telegram_chat_id` -- coerces a raw chat-id value to
      either ``int`` (numeric) or ``str`` (username), never raising on valid
      input.
    - :func:`telegram_chat_id_key` -- returns a stable string key suitable
      for dict keys and persisted session state.
    - :func:`looks_like_telegram_username` -- returns ``True`` when a value
      matches the ``@username`` pattern (5-32 chars).
    - :func:`parse_telegram_username_target` -- extracts the ``@username``
      value from a delivery-target reference, or ``None`` if the value is
      not a username.

All functions are pure and thread-safe; they perform no I/O and hold no
module-level mutable state.
"""
import re
from typing import Any, Union

# Matches Telegram bot and user @username handles. Telegram permits
# 4-32 characters for some legacy/official accounts, but the public
# minimum for bot accounts is 5 characters (ending in "bot"). The 4-32
# range is used here as a safe lower bound for routing decisions that
# must not reject valid usernames. Only ASCII letters, digits, and
# underscores are valid in Telegram handles.
_TELEGRAM_USERNAME_RE = re.compile(r"@[A-Za-z0-9_]{4,32}")


def normalize_telegram_chat_id(chat_id: Any) -> Union[int, str]:
    """Coerce a raw chat-id value to a Bot API-compatible form.

    Numeric values (including negative channel/supergroup IDs) are returned
    as ``int``. Any non-numeric value (e.g. an ``@username``) is returned
    as a stripped ``str``. The Bot API accepts both forms, so this function
    never raises on a valid username the way a bare ``int(chat_id)`` call
    would.

    Args:
        chat_id: A chat identifier in any accepted form -- an ``int``, a
            numeric string (``"123456"`` or ``"-100123456"``), or an
            ``@username`` string (``"@somechannel"``). May also be ``None``
            or other types; these are stringified and returned as-is.

    Returns:
        An ``int`` for numeric IDs, or a stripped ``str`` for usernames and
        any other non-numeric values.
    """
    chat_id_str = str(chat_id).strip()
    try:
        return int(chat_id_str)
    except (TypeError, ValueError):
        return chat_id_str


def telegram_chat_id_key(chat_id: Any) -> str:
    """Return a stable string key for a chat_id.

    The Bot API returns numeric IDs as ``int`` and usernames as ``str``,
    but Python dicts and persisted session state require a consistent key
    type. This function normalizes first (via :func:`normalize_telegram_chat_id`)
    and then stringifies, so ``123456``, ``"123456"``, and ``123456.0`` all
    produce the same key ``"123456"``.

    Args:
        chat_id: Any chat identifier form accepted by the Bot API.

    Returns:
        A string suitable for use as a dictionary key or persisted state
        identifier. Never ``None``.
    """
    return str(normalize_telegram_chat_id(chat_id))


def looks_like_telegram_username(chat_id: Any) -> bool:
    """Return ``True`` when the value matches the ``@username`` pattern.

    This is a structural check against the regex in
    ``_TELEGRAM_USERNAME_RE``. It does not verify that the username is
    registered with Telegram; it only confirms the value has the shape of
    a valid handle.

    Args:
        chat_id: A value to test (typically a string, but any type is
            coerced via ``str()``).

    Returns:
        ``True`` if the stripped string value matches ``@[A-Za-z0-9_]{4,32}``,
        ``False`` otherwise.
    """
    return bool(_TELEGRAM_USERNAME_RE.fullmatch(str(chat_id).strip()))


def parse_telegram_username_target(target_ref: Any) -> Union[str, None]:
    """Extract an ``@username`` from a delivery-target reference.

    Delivery targets in Encre's message-routing layer may be specified as
    either a numeric chat ID or an ``@username``. This function returns
    the ``@username`` value when the reference is in username form, or
    ``None`` when it is a numeric ID or any other non-username value.

    Args:
        target_ref: A delivery-target value (typically a string, but any
            type is coerced via ``str()``).

    Returns:
        The stripped ``@username`` string if the value matches the username
        pattern, or ``None`` otherwise.
    """
    value = str(target_ref).strip()
    return value if looks_like_telegram_username(value) else None
