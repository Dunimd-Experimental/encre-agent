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

"""Task-local session variables for gateway turns.

A single process serves many concurrent conversations -- multiple HTTP API
requests, multiple chat platforms, and background approval flows.  Deeper
layers (tools, skills) occasionally need to know which conversation they are
running inside without threading a context object through every call.

:mod:`contextvars` is the right tool: values set on a task are visible to that
task and the tasks it spawns, and are invisible to siblings, so concurrent
turns never bleed into one another.

This module stores each field in its own :class:`contextvars.ContextVar`.
:func:`set_session_vars` returns one reset token per field; the caller passes
that list back to :func:`clear_session_vars` (or lets the list fall out of
scope) to restore the previous values.  Passing no tokens resets every field to
its default, which is the convenient path for a fresh task.
"""

import contextvars
from typing import Any

__all__ = [
    "set_session_vars",
    "clear_session_vars",
    "get_session_vars",
    "get_session_var",
    "get_platform",
    "get_chat_id",
    "get_session_key",
    "get_session_id",
    "is_async_delivery",
]

_platform: contextvars.ContextVar[str] = contextvars.ContextVar(
    "encre_session_platform", default=""
)
_chat_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "encre_session_chat_id", default=""
)
_session_key: contextvars.ContextVar[str] = contextvars.ContextVar(
    "encre_session_key", default=""
)
_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "encre_session_id", default=""
)
_async_delivery: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "encre_session_async_delivery", default=False
)
_extra: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "encre_session_extra", default={}
)

# The canonical fields, in the order their tokens are produced.  ``_extra`` is
# handled separately because it is a single mapping rather than a scalar.
_FIELD_VARS: tuple[tuple[str, contextvars.ContextVar[Any], Any], ...] = (
    ("platform", _platform, ""),
    ("chat_id", _chat_id, ""),
    ("session_key", _session_key, ""),
    ("session_id", _session_id, ""),
    ("async_delivery", _async_delivery, False),
)


def _as_str(value: Any) -> str:
    """Coerce an id-like value to a stripped string (``None`` -> ``""``)."""
    if value is None:
        return ""
    return str(value).strip()


def set_session_vars(
    platform: str = "",
    chat_id: str = "",
    session_key: str = "",
    session_id: str = "",
    async_delivery: bool = False,
    **extra: Any,
) -> list[contextvars.Token]:
    """Bind session variables for the current task.

    Args:
        platform: Platform name, e.g. ``"api_server"`` or ``"telegram"``.
        chat_id: Platform-native chat identifier.
        session_key: Gateway routing key for the conversation.
        session_id: Persistent session (transcript) identifier.
        async_delivery: Whether replies are delivered asynchronously.
        **extra: Additional ad-hoc fields merged into the extra mapping.

    Returns:
        A list of reset tokens, in the order the fields were set.  Pass it to
        :func:`clear_session_vars` to restore the prior values.
    """
    tokens: list[contextvars.Token] = []
    tokens.append(_platform.set(_as_str(platform)))
    tokens.append(_chat_id.set(_as_str(chat_id)))
    tokens.append(_session_key.set(_as_str(session_key)))
    tokens.append(_session_id.set(_as_str(session_id)))
    tokens.append(_async_delivery.set(bool(async_delivery)))
    if extra:
        merged = dict(_extra.get() or {})
        merged.update(extra)
        tokens.append(_extra.set(merged))
    return tokens


def clear_session_vars(tokens: list[contextvars.Token] | None = None) -> None:
    """Restore session variables.

    Args:
        tokens: The token list returned by :func:`set_session_vars`.  Tokens are
            reset in reverse order so nested binds unwind correctly.  When
            ``None`` or empty, every field is reset to its default, which is the
            right behaviour for a fresh task that never bound anything.
    """
    if tokens:
        for token in reversed(list(tokens)):
            _reset_token(token)
        return
    for _name, var, default in _FIELD_VARS:
        var.set(default)
    _extra.set({})


def _reset_token(token: Any) -> None:
    """Best-effort reset of a single token, tolerating cross-context misuse."""
    var = getattr(token, "var", None)
    if var is None:
        return
    try:
        var.reset(token)
    except (ValueError, RuntimeError):
        # Token created in a different context, or already reset; nothing safe
        # to do beyond leaving the current value in place.
        pass


def get_session_vars() -> dict[str, Any]:
    """Return a snapshot of all session variables as a mapping.

    Returns:
        A dict with ``platform``, ``chat_id``, ``session_key``, ``session_id``,
        ``async_delivery``, plus any extra fields that were bound.
    """
    snapshot: dict[str, Any] = {
        "platform": _platform.get(),
        "chat_id": _chat_id.get(),
        "session_key": _session_key.get(),
        "session_id": _session_id.get(),
        "async_delivery": _async_delivery.get(),
    }
    snapshot.update(_extra.get() or {})
    return snapshot


def get_session_var(key: str, default: Any = None) -> Any:
    """Return one session variable by name.

    Args:
        key: The field name (e.g. ``"session_id"``) or an extra field name.
        default: Returned when the field is unset or empty.

    Returns:
        The field's value, or *default*.
    """
    vars_map = get_session_vars()
    value = vars_map.get(key)
    if value is None or value == "":
        return default
    return value


def get_platform() -> str:
    """Return the current platform name (``""`` when unbound)."""
    return _platform.get()


def get_chat_id() -> str:
    """Return the current chat id (``""`` when unbound)."""
    return _chat_id.get()


def get_session_key() -> str:
    """Return the current session key (``""`` when unbound)."""
    return _session_key.get()


def get_session_id() -> str:
    """Return the current session id (``""`` when unbound)."""
    return _session_id.get()


def is_async_delivery() -> bool:
    """Return whether the current turn delivers replies asynchronously."""
    return bool(_async_delivery.get())
