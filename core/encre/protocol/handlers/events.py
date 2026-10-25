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

"""Agent-event to WebSocket-frame bridge.

This is the producer half of the protocol: where the other modules in this
package all consume client frames, :class:`EventDispatchMixin` consumes agent
events produced by the Encre Agent Core loop and turns each one into a server frame.
It is composed last into :class:`encre.transport.ws.EncreWSHandler` because it
is not a client-message domain and therefore has no entry in ``_DISPATCH`` --
it is invoked from the per-connection event path that the session router
drives.

The frame projection itself is deliberately *not* here. It lives in
:mod:`encre.events.wire` as the single source of truth shared by every
transport: this WebSocket bridge, the headless JSONL transport in
:mod:`encre.headless`, and anything else that renders agent events. Centralising
the projection means the two transports cannot drift apart -- a new event type
or a renamed field is added once and appears everywhere.

What remains here is the part that is genuinely transport-specific, i.e. the
side effects a frame has on live server state:

- the permission gate, which must mark the session as awaiting approval *before*
  the request reaches the client, since the user's answer may arrive on the
  very next frame;
- plan-item persistence plus the ``agent_state`` mirror push;
- the canvas ``context_usage`` refresh.

Public API:

- :class:`EventDispatchMixin`
"""

import contextlib
import logging
from typing import Any

from encre.events.wire import (
    SessionSnapshot,
    project_event,
    session_snapshot_need,
)
from encre.protocol.render import renderer_session_messages
from encre.server.session_manager import SessionState
from encre.utils.tokens import count_message_tokens
from encre.utils.types import (
    CompactNotification,
    Finish,
    ModelSwitch,
    PermissionRequest,
    PlanUpdate,
    SystemMessage,
    ToolCallStart,
)

logger = logging.getLogger("encre.transport.ws")


