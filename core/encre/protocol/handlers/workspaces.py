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

"""Workspace domain handlers: open / close / remove / icons / config.

One of the mixin families composed into
:class:`encre.transport.ws.EncreWSHandler`; the desktop "iwork" view is the
main consumer.  This module owns the workspace lifecycle end to end:

* **Registration** -- ``open_workspace`` computes a stable workspace id
  from the folder path, creates the ``.encre`` project directory and the
  per-workspace data directory, and records the workspace in the
  encrypted workspace list (capped at the most recent 20).
* **Workspace-local config** -- ``.encre/config.json`` and
  ``.encre/.gitignore`` are read and written on behalf of the editor
  panels.
* **Identity** -- letter avatars are generated on first open and refreshed
  on rename; uploaded icons supersede the generated ones.

Session storage is workspace-scoped while a workspace is open
(``SessionManager.set_workspace``) and returns to the global scope on
close, so sessions created under one project never bleed into another.

Public API:
  - :meth:`WorkspacesHandlers._h_open_workspace` -- register, configure
    and seed a session for a project folder.
  - :meth:`WorkspacesHandlers._h_close_workspace` -- return to the global
    context and recreate a session.
  - :meth:`WorkspacesHandlers._h_remove_workspace` -- purge records,
    sessions and icons together.
"""

import asyncio
import base64
import logging
import os
import shutil
import time
from dataclasses import replace
from typing import Any

from encre.protocol.handlers.workspace_store import (
    _apply_workspace_config,
    _ensure_workspace_dirs,
    _generate_default_workspace_icon,
    _get_workspace_dir,
    _get_workspace_icon_file,
    _load_workspace_config,
    _load_workspaces,
    _make_workspace_id,
    _normalize_workspace_icon,
    _remove_workspace_icon_file,
    _save_workspace_config,
    _save_workspaces,
    _workspaces_with_session_counts,
    _workspace_context_files,
    _write_workspace_icon_file,
)
from encre.server.protocol import (
    ClientCloseWorkspace,
    ClientGetGitignore,
    ClientGetWorkspaceConfig,
    ClientListWorkspaces,
    ClientOpenWorkspace,
    ClientRemoveWorkspace,
    ClientRenameWorkspace,
    ClientSaveWorkspaceConfig,
    ClientSetGitignore,
    ClientUploadWorkspaceIcon,
)

logger = logging.getLogger("encre.transport.ws")


