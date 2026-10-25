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

"""Settings-domain handlers: models, providers, agents, MCP, data.

This module owns every client-visible configuration surface and is one
of the mixin families composed into :class:`encre.transport.ws.EncreWSHandler`
(mapped into ``_DISPATCH`` under ``ClientConfigure``,
``ClientGetConfig``, ``ClientListModels``, ``ClientFetchModels``,
``ClientValidateModel``, ``ClientUpdateProviders``,
``ClientSetActiveModel``, ``ClientDeleteModel``, ``ClientUpdateMCP``,
``ClientUpdateAgent``, ``ClientAgentList`` / ``ClientAgentCreate`` /
``ClientAgentDelete`` / ``ClientAgentUpdate`` / ``ClientAgentSetActive``,
``ClientUpdateSubAgents``, ``ClientExportData``, ``ClientImportData``,
``ClientGetUsageStats``, ``ClientPing`` and ``ClientWechatScan``).

Two config stores are involved and this module keeps them in step:

- ``self._default_config`` -- the connection-wide default that new
  sessions inherit.  Changes made by any of these handlers are mirrored
  into it so an :mod:`encre.gateway` adapter session started later sees
  the same backend, models and providers.
- ``session.agent.config`` -- the live per-session config read by the
  Rust loop and the backend factory.  This is what actually takes
  effect for the session being edited.

The runtime overlay store (:mod:`encre.config_store`, persisting to
``<data_dir>/config/ui.json``) is reached through ``_persist_config``
and ``_persist_mcp_json``, which are provided by the composed host
class.

Public API highlights:
  - :meth:`SettingsHandlers._h_configure` -- generic patch entry point.
  - :meth:`SettingsHandlers._h_update_providers` -- provider namespace
    management with duplicate-name rejection.
  - :meth:`SettingsHandlers._h_validate_model` -- endpoint probe plus
    live capability and reasoning-level detection.
  - :meth:`SettingsHandlers._models_updated_payload` -- the single
    source of truth for the ``models_updated`` frame.
"""

import asyncio
import json
import logging
import os
import shutil
import time
import traceback
from pathlib import Path
from typing import Any

from encre.config import (
    AgentConfig,
    ModelConfig,
    ProviderConfig,
    SubAgentConfig,
)
from encre.keybinds import save_keybinds
from encre.model_selection import model_identity, resolve_model_index
from encre.protocol.handlers.base import _inject_context_windows, _resolve_model_context_windows
from encre.protocol.handlers.workspace_store import _save_workspace_config
from encre.session import _is_synthetic_user_message
from encre.server.protocol import (
    ClientAgentCreate,
    ClientAgentDelete,
    ClientAgentList,
    ClientAgentSetActive,
    ClientAgentUpdate,
    ClientConfigure,
    ClientDeleteModel,
    ClientExportData,
    ClientFetchModels,
    ClientGetConfig,
    ClientGetUsageStats,
    ClientImportData,
    ClientListModels,
    ClientPing,
    ClientSetActiveModel,
    ClientUpdateAgent,
    ClientUpdateMCP,
    ClientUpdateProviders,
    ClientUpdateSubAgents,
    ClientValidateModel,
    ClientWechatScan,
)
from encre.settings_manager import (
    _GENERAL_SETTINGS_KEYS,
    load_settings,
    save_custom_slash_commands,
)
from encre.tools.runtime import set_search_engine_url

logger = logging.getLogger("encre.transport.ws")


