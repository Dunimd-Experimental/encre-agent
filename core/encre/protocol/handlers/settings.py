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

"""Settings-domain handlers.

Backend/model configuration, agent profiles, MCP servers, permission
settings, data import/export, gateway scan and usage statistics.
Extracted verbatim from ``encre.transport.ws`` (architecture refactor
Stage 3 / Task 5.2); behaviour is unchanged, only the layout moved.
Outer-loop ``continue`` statements of the original dispatch chain became
``return`` inside the extracted methods.
"""

import asyncio
import logging
import traceback
from typing import Any

from encre.config import (
    AgentConfig,
    ModelConfig,
    SubAgentConfig,
    _thinking_config_from_dict,
)
from encre.keybinds import save_keybinds
from encre.protocol.handlers.base import _inject_context_windows, _resolve_model_context_windows
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
    ClientUpdateModels,
    ClientUpdateSubAgents,
    ClientValidateModel,
    ClientWechatScan,
)
from encre.settings_manager import (
    _GENERAL_SETTINGS_KEYS,
    save_custom_slash_commands,
)
from encre.tools.runtime import set_search_engine_url

logger = logging.getLogger("encre.transport.ws")


class SettingsHandlers:
    """Model / config / MCP / agent / data handlers."""

    async def _h_ping(self, ws: Any, msg: ClientPing) -> None:
        if self._info:
            self._manager.touch(self._info.session_id)
        await self._send(ws, "pong")

    async def _h_list_models(self, ws: Any, msg: ClientListModels) -> None:
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        backend = info.agent.loop.backend
        if backend is None:
            # No backend configured yet - return empty model list
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
        session = (
            self._manager.get_session(self._current_session_id)
            if self._current_session_id else None
        )
        if session is None:
            session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        _backend_keys = {"backend_type", "api_key", "base_url", "model"}
        _rebuild = _backend_keys & set(msg.config.keys())
        logger.info("[configure] keys=%s, rebuild=%s", list(msg.config.keys()), _rebuild)
        for key, value in msg.config.items():
            if value == "" or value is None:
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
        # language or language_preference changes, so the next turn
        # immediately rebuilds with the new language instruction.
        if {"language", "language_preference"} & set(msg.config.keys()):
            if hasattr(session, "agent") and hasattr(session.agent, "loop"):
                session.agent.loop._sys_prompt_cache = None
                logger.info("[configure] cleared _sys_prompt_cache due to language change")
        if _rebuild:
            session.agent.rebuild_backend()
            logger.info("[configure] backend rebuilt due to key change")
            #   Sync backend config to _default_config so EventRouter/adapter sessions
            # get it too
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
                # Handle unbind: if all weixin credential fields are empty, remove config
                for aid, fields in list(parsed.items()):
                    if aid == "weixin" and "app_id" in fields and "token" in fields and not fields.get("app_id") and not fields.get("token"):
                        session.agent.config.adapter_configs.pop(aid, None)
                        self._default_config.adapter_configs.pop(aid, None)
                        parsed.pop(aid, None)
                        continue
                    # Merge into adapter_configs so existing fields (e.g. push_chat_id)
                    # are not lost when only a subset of keys is sent.
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
                # Empty dict is a read request: return the persisted settings.
                if not raw:
                    msg.config["permission_settings"] = dict(session.agent.config.permission_settings)
                else:
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
        # Single write at the very end: models, adapters, UI settings and
        # permission_settings are all persisted in one pass, so a partially
        # updated configuration can never reach disk.  (Previously this ran
        # twice -- once here and once before permission_settings was merged --
        # through two different stores that could disagree.)
        self._persist_config(session)
        await self._send(ws, "configured", config=msg.config)
        # Config changed 鈫?unified push so the frontend store
        # (model selector, settings, sessions) refreshes from one
        # consistent snapshot.
        await self._push_all_data(ws)

    async def _h_wechat_scan(self, ws: Any, msg: ClientWechatScan) -> None:
        if not self._adapter_manager:
            await self._send(ws, "wechat_scan_result",
                qrcode_url="", success=False,
                message="Server not ready")
            return
        try:
            # Prefer a running adapter instance; otherwise build a
            # temporary one so get_qrcode_url() works without
            # the adapter being fully connected (QR fetch needs no token).
            instances = getattr(self._adapter_manager, "_instances", {}) or {}
            adapter = instances.get("weixin")
            if adapter is None:
                from encre.gateway.platform_registry import platform_registry
                from encre.gateway.config import PlatformConfig as _PC
                entry = platform_registry.get("weixin")
                if entry is None:
                    await self._send(ws, "wechat_scan_result",
                        qrcode_url="", success=False,
                        message="WeChat adapter class not found")
                    return
                # Use stored base_url if available, otherwise defaults
                stored = getattr(self._adapter_manager, "_stored_configs", {}).get("weixin", {})
                _cfg = _PC(
                    enabled=False,
                    token=stored.get("token", ""),
                    extra={"base_url": stored.get("base_url", "")},
                )
                adapter = entry.adapter_factory(_cfg)
            if not hasattr(adapter, "get_qrcode_url"):
                await self._send(ws, "wechat_scan_result",
                    qrcode_url="", success=False,
                    message="WeChat adapter does not support QR login")
                return
            url, qrcode_token = await adapter.get_qrcode_url()
            await self._send(ws, "wechat_scan_result",
                qrcode_url=url, success=True, message="")
            # Start background polling for scan confirmation
            if qrcode_token:
                _t = asyncio.ensure_future(self._poll_wechat_scan(ws, adapter, qrcode_token))
                self._tasks.add(_t)
        except Exception as e:
            await self._send(ws, "wechat_scan_result",
                qrcode_url="", success=False,
                message=str(e))

    async def _h_get_config(self, ws: Any, msg: ClientGetConfig) -> None:
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
        await self._send(ws, "config_data", config=config_data)

    async def _h_update_models(self, ws: Any, msg: ClientUpdateModels) -> None:
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        models = [
            ModelConfig.from_dict(m) if isinstance(m, dict) else m
            for m in msg.models
        ]
        info.agent.config.models = models
        info.agent.config.active_model_index = msg.active_model_index
        _resolve_model_context_windows(models)
        info.agent.config.apply_active_model()
        info.agent.rebuild_backend()
        # Sync to _default_config so new sessions pick up the models
        self._default_config.models = models
        self._default_config.active_model_index = msg.active_model_index
        self._default_config.apply_active_model()
        logger.info("[update_models] models=%d, calling _persist_config", len(models))
        self._persist_config(info)
        models_dict = _inject_context_windows([
            m.to_dict(encrypt_api_keys=False) if isinstance(m, ModelConfig) else m
            for m in models
        ])
        await self._send(ws, "models_updated",
            models=models_dict, active_model_index=msg.active_model_index)
        # Models changed 鈫?unified push so every panel (selector,
        # settings) sees the fresh model set from one snapshot.
        await self._push_all_data(ws)

    async def _h_set_active_model(self, ws: Any, msg: ClientSetActiveModel) -> None:
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        if 0 <= msg.model_index < len(info.agent.config.models):
            # Refuse to activate a disabled model
            target = info.agent.config.models[msg.model_index]
            if not target.enabled:
                await self._send(ws, "error",
                    message=f"Model '{target.name}' is disabled", code="model_disabled")
                return
            info.agent.config.active_model_index = msg.model_index
            _resolve_model_context_windows(info.agent.config.models)
            info.agent.config.apply_active_model()
            info.agent.rebuild_backend()
            # Sync to _default_config
            self._default_config.active_model_index = msg.model_index
            self._default_config.apply_active_model()
            self._persist_config(info)
            cfg_models = info.agent.config.models
            models_dict = _inject_context_windows([
                m.to_dict(encrypt_api_keys=False) if isinstance(m, ModelConfig) else m
                for m in cfg_models
            ])
            await self._send(ws, "models_updated",
                models=models_dict, active_model_index=msg.model_index)
        else:
            await self._send(ws, "error",
                message="Invalid model index", code="invalid_index")

    async def _h_delete_model(self, ws: Any, msg: ClientDeleteModel) -> None:
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        try:
            if 0 <= msg.model_index < len(info.agent.config.models):
                logger.info("[delete_model] deleting index=%d, total_models=%d",
                            msg.model_index, len(info.agent.config.models))
                del info.agent.config.models[msg.model_index]
                if msg.model_index < info.agent.config.active_model_index:
                    info.agent.config.active_model_index -= 1
                if info.agent.config.active_model_index >= len(info.agent.config.models):
                    info.agent.config.active_model_index = max(0, len(info.agent.config.models) - 1)
                if info.agent.config.models:
                    _resolve_model_context_windows(info.agent.config.models)
                    info.agent.config.apply_active_model()
                    info.agent.rebuild_backend()
                # Sync to _default_config
                self._default_config.models = info.agent.config.models
                self._default_config.active_model_index = info.agent.config.active_model_index
                if info.agent.config.models:
                    self._default_config.apply_active_model()
                self._persist_config(info)
                cfg_models = info.agent.config.models
                models_dict = _inject_context_windows([
                    m.to_dict(encrypt_api_keys=False) if isinstance(m, ModelConfig) else m
                    for m in cfg_models
                ])
                await self._send(ws, "models_updated",
                    models=models_dict, active_model_index=info.agent.config.active_model_index)
                logger.info("[delete_model] done, remaining=%d", len(cfg_models))
            else:
                logger.warning("[delete_model] invalid index %d (max %d)",
                               msg.model_index, len(info.agent.config.models))
                await self._send(ws, "error",
                    message="Invalid model index", code="invalid_index")
        except Exception as exc:
            logger.error("[delete_model] failed: %s\n%s", exc, traceback.format_exc())
            await self._send(ws, "error", message=f"Delete model failed: {exc}", code="handler_error")

    async def _h_fetch_models(self, ws: Any, msg: ClientFetchModels) -> None:
        from encre.backend import create_backend
        from encre.backends.base import format_backend_error
        backend = create_backend(
            msg.backend_type,
            api_key=msg.api_key,
            base_url=msg.base_url,
            model="",
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
        from encre.backend import create_backend
        from encre.backends.base import format_backend_error
        backend = create_backend(
            msg.backend_type,
            api_key=msg.api_key,
            base_url=msg.base_url,
            model=msg.model_id,
        )
        if backend is None:
            await self._send(ws, "model_validation_error",
                message=f"Unknown backend type: {msg.backend_type}")
        else:
            validated = False
            caps: dict[str, str] = {}
            probe_multimodal = "unknown"
            try:
                async for _ in backend.chat(
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=msg.max_tokens,
                    stream=False,
                ):
                    pass
                validated = True
                # Full capability probe runs synchronously here so the
                # dialog waits for the result before closing.
                from encre.backends.multimodal import probe_backend_capabilities
                try:
                    caps = await probe_backend_capabilities(
                        backend, include_multimodal=bool(msg.multimodal),
                    )
                    if msg.multimodal:
                        probe_multimodal = caps.get("multimodal_input", "unknown")
                except Exception:
                    logger.warning(
                        "[validate_model] capability probe failed for %s: %s",
                        msg.model_id, traceback.format_exc(),
                    )
                if msg.multimodal and probe_multimodal == "unsupported":
                    await self._send(ws, "model_validation_error",
                        message=f'The endpoint for "{msg.model_id}" does not support multimodal content (text-only). The multimodal option cannot be enabled for this model.')
                    validated = False
            except Exception as e:
                await self._send(ws, "model_validation_error",
                    message=format_backend_error(e, "Validation failed:"))
            finally:
                await backend.aclose()

            if validated:
              try:
                info = self._get_or_create_session()
                self._manager.touch(info.session_id)
                cfg = info.agent.config
                new_model = ModelConfig(
                    name=msg.name or msg.model_id,
                    model_id=msg.model_id,
                    backend_type=msg.backend_type,
                    api_key=msg.api_key,
                    base_url=msg.base_url,
                    max_tokens=msg.max_tokens or 4096,
                    context_window=0,
                    enabled=True,
                    multimodal=msg.multimodal,
                    multimodal_support=probe_multimodal,
                    capabilities=caps,
                    thinking_config=_thinking_config_from_dict(msg.thinking_config) if msg.thinking_config else None,
                )
                if 0 <= msg.model_index < len(cfg.models):
                    cfg.models[msg.model_index] = new_model
                    active_idx = cfg.active_model_index
                else:
                    existing_idx = next(
                        (i for i, m in enumerate(cfg.models)
                         if m.backend_type == msg.backend_type
                         and m.model_id == msg.model_id
                         and (m.base_url or "") == (msg.base_url or "")),
                        None,
                    )
                    if existing_idx is not None:
                        cfg.models[existing_idx] = new_model
                        active_idx = existing_idx
                    else:
                        cfg.models.append(new_model)
                        active_idx = len(cfg.models) - 1
                cfg.active_model_index = active_idx
                _resolve_model_context_windows(cfg.models)
                cfg.apply_active_model()
                self._default_config.models = list(cfg.models)
                self._default_config.active_model_index = active_idx
                self._default_config.apply_active_model()
                try:
                    info.agent.rebuild_backend()
                except Exception as exc:
                    logger.error("[validate_model] rebuild_backend failed: %s\n%s",
                        exc, traceback.format_exc())
                    await self._send(ws, "model_validation_error",
                        message=f"Validation passed but backend rebuild failed: {exc}")
                    return
                if info.agent.loop.backend is None:
                    logger.error("[validate_model] backend is None after rebuild - config: type=%s api_key=%s base_url=%s",
                        cfg.backend_type, bool(cfg.api_key), cfg.base_url)
                    await self._send(ws, "model_validation_error",
                        message="Backend failed to initialize. Check backend_type/api_key/base_url.")
                    return
                self._persist_config(info)
                models_dict = _inject_context_windows([
                    m.to_dict(encrypt_api_keys=False) for m in cfg.models
                ])
                await self._send(ws, "models_updated",
                    models=models_dict, active_model_index=active_idx)
                await self._send(ws, "model_validated",
                    backend_type=msg.backend_type,
                    model_id=msg.model_id,
                    model_index=active_idx)
              except Exception as exc:
                # The connection test passed but persisting failed.
                # Surface a real error instead of letting the
                # frontend hang until its 30s timeout.
                logger.error("[validate_model] save failed: %s\n%s",
                    exc, traceback.format_exc())
                await self._send(ws, "model_validation_error",
                    message=f"Validation passed but saving failed: {exc}")

    async def _h_update_mcp(self, ws: Any, msg: ClientUpdateMCP) -> None:
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        try:
            # Normalize: accept both list and dict (map) formats
            raw = msg.mcp_servers
            if isinstance(raw, dict):
                # Standard mcpServers map format: {name: {config}, ...}
                servers = []
                for name, cfg in raw.items():
                    entry: dict[str, Any] = {"name": name, **cfg}
                    # Normalize transport field name
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
            info.agent.config.mcp_servers = servers
            self._persist_mcp_json(info, servers)
            self._persist_config(info)
            await info.agent.reconnect_mcp()
            await self._send(ws, "mcp_updated", mcp_servers=servers)
            logger.info("[update_mcp] done")
        except Exception as exc:
            logger.error("[update_mcp] failed: %s\n%s", exc, traceback.format_exc())
            await self._send(ws, "error", message=f"MCP update failed: {exc}", code="handler_error")

    async def _h_update_agent(self, ws: Any, msg: ClientUpdateAgent) -> None:
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
        # Full data-dir export: decrypt everything into a plaintext
        # zip on disk, then hand back its path.  Progress is streamed
        # as data_export_progress events so the UI can show a bar.
        from encre.migration import export_all
        loop = asyncio.get_running_loop()
        last_state: dict[str, object] = {"pct": -1, "t": 0.0}

        def _export_progress(done: int, total: int, cur: str) -> None:
            pct = int(done * 100 / max(total, 1))
            now = loop.time()
            # Throttle to ~150ms so many small files don't flood the
            # socket, but always emit the final tick.
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
        # Full data-dir import: read the plaintext zip directly from
        # the user-picked path (same machine), re-encrypt with THIS
        # machine's fresh key, and write into the data dir.  Progress
        # is streamed as data_import_progress events.
        from encre.migration import import_all
        if not msg.zip_path:
            await self._send(ws, "error", message="No zip path provided", code="invalid_request")
            return
        loop = asyncio.get_running_loop()
        last_state = {"pct": -1, "t": 0.0}

        def _import_progress(done: int, total: int, cur: str) -> None:
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
        info = self._get_or_create_session()
        agents = [a.to_dict() for a in info.agent.config.agents]
        await self._send(ws, "agents_list", agents=agents, active_index=info.agent.config.active_agent_index)

    async def _h_agent_create(self, ws: Any, msg: ClientAgentCreate) -> None:
        info = self._get_or_create_session()
        agent_data = dict(msg.agent)
        agent = AgentConfig.from_dict(agent_data)
        info.agent.config.agents.append(agent)
        self._persist_config(info)
        agents = [a.to_dict() for a in info.agent.config.agents]
        await self._send(ws, "agents_updated", agents=agents, active_index=info.agent.config.active_agent_index)

    async def _h_agent_delete(self, ws: Any, msg: ClientAgentDelete) -> None:
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
        info = self._get_or_create_session()
        idx = msg.index
        if -1 <= idx < len(info.agent.config.agents):
            info.agent.config.active_agent_index = idx
            self._persist_config(info)
        agents = [a.to_dict() for a in info.agent.config.agents]
        await self._send(ws, "agents_updated", agents=agents, active_index=info.agent.config.active_agent_index)

    async def _h_update_sub_agents(self, ws: Any, msg: ClientUpdateSubAgents) -> None:
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        try:
            logger.info("[update_sub_agents] updating, count=%d", len(msg.sub_agents))
            sub_agents = [
                SubAgentConfig.from_dict(s) if isinstance(s, dict) else s
                for s in msg.sub_agents
            ]
            info.agent.config.sub_agents = sub_agents
            self._persist_config(info)
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
        await self._send(ws, "usage_stats", stats=self._build_usage_stats())

    def _build_usage_stats(self) -> dict[str, Any]:
        """Aggregated usage stats with display-name resolution."""
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
