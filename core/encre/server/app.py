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

"""Encre server orchestrator.

Defines :class:`EncreServer`, the top-level object that wires together the
session manager, the code-index manager, the adapter (gateway) manager and the
automation scheduler, and serves both the WebSocket agent channel and the HTTP
admin endpoints.  :func:`run_server` is the synchronous convenience entry point
used by the CLI and the background service.
"""

import asyncio
import contextlib
import logging
from typing import Any

from encre.gateway.run import GatewayRunner
from encre.capabilities.search.codebase.index_manager import IndexManager
from encre.config import EncreConfig
from encre.crypto import ensure_keyfile
from encre.scheduler import EncreScheduler
from encre.server.admin import handle_admin
from encre.server.session_manager import SessionManager
from encre.transport.ws import EncreWSHandler

logger = logging.getLogger("encre.server")


class EncreServer:
    """Top-level server that wires the agent runtime to the network.

    Owns the :class:`~encre.server.session_manager.SessionManager`, the
    code-index manager, the adapter (gateway) manager and the automation
    scheduler, and serves both the agent WebSocket (via
    :class:`~encre.transport.ws.EncreWSHandler`) and the HTTP admin endpoints.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 7110,
        config: EncreConfig | None = None,
        max_concurrent: int = 20,
        idle_timeout: float = 3600.0,
        scheduler: EncreScheduler | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.config = config or EncreConfig()
        self._manager = SessionManager(
            max_concurrent=max_concurrent,
            idle_timeout=idle_timeout,
        )
        self._index_manager = IndexManager()
        self._adapter_manager = GatewayRunner(
            session_manager=self._manager,
            config=self.config,
            status_callback=self._broadcast_gateway_status,
        )
        # Create embedded scheduler for automation tasks (or use provided one from iClaw)
        self._scheduler = scheduler or EncreScheduler()
        self._ws_handler = EncreWSHandler(
            self._manager, config=self.config,
            index_manager=self._index_manager,
            adapter_manager=self._adapter_manager,
            scheduler=self._scheduler,
        )
        self._cleanup_task: asyncio.Task[None] | None = None
        self._plugin_warmup_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()

    @property
    def session_manager(self) -> SessionManager:
        """The server-owned session manager (launcher-profile seam)."""
        return self._manager

    @property
    def scheduler(self) -> EncreScheduler:
        """The embedded automation scheduler (launcher-profile seam)."""
        return self._scheduler

    async def _handle_connection(self, ws) -> None:
        """Dispatch one WebSocket connection to the message handler."""
        try:
            await self._ws_handler.handle(ws)
        except Exception:
            logger.exception("Unhandled error in WebSocket handler")

    async def _handle_http(self, path: str, request_headers) -> tuple[int, str, dict[str, str]] | None:
        result = handle_admin(path, self._manager, request_headers=request_headers)
        if result is None:
            return None
        status, body, header_list = result
        headers_dict = {k: v for k, v in header_list}
        return status, body, headers_dict

    def _safe_broadcast_automation_update(self, job: Any) -> None:
        """Wrapper around broadcast_automation_update that logs errors.

        Called by the scheduler's on_complete callback.  The scheduler calls
        this synchronously from _execute_job (inside the event loop thread).
        """
        try:
            self._ws_handler.broadcast_automation_update(job)
        except Exception:
            logger.exception("[automation] broadcast_automation_update failed")

    async def start(self) -> None:
        try:
            import websockets
        except ImportError:
            raise ImportError(
                "websockets library is required to run the server. "
                "Install it with: pip install websockets"
            ) from None

        logger.info(f"Starting encre server on {self.host}:{self.port}")

        async def process_request(path, request_headers):
            # Block cross-site browser handshakes (drive-by WebSocket
            # hijacking): the desktop renderer connects over file:///custom
            # schemes or Node, never from an http(s) page on another host.
            from encre.server.admin import _origin_is_local
            if not _origin_is_local(request_headers):
                return (403, [("Content-Type", "text/plain")], b"Forbidden")
            if path and path.startswith("/ws"):
                return None  # Let websockets handle it
            # Handle HTTP admin endpoints.  Some of them (git status/diff/
            # push/pull/...) shell out to git with timeouts of up to 60s;
            # running that inline would freeze the whole event loop (and
            # every live agent session), so it goes to a worker thread.
            result = await asyncio.to_thread(
                handle_admin, path, self._manager, request_headers
            )
            if result is None:
                return (404, [], b"Not Found")
            status, body, header_list = result
            if isinstance(body, str):
                body = body.encode()
            return (status, header_list, body)

        self._ws_server = await websockets.serve(
            self._handle_connection,
            self.host,
            self.port,
            process_request=process_request,
            # Keepalive must tolerate long model responses: a slow stream burst
            # (or a briefly saturated renderer) can delay keepalive pong
            # processing past the old 10s budget, and the server then kills an
            # otherwise healthy connection with `1011 keepalive ping timeout`.
            ping_interval=30,
            ping_timeout=60,
            max_size=10 * 1024 * 1024,  # 10MB max message
        )
        # Resolve actual port when port=0 (OS-assigned)
        actual_port = self.port
        if actual_port == 0 and self._ws_server.sockets:
            actual_port = self._ws_server.sockets[0].getsockname()[1]

        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        # Warm up the shared plugin registry in the background so the first
        # session creation never blocks the event loop on hundreds of imports.
        self._plugin_warmup_task = asyncio.create_task(self._warmup_plugins())

        # Start gateway runner (adapter lifecycle manager)
        try:
            await self._adapter_manager.start()
        except Exception as e:
            logger.error("Failed to start gateway runner: %s", e)
            logger.warning("Gateway not available -- QQ, Telegram, etc. will not connect")

        # Sync adapter configs (from saved settings) into EncreConfig so the frontend
        # sees adapter_* keys in config_data immediately -- not "not configured".
        # Only the actual adapter connection (HTTP/WS) is deferred to background.
        try:
            from encre.settings_manager import load_settings
            saved_config = load_settings()
            if saved_config:
                adapter_cfgs: dict[str, dict[str, Any]] = {}
                for k, v in saved_config.items():
                    if k.startswith("adapter_"):
                        parts = k.split("_", 2)
                        if len(parts) >= 3:
                            adapter_cfgs.setdefault(parts[1], {})[parts[2]] = v
                if adapter_cfgs:
                    self.config.adapter_configs.update(adapter_cfgs)
                    logger.info("Loaded adapter configs from saved settings: %s", list(adapter_cfgs.keys()))
        except Exception as e:
            logger.warning("Failed to load adapter configs from settings: %s", e)

        # Start adapter connections in background (network I/O may be slow)
        _t = asyncio.ensure_future(self._start_adapters())
        self._background_tasks.add(_t)

        # Start the embedded scheduler for automation tasks
        try:
            await self._scheduler.start(self._make_scheduler_agent_factory())
            # Register callback so frontend gets notified when jobs complete
            self._scheduler.on_job_complete(
                lambda job: self._safe_broadcast_automation_update(job)
            )
            self._scheduler.on_job_progress(
                lambda job, event_type, event_data: self._ws_handler.broadcast_automation_progress(job, event_type, event_data)
            )
            logger.info("Automation scheduler started (poll_interval=30s)")
        except Exception as e:
            logger.warning("Failed to start automation scheduler: %s", e)

        logger.info(f"Server ready: ws://{self.host}:{actual_port}/ws")
        logger.info(f"Admin API: http://{self.host}:{actual_port}/health")
        # Also emit to stdout for parent processes that parse stdout
        print(f"Server ready: ws://{self.host}:{actual_port}/ws", flush=True)

    async def _start_adapters(self) -> None:
        """Background task: apply saved config and start adapter connections.

        Runs asynchronously so the server reports "ready" immediately and
        the desktop client's 30s timeout doesn't fire. Adapter configs are
        already synced to self.config.adapter_configs synchronously so the
        frontend sees adapter_* keys in config_data right away.

        Delayed ~8s: starting an adapter lazily imports its SDK (feishu's
        lark_oapi alone is a ~6s CPU-bound import). Running that while the
        first desktop client is fetching config/session lists starves those
        requests (GIL) and stretches the splash phase to ~10s.
        """
        try:
            await asyncio.sleep(8)
            from encre.settings_manager import load_settings
            saved_config = load_settings()
            if saved_config:
                await self._adapter_manager.apply_config(saved_config)
                logger.info("Applied adapter configs from saved settings")
        except Exception as e:
            logger.error("Failed to apply adapter configs: %s", e)

    def _automation_model_enabled(self, job_config: dict[str, Any]) -> bool:
        """Return True when the job's snapshot model still exists and is enabled.

        Guards against automations pinning a model that the user has since
        disabled (or deleted) in the model settings panel. Matching on
        ``model_id`` + ``base_url`` keeps distinct providers apart.
        """
        models = getattr(self.config, "models", None)
        mid = job_config.get("model_id")
        if not models or not mid:
            return False
        burl = (job_config.get("base_url") or "").rstrip("/")
        for m in models:
            if getattr(m, "enabled", True) and m.model_id == mid and (m.base_url or "").rstrip("/") == burl:
                return True
        return False

    def _make_scheduler_agent_factory(self):
        """Create an agent factory for automation jobs.

        Same pattern as _run_sub_agent: fresh agent with bypass permissions,
        builder-generated system prompt, no custom overrides. The snapshot
        model config is only applied when the model is still enabled; a job
        pinned to a disabled/deleted model runs with the active model instead.
        """
        from encre.agent import EncreAgent

        def _factory(job_config: dict[str, Any] | None = None) -> EncreAgent:
            from encre.modes.mode_profiles import AgentMode
            agent = EncreAgent(config=self.config, mode=AgentMode.AUTOMATION, defer_plugins=True)
            agent.config.permission_mode = "bypass"
            if job_config:
                if self._automation_model_enabled(job_config):
                    agent.config.backend_type = job_config.get("backend_type", agent.config.backend_type)
                    agent.config.api_key = job_config.get("api_key", agent.config.api_key)
                    agent.config.base_url = job_config.get("base_url", agent.config.base_url)
                    agent.config.model = job_config.get("model_id", agent.config.model)
                    agent.config.max_tokens = job_config.get("max_tokens", agent.config.max_tokens)
                else:
                    logger.warning(
                        "[automation] job model_id=%s disabled/unavailable; "
                        "running job with the active model instead",
                        job_config.get("model_id"),
                    )
                agent.rebuild_backend()
            return agent

        return _factory

    async def _broadcast_gateway_status(self, status: dict) -> None:
        """Called by GatewayRunner when adapter status changes."""
        self._ws_handler.broadcast_gateway_status(status)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            removed = await self._manager.cleanup_idle()
            if removed:
                logger.debug(f"Cleaned up {removed} idle sessions")

    async def _warmup_plugins(self) -> None:
        """Discover/activate plugins and load skills off the event loop.

        Discovery imports every bundled EA package (hundreds of modules) and
        loading skills reads hundreds of ``SKILL.md`` files; doing both in a
        worker thread keeps the server responsive and makes the first session
        creation cheap because the shared registries are already primed.
        """
        try:
            from encre.agent import _get_shared_plugin_registry, _get_shared_skill_registry

            registry = await asyncio.to_thread(_get_shared_plugin_registry)
            await asyncio.to_thread(_get_shared_skill_registry)
            logger.info(f"Plugin registry warmed up ({len(registry._plugins)} plugins)")
        except Exception as e:  # noqa: BLE001 - warmup is best-effort
            logger.warning(f"Plugin warmup failed: {e}")

    async def serve_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Future()  # Run forever
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._index_manager.shutdown()
        try:
            await self._adapter_manager.stop()
        except Exception as e:
            logger.warning("Error stopping adapters: %s", e)
        try:
            await self._scheduler.stop()
            logger.info("Automation scheduler stopped")
        except Exception as e:
            logger.warning("Error stopping scheduler: %s", e)
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
        if self._plugin_warmup_task:
            self._plugin_warmup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._plugin_warmup_task
        if hasattr(self, '_ws_server') and self._ws_server:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        await self._manager.shutdown()
        try:
            from encre.tools.runtime import get_search_manager as _get_manager
            mgr = _get_manager()
            if mgr is not None:
                await mgr.close()
        except Exception:
            pass
        logger.info("Server stopped")


def run_server(
    host: str = "localhost",
    port: int = 7110,
    config: EncreConfig | None = None,
    max_concurrent: int = 20,
    config_file: str | None = None,
    scheduler: Any | None = None,
) -> None:
    """Convenience function to run the server synchronously."""
    # Ensure the crypto keyfile exists before anything else
    ensure_keyfile()

    if config_file:
        config = EncreConfig.from_file(config_file)
    elif config is None:
        config = EncreConfig.from_file()  # Try auto-discovery

    # Merge general settings from encrypted settings.json
    if config is not None:
        try:
            from encre.settings_manager import _GENERAL_SETTINGS_KEYS, load_settings
            stored = load_settings()
            for key in _GENERAL_SETTINGS_KEYS:
                if key in stored and hasattr(config, key):
                    setattr(config, key, stored[key])
        except Exception:
            pass

    server = EncreServer(
        host=host,
        port=port,
        config=config,
        max_concurrent=max_concurrent,
        scheduler=scheduler,
    )
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        logger.info("Server interrupted, shutting down...")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Encre Agent Server")
    parser.add_argument("--host", default="localhost", help="Host to bind to")
    parser.add_argument("--port", type=int, default=7110, help="Port to bind to")
    parser.add_argument("--config", help="Path to config file (yaml/toml)")
    parser.add_argument("--max-concurrent", type=int, default=20, help="Max concurrent agent sessions")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--service", action="store_true", help="Run as background service (daemon mode)")

    args = parser.parse_args()

    if args.service:
        from encre.server.service import run_service
        run_service(
            host=args.host,
            port=args.port,
            config_file=args.config,
            max_concurrent=args.max_concurrent,
            log_level=args.log_level,
        )
    else:
        logging.basicConfig(
            level=getattr(logging, args.log_level),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

        config = None
        if args.config:
            config = EncreConfig.from_file(args.config)

        run_server(
            host=args.host,
            port=args.port,
            config=config,
            max_concurrent=args.max_concurrent,
        )