class EventDispatchMixin:
    """Turns typed agent events into server frames plus their server side effects.

    Mixed into :class:`encre.transport.ws.EncreWSHandler`. The class holds no
    state; every method works from ``_info`` (the
    :class:`~encre.server.session_manager.SessionInfo` attached to the event)
    and the collaborators installed by
    :class:`~encre.protocol.handlers.base.HandlerBase` (``_manager``,
    ``_session_snapshot``, ``_send``, ``_renderer_session_messages``).

    Lifecycle: stateless, constructed once per server with the composed handler.

    Async safety: each event is processed serially by the single connection
    task that owns the stream, and every ``_send`` inside
    :meth:`_dispatch_event` is awaited. That ordering is what keeps an
    ``agent_state`` push from arriving before the ``plan_update`` it
    describes, and it is why the snapshot work is done up front rather than in
    a background task.

    Invariant: an event that projects to ``None`` still runs the side-effect
    checks at the end, so a no-op frame (empty delta, unknown type) never
    suppresses a required usage refresh.

    Args:
        None. Inherits ``__init__`` from
        :class:`~encre.protocol.handlers.base.HandlerBase` through the composed
        handler.
    """

    async def push_context_usage(self, ws: Any, info: Any, breakdown: dict[str, Any] | None = None) -> None:
        """Emit one ``context_usage`` frame for a session.

        Token-counts the session's current context messages and compares them
        against the backend's declared context window. Shared by the event
        dispatch path (system messages, finish) and by client-message handlers
        (resume) so every pushpoint emits the identical frame shape.

        Args:
            ws: The connection the frame is delivered on.
            info: The session info the frame belongs to; ``None`` (or an info
                without an agent) suppresses the push, matching the old
                closure behaviour.
            breakdown: Per-request category breakdown (system prompt /
                messages / tools split by connector kind / skills). When
                ``None``, falls back to the loop-cached breakdown from the
                last completed prompt phase (``loop.last_context_usage``), so
                late pushes (resume, system messages) still carry the
                composition instead of degrading to total-only.

        Note:
            The window size comes from the live backend, so an agent with no
            backend attached reports ``0`` rather than a guess. The count runs
            over ``get_context_messages()``, not the full message history, so
            it matches the prompt the core would actually build.
        """
        if info is None:
            return
        session = info.agent.session if getattr(info, "agent", None) else None
        if session is None:
            return
        ctx_msgs = session.get_context_messages()
        ctx_tokens = count_message_tokens(ctx_msgs)
        loop = getattr(info.agent, "loop", None)
        backend = getattr(loop, "backend", None)
        window = backend.context_window_size() if backend else 0
        if breakdown is None:
            # Resolve the composition: the loop's in-memory cache from the
            # last finished turn, then the session's persisted copy (which
            # survives restarts) so reopened sessions still report real
            # figures instead of falling back to the char-based gauge.
            breakdown = getattr(loop, "last_context_usage", None) or session.metadata.get("context_usage")
        if breakdown is None and loop is not None:
            # Last resort for sessions whose last activity predates
            # breakdown recording: recompute the resting composition from
            # the live loop state and persist it, so the figure stays
            # attached to the session until compaction or the next turn
            # replaces it.
            recompute = getattr(loop, "current_context_usage", None)
            if callable(recompute):
                breakdown = recompute()
                if breakdown:
                    with contextlib.suppress(Exception):
                        session.metadata["context_usage"] = breakdown
        if breakdown is not None:
            payload_ctx = breakdown.get("total_tokens")
            if isinstance(payload_ctx, int | float) and payload_ctx > 0:
                ctx_tokens = int(payload_ctx)
            bw = breakdown.get("context_window")
            if isinstance(bw, int | float) and int(bw) > 0:
                window = int(bw)
        payload: dict[str, Any] = {
            "context_tokens": ctx_tokens,
            "context_window": window,
            "session_id": info.session_id if info else None,
        }
        if breakdown:
            payload["breakdown"] = breakdown
        await self._send(ws, "context_usage", **payload)

    async def _dispatch_event(self, ws, _info, event: Any) -> None:
        """Project one agent event into a server frame and apply its side effects.

        The sequence is fixed and matters:

        1. Record the permission gate *before* sending anything. A
           :class:`PermissionRequest` means the loop is parked on a consent
           decision, so the session state has to flip to
           ``AWAITING_APPROVAL`` before the frame leaves -- the user's answer
           can arrive on the very next frame and must find the session in the
           right state.
        2. Compute the snapshot budget with
           :func:`encre.events.wire.session_snapshot_need` and only then build
           a snapshot. Snapshotting is comparatively expensive, so only the
           frames that actually carry one pay for it, and only a compacted
           ``finish`` pays for the renderer history.
        3. Project through :func:`encre.events.wire.project_event` and send
           the frame. Events that project to ``None`` -- an empty delta, an
           unknown type, a malformed spec payload -- send nothing.
        4. Run the server-side mirror of whatever was just sent: plan
           persistence and ``agent_state`` for plan and compact events, a
           ``context_usage`` refresh for system messages, and both for
           ``finish``.

        Args:
            ws: The connection the event is delivered on.
            _info: The session info the event belongs to, or ``None`` when the
                event is not tied to a live session. Used for the session id,
                the session object and the manager lookups.
            event: A typed event from :mod:`encre.utils.types`.

        Returns:
            None.

        Note:
            Everything is awaited in place. A fire-and-forget send here would
            let a follow-up ``agent_state`` frame overtake the ``plan_update``
            that describes it, and the desktop plan panel would render stale
            state.

        Raises:
            Nothing escapes: a malformed event simply produces no frame.
        """
        sid = _info.session_id if _info else None
        session = _info.agent.session if _info else None

        async def _send_agent_state() -> None:
            """Push a fresh agent-state mirror frame for the current loop.

            Reads the loop's internal state manager and mode and emits an
            ``agent_state`` frame so the desktop status strip reflects the run
            the loop is actually in. Called after plan, compact, and finish
            events, which are the moments the desktop's view of mode and state
            would otherwise go stale.

            Note:
                The state manager is fetched from the private ``_state_mgr``
                attribute and the mode from ``loop.mode``, both via
                ``getattr`` with a ``None`` default. This closure exists for the
                happy path but must not raise when the loop is only partly
                initialised, so every optional lookup is guarded and the
                closure simply returns without sending.
            """
            if _info is None:
                return
            state_mgr = getattr(_info.agent.loop, "_state_mgr", None)
            if state_mgr is None:
                return
            mode = getattr(_info.agent.loop, "mode", None)
            await self._send(
                ws,
                "agent_state",
                state=state_mgr.snapshot(),
                mode=str(mode.value) if mode is not None else "general",
                session_id=sid,
            )

        async def _send_context_usage(breakdown: dict[str, Any] | None = None) -> None:
            """Report how much of the context window the session is occupying.

            Thin wrapper over :meth:`push_context_usage` kept as a closure so
            the existing call sites read unchanged.

            Args:
                breakdown: Optional per-request category breakdown overriding
                    the loop-cached one (see :meth:`push_context_usage`).
            """
            await self.push_context_usage(ws, _info, breakdown)

        # Permission gate first, before the request can leave the server: the
        # user's answer may arrive on the very next frame and must find the
        # session already marked as awaiting consent.
        if isinstance(event, PermissionRequest):
            self._manager.set_session_state(sid, SessionState.AWAITING_APPROVAL)

        # Only the snapshot-bearing frames are worth building a snapshot for,
        # and only a compacted finish needs the comparatively costly renderer
        # history. Everything else ships without one.
        need = session_snapshot_need(event)
        snapshot: SessionSnapshot | None = (
            self._session_snapshot(session, renderer=need.renderer_history)
            if need.required
            else None
        )

        frame = project_event(event, session_id=sid, snapshot=snapshot)
        if frame is not None:
            await self._send(ws, frame.name, **frame.payload)

        # Server state that mirrors whatever frame was just sent. Order is
        # significant: the state push follows the frame it describes.
        if isinstance(event, PlanUpdate):
            await _send_agent_state()
            if session is not None:
                session.plan_items = event.plan_items
                self._manager._schedule_save(_info)
        elif isinstance(event, (CompactNotification, ModelSwitch)):
            # A compaction or a model switch both change what the canvas
            # gauge must show: the compaction rewrote the context, and a
            # switch (manual or pool cycling) may have changed the context
            # WINDOW itself.  Refresh the mirror and the gauge together.
            await _send_agent_state()
            await _send_context_usage()
            if isinstance(event, ModelSwitch) and _info is not None:
                # The loop rotated to another pool model: re-announce the
                # model list so the input-toolbar selector follows the
                # switch instead of showing the model that just failed.
                try:
                    await self._send_models_updated(ws, _info)
                except Exception:
                    logger.debug("[dispatch] models_updated push failed", exc_info=True)
        elif isinstance(event, SystemMessage):
            await _send_context_usage()
        elif isinstance(event, ToolCallStart):
            # Mid-turn refresh: the prompt phase already froze this turn's
            # composition into the loop cache, so the first tool call is the
            # earliest moment the gauge can show the current request's real
            # figures instead of waiting for the whole turn to finish.
            await _send_context_usage()
        elif isinstance(event, Finish):
            await _send_agent_state()
            await _send_context_usage(getattr(event, "context_usage", None))

    @staticmethod
    def _session_snapshot(session: Any, *, renderer: bool) -> SessionSnapshot | None:
        """Collect the session extras that a snapshot-bearing frame carries.

        Builds the small bundle of session fields the projection needs beyond
        the event payload itself: the rendered history (only when a frame
        actually embeds it), the tail message, and the plan/artifact/reference
        sidebars. Keeping this a staticmethod means the projection code in
        :mod:`encre.events.wire` stays the only thing that decides what a frame
        contains, while the transport decides which fields were worth gathering.

        Args:
            session: The live session, or ``None`` when the event is not tied
                to a session. ``None`` short-circuits to ``None`` so callers do
                not have to branch.
            renderer: Whether the (O(n)) renderer history is actually needed.
                ``False`` ships an empty list instead of rendering the whole
                branch and then discarding it, which is what keeps ordinary
                ``plan_update`` and ``compact`` frames cheap.

        Returns:
            The :class:`encre.events.wire.SessionSnapshot`, or ``None`` when
            there is no session.

        Note:
            ``tail_message`` is the last message of the branch, not the full
            history. Snapshot frames that need a single message -- the one the
            event refers to -- do not have to fetch the rest.
        """
        if session is None:
            return None
        messages = session.messages
        return SessionSnapshot(
            renderer_messages=(
                renderer_session_messages(session) if renderer else []
            ),
            tail_message=messages[-1] if messages else None,
            plan_items=session.plan_items,
            artifacts=session.artifacts,
            references=session.references,
        )
