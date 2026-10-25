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

"""Connection lifecycle and transport shared by every domain handler.

This module is the common base for the protocol handler mixins composed into
the per-connection WebSocket handler. It owns per-connection state (encryption
flag, current session/workspace, running tasks), the single ``_send`` frame
path, session bootstrap, the unified full-data snapshot push, and
config/settings persistence helpers.

Data flow: a client opens a WebSocket, the handler calls
:meth:`HandlerBase._push_all_data`, which pushes config, sessions,
workspaces, models, and every other static data plane the desktop panels
subscribe to. From then on the frontend never pulls slices on its own, so a
mode switch cannot leave one panel showing stale data while another is fresh.

It sits one level below the composed connection handler, which is assembled
from the domain handler mixins in :mod:`encre.protocol.handlers`; those
mixins import the shared state and helpers defined here rather than carrying
their own copies.

Public API:

- :class:`HandlerBase`
- :func:`_apply_session_files_dir`
- :func:`_inject_context_windows`
- :func:`_resolve_model_context_windows`
"""

import asyncio
import contextlib
import logging
import os
import time
import traceback
from dataclasses import replace
from typing import Any

from encre.backends.catalog import catalog_payload
from encre.backends.mcp_catalog import mcp_catalog_payload
from encre.backend import create_backend
from encre.config import EncreConfig
from encre.keybinds import load_keybinds
from encre.crypto import encrypt
from encre.server.protocol import serialize_server_message
from encre.server.session_manager import SessionManager
from encre.settings_manager import load_custom_slash_commands
from encre.slash_commands import get_slash_command_defs
from encre.spec import EncreSpecEngine
from encre.tools.runtime import set_search_engine_url

from encre.protocol.handlers.connection import ConnectionRegistry, ConnectionState
from encre.protocol.handlers.workspace_store import (
    _load_workspace_config,
    _load_workspaces,
    _workspace_context_files,
    _workspaces_with_session_counts,
)
from encre.protocol.render import renderer_session_messages
from encre.protocol.session_router import SubmitBacklog

logger = logging.getLogger("encre.transport.ws")


class _BroadcastWs:
    """Fan-out stand-in for a single WebSocket connection.

    Drained queue entries re-enter the run path without an originating
    socket (the client that queued the message may have disconnected).  The
    run path treats *ws* as an opaque send target, so this wrapper fans every
    frame out to every live connection instead: clients filter events by the
    ``session_id`` stamped onto each frame, so only the conversation's viewer
    renders them.  ``send`` never raises -- one dead connection must not
    cancel a drained run -- which also means :meth:`_send`'s disconnect
    cancel path can never fire for a drained run.

    Args:
        handler: The composed handler whose ``_connections`` list to fan out
            to.
    """

    def __init__(self, handler: Any) -> None:
        """Pin the owning handler.

        Args:
            handler: The handler instance holding ``_connections``.
        """
        self._handler = handler

    async def send(self, payload: Any) -> None:
        """Forward one already-encrypted frame to every live connection.

        Routed through each connection's outbox rather than the socket
        directly, so a drained run's frames keep their order relative to the
        frames produced by the normal send path and inherit the same
        backpressure/ordering guarantees.

        Args:
            payload: The encoded (ciphertext) frame produced by ``_send_direct``.
        """
        for state in list(self._handler._connections_state.states()):
            if state.ws is None or state.closed:
                continue
            writer = state.writer_task
            if writer is not None and not writer.done():
                try:
                    state.outbox.put_nowait((payload, True))
                except asyncio.QueueFull:
                    await state.outbox.put((payload, True))
            else:
                try:
                    await state.ws.send(payload)
                except Exception:
                    continue  # one dead peer must not cancel a drained run


def _apply_session_files_dir(config_data: dict[str, Any], session_id: str) -> None:
    """Attach the current session's files directory to a config snapshot.

    The absolute path lets frontend panels (the editor tree) operate on the
    per-session sandbox in general mode without guessing at the data-dir
    layout. Best-effort: a failure leaves the field absent so the frontend
    falls back to its own default.

    Args:
        config_data: The config snapshot dict, mutated in place.
        session_id: The session whose files directory is requested.

    Returns:
        None.
    """
    try:
        from encre.capabilities.process import get_session_files_dir
        config_data["session_files_dir"] = str(get_session_files_dir(session_id))
    except Exception as exc:
        logger.debug("[config] session_files_dir skipped: %s", exc)