class SettingsHandlers:
    """Model, config, MCP, agent and data-management mixin.

    Composed into :class:`encre.transport.ws.EncreWSHandler`; every
    method assumes the host provides ``_get_or_create_session``,
    ``_manager``, ``_default_config``, ``_adapter_manager``,
    ``_workspace_path``, ``_send``, ``_persist_config``,
    ``_persist_mcp_json``, ``_build_config_data``, ``_build_skills_list``,
    ``_build_memory_list``, ``_build_global_rules_list``,
    ``_build_usage_stats``, ``_push_all_data`` and ``_poll_wechat_scan``.

    Invariant:
        Any mutation that can be observed by another session or by a
        future session is applied to ``session.agent.config`` AND
        mirrored into ``_default_config``, then persisted exactly once
        with a single :func:`_persist_config` call.  Applying both
        halves and persisting only at the end guarantees that a handler
        failing midway never leaves a partially written config on disk.
    """

    async def _h_ping(self, ws: Any, msg: ClientPing) -> None:
        """Heartbeat: bump the current session and reply with a pong.

        Touching the session keeps the "last active" ordering accurate
        even for clients that are idle but still connected.
        """
        if self._info:
            self._manager.touch(self._info.session_id)
        await self._send(ws, "pong")

    async def _h_list_models(self, ws: Any, msg: ClientListModels) -> None:
        """Return the model list advertised by the active backend.

        The backend call is bounded to five seconds; a slow or
        unreachable provider must not wedge the UI, so both the timeout
        and the absent-backend case degrade to an empty list instead of
        raising.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientListModels` request.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        backend = info.agent.loop.backend
        if backend is None:
            # No backend configured yet -- return empty model list
            # instead of crashing with AttributeError.
            models = []
        else:
            try:
                models = await asyncio.wait_for(backend.list_models(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("[list_models] provider timed out, returning empty list")
                models = []
        await self._send(ws, "models_list", models=models)

    async def _h_configure(self, ws: Any, msg: ClientConfigure) -> None:
        """Apply a config patch from the frontend, persist, and push.

        This is the generic settings panel entry point: the client sends
        an arbitrary key/value patch and this handler routes each key to
        its real sink.  Empty strings and ``None`` values are skipped so
        a partially filled form cannot zero out working settings.

        Key routing:

        - ``default_search_engine_url`` -> global runtime state via
          :func:`encre.tools.runtime.set_search_engine_url`.
        - ``language`` / ``language_preference`` -> live session plus
          ``_default_config``; the loop's cached system prompt is cleared
          so the next turn is rebuilt with the new language immediately.
        - Backend keys (``backend_type``, ``api_key``, ``base_url``,
          ``model``) -> live session, backend rebuild, and a mirror into
          ``_default_config`` so :mod:`encre.gateway` adapter sessions
          pick the same credentials up.
        - ``adapter_<name>_<field>`` ->
          :func:`encre.gateway.adapter_manager.AdapterManager.apply_config`
          for the running instance, and a merge into
          ``adapter_configs`` for persistence.
        - ``custom_slash_commands``, ``keybinds`` -> their own
          persistence helpers.
        - ``network_proxy_*`` -> persisted like every other UI key, then
          mirrored onto the proxy environment variables by
          :func:`encre.proxy_env.apply_network_proxy` and the backend is
          rebuilt so LLM clients pick the new tunnel up.  Empty strings
          are honoured here (clearing a field is a real operation).
        - ``permission_settings`` -> :func:`Agent.safety.set_policies`
          for immediate enforcement plus a merge into config.  An empty
          dict is a read request and returns the persisted values.

        Only one :func:`_persist_config` call runs, at the very end, so
        no partially updated configuration can reach disk.  The reply is
        followed by a unified data push so every panel refreshes from one
        consistent snapshot.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientConfigure` request carrying the patch.
        """
        session = (
            self._manager.get_session(self._current_session_id)
            if self._current_session_id else None
        )
        if session is None:
            session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        # Snapshot BEFORE mutating: the reply push is a diff against this
        # snapshot, so only the fields the patch actually touched reach the
        # frontend instead of a full config_data rebuild.
        before_snapshot = await asyncio.to_thread(self._build_config_data, session)
        _backend_keys = {"backend_type", "api_key", "base_url", "model"}
        # Proxy keys ride along with the rebuild trigger: LLM clients cache
        # their httpx transport at construction, so a proxy change must
        # rebuild the backend for the new tunnel to take effect.
        _network_proxy_keys = {k for k in msg.config if k.startswith("network_proxy_")}
        _rebuild = (_backend_keys | _network_proxy_keys) & set(msg.config.keys())
        logger.info("[configure] keys=%s, rebuild=%s", list(msg.config.keys()), _rebuild)
        for key, value in msg.config.items():
            # Proxy fields treat "" as an intentional clear (emptying the
            # username/password/host is a real operation there); every other
            # key keeps the skip-empty contract so a partially filled form
            # cannot zero out working settings.
            if value is None or (value == "" and not key.startswith("network_proxy_")):
                logger.info("[configure] skip key=%s (empty/null)", key)
                continue
            if key == "default_search_engine_url":
                set_search_engine_url(str(value))
            if hasattr(session.agent.config, key):
                old_val = getattr(session.agent.config, key)
                setattr(session.agent.config, key, value)
                logger.info("[configure] set %s: %r -> %r", key, old_val, value)
            else:
                logger.warning("[configure] key=%s NOT found on EncreConfig, skipping", key)
        # Sync general settings (language, language_preference, etc.) to
        # _default_config so newly created sessions (new_session / resume)
        # inherit them instead of reverting to "auto"/"zh".
        for key in _GENERAL_SETTINGS_KEYS:
            if key in msg.config and msg.config.get(key) != "" and hasattr(self._default_config, key):
                setattr(self._default_config, key, msg.config[key])
        # Force clear the live session's system-prompt cache whenever
        # language or language_preference changes.  The cache is keyed on
        # the old language, so leaving it would make the next turn keep
        # using the previous language's instructions.
        if {"language", "language_preference"} & set(msg.config.keys()):
            if hasattr(session, "agent") and hasattr(session.agent, "loop"):
                session.agent.loop._sys_prompt_cache = None
                logger.info("[configure] cleared _sys_prompt_cache due to language change")
        if _rebuild:
            session.agent.rebuild_backend()
            logger.info("[configure] backend rebuilt due to key change")
            # Mirror the backend keys into _default_config so adapter and
            # iClaw sessions spawned by the EventRouter use the same
            # credentials instead of the stale startup values.
            for key in _backend_keys:
                if key in msg.config and msg.config.get(key):
                    setattr(self._default_config, key, msg.config[key])
            if "models" in msg.config and isinstance(msg.config["models"], list):
                self._default_config.models = [
                    ModelConfig.from_dict(m) if isinstance(m, dict) else m
                    for m in msg.config["models"]
                ]
                self._default_config.apply_active_model()
                logger.info("[configure] synced models to _default_config")
        if self._adapter_manager:
            adapter_keys = {k: v for k, v in msg.config.items() if k.startswith("adapter_")}
            if adapter_keys:
                await self._adapter_manager.apply_config(adapter_keys)
                # Persist adapter configs on EncreConfig so they survive restart
                parsed: dict[str, dict[str, Any]] = {}
                for ak, av in adapter_keys.items():
                    parts = ak.split("_", 2)
                    if len(parts) >= 3:
                        parsed.setdefault(parts[1], {})[parts[2]] = av
                # Handle unbind: when every credential field for a weixin
                # adapter is empty, remove the config entirely rather than
                # persisting an empty record.
                for aid, fields in list(parsed.items()):
                    if aid == "weixin" and "app_id" in fields and "token" in fields and not fields.get("app_id") and not fields.get("token"):
                        session.agent.config.adapter_configs.pop(aid, None)
                        self._default_config.adapter_configs.pop(aid, None)
                        parsed.pop(aid, None)
                        continue
                    # Merge rather than replace, so fields the client did
                    # not resend (for example push_chat_id) survive the
                    # partial update.
                    if aid in session.agent.config.adapter_configs:
                        session.agent.config.adapter_configs[aid].update(fields)
                    else:
                        session.agent.config.adapter_configs[aid] = fields
                    if aid in self._default_config.adapter_configs:
                        self._default_config.adapter_configs[aid].update(fields)
                    else:
                        self._default_config.adapter_configs[aid] = fields
                logger.info("[configure] applied %d adapter config keys and persisted", len(adapter_keys))
        if "custom_slash_commands" in msg.config:
            custom_cmds = msg.config["custom_slash_commands"]
            if isinstance(custom_cmds, list):
                save_custom_slash_commands(custom_cmds)
                logger.info("[configure] saved %d custom slash commands", len(custom_cmds))
        if "keybinds" in msg.config:
            raw = msg.config["keybinds"]
            if isinstance(raw, dict):
                save_keybinds(raw)
                logger.info("[configure] saved keybinds (%d entries)", len(raw.get("keybinds", [])))
        if "permission_settings" in msg.config:
            raw = msg.config["permission_settings"]
            if isinstance(raw, dict):
                capability_keys = {
                    "network", "file", "bash_io", "docker", "browser",
                    "workflow", "git", "deploy", "desktop", "database", "misc", "mcp",
                }
                # An empty dict is a read request: return the persisted
                # settings instead of clearing the live ones.
                if not raw:
                    msg.config["permission_settings"] = dict(session.agent.config.permission_settings)
                else:
                    # Split the flat key/value pairs into the two shapes
                    # the safety layer understands: named capabilities
                    # (network, file, bash_io, ...) and per-tool policies.
                    tools: dict[str, str] = {}
                    capabilities: dict[str, str] = {}
                    for key, value in raw.items():
                        if not isinstance(value, str):
                            continue
                        if key in capability_keys:
                            capabilities[key] = value
                        else:
                            tools[key] = value
                    session.agent.safety.set_policies(tools, capabilities)
                    session.agent.config.permission_settings = {**session.agent.config.permission_settings, **raw}
                    logger.info("[configure] applied permission_settings (%d tools, %d capabilities)", len(tools), len(capabilities))
        # One write at the very end: models, adapters, UI settings and
        # permission_settings all reach disk in a single pass, so a
        # partially updated configuration can never be persisted.
        self._persist_config(session)
        if _network_proxy_keys:
            # Mirror the freshly persisted proxy settings onto the process
            # environment.  Runs AFTER the persist so load_settings() reads
            # the values that just reached disk -- the same source of truth
            # the startup path applies.
            try:
                from encre.proxy_env import apply_network_proxy
                apply_network_proxy(load_settings())
            except Exception as exc:
                logger.warning("[configure] network proxy apply failed: %s", exc)
        await self._send(ws, "configured", config=msg.config)
        # Config changed -- push ONLY the fields the patch touched.  A full
        # unified push here re-walked every session index, memory store and
        # telemetry file to refresh panels the edited keys never affect; the
        # incremental patch carries the same payload shape, so the frontend
        # applies it through the same config applier.
        await self._send_config_patch(ws, session, before_snapshot)

    async def _h_wechat_scan(self, ws: Any, msg: ClientWechatScan) -> None:
        """Request a platform QR code for login and start background polling.

        Supports every scan-login adapter (``weixin``, ``qqbot``, ``wecom``);
        the target platform comes from ``msg.adapter_id``.  A QR URL can be
        fetched without a live, fully connected adapter, so if no matching
        instance is running a temporary one is built from the stored config.  Once the URL
        is delivered the handler spawns a tracked polling task that waits for
        scan confirmation.

        Every failure mode replies with ``wechat_scan_result`` carrying
        ``success=False`` rather than raising, so the login dialog always
        gets a usable answer.
        """
        platform = str(msg.adapter_id or "weixin").strip() or "weixin"
        if not self._adapter_manager:
            await self._send(ws, "wechat_scan_result",
                qrcode_url="", success=False,
                message="Server not ready")
            return
        try:
            # Prefer a running adapter instance; otherwise build a
            # temporary one so get_qrcode_url() works without the
            # adapter being fully connected (QR fetch needs no token).
            instances = getattr(self._adapter_manager, "_instances", {}) or {}
            adapter = instances.get(platform)
            if adapter is None:
                from encre.gateway.platform_registry import platform_registry
                from encre.gateway.config import PlatformConfig as _PC
                entry = platform_registry.get(platform)
                if entry is None:
                    await self._send(ws, "wechat_scan_result",
                        qrcode_url="", success=False,
                        message=f"{platform} adapter class not found")
                    return
                # Carry stored endpoint config through when present, so a
                # self-hosted endpoint keeps working.
                stored = getattr(self._adapter_manager, "_stored_configs", {}).get(platform, {})
                if platform == "weixin":
                    _cfg = _PC(
                        enabled=False,
                        token=stored.get("token", ""),
                        extra={"base_url": stored.get("base_url", "")},
                    )
                else:
                    _cfg = _PC(enabled=False, extra={})
                adapter = entry.adapter_factory(_cfg)
            if not hasattr(adapter, "get_qrcode_url"):
                await self._send(ws, "wechat_scan_result",
                    qrcode_url="", success=False,
                    message=f"{platform} adapter does not support QR login")
                return
            url, qrcode_token = await adapter.get_qrcode_url()
            await self._send(ws, "wechat_scan_result",
                qrcode_url=url, success=True, message="")
            # Cancel any previous poll task for this platform: its late
            # scan_confirmed/qr_expired events must not clobber the new QR.
            old_task = self._wechat_poll_tasks.pop(platform, None)
            if old_task and not old_task.done():
                old_task.cancel()
            # Poll for scan confirmation in the background; the socket
            # may come and go while the user is scanning.
            if qrcode_token:
                _t = asyncio.ensure_future(
                    self._poll_wechat_scan(ws, adapter, qrcode_token, platform=platform))
                self._tasks.add(_t)
                self._wechat_poll_tasks[platform] = _t
        except Exception as e:
            await self._send(ws, "wechat_scan_result",
                qrcode_url="", success=False,
                message=str(e))

    async def _h_get_config(self, ws: Any, msg: ClientGetConfig) -> None:
        """Return the current configuration data and available skills.

        The config builder performs disk and file-system work, so it runs
        on a worker thread; on failure it falls back to serialising the
        live config object directly.  Skill discovery is attached as
        ``available_skills``, and the workspace context plus the
        session-files directory are injected so the renderer can wire its
        panels up in a single round trip.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)

        try:
            config_data = await asyncio.to_thread(self._build_config_data, info)
        except Exception:
            config_data = info.agent.config.to_dict(encrypt_api_keys=False)
        available = await self._build_skills_list(info)
        config_data["available_skills"] = available
        config_data["workspace_mode"] = "iwork" if self._workspace_path else "normal"
        config_data["workspace_path"] = self._workspace_path
        from encre.protocol.handlers.base import _apply_session_files_dir
        _apply_session_files_dir(config_data, info.session_id)
        await self._send(ws, "config_data", config=config_data)

    def _models_updated_payload(self, info: Any) -> dict[str, Any]:
        """Build the unified ``models_updated`` payload.

        Carries the flattened model list (for the model selector), the
        provider-centric list (for the settings panel), the active
        model index, and the owning session id -- one consistent snapshot
        from a single source of truth.  Every caller that needs to announce
        a model/provider change goes through this method so the fields can
        never drift apart.

        Args:
            info: The session whose agent config is the source of truth.

        Returns:
            A dict ready to be splatted into :func:`_send` as keyword
            arguments.  API keys are always excluded from the serialised
            models.
        """
        cfg = info.agent.config
        models_dict = _inject_context_windows([
            m.to_dict(encrypt_api_keys=False) if isinstance(m, ModelConfig) else m
            for m in cfg.models
        ])
        providers_dict = [
            p.to_dict(encrypt_api_keys=False) if isinstance(p, ProviderConfig) else p
            for p in cfg.providers
        ]
        return {
            "models": models_dict,
            "providers": providers_dict,
            "active_model_index": cfg.active_model_index,
            # Tag the frame with the session whose config it describes so a
            # background conversation's model event can never repaint the
            # selector of the conversation the user is actually looking at.
            "session_id": info.session_id,
        }

    async def _send_models_updated(self, ws: Any, info: Any) -> None:
        """Emit one ``models_updated`` frame using the unified payload.

        Thin wrapper over :func:`_models_updated_payload` so that all
        model/provider mutation handlers send byte-identical frames.
        """
        await self._send(ws, "models_updated", **self._models_updated_payload(info))

    def _remember_session_model(self, info: Any, index: int) -> None:
        """Persist the active model on the session that actually uses it.

        The flat model list is rebuilt from providers at every launch, so an
        index alone cannot survive a restart.  Store the model's identity in
        the session metadata instead; ``load_or_create_session`` restores it
        for general-mode conversations.

        Args:
            info: Session whose model was selected.
            index: Selected position in ``info.agent.config.models``.
        """
        cfg = info.agent.config
        if not 0 <= index < len(cfg.models):
            return
        session = getattr(info.agent, "session", None)
        identity = model_identity(cfg.models[index])
        if session is None or identity is None:
            return
        session.metadata["active_model"] = identity
        try:
            self._manager._schedule_save(info)
        except Exception:
            logger.debug("[model scope] session save failed", exc_info=True)

    def _sync_model_scope(self, info: Any, index: int) -> None:
        """Apply a model selection to the storage scope that owns it.

        General mode: the conversation keeps its own choice, and the
        connection default mirrors it so the next new conversation starts
        where this one is.

        Workspace mode: the choice belongs to the workspace, not the
        conversation.  It is written into ``.encre/config.json`` and pushed
        onto every resident session of the same workspace, which is what
        keeps all conversations of one project on one model.  The global
        default is deliberately left untouched so one workspace's model can
        never leak into another workspace or into general mode.

        Args:
            info: Session the selection was made in.
            index: Selected position in ``info.agent.config.models``.
        """
        cfg = info.agent.config
        if not 0 <= index < len(cfg.models):
            return
        self._remember_session_model(info, index)
        workspace = (getattr(self, "_workspace_path", "") or "").strip()
        if workspace:
            identity = model_identity(cfg.models[index])
            if identity is not None and os.path.isdir(workspace):
                _save_workspace_config(workspace, {"active_model": identity})
            self._propagate_workspace_model(info, identity, workspace)
            return
        self._default_config.active_model_index = index
        self._default_config.apply_active_model()

    def _propagate_workspace_model(
        self, info: Any, identity: dict[str, str] | None, workspace: str,
    ) -> None:
        """Move every resident conversation of *workspace* onto *identity*.

        Each session is resolved independently: their model lists can differ
        after an edit performed on another connection, so the index from the
        requesting session is meaningless in a sibling's list.

        Args:
            info: Session that initiated the switch (already updated).
            identity: Durable model identity to apply.
            workspace: Absolute workspace path shared by the siblings.
        """
        if identity is None:
            return
        for other in self._manager.iter_sessions():
            if other is info:
                continue
            other_cfg = other.agent.config
            if (other_cfg.workspace or "").strip() != workspace:
                continue
            other_index = resolve_model_index(other_cfg, identity)
            if other_index is None:
                continue
            if getattr(other_cfg.models[other_index], "enabled", True) is False:
                continue
            other_cfg.active_model_index = other_index
            other_cfg.apply_active_model()
            # Rebuild idle siblings now so their context-window gauge reflects
            # the shared model's window as soon as they are opened. A running
            # sibling is left alone: its next run rebuilds from this config.
            task = getattr(other, "agent_task", None)
            if task is None or task.done():
                try:
                    other.agent.rebuild_backend()
                except Exception:
                    logger.debug("[model scope] sibling backend rebuild failed", exc_info=True)
            session = getattr(other.agent, "session", None)
            if session is not None:
                session.metadata["active_model"] = dict(identity)
            try:
                self._manager._schedule_save(other)
            except Exception:
                logger.debug("[model scope] sibling save failed", exc_info=True)

    async def _h_update_providers(self, ws: Any, msg: ClientUpdateProviders) -> None:
        """Replace the provider list, rebuild the backend, and persist.

        Providers are the durable container for models: replacing the
        list re-derives the flat ``config.models`` view, re-resolves
        context windows, and rebuilds the backend so the new credentials
        take effect immediately.  The change is mirrored into
        ``_default_config`` so sessions created afterwards inherit it.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientUpdateProviders` request carrying the
                full provider list and the intended active model index.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        # Snapshot BEFORE mutating: the reply push is a diff against this
        # snapshot so only providers/models (and fields derived from them)
        # reach the frontend.
        before_snapshot = await asyncio.to_thread(self._build_config_data, info)
        providers = [
            ProviderConfig.from_dict(p) if isinstance(p, dict) else p
            for p in msg.providers
        ]
        # Provider names share ONE namespace: built-in providers are
        # identified by their official catalog label, custom ones
        # (openai_compatible) by the user-defined name.  Duplicates are
        # rejected outright -- e.g. a custom provider may not masquerade as
        # "DeepSeek" when the built-in DeepSeek is already configured.  The
        # display name is what the model selector shows, so ambiguity
        # there would make it impossible to tell which endpoint a model
        # actually talks to.
        from encre.backends.catalog import PROVIDERS as _backend_providers
        label_by_id = {str(p.get("id", "")): str(p.get("label", "")) for p in _backend_providers}
        seen: dict[str, str] = {}
        for p in providers:
            if not isinstance(p, ProviderConfig) or not p.backend_type:
                continue
            if p.backend_type == "openai_compatible":
                display = p.name.strip()
                if not display:
                    await self._send(ws, "error",
                        message="Custom provider requires a name", code="provider_name_required")
                    return
            else:
                display = label_by_id.get(p.backend_type, p.backend_type)
            key = display.casefold()
            if key in seen:
                await self._send(ws, "error",
                    message=f"Duplicate provider name '{display}'", code="duplicate_provider")
                return
            seen[key] = display
        info.agent.config.providers = providers
        info.agent.config.active_model_index = msg.active_model_index
        _resolve_model_context_windows(info.agent.config.models)
        info.agent.config.apply_active_model()
        info.agent.rebuild_backend()
        # Providers are shared, but the active model is scoped: a general
        # conversation only mirrors it into the connection default, while a
        # workspace session writes it to the project and to its siblings.
        self._default_config.providers = providers
        self._sync_model_scope(info, msg.active_model_index)
        logger.info("[update_providers] providers=%d, calling _persist_config", len(providers))
        self._persist_config(info)
        await self._send_models_updated(ws, info)
        # Providers changed -- push the config delta only.  The
        # ``models_updated`` frame above covers the selector's immediate
        # needs; the patch refreshes every other config-bound panel without
        # re-pushing the whole unified snapshot.
        await self._send_config_patch(ws, info, before_snapshot)

    async def _send_context_usage_frame(self, ws: Any, info: Any) -> None:
        """Push a fresh ``context_usage`` frame for *info*'s live backend.

        Used after a model switch: the gauge must re-baseline on the NEW
        model's context window immediately, otherwise a switch to a 1M
        model keeps rendering against the old 128K cap (and a switch down
        to 128K keeps showing the old, larger budget).
        """
        session = getattr(info.agent, "session", None)
        if session is None:
            return
        try:
            from encre.utils.tokens import count_message_tokens

            ctx_tokens = count_message_tokens(session.get_context_messages())
        except Exception:
            return
        backend = getattr(getattr(info.agent, "loop", None), "backend", None)
        window = 0
        if backend is not None:
            try:
                window = int(backend.context_window_size() or 0)
            except Exception:
                window = 0
        await self._send(
            ws,
            "context_usage",
            context_tokens=ctx_tokens,
            context_window=window,
            session_id=info.session_id,
        )

    async def _h_set_active_model(self, ws: Any, msg: ClientSetActiveModel) -> None:
        """Activate a model by index and rebuild the backend.

        The chosen model must be enabled; disabled entries are refused so
        the UI cannot strand a session on credentials that will never
        authenticate.  Context windows are re-resolved and the backend is
        rebuilt because ``backend_type`` / ``api_key`` / ``base_url`` are
        all properties of the active model, not of the provider list.

        The selection is scoped: a general-mode conversation remembers it
        for itself (and the connection default mirrors it for the next new
        conversation), while a workspace conversation writes it to the
        project's ``.encre/config.json`` and applies it to every resident
        conversation of that workspace.  Workspace choices never touch the
        global default, so projects stay isolated from each other.

        A switch is also RECORDED like a tool call: a persisted UI notice on
        the session plus a live ``model_switch`` frame, so the desktop shows
        the "模型已从 A 更改为 B" line for manual switches exactly as it does
        for automatic model-pool cycling.  A fresh ``context_usage`` frame
        follows so the context gauge re-baselines on the NEW model's window
        immediately (a 1M model must not keep rendering against the old
        128K cap, and a switch down to 128K must show the smaller budget
        at once).
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        if 0 <= msg.model_index < len(info.agent.config.models):
            # Refuse to activate a disabled model.
            target = info.agent.config.models[msg.model_index]
            if not target.enabled:
                await self._send(ws, "error",
                    message=f"Model '{target.name}' is disabled", code="model_disabled")
                return
            _prev_model = ""
            try:
                _prev_active = info.agent.config.get_active_model()
                _prev_model = getattr(_prev_active, "model_id", "") or ""
            except Exception:
                _prev_model = ""
            info.agent.config.active_model_index = msg.model_index
            _resolve_model_context_windows(info.agent.config.models)
            info.agent.config.apply_active_model()
            info.agent.rebuild_backend()
            # Remember the choice in the scope that owns it (this session in
            # general mode, the whole workspace in iwork mode).
            self._sync_model_scope(info, msg.model_index)
            self._persist_config(info)
            await self._send_models_updated(ws, info)
            # ── Record + announce the switch ─────────────────────────
            _new_model = getattr(target, "model_id", "") or getattr(target, "name", "")
            session = getattr(info.agent, "session", None)
            # A conversation that has not started yet gets NO switch record:
            # there is no round to anchor the line to, and telling the user
            # "model changed" about a conversation with no content is noise.
            _has_turn = session is not None and any(
                m.get("role") == "user" and not _is_synthetic_user_message(m)
                for m in session.messages
            )
            if _new_model and _new_model != _prev_model and _has_turn:
                _notice_id = f"ms-{int(time.time() * 1000)}-{msg.model_index}"
                _notice: dict = {}
                if session is not None:
                    try:
                        _notice = session.record_ui_notice(
                            "model_switch",
                            {
                                "from_model": _prev_model,
                                "to_model": _new_model,
                                "reason": "",
                                "source": "manual",
                            },
                            notice_id=_notice_id,
                        ) or {}
                    except Exception:
                        logger.warning("[set_active_model] notice record failed", exc_info=True)
                    # Persist the notice with the session (no turn is
                    # running, so nothing else would trigger a save).
                    try:
                        self._manager._schedule_save(info)
                    except Exception:
                        logger.debug("[set_active_model] schedule_save failed", exc_info=True)
                # Consecutive switches with no round in between coalesce on
                # the session: the returned notice carries the net change
                # (chain-start from_model -> final model) under the ORIGINAL
                # id, so live clients update the existing line in place.
                await self._send(
                    ws,
                    "model_switch",
                    from_model=str(_notice.get("from_model", _prev_model)),
                    to_model=str(_notice.get("to_model", _new_model)),
                    reason=str(_notice.get("reason", "")),
                    source=str(_notice.get("source", "manual")),
                    notice_id=str(_notice.get("id", _notice_id)),
                    anchor_id=str(_notice.get("anchor_id", "")),
                    session_id=info.session_id,
                )
            # Re-baseline the context gauge on the new backend's window.
            await self._send_context_usage_frame(ws, info)
        else:
            await self._send(ws, "error",
                message="Invalid model index", code="invalid_index")

    async def _h_delete_model(self, ws: Any, msg: ClientDeleteModel) -> None:
        """Delete a model from its owning provider and rebuild the backend.

        The flat ``config.models`` view is index-addressed by the client,
        but models actually live under providers, so the target is located
        by object identity and removed from the provider that owns it.
        The active index is then adjusted for the shift and clamped into
        range -- deleting below the active index moves it, and deleting the
        last model must not leave a dangling index.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientDeleteModel` request.

        Raises:
            Nothing; failures are reported as ``error`` frames so the
            settings panel can surface them without disconnecting.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        try:
            flat = info.agent.config.models
            if 0 <= msg.model_index < len(flat):
                logger.info("[delete_model] deleting index=%d, total_models=%d",
                            msg.model_index, len(flat))
                # Locate the owning provider by object identity: the flat
                # view stamps the same ModelConfig instances the providers
                # hold, so ``is`` is both fast and unambiguous.
                target = flat[msg.model_index]
                for provider in info.agent.config.providers:
                    for j, m in enumerate(provider.models):
                        if m is target:
                            del provider.models[j]
                            break
                    else:
                        continue
                    break
                if msg.model_index < info.agent.config.active_model_index:
                    info.agent.config.active_model_index -= 1
                if info.agent.config.active_model_index >= len(info.agent.config.models):
                    info.agent.config.active_model_index = max(0, len(info.agent.config.models) - 1)
                if info.agent.config.models:
                    _resolve_model_context_windows(info.agent.config.models)
                    info.agent.config.apply_active_model()
                    info.agent.rebuild_backend()
                # Mirror the whole provider set and the adjusted index so
                # new sessions do not resurrect the deleted model.  The
                # active model itself follows the same scope rule as a
                # manual switch.
                self._default_config.providers = info.agent.config.providers
                if info.agent.config.models:
                    self._sync_model_scope(info, info.agent.config.active_model_index)
                else:
                    self._default_config.active_model_index = info.agent.config.active_model_index
                self._persist_config(info)
                await self._send_models_updated(ws, info)
                logger.info("[delete_model] done, remaining=%d", len(info.agent.config.models))
            else:
                logger.warning("[delete_model] invalid index %d (max %d)",
                               msg.model_index, len(flat))
                await self._send(ws, "error",
                    message="Invalid model index", code="invalid_index")
        except Exception as exc:
            logger.error("[delete_model] failed: %s\n%s", exc, traceback.format_exc())
            await self._send(ws, "error", message=f"Delete model failed: {exc}", code="handler_error")

    async def _h_fetch_models(self, ws: Any, msg: ClientFetchModels) -> None:
        """Fetch the model catalogue from a provider endpoint.

        Builds a throw-away backend from the supplied credentials --
        nothing is persisted here -- and asks it for its model list.  The
        returned ids are passed through unmodified, so the settings UI
        decides how to render them.  A failure to construct the backend
        (unknown provider type) and a failure to call it (network or auth
        error) are reported separately so the dialog can show actionable
        text instead of a generic failure.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientFetchModels` request carrying
                provider credentials and an optional ``extra_headers``
                map for gateways that need auth headers.
        """
        from encre.backend import create_backend
        from encre.backends.base import format_backend_error
        backend = create_backend(
            msg.backend_type,
            api_key=msg.api_key,
            base_url=msg.base_url,
            model="",
            extra_headers=msg.headers or None,
        )
        if backend is None:
            await self._send(ws, "error",
                message=f"Unknown backend type: {msg.backend_type}", code="api_error")
        else:
            model_ids: list[str] = []
            try:
                model_ids = await backend.list_models()
            except Exception as e:
                await self._send(ws, "error",
                    message=format_backend_error(e, "Failed to fetch models:"),
                    code="api_error")
            finally:
                await backend.aclose()
            if model_ids:
                await self._send(ws, "models_fetched", models=model_ids)

    async def _h_validate_model(self, ws: Any, msg: ClientValidateModel) -> None:
        """Validate a model endpoint and probe its live capabilities.

        Constructs a throw-away backend from the supplied credentials and
        fires a minimal non-streaming chat to confirm the endpoint answers.
        On success the handler runs two optional live probes:

        * :func:`~encre.backends.multimodal.probe_backend_capabilities` --
          reports whether the endpoint accepts multimodal input, so the
          dialog can disable the toggle instead of failing the save.
        * :func:`~encre.backends.multimodal.probe_reasoning_levels` --
          reports which reasoning levels the provider currently accepts;
          this result, never a static table, drives the level selector.

        Nothing is persisted here: the results are returned so the provider
        dialog can attach them to the model row before calling
        ``update_providers``.  Probe failures are logged and degraded to
        ``"unknown"`` rather than failing validation, because a flaky
        probe should not block a working credential.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientValidateModel` request.
        """
        from dataclasses import replace

        from encre.backend import create_backend
        from encre.backends.base import format_backend_error
        from encre.backends.multimodal import (
            _ProbeTimeoutError,
            _PROBE_CHAT_TIMEOUT,
            _PROBE_REASONING_TIMEOUT,
            _PROBE_TOTAL_TIMEOUT,
            _validate_chat,
            probe_backend_capabilities,
            probe_reasoning_levels,
        )
        from encre.backends.retry import DEFAULT_RETRY_CONFIG

        def _timed_out(kind: str) -> str:
            """Format a probe timeout message.

            Args:
                kind: Which probe timed out (``"auth"`` or ``"models"``).

            Returns:
                A human-readable message naming the model and the probe.
            """
            return f"{msg.model_id}: {kind} timed out"

        # Probes must never retry: the retry engine would amplify one hung
        # endpoint into minutes of backoff, which is exactly why validation
        # always timed out.  Fail fast and report the real error instead.
        probe_retry = replace(DEFAULT_RETRY_CONFIG, max_retries=0, rate_limit_retries=0)
        # The sub-page's sampling overrides are part of the candidate config:
        # stamp them on every probe backend so relays that force-require
        # temperature/top_p/top_k don't reject a perfectly usable model.
        _sampling = {
            k: v
            for k, v in {
                "temperature": msg.temperature,
                "top_p": msg.top_p,
                "top_k": msg.top_k,
            }.items()
            if v is not None
        }
        backend = create_backend(
            msg.backend_type,
            api_key=msg.api_key,
            base_url=msg.base_url,
            model=msg.model_id,
            extra_headers=msg.headers or None,
            retry_config=probe_retry,
            http_timeout=_PROBE_CHAT_TIMEOUT,
            **_sampling,
        )
        if backend is None:
            await self._send(ws, "model_validation_error",
                model_id=msg.model_id,
                request_id=msg.request_id,
                message=f"Unknown backend type: {msg.backend_type}")
        else:
            validated = False
            caps: dict[str, str] = {}
            probe_multimodal = "unknown"
            reasoning_levels: list[str] = []
            reasoning_support = "unknown"
            try:
                # The whole validation is capped: a hung endpoint must be
                # able to fail the request instead of holding the WebSocket
                # open for minutes.  The chat timeout is deliberately shorter
                # than the default backend timeout so an unresponsive "hi"
                # reply is surfaced quickly.
                try:
                    await asyncio.wait_for(
                        _validate_chat(backend, msg.max_tokens),
                        timeout=_PROBE_CHAT_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    raise _ProbeTimeoutError(_timed_out("connectivity probe"))
                validated = True
                # Full capability probe runs synchronously here so the
                # dialog waits for the result before closing.
                try:
                    caps = await asyncio.wait_for(
                        probe_backend_capabilities(
                            backend, include_multimodal=bool(msg.multimodal),
                        ),
                        timeout=_PROBE_TOTAL_TIMEOUT,
                    )
                    if msg.multimodal:
                        probe_multimodal = caps.get("multimodal_input", "unknown")
                except asyncio.TimeoutError:
                    logger.warning(
                        "[validate_model] capability probe timed out for %s",
                        msg.model_id,
                    )
                except Exception:
                    logger.warning(
                        "[validate_model] capability probe failed for %s: %s",
                        msg.model_id, traceback.format_exc(),
                    )
                if msg.multimodal and probe_multimodal == "unsupported":
                    # Provider-level capability toggle: the endpoint simply
                    # does not support multimodal -- record the probe result
                    # and let the caller decide (no hard failure; the model
                    # is saved without multimodal enabled).
                    logger.info(
                        "[validate_model] multimodal unsupported for %s",
                        msg.model_id,
                    )

                # Reasoning-level verification: every candidate level is
                # probed live against the endpoint; only accepted levels are
                # reported.  Providers change level support constantly, so
                # this result -- never a static table -- drives the frontend
                # level selector.  Skipped entirely when the capability
                # toggle is off (no probe, nothing recorded).
                if msg.probe_reasoning:
                    try:
                        reasoning_levels, reasoning_support = await asyncio.wait_for(
                            probe_reasoning_levels(
                                msg.backend_type, msg.api_key, msg.base_url, msg.model_id,
                                headers=msg.headers or None,
                                sampling=_sampling,
                            ),
                            timeout=_PROBE_REASONING_TIMEOUT,
                        )
                        caps["reasoning"] = reasoning_support
                    except asyncio.TimeoutError:
                        logger.warning(
                            "[validate_model] reasoning probe timed out for %s",
                            msg.model_id,
                        )
                    except Exception:
                        reasoning_levels, reasoning_support = [], "unknown"
                        logger.warning(
                            "[validate_model] reasoning probe failed for %s: %s",
                            msg.model_id, traceback.format_exc(),
                        )
            except _ProbeTimeoutError as e:
                await self._send(ws, "model_validation_error",
                    model_id=msg.model_id,
                    request_id=msg.request_id,
                    message=str(e))
            except Exception as e:
                await self._send(ws, "model_validation_error",
                    model_id=msg.model_id,
                    request_id=msg.request_id,
                    message=format_backend_error(e, "Validation failed:"))
            finally:
                await backend.aclose()

            if validated:
                # Validate-only: probe results are returned to the caller so
                # the provider dialog can attach them to the model row before
                # persisting via ``update_providers``.  Nothing is saved here.
                await self._send(ws, "model_validated",
                    backend_type=msg.backend_type,
                    model_id=msg.model_id,
                    request_id=msg.request_id,
                    multimodal_support=probe_multimodal,
                    reasoning_levels=reasoning_levels,
                    capabilities=caps)

    async def _h_update_mcp(self, ws: Any, msg: ClientUpdateMCP) -> None:
        """Replace the MCP server list, persist it, and reconnect.

        Accepts two input shapes so the settings panel and external clients
        can both round-trip easily: the standard ``mcpServers`` map
        (``{name: {config}, ...}``) and a flat list of entry dicts.  In the
        map form each entry is given a default transport of ``stdio`` unless
        one is specified, and ``transport`` is normalised onto ``type``.

        Both the JSON sidecar and the main config are written before the
        reconnect, so a crash between the two leaves the JSON authoritative.
        Failures are reported as ``error`` frames without disconnecting.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientUpdateMCP` request carrying the full
                server list (empty list to remove all servers).
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        try:
            # Accept both the flat list form and the standard mcpServers
            # map form ({name: {config}, ...}).
            raw = msg.mcp_servers
            if isinstance(raw, dict):
                # Map form: lift each config under its key as "name".
                servers = []
                for name, cfg in raw.items():
                    entry: dict[str, Any] = {"name": name, **cfg}
                    # Normalise the transport field name onto "type".
                    if "type" not in entry and "transport" in entry:
                        entry["type"] = entry.pop("transport")
                    if "type" not in entry:
                        entry["type"] = "stdio"
                    servers.append(entry)
            elif isinstance(raw, list):
                servers = list(raw)
            else:
                servers = []

            logger.info("[update_mcp] updating %d servers", len(servers))
            # Unified plugin pipeline: every server becomes an ``mcp-<name>``
            # declarative plugin package under the user plugin root.  The
            # package manifest declares ``contributes.mcp_servers`` so the
            # plugin loader injects the entry into ``config.mcp_servers``
            # on every startup — the folder/config store is no longer the
            # install target, the plugin root is.
            from encre.agent import _get_shared_plugin_registry_raw
            from encre.config import get_data_dir
            from encre.protocol.handlers.base import safe_pkg_name

            registry = _get_shared_plugin_registry_raw()
            plugins_root = Path(get_data_dir("plugins"))
            wanted: dict[str, dict[str, Any]] = {}
            for srv in servers:
                name = str(srv.get("name", "")).strip()
                if not name:
                    continue
                wanted[safe_pkg_name(name)] = srv

            # Reconcile removals: plugin packages whose server is gone.
            if plugins_root.is_dir():
                for entry in list(plugins_root.iterdir()):
                    if not (entry.is_dir() and entry.name.startswith("mcp-")):
                        continue
                    safe = entry.name[len("mcp-"):]
                    if safe in wanted:
                        continue
                    plugin_name = f"mcp-{safe}"
                    registry.unregister(plugin_name)
                    registry.record_uninstalled(plugin_name)
                    shutil.rmtree(str(entry), ignore_errors=True)
                    logger.info("[update_mcp] removed stale MCP plugin %s", plugin_name)

            # (Re)write one declarative package per server.
            for safe, srv in wanted.items():
                name = str(srv.get("name", "")).strip()
                plugin_name = f"mcp-{safe}"
                pkg = plugins_root / f"mcp-{safe}"
                if pkg.exists():
                    shutil.rmtree(str(pkg), ignore_errors=True)
                pkg.mkdir(parents=True, exist_ok=True)
                (pkg / "encre-plugin.json").write_text(
                    json.dumps({
                        "name": plugin_name,
                        "version": "1.0.0",
                        "description": f"MCP server plugin: {name}",
                        "tier": "user",
                        "contributes": {"mcp_servers": [{"name": name, **{
                            k: v for k, v in srv.items() if k != "name"
                        }}]},
                    }, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                registry.record_installed(plugin_name, "1.0.0", source="user",
                                          pkg_dir=str(pkg))
            registry.discover_all()

            # Persist: the plugin loader re-injects the servers on every
            # startup, so the persisted config list stays empty — the plugin
            # root is the single source of truth.  The JSON sidecar keeps a
            # read-only mirror for external tools (Claude Code compat).
            info.agent.config.mcp_servers = []
            self._persist_mcp_json(info, servers)
            self._persist_config(info)
            # Runtime: live list so reconnect picks the new set up now.
            info.agent.config.mcp_servers = servers
            await info.agent.reconnect_mcp()
            await self.broadcast_plugin_changed(registry.snapshot_states())
            await self.broadcast_plugin_ui_sync()
            await self._send(ws, "mcp_updated", mcp_servers=servers)
            logger.info("[update_mcp] done")
        except Exception as exc:
            logger.error("[update_mcp] failed: %s\n%s", exc, traceback.format_exc())
            await self._send(ws, "error", message=f"MCP update failed: {exc}", code="handler_error")

    async def _h_update_agent(self, ws: Any, msg: ClientUpdateAgent) -> None:
        """Update the live agent configuration and persist it.

        Each field is applied independently: a missing or empty value is
        ignored so a partial update never wipes a previously set prompt or
        mode.  ``max_turns`` uses a ``> 0`` guard rather than a truthiness
        check because 0 is also a meaningful "unbounded" sentinel in the
        config model.

        The response echoes the resulting values (not just what changed)
        so the settings panel can reconcile its local state with the
        server in one frame.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientUpdateAgent` request; every field is
                optional.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        if msg.system_prompt:
            info.agent.config.system_prompt = msg.system_prompt
        if msg.specialty:
            info.agent.config.default_specialty = msg.specialty
        if msg.permission_mode:
            info.agent.config.permission_mode = msg.permission_mode
        if msg.max_turns > 0:
            info.agent.config.max_turns = msg.max_turns
        self._persist_config(info)
        await self._send(ws, "agent_updated", config={
            "system_prompt": info.agent.config.system_prompt,
            "specialty": info.agent.config.default_specialty,
            "permission_mode": info.agent.config.permission_mode,
            "max_turns": info.agent.config.max_turns,
        })

    async def _h_export_data(self, ws: Any, msg: ClientExportData) -> None:
        """Export the entire data directory as a plaintext zip archive.

        Decrypts every file into a plaintext zip on disk, then hands back
        its path so the UI can attach it to a download.  The heavy work
        runs in a worker thread (``asyncio.to_thread``) so the event loop
        stays responsive during large exports.

        Progress is streamed as ``data_export_progress`` events, throttled
        to roughly 150 ms so thousands of small files cannot flood the
        socket.  The final tick is always emitted regardless of the
        throttle.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientExportData` request; ``request_id`` is
                echoed back on the result frame so the UI can match it
                against the progress stream.
        """
        from encre.migration import export_all
        loop = asyncio.get_running_loop()
        last_state: dict[str, object] = {"pct": -1, "t": 0.0}

        def _export_progress(done: int, total: int, cur: str) -> None:
            """Stream export progress events, throttled to ~150 ms."""
            pct = int(done * 100 / max(total, 1))
            now = loop.time()
                # Throttle to ~150 ms so thousands of small files cannot
                # flood the socket; the final tick is always emitted.
            if done < total and now - last_state["t"] < 0.15 and pct == last_state["pct"]:
                return
            last_state["pct"] = pct
            last_state["t"] = now
            loop.call_soon_threadsafe(
                lambda cur=cur: loop.create_task(
                    self._send(
                        ws, "data_export_progress",
                        done=done, total=total, percent=pct, file=cur,
                    )
                )
            )

        try:
            zpath = await asyncio.to_thread(export_all, None, _export_progress)
            await self._send(
                ws, "data_exported_zip",
                zip_path=str(zpath), filename=zpath.name,
                request_id=msg.request_id,
            )
        except Exception as e:
            logger.error("[export_data] failed: %s", e)
            await self._send(ws, "error", message=str(e), code="export_data_error")

    async def _h_import_data(self, ws: Any, msg: ClientImportData) -> None:
        """Import a data archive, re-encrypting with this machine's key.

        Reads the plaintext zip from the user-picked path (the same
        machine that produced it), re-encrypts every file under this
        machine's fresh key, and writes it into the data directory.
        ``msg.mode`` selects the conflict policy (overwrite vs. skip).

        Progress is streamed as ``data_import_progress`` events with the
        same 150 ms throttle as export.  The final ``data_import_done``
        frame reports counts for restored, skipped, overwritten and kept
        files so the UI can summarise the outcome.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientImportData` request carrying the zip
                path and the conflict mode.
        """
        from encre.migration import import_all
        if not msg.zip_path:
            await self._send(ws, "error", message="No zip path provided", code="invalid_request")
            return
        loop = asyncio.get_running_loop()
        last_state = {"pct": -1, "t": 0.0}

        def _import_progress(done: int, total: int, cur: str) -> None:
            """Stream import progress events, throttled to ~150 ms."""
            pct = int(done * 100 / max(total, 1))
            now = loop.time()
            if done < total and now - last_state["t"] < 0.15 and pct == last_state["pct"]:
                return
            last_state["pct"] = pct
            last_state["t"] = now
            loop.call_soon_threadsafe(
                lambda cur=cur: loop.create_task(
                    self._send(
                        ws, "data_import_progress",
                        done=done, total=total, percent=pct, file=cur,
                    )
                )
            )

        try:
            result = await asyncio.to_thread(import_all, msg.zip_path, msg.mode, _import_progress)
            await self._send(
                ws, "data_import_done",
                files=result["files"], restored=result["restored"], skipped=result["skipped"],
                overwritten=result["overwritten"], kept=result["kept"],
            )
        except Exception as e:
            logger.error("[import_data] failed: %s", e)
            await self._send(ws, "error", message=str(e), code="import_data_error")

    async def _h_agent_list(self, ws: Any, msg: ClientAgentList) -> None:
        """Return the configured agent profiles and the active index."""
        info = self._get_or_create_session()
        agents = [a.to_dict() for a in info.agent.config.agents]
        await self._send(ws, "agents_list", agents=agents, active_index=info.agent.config.active_agent_index)

    async def _h_agent_create(self, ws: Any, msg: ClientAgentCreate) -> None:
        """Append a new agent profile and persist the full list."""
        info = self._get_or_create_session()
        agent_data = dict(msg.agent)
        agent = AgentConfig.from_dict(agent_data)
        info.agent.config.agents.append(agent)
        self._persist_config(info)
        agents = [a.to_dict() for a in info.agent.config.agents]
        await self._send(ws, "agents_updated", agents=agents, active_index=info.agent.config.active_agent_index)

    async def _h_agent_delete(self, ws: Any, msg: ClientAgentDelete) -> None:
        """Delete an agent profile by index and persist.

        An out-of-range index is logged and ignored rather than raising,
        so a stale UI cannot corrupt the list.  The active index is
        clamped when the last profile is removed.  The full list is
        always echoed back, even on a no-op delete, so the client can
        reconcile.
        """
        info = self._get_or_create_session()
        idx = msg.index
        total_before = len(info.agent.config.agents)
        logger.info("[agent_delete] index=%d, total_before=%d", idx, total_before)
        if 0 <= idx < total_before:
            deleted_name = info.agent.config.agents[idx].name
            del info.agent.config.agents[idx]
            logger.info("[agent_delete] deleted agent '%s' at index %d", deleted_name, idx)
            if info.agent.config.active_agent_index >= len(info.agent.config.agents):
                info.agent.config.active_agent_index = len(info.agent.config.agents) - 1
                logger.info("[agent_delete] adjusted active_agent_index to %d", info.agent.config.active_agent_index)
            self._persist_config(info)
        else:
            logger.warning("[agent_delete] invalid index %d (total=%d)", idx, total_before)
        agents = [a.to_dict() for a in info.agent.config.agents]
        await self._send(ws, "agents_updated", agents=agents, active_index=info.agent.config.active_agent_index)
        logger.info("[agent_delete] done, remaining=%d", len(agents))

    async def _h_agent_update(self, ws: Any, msg: ClientAgentUpdate) -> None:
        """Update an agent profile in place and persist.

        An out-of-range index is a silent no-op so a stale client cannot
        shift the list.  The response echoes the full list regardless of
        whether anything changed, keeping the client and server trivially
        reconcilable.
        """
        info = self._get_or_create_session()
        idx = msg.index
        if 0 <= idx < len(info.agent.config.agents):
            agent_data = dict(msg.agent)
            updated = AgentConfig.from_dict(agent_data)
            info.agent.config.agents[idx] = updated
            self._persist_config(info)
        agents = [a.to_dict() for a in info.agent.config.agents]
        await self._send(ws, "agents_updated", agents=agents, active_index=info.agent.config.active_agent_index)

    async def _h_agent_set_active(self, ws: Any, msg: ClientAgentSetActive) -> None:
        """Activate an agent profile by index and persist.

        ``-1`` is a valid index meaning "no profile" (the default agent),
        which is why the lower bound is ``-1`` rather than ``0``.  An
        out-of-range positive index is ignored so a stale client cannot
        point at a profile that no longer exists.
        """
        info = self._get_or_create_session()
        idx = msg.index
        if -1 <= idx < len(info.agent.config.agents):
            info.agent.config.active_agent_index = idx
            self._persist_config(info)
        agents = [a.to_dict() for a in info.agent.config.agents]
        await self._send(ws, "agents_updated", agents=agents, active_index=info.agent.config.active_agent_index)

    async def _h_update_sub_agents(self, ws: Any, msg: ClientUpdateSubAgents) -> None:
        """Replace the sub-agent list, persist, and send the visible set.

        The whole list is written in one pass (no merge), so the request
        is authoritative.  Profiles marked ``hidden`` are persisted but
        filtered out of the response, since they exist for internal
        routing and should not appear in the picker.

        Args:
            ws: The requesting WebSocket.
            msg: The :class:`ClientUpdateSubAgents` request carrying the
                full replacement list (an empty list removes all).
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        try:
            logger.info("[update_sub_agents] updating, count=%d", len(msg.sub_agents))
            sub_agents = [
                SubAgentConfig.from_dict(s) if isinstance(s, dict) else s
                for s in msg.sub_agents
            ]
            # Unified plugin pipeline: every sub-agent becomes an
            # ``agent-<name>`` declarative plugin package under the user
            # plugin root.  The manifest declares ``contributes.agents`` so
            # the plugin loader injects the definition into
            # ``config.sub_agents`` on every startup — alongside the main
            # agent, never altering its identity.  The persisted
            # ``sub_agents`` list stays empty; the plugin root is the
            # single source of truth.
            from encre.agent import _get_shared_plugin_registry_raw
            from encre.config import get_data_dir
            from encre.protocol.handlers.base import safe_pkg_name

            registry = _get_shared_plugin_registry_raw()
            plugins_root = Path(get_data_dir("plugins"))
            wanted: dict[str, dict[str, Any]] = {}
            for sa in sub_agents:
                data = sa.to_dict() if isinstance(sa, SubAgentConfig) else dict(sa)
                name = str(data.get("name", "")).strip()
                if not name:
                    continue
                wanted[safe_pkg_name(name)] = data

            # Reconcile removals: plugin packages whose agent is gone.
            if plugins_root.is_dir():
                for entry in list(plugins_root.iterdir()):
                    if not (entry.is_dir() and entry.name.startswith("agent-")):
                        continue
                    safe = entry.name[len("agent-"):]
                    if safe in wanted:
                        continue
                    plugin_name = f"agent-{safe}"
                    registry.unregister(plugin_name)
                    registry.record_uninstalled(plugin_name)
                    shutil.rmtree(str(entry), ignore_errors=True)
                    logger.info("[update_sub_agents] removed stale agent plugin %s", plugin_name)

            # (Re)write one declarative package per sub-agent.
            for safe, data in wanted.items():
                name = str(data.get("name", "")).strip()
                plugin_name = f"agent-{safe}"
                pkg = plugins_root / f"agent-{safe}"
                if pkg.exists():
                    shutil.rmtree(str(pkg), ignore_errors=True)
                pkg.mkdir(parents=True, exist_ok=True)
                (pkg / "encre-plugin.json").write_text(
                    json.dumps({
                        "name": plugin_name,
                        "version": "1.0.0",
                        "description": str(data.get("description", "")) or f"Agent plugin: {name}",
                        "tier": "user",
                        "contributes": {"agents": [data]},
                    }, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                registry.record_installed(plugin_name, "1.0.0", source="user",
                                          pkg_dir=str(pkg))
            registry.discover_all()

            # Persist empty; the plugin loader re-injects on startup.
            info.agent.config.sub_agents = []
            self._persist_config(info)
            # Runtime: live list so the picker reflects the edit right away;
            # reload_plugins() then re-injects the plugin packages with
            # name reconciliation so nothing is duplicated.
            info.agent.config.sub_agents = sub_agents
            info.agent.reload_plugins()
            await self.broadcast_plugin_changed(registry.snapshot_states())
            await self.broadcast_plugin_ui_sync()
            sub_agents_dict = [
                s.to_dict() if isinstance(s, SubAgentConfig) else s
                for s in sub_agents if not getattr(s, "hidden", False)
            ]
            await self._send(ws, "sub_agents_updated", sub_agents=sub_agents_dict)
            logger.info("[update_sub_agents] done")
        except Exception as exc:
            logger.error("[update_sub_agents] failed: %s\n%s", exc, traceback.format_exc())
            await self._send(ws, "error", message=f"Sub agents update failed: {exc}", code="handler_error")

    async def _h_get_usage_stats(self, ws: Any, msg: ClientGetUsageStats) -> None:
        """Return aggregated usage statistics for all sessions."""
        await self._send(ws, "usage_stats", stats=self._build_usage_stats())

    def _build_usage_stats(self) -> dict[str, Any]:
        """Aggregate usage stats and resolve model display names.

        Reads the raw telemetry rollup and enriches it with human-readable
        model names from the live config.  A model that is no longer in
        the user's config is kept as its raw id with ``model_status`` of
        ``"deleted"`` -- the historical record must survive config churn
        rather than silently becoming ``(unknown model)``.  ``"unknown"``
        ids get ``model_status`` of ``"unknown"``.

        The per-model breakdown is re-keyed onto display names so tokens
        from the same model but different ids do not split into separate
        bars.  Any failure returns a fully zeroed shape so the UI never
        has to handle a missing field.
        """
        try:
            from encre.telemetry import EncreTelemetry
            stats = EncreTelemetry.get_all_sessions_usage()
            model_names: dict[str, str] = {}
            if self._default_config:
                for mc in self._default_config.models:
                    mid = (mc.model_id or "").strip()
                    if mid and mc.name:
                        model_names[mid] = mc.name
            if stats.get("sessions"):
                for s in stats["sessions"]:
                    raw = (s.get("model", "") or "").strip()
                    if not raw or raw == "unknown":
                        s["model"] = "(unknown model)"
                        s["model_status"] = "unknown"
                    elif raw in model_names:
                        s["model"] = model_names[raw]
                        s["model_status"] = "active"
                    else:
                        # Model is no longer in the user's config: keep the
                        # raw id so the historical record is preserved.
                        s["model"] = raw
                        s["model_status"] = "deleted"
            if stats.get("model_breakdown"):
                mb: dict[str, dict[str, Any]] = {}
                for raw, data in stats["model_breakdown"].items():
                    if not raw or raw == "unknown":
                        display = "(unknown model)"
                    elif raw in model_names:
                        display = model_names[raw]
                    else:
                        display = raw
                    if display in mb:
                        for k in ("input_tokens", "output_tokens", "total_tokens", "turns"):
                            mb[display][k] = mb[display].get(k, 0) + data.get(k, 0)
                    else:
                        mb[display] = dict(data)
                stats["model_breakdown"] = mb
            return stats
        except Exception:
            return {
                "total_sessions": 0, "total_tokens": 0,
                "total_input_tokens": 0, "total_output_tokens": 0,
                "total_tool_calls": 0,
                "tool_call_breakdown": {},
                "model_breakdown": {},
                "sessions": [],
            }
