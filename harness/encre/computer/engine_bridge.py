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

"""Bridge between :class:`EncreBrowserSession` and the agent's event
stream, used to surface *engine install* / *driver provisioning*
prompts to the user (via the desktop frontend) **without** involving
the LLM in the choice.

The LLM-driven flow goes like this:

1. The model calls a browser action (``navigate`` / ``click`` / ...).
2. :class:`EncreComputerUseSession.dispatch` calls into
   :class:`EncreBrowserSession`, which calls ``_ensure_browser``.
3. ``_ensure_browser`` sees the bundled chromium binary is missing.
4. Instead of raising an error that the LLM would have to read, it
   calls :class:`EngineRequester.request_install` -- the requester
   yields an :class:`EngineInstallRequest` event into the agent
   stream and **suspends the calling coroutine** until the user
   responds (or 5 min elapse).
5. The agent's ``run()`` forwards the event over the WebSocket.
6. The Electron renderer pops ``Dialog.confirmInstall`` and posts
   back an :class:`EngineInstallResponse` action.
7. The WebSocket handler resolves the requester's future, the
   browser session continues, and the LLM only ever sees the
   *eventual* success/failure of the original browser action.

Headless / server contexts that don't have a requester wired up
get the original "Playwright not installed" ``RuntimeError`` --
back-compat is preserved for any non-desktop deployment.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from encre.utils.types import (
    EngineInstallProgress,
    EngineInstallRequest,
)

logger = logging.getLogger(__name__)


#: Maximum time to wait for the user to pick an option.  When this
#: expires the request resolves to ``"cancelled"`` so the calling
#: tool returns a clear "user did not respond" failure rather than
#: hanging forever.
DEFAULT_REQUEST_TIMEOUT_S = 300.0


@dataclass
class _Pending:
    """Internal record for one outstanding engine-install request.

    Holds the display text (and i18n codes), the resolvable future the
    calling coroutine awaits, and an optional progress callback.
    """
    request_id: str
    engine: str
    title: str
    body: str
    hint: str
    options: list[dict[str, Any]]
    future: asyncio.Future
    created_at: float
    on_progress: Any | None = None  # callable: (EngineInstallProgress) -> None
    # I18n: message code keys the frontend resolves via t()
    title_code: str = ""
    title_args: dict[str, str] = field(default_factory=dict)
    body_code: str = ""
    body_args: dict[str, str] = field(default_factory=dict)
    hint_code: str = ""
    hint_args: dict[str, str] = field(default_factory=dict)


class EngineRequester:
    """Awaits the user's choice on an engine install prompt.

    Constructed once per agent run, then handed to every browser /
    desktop session via :meth:`EncreBrowserSession.set_engine_requester`.
    The agent's ``run()`` calls :meth:`drain` on every loop tick to
    forward pending requests to the frontend.
    """

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        on_request: Any | None = None,
        on_progress: Any | None = None,
        emit: Any | None = None,
    ) -> None:
        """Configure timeout and the request/progress/emit hook callbacks.

        Args:
            timeout_s: Seconds to wait for the user before cancelling.
            on_request: Optional hook called synchronously on each new request.
            on_progress: Optional default progress callback.
            emit: Optional coroutine/callable to push events to the frontend.
        """
        self._timeout_s = timeout_s
        self._pending: dict[str, _Pending] = {}
        self._on_request = on_request
        self._on_progress = on_progress
        # Cache the last request_id so progress events can still be
        # emitted after the _pending entry is cleaned up (the progress
        # happens AFTER the user picks an option, e.g. during engine
        # download).
        self._last_request_id: str | None = None
        # Optional immediate-emit hook.  When set, the requester
        # will call ``emit(event)`` the moment a new request is
        # created, *before* the calling coroutine starts awaiting
        # the future.  This is what makes the desktop dialog pop
        # up promptly even when the agent's main event loop is
        # blocked on the tool call.  Typically set by the
        # WebSocket router to its own ``_send_event`` method.
        self._emit: Any | None = emit
        self._closed = False

    def set_emit(self, emit: Any | None) -> None:
        """Install (or replace) the immediate-emit hook."""
        self._emit = emit

    async def __call__(
        self,
        *,
        engine: str,
        title: str,
        body: str,
        hint: str = "",
        options: list[dict[str, Any]] | None = None,
        on_progress: Any | None = None,
        title_code: str = "",
        title_args: dict[str, str] | None = None,
        body_code: str = "",
        body_args: dict[str, str] | None = None,
        hint_code: str = "",
        hint_args: dict[str, str] | None = None,
    ) -> str:
        """Make ``EngineRequester`` itself an async callable so it can
        be used directly as the ``_engine_requester`` hook on
        :class:`EncreBrowserSession`.

        Delegates to :meth:`request_install`.
        """
        return await self.request_install(
            engine=engine,
            title=title,
            body=body,
            hint=hint,
            options=options,
            on_progress=on_progress,
            title_code=title_code,
            title_args=title_args,
            body_code=body_code,
            body_args=body_args,
            hint_code=hint_code,
            hint_args=hint_args,
        )

    @property
    def pending(self) -> dict[str, _Pending]:
        """Return the mapping of request_id -> outstanding pending request."""
        return self._pending

    async def request_install(
        self,
        *,
        engine: str,
        title: str,
        body: str,
        hint: str = "",
        options: list[dict[str, Any]] | None = None,
        on_progress: Any | None = None,
        title_code: str = "",
        title_args: dict[str, str] | None = None,
        body_code: str = "",
        body_args: dict[str, str] | None = None,
        hint_code: str = "",
        hint_args: dict[str, str] | None = None,
    ) -> str:
        """Block until the user (or timeout) picks an option.

        Returns the option ``id`` (e.g. ``"download"``, ``"local-edge"``,
        ``"webdriver"``), or ``"cancelled"`` if the user dismisses
        the prompt or the timeout expires.
        """
        if self._closed:
            return "cancelled"
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending = _Pending(
            request_id=request_id,
            engine=engine,
            title=title,
            body=body,
            hint=hint,
            options=list(options or []),
            future=fut,
            created_at=time.time(),
            on_progress=on_progress,
            title_code=title_code,
            title_args=title_args or {},
            body_code=body_code,
            body_args=body_args or {},
            hint_code=hint_code,
            hint_args=hint_args or {},
        )
        self._pending[request_id] = pending
        if self._on_request is not None:
            try:
                self._on_request(pending)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("engine on_request hook raised: %s", exc)
        # Emit the event IMMEDIATELY so the desktop dialog can
        # pop up even when the agent's main loop is blocked on
        # the tool call.  This is the difference between
        # "dialog appears the moment the tool needs an engine"
        # and "dialog appears 30 s later when the LLM streams
        # its next tool_progress event".
        if self._emit is not None:
            try:
                evt = self.make_event(request_id)
                if evt is not None:
                    result = self._emit(evt)
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("engine emit hook raised: %s", exc)
        try:
            choice = await asyncio.wait_for(fut, timeout=self._timeout_s)
        except TimeoutError:
            logger.info(
                "engine install request %s timed out after %.0fs",
                request_id, self._timeout_s,
            )
            return "cancelled"
        except asyncio.CancelledError:
            return "cancelled"
        finally:
            self._last_request_id = request_id
            self._pending.pop(request_id, None)
        if not isinstance(choice, str):
            return "cancelled"
        return choice

    def resolve(self, request_id: str, choice: str) -> bool:
        """Resolve a pending request.  Returns True if a request was
        actually resolved (i.e. the id was known)."""
        pending = self._pending.get(request_id)
        if pending is None:
            return False
        if pending.future.done():
            return False
        pending.future.set_result(choice)
        return True

    def cancel_all(self, reason: str = "cancelled") -> int:
        """Cancel every pending request (used on agent shutdown)."""
        n = 0
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(reason)
                n += 1
        self._pending.clear()
        return n

    def close(self) -> None:
        """Mark the requester closed and cancel all pending requests."""
        self._closed = True
        self.cancel_all()

    def make_event(self, request_id: str) -> EngineInstallRequest | None:
        """Build an :class:`EngineInstallRequest` event for a pending request."""
        pending = self._pending.get(request_id)
        if pending is None:
            return None
        return EngineInstallRequest(
            request_id=pending.request_id,
            engine=pending.engine,
            title=pending.title,
            body=pending.body,
            hint=pending.hint,
            options=list(pending.options),
            title_code=pending.title_code,
            title_args=dict(pending.title_args),
            body_code=pending.body_code,
            body_args=dict(pending.body_args),
            hint_code=pending.hint_code,
            hint_args=dict(pending.hint_args),
        )

    async def drain(self) -> AsyncIterator[EngineInstallRequest]:
        """Yield :class:`EngineInstallRequest` events for every
        pending request.  The agent's run() should call this on
        every loop tick; the request_id field is what the frontend
        echoes back in its response action.
        """
        for pending in list(self._pending.values()):
            evt = self.make_event(pending.request_id)
            if evt is not None:
                yield evt

    def progress(
        self,
        request_id: str,
        *,
        pct: float,
        message: str,
        sub_message: str = "",
        indeterminate: bool = False,
        status: str = "running",
    ) -> EngineInstallProgress | None:
        """Build an :class:`EngineInstallProgress` event for a
        pending request.  Callers can yield this from the agent
        stream to update the progress bar in the frontend.
        """
        if request_id not in self._pending:
            return None
        return EngineInstallProgress(
            request_id=request_id,
            pct=pct,
            message=message,
            sub_message=sub_message,
            indeterminate=indeterminate,
            status=status,
        )


__all__ = [
    "DEFAULT_REQUEST_TIMEOUT_S",
    "EngineRequester",
]