def _inject_context_windows(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure every model dict has a real ``context_window`` value.

    Legacy configs return 0 for context_window. This function creates a
    temporary backend to resolve the actual window size so the frontend
    canvas panel shows the correct token budget.

    Args:
        models: Model dicts from a config snapshot, mutated in place.

    Returns:
        The same list, with any zero ``context_window`` resolved where
        possible.
    """
    for m in models:
        if m.get("context_window", 0) > 0:
            continue
        bt = m.get("backend_type", "")
        mid = m.get("model_id", "")
        if bt and mid:
            try:
                be = create_backend(bt, model=mid, base_url=m.get("base_url", ""))
                if be:
                    m["context_window"] = be.context_window_size()
            except Exception:
                pass
    return models


def _resolve_model_context_windows(models: list[Any]) -> list[Any]:
    """Write the real context-window size back into each ``ModelConfig``.

    Legacy or freshly-added models carry ``context_window == 0``; without
    a resolved value ``apply_active_model()`` cannot forward the window
    through ``backend_kwargs``, so the backend falls back to name-based
    detection and the effective context window never changes when the user
    switches models (nor does compaction trigger for a smaller window).
    Resolution mirrors :func:`_inject_context_windows` but mutates the
    ``ModelConfig`` objects themselves so the value persists with config.

    Args:
        models: ``ModelConfig`` objects, mutated in place.

    Returns:
        The same list, with resolved windows where a backend could be built.
    """
    for m in models:
        if not hasattr(m, "context_window"):
            continue
        if getattr(m, "context_window", 0) > 0:
            continue
        bt = getattr(m, "backend_type", "")
        mid = getattr(m, "model_id", "")
        if bt and mid:
            try:
                be = create_backend(bt, model=mid, base_url=getattr(m, "base_url", ""))
                if be:
                    m.context_window = be.context_window_size()
            except Exception:
                pass
    return models


def safe_pkg_name(name: str) -> str:
    """Normalise a user-provided name into a safe plugin package suffix.

    Only ``[A-Za-z0-9._-]`` survive; every other character (including path
    separators, so a hostile name cannot escape the plugin root) collapses
    to ``-``.  Used by every handler that turns a user-installed skill /
    MCP server / sub-agent into a plugin package directory.

    Args:
        name: Raw user-facing name.

    Returns:
        A filesystem-safe, non-empty suffix string.
    """
    cleaned = "".join(c if c.isalnum() or c in "._-" else "-" for c in name)
    cleaned = cleaned.strip(".-") or "item"
    return cleaned[:64]


class HandlerBase:
    """Connection state, frame send, and session bootstrap for all mixins.

    Every protocol handler mixin for the WebSocket transport inherits from
    this class. It is the single owner of per-connection mutable state and the
    single write path for server frames, which is what stops a domain handler
    from sending raw frames and bypassing the encryption negotiation.

    Invariants:

    - All frames go through :meth:`_send`. Nothing else writes to ``ws``.
    - ``_info`` is lazily created on first use and is the session the
      connection is currently bound to; :meth:`_get_or_create_session` is the
      only place it is (re)assigned.
    - Spec engines are keyed by session id, never shared, so approve/reject
      state cannot leak across conversations.
    - Transport encryption is **mandatory in both directions**. The first
      client frame is verified as ciphertext during the handshake; a plaintext
      opener is closed with code 4400 rather than served. :meth:`_send` has no
      plaintext branch.

    Thread/async safety: all state is touched from the connection's own event
    loop. Per-connection attributes live on a
    :class:`~encre.protocol.handlers.connection.ConnectionState` addressed
    through :attr:`_connections_state`, so concurrent clients cannot overwrite
    each other's session binding, workspace or write lock. Broadcasts spawn
    tasks tracked against their *target* connection so a client disconnecting
    cancels only its own work.

    Attributes:
        _manager: The :class:`SessionManager` owning all sessions.
        _default_config: The config template for new sessions.
        _adapter_manager: The gateway runner, if one is wired in.
        _scheduler: The iClaw scheduler, if available.
        _connections_state: The :class:`ConnectionRegistry` owning every live
            connection's state and routing the attributes below to whichever
            connection is currently being dispatched.
        _connections: Legacy list of live sockets, kept in step with the
            registry.

    Args:
        session_manager: The session manager for this server.
        config: Default config for new sessions, or ``None`` for a fresh one.
        adapter_manager: Optional gateway runner for status broadcasts.
        scheduler: Optional iClaw scheduler.
    """

    def __init__(self, session_manager: SessionManager, config: EncreConfig | None = None,
                 adapter_manager=None, scheduler=None) -> None:
        """Initialise handler state and register the sessions-changed callback.

        The sessions-changed callback is registered here rather than in the
        constructor of the composed handler so every mixin that inherits from
        this base automatically gets session-list pushes without wiring it
        itself.

        Raises:
            Exception: Anything raised by ``on_sessions_changed`` propagates.
        """
        self._manager = session_manager
        self._default_config = config
        self._adapter_manager = adapter_manager
        self._scheduler = scheduler  # EncreScheduler from iClawEngine, if available
        # Per-connection state lives in ConnectionState, addressed through this
        # registry.  Every name in CONNECTION_SCOPED_ATTRS is proxied below to
        # the connection currently being dispatched, so the two handler mixes
        # that used to share one set of attributes can no longer clobber each
        # other's session binding, workspace, terminal table or write lock.
        self._connections_state = ConnectionRegistry(self)
        self._connections: list[Any] = []
        self._manager.on_sessions_changed(self._broadcast_sessions)
        # Busy-submit backlog for the desktop run path: prompts that arrived
        # while their conversation was running.  Shares the durable
        # pending-message table with the router backlog under its own
        # adapter name, so a restart replays them headless.  Deliberately
        # server-wide rather than per-connection: the queue must survive the
        # client that enqueued it.
        self._submit_backlog = SubmitBacklog("desktop", runner=self._drain_desktop_entry)
        self._queue_listener_installed = False
        self._queue_replayed = False
        # Last broadcast session snapshot per channel. Session-change callbacks
        # can fire without the visible list changing; re-sending a multi-MB
        # list (and making every client re-decrypt/parse it) is pure waste.
        self._sessions_broadcast_fp: dict[str, tuple] = {}
        # One coalesced broadcast at a time: boot/warmup/streaming produce
        # bursts of session-change callbacks, and each one used to rebuild the
        # whole 5k-row snapshot.
        self._sessions_broadcast_timer: Any = None
        # Server-owned background work (gateway delivery, scheduler ticks) that
        # belongs to no single connection.  Tracked separately so it is not
        # cancelled when an unrelated client disconnects, but still gets a
        # strong reference so it is not garbage-collected mid-flight.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _spawn_background(self, coro) -> asyncio.Task[Any]:
        """Spawn server-owned background work, tracked for the server's lifetime.

        Use this instead of ``self._tasks.add(...)`` for work that is *not*
        driven by a particular client -- for example pushing an automation
        result out through a gateway adapter.  Attaching such work to a
        connection's task set would cancel it the moment that client
        disconnected, even though the job had nothing to do with it.

        Args:
            coro: The coroutine to run.

        Returns:
            The scheduled task.
        """
        _t = asyncio.ensure_future(coro)
        self._background_tasks.add(_t)
        _t.add_done_callback(self._background_tasks.discard)
        return _t

    # ---- Per-connection attribute proxies --------------------------------
    #
    # Each property below reads and writes the ConnectionState of the
    # connection whose task is currently running, so the ~100 existing
    # ``self._current_session_id`` style references resolve to that
    # connection's own value instead of a single server-wide slot.
    _info = property(lambda self: self._connections_state._routed("info"),
                     lambda self, v: setattr(self._connections_state.current, "info", v))
    _current_session_id = property(
        lambda self: self._connections_state._routed("current_session_id"),
        lambda self, v: setattr(self._connections_state.current, "current_session_id", v),
    )
    _workspace_path = property(
        lambda self: self._connections_state._routed("workspace_path"),
        lambda self, v: setattr(self._connections_state.current, "workspace_path", v),
    )
    _current_ws_id = property(
        lambda self: self._connections_state._routed("current_ws_id"),
        lambda self, v: setattr(self._connections_state.current, "current_ws_id", v),
    )
    _protocol_version = property(
        lambda self: self._connections_state._routed("protocol_version"),
        lambda self, v: setattr(self._connections_state.current, "protocol_version", v),
    )
    _client_encrypted = property(
        lambda self: self._connections_state._routed("client_encrypted"),
        lambda self, v: setattr(self._connections_state.current, "client_encrypted", v),
    )
    _send_lock = property(
        lambda self: self._connections_state._routed("send_lock"),
        lambda self, v: setattr(self._connections_state.current, "send_lock", v),
    )
    _spec_engines = property(
        lambda self: self._connections_state._routed("spec_engines"),
        lambda self, v: setattr(self._connections_state.current, "spec_engines", v),
    )
    _term_sessions = property(
        lambda self: self._connections_state._routed("term_sessions"),
        lambda self, v: setattr(self._connections_state.current, "term_sessions", v),
    )
    _term_seq = property(
        lambda self: self._connections_state._routed("term_seq"),
        lambda self, v: setattr(self._connections_state.current, "term_seq", v),
    )
    _tasks = property(
        lambda self: self._connections_state._routed("tasks"),
        lambda self, v: setattr(self._connections_state.current, "tasks", v),
    )
    _iclaw_task = property(
        lambda self: self._connections_state._routed("iclaw_task"),
        lambda self, v: setattr(self._connections_state.current, "iclaw_task", v),
    )
    _wechat_poll_tasks = property(
        lambda self: self._connections_state._routed("wechat_poll_tasks"),
        lambda self, v: setattr(self._connections_state.current, "wechat_poll_tasks", v),
    )

    def _connection_for(self, ws: Any) -> ConnectionState:
        """Return the :class:`ConnectionState` that owns *ws*.

        Broadcast paths must never read the ambient connection when encoding a
        frame: they address the target socket's state explicitly, so a frame
        destined for connection B is never encoded with connection A's
        encryption flag or serialised behind A's write lock.

        Args:
            ws: The target socket.

        Returns:
            The target's connection state.
        """
        return self._connections_state.state_for(ws)

    async def _drain_desktop_entry(self, session_id: str, entry: dict[str, Any]) -> None:
        """Execute one drained desktop queue entry as a full run.

        Re-enters :meth:`_h_run` with a rebuilt :class:`ClientRun` so mode
        application, attachments, auto-naming and transcript behaviour are
        identical to a client-initiated run.  The original socket is gone by
        drain time in the general case, so the run targets a broadcast
        stand-in: every frame fans out to all live connections and clients
        filter by ``session_id``.

        Args:
            session_id: The conversation to run the drained entry against.
            entry: The payload recorded by the busy branch of ``_h_run``.
        """
        from encre.server.protocol import ClientRun

        # Mirror the transition to every client first: the viewer renders the
        # user bubble and flips to its running state exactly like a local
        # send, before the first stream frame arrives.
        await self._broadcast_queued_run_started(session_id, entry)

        run_msg = ClientRun(
            type="run",
            prompt=entry.get("prompt", ""),
            system_prompt=entry.get("system_prompt"),
            session_id=session_id,
            attachments=entry.get("attachments"),
            mode=entry.get("mode"),
            mode_prompt=entry.get("mode_prompt"),
            temp_chat=bool(entry.get("temp_chat", False)),
        )
        try:
            await self._h_run(_BroadcastWs(self), run_msg)
        except Exception as e:
            logger.error("[queue] drained desktop entry failed for %s: %s", session_id, e)

    def _queue_backlog_for(self, session_id: str) -> SubmitBacklog | None:
        """Return the backlog that owns *session_id*'s queue.

        The router (adapter/iclaw space) and the desktop handler keep
        separate backlogs; the session's channel tag decides which one
        serves it, defaulting to the desktop backlog.

        Args:
            session_id: The conversation whose backlog to resolve.

        Returns:
            The owning :class:`SubmitBacklog`, or ``None`` when the session
            is unknown.
        """
        session = self._manager.get_session(session_id) if session_id else None
        channel = ""
        if session is not None:
            channel = session.agent.session.metadata.get("channel", "") or ""
        if channel == "iclaw" and self._adapter_manager is not None:
            router = getattr(self._adapter_manager, "router", None)
            if router is not None:
                return router.backlog
        return self._submit_backlog

    def _ensure_queue_listener(self) -> None:
        """Wire the queue-broadcast listener and durable store (idempotent).

        Registered lazily on the first queue-affecting message so a handler
        that never touches queues pays nothing.  The listener broadcasts a
        ``queued_submits`` frame after every mutation, and the desktop
        backlog gets the gateway's durable pending store so a restart can
        replay its rows.
        """
        if not self._queue_listener_installed:
            self._queue_listener_installed = True
            self._submit_backlog.set_listener(self._on_queue_changed)
        if self._adapter_manager is not None:
            gateway = self._adapter_manager
            store = getattr(gateway, "_session_store", None)
            if store is not None:
                self._submit_backlog.set_store(store)
                if not self._queue_replayed:
                    self._queue_replayed = True
                    self._submit_backlog.replay()
            router = getattr(gateway, "router", None)
            if router is not None:
                router.backlog.set_listener(self._on_queue_changed)

    async def _on_queue_changed(self, backlog: SubmitBacklog, session_id: str) -> None:
        """Broadcast one conversation's queue state to every connection.

        Args:
            backlog: The backlog that mutated.
            session_id: The conversation whose queue changed.
        """
        # Strip private keys (e.g. the non-serialisable ``_owner`` socket
        # reference) so the payload stays JSON-encodable.
        items = [
            {k: v for k, v in e.items() if not k.startswith("_")}
            for e in backlog.entries(session_id)
        ]
        for state in self._connections_state.states():
            if state.ws is None:
                continue
            with contextlib.suppress(Exception):
                await self._send(state.ws, "queued_submits",
                                 session_id=session_id,
                                 items=items)
    async def _broadcast_queued_run_started(self, session_id: str, entry: dict[str, Any]) -> None:
        """Announce that a drained queue entry started running.

        Frontends use this to add the user bubble and switch the session to
        its running state, mirroring what a client-initiated run does
        locally.

        Args:
            session_id: The conversation the drained entry runs against.
            entry: The drained payload (``prompt``/``metadata`` are read).
        """
        metadata = entry.get("metadata") or {}
        for state in self._connections_state.states():
            if state.ws is None:
                continue
            with contextlib.suppress(Exception):
                await self._send(state.ws, "queued_run_started",
                                 session_id=session_id,
                                 prompt=entry.get("prompt", ""),
                                 mode=(metadata or {}).get("mode"))

    # Max items per ``data_shard`` frame. Small enough that each frame's
    # JSON encode + transfer stays in the milliseconds, large enough that the
    # frame count never dominates. Every topic push shares this bound.
    TOPIC_SHARD_SIZE = 80

    async def _send_topic_shards(
        self, ws, topic: str, meta: dict | None, items: list
    ) -> None:
        """Push any large data set as one sharded topic over the WebSocket.

        THE single generic bulk-data channel between backend and renderer.
        Every payload that can grow with usage (session history, the memory
        graph, file listings, ...) is pushed through this method instead of
        growing its own bespoke mega-frame: it emits ``data_begin`` (topic +
        metadata + shard count), then fixed-size ``data_shard`` frames, then
        ``data_end``. Because each shard is encoded and sent independently,
        no single multi-megabyte frame is ever built, the socket stays
        responsive for interactive events between shards, and the renderer
        can start working from the meta frame alone.

        Args:
            ws: Target WebSocket connection.
            topic: Topic identifier, ``"<kind>:<scope>:<what>"``. The renderer
                routes by prefix/suffix so new producers need no new
                client-side plumbing beyond one handler entry.
            meta: Small metadata dict sent with ``data_begin`` (request ids,
                counts, cursor positions -- never bulk data).
            items: The bulk items; chunked by :data:`TOPIC_SHARD_SIZE`.

        Returns:
            None. Never raises: a dead socket or an unencodable item only
            ends the push, never the caller.
        """
        try:
            total = len(items)
            shard_size = self.TOPIC_SHARD_SIZE
            total_shards = (total + shard_size - 1) // shard_size if total else 0
            await self._send(ws, "data_begin", topic=topic, meta=meta or {},
                             total_shards=total_shards, total_items=total)
            for seq in range(total_shards):
                chunk = items[seq * shard_size:(seq + 1) * shard_size]
                await self._send(ws, "data_shard", topic=topic, seq=seq, items=chunk)
            await self._send(ws, "data_end", topic=topic)
        except Exception:
            logger.debug("[topic] shard push %s failed", topic, exc_info=True)

    async def _send(self, ws, msg_type: str, **kwargs) -> None:
        """Serialise a server message and hand it to the connection's writer.

        This is the single outbound entry point for every server frame.  It
        deliberately performs **no socket I/O**: the payload is snapshotted to
        JSON here and enqueued, while the per-connection writer task encrypts
        and writes it.  The agent loop therefore never blocks on a slow client,
        and several producers (run loop, broadcasts, background jobs) can feed
        one socket without serialising on a shared lock.

        Transport encryption remains mandatory and happens in the writer; no
        frame is ever sent in the clear.

        Args:
            ws: The WebSocket connection to write to.
            msg_type: The server message type being sent.
            **kwargs: Fields of the message payload.

        Returns:
            None.

        Note:
            Never raises. Encoding failures are logged and dropped, because a
            caller sending a best-effort broadcast must not be interrupted by
            one bad payload.
        """
        state = self._connection_for(ws)
        if state.closed:
            return
        try:
            payload = serialize_server_message(msg_type, **kwargs)
        except Exception as exc:
            logger.error("[_send] Failed to encode %s: %s\n%s", msg_type, exc, traceback.format_exc())
            return

        writer = state.writer_task
        if writer is None or writer.done():
            # Detached/placeholder socket or a writer that already exited:
            # preserve the old direct path so broadcasts are not dropped.
            await self._send_direct(state, payload, msg_type)
            return

        try:
            state.outbox.put_nowait((payload, False))
        except asyncio.QueueFull:
            # Bounded backpressure: a wedged socket pauses the producer
            # instead of letting the outbox grow without bound.
            await state.outbox.put((payload, False))

    async def _writer_loop(self, state: ConnectionState) -> None:
        """Drain one connection's outbox in order: encrypt, then write.

        One writer per socket means per-connection frame ordering is preserved
        without a shared lock, while producers keep running.  Frames are
        plaintext JSON strings here; encryption is applied immediately before
        the write and has no opt-out.

        Args:
            state: The connection whose outbox this task owns.
        """
        ws = state.ws
        while True:
            item = await state.outbox.get()
            if item is None or state.closed:
                return
            payload, already_encrypted = item
            try:
                frame = payload if already_encrypted else encrypt(payload)
            except Exception as exc:
                logger.error("[_writer] Failed to encrypt frame: %s", exc)
                continue
            try:
                await ws.send(frame)
            except Exception as exc:
                logger.warning("[_writer] Failed to send frame: %s", exc)
                state.closed = True
                # The peer is gone; stopping the run is cheaper than leaving
                # a task streaming into a closed socket.  Scoped to this
                # connection so a dead client cannot cancel another's run.
                self._cancel_current_task(state)
                return

    async def _send_direct(self, state: ConnectionState, payload: str, msg_type: str) -> None:
        """Write one already-serialised frame without the writer pipeline.

        Only used for connections that have no writer task (detached or
        placeholder states).  The lock keeps concurrent fallback senders from
        interleaving their writes.

        Args:
            state: Target connection.
            payload: Serialised (plaintext) frame.
            msg_type: Frame type, for diagnostics.
        """
        await state.send_lock.acquire()
        try:
            await state.ws.send(encrypt(payload))
        except Exception as exc:
            logger.warning("[_send] Failed to send %s: %s", msg_type, exc)
            state.closed = True
            self._cancel_current_task(state)
        finally:
            state.send_lock.release()

    def _fanout(self, msg_type: str, build_kwargs=None, on_error=None, **kwargs) -> None:
        """Send one frame to every live connection, concurrently and untracked.

        This is the only sanctioned way for server-side code that runs *outside*
        any connection task (gateway status changes, automation progress,
        session mutations, plugin events) to reach the clients.  It exists so
        such code never reaches for the ambient connection: each target's own
        :class:`ConnectionState` is addressed explicitly, so a frame for
        connection B is encoded under B's state and never blocks behind A's
        write lock.

        Args:
            msg_type: The server message type to broadcast.
            build_kwargs: Optional callable taking a
                :class:`ConnectionState` and returning that connection's
                payload kwargs, merged over *kwargs*.  Use it when the payload
                differs per connection (for example a session list scoped to
                each connection's own workspace).
            on_error: Optional callable taking the failing socket, invoked
                when a send cannot even be scheduled so the caller can prune it.
            **kwargs: Payload fields shared by every recipient.

        Returns:
            None.
        """
        for state in self._connections_state.states():
            if state.ws is None:
                continue
            payload = dict(kwargs)
            if build_kwargs is not None:
                try:
                    payload.update(build_kwargs(state))
                except Exception as exc:
                    logger.warning("[fanout] %s payload build failed for %s: %s",
                                   msg_type, state.ws_id, exc)
                    continue
            try:
                _t = asyncio.ensure_future(self._send(state.ws, msg_type, **payload))
            except Exception as exc:
                logger.warning("[fanout] %s schedule failed: %s", msg_type, exc)
                if on_error is not None:
                    with contextlib.suppress(Exception):
                        on_error(state.ws)
                continue
            if _t is None:
                continue
            # Track against the *target* connection so the task is cancelled
            # when that client disconnects, not when some other one does.
            state.tasks.add(_t)
            _t.add_done_callback(state.tasks.discard)

    def _drop_connection(self, ws: Any) -> None:
        """Remove a dead socket from the connection registry.

        Args:
            ws: The socket that failed.

        Returns:
            None.
        """
        with contextlib.suppress(ValueError):
            self._connections.remove(ws)
        self._connections_state.unregister(ws)

    def _spec_engine_for(self, session_or_sid: Any) -> EncreSpecEngine:
        """Return the spec engine owned by *session_or_sid* (created on first use).

        Spec state (parsed document, approve/reject decisions) must never
        leak across sessions, so every session owns its engine instead of
        sharing a connection-wide singleton.

        Args:
            session_or_sid: A session wrapper exposing ``session_id``, or a
                raw session id string, or ``None`` for the anonymous bucket.

        Returns:
            The :class:`encre.spec.EncreSpecEngine` for that session.
        """
        sid = getattr(session_or_sid, "session_id", None) or session_or_sid or ""
        engine = self._spec_engines.get(sid)
        if engine is None:
            engine = EncreSpecEngine()
            self._spec_engines[sid] = engine
        return engine

    def _resolve_spec_engine(self, session_id: str | None):
        """Resolve the spec engine bound to *session_id*.

        The spec engine is a per-loop collaborator; sharing one singleton
        across sessions let one session's approve/reject state bleed into
        another session's approval gate.  Prefer the loop's own engine
        (wired at run time from the per-session registry) so approvals stay
        scoped; fall back to the session's registry entry (creating it if
        needed) when no live session or loop is available.

        Args:
            session_id: The session to resolve, or ``None`` to use the
                connection's current session.

        Returns:
            The :class:`EncreSpecEngine` for that session.
        """
        sid = session_id or self._current_session_id
        if sid:
            # ``get_session`` is the only accessor that yields a SessionInfo;
            # ``list_sessions`` returns renderer dicts, and reading
            # ``.session_id`` off those raised AttributeError -- which killed
            # every spec/plan approval before it reached the loop.
            info = self._manager.get_session(sid)
            agent = getattr(info, "agent", None) if info is not None else None
            loop = getattr(agent, "loop", None) if agent is not None else None
            if loop is not None and getattr(loop, "spec_engine", None) is not None:
                return loop.spec_engine
        return self._spec_engine_for(sid or "")

    def _resolve_loop(self, session_id: str | None):
        """Resolve the live agent loop bound to *session_id* (or None).

        Used by the review handlers to wake a blocked ``submit_review``
        tool call via ``loop.resolve_review`` without leaking state across
        sessions.
        """
        sid = session_id or self._current_session_id
        if not sid:
            return None
        info = self._manager.get_session(sid)
        if info is None:
            return None
        agent = getattr(info, "agent", None)
        return getattr(agent, "loop", None) if agent is not None else None

    async def _send_session_mode(self, ws, session) -> None:
        """Send mode_changed for the session's persisted mode (if any).

        Always send, even when the mode is empty, otherwise the frontend
        keeps the previous session's mode chip visible (stale state) when
        switching to a session that has no mode active.

        Mode is persisted on the EncreSession (``agent.session.metadata``),
        not on the ``SessionInfo`` wrapper, so we read from the underlying
        session object.

        Args:
            ws: The connection to send on.
            session: The session whose mode is being reported.

        Returns:
            None.
        """
        try:
            es = getattr(session, "agent", None)
            es = getattr(es, "session", None) or session
            mode = (es.metadata.get("slash_command_mode") if hasattr(es, 'metadata') else None) or ""
            await self._send(ws, "mode_changed", mode=mode, session_id=session.session_id)
        except Exception:
            pass

    async def _apply_mode(self, ws, session, mode: str) -> str:
        """Apply a slash-command mode transition through the single entry point.

        Normalises ``mode`` (only ``""``/``"plan"``/``"spec"`` survive) and
        drives it through ``loop.set_mode`` so ``config.slash_command_mode``,
        the ``session.metadata`` mirror, and the derived ``plan_mode_active``
        flag never disagree.  Then broadcasts ``mode_changed`` so the desktop
        toolbar chip and exit button reflect the new mode, and -- only when
        the plan-active state actually changed -- ``plan_mode_changed`` so the
        plan proposals panel stays in sync.  Returns the normalised mode.

        Args:
            ws: The connection to broadcast on.
            session: The session whose mode is changing.
            mode: The requested mode string; anything outside the valid set is
                treated as clearing the mode.

        Returns:
            The normalised mode string.
        """
        valid = ("", "plan", "spec")
        mode = mode if mode in valid else ""
        was_plan = session.agent.loop.plan_mode_active
        # The metadata mirror is the authoritative persisted mode -- it is
        # what ``session_ready`` / ``mode_changed`` replay on a reopen.  The
        # in-memory config can lag after a disk reload (a fresh cloned config
        # starts empty and only catches up on the next run echo), so reading
        # the previous mode from config alone let a close on a freshly
        # reopened session skip the pending-review dismissal below and leave
        # the card rebuildable on the next switch back.
        _es = getattr(session.agent, "session", None)
        _meta = getattr(_es, "metadata", None) or {}
        prev_mode = (_meta.get("slash_command_mode")
                     or getattr(session.agent.config, "slash_command_mode", "")
                     or "")
        session.agent.loop.set_mode(mode)
        now_plan = session.agent.loop.plan_mode_active
        logger.info("[set_mode] mode applied: '%s' session=%s plan_mode_active=%s->%s",
                    mode, session.session_id[:8], was_plan, now_plan)
        await self._send(ws, "mode_changed", mode=mode, session_id=session.session_id)
        # Only send the plan panel signal when the derived flag actually
        # flipped, so an unrelated mode change does not force a re-render.
        if was_plan != now_plan:
            await self._send(ws, "plan_mode_changed",
                             active=now_plan, session_id=session.session_id)
        # Closing (or switching away from) a plan/spec mode abandons any
        # review still pending on it.  Without this the persisted record
        # kept rebuilding the review card on every session reopen, so a
        # closed mode looked like it "came back" after switching sessions.
        # Resolving it here also wakes a submit_review blocked on the loop,
        # if any, so the turn does not wait on an abandoned review forever.
        if prev_mode in ("plan", "spec") and mode != prev_mode:
            await self._dismiss_pending_review(ws, session, prev_mode)
        # Persist immediately so the applied mode survives eviction, reload
        # and restart.  Without this the on-disk copy keeps the stale mode
        # and re-broadcasts it when the session is opened again, making a
        # closed mode reappear in the frontend.
        try:
            await self._manager._save_session_async(session)
        except Exception:
            logger.debug("[set_mode] failed to persist mode change", exc_info=True)
        return mode

    async def _dismiss_pending_review(self, ws, session, prev_mode: str) -> None:
        """Resolve a still-pending plan/spec review because its mode closed.

        Marks the newest pending record rejected in the session metadata
        (through ``loop.resolve_review``, which also wakes a blocked
        ``submit_review``), then tells the client to drop the review card
        immediately instead of waiting for the next session reload.

        Args:
            ws: The connection to notify.
            session: The session whose mode was closed.
            prev_mode: The mode being left (``"plan"`` or ``"spec"``).

        Returns:
            None.
        """
        try:
            es = getattr(session, "agent", None)
            es = getattr(es, "session", None) or session
            reviews = es.metadata.get("reviews") or []
            pending = next(
                (r for r in reversed(reviews)
                 if isinstance(r, dict) and r.get("status") in ("review", "draft")),
                None,
            )
            if pending is not None:
                loop = getattr(session.agent, "loop", None)
                if loop is not None:
                    loop.resolve_review(
                        pending.get("review_id", ""), False,
                        feedback="Mode closed by user",
                    )
            # Plan reviews and spec reviews share the plan-card slot; spec
            # mode additionally owns a dedicated card driven by spec state.
            await self._send(ws, "plan_review", review=None, status="rejected",
                             session_id=session.session_id)
            if prev_mode == "spec":
                await self._send(ws, "spec_update", spec=None,
                                 session_id=session.session_id)
        except Exception:
            logger.debug("[set_mode] failed to dismiss pending review", exc_info=True)

    def _restore_persisted_mode(self, session) -> None:
        """Re-apply a session's persisted slash-command mode after reload.

        A resumed or freshly-created session carries its mode in metadata but
        the agent's ``config.slash_command_mode`` starts at the default.  Drive
        it through ``loop.set_mode`` so the string, metadata mirror, and
        derived ``plan_mode_active`` flag all agree before the next run.

        Args:
            session: The session to restore.

        Returns:
            None.
        """
        try:
            es = getattr(session, "agent", None)
            es = getattr(es, "session", None) or session
            mode = es.metadata.get("slash_command_mode", "") or ""
            session.agent.loop.set_mode(mode)
        except Exception:
            logger.debug("failed to restore persisted mode", exc_info=True)

    async def _send_session_command(self, ws, session) -> None:
        """Send command_changed for the session's persisted command (if any).

        Unlike mode, nothing is sent when no command is active: the frontend
        clears its chip on session switch, so an absent command needs no
        explicit frame.

        Args:
            ws: The connection to send on.
            session: The session whose command is being reported.

        Returns:
            None.
        """
        try:
            es = getattr(session, "agent", None)
            es = getattr(es, "session", None) or session
            cmd = (es.metadata.get("active_command") if hasattr(es, 'metadata') else None)
            if cmd and cmd.get("name"):
                await self._send(ws, "command_changed", command=cmd,
                                 session_id=session.session_id)
        except Exception:
            pass

    async def _apply_command(self, ws, session, name: str, prompt: str = "",
                             icon: str = "", title: str = "") -> None:
        """Apply a slash-command activation or clear through the single entry point.

        Stores the command (``{name, prompt, icon, title}``) via
        ``loop.set_command`` so ``config.active_command`` and the
        ``session.metadata`` mirror stay consistent, then broadcasts
        ``command_changed`` so the desktop command chip reflects the new
        state.  An empty ``name`` clears the active command.  A command is
        independent of the mode (plan/spec) -- both may be active at once.

        Args:
            ws: The connection to broadcast on.
            session: The session whose command is changing.
            name: Command name; empty clears the active command.
            prompt: The command's sticky prompt text.
            icon: Optional command icon.
            title: Optional command title.

        Returns:
            None.
        """
        name = (name or "").strip()
        if name:
            session.agent.loop.set_command(name, prompt, icon=icon, title=title)
        else:
            session.agent.loop.clear_command()
        cmd = session.agent.config.active_command
        logger.info("[set_command] command applied: '%s' session=%s",
                    name or "(cleared)", session.session_id[:8])
        await self._send(ws, "command_changed", command=cmd,
                         session_id=session.session_id)

    def _restore_persisted_command(self, session) -> None:
        """Re-apply a session's persisted slash command after reload.

        Mirrors :meth:`_restore_persisted_mode`: the persisted command lives
        in ``session.metadata["active_command"]`` but ``config.active_command``
        starts at ``None``.  Drive it through ``loop.set_command`` so the
        in-memory mirror agrees before the next run re-injects the block.

        Args:
            session: The session to restore.

        Returns:
            None.
        """
        try:
            es = getattr(session, "agent", None)
            es = getattr(es, "session", None) or session
            cmd = es.metadata.get("active_command") or {}
            if cmd.get("name"):
                session.agent.loop.set_command(
                    cmd.get("name", ""),
                    cmd.get("prompt", ""),
                    icon=cmd.get("icon", ""),
                    title=cmd.get("title", ""),
                )
        except Exception:
            logger.debug("failed to restore persisted command", exc_info=True)

    @staticmethod
    def _renderer_session_messages(session: Any) -> list[dict[str, Any]]:
        """Return visible history without dropping renderer-only tool IDs.

        Model-facing compaction blocks (summary, active archive, archive
        hint, workspace context) are stripped: the compressed context itself
        must never be shown to the user.  The compact summary position is
        replaced with a lightweight ``compact_marker`` entry that the
        frontend renders as an inline "history compacted" divider.

        The projection lives in :func:`encre.protocol.render.
        renderer_session_messages` so the WebSocket handlers and the headless
        JSONL transport share one definition.

        Args:
            session: The session whose history is projected.

        Returns:
            The list of renderer-safe message dicts.
        """
        return renderer_session_messages(session)

    def _resolve_startup_mode(self) -> str:
        """Resolve startup_session_mode: settings.json takes priority over config default.

        A value that is not one of the three known modes is treated as
        unset so a typo in settings.json cannot produce a broken startup
        instead of the default.

        Returns:
            ``"normal"``, ``"iwork"``, or ``"iclaw"``.
        """
        try:
            from encre.settings_manager import load_settings
            stored = load_settings()
            mode = stored.get("startup_session_mode")
            if mode in ("normal", "iwork", "iclaw"):
                return mode
        except Exception:
            pass
        if self._default_config is not None:
            return getattr(self._default_config, 'startup_session_mode', 'normal')
        return "normal"

    def _resolve_startup_behavior(self) -> str:
        """Resolve startup_session_behavior: settings.json takes priority over config default.

        Returns:
            ``"new"`` when the launcher should open a fresh session, or
            ``"last"`` to resume the most recent one.
        """
        try:
            from encre.settings_manager import load_settings
            stored = load_settings()
            behavior = stored.get("startup_session_behavior")
            if behavior in ("new", "last"):
                return behavior
        except Exception:
            pass
        if self._default_config is not None:
            return getattr(self._default_config, 'startup_session_behavior', 'new')
        return "new"

    def _get_or_create_session(self):
        """Lazily create a session on first use or after a new-session request.

        Creation is the single place where a fresh session inherits the
        operator's persisted preferences: permission settings, MCP servers,
        and any sticky slash-command mode or command. Doing it all here keeps
        every other call site free of bootstrap logic.

        Returns:
            The current :class:`SessionInfo` wrapper for this connection.
        """
        if self._info is None:
            self._info = self._manager.create_session(config=self._default_config)
            self._current_session_id = self._info.session_id
            # Tag with current channel so _list_all_sessions groups it correctly
            self._info.agent.session.metadata["channel"] = "iwork" if self._workspace_path else "normal"
            # Restore permission_settings from persisted settings
            try:
                from encre.settings_manager import load_settings
                stored = load_settings()
                pset = stored.get("permission_settings")
                if pset and hasattr(self._info.agent.config, "permission_settings"):
                    self._info.agent.config.permission_settings = pset
                    if hasattr(self._info.agent, "safety"):
                        tools_dict = {}
                        caps_dict = {}
                        for k, v in pset.items():
                            # Anything outside the four known verdicts is
                            # ignored so a stale settings file cannot install
                            # an unrecognised policy.
                            if v not in ("default", "allow", "deny", "ask"):
                                continue
                            if k in ("network", "file", "bash_io", "docker", "browser", "workflow", "git", "deploy", "desktop", "database", "misc", "mcp"):
                                caps_dict[k] = v
                            else:
                                tools_dict[k] = v
                        self._info.agent.safety.set_policies(tools_dict, caps_dict)
            except Exception:
                pass
            # Load MCP servers from the canonical mcp.json
            try:
                from encre.tools.mcp_manager import default_mcp_config_path
                mcp_path = default_mcp_config_path()
                servers = self._load_mcp_servers(mcp_path)
                if servers:
                    self._info.agent.config.mcp_servers = servers
            except Exception:
                pass
            # Re-apply any persisted slash-command mode so config and the
            # derived ``plan_mode_active`` flag are correct before the first run.
            self._restore_persisted_mode(self._info)
            # Re-apply any persisted slash command so its sticky prompt
            # block is re-injected on the next run.
            self._restore_persisted_command(self._info)
        return self._info

    @staticmethod
    def _build_tools_info(info: Any) -> dict[str, Any]:
        """Snapshot of tool catalog for the client UI.

        Exposes the harness builtin core tools, the full active payload the
        model sees (registration IS activation: every registered tool), and
        a category breakdown so the UI can render a discovery panel.

        Args:
            info: The session whose agent supplies the tool state.

        Returns:
            A dict with ``base``, ``unlocked``, ``active``,
            ``by_category``, and ``total_available``. All fields are empty
            on failure so the panel renders blank instead of erroring.
        """
        try:
            from encre.tools.discovery import BASE_TOOLS
            discovery = info.agent.loop.discovery
            session_id = info.agent.session.id
            tools_map = info.agent.tool_registry.list_tools()
            return {
                "base": sorted(BASE_TOOLS),
                # Kept for UI compatibility: with register-as-activate there
                # is no per-session unlock state, so this is always empty.
                "unlocked": [],
                "active": discovery.get_active_tool_names(session_id),
                "by_category": discovery.list_by_category(),
                "total_available": len(tools_map),
            }
        except Exception as exc:
            logger.warning("Failed to build tools_info: %s", exc)
            return {
                "base": [],
                "unlocked": [],
                "active": [],
                "by_category": {},
                "total_available": 0,
            }

    def _build_config_data(self, info) -> dict[str, Any]:
        """Build the full config_data payload (CPU-heavy, run in a worker).

        Build the snapshot that the client needs to render its model
        selector, settings, and configuration panels. It is the single
        source of truth for that data, so both the explicit config request
        and the unified :meth:`_push_all_data` push produce identical
        payloads from here.

        Args:
            info: The session the snapshot is built from.

        Returns:
            The config payload dict.
        """
        cd = info.agent.config.to_dict(encrypt_api_keys=False)
        se_url = cd.get("default_search_engine_url", "")
        if se_url:
            # The search engine is a process-wide default, so apply it as a
            # side effect while building config data.
            set_search_engine_url(se_url)
        if "models" in cd:
            cd["models"] = _inject_context_windows(list(cd["models"]))
        cd["tools_info"] = self._build_tools_info(info)
        cd["model_catalog"] = catalog_payload()
        cd["mcp_catalog"] = mcp_catalog_payload()
        if "sub_agents" in cd:
            cd["sub_agents"] = [
                sa for sa in cd["sub_agents"] if not sa.get("hidden", False)
            ]
        # Spec state is per-session: read the owning session's engine, never
        # a shared one. Sharing an engine here would leak spec
        # approve/reject state into every other session's config snapshot.
        current_spec = self._spec_engine_for(info).current_spec
        cd["spec"] = current_spec.to_dict() if current_spec else None
        # Tag the snapshot with the session it was built from so the frontend
        # can scope session-bound fields (active_command) instead of applying
        # them globally (which resurrected stale chips across sessions).
        cd["session_id"] = info.session_id
        cd["slash_commands"] = get_slash_command_defs(
            info.agent.command_registry
        )
        # Plugin-contributed slash commands ride the same frame so the
        # renderer's existing applyServerCommands path picks them up with
        # zero extra plumbing (the host collected them but nothing ever
        # shipped them to the frontend).
        registry = getattr(info.agent, "plugin_registry", None)
        plugin_host = getattr(registry, "host", None) or getattr(
            info.agent, "_plugin_host", None
        )
        plugin_cmds = getattr(plugin_host, "command_entries", None) or []
        if plugin_cmds:
            cd["slash_commands"] = [
                *cd["slash_commands"],
                *[
                    {
                        "name": str(c.get("name", "")).strip(),
                        "title": str(c.get("title") or c.get("name", "")),
                        "description": str(c.get("description", "")),
                        "kind": str(c.get("kind", "prompt")),
                        "icon": str(c.get("icon", "")),
                        "argument_hint": str(c.get("argument_hint", "")),
                        "source": "plugin",
                        "has_prompt_body": bool(c.get("prompt")),
                        "prompt": str(c.get("prompt", "")),
                        "file_path": "",
                        "availability": list(c.get("availability", []) or []),
                        "plugin": str(c.get("plugin", "")),
                    }
                    for c in plugin_cmds
                    if isinstance(c, dict) and str(c.get("name", "")).strip()
                ],
            ]
        cd["custom_slash_commands"] = load_custom_slash_commands()
        cd["keybinds"] = load_keybinds()
        # Ship the session's active command (if any) so the frontend can
        # render the command chip on connect or config refresh.
        cd["active_command"] = (
            getattr(info.agent.config, "active_command", None)
            or info.agent.session.metadata.get("active_command")
        )
        return cd

    async def _push_all_data(self, ws) -> None:
        """Unified full-data push (config+models, sessions, workspaces).

        Called on connect and after every mode, config, or session change
        so the frontend store is refreshed from one consistent snapshot --
        panels never pull their own slices.  Each plane is guarded so a
        failure in one does not blank the others.

        The planes run **concurrently**: every blocking builder executes in
        a worker thread, so a slow plane (a model listing that takes
        its full five-second budget) delays only itself and never the rest
        of the snapshot.  Sends are ordered where semantics demand it:
        ``config_data`` goes out before the session planes because the
        renderer derives its channel view from the config snapshot's
        ``workspace_mode``; everything else streams as soon as it is built.

        Args:
            ws: The connection the snapshot is pushed to.

        Returns:
            None.

        Note:
            Every plane is wrapped in its own ``try`` because the planes
            read different backends (disk, session store, live model APIs).
            A failing model listing must not suppress the session list.
            Builders are passed as zero-arg callables and evaluated lazily
            inside their own plane: a tuple literal of pre-built payloads
            would evaluate every builder serially on the event loop before
            the first frame could be sent.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        channel = "iwork" if self._workspace_path else "normal"

        async def _plane(coro_factory, *, level: int = logging.WARNING) -> None:
            """Run one plane builder + send, isolated from its siblings.

            Args:
                coro_factory: Zero-arg callable returning the plane's
                    awaitable (build + send).
                level: Log level for a failed plane -- boot-critical planes
                    warn, best-effort ones (models, static snapshots) debug.
            """
            try:
                await coro_factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.log(level, "[push_all_data] plane skipped: %s", exc)

        async def _push_config() -> None:
            """Build the config snapshot payload (boot-critical plane).

            The send is deliberately NOT part of this coroutine: the renderer
            derives its channel view (``normal``/``iwork``) from this frame's
            ``workspace_mode``, so it must be SENT before the session planes
            -- a ``sessions_list`` that overtakes it is silently dropped by
            the renderer's channel filter and an iwork user boots with an
            empty sidebar.  Builds stay parallel; only the send order is
            pinned.
            """
            # Config data is built in a worker thread: serialising config and
            # resolving model context windows is synchronous and slow enough to
            # block the loop if run inline.
            config_data = await asyncio.to_thread(self._build_config_data, info)
            available = await self._build_skills_list(info)
            config_data["available_skills"] = available
            config_data["workspace_mode"] = channel
            config_data["workspace_path"] = self._workspace_path
            _apply_session_files_dir(config_data, info.session_id)
            return config_data

        async def _build_sessions_payload() -> tuple[list[Any], list[Any]]:
            """Build both channel session views in one thread hop.

            One listing pass produces both planes: the channel-scoped
            ``sessions_list`` row set is exactly the matching slice of the
            dual-channel ``sessions_all`` payload, so the previous separate
            ``_list_all_sessions`` call (a full re-walk of every session
            index) is gone.

            Returns:
                A ``(normal, iwork)`` tuple of session row lists.
            """
            def _build_both():
                """Build the normal and iwork session lists in one thread.

                Returns:
                    A ``(normal, iwork)`` tuple of session row lists.
                """
                return (
                    self._list_all_sessions(channel_filter="normal"),
                    self._list_all_sessions(channel_filter="iwork"),
                )
            return await asyncio.to_thread(_build_both)

        async def _push_workspaces() -> None:
            """Build and send the workspace list (disk read, worker thread)."""
            def _build():
                """Load workspaces and annotate them with session counts.

                Returns:
                    The renderer-ready workspace row list.
                """
                return _workspaces_with_session_counts(_load_workspaces())
            workspaces = await asyncio.to_thread(_build)
            await self._send(ws, "workspaces_list", workspaces=workspaces)

        async def _push_workspace_config() -> None:
            """Push the active workspace's config (iwork connections only)."""
            if not self._workspace_path:
                return
            # Push the active workspace's config early so the model selector can
            # apply model-pool isolation without waiting for the settings panel.
            cfg = await asyncio.to_thread(_load_workspace_config, self._workspace_path)
            files = await asyncio.to_thread(_workspace_context_files, self._workspace_path)
            await self._send(ws, "workspace_config", path=self._workspace_path,
                             config=cfg, files=files)

        async def _push_models() -> None:
            """Push the live model list for the active backend."""
            backend = info.agent.loop.backend
            models = []
            if backend is not None:
                try:
                    # Bounded wait: a slow provider must not stall the whole
                    # snapshot, and an empty model list is a valid reply.
                    models = await asyncio.wait_for(backend.list_models(), timeout=5)
                except asyncio.TimeoutError:
                    models = []
            await self._send(ws, "models_list", models=models)

        async def _push_memory_planes() -> None:
            """Build and send both memory planes from ONE loaded store.

            The memory graph (nodes + links) is loaded from disk once and
            handed to both builders; loading it twice per push used to read
            and parse the whole memory directory two times over.
            """
            def _build_memory_payloads():
                """Load the store once and run both memory builders.

                Returns:
                    ``(entries, graph)`` for the ``memory_list`` and
                    ``memory_graph`` frames.
                """
                from encre.memdir.layers import MemoryLayerStore
                try:
                    store = MemoryLayerStore()
                except Exception:
                    store = None
                return (
                    self._build_memory_list(store=store),
                    self._build_memory_graph(store=store),
                )
            entries, graph = await asyncio.to_thread(_build_memory_payloads)
            await self._send_plane(ws, "memory_list", "entries", entries)
            await self._send(ws, "memory_graph", graph=graph)

        # ---- Extended snapshot planes ------------------------------------
        # Every static data domain the frontend panels subscribe to is
        # pushed in the same snapshot, so no panel ever has to pull its own
        # slice after a mode switch.  Each builder runs in a worker thread
        # (they all do disk I/O) and each plane is independently guarded
        # (one failure must not blank the rest).  The middle tuple element
        # is the payload kwarg name: list-returning builders cannot be
        # splatted with ``**`` directly.
        static_planes: tuple[tuple[str, str, Any], ...] = (
            ("global_rules_list", "rules", self._build_global_rules_list),
            ("profile_data", "profile", self._build_profile_data),
            ("library_tree", "nodes", self._build_library_tree),
            ("usage_stats", "stats", self._build_usage_stats),
            ("project_rules_list", "rules", self._build_project_rules_list),
            ("project_hooks_list", "hooks", self._build_project_hooks_list),
            ("automation_jobs_list", "jobs", self._build_automation_jobs),
            # The run history is pushed alongside the jobs list so the
            # automation view is fully populated from the boot snapshot:
            # entering the view must render from state, not fire a per-entry
            # refill round-trip. Same builder the automation_get_history
            # request handler uses, so the payload shape is identical.
            ("automation_job_history", "history", self._build_automation_history),
        )

        async def _push_static(name: str, kwarg: str, builder: Any) -> None:
            """Build one static plane in a worker thread and send it.

            Args:
                name: The frame type the payload is sent as.
                kwarg: The payload field name the frame carries.
                builder: Zero-arg callable returning the payload value.
            """
            payload = await asyncio.to_thread(builder)
            await self._send_plane(ws, name, kwarg, payload)

        # Launch the boot-critical builds first so their worker threads start
        # before anything else contends for workers, then stream every
        # order-independent plane concurrently.  The mode-dependent planes
        # (config_data, sessions_list, sessions_all) are awaited and SENT in
        # that order afterwards: builds stay parallel, but the renderer must
        # learn its channel view from ``config_data`` before the channel-
        # filtered ``sessions_list`` lands, or an iwork boot drops the
        # session list and opens with an empty sidebar.  ``config_data``
        # keeps a warning-level failure log because a blank config snapshot
        # is the one failure the frontend cannot work around.
        config_task = asyncio.ensure_future(_push_config())
        sessions_task = asyncio.ensure_future(_build_sessions_payload())

        try:
            config_data = await config_task
            await self._send(ws, "config_data", config=config_data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[push_all_data] config_data failed: %s", exc)
        try:
            normal, iwork = await sessions_task
            # Dual-channel snapshot feeds the tray popup (and the global
            # search cache) through the same unified push -- the tray never
            # needs a separate list_all_sessions request.
            await self._send_session_snapshot(
                ws, channel, (iwork if channel == "iwork" else normal))
            await self._send_sessions_all_sharded(ws, normal, iwork)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[push_all_data] sessions failed: %s", exc)
        # Secondary planes start only after the boot-critical frames are on
        # the wire. They used to be built concurrently, competing with
        # config/sessions for worker threads and the GIL during the exact
        # window the splash waits on -- deferring their start shortens
        # time-to-first-data without changing what any panel receives.
        # The short pause leaves the renderer a clean window to paint its
        # first interactive frame before nine disk-bound builders start.
        await asyncio.sleep(0.15)
        await asyncio.gather(
            _plane(_push_workspaces),
            _plane(_push_workspace_config, level=logging.DEBUG),
            _plane(_push_models, level=logging.DEBUG),
            _plane(_push_memory_planes),
            *(
                _plane(lambda n=name, k=kwarg, b=builder: _push_static(n, k, b),
                       level=logging.DEBUG)
                for name, kwarg, builder in static_planes
            ),
            return_exceptions=True,
        )

    async def _send_plane(self, ws, name: str, kwarg: str, payload: Any) -> None:
        """Send one list/dict plane, sharding large lists.

        Any bulk list that can grow with usage goes through the generic shard
        channel instead of one mega-frame, so a large rules/library/automation
        payload never becomes a single long JSON-parse task in the renderer.

        Args:
            ws: Target socket.
            name: Frame type / topic leaf.
            kwarg: Field name the renderer expects on the assembled event.
            payload: Plane payload (list or dict).
        """
        if isinstance(payload, list) and len(payload) > 200:
            await self._send_topic_shards(
                ws, f"plane:{name}", {"kwarg": kwarg}, payload,
            )
            return
        await self._send(ws, name, **{kwarg: payload})

    async def _send_config_patch(self, ws, info, before: dict[str, Any]) -> None:
        """Diff the config snapshot against *before* and push only the delta.

        The post-change snapshot is built through the same
        :meth:`_build_config_data` path the full push uses, so a patch field
        is byte-identical to what a ``config_data`` snapshot would carry and
        the frontend applies both through one code path.  An empty diff sends
        nothing: a no-op configure request must not wake a single subscriber.

        Args:
            ws: The connection to push the patch to.
            info: The session the snapshot is built from.
            before: The ``_build_config_data`` snapshot captured before the
                caller mutated the config.

        Returns:
            None.

        Note:
            This replaces the previous full :meth:`_push_all_data` call on
            the configure path: a config edit must not re-walk every session
            index, memory store and telemetry file just to refresh the
            panels the edited keys actually touch.  Workspace open/close
            keeps the full snapshot on purpose (it switches context, not a
            setting).
        """
        try:
            after = await asyncio.to_thread(self._build_config_data, info)
            patch = {
                key: value
                for key, value in after.items()
                if key not in before or before[key] != value
            }
            # ``models`` and ``active_model_index`` are ONE logical snapshot:
            # a consumer that receives a new model list without an index has
            # to guess, and the historical guess was "0" -- which silently
            # activated the first model on any edit that left the index
            # untouched (e.g. picking a reasoning level).  Keep the pair
            # together instead of splitting it across patches.
            if "models" in patch and "active_model_index" not in patch:
                patch["active_model_index"] = after.get("active_model_index", 0)
            if not patch:
                logger.debug("[config_patch] no effective change, nothing to push")
                return
            # Tag the patch with the session it was built from, mirroring the
            # snapshot's session scoping so the frontend can drop patches
            # that describe a session other than the one being viewed.
            patch["session_id"] = info.session_id
            await self._send(ws, "config_patch", config=patch)
        except Exception as exc:
            # Fallback: a failed diff must not leave the frontend stale, so
            # degrade to the full snapshot rather than skipping the refresh.
            logger.warning("[config_patch] diff failed (%s), falling back to full push", exc)
            await self._push_all_data(ws)

    def _cancel_current_task(self, state: ConnectionState) -> None:
        """Cancel the running agent task for the current session (on disconnect).

        Cancels both the loop and the wrapper task so the run stops promptly
        instead of finishing its current tool call and only then noticing the
        socket is gone.

        Args:
            state: The connection whose work must stop.  Scoped explicitly so
                one client's disconnect never cancels another client's run.

        Returns:
            None.
        """
        if state.iclaw_task and not state.iclaw_task.done():
            state.iclaw_task.cancel()
        if state.current_session_id:
            sess = self._manager.get_session(state.current_session_id)
            if sess and sess.agent_task and not sess.agent_task.done():
                sess.agent.loop.cancel()
                sess.agent_task.cancel()

    def broadcast_gateway_status(self, status: dict) -> None:
        """Called by :class:`encre.gateway.run.GatewayRunner` when adapter state changes.

        Fans the status out to every open connection. Each send is spawned as
        a tracked task rather than awaited, because this is called from the
        gateway's status callback path which must not block on N slow clients.
        Connections that fail synchronously are removed from the set.

        Args:
            status: The payload from :meth:`GatewayRunner.get_status`.

        Returns:
            None.
        """
        closed = []
        for state in self._connections_state.states():
            try:
                _t = asyncio.ensure_future(self._send(state.ws, "gateway_status", status=status))
                state.tasks.add(_t)
                _t.add_done_callback(state.tasks.discard)
            except Exception:
                closed.append(state.ws)
        for ws in closed:
            self._connections.remove(ws)

    async def _poll_wechat_scan(self, ws, adapter, qrcode_token: str,
                                platform: str = "weixin") -> None:
        """Poll platform QR code status in background and notify frontend.

        Polls once per second. On success it confirms the scan to the client,
        starts the target adapter with the freshly minted credentials, and
        persists them into both the live session config and the default
        config so a later gateway restart reconnects automatically.

        Intermediate states are relayed too: a ``scanned`` signal (WeChat)
        tells the frontend to stop the countdown and restarts the poll
        window, so a slow phone-side confirmation is not cut off by the
        deadline; a server-side ``expired`` signal (QQ bind task) stops the
        poll early and lets the frontend show the refresh overlay.

        Args:
            ws: The connection that initiated the scan.
            adapter: The adapter exposing ``poll_qrcode_status``.
            qrcode_token: The token identifying the pending QR code.
            platform: Registry name of the adapter being bound (``weixin``,
                ``qqbot``, ...). Selects the credential mapping.

        Returns:
            None.

        Note:
            The task registers itself in ``_tasks`` and removes itself in
            ``finally`` so a long poll cannot accumulate zombie entries.
        """
        # QQ bind tasks carry a server-side expiry signal, so the poll window
        # can safely outlive the frontend countdown; the WeChat QR has none,
        # so its window stays at the QR validity estimate.
        deadline = 300.0 if platform == "qqbot" else 120.0
        start = time.monotonic()
        scan_notified = False
        try:
            while time.monotonic() - start < deadline:
                result = await adapter.poll_qrcode_status(qrcode_token)
                # A falsy result means "not scanned yet"; keep polling.
                if not result:
                    await asyncio.sleep(1)
                    continue
                if result.get("scanned"):
                    # Scanned, confirmation pending on the phone: stop the
                    # frontend countdown once and extend the poll window.
                    if not scan_notified:
                        scan_notified = True
                        await self._send(ws, "wechat_scan_result",
                            qrcode_url="", success=True, message="",
                            scan_detected=True)
                    start = time.monotonic()
                    await asyncio.sleep(1)
                    continue
                if result.get("expired"):
                    # Server-side QR expiry (QQ bind task): stop polling and
                    # let the frontend show the refresh overlay.
                    await self._send(ws, "wechat_scan_result",
                        qrcode_url="", success=False, message="",
                        qr_expired=True)
                    return
                if platform == "qqbot":
                    credentials = {
                        "app_id": result.get("app_id", ""),
                        "app_secret": result.get("app_secret", ""),
                        "user_openid": result.get("user_openid", ""),
                    }
                    adapter_cfg = {
                        "app_id": credentials["app_id"],
                        "client_secret": credentials["app_secret"],
                        "enabled": True,
                    }
                elif platform == "wecom":
                    credentials = {
                        "bot_id": result.get("bot_id", ""),
                        "secret": result.get("secret", ""),
                    }
                    adapter_cfg = {
                        "bot_id": credentials["bot_id"],
                        "secret": credentials["secret"],
                        "enabled": True,
                    }
                else:
                    credentials = {
                        "ilink_bot_id": result.get("ilink_bot_id", ""),
                        "bot_token": result.get("bot_token", ""),
                        "baseurl": result.get("baseurl", ""),
                        "ilink_user_id": result.get("ilink_user_id", ""),
                    }
                    adapter_cfg = {
                        "account_id": credentials["ilink_bot_id"],
                        "WEIXIN_ACCOUNT_ID": credentials["ilink_bot_id"],
                        "token": credentials["bot_token"],
                        "base_url": credentials["baseurl"],
                        "WEIXIN_BASE_URL": credentials["baseurl"],
                        "enabled": True,
                    }
                # Persisted shape differs from the live-start shape: the
                # weixin env-mirror keys (WEIXIN_*) are runtime-only.
                if platform in ("qqbot", "wecom"):
                    persist_cfg = dict(adapter_cfg)
                else:
                    persist_cfg = {
                        "account_id": credentials["ilink_bot_id"],
                        "token": credentials["bot_token"],
                        "base_url": credentials["baseurl"],
                        "enabled": True,
                    }
                await self._send(ws, "wechat_scan_result",
                    qrcode_url="", success=True, message="",
                    scan_confirmed=True,
                    credentials=credentials)
                if self._adapter_manager and credentials and all(
                    str(v) for k, v in credentials.items() if k != "user_openid"
                ):
                    # Drop any stale stored config so the new credentials win.
                    if hasattr(self._adapter_manager, "_stored_configs"):
                        self._adapter_manager._stored_configs.pop(platform, None)
                    await self._adapter_manager.start_adapter(platform, adapter_cfg)
                    info = self._info
                    if info and hasattr(info, "agent") and hasattr(info.agent, "config"):
                        info.agent.config.adapter_configs[platform] = dict(persist_cfg)
                        if hasattr(self, "_default_config"):
                            self._default_config.adapter_configs[platform] = dict(persist_cfg)
                        self._persist_config(info)
                return
        except Exception as e:
            logger.warning("[wechat_scan] poll error: %s", e)
        finally:
            task = asyncio.current_task()
            if task:
                self._tasks.discard(task)
                if self._wechat_poll_tasks.get(platform) is task:
                    self._wechat_poll_tasks.pop(platform, None)

    def _persist_config(self, info: Any) -> None:
        """Persist the whole configuration through the single writer.

        ``workspace`` is deliberately cleared: it is a per-session runtime
        value, not persisted state.

        Adapter credentials are synced from the server-wide
        ``_default_config`` before the write.  That config is the canonical
        adapter set -- every connector mutation (a ``configure`` patch, a QR
        bind, an unbind) updates it -- while a session's own
        ``adapter_configs`` can be a stale snapshot, e.g. a session loaded
        from disk whose snapshot predates a connector bound on another
        connection.  Because ``config_store.save_config`` replaces the
        ``adapters`` group wholesale (that is what makes unbinding stick),
        saving a stale snapshot would silently erase every connector it is
        missing from disk -- the "I connected three platforms, only one
        survived" class of bug.  Syncing first makes the saved set the union
        of both, while an unbind (which pops the adapter from
        ``_default_config`` too) still removes it.

        Args:
            info: The session whose config is written to disk.

        Returns:
            None.
        """
        try:
            from encre import config_store
            cfg = info.agent.config
            default_cfg = getattr(self, "_default_config", None)
            if (
                default_cfg is not None
                and default_cfg is not cfg
                and hasattr(default_cfg, "adapter_configs")
                and hasattr(cfg, "adapter_configs")
            ):
                for aid, fields in default_cfg.adapter_configs.items():
                    current = cfg.adapter_configs.get(aid)
                    if current is None:
                        cfg.adapter_configs[aid] = dict(fields)
                    else:
                        current.update(fields)
            config_to_save = replace(cfg, workspace="")
            # A workspace conversation's model belongs to the project, not to
            # the global config. Writing it here would make the next launch's
            # default -- and every other workspace -- inherit this project's
            # choice. Keep the connection default's index instead.
            if (
                (getattr(cfg, "workspace", "") or "").strip()
                and default_cfg is not None
                and 0 <= getattr(default_cfg, "active_model_index", -1) < len(config_to_save.models)
            ):
                config_to_save.active_model_index = default_cfg.active_model_index
            config_to_save.save()
            logger.info("[persist_config] saved to %s", config_store.config_dir())
        except Exception as exc:
            logger.error("[persist_config] Failed to persist config: %s\n%s", exc, traceback.format_exc())

    @staticmethod
    def _persist_mcp_json(_info: Any, servers: list[dict[str, Any]]) -> None:
        """Persist MCP servers to canonical encre mcp.json and ~/.claude/mcp.json.

        Args:
            _info: Unused, retained for the existing call signature.
            servers: Server dicts as edited by the client.

        Returns:
            None.
        """
        try:
            mcp_data: dict[str, dict[str, Any]] = {}
            for srv in servers:
                name = srv.get("name", "")
                if not name:
                    continue
                # Only fields the receiving tool actually consumes are copied,
                # so an unknown client field cannot corrupt the persisted file.
                entry: dict[str, Any] = {
                    "type": srv.get("type", "stdio"),
                }
                if srv.get("type") == "http":
                    if srv.get("url"):
                        entry["url"] = srv["url"]
                    if srv.get("timeout"):
                        entry["timeout"] = srv["timeout"]
                    if srv.get("headers"):
                        entry["headers"] = srv["headers"]
                else:
                    if srv.get("command"):
                        entry["command"] = srv["command"]
                    if srv.get("args"):
                        entry["args"] = srv["args"]
                if srv.get("cwd"):
                    entry["cwd"] = srv["cwd"]
                if srv.get("env"):
                    entry["env"] = srv["env"]
                if srv.get("disabled"):
                    entry["disabled"] = True
                mcp_data[name] = entry

            import json as _json

            payload: dict[str, Any] = {"mcpServers": mcp_data}

            # Canonical encre location (used by mcp_manager.py).  Encrypted
            # at rest: mcp.json carries server env and credentials.  Readers
            # go through encre.secure_io, which also accepts legacy plaintext.
            from encre.tools.mcp_manager import default_mcp_config_path
            from encre.secure_io import write_json
            yim_path = default_mcp_config_path()
            write_json(yim_path, payload)
            logger.info("[persist_mcp_json] saved %d servers to %s", len(mcp_data), yim_path)

            # Claude Code compat location -- must stay plaintext: Claude Code
            # parses this file itself and knows nothing about our crypto.
            claude_dir = os.path.expanduser("~/.claude")
            os.makedirs(claude_dir, exist_ok=True)
            claude_path = os.path.join(claude_dir, "mcp.json")
            with open(claude_path, "w", encoding="utf-8") as f:
                _json.dump(payload, f, ensure_ascii=False, indent=2)
            logger.info("[persist_mcp_json] saved %d servers to %s", len(mcp_data), claude_path)
        except Exception as exc:
            logger.warning("[persist_mcp_json] failed: %s", exc)

    @staticmethod
    def _load_mcp_servers(path: str) -> list[dict[str, Any]]:
        """Load MCP servers from a mcp.json file.

        Tolerates both the canonical ``{"mcpServers": {...}}`` object shape and
        a bare list, and accepts the ``transport`` alias for ``type`` so files
        written by other tools still load. Every failure mode returns ``[]``
        rather than raising, because a broken MCP file must not prevent the
        server from starting.

        Args:
            path: Path to the mcp.json file.

        Returns:
            A list of server dicts compatible with ``EncreConfig.mcp_servers``.
        """
        from encre.secure_io import read_json

        try:
            if not os.path.exists(path):
                return []
            data = read_json(path, default=None)
            if data is None:
                return []
            raw = data.get("mcpServers") or data
            if isinstance(raw, dict):
                servers = []
                for name, cfg in raw.items():
                    entry: dict[str, Any] = {"name": name, **cfg}
                    if "type" not in entry and "transport" in entry:
                        entry["type"] = entry.pop("transport")
                    if "type" not in entry:
                        entry["type"] = "stdio"
                    servers.append(entry)
                return servers
            if isinstance(raw, list):
                return raw
            return []
        except Exception:
            return []