class WorkspacesHandlers:
    """Workspace lifecycle and per-workspace config handlers.

    Composed into :class:`encre.transport.ws.EncreWSHandler`.  Every
    method assumes the host provides ``_manager`` (session lifecycle and
    workspace scoping), ``_default_config``, ``_info``, ``_tasks``,
    ``_current_ws_id``,
    ``_current_session_id``, ``_workspace_path``, ``_send``,
    ``_persist_config``, ``_push_all_data``, ``_broadcast_sessions``,
    ``_resolve_startup_mode``, ``_send_session_mode``,
    ``_send_session_command`` and ``_renderer_session_messages``.

    Invariant:
        ``_current_ws_id``, ``_workspace_path`` and ``self._info`` are
        moved as a unit.  Every handler that switches context clears the
        session reference before creating the replacement, so a stale
        agent can never be answered to after a workspace change.
    """

    # ------------------------------------------------------------------
    # Client message handlers
    # ------------------------------------------------------------------

    async def _h_open_workspace(self, ws: Any, msg: ClientOpenWorkspace) -> None:
        """Register a project folder as the active workspace.

        The full sequence, in dependency order:

        1. Validate the path and create the project-local ``.encre``
           directory.
        2. Derive the stable workspace id and ensure the per-workspace
           data directory exists.
        3. Upsert the record in the encrypted workspace list, refreshing
           ``opened_at`` for repeat opens and preserving the original
           ``created_at``.  The list is capped at the most recent 20 so the
           sidebar stays bounded.
        4. Generate a default letter avatar if none exists yet -- this also
           backfills legacy records that predate icons.
        5. Switch the session store to workspace scope and clear the stale
           session reference.
        6. Clone the connection-wide default config with the workspace
           applied and create (or resume) the session for it.
        7. Push the unified snapshot so every frontend panel reflects the
           new workspace context.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientOpenWorkspace` request carrying the
                folder path and an optional ``request_id`` echoed back on
                the ``session_ready`` frame.
        """
        folder_path = os.path.abspath(os.path.expanduser(msg.path))
        if not os.path.isdir(folder_path):
            await self._send(ws, "error", message="Folder not found", code="invalid_path")
            return

        _t_open = time.time()

        # Create .encre directory in the project folder
        yim_dir = os.path.join(folder_path, ".encre")
        os.makedirs(yim_dir, exist_ok=True)

        # Generate stable ID and create workspace data dir under ~/.dunimd/encre
        ws_id = _make_workspace_id(folder_path)
        _ensure_workspace_dirs(ws_id)

        # Save to encrypted workspace records
        workspaces = _load_workspaces()
        existing = next((w for w in workspaces if w["path"] == folder_path), None)
        if existing:
            existing["opened_at"] = time.time()
            existing["id"] = ws_id
            # Creation time is recorded once and never recomputed
            existing.setdefault("created_at", time.time())
        else:
            workspaces.append({
                "id": ws_id,
                "path": folder_path,
                "name": os.path.basename(folder_path),
                "opened_at": time.time(),
                "created_at": time.time(),
            })
        # Auto-generate a default letter avatar once for workspaces
        # that have no icon file yet (covers legacy records too).
        ws_record = existing or workspaces[-1]
        if not (os.path.isfile(_get_workspace_icon_file(ws_id, "svg"))
                or os.path.isfile(_get_workspace_icon_file(ws_id, "png"))):
            try:
                svg = await asyncio.to_thread(
                    _generate_default_workspace_icon, ws_record["name"])
                await asyncio.to_thread(_write_workspace_icon_file, ws_id, "svg", svg)
            except Exception:
                logger.warning("default workspace icon generation failed", exc_info=True)
        # Keep last 20
        workspaces = workspaces[-20:]
        _save_workspaces(workspaces)

        # Change working directory
        os.chdir(folder_path)

        # Switch session storage to workspace context
        await self._manager.set_workspace(ws_id)
        self._info = None  # invalidate stale session ref after workspace switch
        self._workspace_path = folder_path
        self._current_ws_id = ws_id

        # Clone the default config so the workspace override never leaks
        # back into the connection-wide _default_config.
        ws_config = replace(
            self._default_config,
            workspace=folder_path,
        )
        _apply_workspace_config(ws_config, folder_path)

        # When the startup mode is "resume", continue the most recent
        # session for this workspace instead of starting a fresh one.
        startup_mode = self._resolve_startup_mode()
        if startup_mode == "resume":
            existing = self._manager.try_resume_most_recent(config=ws_config)
            info = existing if existing is not None else self._manager.create_session(config=ws_config)
        else:
            info = self._manager.create_session(config=ws_config)

        self._info = info
        self._current_session_id = info.session_id
        self._manager.touch(info.session_id)
        info.metadata["workspace"] = folder_path
        info.agent.session.metadata["workspace"] = folder_path
        info.agent.session.metadata["channel"] = "iwork"
        self._persist_config(info)

        logger.info("[workspace] open_workspace session=%s setup=%.2fs",
                    info.session_id[:8], time.time() - _t_open)

        await self._send(ws, "workspace_opened",
            path=folder_path, name=os.path.basename(folder_path),
            id=ws_id, workspaces=_workspaces_with_session_counts(workspaces))

        sess = info.agent.session
        sess.ensure_artifacts_from_messages()
        msgs = self._renderer_session_messages(sess)
        branches_list = [b.__dict__ for b in sess.branches.values()]
        await self._send(ws, "session_ready", session_id=info.session_id, messages=msgs,
                         plan_items=sess.plan_items, artifacts=sess.artifacts, references=sess.references,
                         branches=branches_list, active_branch_id=sess.active_branch_id,
                         request_id=msg.request_id,
                         active_model_index=getattr(info.agent.config, "active_model_index", 0))
        await self._send_session_mode(ws, info)
        await self._send_session_command(ws, info)
        # Unified data push: config+models, sessions, workspaces in
        # one consistent snapshot so every frontend panel (sidebar,
        # model selector, settings) fills for the new workspace
        # context.  The frontend also broadcasts to all connections.
        await self._push_all_data(ws)
        self._broadcast_sessions()
        _t1 = time.time()
        logger.info("[workspace] open_workspace done session=%s total=%.2fs",
                    info.session_id[:8], _t1 - _t_open)

    async def _h_list_workspaces(self, ws: Any, msg: ClientListWorkspaces) -> None:
        """Return the registered workspaces with their session counts.

        Reading and annotating the workspace list touches disk (the
        encrypted records plus one session-count scan per workspace), so
        it runs in a worker thread to keep the event loop responsive.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientListWorkspaces` request (carries no
                payload).
        """
        def _build_ws_list():
            """Load and annotate the workspace list.

            Runs on the worker thread that owns the call, so keep it free
            of any awaitable work.

            Returns:
                Workspace records with live session counts attached.
            """
            wss = _load_workspaces()
            return _workspaces_with_session_counts(wss)
        workspaces = await asyncio.to_thread(_build_ws_list)
        await self._send(ws, "workspaces_list", workspaces=workspaces)

    async def _h_remove_workspace(self, ws: Any, msg: ClientRemoveWorkspace) -> None:
        """Remove a workspace record and purge all of its on-disk state.

        The record is dropped from the encrypted list first, then the
        unified lifecycle entry point :func:`encre.lifecycle.purge_workspace`
        removes the icon files, every session directory and the index
        caches in one pass.  Routing this through the single entry point
        is what prevents a removed workspace from leaving an orphaned
        empty folder and ``index.json`` behind.

        If the removed workspace was the active one, the connection falls
        back to the global context and pushes a full snapshot; otherwise
        only the session list is refreshed so the vanished sessions
        disappear from the sidebar.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientRemoveWorkspace` request carrying the
                folder path to remove.
        """
        workspaces = _load_workspaces()
        removed_ws = None
        for w in workspaces:
            if w["path"] == msg.path:
                removed_ws = w
                break
        workspaces = [w for w in workspaces if w["path"] != msg.path]
        _save_workspaces(workspaces)
        # Clean the whole workspace tree through the single lifecycle
        # entry point: icon, session directories and index caches all go
        # together.  Removing the folder and pruning the index as two
        # separate steps is how orphaned empty folders and index.json
        # files get left behind.
        ws_id = removed_ws["id"] if removed_ws and removed_ws.get("id") else _make_workspace_id(msg.path)
        from encre.lifecycle import purge_workspace

        purge_workspace(ws_id)
        await self._send(ws, "workspace_removed", path=msg.path, workspaces=_workspaces_with_session_counts(workspaces))
        # Drop back to the global context if this was the active
        # workspace; otherwise a plain session-list refresh is enough.
        if self._workspace_path and os.path.normcase(os.path.normpath(self._workspace_path)) == os.path.normcase(os.path.normpath(msg.path)):
            self._workspace_path = ""
            await self._push_all_data(ws)
        else:
            self._broadcast_sessions()

    async def _h_close_workspace(self, ws: Any, msg: ClientCloseWorkspace) -> None:
        """Leave the current workspace and return to the global context.

        The session store is switched back to global scope,
        the stale session reference is cleared, and a fresh session is
        created (or resumed when the startup mode is ``resume``) so the
        client always has a valid session to talk to afterwards.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientCloseWorkspace` request; ``request_id``
                is echoed back on the ``session_ready`` frame.
        """
        self._current_ws_id = ""
        # Return to global session storage and clear the stale session.
        await self._manager.set_workspace(None)
        self._info = None  # invalidate stale session ref after workspace switch
        self._workspace_path = ""
        self._default_config = replace(self._default_config, workspace="")
        # Reset the process working directory to the app home so later
        # relative paths resolve against the right root.
        os.chdir(os.path.join(os.path.expanduser("~"), ".dunimd", "encre"))
        clean_config = replace(self._default_config, workspace="")
        # Resume the most recent global-context session when the startup
        # mode is "resume", otherwise start a fresh one.
        startup_mode = self._resolve_startup_mode()
        if startup_mode == "resume":
            existing = self._manager.try_resume_most_recent(config=clean_config)
            info = existing if existing is not None else self._manager.create_session(config=clean_config)
        else:
            info = self._manager.create_session(config=clean_config)
        self._info = info
        self._current_session_id = info.session_id
        self._manager.touch(info.session_id)
        self._persist_config(info)
        await self._send(ws, "workspace_closed")
        sess = info.agent.session
        sess.ensure_artifacts_from_messages()
        msgs = self._renderer_session_messages(sess)
        branches_list = [b.__dict__ for b in sess.branches.values()]
        await self._send(ws, "session_ready", session_id=info.session_id, messages=msgs,
                         plan_items=sess.plan_items, artifacts=sess.artifacts, references=sess.references,
                         branches=branches_list, active_branch_id=sess.active_branch_id,
                         request_id=msg.request_id,
                         active_model_index=getattr(info.agent.config, "active_model_index", 0))
        await self._send_session_mode(ws, info)
        await self._send_session_command(ws, info)
        # Back in the global context: push the full snapshot so the
        # sidebar, model selector and settings repopulate for normal mode.
        await self._push_all_data(ws)

    async def _h_upload_workspace_icon(self, ws: Any, msg: ClientUploadWorkspaceIcon) -> None:
        """Replace a workspace's icon with an uploaded image.

        The payload is a base64 data URL.  Uploads are capped at 8 MB to
        bound memory, then normalised to PNG off the event loop because
        image decoding is CPU-heavy.  A custom upload always supersedes
        the generated letter avatar, so the generated SVG is removed once
        the PNG lands.

        On any failure the on-disk icon is left untouched and the client
        simply sees the old one; the list is echoed either way so the
        manager can re-render.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientUploadWorkspaceIcon` request carrying
                the folder path and the base64 data URL.
        """
        workspaces = _load_workspaces()
        target = next((w for w in workspaces if w["path"] == msg.path), None)
        ok = False
        if target is None:
            await self._send(ws, "error", message="Workspace not found", code="invalid_path")
        elif msg.icon_data.startswith("data:image/") and ";base64," in msg.icon_data:
            try:
                b64 = msg.icon_data.split(";base64,", 1)[1]
                raw = base64.b64decode(b64)
                if len(raw) <= 8 * 1024 * 1024:  # 8 MB upload cap
                    png = await asyncio.to_thread(_normalize_workspace_icon, raw)
                    if png:
                        target_ws_id = target.get("id") or _make_workspace_id(target["path"])
                        await asyncio.to_thread(_write_workspace_icon_file, target_ws_id, "png", png)
                        # A custom upload supersedes the generated default.
                        await asyncio.to_thread(_remove_workspace_icon_file, target_ws_id, "svg")
                        ok = True
            except Exception:
                logger.warning("workspace icon upload failed", exc_info=True)
        if not ok and target is not None:
            await self._send(ws, "error", message="Invalid icon image", code="invalid_icon")
        # Always echo the list so the manager re-renders; on failure the
        # on-disk icon is untouched, so the old one simply stays.
        await self._send(ws, "workspaces_list",
                         workspaces=await asyncio.to_thread(
                             _workspaces_with_session_counts, workspaces))

    async def _h_rename_workspace(self, ws: Any, msg: ClientRenameWorkspace) -> None:
        """Rename a workspace record and refresh its generated avatar.

        Only the display name changes -- the id and path stay fixed, so
        sessions and index caches keyed off the path are unaffected.  The
        generated letter avatar is re-rendered for the new name, but a
        custom upload (``icon.png``) always wins and is left untouched.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientRenameWorkspace` request carrying the
                folder path and the new display name.
        """
        new_name = msg.name.strip()
        workspaces = _load_workspaces()
        target = next((w for w in workspaces if w["path"] == msg.path), None)
        if target is None:
            await self._send(ws, "error", message="Workspace not found", code="invalid_path")
        elif not new_name:
            await self._send(ws, "error", message="Name cannot be empty", code="invalid_name")
        elif new_name != target.get("name"):
            target["name"] = new_name
            _save_workspaces(workspaces)
            # Re-render the generated letter avatar for the new name; a
            # custom upload takes priority and is left as-is.
            ws_id = target.get("id") or _make_workspace_id(msg.path)
            if (os.path.isfile(_get_workspace_icon_file(ws_id, "svg"))
                    and not os.path.isfile(_get_workspace_icon_file(ws_id, "png"))):
                try:
                    svg = await asyncio.to_thread(
                        _generate_default_workspace_icon, new_name)
                    await asyncio.to_thread(_write_workspace_icon_file, ws_id, "svg", svg)
                except Exception:
                    logger.warning("workspace icon regeneration failed", exc_info=True)
        await self._send(ws, "workspaces_list",
                         workspaces=await asyncio.to_thread(
                             _workspaces_with_session_counts, workspaces))

    async def _h_get_workspace_config(self, ws: Any, msg: ClientGetWorkspaceConfig) -> None:
        """Read the workspace-local config and its context files.

        Both the config document (``.encre/config.json``) and the list of
        recognised context files are returned in one frame so the editor
        panel can render without a round trip.  A missing or unreadable
        directory yields an empty config rather than an error, since a
        brand-new project legitimately has neither yet.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientGetWorkspaceConfig` request carrying
                the folder path.
        """
        cfg = {}
        if os.path.isdir(msg.path):
            cfg = await asyncio.to_thread(_load_workspace_config, msg.path)
        await self._send(ws, "workspace_config", path=msg.path, config=cfg,
                         files=await asyncio.to_thread(_workspace_context_files, msg.path))

    async def _h_save_workspace_config(self, ws: Any, msg: ClientSaveWorkspaceConfig) -> None:
        """Write the workspace-local config and report the saved view.

        The response echoes the canonicalised config rather than the raw
        request, so the editor reconciles against what actually landed on
        disk (keys normalised, unknown fields dropped).

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientSaveWorkspaceConfig` request carrying
                the folder path and the config document to write.
        """
        cfg = {}
        if os.path.isdir(msg.path):
            cfg = await asyncio.to_thread(_save_workspace_config, msg.path, msg.config)
        await self._send(ws, "workspace_config", path=msg.path, config=cfg,
                         files=await asyncio.to_thread(_workspace_context_files, msg.path))

    async def _h_get_gitignore(self, ws: Any, msg: ClientGetGitignore) -> None:
        """Read ``.encre/.gitignore`` for the active workspace.

        The project-local gitignore keeps Encre's own generated files out
        of the user's repository.  The directory is created on demand so a
        first read of a fresh project returns an empty string rather than
        an error.  A read failure is surfaced inside the ``content`` field
        as a comment line, keeping the frame shape stable for the editor.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientGetGitignore` request.
        """
        if not self._workspace_path:
            await self._send(ws, "gitignore_content", path="", content="")
        else:
            yim_dir = os.path.join(self._workspace_path, ".encre")
            gitignore_path = os.path.join(yim_dir, ".gitignore")
            os.makedirs(yim_dir, exist_ok=True)
            if os.path.isfile(gitignore_path):
                try:
                    with open(gitignore_path, encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    await self._send(ws, "gitignore_content", path=gitignore_path, content=content)
                except Exception as e:
                    await self._send(ws, "gitignore_content", path=gitignore_path, content=f"# Error reading .gitignore: {e}")
            else:
                await self._send(ws, "gitignore_content", path=gitignore_path, content="")

    async def _h_set_gitignore(self, ws: Any, msg: ClientSetGitignore) -> None:
        """Write ``.encre/.gitignore`` for the active workspace.

        The content is written verbatim with no sanitisation, since the
        editor owns the semantics of gitignore patterns.  The frame is
        echoed back so the editor can confirm the save.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientSetGitignore` request carrying the full
                file content.
        """
        if self._workspace_path:
            yim_dir = os.path.join(self._workspace_path, ".encre")
            gitignore_path = os.path.join(yim_dir, ".gitignore")
            os.makedirs(yim_dir, exist_ok=True)
            try:
                with open(gitignore_path, "w", encoding="utf-8") as f:
                    f.write(msg.content)
                await self._send(ws, "gitignore_content", path=gitignore_path, content=msg.content)
            except Exception as e:
                await self._send(ws, "error", message=f"Failed to save .gitignore: {e}")
