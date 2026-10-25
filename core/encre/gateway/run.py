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

"""Gateway runner: adapter lifecycle manager and inbound message router.

This module is the Python gateway layer of Encre. It owns the lifecycle of
every IM platform adapter registered in
:data:`encre.gateway.platform_registry.platform_registry` and is the single
entry point that turns an inbound platform message into a run of the agent
core. Inbound traffic arrives here as adapter callbacks; the same agent core
that desktop and headless clients drive over WebSocket is reached through
:class:`encre.protocol.session_router.EventRouter`.

Inbound message lifecycle for one platform user message:

1. An adapter invokes the callback wired via
   ``BasePlatformAdapter.set_message_handler``, which is
   :meth:`GatewayRunner._handle_message`.
2. Source resolution. Adapters that could not attach a
   :class:`encre.gateway.session.SessionSource` get one synthesised from the
   raw ``message_id`` so that no downstream step ever sees a ``None`` source.
3. Authorization. :class:`encre.gateway.authz.AuthorizationChecker` is
   consulted, backed by :class:`encre.gateway.pairing.PairingStore` for
   ``/pair`` codes, unless the adapter reports that it authorizes upstream.
4. Session routing. :func:`encre.gateway.session.build_session_key` derives a
   stable key from the resolved source.
5. Agent execution. :meth:`_handle_message_with_agent` resolves the session id,
   streams agent events through
   :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`, and finalises
   delivery back through the adapter.

Adapter lifecycle is managed by :meth:`start_adapter`, :meth:`stop_adapter`,
and :meth:`apply_config`. Fatal transport errors surface through
:meth:`_handle_adapter_fatal_error`; outbound delivery policy is delegated to
:class:`encre.gateway.delivery.DeliveryRouter`.

Public API (consumed by the desktop frontend and the WS bridge):

- :meth:`GatewayRunner.start` / :meth:`GatewayRunner.stop`
- :meth:`GatewayRunner.start_adapter` / :meth:`GatewayRunner.stop_adapter`
- :meth:`GatewayRunner.apply_config`
- :meth:`GatewayRunner.get_status`
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from encre.gateway.authz import AuthorizationChecker
from encre.gateway.channel_directory import ChannelDirectory
from encre.gateway.commands import apply_session_mode, build_command_registry, register_command_hook
from encre.gateway.config import GatewayConfig, Platform, PlatformConfig, load_gateway_config
from encre.gateway.delivery import DeliveryRouter
from encre.gateway.hooks import GATEWAY_STARTUP, get_hook_registry
from encre.gateway.pairing import PairingStore
from encre.gateway.platform_registry import PlatformEntry, platform_registry
from encre.gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from encre.gateway.response_filters import is_intentional_silence_response
from encre.gateway.session import SessionSource, SessionStore, build_session_key
from encre.gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from encre.utils.types import Finish, TextDelta, ToolResult

logger = logging.getLogger("encre.gateway.run")


def _field_key(adapter_id: str, field: str) -> str:
    """Compose the flat config key that represents one adapter field.

    The frontend and the gateway config store address adapter settings as a
    single flat namespace rather than a nested tree, so every field must map
    onto a unique dotted-free key. The ``adapter_`` prefix is also what
    :func:`_extract_adapter_config` matches on to invert the mapping.

    Args:
        adapter_id: Registry name of the adapter, e.g. ``"telegram"``.
        field: Raw field name, e.g. ``"token"``.

    Returns:
        The flat key, e.g. ``"adapter_telegram_token"``.
    """
    return f"adapter_{adapter_id}_{field}"


def _extract_adapter_config(flat_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Invert the flat config namespace into per-adapter field dicts.

    ``apply_config`` receives the frontend's flat config. Only keys carrying
    the ``adapter_`` prefix belong to a platform; the second underscore-
    separated segment is the adapter id and everything after it is the field
    name. Splitting with ``maxsplit=2`` keeps fields that themselves contain
    underscores intact.

    Keys that cannot be parsed (shorter than two segments) are dropped rather
    than raising, because the flat config also carries global settings that
    are not adapter-scoped.

    Args:
        flat_config: The flat config mapping from the frontend.

    Returns:
        ``{adapter_id: {field: value}}`` for every recognised adapter key.
    """
    result: dict[str, dict[str, Any]] = {}
    for key, value in flat_config.items():
        if key.startswith("adapter_"):
            parts = key.split("_", 2)
            if len(parts) >= 3:
                aid = parts[1]
                fk = parts[2]
                result.setdefault(aid, {})[fk] = value
    return result


def _export_adapter_env(name: str, config: dict[str, Any]) -> None:
    """Mirror one adapter's config fields into ``os.environ`` for vendor SDKs.

    Field names are not globally unique across adapters -- ``token``,
    ``app_id``, ``secret``, ``server_url`` and ``webhook_url`` are each shared
    by several platforms -- so a bare export lets whichever adapter starts
    *last* overwrite every earlier adapter's values.  On Windows
    ``os.environ`` is case-insensitive, which makes it worse: a bare
    ``token`` shadows ``TOKEN`` for every other SDK in the process.  Generic
    (lower-case) field names are therefore namespaced per adapter, while
    upper-snake keys are already vendor-namespaced (``WEIXIN_TOKEN``,
    ``QQ_APP_ID``, ``WECOM_BOT_ID``, ...) and pass through untouched.

    Args:
        name: Registry name of the adapter that owns *config*.
        config: Flat adapter config; ``enabled`` is skipped because it is
            runner state, not a vendor setting.
    """
    import os

    for k, v in config.items():
        if k in ("enabled",):
            continue
        key = str(k)
        env_key = key if key.isupper() else f"ENCRE_ADAPTER_{name}_{key}"
        os.environ[env_key.upper()] = str(v)


