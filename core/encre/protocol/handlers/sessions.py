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

"""Session domain handlers: list, new, delete, archive, export, rename.

This module owns the client-visible session catalogue and is one of the
mixin families composed into :class:`encre.transport.ws.EncreWSHandler`
(mapped into ``_DISPATCH`` under ``ClientListSessions``,
``ClientNewSession``, ``ClientDeleteSession``, ``ClientArchiveSession``,
``ClientUnarchiveSession``, ``ClientListArchivedSessions``,
``ClientExportSession``, ``ClientExportSessionsBatch``,
``ClientRenameSession`` and ``ClientIclawResume``).

Every operation routes through :class:`encre.server.session_manager.
SessionManager`, which owns the in-memory session table, the on-disk
index under ``data/sessions/index.json`` and the iwork tree under
``data/iwork/<ws_id>/sessions/``.  Sessions that live inside the
gateway's :class:`encre.gateway.event_router.EventRouter` session
manager (IM adapters plus iClaw) are merged in by this module, so the
desktop sidebar is a single view over two independent managers.

The unified snapshot builder :meth:`SessionsHandlers._list_all_sessions`
is shared by the list handler, the broadcast path and the full-data
push, so a session is rendered identically wherever it appears.

Control-flow note: in the original monolithic dispatch chain these
methods used bare ``continue`` to restart the loop; outside the loop
each of those became an explicit ``return``.
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import shutil
from dataclasses import replace
from typing import Any

from encre.config import get_data_dir
from encre.protocol.handlers.workspace_store import (
    _get_workspace_dir,
    _load_workspaces,
    _make_workspace_id,
    _remove_session_from_workspace_indices,
)
from encre.server.protocol import (
    ClientArchiveSession,
    ClientClearAllSessions,
    ClientDeleteSession,
    ClientExportSession,
    ClientExportSessionsBatch,
    ClientIclawResume,
    ClientListAllSessions,
    ClientListArchivedSessions,
    ClientListSessions,
    ClientNewSession,
    ClientRenameSession,
    ClientUnarchiveSession,
)

logger = logging.getLogger("encre.transport.ws")


class SessionsHandlers:
    """Session lifecycle and snapshot mixin.

    Composed into :class:`encre.transport.ws.EncreWSHandler`; every
    method assumes the host provides ``_manager``, ``_info``,
    ``_current_session_id``, ``_workspace_path``, ``_adapter_manager``,
    ``_connections``, ``_tasks``, ``_client_encrypted``, ``_spec_engines``,
    ``_scheduler`` and the shared ``_send`` / ``_get_or_create_session`` /
    ``_broadcast_sessions`` helpers.  The class itself is stateless.

    Two views are maintained in parallel:

    - **Active** -- un-archived sessions in the current channel, fed by
      :meth:`_list_all_sessions` and pushed over ``sessions_list``.
    - **Archived** -- sessions removed from the sidebar but still on
      disk, fed by :meth:`_list_archived_sessions` and pushed over
      ``archived_sessions_list``.

    Disk-heavy paths run inside :func:`asyncio.to_thread` because this
    module and the WS dispatch loop share one event loop; blocking here
    would stall every other request on the same socket.
    """

    # ---- Unified session-list snapshot ---------------------------------

    def _list_all_sessions(self, channel_filter: str | None = None) -> list[dict[str, Any]]:
        """Build the unified session snapshot for one channel view.

        Merges four independent stores so the sidebar never shows a
        partial picture:

        1. Resident sessions from
           :meth:`encre.server.session_manager.SessionManager.list_sessions`.
        2. The on-disk session index via ``query_index`` (covers sessions
           not currently loaded in memory).
        3. The gateway EventRouter's manager and index (IM adapter plus
           iClaw sessions).
        4. Per-workspace index files under
           ``data/iwork/<ws_id>/sessions/index.json`` for the iwork
           channel, plus the global session index when the handler is in
           workspace mode -- ``_manager`` only sees the workspace tree
           in that mode, so the global tree has to be read directly.

        Args:
            channel_filter: The channel to keep (``"normal"`` or
                ``"iwork"``).  ``None`` selects the channel implied by
                the current workspace mode; the tray popup passes an
                explicit value to fetch both groups.

        Returns:
            Session dicts sorted by recency, newest first, each already
            annotated with the workspace attribution the renderer uses
            for icons.

        Invariant:
            The iwork view deliberately spans every registered
            workspace rather than filtering by the active one, so a
            workspace picker never hides history.  Routing channels
            (``automation``, ``sub_agent``), archived sessions, temp
            chats and orphan index rows are all excluded here.
        """
        channel = channel_filter
        if channel is None:
            channel = "iwork" if self._workspace_path else "normal"

        def _matches(s: dict[str, Any]) -> bool:
            """Return ``True`` when a row belongs to the requested view.

            Only the channel is compared.  iwork sessions are not
            filtered by workspace: the sidebar shows every un-archived
            workspace's history at once, each row carrying its owning
            ``workspace_path`` so the renderer can draw the right icon.
            """
            sch = s.get("channel", "normal")
            if sch != channel:
                return False
            return True

        result = [s for s in self._manager.list_sessions() if _matches(s)]
        active_ids = {s["session_id"] for s in result}
        for entry in self._manager.query_index():
            if entry["session_id"] in active_ids:
                continue
            if not _matches(entry):
                continue
            result.append(entry)
            active_ids.add(entry["session_id"])

        # Merge the gateway-managed sessions: IM adapters and iClaw
        # conversations live in the EventRouter's own manager, so both
        # its live list and its on-disk index have to be consulted.
        if self._adapter_manager and self._adapter_manager.router:
            router = self._adapter_manager.router
            for s in router.session_manager.list_sessions():
                if s["session_id"] in active_ids:
                    continue
                if not _matches(s):
                    continue
                result.append(s)
                active_ids.add(s["session_id"])
            for entry in router.session_manager.query_index():
                if entry["session_id"] in active_ids:
                    continue
                if not _matches(entry):
                    continue
                result.append(entry)
                active_ids.add(entry["session_id"])

        # Walk each registered workspace's own session index.  The
        # index may be encrypted at rest, so a payload that does not
        # begin with "{" is attempted as ciphertext first; any decode
        # or parse failure simply skips that workspace rather than
        # aborting the whole listing.
        if channel == "iwork":
            workspaces = _load_workspaces()
            for ws in workspaces:
                ws_id = ws.get("id") or _make_workspace_id(ws["path"])
                ws_dir = _get_workspace_dir(ws_id)
                sess_dir = os.path.join(ws_dir, "sessions")
                idx_file = os.path.join(sess_dir, "index.json")
                if not os.path.isfile(idx_file):
                    continue
                try:
                    with open(idx_file, encoding="utf-8") as f:
                        raw = f.read().strip()
                    if raw and not raw.startswith("{"):
                        from encre.crypto import decrypt as _decrypt
                        with contextlib.suppress(Exception):
                            raw = _decrypt(raw)
                    ws_index = json.loads(raw)
                    if not isinstance(ws_index, dict):
                        continue
                except Exception:
                    continue
                for sid, entry in ws_index.items():
                    if sid in active_ids:
                        continue
                    ech = entry.get("channel", "iwork") or "iwork"
                    if ech in ("automation", "sub_agent"):
                        continue
                    # Skip orphan index rows whose session directory
                    # does not exist in this workspace.  A row written
                    # into the wrong workspace's index before a fix
                    # would otherwise hijack the session's workspace
                    # attribution and render the wrong icon.
                    if not os.path.isdir(os.path.join(sess_dir, sid)):
                        continue
                    # Archived sessions only surface in the archive manager.
                    if entry.get("archived"):
                        continue
                    active_ids.add(sid)
                    ws_path = ws.get("path", "")
                    # Use meta.json's last_message_at as the canonical
                    # "when was this conversation last active" value
                    # instead of the index row, so rendering a sidebar
                    # entry never rewrites the displayed timestamp.
                    from encre.session import EncreSession as _EncreSession
                    fallback_active = entry.get("last_active", 0)
                    last_active = _EncreSession.read_meta_last_active(
                        os.path.join(sess_dir, sid),
                        fallback_active,
                    )
                    result.append({
                        "session_id": sid,
                        "created_at": entry.get("created_at", 0),
                        "last_active": last_active,
                        "state": self._manager.get_session_state(sid),
                        "metadata": {"workspace": ws_path, "workspace_path": ws_path},
                        "preview": entry.get("preview", ""),
                        "name": entry.get("name", ""),
                        "channel": ech,
                        "message_count": entry.get("message_count", 0),
                    })

        # In workspace mode the normal-channel sessions live in the
        # GLOBAL session directory rather than the workspace one, and
        # ``_manager`` only points at the workspace tree, so the global
        # index must be read directly.
        if self._workspace_path:
            # ensure=False is required: index.json is a FILE, and the
            # default ensure=True would mkdir() it, which raises
            # WinError 183 once the file exists.
            global_idx = str(get_data_dir("sessions", "index.json", ensure=False))
            if os.path.isfile(global_idx):
                try:
                    with open(global_idx, encoding="utf-8") as f:
                        raw = f.read().strip()
                    if raw and not raw.startswith("{"):
                        from encre.crypto import decrypt as _decrypt
                        with contextlib.suppress(Exception):
                            raw = _decrypt(raw)
                    g_index = json.loads(raw)
                    if isinstance(g_index, dict):
                        global_sess_dir = str(get_data_dir("sessions"))
                        for sid, entry in g_index.items():
                            if sid in active_ids:
                                continue
                            ech = entry.get("channel", "normal") or "normal"
                            if ech in ("automation", "sub_agent"):
                                continue
                            # Skip orphan rows whose session directory is
                            # not actually in the global dir (same
                            # reasoning as the workspace branch above).
                            if not os.path.isdir(os.path.join(global_sess_dir, sid)):
                                continue
                    # Archived rows only belong to the archive manager.
                            if entry.get("archived"):
                                continue
                            active_ids.add(sid)
                            # Same reasoning as the workspace branch:
                            # always take the canonical last_message_at
                            # from meta.json so clicking a sidebar entry
                            # cannot rewrite the timestamp to "now".
                            from encre.session import EncreSession as _EncreSession
                            fallback_active = entry.get("last_active", 0)
                            last_active = _EncreSession.read_meta_last_active(
                                os.path.join(global_sess_dir, sid),
                                fallback_active,
                            )
                            result.append({
                                "session_id": sid,
                                "created_at": entry.get("created_at", 0),
                                "last_active": last_active,
                                "state": self._manager.get_session_state(sid),
                                "metadata": {},
                                "preview": entry.get("preview", ""),
                                "name": entry.get("name", ""),
                                "channel": ech,
                                "message_count": entry.get("message_count", 0),
                            })
                except Exception:
                    pass

        result.sort(key=lambda s: s.get("last_active", s.get("created_at", 0)), reverse=True)
        # Brand-new sessions with zero messages must still appear in
        # the sidebar (that is part of _make_index_entry's contract),
        # so only routing channels are dropped here -- never empty
        # sessions.
        result = [s for s in result if s.get("channel", "normal") not in ("automation", "sub_agent")]

        # ---- Workspace-channel filter -----------------------------------
        expected_channel = ("iwork" if self._workspace_path else "normal") if channel_filter is None else channel_filter
        result = [s for s in result if s.get("channel", "normal") == expected_channel]

        # ---- Exclude temp chats from the sidebar ------------------------
        result = [s for s in result if not s.get("temp_chat") and not s.get("metadata", {}).get("temp_chat")]

        # ---- Exclude archived sessions (only visible in archive view) ---
        result = [s for s in result if not s.get("archived")]

        # ---- Workspace attribution fallback (icon stability) ------------
        # A session's workspace is resolved from its physical directory,
        # which is the only authoritative source.  In-memory metadata and
        # meta.json can both be missing for legacy or archived sessions,
        # and an unreliable value made the sidebar icon flicker between
        # different list paths for the same conversation.  Any iwork row
        # that still has no workspace path is resolved by mapping the
        # ws_id component of its directory back to a registered path.
        ws_id_to_path: dict[str, str] = {}
        try:
            for _w in _load_workspaces():
                _wid = _w.get("id") or _make_workspace_id(_w["path"])
                ws_id_to_path[_wid] = _w["path"]
        except Exception:
            pass
        for _s in result:
            if _s.get("channel") != "iwork":
                continue
            _meta = _s.get("metadata") or {}
            if _meta.get("workspace_path") or _meta.get("workspace"):
                continue
            _p = None
            try:
                _d = self._manager._session_dir_path(_s["session_id"])
                # <data>/iwork/{ws_id}/sessions/{sid} -- ws_id = parent.parent.name
                _p = ws_id_to_path.get(_d.parent.parent.name)
            except Exception:
                _p = None
            if _p:
                _s["metadata"] = {**_meta, "workspace": _p, "workspace_path": _p}

        return result

    def _list_archived_sessions(self) -> list[dict[str, Any]]:
        """List every archived session across both managers.

        Archived rows are excluded from :meth:`_list_all_sessions`, so
        this method is the sole source for the archive manager view in
        the workspace management dialog.  Both the local
        :class:`encre.server.session_manager.SessionManager` and the
        gateway EventRouter's manager are consulted, deduplicated by id,
        enriched with workspace attribution and sorted by last activity.

        Returns:
            Archived session dicts, newest first.
        """
        result = self._manager.list_archived_sessions()
        seen = {s["session_id"] for s in result}
        if self._adapter_manager and self._adapter_manager.router:
            router = self._adapter_manager.router
            for s in router.session_manager.list_archived_sessions():
                if s["session_id"] not in seen:
                    result.append(s)
                    seen.add(s["session_id"])
        # Apply the same physical-directory workspace attribution as
        # _list_all_sessions so archived rows render the owning
        # workspace's real icon as well.
        ws_id_to_path: dict[str, str] = {}
        try:
            for _w in _load_workspaces():
                _wid = _w.get("id") or _make_workspace_id(_w["path"])
                ws_id_to_path[_wid] = _w["path"]
        except Exception:
            pass
        for _s in result:
            _meta = _s.get("metadata") or {}
            if _meta.get("workspace_path") or _meta.get("workspace"):
                continue
            _p = None
            try:
                _d = self._manager._session_dir_path(_s["session_id"])
                _p = ws_id_to_path.get(_d.parent.parent.name)
            except Exception:
                _p = None
            if _p:
                _s["metadata"] = {**_meta, "workspace": _p, "workspace_path": _p}
        result.sort(key=lambda s: s.get("last_active", s.get("created_at", 0)), reverse=True)
        return result

    async def _broadcast_archived_sessions(self) -> None:
        """Broadcast the current archived session list to every client.

        Fired after archive, unarchive or delete so the archive manager
        view stays current in real time; it mirrors
        :meth:`_broadcast_sessions` for the active view.

        The snapshot is built off the event loop because it performs
        per-session disk reads, then sent once per connection.  A connection
        that fails to accept the frame is dropped from the registry so dead
        sockets do not accumulate across many broadcasts.
        """
        states = [s for s in self._connections_state.states() if s.ws is not None]
        if not states:
            return
        sessions = await asyncio.to_thread(self._list_archived_sessions)
        for state in states:
            try:
                _t = asyncio.ensure_future(
                    self._send_plane(
                        state.ws, "archived_sessions_list", "sessions", sessions)
                )
            except Exception as exc:
                logger.warning("[broadcast] archived schedule send failed: %s", exc)
                continue
            state.tasks.add(_t)
            _t.add_done_callback(state.tasks.discard)

    def _broadcast_sessions(self) -> None:
        """Broadcast the updated session snapshot to every connected client.

        Registered as a callback on
        :class:`encre.server.session_manager.SessionManager`, so it fires
        whenever any session is created, updated or deleted -- including
        from gateway adapters (QQ/Telegram) and iClaw flows that never
        touch this handler directly.

        The snapshot is scoped to each connection's *own* workspace: a client
        that has opened a workspace sees the workspace channel, a client in
        normal mode sees the global channel, and neither is ever handed the
        other's list.  Reading the ambient ``self._workspace_path`` here would
        read whichever connection happened to be bound, which is exactly the
        cross-client mix-up this refactor removes.

        Connections in the same scope share one build (and therefore one disk
        scan), and the build runs off the event loop so a large session tree
        cannot stall the server.
        """
        groups = self._collect_session_broadcast_groups()
        if not groups:
            logger.info("[broadcast] skipping -- no connections")
            return
        # Coalesce bursts: a session-change callback fires several times
        # during boot/warmup and each build scans the whole session tree.
        # One deferred flush publishes the latest state instead.
        timer = getattr(self, "_sessions_broadcast_timer", None)
        if timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (tests/tools): publish immediately.
            self._schedule_sessions_broadcast(groups)
            return
        self._sessions_broadcast_timer = loop.call_later(
            0.12, self._flush_sessions_broadcast,
        )

    def _flush_sessions_broadcast(self) -> None:
        """Fire the coalesced session broadcast with the latest state."""
        self._sessions_broadcast_timer = None
        groups = self._collect_session_broadcast_groups()
        if groups:
            self._schedule_sessions_broadcast(groups)

    def _collect_session_broadcast_groups(self) -> dict[str, list[Any]]:
        """Group live connections by workspace scope for one snapshot build."""
        groups: dict[str, list[Any]] = {}
        for state in self._connections_state.states():
            if state.ws is None:
                continue
            # A connection running its initial full-data push already
            # receives the fresh session planes inside that push; the
            # create_session callback firing mid-push would deliver the
            # same sessions_list twice (once from the callback, once from
            # the push), so it is skipped for the push window only.
            if getattr(state, "suppress_session_broadcast", False):
                continue
            channel = "iwork" if state.workspace_path else "normal"
            groups.setdefault(channel, []).append(state.ws)
        return groups

    def _schedule_sessions_broadcast(self, groups: dict[str, list[Any]]) -> None:
        """Build the per-scope session snapshots off-loop and fan them out.

        Args:
            groups: ``channel -> sockets`` as assembled by
                :meth:`_broadcast_sessions`.

        Returns:
            None.
        """

        async def _run() -> None:
            """Build one snapshot per distinct channel concurrently, then send.

            Returns:
                None.
            """
            def _fingerprint(sessions: list[dict[str, Any]], sockets: list[Any]) -> tuple:
                """Sidebar-visible identity of one channel's snapshot + recipients."""
                rows = tuple(
                    (
                        s.get("session_id", ""),
                        s.get("name", ""),
                        (s.get("preview", "") or "")[:60],
                        s.get("state", ""),
                        s.get("channel", ""),
                        ((s.get("metadata") or {}).get("workspace_path", "")
                         or (s.get("metadata") or {}).get("workspace", "")),
                        s.get("last_active", 0),
                        s.get("message_count", s.get("msg_count", 0)),
                        (s.get("source") or {}).get("platform", ""),
                    )
                    for s in sessions
                )
                return (rows, tuple(sorted(id(ws) for ws in sockets)))

            for channel, sockets in groups.items():
                snapshot = await asyncio.to_thread(self._list_all_sessions, channel)
                fp = _fingerprint(snapshot, sockets)
                if self._sessions_broadcast_fp.get(channel) == fp:
                    # Identical list to the one every recipient already has.
                    continue
                self._sessions_broadcast_fp[channel] = fp
                await asyncio.gather(
                    *(self._send_session_snapshot(ws, channel, snapshot) for ws in sockets),
                    return_exceptions=True,
                )

        _t = asyncio.ensure_future(_run())
        # Tracked against the first live connection purely so the fan-out is
        # cancelled if every client disconnects mid-build.
        for state in self._connections_state.states():
            state.tasks.add(_t)
            _t.add_done_callback(state.tasks.discard)
            break

    async def _send_session_snapshot(
        self, ws: Any, channel: str, sessions: list[dict[str, Any]],
    ) -> None:
        """Send one connection its scoped ``sessions_list`` snapshot, sharded.

        A large history is thousands of rows; one mega-frame made the renderer
        JSON-parse multiple megabytes in a single long task. The generic
        sharded topic channel keeps every frame in the milliseconds.

        Args:
            ws: Target socket.
            channel: Channel the snapshot belongs to.
            sessions: Prebuilt snapshot for that channel.
        """
        try:
            await self._send_topic_shards(
                ws, f"sessions:list:{channel}", {"channel": channel}, sessions,
            )
        except Exception as exc:
            logger.warning("[broadcast] sessions send failed: %s", exc)
            self._drop_connection(ws)

    async def _send_sessions_all_sharded(
        self, ws: Any, normal: list[dict[str, Any]], iwork: list[dict[str, Any]],
    ) -> None:
        """Push the dual-channel snapshot as one sharded ``sessions:all`` topic.

        Args:
            ws: Target socket.
            normal: Sessions in the normal channel.
            iwork: Sessions in the iwork channel.
        """
        tagged = []
        for s in normal:
            tagged.append({**s, "channel": "normal"})
        for s in iwork:
            tagged.append({**s, "channel": "iwork"})
        await self._send_topic_shards(ws, "sessions:all", {}, tagged)

    # ---- Client message handlers ---------------------------------------

    async def _h_list_sessions(self, ws: Any, msg: ClientListSessions) -> None:
        """Return the session list for the current channel view.

        The snapshot is assembled on a worker thread because it performs
        per-session meta reads and workspace index parsing; running it
        inline would stall the shared dispatch loop and any background
        resume work on the same socket.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientListSessions` request.

        Returns:
            Nothing; the list is pushed as a ``sessions_list`` frame
            carrying the channel the rows belong to.
        """
        # Disk I/O (per-session meta reads + workspace indexes)
        # must not block the dispatch loop -- other requests and
        # the background resume share this loop.
        sessions = await asyncio.to_thread(self._list_all_sessions)
        channel = "iwork" if self._workspace_path else "normal"
        await self._send_session_snapshot(ws, channel, sessions)

    async def _h_list_all_sessions(self, ws: Any, msg: ClientListAllSessions) -> None:
        """Return both the normal and iwork session lists at once.

        The tray popup needs both views in a single round trip, so both
        snapshots are built inside one worker thread call; the second
        pass is cheap because the on-disk index results are memoised by
        the session manager.

        Returns:
            Nothing; both lists are pushed as a single
            ``sessions_all`` frame keyed by channel.
        """
        # Tray popup needs both modes' sessions at once.  Build
        # both lists in one worker thread (query_index results are
        # memoised, so the second pass is cheap).
        def _build_both():
            """Build the normal and iwork session lists in one worker thread.

            ``query_index`` results are memoised, so the second pass is
            cheap -- this keeps both scans off the event loop in a single
            thread hop instead of two.

            Returns:
                A ``(normal, iwork)`` tuple of session lists.
            """
            return (
                self._list_all_sessions(channel_filter="normal"),
                self._list_all_sessions(channel_filter="iwork"),
            )
        normal, iwork = await asyncio.to_thread(_build_both)
        await self._send_sessions_all_sharded(ws, normal, iwork)

    async def _h_new_session(self, ws: Any, msg: ClientNewSession) -> None:
        """Create a fresh session in the current channel context.

        An existing handler session that contains no real (non-system)
        messages is discarded first, so repeatedly clicking "new chat"
        does not accumulate empty rows in the on-disk index.

        The new session is created with the workspace-scoped config when
        in iwork mode, tagged with its channel immediately, and its
        mode/command state is authoritatively cleared so no stale chip
        from a previous conversation can leak in through a late event.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientNewSession` request; ``request_id``
                is echoed back so the client can correlate the reply.
        """
        from encre.protocol.handlers.workspace_store import _apply_workspace_config
        if self._info:
            real_msgs = [m for m in self._info.agent.session.messages if m.get("role") != "system"]
            if not real_msgs:
                await self._manager.remove_session(self._info.session_id)
        # Use workspace config if currently in workspace mode, so the
        # new session inherits the project's overrides from the start.
        if self._workspace_path and os.path.isdir(self._workspace_path):
            ws_config = replace(self._default_config, workspace=self._workspace_path)
            _apply_workspace_config(ws_config, self._workspace_path)
        else:
            ws_config = replace(self._default_config, workspace="")
        self._info = self._manager.create_session(config=ws_config)
        self._current_session_id = self._info.session_id
        # Tag the session with its channel immediately so the unified
        # snapshot filters it into the correct sidebar view instead of
        # falling back to the default "normal".
        self._info.agent.session.metadata["channel"] = "iwork" if self._workspace_path else "normal"
        await self._send(ws, "session_ready", session_id=self._info.session_id, plan_items=[],
                         request_id=msg.request_id,
                         active_model_index=getattr(
                             self._info.agent.config, "active_model_index", 0))
        # Authoritatively clear chip state for the brand-new session.
        # The frontend clears its chips optimistically on session_ready,
        # but this mode_changed("") is the backend's source of truth:
        # without it, an in-flight or stale event could re-introduce the
        # previous session's mode into the new conversation.
        await self._send_session_mode(ws, self._info)
        await self._send_session_command(ws, self._info)

    async def _h_delete_session(self, ws: Any, msg: ClientDeleteSession) -> None:
        """Delete a session from disk and every related store.

        Deletion has to reach all four places a session can be recorded,
        otherwise the row reappears after a reload:

        1. The local :class:`SessionManager` (directory + index).
        2. The gateway EventRouter's manager, for IM/iClaw sessions.
        3. The per-workspace session index files in
           :mod:`encre.protocol.handlers.workspace_store`.
        4. The ``data/sub_agents/<sid>`` tree plus any scheduler entry,
           for automation history rows.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientDeleteSession` request.

        Returns:
            Nothing; the outcome is reported as ``session_deleted`` or an
            ``error`` frame, and the archive view is always refreshed.
        """
        if not msg.session_id:
            await self._send(ws, "error", message="No session_id provided", code="invalid_request")
            return
        if self._current_session_id == msg.session_id:
            self._current_session_id = None
            self._info = None
        # Drop the deleted session's spec engine so its approval state
        # cannot be resurrected if a future session ever reuses the id.
        self._spec_engines.pop(msg.session_id, None)
        ok, had_sub_agent = await self._delete_session_now(msg.session_id)
        # Always broadcast an automation update when a sub-agent
        # session was deleted, so the history list refreshes immediately
        # regardless of whether the scheduler had a matching entry.
        if self._scheduler and had_sub_agent:
            self.broadcast_automation_update()
        if ok:
            await self._send(ws, "session_deleted", session_id=msg.session_id)
        else:
            await self._send(ws, "error", message="Session not found", code="not_found")
        # The deleted session may have been archived; refresh the
        # archive view so it disappears from there too.
        await self._broadcast_archived_sessions()

    async def _delete_session_now(self, session_id: str) -> tuple[bool, bool]:
        """Remove one session from every store, off the event loop.

        Deletion walks the session folder, derived stores and every workspace
        index; running that synchronously in the handler blocked the event
        loop (and every desktop client sharing it) for the duration.

        Args:
            session_id: Session to delete.

        Returns:
            ``(removed, had_sub_agent)``. ``had_sub_agent`` tells the caller
            whether the automation history needs a refresh.
        """
        ok = await asyncio.to_thread(self._manager.delete_session_from_disk, session_id)
        # Mirror the deletion into the EventRouter's manager so adapter
        # sessions cannot resurface in a later list_sessions call.
        if self._adapter_manager and self._adapter_manager.router:
            await asyncio.to_thread(
                self._adapter_manager.router.session_manager.delete_session_from_disk,
                session_id,
            )
        # Also clean up the workspace session indices so the session
        # does not reappear when the sidebar reloads from workspace dirs.
        await asyncio.to_thread(_remove_session_from_workspace_indices, session_id)
        # Also clean up the sub-agent session directory, which backs
        # the automation history entries rather than the sidebar.
        sub_agent_dir = get_data_dir() / "sub_agents" / session_id
        had_sub_agent = await asyncio.to_thread(sub_agent_dir.is_dir)
        if had_sub_agent:
            await asyncio.to_thread(shutil.rmtree, str(sub_agent_dir))
            ok = True
        # Also clean up the scheduler's automation history entry if
        # one was recorded for this session.
        if self._scheduler:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self._scheduler.delete_job_execution_by_session_id, session_id
                )
        return ok, had_sub_agent

    async def _h_clear_all_sessions(self, ws: Any, msg: ClientClearAllSessions) -> None:
        """Delete every session in every store (storage → danger zone).

        The client already confirmed; this is irreversible. The sidebar is
        refreshed by one sessions-list broadcast at the end, and the client
        is moved onto a fresh empty conversation so it never keeps showing a
        session id that no longer exists.
        """
        ids: set[str] = set()
        for info in self._manager.list_sessions():
            ids.add(info.session_id)
        if self._adapter_manager and self._adapter_manager.router:
            for info in self._adapter_manager.router.session_manager.list_sessions():
                ids.add(info.session_id)
        archived = await asyncio.to_thread(self._list_archived_sessions)
        ids.update(row.get("session_id", "") for row in archived if isinstance(row, dict))
        for channel in ("normal", "iwork"):
            listing = await asyncio.to_thread(self._list_all_sessions, channel)
            ids.update(row.get("session_id", "") for row in listing if isinstance(row, dict))
        ids.discard("")

        removed = 0
        had_any_sub_agent = False
        for session_id in list(ids):
            self._spec_engines.pop(session_id, None)
            try:
                ok, had_sub_agent = await self._delete_session_now(session_id)
            except Exception:
                logger.exception("[clear_all_sessions] failed to delete %s", session_id)
                continue
            removed += 1 if ok else 0
            had_any_sub_agent = had_any_sub_agent or had_sub_agent

        if self._current_session_id in ids:
            self._current_session_id = None
            self._info = None
        if self._scheduler and had_any_sub_agent:
            self.broadcast_automation_update()
        self._broadcast_sessions()
        await self._broadcast_archived_sessions()
        await self._send(ws, "sessions_cleared", count=removed)
        # Leave the client on a clean conversation instead of a deleted id.
        await self._h_new_session(
            ws,
            ClientNewSession(type="new_session", request_id=msg.request_id),
        )

    async def _h_archive_session(self, ws: Any, msg: ClientArchiveSession) -> None:
        """Archive a session so it disappears from the sidebar but stays on disk.

        Archiving is a metadata flag, not a delete: the session remains
        resumable and exportable from the archive manager.  Both the
        local manager and the EventRouter's manager are updated so the
        row cannot resurface through either listing path, and both the
        sidebar and archive broadcasts are fired.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientArchiveSession` request.
        """
        if not msg.session_id:
            await self._send(ws, "error", message="No session_id provided", code="invalid_request")
            return
        ok = self._manager.archive_session(msg.session_id)
        # Keep the EventRouter's manager in sync so adapter-owned
        # sessions are archived on that side too.
        if self._adapter_manager and self._adapter_manager.router:
            self._adapter_manager.router.session_manager.archive_session(msg.session_id)
        # Refresh both views: the archived session drops out of the
        # sidebar list and into the archive list.
        self._broadcast_sessions()
        await self._broadcast_archived_sessions()
        if not ok:
            await self._send(ws, "error", message="Session not found", code="not_found")

    async def _h_unarchive_session(self, ws: Any, msg: ClientUnarchiveSession) -> None:
        """Unarchive a session so it returns to the sidebar.

        The inverse of :meth:`_h_archive_session`; the same two managers
        and both broadcasts are touched so the row moves back across
        the two views atomically from the client's point of view.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientUnarchiveSession` request.
        """
        if not msg.session_id:
            await self._send(ws, "error", message="No session_id provided", code="invalid_request")
            return
        ok = self._manager.unarchive_session(msg.session_id)
        if self._adapter_manager and self._adapter_manager.router:
            self._adapter_manager.router.session_manager.unarchive_session(msg.session_id)
        # The session re-enters the sidebar and leaves the archive view,
        # so both broadcasts are required.
        self._broadcast_sessions()
        await self._broadcast_archived_sessions()
        if not ok:
            await self._send(ws, "error", message="Session not found", code="not_found")

    async def _h_list_archived_sessions(self, ws: Any, msg: ClientListArchivedSessions) -> None:
        """Return the full archived session list.

        Built on a worker thread for the same reason as the active list:
        the snapshot performs disk reads per row and this handler shares
        the dispatch loop with unrelated requests.

        Returns:
            Nothing; rows are pushed as an ``archived_sessions_list`` frame.
        """
        sessions = await asyncio.to_thread(self._list_archived_sessions)
        await self._send_plane(ws, "archived_sessions_list", "sessions", sessions)

    async def _h_export_session(self, ws: Any, msg: ClientExportSession) -> None:
        """Export one session as a Markdown document.

        The session is resolved through the manager's directory lookup
        rather than by loading it into memory, so an unresumed session
        can still be exported.  The row is streamed back in a single
        ``session_exported`` frame together with a suggested filename
        derived from the indexed name.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientExportSession` request.
        """
        if not msg.session_id:
            await self._send(ws, "error", message="No session_id provided", code="invalid_request")
            return
        from encre.session import EncreSession
        dir_path = self._manager._session_dir_path(msg.session_id)
        if not dir_path.is_dir():
            await self._send(ws, "error", message="Session not found", code="not_found")
            return
        try:
            md = EncreSession.export_to_markdown(str(dir_path))
            name = self._manager._index.get(msg.session_id, {}).get("name", msg.session_id[:8])
            filename = f"{name or msg.session_id[:8]}.md"
            await self._send(ws, "session_exported", session_id=msg.session_id, markdown=md, filename=filename)
        except Exception as e:
            logger.error(f"Export session failed: {e}")
            await self._send(ws, "error", message=str(e), code="export_error")

    async def _h_export_sessions_batch(self, ws: Any, msg: ClientExportSessionsBatch) -> None:
        """Export several sessions into one base64-encoded zip archive.

        Each session is rendered to Markdown independently and missing
        or unreadable rows are skipped rather than failing the batch,
        so a partially valid selection still produces a usable archive.
        Sessions that belong to a workspace are nested under that
        workspace's folder name to keep the archive browsable.

        The payload is returned base64-encoded inside a
        ``sessions_exported_zip`` frame because the client protocol is
        text-only; the receiver decodes it and writes the file.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientExportSessionsBatch` request.
        """
        if not msg.session_ids:
            await self._send(ws, "error", message="No session_ids provided", code="invalid_request")
            return
        from encre.session import EncreSession
        # base64 must NOT be re-imported here: a function-level import
        # would shadow the module-level name across the entire
        # enclosing scope and raise UnboundLocalError in earlier
        # branches such as the icon upload handler.
        import zipfile, io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for sid in msg.session_ids:
                dir_path = self._manager._session_dir_path(sid)
                if not dir_path or not dir_path.is_dir():
                    continue
                try:
                    md = EncreSession.export_to_markdown(str(dir_path))
                    name = self._manager._index.get(sid, {}).get("name", sid[:8])
                    # Nest the entry under the owning workspace's
                    # folder name so multi-workspace exports stay
                    # organised instead of flattening into one bag.
                    ws_path = self._manager._index.get(sid, {}).get("workspace", "") or ""
                    if ws_path:
                        ws_dir = os.path.basename(ws_path.rstrip("/\\"))
                        arcname = f"{ws_dir}/{name or sid[:8]}.md"
                    else:
                        arcname = f"{name or sid[:8]}.md"
                    zf.writestr(arcname, md)
                except Exception:
                    continue
        zip_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        fname = f"encre-export-{len(msg.session_ids)}-sessions.zip"
        await self._send(ws, "sessions_exported_zip", zip_base64=zip_b64, filename=fname)

    async def _h_rename_session(self, ws: Any, msg: ClientRenameSession) -> None:
        """Rename a session and broadcast the refreshed snapshot.

        Both the workspace sidebar and the tray popup read their names
        from the unified session list, so a rename must re-broadcast
        that snapshot; the point-to-point ``session_renamed`` frame
        only covers the row the client renamed.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientRenameSession` request; the name is
                stripped before use so invisible whitespace cannot
                create a distinct duplicate entry.
        """
        if not msg.session_id or not msg.new_name.strip():
            await self._send(ws, "error", message="Missing session_id or new_name", code="invalid_request")
            return
        new_name = msg.new_name.strip()
        ok = self._manager.rename_session(msg.session_id, new_name)
        if ok:
            await self._send(ws, "session_renamed", session_id=msg.session_id, new_name=new_name)
            # Sidebar and tray names come from the unified session
            # list, so broadcast the refreshed snapshot.
            self._broadcast_sessions()
        else:
            await self._send(ws, "error", message="Session not found", code="not_found")

    async def _h_iclaw_resume(self, ws: Any, msg: ClientIclawResume) -> None:
        """Resume the most recent iClaw session through the EventRouter.

        iClaw conversations are owned by the gateway's
        :class:`encre.gateway.event_router.EventRouter` manager rather
        than this handler's desktop manager, so the resume must run
        inside :func:`EventRouter.iclaw_context` to be recorded against
        that manager's index.  The config is rebuilt with an empty
        workspace because iClaw is not a workspace-scoped surface.

        No messages are sent when there is nothing to resume; the
        client simply keeps its empty state.
        """
        logger.info("[iclaw] resume requested")
        router = self._adapter_manager.router if self._adapter_manager else None
        if router:
            async with router.iclaw_context():
                existing = router.session_manager.try_resume_most_recent(
                    config=replace(self._default_config, workspace=""))
                if existing is not None:
                    msgs = self._renderer_session_messages(existing.agent.session)
                    self._current_session_id = existing.session_id
                    await self._send(ws, "session_ready",
                        session_id=existing.session_id, messages=msgs)
                    logger.info("[iclaw] resume sent session_ready with %d messages sid=%s",
                                len(msgs), existing.session_id)
                else:
                    logger.info("[iclaw] no session to resume")
        else:
            logger.warning("[iclaw] no router available for resume")
