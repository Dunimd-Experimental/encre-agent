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

"""Slash-command confirmation routing.

Interactive slash commands can ask the user to confirm an action via
a platform-native button. Adapters call :func:`resolve` when a button
is pressed; the registered waiter receives the choice and may return
a follow-up message to post in the same channel.

The registry is intentionally process-wide and not scoped per session
because a confirmation prompt is uniquely identified by ``confirm_id``
— the session is only used for log correlation in :func:`resolve`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Pending confirmations keyed by ``confirm_id``.
#
# Two dicts are kept in lockstep: the waiters dict holds the Future
# awaiting the choice, and the resolvers dict holds the (optional)
# callable the caller registered to produce a follow-up message.
_waiters: dict[str, "asyncio.Future[Any]"] = {}
_resolvers: dict[str, Any] = {}


def register(confirm_id: str, resolver: Any = None) -> "asyncio.Future[Any]":
    """Register a pending confirmation and return the future to await.

    A new Future is created against the running event loop so the
    caller can ``await`` the returned object directly. Overwriting a
    previously registered entry with the same ``confirm_id`` is a
    caller bug — the stale Future is leaked — but no cleanup is
    attempted because the code path where ``confirm_id`` collides
    with a live registration would already have a caller that
    needs attention.

    Args:
        confirm_id: Unique identifier for the confirmation prompt.
        resolver: Optional callable invoked with the user's choice.
            When the callable returns a coroutine, the coroutine is
            awaited so async resolvers are supported.

    Returns:
        An :class:`asyncio.Future` that resolves to the user's choice
        when :func:`resolve` is called with the matching
        ``confirm_id``.
    """
    loop = asyncio.get_event_loop()
    future: "asyncio.Future[Any]" = loop.create_future()
    _waiters[confirm_id] = future
    if resolver is not None:
        _resolvers[confirm_id] = resolver
    return future


async def resolve(session_key: str, confirm_id: str, choice: Any) -> str | None:
    """Resolve a pending confirmation with *choice*.

    The waiters and resolvers dicts are popped *before* the future is
    resolved so a rapid double-click from the platform does not
    re-resolve the same confirmation (the second call finds an empty
    dict and just logs). The resolver's return value is awaited if
    it is a coroutine, allowing async resolvers to fetch resources
    before producing the follow-up message.

    Args:
        session_key: The session that owns the confirmation (for
            logging only — routing is by ``confirm_id``).
        confirm_id: Identifier of the confirmation prompt.
        choice: The user's chosen value.

    Returns:
        An optional follow-up message string to post in the channel,
        or ``None`` when the resolver returned no message.
    """
    future = _waiters.pop(confirm_id, None)
    resolver = _resolvers.pop(confirm_id, None)
    if future is not None and not future.done():
        future.set_result(choice)
    if resolver is not None:
        result = resolver(choice)
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return str(result)
    logger.debug("slash_confirm: resolved %s (session=%s, choice=%r)", confirm_id, session_key, choice)
    return None


def cancel(confirm_id: str) -> None:
    """Cancel a pending confirmation without a choice.

    Called when the confirmation prompt is torn down (session ended,
    message expired) so the waiting coroutine unwinds cleanly rather
    than blocking forever.
    """
    future = _waiters.pop(confirm_id, None)
    _resolvers.pop(confirm_id, None)
    if future is not None and not future.done():
        future.cancel()