class GatewayRunner:
    """Owns and drives every IM platform adapter for the lifetime of the gateway.

    In-process adapters are constructed from the platform registry and talk to
    the agent core through direct async callbacks; remote or plugin adapters
    connect over WebSocket through the optional bridge started by
    :meth:`start_ws_bridge` and are wrapped as remote adapter instances. The
    same runner instance is reachable from the desktop frontend (via
    ``GatewayRunner.get_status`` and :meth:`_notify_status`) and from the WS
    bridge (via :meth:`resolve_session`), so it doubles as the status source
    for the adapter panel in the desktop UI.

    Invariants:

    - ``_instances`` holds only adapters whose ``connect()`` succeeded;
      anything else is recorded in ``_last_errors`` so :meth:`get_status` can
      still report it.
    - ``_stored_configs`` is the only place adapter secrets persist between
      stop/start cycles, so an adapter can be re-enabled without the frontend
      re-sending its tokens.
    - Every adapter registered in ``_instances`` is mirrored into
      ``router._connected_adapters``, which is what the agent's iClaw prompt
      reports as "active IM platforms".
    - ``_session_store`` is closed only by :meth:`stop`.

    Lifecycle: construct (injected session manager + config) -> :meth:`start`
    (config load, platform discovery, hook load) -> :meth:`start_adapter` per
    enabled platform -> :meth:`stop`. The class is bound to a single running
    event loop; all public methods are coroutines except the pure read-only
    accessors and ``get_status``.

    Attributes:
        router: The :class:`encre.protocol.session_router.EventRouter`, or
            ``None`` when the runner was constructed without a session manager
            (e.g. in unit tests). All message routing fails closed without it.
        delivery: The :class:`encre.gateway.delivery.DeliveryRouter`, created
            lazily on first access.
        instances: Live adapter registry, keyed by registry name.
        running: Whether :meth:`start` has completed; guarded re-entry for
            :meth:`start` and :meth:`stop`.
        status_callback: Optional sync-or-async callable invoked with the
            :meth:`get_status` payload whenever an adapter changes state.

    Args:
        session_manager: Session manager used to construct the event router.
            ``None`` disables routing entirely.
        config: Default agent config handed to the event router.
        status_callback: Invoked after every adapter state transition.
    """

    def __init__(
        self,
        session_manager=None,
        config=None,
        status_callback: Callable[[dict[str, Any]], None] | None = None,
        plugin_host: Any = None,
    ) -> None:
        """Wire the runner's collaborators and pre-load persisted routing state.

        Everything an adapter will need during its whole lifecycle is bound
        here, ahead of :meth:`start`, because adapters are constructed one at a
        time by :meth:`start_adapter` and must be handed ready-made objects
        rather than making them construct their own: the routing
        :class:`encre.gateway.session.SessionStore`, the shared
        :class:`encre.gateway.pairing.PairingStore` behind the
        :class:`encre.gateway.authz.AuthorizationChecker`, the channel
        directory cache, and the process-wide :class:`encre.gateway.hooks.HookRegistry`.
        The event router is built eagerly -- and only when both ``session_manager``
        and ``config`` are present -- because it is the single hop from an
        inbound platform message to the Rust agent core, and a runner without
        one has no message path at all, so routing then fails closed everywhere.
        Persisted adapter-session bindings and the loaded hook registry are
        read from disk during construction so that a restart resumes each
        platform chat in its own history instead of starting cold.

        Invariant: ``_router`` is ``None`` only for an unconfigured (test)
        runner, ``_running`` is ``False`` until :meth:`start` finishes, and
        every registry dict starts empty so :meth:`get_status` reports no
        connected platform and no stale error for a fresh process. The
        ``DeliveryRouter`` is deliberately deferred to its first access because
        it needs a reference back to this still-being-built runner.

        Args:
            session_manager: Session manager used to build the event router.
                ``None`` disables routing.
            config: Default agent config handed to the event router.
            status_callback: Invoked after every adapter state transition.
            plugin_host: The embedding application's
                :class:`encre.plugins.host.PluginHost`.  When present, adapter
                specs registered by plugins (``context.gateway_adapters``) are
                bridged into the platform registry; ``None`` keeps the runner
                built-in-only (CLI/test runners without a plugin system).
        """
        # The router is the only path from a platform message to the agent core,
        # so it is wired up eagerly while its collaborators are still in scope.
        self._router = None
        if session_manager and config:
            from encre.protocol.session_router import EventRouter
            self._router = EventRouter(session_manager, config)
        self._config = config

        # Maps a platform chat/conversation to a single agent session id so that
        # a Telegram topic or Discord thread resumes its own history.
        self._session_store = SessionStore()
        # The router's busy-backlog shares the durable pending-message queue
        # with the adapters, so a protocol submit that races a running turn
        # survives a restart too.
        if self._router is not None:
            self._router.set_pending_store(self._session_store)

        # Authorization combines a static allow list with one-time /pair codes;
        # the PairingStore is the shared source of truth for both.
        self._pairing = PairingStore()
        self._authz = AuthorizationChecker(pairing=self._pairing)

        # Created on demand because DeliveryRouter needs a live runner reference.
        self._delivery: DeliveryRouter | None = None

        # Cached channel metadata; reloaded from disk on start() so cached
        # channel ids survive a gateway restart.
        self._channel_dir = ChannelDirectory()

        # name -> connected adapter instance. Membership here is what the UI
        # reads as "connected".
        self._instances: dict[str, BasePlatformAdapter] = {}

        # name -> last connect/start failure. Kept separately from _instances so
        # the adapter panel can show a platform that is off *and* broken.
        self._last_errors: dict[str, str] = {}

        # Secrets never round-trip through the frontend twice: apply_config()
        # merges into this map and restarts always read from it.
        self._stored_configs: dict[str, dict[str, Any]] = {}

        # Plugin hooks (e.g. session lifecycle, agent start/end) share one
        # registry across the process; the gateway is one of their emitters.
        self._hooks = get_hook_registry()

        # Adapter slash-command actions (/new, /model, ...).  The registry is
        # extensible: new commands are one CommandAction registration away.
        # The hook itself is registered in start(), *after* discover_and_load()
        # -- discovery clears the handler table, so anything registered here
        # would be wiped before the first message arrives.
        self._commands = build_command_registry()

        # Push channel: adapters are polled on demand, the UI subscribes here.
        self._status_callback = status_callback

        # Runtime state
        self._running = False
        self._gateway_config: GatewayConfig | None = None

        # Plugin connector wiring (S8): the host accumulates adapter specs
        # registered via ``context.gateway_adapters.register()``; this map
        # records which registry names this runner already bridged, so
        # sync_plugin_adapters() is idempotent and can also detect specs that
        # disappeared (plugin disabled/uninstalled -> stop + unregister).
        self._plugin_host = plugin_host
        self._plugin_adapter_specs: dict[str, str] = {}
        self._pending_plugin_stops: set[str] = set()
        self._plugin_adapter_tasks: set[asyncio.Task[None]] = set()

        # WS bridge (optional, for remote adapters)
        self._ws_bridge = None

    # -- Properties --

    @property
    def router(self):
        """Return the event router used to reach the agent core.

        ``None`` when the runner was built without a session manager. Callers
        that submit prompts must check before use;
        :meth:`_handle_message_with_agent` turns a missing router into a
        visible gateway error instead of a silent drop.

        Returns:
            The :class:`encre.protocol.session_router.EventRouter`, or
            ``None``.
        """
        return self._router

    @property
    def session_store(self) -> SessionStore:
        """Return the shared session routing store.

        Exposed for command handlers (:mod:`encre.gateway.commands`) that need
        to reset a conversation or read/write its preferences without touching
        runner internals.

        Returns:
            The process-wide :class:`encre.gateway.session.SessionStore`.
        """
        return self._session_store

    def get_adapter(self, name: str) -> BasePlatformAdapter | None:
        """Return a connected adapter instance by registry name.

        Args:
            name: Registry name of the adapter, e.g. ``"telegram"``.

        Returns:
            The live adapter instance, or ``None`` when no adapter with that
            name is currently connected.
        """
        return self._instances.get(name)

    @property
    def delivery(self) -> DeliveryRouter:
        """Return the outbound delivery router, constructing it on first use.

        The router is created lazily because it takes a back-reference to this
        runner, and constructing it in ``__init__`` would be circular.

        Returns:
            The shared :class:`encre.gateway.delivery.DeliveryRouter`.
        """
        if self._delivery is None:
            self._delivery = DeliveryRouter(self)
        return self._delivery

    @property
    def instances(self) -> dict[str, BasePlatformAdapter]:
        """Return the live adapter registry.

        The mapping is handed out by reference so callers can enumerate
        connected adapters without a copy; it must not be mutated outside
        ``start_adapter`` / ``stop_adapter``.

        Returns:
            The adapter dict keyed by registry name.
        """
        return self._instances

    # -- Plugin connector wiring (S8) --

    def sync_plugin_adapters(self) -> None:
        """Bridge plugin-registered adapter specs into the platform registry.

        Idempotent and cheap: reads the host accumulator populated via
        ``context.gateway_adapters.register()``, registers specs that appeared
        since the last sync, and tears down (stop + unregister) specs that
        disappeared because their plugin was disabled, uninstalled, or
        hot-reloaded away.  Called at gateway start, at the end of plugin
        warmup, after hot reloads, and on every status poll — whichever runs
        first after a plugin activation wins; later calls are no-ops.
        """
        if self._plugin_host is None:
            return
        try:
            specs = list(self._plugin_host.gateway_adapters)
        except Exception as e:
            logger.warning("[gateway] plugin adapter sync failed: %s", e)
            return

        from encre.gateway.platform_registry import register_plugin_adapter

        seen: dict[str, str] = {}
        for spec in specs:
            try:
                name = register_plugin_adapter(spec, "plugin")
                seen[name] = str(getattr(spec, "plugin_name", "") or "plugin")
            except Exception as e:
                logger.warning("[gateway] plugin adapter registration failed: %s", e)
        for name, plugin_name in seen.items():
            if name not in self._plugin_adapter_specs:
                self._plugin_adapter_specs[name] = plugin_name
                # Freshly bridged: replay a parked/enabled config if any, so
                # an adapter configured before its plugin activated connects
                # without waiting for the next frontend push.
                self._spawn_plugin_start(name)

        # Specs that vanished: their plugin was deactivated.  Stop any live
        # instance and drop the registry entry so the panel row disappears.
        for name in [n for n in self._plugin_adapter_specs if n not in seen]:
            self._plugin_adapter_specs.pop(name, None)
            if name in self._instances:
                self._spawn_plugin_stop(name)
            else:
                platform_registry.unregister(name)

        # Retry stops deferred earlier from a loop-less sync call.
        for name in list(self._pending_plugin_stops):
            if name in self._instances:
                self._spawn_plugin_stop(name)
            else:
                self._pending_plugin_stops.discard(name)
                platform_registry.unregister(name)

    def _spawn_plugin_stop(self, name: str) -> None:
        """Asynchronously stop one plugin adapter, best-effort.

        Sync callers (status polling) have no loop to await on; when a running
        loop is reachable the stop task is spawned there, otherwise the name is
        parked in ``_pending_plugin_stops`` and retried on the next sync from
        an async context.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._pending_plugin_stops.add(name)
            return

        async def _stop_and_unregister() -> None:
            try:
                await self.stop_adapter(name)
            finally:
                platform_registry.unregister(name)

        self._pending_plugin_stops.discard(name)
        task = loop.create_task(_stop_and_unregister())
        # Keep a reference so the task is not garbage-collected mid-stop
        # (RUF006); the done-callback releases it when the stop finishes.
        self._plugin_adapter_tasks.add(task)
        task.add_done_callback(self._plugin_adapter_tasks.discard)

    def _spawn_plugin_start(self, name: str) -> None:
        """Auto-start a newly bridged plugin adapter when a stored config
        enables it.

        Covers the warmup race: the frontend pushes its saved connector
        configs right after connect, before plugin activation has bridged
        the registry entries, so ``apply_config`` parks the config (see the
        unknown-platform branch) instead of starting the adapter.  When the
        plugin finally activates and this runner bridges the entry, the
        parked config is replayed here.

        Args:
            name: Registry name of the freshly bridged plugin adapter.
        """
        stored = self._stored_configs.get(name, {})
        if str(stored.get("enabled", "")).lower() not in ("true", "1", "yes"):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _start() -> None:
            try:
                await self.start_adapter(name, dict(stored))
            except Exception as e:
                logger.warning(
                    "[gateway] plugin adapter %s auto-start failed: %s", name, e
                )

        task = loop.create_task(_start())
        self._plugin_adapter_tasks.add(task)
        task.add_done_callback(self._plugin_adapter_tasks.discard)

    async def stop_plugin_adapters(self, plugin_name: str) -> None:
        """Stop and unregister every live adapter contributed by *plugin_name*.

        Explicit teardown API for plugin deactivation paths; the diff-based
        :meth:`sync_plugin_adapters` is the passive fallback.

        Args:
            plugin_name: Name of the plugin whose adapters should stop.
        """
        names = [
            n for n, p in self._plugin_adapter_specs.items() if p == plugin_name
        ]
        for name in names:
            self._plugin_adapter_specs.pop(name, None)
            if name in self._instances:
                await self.stop_adapter(name)
            platform_registry.unregister(name)

    # -- Lifecycle --

    async def start(self) -> None:
        """Bring the gateway up: load config, discover adapters, load hooks.

        Idempotent: a second call while already running returns immediately,
        which lets the desktop launcher and the gateway command both call this
        without racing. Platform discovery happens here rather than at import
        time so an optional vendor SDK that failed to install never blocks the
        rest of the gateway from booting.

        Hook discovery and the relay registration are both best-effort: a
        broken hook module or a misconfigured relay must not prevent a healthy
        adapter from connecting.

        Raises:
            Exception: Any error raised by config loading or hook emission
                propagates to the caller.
        """
        if self._running:
            return
        self._running = True

        # Late-imported so that platforms missing an optional dependency do not
        # fail the whole gateway import.
        from encre.gateway.platforms import discover_platforms
        discover_platforms()

        # Bridge plugin connector specs (S8) before the config load so
        # adapter_<name>_ settings for plugin platforms are picked up.
        self.sync_plugin_adapters()

        # Single config load for the whole process; adapters started later reuse
        # this object rather than re-reading config.yaml.
        self._gateway_config = load_gateway_config()

        # Hooks are loaded once and then emitted on every gateway event; the
        # GATEWAY_STARTUP event lets a plugin snapshot the current adapters.
        try:
            loaded = self._hooks.discover_and_load()
            if loaded:
                logger.info("[gateway] loaded %d hook(s): %s", len(loaded), loaded)
            await self._hooks.emit(GATEWAY_STARTUP, {"adapters": list(self._instances.keys())})
        except Exception as e:
            logger.warning("[gateway] hook startup failed: %s", e)

        # Register the adapter command hook after discovery: discover_and_load()
        # rebuilds the handler table, so a registration made in __init__ would
        # not survive to the first dispatched command.
        register_command_hook(self, self._commands)

        # Restore cached channel metadata so adapters do not have to re-fetch
        # channel ids from vendor APIs after a restart.
        self._channel_dir.load()

        # The relay adapter is a first-class platform that forwards messages
        # between gateways; registering it here makes it appear in the normal
        # adapter panel.
        try:
            from encre.gateway.relay import relay_is_configured, register_relay_adapter
            if relay_is_configured():
                await register_relay_adapter(self)
        except Exception as e:
            logger.warning("[gateway] relay registration failed (non-fatal): %s", e)

        # Replay submit requests that were queued (and persisted) when the
        # previous process ended.  The drain tasks manage their own iClaw
        # lock, so no wrapping context is needed here.
        if self._router is not None:
            try:
                await self._router.replay_queued_submits()
            except Exception as replay_err:
                logger.warning("[gateway] queued submit replay failed: %s", replay_err)

        logger.info("[gateway] Gateway started")

    async def stop(self) -> None:
        """Tear down every adapter, the WS bridge, and the session store.

        The order matters: adapters are disconnected first so they stop
        emitting, then the bridge so no new remote adapter can connect, and
        only then is the session store closed so in-flight saves can finish.

        Raises:
            Exception: Errors raised by ``stop_all`` propagate; a failing
                adapter disconnect is only logged inside ``stop_all``.
        """
        await self.stop_all()
        if self._ws_bridge is not None:
            await self._ws_bridge.stop()
            self._ws_bridge = None
        if self._session_store is not None:
            self._session_store.close()
        self._running = False
        logger.info("[gateway] Gateway stopped")

    async def start_ws_bridge(self, host: str = "127.0.0.1", port: int = 18792) -> None:
        """Serve remote/plugin adapters on a local WebSocket endpoint.

        Remote adapters run in their own process and speak the adapter
        protocol over the bridge, which wraps each connection as a
        ``RemotePlatformAdapter`` indistinguishable from an in-process one.
        Binding to loopback by default keeps third-party plugins off the
        network unless the operator opts into a reachable address.

        Args:
            host: Interface to bind. Loopback is the safe default.
            port: TCP port for the bridge listener.
        """
        from encre.gateway.ws_bridge.server import WsBridgeServer
        self._ws_bridge = WsBridgeServer(
            runner=self,
            host=host,
            port=port,
        )
        await self._ws_bridge.start()
        logger.info("[gateway] WS bridge started on ws://%s:%d", host, port)

    # -- Adapter management --

    async def start_adapter(self, name: str, config: dict[str, Any]) -> bool:
        """Instantiate, validate, and connect one adapter by registry name.

        The adapter is looked up in
        :data:`encre.gateway.platform_registry.platform_registry`, built by
        its factory, wired to this runner's handlers, and connected. Every
        failure path records a human-readable reason in ``_last_errors`` and
        pushes a status update, so the desktop adapter panel always reflects
        the outcome.

        Config values are exported to ``os.environ`` because most vendor SDKs
        read credentials from the environment rather than through a config
        object; the ``PlatformConfig`` is the structured form handed to the
        factory and to ``validate_config``.  A live instance for *name* is
        torn down first, so a re-bind (QR login while already connected, a
        racing config push) can never leave two sockets of one platform
        running -- which would answer every inbound message twice.

        Args:
            name: Registry name of the platform to start.
            config: Flat adapter config; ``enabled`` is stripped before
                exporting because it is runner state, not a vendor setting.

        Returns:
            ``True`` when the adapter connected and was registered.

        Raises:
            Exception: Never raised; all failures are converted into a
                ``False`` return plus a recorded error message.
        """
        # Load the adapter module off the event loop when it is still
        # deferred: the first lookup imports the vendor SDK (feishu ~11s of
        # CPU-bound import), which must never block the loop.  The import is
        # idempotent, so callers that already promoted the platform pay only
        # the cheap ``is_registered`` check.
        if not platform_registry.is_registered(name):
            await asyncio.to_thread(platform_registry.ensure_loaded, name)
        entry = platform_registry.get(name)
        if entry is None:
            msg = f"Unknown platform: {name}"
            logger.warning(msg)
            self._last_errors[name] = msg
            return False

        # Tear down a live instance before replacing it: overwriting
        # ``_instances[name]`` would leak the old socket and its background
        # tasks, and the platform would deliver every message twice.
        if name in self._instances:
            await self.stop_adapter(name)

        # Vendor SDKs consume credentials from the environment, so mirror every
        # non-lifecycle field there before building the structured config.
        # Namespaced per adapter: adapters share generic field names, and a
        # bare export would let the last-started adapter overwrite the
        # credentials every earlier adapter's SDK would read.
        _export_adapter_env(name, config)
        pconfig = PlatformConfig(
            enabled=True,
            token=str(config.get("token", "") or ""),
            extra={k: v for k, v in config.items() if k not in ("enabled", "token")},
        )

        # Vendor validation can both reject silently and raise; both must be
        # recorded rather than only one, otherwise a crashing validator would
        # look like a successful start.
        if entry.validate_config is not None:
            try:
                if not entry.validate_config(pconfig):
                    msg = f"Config validation failed for {name}"
                    logger.warning(msg)
                    self._last_errors[name] = msg
                    return False
            except Exception as e:
                msg = f"Config validation error: {e}"
                self._last_errors[name] = msg
                return False

        try:
            adapter = entry.adapter_factory(pconfig)
            if adapter is None:
                msg = f"Factory returned None for {name}"
                self._last_errors[name] = msg
                return False

            # Inbound messages and fatal transport errors both arrive through
            # callbacks registered on the adapter instance rather than through
            # the runner, so all three must be wired before connect() runs.
            adapter.set_message_handler(self._handle_message)
            adapter.set_fatal_error_handler(self._handle_adapter_fatal_error)
            if hasattr(adapter, 'set_session_store') and self._session_store is not None:
                adapter.set_session_store(self._session_store)

            # connect() is the only point where the adapter can fail silently:
            # it may return False with the reason stored on the instance.
            ok = await adapter.connect()
            if ok:
                self._instances[name] = adapter
                self._last_errors.pop(name, None)
                logger.info("[gateway] Adapter '%s' started", name)
                # Deliver messages that were queued for this adapter when the
                # previous process ended; a no-op when nothing was persisted.
                try:
                    await adapter.replay_pending()
                except Exception as replay_err:
                    logger.warning("[gateway] Pending message replay failed for '%s': %s", name, replay_err)
                # The router advertises connected adapters into the iClaw
                # prompt, so this list must change together with _instances.
                if self._router:
                    self._router.set_connected_adapters(list(self._instances.keys()))
            else:
                err = getattr(adapter, "_fatal_error_message", None) or "connect() returned False"
                self._last_errors[name] = err
                logger.warning("[gateway] Adapter '%s' connect failed: %s", name, err)

            await self._notify_status()
            return ok

        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            self._last_errors[name] = msg
            logger.error("[gateway] Failed to start adapter '%s': %s", name, msg)
            await self._notify_status()
            return False

    async def stop_adapter(self, name: str) -> None:
        """Disconnect and unregister one adapter.

        The instance is removed from ``_instances`` *before* ``disconnect()``
        runs so a slow or failing disconnect cannot keep the adapter
        reported as connected, and any message arriving during teardown is
        dropped rather than queued.

        Args:
            name: Registry name of the adapter to stop. Stopping an adapter
                that is not running is a no-op apart from the status push.
        """
        instance = self._instances.pop(name, None)
        if instance:
            try:
                await instance.disconnect()
                logger.info("[gateway] Adapter '%s' stopped", name)
            except Exception as e:
                logger.warning("[gateway] Error stopping adapter '%s': %s", name, e)
        if self._router:
            self._router.set_connected_adapters(list(self._instances.keys()))
        await self._notify_status()

    async def stop_all(self) -> None:
        """Stop every registered adapter, one disconnect per adapter.

        Adapters are stopped sequentially: a shared vendor rate limit or a
        per-process connection pool can be overwhelmed by a parallel teardown
        burst, and one hanging disconnect would not block the others this way.

        Iterating over a copy of the key list is required because
        :meth:`stop_adapter` mutates ``_instances``.
        """
        for name in list(self._instances.keys()):
            await self.stop_adapter(name)

    async def apply_config(self, flat_config: dict[str, Any]) -> None:
        """Reconcile a flat config push from the frontend with live adapters.

        Each adapter key is merged into :attr:`_stored_configs` before the
        start/stop decision, so a later restart reuses previously supplied
        secrets even when the frontend only sent the ``enabled`` flag. The
        ``enabled`` value is read from the merged dict, not the incoming
        dict, which is what makes toggling an adapter off and on again
        re-authenticate it without re-entering tokens.

        An adapter that is both enabled and already running is restarted: the
        running instance holds live vendor connections that only a full
        disconnect/refresh will pick up new credentials on.

        Args:
            flat_config: Flat config mapping from the frontend; non-adapter
                keys are ignored here and applied elsewhere.
        """
        nested = _extract_adapter_config(flat_config)
        for adapter_id, cfg in nested.items():
            # Load the adapter module off the event loop: the first lookup of
            # a deferred platform imports its vendor SDK (feishu ~11s), which
            # must never stall the loop.  ``is_registered`` covers both
            # concrete and deferred names without triggering a load, so only
            # truly unknown names are resolved here (once, in a worker
            # thread).
            if not platform_registry.is_registered(adapter_id):
                await asyncio.to_thread(platform_registry.ensure_loaded, adapter_id)
            entry = platform_registry.get(adapter_id)
            if entry is None:
                # Unknown right now — most commonly a plugin connector whose
                # activation has not bridged its registry entry yet (the
                # frontend pushes saved configs before warmup finishes).
                # Park the config so sync_plugin_adapters can start the
                # adapter once the entry appears, instead of dropping it.
                stored = self._stored_configs.get(adapter_id, {})
                stored.update(cfg)
                self._stored_configs[adapter_id] = stored
                continue

            # Merge rather than replace so secrets from an earlier push survive.
            stored = self._stored_configs.get(adapter_id, {})
            stored.update(cfg)
            self._stored_configs[adapter_id] = stored

            # String comparison because the frontend serialises booleans as
            # text; accepting three spellings avoids a false "disabled".
            enabled = str(cfg.get("enabled", stored.get("enabled", ""))).lower() in ("true", "1", "yes")
            is_running = adapter_id in self._instances

            if enabled and not is_running:
                await self.start_adapter(adapter_id, stored)
            elif not enabled and is_running:
                await self.stop_adapter(adapter_id)
            elif enabled and is_running:
                # Live vendor connections do not re-read credentials, so a
                # config change for a running adapter needs a full restart.
                await self.stop_adapter(adapter_id)
                await self.start_adapter(adapter_id, stored)

    def _resolve_adapter_target_models(self, adapter_id: str) -> list[str] | None:
        """Return the model restriction configured for one adapter.

        The ``models`` field is a JSON array of model ids selected in
        Settings -> Gateway -> Target Models. When it is present but cannot be
        parsed, the adapter silently falls back to unrestricted model
        selection and a warning is logged rather than failing the message,
        because an unusable model list must not take an adapter offline.

        Args:
            adapter_id: Registry name whose stored config is inspected.

        Returns:
            The list of allowed model ids, or ``None`` when the adapter has no
            restriction and the agent may pick any enabled model.
        """
        from encre.model_selection import target_models_from_config_dict
        stored = self._stored_configs.get(adapter_id, {})
        raw = stored.get("models") or stored.get("model_ids")
        ids = target_models_from_config_dict(stored)
        if raw and ids is None:
            logger.warning("[gateway] adapter '%s' has unparseable target models: %r", adapter_id, raw)
        return ids

    def get_status(self) -> dict[str, Any]:
        """Build the aggregated adapter status consumed by the desktop UI.

        The adapter list is derived from the *registry*, not from
        ``_instances``, so a platform that was never configured still shows up
        as ``disconnected`` instead of disappearing from the panel.

        Returns:
            A dict with ``running``, ``adapters`` (one entry per registered
            platform carrying ``name``, ``connected``, ``status``, ``error``,
            and ``platform``), and ``adapter_count``.
        """
        # Pick up connectors from plugins that activated after the last sync
        # (warmup races gateway start) and drop deactivated ones.  No-op when
        # the runner has no plugin host.
        self.sync_plugin_adapters()
        adapters_list = []
        # No-resolve listing: get_status() runs at every WS connect and on
        # every gateway broadcast, so it must never import a pending vendor
        # SDK module (feishu's lark_oapi alone costs ~11s).  Platforms still
        # deferred render from their labels as "disconnected"; their real
        # metadata appears once something actually loads them.
        known_labels: dict[str, str] = {
            entry.name: entry.label
            for entry in platform_registry.all_entries(resolve=False)
        }
        # Plugin connector cards: declarative UI definitions ride the same
        # registry payload so the renderer can build the settings card from
        # the adapter data alone (no renderer-side per-platform tables).
        known_ui: dict[str, dict] = {
            entry.name: entry.ui
            for entry in platform_registry.all_entries(resolve=False)
            if getattr(entry, "ui", None)
        }
        for dname, dlabel in platform_registry.pending_labels().items():
            known_labels.setdefault(dname, dlabel)
        for name, label in known_labels.items():
            instance = self._instances.get(name)
            if instance:
                adapters_list.append({
                    "name": name,
                    "connected": instance.running,
                    "status": "connected" if instance.running else "error",
                    "error": instance._fatal_error_message,
                    "platform": label,
                    "ui": known_ui.get(name, {}),
                })
            elif name in self._last_errors:
                adapters_list.append({
                    "name": name,
                    "connected": False,
                    "status": "error",
                    "error": self._last_errors[name],
                    "platform": label,
                    "ui": known_ui.get(name, {}),
                })
            else:
                adapters_list.append({
                    "name": name,
                    "connected": False,
                    "status": "disconnected",
                    "platform": label,
                    "ui": known_ui.get(name, {}),
                })
        return {
            "running": self._running,
            "adapters": adapters_list,
            "adapter_count": len(self._instances),
        }

    # -- Core message handling --

    async def _handle_message(self, adapter: BasePlatformAdapter, event: MessageEvent) -> None:
        """Entry point for inbound platform messages, wired as the adapter callback.

        Called by :class:`encre.gateway.platforms.base.BasePlatformAdapter`
        when a user message arrives. It resolves the source, authorizes the
        sender, derives a session key, and hands off to
        :meth:`_handle_message_with_agent`.

        Steps:

        1. Source resolution. A ``None`` source is repaired from the raw
           ``message_id`` so that authorization and routing always receive a
           usable :class:`SessionSource`; dropping the message instead would
           make a malformed adapter event look like a silent no-op.
        2. Authorization. Skipped for adapters with
           ``authorization_is_upstream`` because those platforms enforce
           access control themselves (e.g. an owner-only bot account).
        3. Session key via :func:`encre.gateway.session.build_session_key`.
        4. Delegation to :meth:`_handle_message_with_agent`.

        Args:
            adapter: The adapter that received the message.
            event: The inbound :class:`encre.gateway.platforms.base.MessageEvent`.

        Raises:
            Exception: Never raised; an unauthorized message is answered with
                a pairing hint and a send failure is only logged.
        """
        source = event.source
        if source is None:
            # Some adapters surface events without a full source; reconstructing
            # one from the chat id keeps the message deliverable.
            chat_id = event.message_id or "unknown"
            source = adapter.build_source(
                chat_id=chat_id,
                user_id=None,
            )
            logger.warning(
                "[gateway] Message received without source, constructed from event: chat_id=%s",
                chat_id
            )

        # Only gate when the platform does not already enforce access upstream.
        if not adapter.authorization_is_upstream and self._authz is not None:
            result = self._authz.is_authorized(source, adapter.name)
            if not result.authorized:
                logger.info("[gateway] Unauthorized message from %s/%s: %s",
                           source.platform, source.user_id, result.reason)
                # The rejection reply is best-effort: the platform may reject it
                # for the same reason the message was unauthorized.
                try:
                    await adapter.send(
                        source.chat_id,
                        "⛔ Not authorized. Please contact administrator or use /pair <code> to pair."
                    )
                except Exception as send_err:
                    logger.warning("[gateway] Failed to send auth rejection notice: %s", send_err)
                return

        # The key is stable per chat so a conversation resumes its history.
        session_key = build_session_key(source)

        await self._handle_message_with_agent(adapter, source, event, session_key)

    async def _handle_message_with_agent(
        self,
        adapter: BasePlatformAdapter,
        source: SessionSource,
        event: MessageEvent,
        session_key: str,
    ) -> None:
        """Run the agent for one inbound message and deliver the answer.

        Steps:

        1. Resolve the session id for this conversation from the shared routing
           store, so each chat/thread on a platform keeps its own history and
           that history survives a gateway restart.
        2. Fail closed with a visible error when no router is configured.
        3. Build a :class:`GatewayStreamConsumer` tuned to the adapter's
           edit support.
        4. Submit the prompt and feed every agent event into the consumer.
        5. Finalize delivery, then drop intentional-silence responses.

        The whole agent interaction is wrapped in two timeouts: an outer
        600 s wall-clock bound so a stuck backend cannot hang the adapter
        forever, and an inner 30 s bound on :meth:`iclaw_context` so one slow
        desktop session cannot starve platform messages indefinitely.

        Args:
            adapter: The adapter that will receive the response.
            source: The resolved session source.
            event: The inbound message event carrying the text and reply id.
            session_key: Stable key used only for logging and silence checks.

        Raises:
            Exception: Never raised. Timeouts and agent errors are converted
                into a visible error message for the chat, and a failure to
                deliver that message is logged.
        """
        # 1. One session per conversation: every chat/thread keeps its own
        # history, persisted in the routing store so it survives a restart.
        session_id = await self.resolve_session(adapter.name, source)

        # 2. Without a router there is nothing to run; tell the user rather
        # than silently dropping the message.
        if self._router is None:
            logger.error("[gateway] No router configured, cannot process message from %s", source.chat_id)
            try:
                await adapter.send(
                    source.chat_id,
                    "⚠️ Gateway error: Router not configured. Please contact administrator."
                )
            except Exception as send_err:
                logger.warning("[gateway] Failed to send router error notice: %s", send_err)
            return

        # 2b. Apply a mode requested via /plan // /spec while the session did
        # not exist yet.  Once applied it is mirrored into the session
        # metadata (the same store the desktop set_mode path uses), so the
        # pending preference is cleared and persistence is automatic.
        pending_mode = self._session_store.get_active_mode(build_session_key(source))
        if pending_mode and apply_session_mode(self, session_id, pending_mode):
            self._session_store.set_active_mode(build_session_key(source), None)

        # 3. Adapters without edit support (or with zero length) cannot stream
        # edits, so the consumer must buffer and send once.
        consumer_config = StreamConsumerConfig()
        if not hasattr(adapter, "edit_message") or adapter.max_message_length == 0:
            consumer_config.buffer_only = True

        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id=source.chat_id,
            config=consumer_config,
            reply_to=event.message_id,
            metadata={"thread_id": source.thread_id} if source.thread_id else None,
        )

        # 4. Two nested timeouts: the outer one bounds total wall-clock time so
        # a hung backend cannot stall the adapter, the inner one bounds the lock
        # wait so one slow desktop session cannot starve platform messages.
        target_model_ids = self._resolve_adapter_target_models(adapter.name)
        # A conversation-level pin (via /model) takes priority: it becomes the
        # first target id, which is the candidate the run is pinned onto, while
        # the adapter-wide restriction stays behind it as fallback scope.
        preferred_model = self._session_store.get_preferred_model(session_key)
        if preferred_model:
            target_model_ids = [
                preferred_model,
                *[m for m in (target_model_ids or []) if m != preferred_model],
            ]
        try:
            async with asyncio.timeout(600):
                async with self._router.iclaw_context(timeout=30.0):
                    async for agent_event in self._router.submit_stream(
                        source.platform, event.text, session_id=session_id,
                        target_model_ids=target_model_ids,
                    ):
                        await consumer.feed(agent_event)

            # 5. flush any trailing buffer and return the final text.
            final_text = await consumer.finalize()

            # 6. The agent may intentionally stay silent; that must not surface
            # as an empty message on the platform.
            if is_intentional_silence_response(final_text):
                logger.info("[gateway] Suppressed silence response for %s", session_key)

        except asyncio.TimeoutError:
            logger.error("[gateway] Agent execution timed out for %s (session=%s)", session_key, session_id)
            try:
                await adapter.send(source.chat_id, "⚠️ Agent execution timed out. Please try again.")
            except Exception as send_err:
                logger.error("[gateway] Failed to send timeout notice: %s", send_err)
        except Exception as e:
            logger.error("[gateway] Message processing error: %s %s", type(e).__name__, e, exc_info=True)
            error_msg = f"⚠️ Error processing message: {type(e).__name__}: {e}"
            try:
                # A failed error reply is still an error, but only worth a log:
                # the adapter is likely disconnected and _handle_adapter_fatal_error
                # will report it.
                result = await adapter.send(source.chat_id, error_msg)
                if not result.success:
                    logger.warning("[gateway] Failed to send error notice: %s", result.error)
            except Exception as send_err:
                logger.error("[gateway] Failed to send error notice: %s -> %s", send_err, type(send_err).__name__)

    # -- Fatal error handling --

    async def _handle_adapter_fatal_error(self, adapter: BasePlatformAdapter) -> None:
        """Handle an adapter-reported fatal transport error.

        Called by the adapter itself when its connection is unrecoverable.
        The adapter is removed from ``_instances`` *before* the status is
        pushed so the UI never shows a dead adapter as connected, and the
        router's connected-adapters list is refreshed in the same tick so
        the iClaw prompt stops advertising a dead platform.

        Args:
            adapter: The adapter that hit the fatal error. Its private
                ``_fatal_error_code``, ``_fatal_error_message``, and
                ``_fatal_error_retryable`` attributes carry the detail.

        Raises:
            Exception: Never raised; a failing status callback is logged only.
        """
        name = adapter.name
        code = adapter._fatal_error_code or "unknown"
        message = adapter._fatal_error_message or "Unknown error"
        retryable = adapter._fatal_error_retryable

        logger.error("[gateway] Adapter '%s' fatal error [%s]: %s (retryable=%s)",
                      name, code, message, retryable)

        # Remove from the active set first so the status snapshot is consistent.
        self._instances.pop(name, None)
        self._last_errors[name] = message

        try:
            await self._notify_status()
        except Exception as e:
                logger.warning("[gateway] Status callback error: %s", e)

        # Keep the router's advertised adapter list in step with _instances.
        if self._router:
            self._router.set_connected_adapters(list(self._instances.keys()))

    # -- Session management --

    async def resolve_session(self, adapter_name: str, source: SessionSource) -> str:
        """Resolve or create a session id for one platform source.

        Called on every inbound message, both for in-process adapters and by
        the WS bridge (where a remote adapter cannot reach the session store
        directly). The routing decision is persisted, so a conversation
        resumes its own history across a gateway restart.

        Args:
            adapter_name: The adapter that owns the source (informational).
            source: The platform source to resolve.

        Returns:
            The session id bound to ``source`` by the session store.
        """
        import uuid
        return self._session_store.get_or_create(
            source,
            create_fn=lambda: uuid.uuid4().hex,
        )

    # -- Status notification --

    async def _notify_status(self) -> None:
        """Push the current status snapshot to the frontend.

        The callback may be a plain function or a coroutine function; the
        coroutine form is awaited so a callback that persists status cannot be
        left dangling. A failing callback must never break the caller, since
        every adapter state transition funnels through here.

        Raises:
            Exception: Never raised.
        """
        if self._status_callback:
            try:
                status = self.get_status()
                result = self._status_callback(status)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning("[gateway] Status callback error: %s", e)
