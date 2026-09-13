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

"""Connection lifecycle and transport shared by every domain handler.

Owns the per-connection state (encryption flag, current session/workspace,
running tasks), the single ``_send`` frame path, session bootstrap, the
unified full-data snapshot push and config/settings persistence helpers.
Extracted verbatim from ``encre.transport.ws`` (architecture refactor
Stage 3 / Task 5.2); behaviour is unchanged, only the layout moved.
"""

import asyncio
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
from encre.server.protocol import encode_server_message
from encre.server.session_manager import SessionManager
from encre.settings_manager import load_custom_slash_commands
from encre.slash_commands import get_slash_command_defs
from encre.spec import EncreSpecEngine
from encre.tools.runtime import set_search_engine_url

from encre.protocol.handlers.workspace_store import (
    _load_workspaces,
    _workspaces_with_session_counts,
)

logger = logging.getLogger("encre.transport.ws")


def _inject_context_windows(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure every model dict has a real ``context_window`` value.

    ``ModelConfig.to_dict()`` returns 0 for legacy configs.  This function
    creates a temporary backend to resolve the actual window size so the
    frontend canvas panel shows the correct token budget.
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

    Legacy / freshly-added models carry ``context_window == 0``; without a
    resolved value ``apply_active_model()`` cannot forward the window through
    ``backend_kwargs``, so the backend falls back to name-based detection and
    the effective context window never changes when the user switches models
    (nor does compaction trigger for a smaller window).  Resolution mirrors
    :func:`_inject_context_windows` but mutates the ``ModelConfig`` objects
    themselves so the value persists with the config.
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


class HandlerBase:
    """Connection state, frame send and session bootstrap for all mixins."""

    def __init__(self, session_manager: SessionManager, config: EncreConfig | None = None,
                 index_manager=None, adapter_manager=None, scheduler=None) -> None:
        self._manager = session_manager
        self._default_config = config
        self._index_manager = index_manager
        self._adapter_manager = adapter_manager
        self._scheduler = scheduler  # EncreScheduler from iClawEngine, if available
        self._current_session_id: str | None = None
        self._info = None  # lazily created session
        self._workspace_path: str = ""  # current workspace path (empty = normal mode)
        self._current_ws_id: str = ""
        self._index_progress_callback = None
        self._client_encrypted: bool | None = None  # detected from first client message
        self._protocol_version: int | None = None  # declared by client handshake, else PROTOCOL_VERSION
        # Per-session spec engines.  A single connection-wide singleton let
        # one session's spec (parsed document, approve/reject state) bleed
        # into another session's approval gate, so the state "carried over"
        # when switching conversations.
        self._spec_engines: dict[str, EncreSpecEngine] = {}
        self._term_sessions: dict[int, dict] = {}  # terminal_id -> {proc, reader_task}
        self._term_seq = 0
        self._connections: list[Any] = []
        self._iclaw_task: asyncio.Task[None] | None = None
        self._manager.on_sessions_changed(self._broadcast_sessions)
        self._tasks: set[asyncio.Task[Any]] = set()

    async def _send(self, ws, msg_type: str, **kwargs) -> None:
        """Encode and send a server message, then deliver it over the WebSocket.

        Honours the per-connection ``_client_encrypted`` flag so legacy
        plaintext clients keep receiving plain JSON while encrypted clients
        get AES-GCM frames.  On send failure (disconnect) the running
        agent task is cancelled.
        """
        encrypt = self._client_encrypted if self._client_encrypted is not None else False
        try:
            payload = encode_server_message(msg_type, encrypt=encrypt, **kwargs)
        except Exception as exc:
            logger.error("[_send] Failed to encode %s: %s\n%s", msg_type, exc, traceback.format_exc())
            return
        try:
            await ws.send(payload)
        except Exception as exc:
            logger.warning("[_send] Failed to send %s: %s", msg_type, exc)
            # WebSocket disconnected -- cancel any running agent
            self._cancel_current_task()

    def _spec_engine_for(self, session_or_sid: Any) -> EncreSpecEngine:
        """Return the spec engine owned by *session_or_sid* (created on first use).

        Spec state (parsed document, approve/reject decisions) must never leak
        across sessions, so every session owns its engine instead of the old
        connection-wide singleton.
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
        across sessions let one session's approve/reject bleed into another
        session's approval gate.  Prefer the loop's own engine (wired at run
        time from the per-session registry) so approvals stay scoped; fall
        back to the session's registry entry (creating it if needed) when no
        live session/loop is available.
        """
        sid = session_id or self._current_session_id
        if sid:
            info = None
            for s in getattr(self._manager, "list_sessions", lambda: [])():
                if s.session_id == sid:
                    info = s
                    break
            loop = getattr(info, "agent", None)
            loop = getattr(loop, "loop", None)
            if loop is not None and getattr(loop, "spec_engine", None) is not None:
                return loop.spec_engine
        return self._spec_engine_for(sid or "")

    async def _send_session_mode(self, ws, session) -> None:
        """Send mode_changed for the session's persisted mode (if any).

        ALWAYS send, even when the mode is empty 鈥?otherwise the frontend
        keeps the previous session's mode chip visible (stale state) when
        switching to a session that has no mode active.

        Mode is persisted on the EncreSession (``agent.session.metadata``),
        not on the ``SessionInfo`` wrapper, so we read from the underlying
        session object.
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
        toolbar chip / exit button reflect the new mode, and -- only when the
        plan-active state actually changed -- ``plan_mode_changed`` so the plan
        proposals panel stays in sync.  Returns the normalised mode.
        """
        valid = ("", "plan", "spec")
        mode = mode if mode in valid else ""
        was_plan = session.agent.loop.plan_mode_active
        session.agent.loop.set_mode(mode)
        now_plan = session.agent.loop.plan_mode_active
        logger.info("[set_mode] mode applied: '%s' session=%s plan_mode_active=%s->%s",
                    mode, session.session_id[:8], was_plan, now_plan)
        await self._send(ws, "mode_changed", mode=mode, session_id=session.session_id)
        if was_plan != now_plan:
            await self._send(ws, "plan_mode_changed",
                             active=now_plan, session_id=session.session_id)
        return mode

    def _restore_persisted_mode(self, session) -> None:
        """Re-apply a session's persisted slash-command mode after (re)load.

        A resumed or freshly-created session carries its mode in metadata but
        the agent's ``config.slash_command_mode`` starts at the default.  Drive
        it through ``loop.set_mode`` so the string, metadata mirror, and
        derived ``plan_mode_active`` flag all agree before the next run.
        """
        try:
            es = getattr(session, "agent", None)
            es = getattr(es, "session", None) or session
            mode = es.metadata.get("slash_command_mode", "") or ""
            session.agent.loop.set_mode(mode)
        except Exception:
            logger.debug("failed to restore persisted mode", exc_info=True)

    async def _send_session_command(self, ws, session) -> None:
        """Send command_changed for the session's persisted command (if any)."""
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
        """Apply a slash-command activation/clear through the single entry point.

        Stores the command (``{name, prompt, icon, title}``) via
        ``loop.set_command`` so ``config.active_command`` and the
        ``session.metadata`` mirror stay consistent, then broadcasts
        ``command_changed`` so the desktop command chip reflects the new
        state.  An empty ``name`` clears the active command.  A command is
        independent of the mode (plan/spec) -- both may be active at once.
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
        """Re-apply a session's persisted slash command after (re)load.

        Mirrors :meth:`_restore_persisted_mode`: the persisted command lives
        in ``session.metadata["active_command"]`` but ``config.active_command``
        starts at ``None``.  Drive it through ``loop.set_command`` so the
        in-memory mirror agrees before the next run re-injects the block.
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
        """
        result: list[dict[str, Any]] = []
        for message in session.get_renderer_messages():
            if message.get("role") == "system":
                continue
            if message.get("is_compact_summary") or message.get("is_compact_boundary"):
                result.append({
                    "id": message.get("id") or f"compact-marker-{len(result)}",
                    "role": "compact_marker",
                    "created_at": message.get("created_at") or message.get("timestamp") or 0,
                })
                continue
            if (
                message.get("is_compact_active_archive")
                or message.get("is_compact_archive_hint")
                or message.get("is_compact_context")
            ):
                continue
            result.append(message)
        return result

    def _resolve_startup_mode(self) -> str:
        """Resolve startup_session_mode: settings.json takes priority (runtime changes),
        fall back to config default."""
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
        """Resolve startup_session_behavior: settings.json priority, fall back to config."""
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
        """Lazily create a session only when needed (first run or new_session)."""
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
            # Re-apply any persisted slash *command* (sticky prompt injection)
            # so its ``command_instructions`` block is re-injected next run.
            self._restore_persisted_command(self._info)
        return self._info

    @staticmethod
    def _build_tools_info(info: Any) -> dict[str, Any]:
        """Snapshot of tool catalog for the client UI.

        Exposes the base always-on tools, the per-session unlocked set,
        the full active payload the model sees this turn, and a category
        breakdown so the UI can render a discovery panel.
        """
        try:
            from encre.tools.discovery import BASE_TOOLS
            discovery = info.agent.loop.discovery
            session_id = info.agent.session.id
            tools_map = info.agent.tool_registry.list_tools()
            return {
                "base": sorted(BASE_TOOLS),
                "unlocked": discovery.get_unlocked(session_id),
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

        Extracted from the old ClientGetConfig closure so the same snapshot can
        be pushed by the unified ``_push_all_data`` path 鈥?one source of truth
        for the frontend's model selector, settings and configuration panels.
        """
        cd = info.agent.config.to_dict(encrypt_api_keys=False)
        se_url = cd.get("default_search_engine_url", "")
        if se_url:
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
        # a shared one (the old singleton leaked spec approve/reject state
        # into every other session's config snapshot).
        current_spec = self._spec_engine_for(info).current_spec
        cd["spec"] = current_spec.to_dict() if current_spec else None
        # Tag the snapshot with the session it was built from so the frontend
        # can scope session-bound fields (active_command) instead of applying
        # them globally (which resurrected stale chips across sessions).
        cd["session_id"] = info.session_id
        cd["slash_commands"] = get_slash_command_defs(
            info.agent.command_registry
        )
        cd["custom_slash_commands"] = load_custom_slash_commands()
        cd["keybinds"] = load_keybinds()
        # Ship the session's active command (if any) so the frontend can render
        # the command chip on connect / config refresh.
        cd["active_command"] = (
            getattr(info.agent.config, "active_command", None)
            or info.agent.session.metadata.get("active_command")
        )
        return cd

    async def _push_all_data(self, ws) -> None:
        """Unified full-data push (config+models, sessions, workspaces).

        Called on connect and after every mode / config / session change so the
        frontend store is refreshed from ONE consistent snapshot 鈥?panels never
        pull their own slices.  Each plane is guarded so a failure in one does
        not blank the others.
        """
        try:
            info = self._get_or_create_session()
            self._manager.touch(info.session_id)
            config_data = await asyncio.to_thread(self._build_config_data, info)
            available = await self._build_skills_list(info)
            config_data["available_skills"] = available
            config_data["workspace_mode"] = "iwork" if self._workspace_path else "normal"
            config_data["workspace_path"] = self._workspace_path
            await self._send(ws, "config_data", config=config_data)
        except Exception as exc:
            logger.warning("[push_all_data] config_data failed: %s", exc)
        try:
            channel = "iwork" if self._workspace_path else "normal"
            sessions = self._list_all_sessions(channel_filter=channel)
            await self._send(ws, "sessions_list", sessions=sessions, channel=channel)
        except Exception as exc:
            logger.warning("[push_all_data] sessions failed: %s", exc)
        try:
            # Dual-channel snapshot feeds the tray popup (and the global
            # search cache) through the SAME unified push 鈥?the tray never
            # needs a separate list_all_sessions request.
            normal = self._list_all_sessions(channel_filter="normal")
            iwork = self._list_all_sessions(channel_filter="iwork")
            await self._send(ws, "sessions_all", normal=normal, iwork=iwork)
        except Exception as exc:
            logger.warning("[push_all_data] sessions_all failed: %s", exc)
        try:
            workspaces = _workspaces_with_session_counts(_load_workspaces())
            await self._send(ws, "workspaces_list", workspaces=workspaces)
        except Exception as exc:
            logger.warning("[push_all_data] workspaces failed: %s", exc)
        try:
            info = self._get_or_create_session()
            backend = info.agent.loop.backend
            models = []
            if backend is not None:
                try:
                    models = await asyncio.wait_for(backend.list_models(), timeout=5)
                except asyncio.TimeoutError:
                    models = []
            await self._send(ws, "models_list", models=models)
        except Exception as exc:
            logger.debug("[push_all_data] models skipped: %s", exc)
        # 鈹€鈹€ Extended snapshot planes: every static data domain the frontend
        #    panels subscribe to is pushed in the SAME snapshot, so no panel
        #    ever has to pull its own slice after a mode switch.  Each plane
        #    is independently guarded (one failure must not blank the rest).
        for name, payload in (
            ("global_rules_list", {"rules": self._build_global_rules_list()}),
            ("memory_list", {"entries": self._build_memory_list()}),
            ("documents_list", {"documents": self._build_documents_list()}),
            ("usage_stats", {"stats": self._build_usage_stats()}),
            ("project_rules_list", {"rules": self._build_project_rules_list()}),
            ("project_hooks_list", {"hooks": self._build_project_hooks_list()}),
            ("automation_jobs_list", {"jobs": self._build_automation_jobs()}),
        ):
            try:
                await self._send(ws, name, **payload)
            except Exception as exc:
                logger.debug("[push_all_data] %s skipped: %s", name, exc)

    def _cancel_current_task(self) -> None:
        """Cancel the running agent task for the current session (on disconnect)."""
        if self._iclaw_task and not self._iclaw_task.done():
            self._iclaw_task.cancel()
        if self._current_session_id:
            sess = self._manager.get_session(self._current_session_id)
            if sess and sess.agent_task and not sess.agent_task.done():
                sess.agent.loop.cancel()
                sess.agent_task.cancel()

    def broadcast_gateway_status(self, status: dict) -> None:
        """Called by AdapterManager when adapter status changes."""
        closed = []
        for ws in self._connections:
            try:
                _t = asyncio.ensure_future(self._send(ws, "gateway_status", status=status))
                self._tasks.add(_t)
            except Exception:
                closed.append(ws)
        for ws in closed:
            self._connections.remove(ws)

    async def _poll_wechat_scan(self, ws, adapter, qrcode_token: str) -> None:
        """Poll iLink Bot QR code status in background and notify frontend."""
        deadline = 120.0
        start = time.monotonic()
        try:
            while time.monotonic() - start < deadline:
                result = await adapter.poll_qrcode_status(qrcode_token)
                if not result:
                    await asyncio.sleep(1)
                    continue
                ilink_bot_id = result.get("ilink_bot_id", "")
                bot_token = result.get("bot_token", "")
                baseurl = result.get("baseurl", "")
                ilink_user_id = result.get("ilink_user_id", "")
                await self._send(ws, "wechat_scan_result",
                    qrcode_url="", success=True, message="",
                    scan_confirmed=True,
                    credentials={
                        "ilink_bot_id": ilink_bot_id,
                        "bot_token": bot_token,
                        "baseurl": baseurl,
                        "ilink_user_id": ilink_user_id,
                    })
                if self._adapter_manager and ilink_bot_id and bot_token:
                    if hasattr(self._adapter_manager, "_stored_configs"):
                        self._adapter_manager._stored_configs.pop("weixin", None)
                    cfg = {
                        "account_id": ilink_bot_id,
                        "WEIXIN_ACCOUNT_ID": ilink_bot_id,
                        "token": bot_token,
                        "base_url": baseurl,
                        "WEIXIN_BASE_URL": baseurl,
                        "enabled": True,
                    }
                    await self._adapter_manager.start_adapter("weixin", cfg)
                    info = self._info
                    if info and hasattr(info, "agent") and hasattr(info.agent, "config"):
                        info.agent.config.adapter_configs["weixin"] = {
                            "account_id": ilink_bot_id,
                            "token": bot_token,
                            "base_url": baseurl,
                            "enabled": True,
                        }
                        if hasattr(self, "_default_config"):
                            self._default_config.adapter_configs["weixin"] = {
                                "account_id": ilink_bot_id,
                                "token": bot_token,
                                "base_url": baseurl,
                                "enabled": True,
                            }
                        self._persist_config(info)
                return
        except Exception as e:
            logger.warning("[wechat_scan] poll error: %s", e)
        finally:
            task = asyncio.current_task()
            if task:
                self._tasks.discard(task)

    def _persist_config(self, info: Any) -> None:
        """Persist the whole configuration through the single writer.

        ``workspace`` is deliberately cleared: it is a per-session runtime
        value, not persisted state.
        """
        try:
            from encre import config_store
            config_to_save = replace(info.agent.config, workspace="")
            config_to_save.save()
            logger.info("[persist_config] saved to %s", config_store.config_dir())
        except Exception as exc:
            logger.error("[persist_config] Failed to persist config: %s\n%s", exc, traceback.format_exc())

    @staticmethod
    def _persist_mcp_json(_info: Any, servers: list[dict[str, Any]]) -> None:
        """Persist MCP servers to canonical encre mcp.json + ~/.claude/mcp.json."""
        try:
            mcp_data: dict[str, dict[str, Any]] = {}
            for srv in servers:
                name = srv.get("name", "")
                if not name:
                    continue
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

            # Canonical encre location (used by mcp_manager.py).  Encrypted at
            # rest: mcp.json carries server env / credentials.  Readers go
            # through encre.secure_io, which also accepts legacy plaintext.
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

        Returns a list of server dicts compatible with EncreConfig.mcp_servers.
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
