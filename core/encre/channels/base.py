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

"""Channel base abstraction: the contract every transport surface implements.

The agent runtime is transport-neutral. It does not know whether a request
arrives over a WebSocket from the Electron renderer, over HTTP from the
OpenAI-compatible endpoint, or from a process's stdin. That neutrality is
created by this module: any client endpoint that can implement
:class:`Channel` becomes a first-class client of the same agent.

:class:`Channel` is an interface object only. It carries no state, performs
no I/O, and holds no buffering policy. All of that belongs to a subclass,
because the framing and lifecycle rules differ completely between the
websocket transport, the HTTP
server in :mod:`encre.transport.http.channel`, and the REPL in
:mod:`encre.channels.terminal`.

The router is threaded into :meth:`Channel.start` rather than stored by the
base class on purpose. It keeps the base stateless and lets a server own and
share one router across many channels and many client sessions; see
:class:`encre.server.app.EncreServer` for that composition.

The concrete multi-session router lives in
:mod:`encre.protocol.session_router`; it is imported here only under
``TYPE_CHECKING`` so that a runtime import of this module does not pull in
the protocol and session stack.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from encre.protocol.session_router import EventRouter


class Channel(ABC):
    """Abstract transport surface between one client endpoint and the agent runtime.

    A channel is the owner of exactly one client connection (or one terminal
    session). Its job is framed in three verbs:

    - accept or read client input,
    - decode it and dispatch it to the shared :class:`EventRouter`,
    - encode the returned events and write them back.

    The base class defines only the lifecycle contract. Everything else is
    subclass policy, because the transports differ in ways that cannot be
    normalised: WebSocket channels multiplex many sessions over one socket
    and must survive reconnects; HTTP channels are request/response and die
    with the connection; the terminal channel is a single blocking reader.

    Invariants a subclass must maintain:

    - At most one running loop at a time. Calling :meth:`start` twice must
      either be a no-op or replace the previous loop; it must not leave two
      tasks consuming the same client.
    - :meth:`stop` must be safe to call twice and safe to call before
      :meth:`start` returned, so server shutdown code can call it
      unconditionally during teardown.
    - A channel must never cancel the router. The router outlives any single
      channel; killing it would disconnect every other client too.

    Lifecycle: construct -> :meth:`start` with a router -> dispatch and
    stream until :meth:`stop` -> channel-local resources released. The
    router and the sessions it manages survive the channel.

    Thread and async safety: the whole surface is asyncio-based. Subclasses
    perform I/O with ``await`` and must not block the event loop with
    synchronous calls -- the terminal channel is the one deliberate
    exception, and it isolates stdin via
    :func:`asyncio.loop.run_in_executor`.

    Attributes:
        name: Stable channel identifier, e.g. ``"terminal"`` or
            ``"websocket"``. Routers use it to attribute a submission to a
            client, and telemetry records it as the source channel, so it
            must be a fixed string set at class definition time rather than
            per instance.
    """

    name: ClassVar[str]

    @abstractmethod
    async def start(self, router: EventRouter) -> None:
        """Start the channel's listening loop and begin dispatching messages.

        Implementations spawn their accept/read loop here and normally return
        only after that loop has finished, so a caller awaiting
        ``awaitable = channel.start(router)`` can treat the coroutine as the
        whole lifetime of the channel. Subclasses that instead return early
        and hold the loop in a task must still guarantee that :meth:`stop`
        terminates it.

        Args:
            router: The shared event router. The channel dispatches inbound
                messages through :meth:`encre.protocol.session_router.EventRouter.submit_stream`
                and reads session state through ``router.session_manager``.
                This reference is owned by the server, not by the channel, so
                the channel must not close or mutate it.

        Returns:
            ``None``.

        Raises:
            Exception: Propagated if the accept loop cannot start. A server
                typically catches this and keeps the process alive so the
                remaining channels keep serving.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel's listening loop and release channel-local resources.

        Called during server shutdown. Implementations must:

        - signal the loop to exit and await its completion, so in-flight
          client messages are not lost mid-write;
        - release sockets, files, subprocesses, and background tasks owned by
          the channel;
        - leave sessions managed by the router intact. They belong to the
          session store, which is shared and may still be reached by other
          channels.

        Must be idempotent: shutdown paths and signal handlers can both call
        it.

        Returns:
            ``None``.
        """
        ...
