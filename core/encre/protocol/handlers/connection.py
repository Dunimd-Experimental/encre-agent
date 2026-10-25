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

"""Per-connection state isolation for the WebSocket transport.

Historically every per-connection attribute lived directly on the singleton
:class:`~encre.protocol.handlers.base.HandlerBase`, so two concurrently
connected clients overwrote each other's session binding, workspace, terminal
table and write lock.  This module introduces :class:`ConnectionState`, one
object per socket, plus a :class:`ConnectionRegistry` that

1. maps each live socket to its state,
2. exposes the state of the *currently dispatching* connection through a
   :class:`contextvars.ContextVar`, so the ~100 existing ``self._xyz``
   handler references keep working unchanged while resolving to
   connection-local values, and
3. requires every broadcast path to address a socket's state explicitly via
   :meth:`ConnectionRegistry.state_for`, never implicitly.

Why a ContextVar rather than a per-connection handler instance: the handler
mixins are composed once per *server*, not per socket, and roughly a hundred
call sites read ``self._current_session_id`` / ``self._workspace_path`` /
``self._send_lock``.  A ContextVar gives real isolation (each connection is
served by its own task, so each task carries its own value) without a
100-site rewrite, while still letting broadcasts opt into explicit,
connection-addressed state.

Invariant: transport encryption is **always on**.  ``client_encrypted`` is
recorded for diagnostics only; no code path may branch on it to emit a
plaintext frame.
"""

import asyncio
import contextvars
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("encre.transport.ws")


@dataclass
class ConnectionState:
    """All state owned by exactly one WebSocket connection.

    Args:
        ws: The live socket, used as the registry key and send target.
        ws_id: A short stable identifier for log lines.
    """

    ws: Any
    ws_id: str

    # -- transport ---------------------------------------------------------
    # One lock per connection serialises the direct-send fallback.  Normal
    # frames go through the outbox below instead.
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Outbound pipeline: producers serialise a frame and enqueue it; a single
    # writer task per connection encrypts and writes it.  The agent loop never
    # waits on socket I/O, and a slow client cannot stall generation.  Bounded
    # so a wedged socket applies backpressure instead of growing memory.
    #: ``(payload, already_encrypted)``. Producers queue plaintext JSON;
    #: internal broadcast replay queues an already-encrypted frame so it joins
    #: the same ordered writer instead of racing it with a direct socket write.
    outbox: asyncio.Queue[tuple[str, bool] | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=2048)
    )
    writer_task: asyncio.Task[Any] | None = None
    closed: bool = False
    # Recorded for diagnostics; encryption is mandatory so this is always True.
    client_encrypted: bool = True
    protocol_version: int | None = None

    # -- session / workspace binding --------------------------------------
    info: Any = None
    current_session_id: str | None = None
    workspace_path: str = ""
    current_ws_id: str = ""

    # -- connection-scoped resources ---------------------------------------
    # These used to be shared across every client on the server, which let one
    # session's spec approval state bleed into another's, and exposed one
    # client's live PTYs to all others.
    spec_engines: dict[str, Any] = field(default_factory=dict)
    term_sessions: dict[int, dict] = field(default_factory=dict)
    term_seq: int = 0

    # -- in-flight work ----------------------------------------------------
    # Tasks spawned on this connection's behalf.  Cancelled on disconnect so a
    # dead client cannot keep the Encre Agent Core busy or hold a session open.
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    iclaw_task: asyncio.Task[Any] | None = None
    wechat_poll_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)

    def reset_binding(self) -> None:
        """Clear the session/workspace binding for a fresh connection.

        Returns:
            None.
        """
        self.info = None
        self.current_session_id = None
        self.workspace_path = ""
        self.current_ws_id = ""
        self.protocol_version = None

    def describe(self) -> str:
        """Return a short human-readable identifier for logs.

        Returns:
            A ``ws_id`` plus the bound session prefix, or ``"(unbound)"``.
        """
        sid = self.current_session_id or "(unbound)"
        return f"{self.ws_id} session={sid[:8]}"


