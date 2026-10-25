#!/usr/bin/env python3

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

"""Top-level orchestrator for the Encre agent server.

:class:`EncreServer` is the composition root of the Python side of Encre.  It
constructs and owns every long-lived manager, hands them all to a single
:class:`encre.transport.ws.EncreWSHandler` instance, and then exposes the two
network surfaces that share one TCP port:

* the agent WebSocket at ``/ws``, where the Electron desktop renderer
  (and any headless driver) talks the client protocol;
* the HTTP admin surface handled by
  :func:`encre.server.admin.handle_admin`, reached through the
  ``process_request`` hook the websockets library calls before any upgrade.

Everything an agent turn needs is reachable from that one handler, so this
module is where the layering rule becomes concrete: transports call in,
this class supplies collaborators, no collaborator ever reaches back for a
network socket of its own.

Startup is deliberately staged.  The port starts listening first -- that is
the "server ready" signal the desktop main process waits on with a short
timeout -- and only afterwards do the expensive or network-bound subsystems
come up, mostly in background tasks so a slow import never delays readiness.
Shutdown runs in the strict reverse order.

Public API
    * :class:`EncreServer`
    * :func:`run_server` -- synchronous entry point for the CLI and the
      background service wrapper in :mod:`encre.server.service`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
from pathlib import Path
from typing import Any

import websockets
from encre.config import EncreConfig
from encre.crypto import ensure_keyfile
from encre.gateway.run import GatewayRunner
from encre.plugins.host import PluginHost
from encre.plugins.tool_meta import collect_tool_meta as _collect_tool_meta
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

    Invariants:

    * Exactly one :class:`EncreWSHandler` exists and every manager is passed
      to it.  There is no second copy of a manager handed to another surface,
      because the handler is the only place client messages are decoded.
    * The plugin host is attached to the shared plugin registry before
      discovery runs, so auto-activation side effects land on this server's
      host instead of being lost to a different process's host.
    * Background tasks created during :func:`start` are recorded so
      :func:`stop` can cancel them; forgetting one leaves it as an
      un-awaited task that outlives the server.

    Lifecycle: construct -> :func:`start` (bind port, spawn subsystems) ->
    :func:`serve_forever` -> :func:`stop` (reverse teardown).  The object is
    bound to a single event loop; :func:`stop` must be awaited on the same
    loop that :func:`start` used.
    """

    #: Image extensions the package root of ``/plugin-assets`` will serve.  A
    #: plugin ships its own icon, so its picture files are readable by any
    #: local page; nothing else inside that root is offered.
    _ASSET_IMAGE_SUFFIXES = frozenset({".svg", ".png", ".webp", ".gif", ".jpg", ".jpeg", ".ico"})

    def __init__(
        self,
        host: str = "localhost",
        port: int = 7110,
        config: EncreConfig | None = None,
        max_concurrent: int = 20,
        idle_timeout: float = 3600.0,
        scheduler: EncreScheduler | None = None,
    ) -> None:
        """Construct the managers but start nothing.

        Construction is synchronous and network-free on purpose: the caller
        can build the object on any thread and defer all I/O to
        :func:`start`.  The construction order matters -- the session manager
        and adapter manager must exist before the WebSocket handler is built,
        since the handler takes all collaborators by reference in its own
        constructor.

        Args:
            host: Interface to bind.
            port: Port to bind; ``0`` lets the OS assign one.
            config: Resolved configuration; a fresh
                :class:`encre.config.EncreConfig` is created when omitted.
            max_concurrent: Session manager concurrency ceiling.
            idle_timeout: Seconds an idle session survives before the
                cleanup loop reaps it.
            scheduler: An externally owned scheduler, accepted so the
                iClaw embedded client can drive automation jobs from its own
                scheduler instead of a server-local one.
        """
        self.host = host
        self.port = port
        self.config = config or EncreConfig()
        self._plugin_host = PluginHost()
        # Seed the built-in tool UI catalog so every connected client gets
        # each tool's own icon/label/card before any plugin activates.
        # Plugin activation later replaces this list with the merged
        # builtin + plugin catalog via ``set_tool_meta_entries``.
        with contextlib.suppress(Exception):  # pragma: no cover - harness catalog is optional
            self._plugin_host.set_tool_meta_entries(_collect_tool_meta(include_builtin=True))
        self._manager = SessionManager(
            max_concurrent=max_concurrent,
            idle_timeout=idle_timeout,
        )
        self._adapter_manager = GatewayRunner(
            session_manager=self._manager,
            config=self.config,
            status_callback=self._broadcast_gateway_status,
            plugin_host=self._plugin_host,
        )
        # Create embedded scheduler for automation tasks (or use provided one from iClaw)
        self._scheduler = scheduler or EncreScheduler()
        self._ws_handler = EncreWSHandler(
            self._manager,
            config=self.config,
            adapter_manager=self._adapter_manager,
            scheduler=self._scheduler,
            plugin_host=self._plugin_host,
        )
        self._plugin_host._ws_broadcaster = self._ws_handler
        self._cleanup_task: asyncio.Task[None] | None = None
        self._plugin_warmup_task: asyncio.Task[None] | None = None
        # Started at the end of plugin warmup; watches plugin directories.
        self._hot_reloader: Any | None = None
        # Daily ea-cwh central-index upgrade + market_catalog broadcast.
        self._market_refresher: Any | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        # Plugin name -> package directory mapping, populated during warmup
        # so ``/plugin-assets/<name>/<relpath>`` can serve the files an EA
        # package ships for the UI: its ``ui/`` bundle and its ``assets/``
        # images (a plugin's own icon file lives there — the app never
        # supplies an icon on a plugin's behalf).
        self._plugin_asset_dirs: dict[str, Path] = {}

    @property
    def session_manager(self) -> SessionManager:
        """The server-owned session manager (launcher-profile seam).

        Exposed because external launchers (the iClaw embedded client) need
        to route work through the same session store the network surfaces
        use, without reaching into ``_manager``.
        """
        return self._manager

    @property
    def scheduler(self) -> EncreScheduler:
        """The embedded automation scheduler (launcher-profile seam).

        Same reasoning as :attr:`session_manager`: callers that inject a
        scheduler through :func:`__init__` read it back through this seam
        rather than keeping their own reference to a possibly-defaulted
        instance.
        """
        return self._scheduler

    async def _handle_connection(self, ws) -> None:
        """Dispatch one WebSocket connection to the message handler.

        The websockets library invokes this once per accepted connection and
        keeps it running for the lifetime of that connection.  The
        exception guard is the connection-level fault boundary: an unhandled
        raise here would only close the one connection, but it would also
        escape as a task exception with no log trail, so every failure is
        logged with a traceback instead.

        Args:
            ws: The websockets connection object for one client.
        """
        try:
            await self._ws_handler.handle(ws)
        except Exception:
            logger.exception("Unhandled error in WebSocket handler")

    async def _handle_http(self, path: str, request_headers) -> tuple[int, str, dict[str, str]] | None:
        """Dispatch one admin HTTP path, including plugin contributions.

        This is the synchronous variant used outside the websockets
        ``process_request`` hook (for example by callers that want a plain
        response tuple).  Plugin assets are served before admin routes so a
        package's own frontend cannot be shadowed by an admin path.

        Args:
            path: The request target.
            request_headers: The request headers, forwarded for origin
                checking and plugin route method matching.

        Returns:
            ``(status, body, headers_dict)`` for a matched route, else
            ``None`` for the caller to translate into a 404.

        Note:
            The header list returned by the admin and plugin layers is
            normalised to a dict here; the websockets hook returns it as a
            list of tuples, so do not treat the two shapes as identical.
        """
        # Serve plugin-bundled frontend assets (ui/ folder of each EA package)
        # under /plugin-assets/<name>/<relpath>.
        if path.startswith("/plugin-assets/"):
            return self._serve_plugin_asset(path)
        result = handle_admin(path, self._manager, request_headers=request_headers)
        if result is None:
            # Plugin HTTP routes: prefix-matched against plugin-contributed
            # routes registered through PluginContext.http_routes.
            result = self._call_plugin_http(path, request_headers)
        if result is None:
            return None
        status, body, header_list = result
        headers_dict = {k: v for k, v in header_list}
        return status, body, headers_dict

    def _call_plugin_http(self, path: str, request_headers) -> tuple[int, str, list[tuple[str, str]]] | None:
        """Dispatch an admin HTTP path to a matching plugin route (best effort).

        Plugin routes are prefix-matched: a registered route
        ``{method, path: '/plugin/foo/', handler}`` matches
        ``/plugin/foo/bar``.  The handler receives
        ``(path, query, request_headers)`` and returns
        ``(status, body, headers)``.

        Matching rules: a registered method, when present, must equal the
        request method case-insensitively; an empty registered method is a
        wildcard and matches anything.  Path matching accepts an exact hit or
        a prefix hit, trying the slash-terminated form first so
        ``/plugin/foo`` does not match ``/plugin/foobar``.

        Any exception inside a plugin handler is caught and logged rather
        than raised: a broken third-party route must not be able to take the
        admin surface down with it.

        Args:
            path: The request target path.
            request_headers: Headers; the pseudo-header ``:method`` carries
                the HTTP verb, which is how websockets exposes it.

        Returns:
            The handler's ``(status, body, headers)`` tuple with the header
            list defaulted to empty, or ``None`` when no route matched.
        """
        try:
            method = (request_headers or {}).get(":method", "") or ""
            routes = self._plugin_host.http_routes
            if not routes:
                return None
            for route in routes:
                rpath = route.get("path", "")
                rmethod = (route.get("method") or "").upper()
                if rmethod and method and rmethod != method.upper():
                    continue
                if path == rpath or (rpath and path.startswith(rpath.rstrip("/") + "/")) or (rpath and path.startswith(rpath)):
                    handler = route.get("handler")
                    if handler is None:
                        continue
                    result = handler(path, request_headers)
                    if result is None:
                        continue
                    status, body, header_list = result
                    if header_list is None:
                        header_list = []
                    return status, body, header_list
        except Exception:
            logger.exception("Plugin HTTP route dispatch failed")
        return None

    def _serve_plugin_asset(self, path: str) -> tuple[int, str, dict[str, str]] | None:
        """Serve a plugin-bundled frontend file.

        ``/plugin-assets/<plugin_name>/<relpath>`` resolves inside that
        plugin's own package directory, and only inside two roots:

        * ``ui/`` — the plugin's front-end bundle, addressed relative to it
          (``index.html``, …) with whatever file types it loads;
        * the package itself, restricted to :data:`_ASSET_IMAGE_SUFFIXES` —
          so a manifest can name the icon file it ships (``icon =
          "assets/icon.svg"``) and nothing else in the package is reachable.

        Path traversal is blocked by resolving the joined path and verifying
        it stays inside the root it was asked for.

        The traversal check compares resolved absolute paths rather than
        string prefixes, because ``ui/../package.json`` and a directory
        literally named ``ui-foo`` both pass a naive prefix test.  The plugin
        name itself is not resolved through the registry here: the mapping in
        :attr:`_plugin_asset_dirs` is the allow-list, so an unknown name is a
        404 rather than a chance to name an arbitrary directory.  Restricting
        the package root to picture extensions is what stops that same route
        from reading a package's source or data files.

        SVG is served as ``image/svg+xml`` and is only ever referenced from
        an ``<img>`` element: a script inside an SVG loaded that way does not
        run (the document is isolated), which is not true of inlined markup.

        Args:
            path: The full request path.

        Returns:
            ``(200, bytes, headers)`` on success; a status tuple for
            traversal (403), missing file (404) or read failure (500);
            ``None`` when the path is not shaped like an asset request or
            the plugin is unknown, letting the caller fall through to 404.
        """
        try:
            _, _, name, relpath = path.split("/", 3)
        except ValueError:
            return None
        pkg_dir = self._plugin_asset_dirs.get(name)
        if pkg_dir is None or not relpath:
            return None
        ui_root = pkg_dir / "ui"
        roots = [ui_root]
        if Path(relpath).suffix.lower() in self._ASSET_IMAGE_SUFFIXES:
            roots.append(pkg_dir)
        for root in roots:
            if not root.is_dir():
                continue
            resolved_root = root.resolve()
            target = (resolved_root / relpath).resolve()
            try:
                target.relative_to(resolved_root)
            except ValueError:
                return 403, "Forbidden", {"Content-Type": "text/plain"}
            if not target.is_file():
                continue
            try:
                payload = target.read_bytes()
            except OSError:
                return 500, "Read Failed", {"Content-Type": "text/plain"}
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            return 200, payload, {"Content-Type": content_type}
        return 404, "Not Found", {"Content-Type": "text/plain"}

    def _safe_broadcast_automation_update(self, job: Any) -> None:
        """Wrapper around broadcast_automation_update that logs errors.

        Called by the scheduler's on_complete callback.  The scheduler calls
        this synchronously from _execute_job (inside the event loop thread).
        The broadcast is fire-and-forget, so an exception raised deep inside
        a renderer write would otherwise propagate into the scheduler's job
        completion path and look like a job failure.

        Args:
            job: The completed automation job record, serialised by the
                handler into the protocol frame.
        """
        try:
            self._ws_handler.broadcast_automation_update(job)
        except Exception:
            logger.exception("[automation] broadcast_automation_update failed")

    async def start(self) -> None:
        """Bind the port and bring every subsystem up.

        Ordering is load-bearing and should be read top to bottom:

        1. Import websockets and fail fast, before anything is allocated.
        2. Install ``process_request`` and start listening.  This is the
           readiness boundary: from this moment the desktop main process can
           connect, so everything afterwards must tolerate a client that
           arrives early.
        3. Resolve the OS-assigned port when ``port == 0`` and emit the
           ready lines.  The ``print`` is deliberate: the desktop main
           process parses stdout, not the log file, to learn the port.
        4. Start subsystems in the order a request would touch them, each in
           its own try/except so one failing component degrades the server
           instead of preventing it from serving.
        """
        logger.info(f"Starting encre server on {self.host}:{self.port}")

        async def process_request(path, request_headers):
            """Decide, per inbound request, whether to accept or answer it inline.

            The ``process_request`` hook passed to ``websockets.serve``: it runs
            before the WebSocket handshake and is the single place where
            cross-transport concerns are resolved -- which hosts may connect,
            which paths are WS upgrades, which paths serve plugin assets, and
            which paths fall through to the HTTP admin API.

            Args:
                path: The request path, or an empty value for the WS endpoint.
                request_headers: The raw request headers, used for the local
                    origin check and forwarded to the admin handler.

            Returns:
                ``None`` to let ``websockets`` continue the handshake, or an
                ``(status, headers, body)`` tuple answering the request inline.

            Note:
                The origin check runs before anything else and rejects foreign
                origins outright; the desktop renderer connects from
                ``file://`` or custom schemes, never from a page on another
                host. Admin calls are dispatched through
                ``asyncio.to_thread`` because several of them shell out to git,
                and doing that inline would freeze every live agent session.
            """
            # Block cross-site browser handshakes (drive-by WebSocket
            # hijacking): the desktop renderer connects over file:///custom
            # schemes or Node, never from an http(s) page on another host.
            from encre.server.admin import _origin_is_local

            if not _origin_is_local(request_headers):
                return (403, [("Content-Type", "text/plain")], b"Forbidden")
            if path and path.startswith("/ws"):
                return None  # Let websockets handle it
            # Serve plugin-bundled frontend assets before the admin routes.
            if path.startswith("/plugin-assets/"):
                result = self._serve_plugin_asset(path)
                if result is None:
                    return (404, [], b"Not Found")
                status, body, headers_dict = result
                if isinstance(body, str):
                    body = body.encode()
                header_list = list(headers_dict.items())
                return (status, header_list, body)
            # Handle HTTP admin endpoints.  Some of them (git status/diff/
            # push/pull/...) shell out to git with timeouts of up to 60s;
            # running that inline would freeze the whole event loop (and
            # every live agent session), so it goes to a worker thread.
            result = await asyncio.to_thread(handle_admin, path, self._manager, request_headers)
            if result is None:
                # Plugin HTTP route fallback (prefix-matched).
                try:
                    result = await asyncio.to_thread(self._call_plugin_http, path, request_headers)
                except Exception:
                    result = None
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

        # Once warmup finishes, fire plugin startup lifecycle hooks so
        # plugins can bind resources / start background loops.
        async def _fire_plugin_startup_after_warmup() -> None:
            """Fire the plugin startup lifecycle hook once warmup completes.

            Sequencing matters: plugins are imported during registry warmup, and
            their startup hook must run only after those imports have finished,
            otherwise a plugin would start a background loop referencing code
            that is still being loaded. Awaiting the warmup task here is what
            enforces that order without blocking :meth:`start`.

            Note:
                Both awaits are wrapped in ``contextlib.suppress`` because this
                is a fire-and-forget background task: a plugin that fails to
                import or that raises in its startup hook must not take down
                server startup, only log a degraded plugin set.
            """
            with contextlib.suppress(Exception):
                await self._plugin_warmup_task
            with contextlib.suppress(Exception):
                await self._plugin_host.fire_lifecycle("startup")

        _lt = asyncio.ensure_future(_fire_plugin_startup_after_warmup())
        self._background_tasks.add(_lt)

        # Start gateway runner (adapter lifecycle manager)
        try:
            await self._adapter_manager.start()
        except Exception as e:
            logger.error("Failed to start gateway runner: %s", e)
            logger.warning("Gateway not available -- QQ, Telegram, etc. will not connect")

        # Sync adapter configs (from saved settings) into EncreConfig so the frontend
        # sees adapter_* keys in config_data immediately -- not "not configured".
        # Only the actual adapter connection (HTTP/WS) is deferred to background.
        saved_config: dict[str, Any] = {}
        try:
            from encre.settings_manager import load_settings

            saved_config = load_settings()
            if saved_config:
                adapter_cfgs: dict[str, dict[str, Any]] = {}
                for k, v in saved_config.items():
                    if k.startswith("adapter_"):
                        # Settings are stored flat as adapter_<name>_<field>;
                        # split on the first two underscores so field names
                        # containing underscores survive intact.
                        parts = k.split("_", 2)
                        if len(parts) >= 3:
                            adapter_cfgs.setdefault(parts[1], {})[parts[2]] = v
                if adapter_cfgs:
                    self.config.adapter_configs.update(adapter_cfgs)
                    logger.info("Loaded adapter configs from saved settings: %s", list(adapter_cfgs.keys()))
        except Exception as e:
            logger.warning("Failed to load adapter configs from settings: %s", e)

        # Network proxy must be live before any adapter or backend opens a
        # socket: every HTTP stack in the process reads the standard proxy
        # env vars that apply_network_proxy mirrors from the persisted
        # settings (General -> Network).
        try:
            from encre.proxy_env import apply_network_proxy

            apply_network_proxy(saved_config)
        except Exception as e:
            logger.warning("Failed to apply network proxy settings: %s", e)

        # Start adapter connections in background (network I/O may be slow)
        _t = asyncio.ensure_future(self._start_adapters())
        self._background_tasks.add(_t)

        # Start the embedded scheduler for automation tasks
        try:
            await self._scheduler.start(self._make_scheduler_agent_factory())
            # Register callback so frontend gets notified when jobs complete
            self._scheduler.on_job_complete(lambda job: self._safe_broadcast_automation_update(job))
            self._scheduler.on_job_progress(
                lambda job, event_type, event_data: self._ws_handler.broadcast_automation_progress(job, event_type, event_data)
            )
            logger.info("Automation scheduler started (poll_interval=30s)")
        except Exception as e:
            logger.warning("Failed to start automation scheduler: %s", e)

        # Start the commitment watcher on top of the scheduler: due
        # commitments in the memory graph are converted to wake intents that
        # reuse the automation pipeline (execution, history, gateway push).
        try:
            from encre.memdir.layers import MemoryLayerStore
            from encre.scheduler import CommitmentWatcher

            def _commitment_push_gateways() -> list[str]:
                """Connected adapter names at dispatch time (never stale)."""
                try:
                    status = self._adapter_manager.get_status()
                except Exception:
                    return []
                return [str(adapter.get("name")) for adapter in (status.get("adapters") or []) if adapter.get("connected")]

            self._commitment_watcher = CommitmentWatcher(
                store=MemoryLayerStore(),
                scheduler=self._scheduler,
                push_gateways=_commitment_push_gateways,
            )
            await self._commitment_watcher.start()
            logger.info("Commitment watcher started (check_interval=60s)")
        except Exception as e:
            logger.warning("Failed to start commitment watcher: %s", e)

        # Daily central-repository index refresh: upgrades the ea-cwh
        # package and broadcasts a market_catalog frame when the index
        # fingerprint changes.  First paint does not wait for it — every
        # connection receives the locally readable catalog on connect.
        try:
            from encre.market_catalog import MarketCatalogRefresher

            async def _broadcast_market_catalog(payload: dict[str, Any]) -> None:
                await self._ws_handler.broadcast_event("market_catalog", payload)

            self._market_refresher = MarketCatalogRefresher(on_update=_broadcast_market_catalog)
            await self._market_refresher.start()
            logger.info("Market catalog refresher started (interval=24h)")
        except Exception as e:
            logger.warning("Failed to start market catalog refresher: %s", e)

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
        return any(getattr(m, "enabled", True) and m.model_id == mid and (m.base_url or "").rstrip("/") == burl for m in models)

    def _make_scheduler_agent_factory(self):
        """Create an agent factory for automation jobs.

        Same pattern as _run_sub_agent: fresh agent with bypass permissions,
        builder-generated system prompt, no custom overrides. The snapshot
        model config is only applied when the model is still enabled; a job
        pinned to a disabled/deleted model runs with the active model instead.

        Args:
            None; the returned callable accepts a job config snapshot.

        Returns:
            A zero- or one-argument factory returning a configured
            :class:`encre.agent.EncreAgent` ready to execute a job.

        Note:
            ``defer_plugins=True`` matches interactive sessions: the agent
            attaches the raw registry and loads plugins on demand, so a job
            that never uses a plugin does not pay for importing every EA
            package.
        """
        from encre.agent import EncreAgent

        def _factory(job_config: dict[str, Any] | None = None) -> EncreAgent:
            """Build one automation agent, optionally pinned to a job's model.

            Every automation job gets a fresh agent in
            :class:`AgentMode.AUTOMATION` with permissions bypassed, since a
            scheduled job has no human at the terminal to approve prompts. A
            job config snapshot may override the model; the override is applied
            only when that model is still enabled, and the backend is rebuilt
            whenever any override was applied.

            Args:
                job_config: Optional config snapshot stored with the job.
                    Recognised keys are ``backend_type``, ``api_key``,
                    ``base_url``, ``model_id``, and ``max_tokens``; anything
                    else is ignored.

            Returns:
                A configured :class:`encre.agent.EncreAgent` ready to run the
                job, sharing the server's ``self.config`` as its base.

            Note:
                When the pinned model is disabled or no longer available the
                agent falls back to the server's active model and a warning is
                logged, so a stale job definition degrades to a run with a
                substitute model rather than failing.
            """
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
                        "[automation] job model_id=%s disabled/unavailable; running job with the active model instead",
                        job_config.get("model_id"),
                    )
                agent.rebuild_backend()
            return agent

        return _factory

    async def _broadcast_gateway_status(self, status: dict) -> None:
        """Called by GatewayRunner when adapter status changes.

        This is the callback wired in :func:`__init__`, so the gateway
        manager owns no reference to the WebSocket handler and cannot be
        constructed before it.  Keeping the hop here means adapters stay
        purely IM-facing: they report state, the server decides how it is
        shown.

        Args:
            status: Adapter status payload, forwarded to every connected
                client unchanged.
        """
        self._ws_handler.broadcast_gateway_status(status)

    async def _cleanup_loop(self) -> None:
        """Periodically reap sessions that have sat idle past their timeout.

        Runs every 300 seconds for the whole server lifetime.  Reaping is
        the only thing that bounds memory and the size of ``data/sessions/``
        on a long-running daemon: the session manager marks state but does
        not evict on its own.  The task is created in :func:`start` and
        cancelled in :func:`stop`, so a server that never stops leaks one
        task rather than crashing.
        """
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

        The shared registry is pre-attached to ``self._plugin_host`` so every
        auto-activated plugin registers its ws_handlers / http_routes /
        lifecycle hooks / gateway adapters into the server's host.

        After warmup the plugin hot-reloader starts watching the plugin
        directories: a changed plugin is re-discovered/re-activated in the
        background and every connected client receives a ``plugin_changed``
        frame followed by a fresh ``plugin_ui_sync``.

        Every step is individually guarded because plugin code is
        third-party: a failing package must not leave the server without its
        plugin surface, and a failing warmup must not prevent the server
        from serving agent requests at all.
        """
        try:
            from encre.agent import (
                _get_shared_plugin_registry,
                _get_shared_plugin_registry_raw,
                _get_shared_skill_registry,
            )
            from encre.plugins.hot_reload import PluginHotReloader

            # Attach the server's host before discovery so auto-activation
            # side effects are captured into it (idempotent).
            raw_registry = _get_shared_plugin_registry_raw()
            raw_registry.attach_host(self._plugin_host)

            # Phase A -- skills first.  The first ``config_data`` snapshot
            # carries ``available_skills``, so priming the (disk-bound, ~2s)
            # skill registry immediately means the boot snapshot never has
            # to build it inline, and never lists a half-loaded catalogue.
            await asyncio.to_thread(_get_shared_skill_registry)

            # Phase B -- the heavy plugin discovery imports hundreds of EA
            # packages (tens of seconds cold, GIL-bound).  Running it
            # concurrently with the first client's boot snapshot starved
            # every snapshot build under the GIL and stretched the
            # first-frame-to-filled-UI window from ~2s to ~90s on cold
            # starts.  Wait until that snapshot has landed (the ws handler
            # sets the event after its initial unified push), with a timeout
            # so a headless server with no client ever connecting still
            # warms up.  A client connecting mid-discovery is covered: its
            # per-connection plugin pushes were suppressed during warmup and
            # the end-of-warmup broadcasts below deliver the full surface.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._ws_handler._boot_push_done.wait(), timeout=10.0)
            # Give the client's first interactive frames a clear CPU window:
            # discovery imports hundreds of packages and holds the GIL, and
            # running it the instant the boot push lands is what made the
            # first couple of seconds occasionally stutter.
            await asyncio.sleep(1.5)
            registry = await asyncio.to_thread(_get_shared_plugin_registry)
            self._refresh_plugin_asset_dirs(registry)
            # Prime the shared skill registry as well (bundled + builtin tool +
            # EA package + user skills).  Sessions are built with
            # ``defer_plugins=True``, so each agent attaches the *raw* (empty)
            # registry; if warmup primed plugins only, the first ``config_data``
            # snapshot would carry an empty ``available_skills`` list and the
            # settings panel would show no skills until a run (which awaits
            # ``ensure_plugins_loaded``) or a skill install/update happened.
            await asyncio.to_thread(_get_shared_skill_registry)
            if registry.host is self._plugin_host:
                logger.info(
                    f"Plugin registry warmed up ({len(registry._plugins)} plugins, "
                    f"{len(self._plugin_host.ws_handlers)} ws_handlers, "
                    f"{len(self._plugin_host.http_routes)} http_routes, "
                    f"{list(self._plugin_host.lifecycle_hooks.keys())} lifecycle)"
                )
            # Dynamic authorisation: notify every connected client about
            # third-party plugins awaiting approval so the renderer pops
            # the confirmation dialog (their code is not imported until
            # the user approves).
            with contextlib.suppress(Exception):
                await self._ws_handler.broadcast_plugin_confirm_request()
            # Hot reload: watch plugin dirs; on change the reloader tears the
            # affected plugin down via the registry's public API, re-discovers
            # and re-activates it, then this callback notifies every client.
            self._hot_reloader = PluginHotReloader(raw_registry, on_reload=self._on_plugins_hot_reloaded)
            await self._hot_reloader.start()
            # The per-connection plugin_ui_sync (ws connect handler) races
            # this warmup: a client that connected while discovery was still
            # running already received the builtin-only tool catalog (seeded
            # on the plugin host at construction) but no plugin-sourced
            # entries.  Re-broadcast the full snapshot now that every plugin
            # is activated; the renderer's catalog signature diff makes the
            # overlap a no-op.
            with contextlib.suppress(Exception):
                await self._ws_handler.broadcast_plugin_changed(registry.snapshot_states())
                await self._ws_handler.broadcast_plugin_ui_sync()
            # Clients that connected while warmup ran skipped the
            # per-connection market index push (same gate as the plugin
            # frames above): broadcast the central-repository catalog now
            # so the market view fills in on first paint instead of
            # waiting for the refresher's next cycle.
            with contextlib.suppress(Exception):
                from encre.market_catalog import load_catalog

                await self._ws_handler.broadcast_event("market_catalog", await asyncio.to_thread(load_catalog))
            # Plugins that contributed gateway connector specs are now
            # activated; bridge them into the platform registry so the
            # adapter panel shows them without waiting for a status poll.
            with contextlib.suppress(Exception):
                self._adapter_manager.sync_plugin_adapters()
        except Exception as e:
            logger.warning(f"Plugin warmup failed: {e}")
        finally:
            # Mark warmup as attempted (successfully or not) so per-connection
            # plugin pushes resume for later connections.  Set after the
            # end-of-warmup broadcasts: the per-connection plugin_ui_sync now
            # goes out unconditionally (builtin-only during warmup), so
            # clients that connected before this point receive the merged
            # plugin set only through the broadcast above.
            self._ws_handler._plugin_warmup_complete = True

    def _refresh_plugin_asset_dirs(self, registry: Any) -> None:
        """Rebuild the plugin name -> package directory map.

        Used both at warmup and after a hot reload so
        ``/plugin-assets/<name>/<relpath>`` keeps serving the (possibly
        relocated) bundled files of every active plugin.

        Every activated plugin with a package on disk is mapped, not only the
        ones that happen to carry a ``ui/`` directory: a plugin whose only
        front-end contribution is its own ``assets/icon.svg`` must be able to
        serve it too.  :meth:`_serve_plugin_asset` is where the roots and the
        file types actually offered are decided.

        The map is rebuilt wholesale rather than diffed: entries can vanish
        when a package is reinstalled to a different location, and a stale
        entry would keep serving files that no longer belong to an active
        plugin.

        Args:
            registry: The shared plugin registry, whose ``_plugins`` mapping
                holds the activated package manifests.
        """
        asset_dirs: dict[str, Path] = {}
        for plugin_name, plugin in registry._plugins.items():
            try:
                pkg_dir_s = getattr(getattr(plugin, "manifest", None), "pkg_dir", "") or ""
                if not pkg_dir_s:
                    continue
                pkg_dir = Path(pkg_dir_s)
                if pkg_dir.is_dir():
                    asset_dirs[plugin_name] = pkg_dir
            except Exception:
                continue
        self._plugin_asset_dirs = asset_dirs

    async def _on_plugins_hot_reloaded(self, names: list[str]) -> None:
        """Hot-reload callback: refresh plugin surfaces and notify clients.

        Runs on the event loop after the reloader has re-activated the
        plugins in *names*.  Refreshes the tool UI metadata catalog and the
        plugin assets map, then broadcasts ``plugin_changed`` (lifecycle
        snapshot) followed by ``plugin_ui_sync`` (fresh providers + tool
        metadata) so every connected renderer re-mounts plugin UI.

        The two broadcasts are ordered and both are required: the renderer
        treats ``plugin_changed`` as "the plugin set changed" and needs the
        ui sync to know what to actually render.  Sending only one leaves a
        client showing stale widgets.

        Args:
            names: Plugin package names that were re-activated, used for
                the log line only.
        """
        try:
            from encre.agent import _get_shared_plugin_registry_raw

            registry = _get_shared_plugin_registry_raw()
            self._refresh_plugin_asset_dirs(registry)
            self._plugin_host.set_tool_meta_entries(_collect_tool_meta(include_builtin=True))
            # Re-bridge connector specs: reactivated plugins re-register their
            # adapters, and plugins removed by the reload must be torn down.
            with contextlib.suppress(Exception):
                self._adapter_manager.sync_plugin_adapters()
            await self._ws_handler.broadcast_plugin_changed(registry.snapshot_states())
            await self._ws_handler.broadcast_plugin_ui_sync()
            logger.info("Notified clients about reloaded plugins: %s", ", ".join(names))
        except Exception as e:
            logger.warning(f"Plugin hot-reload notification failed: {e}")

    async def serve_forever(self) -> None:
        """Start every subsystem and block until the loop is cancelled.

        The bare pending future is the run-forever idiom: it creates nothing
        of its own and simply keeps the loop alive so signals and
        ``KeyboardInterrupt`` from the outside can unwind it.  The ``finally``
        makes :func:`stop` run for every exit path -- cancellation, an
        unhandled exception during :func:`start`, or an interrupt -- so a
        failing startup does not leave sessions, adapters or the socket
        open.
        """
        await self.start()
        try:
            await asyncio.Future()  # Run forever
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Tear down every subsystem in the reverse of startup order.

        Order is load-bearing.  Plugins are stopped first because they may
        hold references to adapters and sessions; the index manager next,
        since it is synchronous and independent; then the adapters and the
        scheduler, whose workers must not outlive the session store that
        records their results.  Background tasks are cancelled only after
        the services they drive are stopped, and the WebSocket server is
        closed before the session manager shuts down so no new connection
        can be accepted into a store that is already gone.

        Each step is individually guarded: :func:`stop` runs in a
        ``finally`` block, and an exception here would replace the original
        exit reason in the traceback.

        Raises:
            Exception: Only from :func:`SessionManager.shutdown`; every
                other step swallows and logs its own error.
        """
        # Fire plugin shutdown hooks first so resources are released before
        # the manager/adapters that depend on them get torn down.
        with contextlib.suppress(Exception):
            await self._plugin_host.fire_lifecycle("shutdown", self._manager)
        try:
            await self._adapter_manager.stop()
        except Exception as e:
            logger.warning("Error stopping adapters: %s", e)
        try:
            await self._scheduler.stop()
            logger.info("Automation scheduler stopped")
        except Exception as e:
            logger.warning("Error stopping scheduler: %s", e)
        watcher = getattr(self, "_commitment_watcher", None)
        if watcher is not None:
            try:
                await watcher.stop()
                logger.info("Commitment watcher stopped")
            except Exception as e:
                logger.warning("Error stopping commitment watcher: %s", e)
        if self._market_refresher is not None:
            try:
                await self._market_refresher.stop()
                logger.info("Market catalog refresher stopped")
            except Exception as e:
                logger.warning("Error stopping market catalog refresher: %s", e)
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
        if self._plugin_warmup_task:
            self._plugin_warmup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._plugin_warmup_task
        if hasattr(self, "_ws_server") and self._ws_server:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        await self._manager.shutdown()
        # The search index runtime is a process-wide singleton rather than a
        # per-server object, so it is closed here on the way out.
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
    """Convenience function to run the server synchronously.

    Owns configuration resolution and the ``asyncio.run`` boundary.  Called
    directly by the desktop main process and by
    :func:`encre.server.service.run_service`; both get an identical
    server regardless of who launched it.

    Args:
        host: Interface to bind.
        port: Port to bind; ``0`` for OS assignment.
        config: Pre-resolved configuration; takes precedence over
            ``config_file`` so callers that already loaded and merged config
            do not have it reloaded from disk.
        max_concurrent: Session concurrency ceiling.
        config_file: Explicit config path; only used when ``config`` is
            ``None``, in which case auto-discovery still applies.
        scheduler: Optional externally owned scheduler, forwarded to
            :class:`EncreServer`.

    Raises:
        OSError: From config or keyfile I/O.
        KeyboardInterrupt: Swallowed after logging, which is what lets
            :func:`serve_forever`'s ``finally`` complete a clean shutdown.

    Note:
        :func:`ensure_keyfile` runs before anything else: every other
        component encrypts state with this key, so building the session
        manager before the keyfile exists would create a store that cannot
        be read back.
    """
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
        # Silence watchfiles' per-second idle "rust notify timeout" DEBUG noise.
        logging.getLogger("watchfiles").setLevel(logging.INFO)

        config = None
        if args.config:
            config = EncreConfig.from_file(args.config)

        run_server(
            host=args.host,
            port=args.port,
            config=config,
            max_concurrent=args.max_concurrent,
        )
