#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

"""Workspace domain handlers: open / close / remove / icons / config / index.

Drives the iwork workspace lifecycle: directory registration, code-index
wiring, per-workspace config (``.encre/config.json``) and gitignore
editing.  Extracted verbatim from ``encre.transport.ws`` (architecture
refactor Stage 3 / Task 5.2); behaviour is unchanged, only the layout
moved.  Outer-loop ``continue`` statements of the original dispatch chain
became ``return`` inside the extracted methods.
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
    _save_index_metadata,
    _save_workspace_config,
    _save_workspaces,
    _workspaces_with_session_counts,
    _workspace_context_files,
    _write_workspace_icon_file,
)
from encre.server.protocol import (
    ClientCloseWorkspace,
    ClientDeleteIndex,
    ClientGetGitignore,
    ClientGetIndexStatus,
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
    """Workspace lifecycle, index wiring and config handlers."""

    # 鈹€鈹€ Index plumbing 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def _inject_index_to_session(self, ws_id: str, idx: Any) -> None:
        """Inject a fully-built code index into the current session's agent.

        Called by ``IndexManager._fire_index_ready`` (via the
        ``_on_index_ready`` callback registered in
        ``ClientOpenWorkspace``).  If the current session belongs to the
        same workspace the index was built for, the code index is injected
        into ``agent.loop`` so that future ``_build_codebase_context()``
        calls receive real data instead of an empty string.
        """
        if ws_id != self._current_ws_id:
            return
        if not self._info or not self._info.agent:
            return
        self._info.agent.loop.inject_code_index(idx)
        logger.info("[index] injected ready index into agent session=%s ws=%s",
                     self._info.session_id[:8], ws_id)

    def _make_index_callback(self, ws, ws_id: str | None = None):
        """Return a progress callback for IndexManager that sends WS messages."""
        def callback(data: dict) -> None:
            try:
                status = data.get("status", "indexing")
                files = data.get("files", 0)
                progress = data.get("progress", 0)
                current_file = data.get("current_file", "")
                effective_id = ws_id or self._current_ws_id
                if status in ("indexing", "ready", "error") and progress >= 0:
                    logger.info("[index] progress: %d%% status=%s files=%d ws=%s",
                                progress, status, files,
                                effective_id[:8] if effective_id else "?")
                # Fire-and-forget the send coroutine (ignore connection errors)
                _t = asyncio.ensure_future(
                    self._safe_send_index_status(ws, status, files, progress, current_file, effective_id)
                )
                self._tasks.add(_t)
            except Exception:
                pass
        return callback

    async def _safe_send_index_status(self, ws, status, files, progress, current_file, ws_id=None):
        """Send index_status, ignoring any connection errors."""
        try:
            await self._send(ws, "index_status", files=files, status=status,
                             progress=progress, current_file=current_file,
                             workspace_id=ws_id or (self._current_ws_id if self._current_ws_id else ""))
        except ConnectionResetError:
            logger.debug("[index] connection reset while sending progress")
        except Exception:
            logger.debug("[index] failed to send progress", exc_info=True)

    # 鈹€鈹€ Client message handlers 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    async def _h_open_workspace(self, ws: Any, msg: ClientOpenWorkspace) -> None:
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
        # Start background index via IndexManager (survives WS disconnects)
        if self._index_manager:
            self._index_progress_callback = self._make_index_callback(ws)
            self._index_manager.subscribe(ws_id, self._index_progress_callback)

            # Register a callback that injects the built index into
            # the running agent so the conversation never blocks on
            # codebase queries.  The callback is re-registered for
            # every workspace open to capture the latest session ref.
            self._index_manager.set_on_index_ready(
                lambda _ws_id, idx: self._inject_index_to_session(_ws_id, idx)
            )

            self._index_manager.start_index(ws_id, folder_path)

        # Clone config to avoid mutating the shared _default_config
        ws_config = replace(
            self._default_config,
            workspace=folder_path,
        )
        _apply_workspace_config(ws_config, folder_path)

        #   If startup session mode is "resume", try to resume most recent workspace
        # session
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

        # Get index state for immediate display in sidebar tree
        idx_status = "idle"
        idx_files = 0
        idx_progress = 0
        if self._index_manager:
            # Check if index is already cached (ready)
            cached_status = self._index_manager.get_status(ws_id) if hasattr(self._index_manager, "get_status") else {}
            if cached_status.get("status") == "ready":
                idx_status = "ready"
                idx_files = cached_status.get("files", 0)
                idx_progress = cached_status.get("progress", 100)
                # Persist metadata so the progress file is not lost on the next
                # workspace open (the subprocess-spawn path clears it).
                _save_index_metadata(ws_id, idx_files)
            else:
                task = self._index_manager.get_task(ws_id) if hasattr(self._index_manager, "get_task") else None
                if task is not None and not task.done():
                    idx_status = "indexing"
                    idx_progress = cached_status.get("progress", 0)
        await self._send(ws, "workspace_opened",
            path=folder_path, name=os.path.basename(folder_path),
            id=ws_id, workspaces=_workspaces_with_session_counts(workspaces),
            index_status=idx_status, index_files=idx_files,
            progress=idx_progress)

        sess = info.agent.session
        sess.ensure_artifacts_from_messages()
        msgs = self._renderer_session_messages(sess)
        branches_list = [b.__dict__ for b in sess.branches.values()]
        await self._send(ws, "session_ready", session_id=info.session_id, messages=msgs,
                         plan_items=sess.plan_items, artifacts=sess.artifacts, references=sess.references,
                         branches=branches_list, active_branch_id=sess.active_branch_id,
                         request_id=msg.request_id)
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
        def _build_ws_list():
            wss = _load_workspaces()
            return _workspaces_with_session_counts(wss)
        workspaces = await asyncio.to_thread(_build_ws_list)
        await self._send(ws, "workspaces_list", workspaces=workspaces)

    async def _h_remove_workspace(self, ws: Any, msg: ClientRemoveWorkspace) -> None:
        workspaces = _load_workspaces()
        removed_ws = None
        for w in workspaces:
            if w["path"] == msg.path:
                removed_ws = w
                break
        workspaces = [w for w in workspaces if w["path"] != msg.path]
        _save_workspaces(workspaces)
        # Clean up the whole workspace tree on disk through the unified
        # lifecycle entry point: icon, every session directory and the index
        # caches are removed together.  The previous code rmtree'd the folder
        # while relying on a separate index pass, which is how a removed
        # workspace could leave an empty folder + index.json behind.
        ws_id = removed_ws["id"] if removed_ws and removed_ws.get("id") else _make_workspace_id(msg.path)
        from encre.lifecycle import purge_workspace

        purge_workspace(ws_id)
        await self._send(ws, "workspace_removed", path=msg.path, workspaces=_workspaces_with_session_counts(workspaces))
        # If the removed workspace was active, drop back to the
        # normal context and push the unified snapshot; otherwise
        # refresh the session list so removed sessions vanish.
        if self._workspace_path and os.path.normcase(os.path.normpath(self._workspace_path)) == os.path.normcase(os.path.normpath(msg.path)):
            self._workspace_path = ""
            await self._push_all_data(ws)
        else:
            self._broadcast_sessions()

    async def _h_close_workspace(self, ws: Any, msg: ClientCloseWorkspace) -> None:
        # Unsubscribe from index progress but do NOT cancel -- indexing
        # continues in the background service even without a WS connection.
        if self._index_manager and self._current_ws_id and self._index_progress_callback:
            self._index_manager.unsubscribe(self._current_ws_id, self._index_progress_callback)
            self._index_progress_callback = None
            self._current_ws_id = ""
        # Switch back to global session storage
        await self._manager.set_workspace(None)
        self._info = None  # invalidate stale session ref after workspace switch
        self._workspace_path = ""
        self._default_config = replace(self._default_config, workspace="")
        # Reset working directory
        os.chdir(os.path.join(os.path.expanduser("~"), ".dunimd", "encre"))
        clean_config = replace(self._default_config, workspace="")
        # Try to resume most recent normal mode session if startup mode is "resume"
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
                         request_id=msg.request_id)
        await self._send_session_mode(ws, info)
        await self._send_session_command(ws, info)
        # Back to the global/normal context: unified data push so
        # the sidebar / model selector / settings fill for normal
        # mode again.
        await self._push_all_data(ws)

    async def _h_upload_workspace_icon(self, ws: Any, msg: ClientUploadWorkspaceIcon) -> None:
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
        # Always echo the list so the manager re-renders; on failure
        # the on-disk icon is untouched so the old one simply stays.
        await self._send(ws, "workspaces_list",
                         workspaces=await asyncio.to_thread(
                             _workspaces_with_session_counts, workspaces))

    async def _h_rename_workspace(self, ws: Any, msg: ClientRenameWorkspace) -> None:
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
            # Refresh the generated default letter avatar for the
            # new name; custom uploads (icon.png) stay untouched.
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
        cfg = {}
        if os.path.isdir(msg.path):
            cfg = await asyncio.to_thread(_load_workspace_config, msg.path)
        await self._send(ws, "workspace_config", path=msg.path, config=cfg,
                         files=await asyncio.to_thread(_workspace_context_files, msg.path))

    async def _h_save_workspace_config(self, ws: Any, msg: ClientSaveWorkspaceConfig) -> None:
        cfg = {}
        if os.path.isdir(msg.path):
            cfg = await asyncio.to_thread(_save_workspace_config, msg.path, msg.config)
        await self._send(ws, "workspace_config", path=msg.path, config=cfg,
                         files=await asyncio.to_thread(_workspace_context_files, msg.path))

    async def _h_reindex_workspace(self, ws: Any, msg) -> None:
        target_path = msg.path or self._workspace_path or ""
        if not target_path or not self._index_manager:
            await self._send(ws, "index_status", files=0, status="no_workspace")
        else:
            target_path = os.path.abspath(os.path.expanduser(target_path))
            try:
                ws_id = _make_workspace_id(target_path)
                logger.info("[index] reindex_workspace ws=%s path=%s",
                            ws_id[:8], target_path)
                # Subscribe for progress updates during reindex
                if self._index_progress_callback:
                    self._index_manager.unsubscribe(ws_id, self._index_progress_callback)
                self._index_progress_callback = self._make_index_callback(ws, ws_id)
                self._index_manager.subscribe(ws_id, self._index_progress_callback)
                self._index_manager.reindex(ws_id, target_path)
                asyncio.get_running_loop().call_soon(
                    lambda: logger.info("[index] reindex triggered")
                )
            except Exception as e:
                logger.error("[index] reindex failed: %s", e, exc_info=True)
                await self._send(ws, "index_status", files=0, status=f"error: {e}")

    async def _h_delete_index(self, ws: Any, msg) -> None:
        target_path = msg.path or self._workspace_path or ""
        if not target_path or not self._index_manager:
            await self._send(ws, "index_status", files=0, status="no_workspace")
        else:
            try:
                target_path = os.path.abspath(os.path.expanduser(target_path))
                ws_id = _make_workspace_id(target_path)
                self._index_manager.delete_index(ws_id, target_path)
                await self._send(ws, "index_status", files=0, status="idle",
                                 progress=0, workspace_id=ws_id)
                logger.info("[index] deleted index ws=%s", ws_id[:8])
            except Exception as e:
                await self._send(ws, "index_status", files=0, status=f"error: {e}")

    async def _h_get_index_status(self, ws: Any, msg) -> None:
        target_path = msg.path or self._workspace_path or ""
        if not target_path or not self._index_manager:
            await self._send(ws, "index_status", files=0, status="no_workspace")
        else:
            target_path = os.path.abspath(os.path.expanduser(target_path))
            ws_id = _make_workspace_id(target_path)
            status = self._index_manager.get_status(ws_id)
            await self._send(ws, "index_status",
                             files=status.get("files", 0),
                             status=status.get("status", "idle"),
                             progress=status.get("progress", 0),
                             workspace_id=ws_id)

    async def _h_get_gitignore(self, ws: Any, msg: ClientGetGitignore) -> None:
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

    async def _h_delete_index(self, ws: Any, msg: ClientDeleteIndex) -> None:
        if not self._workspace_path or not self._index_manager:
            await self._send(ws, "index_status", files=0, status="no_workspace")
        else:
            try:
                self._index_manager.delete_index(self._current_ws_id, self._workspace_path)
                await self._send(ws, "index_status", files=0, status="idle")
            except Exception as e:
                await self._send(ws, "index_status", files=0, status=f"error: {e}")