class ConnectionRegistry:
    """Owns the socket -> :class:`ConnectionState` map and the ambient binding.

    Args:
        handler: The composed handler that owns this registry.
    """

    def __init__(self, handler: Any) -> None:
        """Initialise an empty registry bound to *handler*.

        Args:
            handler: The handler whose per-connection attribute reads are
                routed through this registry.
        """
        self._handler = handler
        self._states: dict[int, ConnectionState] = {}
        self._current: contextvars.ContextVar[ConnectionState | None] = (
            contextvars.ContextVar("encre_connection_state", default=None)
        )
        # Read-only fallback for code that runs outside any connection task
        # (broadcast fan-out).  Writes to it are a bug and are logged loudly.
        self._detached = ConnectionState(ws=None, ws_id="detached")

    # ---- registry ---------------------------------------------------------

    def register(self, ws: Any) -> ConnectionState:
        """Create and register the state for a newly accepted socket.

        Args:
            ws: The accepted WebSocket.

        Returns:
            The freshly created :class:`ConnectionState`.
        """
        state = ConnectionState(ws=ws, ws_id=f"ws{id(ws):x}")
        self._states[id(ws)] = state
        return state

    def unregister(self, ws: Any) -> ConnectionState | None:
        """Remove and return the state for a closing socket.

        Args:
            ws: The socket being closed.

        Returns:
            The removed state, or ``None`` if it was not registered.
        """
        return self._states.pop(id(ws), None)

    def state_for(self, ws: Any) -> ConnectionState:
        """Return the state owned by *ws*, registering a placeholder if absent.

        Broadcast paths call this with an arbitrary socket.  A socket that was
        never registered (or was already unregistered) gets a throwaway state so
        the frame is still encoded with the mandatory transport encryption
        rather than raising and silently dropping the broadcast.

        Args:
            ws: The target socket.

        Returns:
            The socket's :class:`ConnectionState`.
        """
        key = id(ws)
        state = self._states.get(key)
        if state is None:
            logger.warning(
                "[conn] no registered state for target socket; using placeholder"
            )
            state = ConnectionState(ws=ws, ws_id=f"ws{key:x}")
            self._states[key] = state
        return state

    def states(self) -> list[ConnectionState]:
        """Return every live connection state.

        Returns:
            A snapshot list, safe to iterate while a connection closes.
        """
        return list(self._states.values())

    @property
    def sockets(self) -> list[Any]:
        """Return every live socket.

        Returns:
            A snapshot list of registered sockets.
        """
        return [s.ws for s in self._states.values() if s.ws is not None]

    # ---- ambient binding --------------------------------------------------

    @property
    def current(self) -> ConnectionState:
        """Return the state of the connection currently being dispatched.

        Returns:
            The bound state, or a detached read-only fallback when called from
            outside any connection task.
        """
        state = self._current.get()
        return state if state is not None else self._detached

    def bind(self, state: ConnectionState) -> contextvars.Token:
        """Bind *state* as the ambient connection for the current task.

        Args:
            state: The connection whose state should be ambient.

        Returns:
            A token to pass to :meth:`unbind`.
        """
        return self._current.set(state)

    def unbind(self, token: contextvars.Token) -> None:
        """Restore the previous ambient connection.

        Args:
            token: The token returned by :meth:`bind`.

        Returns:
            None.
        """
        self._current.reset(token)

    # ---- attribute routing ------------------------------------------------

    def _routed(self, attr: str) -> Any:
        """Read a per-connection attribute from the ambient connection.

        Args:
            attr: The :class:`ConnectionState` field name.

        Returns:
            The field's value.
        """
        return getattr(self.current, attr)

    def _route(self, attr: str) -> Any:
        """Build a property getter for a per-connection attribute.

        Args:
            attr: The :class:`ConnectionState` field name.

        Returns:
            A function suitable for use as a property getter.
        """
        return lambda self: self._connections_state._routed(attr)

    def _route_setter(self, attr: str) -> Any:
        """Build a property setter for a per-connection attribute.

        Args:
            attr: The :class:`ConnectionState` field name.

        Returns:
            A function suitable for use as a property setter.
        """
        def _set(self, value: Any) -> None:
            state = self._connections_state.current
            if state is self._connections_state._detached:
                logger.warning(
                    "[conn] writing %r outside any connection task; ignored", attr
                )
                return
            setattr(state, attr, value)

        return _set

    def property_for(self, attr: str) -> property:
        """Return a :class:`property` that proxies *attr* to the current state.

        Args:
            attr: The :class:`ConnectionState` field name.

        Returns:
            A property exposing get/set behaviour routed to the ambient
            connection.
        """
        return property(self._route(attr), self._route_setter(attr))


#: Per-connection handler attributes that are proxied to the ambient
#: :class:`ConnectionState` instead of living on the singleton handler.
#:
#: Every name here MUST be removed from ``HandlerBase.__init__`` and from the
#: per-connection reset in ``EncreWSHandler.handle`` -- leaving a real
#: attribute would shadow the property and reintroduce the cross-connection
#: leak this module exists to remove.
CONNECTION_SCOPED_ATTRS: tuple[str, ...] = (
    "info",
    "current_session_id",
    "workspace_path",
    "current_ws_id",
    "protocol_version",
    "client_encrypted",
    "send_lock",
    "spec_engines",
    "term_sessions",
    "term_seq",
    "tasks",
    "iclaw_task",
    "wechat_poll_tasks",
)
