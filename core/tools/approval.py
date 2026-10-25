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

"""Gateway approval routing shared by platform adapters.

When the agent needs a human decision (terminal command, file write, etc.)
the turn blocks on an approval.  Adapters render platform-native buttons
and route the user's choice back through :func:`resolve_gateway_approval`.

This module owns the process-wide registry of pending approvals keyed by
session, plus the context-variable that tracks the *current* session so
the approval request knows where to deliver its prompt.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Pending-approval registry.
#
# All three dicts are indexed by ``session_key`` so a session that
# issues two approvals back-to-back (rare but possible with parallel
# tool calls) has a single pending slot per session. That matches the
# harness invariant that a session either has a live approval or does
# not; a session that tries to issue a second approval while the first
# is unresolved simply overwrites its previous state (the caller is
# expected to have unwound it first).
_notify_callbacks: dict[str, Any] = {}      # session_key -> notify callback (adapter-supplied; delivers the prompt)
_pending_events: dict[str, threading.Event] = {}  # session_key -> Event set when a decision arrives
_resolutions: dict[str, Any] = {}           # session_key -> resolved choice
_registry_lock = threading.Lock()

# The current approval session key for this execution context. ``contextvars``
# is used rather than a module global because approvals may run on many
# threads and asyncio tasks concurrently, and the contextvar propagates
# across ``await`` boundaries automatically.
_current_session_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "encre_current_session_key", default=None
)


def set_current_session_key(session_key: str) -> contextvars.Token:
    """Bind *session_key* as the current approval session.

    Callers must hold the returned :class:`contextvars.Token` and pass it
    back to :func:`reset_current_session_key` when the session scope
    exits, otherwise nested approval scopes would leak their key outward.

    Returns:
        A token that restores the previous binding when reset.
    """
    return _current_session_key.set(session_key)


def reset_current_session_key(token: contextvars.Token) -> None:
    """Restore the approval session key to its previous value.

    The token is reset within a ``try/except`` because contextvars tokens
    become invalid if the surrounding context was discarded (a common
    pattern with ``asyncio.Task`` boundaries); falling back to ``None``
    keeps the invariant that a stale binding never points at a live session.
    """
    try:
        _current_session_key.reset(token)
    except (ValueError, RuntimeError):
        _current_session_key.set(None)


def get_current_session_key() -> str | None:
    """Return the approval session key bound to the current context.

    Returns:
        The session key bound by :func:`set_current_session_key`, or
        ``None`` when no approval scope is active.
    """
    return _current_session_key.get()


def register_gateway_notify(session_key: str, callback: Any = None, **_: Any) -> None:
    """Register a pending approval for *session_key*.

    The call creates a fresh :class:`threading.Event` so the waiting
    coroutine has something to block on. Any stale resolution from a
    previous approval with the same session key is cleared so a new
    approval cannot accidentally pick up an old value.

    Args:
        session_key: The approval session identifier.
        callback: Optional adapter callable used to deliver the prompt
            to the platform. When ``None`` the caller is expected to
            deliver the prompt itself and simply want to block on the
            result.
    """
    with _registry_lock:
        _pending_events[session_key] = threading.Event()
        if callback is not None:
            _notify_callbacks[session_key] = callback
        _resolutions.pop(session_key, None)


def unregister_gateway_notify(session_key: str, **_: Any) -> None:
    """Drop any pending approval state for *session_key* (idempotent).

    Safe to call multiple times or on keys that were never registered —
    the wait side treats a missing event as "no pending approval" and
    returns ``None`` immediately.
    """
    with _registry_lock:
        _pending_events.pop(session_key, None)
        _notify_callbacks.pop(session_key, None)
        _resolutions.pop(session_key, None)


def resolve_gateway_approval(
    session_key: str,
    choice: Any,
    resolve_all: bool = False,
    **_: Any,
) -> bool:
    """Resolve the pending approval for *session_key* with *choice*.

    The resolution is written *before* the event is set so that a
    waiter that wakes on the event can read the value without a
    race window.

    Args:
        session_key: The approval session identifier.
        choice: The user's decision value.
        resolve_all: Kept for interface parity with older call sites;
            resolution is always single here.

    Returns:
        ``True`` if a pending approval was waiting and is now resolved;
        ``False`` when there was no pending approval for the session.
    """
    with _registry_lock:
        event = _pending_events.get(session_key)
        if event is None:
            return False
        _resolutions[session_key] = choice
        event.set()
    return True


def wait_for_gateway_approval(session_key: str, timeout: float | None = None) -> Any:
    """Block until a decision arrives for *session_key* or *timeout* elapses.

    The event is captured first, then the lock is released before
    ``wait`` — holding the lock across a potentially long wait would
    deadlock the resolver side, which also acquires the same lock.

    Args:
        session_key: The approval session identifier.
        timeout: Maximum seconds to block. ``None`` blocks forever.

    Returns:
        The resolved choice value, or ``None`` when no approval was
        pending or when the timeout elapsed without a resolution.
    """
    with _registry_lock:
        event = _pending_events.get(session_key)
    if event is None:
        return None
    event.wait(timeout=timeout)
    with _registry_lock:
        return _resolutions.get(session_key)


def has_blocking_approval(session_key: str | None = None, **_: Any) -> bool:
    """Return ``True`` if an approval is still pending for the session.

    Used by readiness/status endpoints to report that a session is
    waiting on a human decision. ``session_key=None`` falls back to the
    contextvar so unbound probes still report meaningful state.
    """
    key = session_key or get_current_session_key()
    if not key:
        return False
    with _registry_lock:
        event = _pending_events.get(key)
        return event is not None and not event.is_set()
