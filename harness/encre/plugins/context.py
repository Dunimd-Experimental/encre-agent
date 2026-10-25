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

"""The ``encre.*`` plugin API context.

Every registration made through a :class:`PluginContext` is a *reversible
side effect*: the method returns an ``undo`` callable, and the registry
records it on a per-plugin LIFO stack so deactivation or uninstall rolls
the plugin back exactly (Task 9.2).

Surfaces (Task 9.3):
- ``tools``      register additional tools
- ``backends``   register model backend classes
- ``loop``       intercept loop phases via the event backbone
- ``events``     subscribe/emit typed plugin events
- ``permissions``register permission policies
- ``commands``   register slash commands
- ``sub_agents`` register sub-agent definitions
- ``templates``  register prompt templates

Permission enforcement (fail-closed, Task P0.2):
Plugins whose manifest does not declare the capability required by a
surface have that surface *blocked*: registration attempts log a warning
and become no-ops instead of silently succeeding.  Trusted tiers
(``mandatory`` / ``system-default``) are exempt — they ship with the app
and their declared permissions are informational only.  Every other
tier (``user`` / project / user-directory plugins) must declare e.g.
``"tools:register"`` before ``context.tools.register(...)`` works.
"""

from collections.abc import Callable
from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.plugins")

_UNDO = Callable[[], None]


def _noop_undo() -> None:
    """Undo closure for registrations the host cannot reverse."""


#: Permission token each surface requires.  A manifest that does not list
#: the token (exactly or via the ``surface:*`` wildcard) is blocked from
#: using that surface.  ``events`` subscriptions are always allowed (they
#: only observe); ``events.emit`` is likewise unrestricted.
SURFACE_PERMISSIONS: dict[str, str] = {
    "tools": "tools:register",
    "backends": "backends:register",
    "loop": "loop:intercept",
    "permissions": "permissions:register",
    "commands": "commands:register",
    "sub_agents": "sub_agents:register",
    "templates": "templates:register",
    # S6-S9: extension surfaces (v0.5+)
    "context_providers": "context:register",
    "ui_providers": "ui:register",
    "ui_contributions": "ui:register",
    "gateway_adapters": "gateways:register",
    "memory_stores": "memory:register",
    # Server integration surfaces (v0.5+)
    "ws_handlers": "ws:register",
    "http_routes": "http:register",
    "lifecycle": "lifecycle:register",
}

#: Tiers exempt from permission gating: they ship with the application and
#: their code is reviewed together with the core.
_TRUSTED_TIERS = frozenset({"mandatory", "system-default"})


def _permission_granted(surface: str, permissions: list[str]) -> bool:
    """Return True when *permissions* authorises *surface*.

    Two layers, both mandatory:

    1. The Rust permission engine's user-managed capability table.  A
       ``deny`` entry for the surface capability (e.g. ``tools:register``)
       blocks registration outright -- the policy lives inside the engine,
       so Python-side state cannot bypass it.  Direct native call: the
       compiled extension is a mandatory runtime component.
    2. The manifest token match below: the exact token (``tools:register``)
       or the surface wildcard (``tools:*``).  The universal wildcard ``*``
       is also honoured so a plugin can explicitly opt into full power.
    """
    from encre import native as _native
    required = SURFACE_PERMISSIONS.get(surface)
    if required is not None and _native.permission_capability_policy(required) == "deny":
        return False
    if required is None:
        return True
    wildcard = f"{surface}:*"
    return "*" in permissions or required in permissions or wildcard in permissions


class PermissionError(Exception):
    """Raised (and logged) when a plugin uses a surface it did not declare."""


class PluginContext:
    """Per-plugin API surface handed to ``on_activate(context)``.

    The context is deliberately thin: every ``register_*`` forwards to a
    *host capability object* supplied by the embedding application (the
    loop, the tool registry, ...), records the returned undo closure on
    the context's LIFO undo list, and returns the undo to the plugin.  When a
    capability is absent (harness-only embedding), registration becomes a
    no-op that logs at debug level, so plugins degrade gracefully instead
    of crashing lightweight hosts.

    ``manifest`` (optional) enables the permission gate: when supplied and
    the plugin's tier is not trusted, each surface checks the manifest's
    declared ``permissions`` first.  The gate is intentionally fail-closed
    for third-party code.
    """

    def __init__(
        self,
        plugin_name: str,
        *,
        undo_sink: Callable[[str, _UNDO], None] | None = None,
        tool_registry: Any = None,
        backend_registry: Any = None,
        loop_bus: Any = None,
        event_bus: Any = None,
        permission_registry: Any = None,
        command_registry: Any = None,
        sub_agent_registry: Any = None,
        template_registry: Any = None,
        # S6-S9: extension surface host registries (all optional)
        context_registry: Any = None,
        ui_registry: Any = None,
        contribution_registry: Any = None,
        gateway_registry: Any = None,
        memory_registry: Any = None,
        # Server integration surface host registries (v0.5+)
        ws_handler_registry: Any = None,
        http_route_registry: Any = None,
        lifecycle_registry: Any = None,
        llm_host: Any = None,
        manifest: Any = None,
    ) -> None:
        self.plugin_name = plugin_name
        self._undos: list[_UNDO] = []
        self._undo_sink = undo_sink
        # Permission gate: trusted tiers skip checks entirely.
        self._permissions: list[str] | None = None
        if manifest is not None:
            tier = getattr(manifest, "tier", "user")
            if tier not in _TRUSTED_TIERS:
                self._permissions = list(getattr(manifest, "permissions", None) or [])
        _mk = _make_api
        self.tools = _mk(_ToolsApi, tool_registry, plugin_name, self)
        self.backends = _mk(_BackendsApi, backend_registry, plugin_name, self)
        self.loop = _mk(_LoopApi, loop_bus, plugin_name, self)
        self.events = _mk(_EventsApi, event_bus, plugin_name, self)
        self.permissions = _mk(_PermissionsApi, permission_registry, plugin_name, self)
        self.commands = _mk(_CommandsApi, command_registry, plugin_name, self)
        self.sub_agents = _mk(_SubAgentsApi, sub_agent_registry, plugin_name, self)
        self.templates = _mk(_TemplatesApi, template_registry, plugin_name, self)
        # S6-S9: extension surfaces
        self.context_providers = _mk(_ContextProvidersApi, context_registry, plugin_name, self)
        self.ui_providers = _mk(_UIProvidersApi, ui_registry, plugin_name, self)
        self.ui_contributions = _mk(_UIContributionsApi, contribution_registry, plugin_name, self)
        self.gateway_adapters = _mk(_GatewayAdaptersApi, gateway_registry, plugin_name, self)
        self.memory_stores = _mk(_MemoryStoresApi, memory_registry, plugin_name, self)
        # Server integration surfaces
        self.ws_handlers = _mk(_WSHandlersApi, ws_handler_registry, plugin_name, self)
        self.http_routes = _mk(_HTTPRoutesApi, http_route_registry, plugin_name, self)
        self.lifecycle = _mk(_LifecycleApi, lifecycle_registry, plugin_name, self)
        # Controlled resource access (network / files / system info): every
        # call is arbitrated by the Rust permission engine against the
        # manifest-declared grants.
        self.resources = _ResourceApi(plugin_name, llm_host)
        # Per-plugin persistent state; isolated by plugin name and shared
        # with a process worker through the same on-disk file.
        self.state = _StateApi(plugin_name)

    def _gate(self, surface: str) -> bool:
        """Return True when the plugin may use *surface*.

        Always True when no permission list is attached (trusted tier or a
        hand-built context without a manifest).  Otherwise the manifest
        must declare the surface token.
        """
        if self._permissions is None:
            return True
        if _permission_granted(surface, self._permissions):
            return True
        required = SURFACE_PERMISSIONS.get(surface, "<unknown>")
        logger.warning(
            "Plugin '%s' used context.%s without declaring '%s'; blocked (add it to manifest permissions)",
            self.plugin_name,
            surface,
            required,
        )
        return False

    def record_undo(self, undo: _UNDO) -> _UNDO:
        """Record *undo* on the plugin's rollback stack and return it.

        With a sink (the live registry), the undo lands on the plugin's
        stack immediately — so registrations made after ``on_activate``
        returns (async handlers, event callbacks) are still rolled back.
        """
        if undo is _noop_undo:
            return undo
        if self._undo_sink is not None:
            self._undo_sink(self.plugin_name, undo)
        else:
            self._undos.append(undo)
        return undo

    def drain_undos(self) -> list[_UNDO]:
        """Return and clear locally-buffered undos (used when no sink)."""
        undos, self._undos = self._undos, []
        return undos


def build_context(
    plugin_name: str,
    host: dict[str, Any] | None = None,
    undo_sink: Callable[[str, _UNDO], None] | None = None,
    manifest: Any = None,
) -> PluginContext:
    """Build a :class:`PluginContext` from a host capability mapping.

    ``host`` keys (all optional): ``tools``/``backends``/``loop``/
    ``events``/``permissions``/``commands``/``sub_agents``/``templates``/
    ``context_providers``/``ui_providers``/``gateway_adapters``/
    ``memory_stores``.  Missing capabilities degrade to no-op registration.
    ``undo_sink`` receives every recorded ``(plugin_name, undo)`` pair.

    ``manifest`` (optional) enables the permission gate — when supplied,
    non-trusted tiers must have declared a surface's permission token in
    the manifest before the matching context API will register anything.
    """
    host = host or {}
    return PluginContext(
        plugin_name,
        undo_sink=undo_sink,
        tool_registry=host.get("tools"),
        backend_registry=host.get("backends"),
        loop_bus=host.get("loop"),
        event_bus=host.get("events"),
        permission_registry=host.get("permissions"),
        command_registry=host.get("commands"),
        sub_agent_registry=host.get("sub_agents"),
        template_registry=host.get("templates"),
        context_registry=host.get("context_providers"),
        ui_registry=host.get("ui_providers"),
        contribution_registry=host.get("ui_contributions"),
        gateway_registry=host.get("gateway_adapters"),
        memory_registry=host.get("memory_stores"),
        ws_handler_registry=host.get("ws_handlers"),
        http_route_registry=host.get("http_routes"),
        lifecycle_registry=host.get("lifecycle"),
        llm_host=host.get("llm"),
        manifest=manifest,
    )


def _make_api(cls: type, host: Any, plugin_name: str, ctx: PluginContext) -> Any:
    """Instantiate an API object wired to the context's undo recorder."""
    api = cls(host, plugin_name)
    api._ctx = ctx
    return api


class _ApiBase:
    """Shared no-host guard for context APIs."""

    _label = "api"

    def __init__(self, host: Any, plugin_name: str) -> None:
        self._host = host
        self._plugin = plugin_name

    def _absent(self) -> _UNDO:
        logger.warning(
            "Plugin '%s': %s host not available in this embedding — the "
            "registration was silently dropped (pass a host dict to "
            "activate()/emit_activation_event)",
            self._plugin,
            self._label,
        )
        return _noop_undo


class _ToolsApi(_ApiBase):
    """``encre.tools`` — register tools into the live tool registry."""

    _label = "tools"

    def register(self, tool: Any) -> _UNDO:
        """Register *tool*; returns its undo closure."""
        if not self._ctx._gate("tools"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        try:
            return self._ctx.record_undo(self._host.plugin_register_tool(tool, source=self._plugin))
        except TypeError:
            return self._ctx.record_undo(self._host.register_tool(tool))


class _BackendsApi(_ApiBase):
    """``encre.backends`` — register model backend classes."""

    _label = "backends"

    def register(self, name: str, backend_class: type) -> _UNDO:
        """Register backend class under *name*; returns undo."""
        if not self._ctx._gate("backends"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.plugin_register_backend(name, backend_class, source=self._plugin))


class _LoopApi(_ApiBase):
    """``encre.loop`` — intercept loop phases via the event backbone."""

    _label = "loop"

    def intercept(
        self,
        phase: str,
        handler: Callable[..., Any],
        *,
        priority: int = 100,
        timeout: float = 5.0,
    ) -> _UNDO:
        """Register *handler* as a phase hook on the backbone; returns undo.

        Args:
            phase: The lifecycle phase to intercept.
            handler: The plugin handler callable.
            priority: Lower runs earlier (default ``100``); the built-in
                arbitration layer keeps priority over plugin handlers.
            timeout: Per-invocation wall-clock budget in seconds.
        """
        if not self._ctx._gate("loop"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        self._host.on(
            phase,
            handler,
            source=self._plugin,
            priority=priority,
            timeout=timeout,
        )
        return self._ctx.record_undo(lambda: self._host.off(phase, handler))


class _EventsApi(_ApiBase):
    """``encre.events`` — typed plugin event pub/sub (always permitted)."""

    _label = "events"

    def subscribe(self, event_type: str, handler: Callable[..., Any]) -> _UNDO:
        """Subscribe to *event_type*; returns the unsubscribe closure."""
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.subscribe(event_type, handler, source=self._plugin))

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Emit a plugin event (best effort; never raises into the plugin)."""
        if self._host is None:
            return
        try:
            self._host.emit(event_type, payload or {}, source=self._plugin)
        except Exception:
            logger.debug("Plugin '%s' emit %s failed", self._plugin, event_type, exc_info=True)


class _PermissionsApi(_ApiBase):
    """``encre.permissions`` — register permission policies."""

    _label = "permissions"

    def register(self, policy: Any) -> _UNDO:
        """Register *policy*; returns undo."""
        if not self._ctx._gate("permissions"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.plugin_register_policy(policy, source=self._plugin))


class _CommandsApi(_ApiBase):
    """``encre.commands`` — register slash commands."""

    _label = "commands"

    def register(self, command: dict[str, Any]) -> _UNDO:
        """Register a slash-command definition; returns undo."""
        if not self._ctx._gate("commands"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.plugin_register_command(command, source=self._plugin))


class _SubAgentsApi(_ApiBase):
    """``encre.sub_agents`` — register sub-agent definitions."""

    _label = "sub_agents"

    def register(self, definition: dict[str, Any]) -> _UNDO:
        """Register a sub-agent definition; returns undo."""
        if not self._ctx._gate("sub_agents"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.plugin_register_sub_agent(definition, source=self._plugin))


class _TemplatesApi(_ApiBase):
    """``encre.templates`` -- register prompt templates."""

    _label = "templates"

    def register(self, name: str, template: str) -> _UNDO:
        """Register prompt template under *name*; returns undo."""
        if not self._ctx._gate("templates"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.plugin_register_template(name, template, source=self._plugin))

    def render(self, name: str, **variables: Any) -> str:
        """Render a registered template with ``{var}`` substitution.

        Usable by any plugin at runtime (and by the prompt pipeline when
        expanding ``{{tpl:NAME}}`` references in command prompt bodies).
        Unknown names or bad format strings return an empty string rather
        than raising into the caller.
        """
        if self._host is None:
            return ""
        try:
            template = self._host._templates.get(name)
        except Exception:
            return ""
        if not isinstance(template, str):
            return ""
        try:
            return template.format(**variables)
        except Exception:
            logger.debug("Template %r render failed", name, exc_info=True)
            return ""


class _ContextProvidersApi(_ApiBase):
    """``encre.context_providers`` -- inject context messages (S6).

    Plugins register providers that the agent loop calls to assemble
    additional context messages before each turn.  Each provider has a
    ``name``, ``priority`` (lower = earlier), and ``provide(context_state)``
    that returns a list of message dicts.
    """

    _label = "context_providers"

    def register(self, provider: Any) -> _UNDO:
        """Register a context *provider*; returns undo."""
        if not self._ctx._gate("context_providers"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.plugin_register_context_provider(provider, source=self._plugin))


class _UIProvidersApi(_ApiBase):
    """``encre.ui_providers`` -- inject desktop UI components (S7).

    Plugins register UI providers that the Electron renderer loads as
    dynamic modules.  Each provider has a ``name``, ``entry_point``
    (JS/TS module path), and ``mount_point`` (where in the UI tree it
    attaches: sidebar / panel / settings / chat-overlay).
    """

    _label = "ui_providers"

    def register(self, provider: Any) -> _UNDO:
        """Register a UI *provider*; returns undo."""
        if not self._ctx._gate("ui_providers"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.plugin_register_ui_provider(provider, source=self._plugin))


class _UIContributionsApi(_ApiBase):
    """``encre.ui_contributions`` -- declarative UI contributions.

    A contribution is a JSON-safe ``{kind, spec}`` declaration the renderer
    renders into an existing first-party surface: ``theme`` (CSS variable
    overrides), ``settings`` (rows in the General page or a dedicated page),
    ``context_menu`` (extra menu items) and ``search_mode`` (extra search
    modes).  Plugins declare data only; the renderer keeps full control of
    the markup.
    """

    _label = "ui_contributions"

    def register(self, kind: str, spec: Any) -> _UNDO:
        """Register a *contribution* of *kind*; returns undo."""
        if not self._ctx._gate("ui_contributions"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.plugin_register_contribution(kind, spec, source=self._plugin))


class _GatewayAdaptersApi(_ApiBase):
    """``encre.gateway_adapters`` -- register IM platform adapters (S8).

    Plugins register gateway adapters that connect the agent to IM
    platforms (Telegram, WeChat, QQ Bot, Slack, Discord, etc.).  Each
    adapter implements ``connect()`` / ``disconnect()`` / ``send_message()``
    / ``receive_message()``.
    """

    _label = "gateway_adapters"

    def register(self, adapter: Any) -> _UNDO:
        """Register a gateway *adapter*; returns undo."""
        if not self._ctx._gate("gateway_adapters"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.plugin_register_gateway_adapter(adapter, source=self._plugin))


class _MemoryStoresApi(_ApiBase):
    """``encre.memory_stores`` -- register memory storage backends (S9).

    Plugins register alternative memory stores (vector DB, graph DB,
    custom backends).  Each store implements ``store()`` / ``retrieve()``
    / ``search()`` / ``delete()``.
    """

    _label = "memory_stores"

    def register(self, store: Any) -> _UNDO:
        """Register a memory *store*; returns undo."""
        if not self._ctx._gate("memory_stores"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.plugin_register_memory_store(store, source=self._plugin))


class _WSHandlersApi(_ApiBase):
    """``encre.ws_handlers`` -- register custom WS message handlers.

    Plugins register handlers for custom client message types.  When the
    server's main dispatch table misses a message, plugin handlers are
    checked by message type string.
    """

    _label = "ws_handlers"

    def register(self, message_type: str, handler: Any) -> _UNDO:
        """Register a WS handler for *message_type*; returns undo."""
        if not self._ctx._gate("ws_handlers"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.plugin_register_ws_handler(message_type, handler, source=self._plugin))


class _HTTPRoutesApi(_ApiBase):
    """``encre.http_routes`` -- register custom HTTP admin routes.

    Plugins register HTTP route handlers.  When the server's hardcoded
    admin routes miss a path, plugin routes are checked by prefix match.
    """

    _label = "http_routes"

    def register(self, method: str, path: str, handler: Any) -> _UNDO:
        """Register an HTTP route; returns undo."""
        if not self._ctx._gate("http_routes"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.plugin_register_http_route(method, path, handler, source=self._plugin))


class _LifecycleApi(_ApiBase):
    """``encre.lifecycle`` -- register server lifecycle hooks.

    Plugins register handlers for ``startup`` and ``shutdown`` events.
    Startup hooks fire after the server is ready; shutdown hooks fire
    before the server stops.
    """

    _label = "lifecycle"

    def register(self, event: str, handler: Any) -> _UNDO:
        """Register a lifecycle *handler* for *event*; returns undo."""
        if not self._ctx._gate("lifecycle"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        return self._ctx.record_undo(self._host.plugin_register_lifecycle_hook(event, handler, source=self._plugin))


class _StateApi:
    """Per-plugin persistent JSON state (``encre.state``).

    The store is bound to the plugin name and cannot address another
    plugin's keys.  Values must be JSON-serialisable; the store caps total
    size so one plugin cannot grow the state tree without bound.
    """

    def __init__(self, plugin_name: str) -> None:
        from encre.plugins.state import PluginStateStore

        self._store = PluginStateStore(plugin_name)

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._store.set(key, value)

    def delete(self, key: str) -> None:
        self._store.delete(key)

    def clear(self) -> None:
        self._store.clear()

    def keys(self) -> list[str]:
        return self._store.keys()

    def snapshot(self) -> dict[str, Any]:
        return self._store.snapshot()


class _ResourceApi:
    """Controlled network / filesystem / system-info access for one plugin.
    Every operation asks the Rust permission engine first
    (``permission_check_plugin_resource``); a missing or non-matching
    manifest grant raises :class:`PermissionError`.  Plugins should route
    all resource access through these APIs so the declarative
    ``[tool.ea.resources]`` grants are actually enforced -- raw sockets and
    direct file I/O bypass this layer and are covered by the process
    sandbox instead.
    """

    #: Hard whitelist of basic system-info keys (anything else is rejected
    #: before the grant check; extend deliberately, never dynamically).
    SYSTEM_INFO_KEYS = frozenset({"platform", "machine", "python", "os", "hostname"})

    def __init__(self, plugin_name: str, llm_host: Any = None) -> None:
        self._plugin = plugin_name
        self._llm_host = llm_host

    def _check(self, kind: str, target: str) -> None:
        from encre import native as _native

        if _native.permission_check_plugin_resource(self._plugin, kind, target) != "allow":
            raise PermissionError(
                f"plugin '{self._plugin}' has no {kind} grant for '{target}' "
                "(declare it under [tool.ea.resources])"
            )

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        json: Any = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Engine-gated HTTP request; returns ``{"status", "headers", "text"}``.

        Redirects are not followed automatically -- each redirect target is
        a new host and must pass the grant check on its own.
        """
        self._check("network", str(url))
        import httpx

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.request(method.upper(), str(url), headers=headers, json=json)
        return {
            "status": resp.status_code,
            "headers": dict(resp.headers),
            "text": resp.text,
        }

    def read_text(self, path: str, *, encoding: str = "utf-8") -> str:
        """Engine-gated file read; the path must match a ``file_read`` glob."""
        self._check("file_read", str(path))
        from pathlib import Path

        return Path(path).read_text(encoding=encoding)

    def write_text(self, path: str, text: str, *, encoding: str = "utf-8") -> None:
        """Engine-gated file write; the path must match a ``file_write`` glob."""
        self._check("file_write", str(path))
        from pathlib import Path

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding=encoding)

    def info(self, key: str) -> str:
        """Engine-gated basic system info for a whitelisted *key*."""
        if str(key) not in self.SYSTEM_INFO_KEYS:
            raise PermissionError(f"unknown system-info key '{key}'")
        self._check("system_info", str(key))
        import platform
        import socket
        import sys

        key = str(key)
        if key == "platform":
            return platform.platform()
        if key == "machine":
            return platform.machine()
        if key == "python":
            return platform.python_version()
        if key == "os":
            return sys.platform
        if key == "hostname":
            return socket.gethostname()
        return ""

    def check(self, kind: str, target: str) -> bool:
        """Ask the engine directly whether *kind* access to *target* is granted.

        Useful for plugins that need to branch on permissions instead of
        catching :class:`PermissionError`.
        """
        from encre import native as _native

        return _native.permission_check_plugin_resource(self._plugin, kind, str(target)) == "allow"

    def env(self, name: str) -> str:
        """Engine-gated environment variable read.

        Secret-shaped variable names (TOKEN / SECRET / PASSWORD / ...) are
        denied inside the engine even when explicitly whitelisted.
        """
        self._check("environment", str(name))
        import os

        return os.environ.get(str(name), "")

    def spawn(
        self,
        program: str,
        *args: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Engine-gated subprocess execution against the program whitelist.

        Returns ``{"code", "stdout", "stderr", "timed_out"}``.  Arguments
        are passed through exec-style (no shell interpolation).
        """
        self._check("process_spawn", str(program))
        import subprocess

        try:
            proc = subprocess.run(
                [str(program), *[str(a) for a in args]],
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "code": -1,
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
                "timed_out": True,
            }
        return {
            "code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "timed_out": False,
        }

    async def listen(self, host: str, port: int, handler: Any) -> Any:
        """Engine-gated TCP listener against the ``network_listen`` grants.

        Returns the running ``asyncio.Server``; stop it with ``server.close()``.
        The plugin must declare the endpoint (``"host:port"`` / ``":port"``).
        """
        target = f"{host}:{int(port)}"
        self._check("network_listen", target)
        import asyncio

        return await asyncio.start_server(handler, host or None, int(port))

    def clipboard_read(self) -> str:
        """Engine-gated clipboard read (requires ``clipboard: read|both``)."""
        self._check("clipboard_read", "")
        import pyperclip

        return pyperclip.paste() or ""

    def clipboard_write(self, text: str) -> None:
        """Engine-gated clipboard write (requires ``clipboard: write|both``)."""
        self._check("clipboard_write", "")
        import pyperclip

        pyperclip.copy(str(text))

    def screenshot(self, monitor: int = 0) -> bytes:
        """Engine-gated screen capture (requires ``screen = true``).

        Returns PNG-encoded bytes of *monitor* (0 = all monitors, mss
        indexing applies).
        """
        self._check("screen", "")
        import io

        import mss
        from PIL import Image

        with mss.mss() as sct:
            region = sct.monitors[int(monitor)] if monitor < len(sct.monitors) else sct.monitors[0]
            shot = sct.grab(region)
        png = io.BytesIO()
        Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX").save(png, format="PNG")
        return png.getvalue()

    #: Valid sections of the host user-memory (soul files).
    MEMORY_SECTIONS = ("soul", "identity", "user")

    def memory_read(self, section: str = "user") -> str:
        """Engine-gated host user-memory read (``memory_read = true``).

        *section* is ``"soul"`` | ``"identity"`` | ``"user"``; reuses the
        host soul system (the only memory implementation).
        """
        section = str(section)
        if section not in self.MEMORY_SECTIONS:
            raise ValueError(f"unknown memory section '{section}'")
        self._check("memory_read", section)
        from encre.soul.system import EncreSoulSystem

        files = EncreSoulSystem().load()
        return getattr(files, section) or ""

    def memory_write(self, section: str, content: str) -> None:
        """Engine-gated host user-memory write (``memory_write = true``)."""
        section = str(section)
        if section not in self.MEMORY_SECTIONS:
            raise ValueError(f"unknown memory section '{section}'")
        self._check("memory_write", section)
        from encre.soul.system import EncreSoulSystem

        soul = EncreSoulSystem()
        updater = {
            "soul": soul.update_soul,
            "identity": soul.update_identity,
            "user": soul.update_user,
        }[section]
        updater(str(content))

    def session_history(self) -> list[dict[str, Any]]:
        """Engine-gated host session metadata read (``session_read = true``).

        Returns the parsed ``meta.json`` of every session directory.
        """
        self._check("session_read", "")
        import json

        from encre.config import get_data_dir

        root = get_data_dir() / "sessions"
        out: list[dict[str, Any]] = []
        if not root.is_dir():
            return out
        for meta in sorted(root.glob("*/meta.json")):
            try:
                loaded = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(loaded, dict):
                out.append(loaded)
        return out

    def config_get(self, key: str) -> Any:
        """Engine-gated host configuration read (``config_read`` key glob).

        Secret-shaped keys (tokens / passwords / ...) are denied inside the
        engine even when whitelisted.  Reuses the host config store.
        """
        self._check("config_read", str(key))
        from encre.config_store import load_config

        return load_config().get(str(key))

    def config_set(self, key: str, value: Any) -> None:
        """Engine-gated host configuration write (``config_write = true``)."""
        self._check("config_write", str(key))
        from encre.config_store import load_config, save_config

        data = load_config()
        data[str(key)] = value
        save_config(data)

    async def llm_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> str:
        """Engine-gated LLM call through the host backend (``llm_call = true``).

        Returns the collected assistant text (non-streaming).  The call goes
        through the loop-injected gateway, so it uses the live backend and
        honours fallback swaps.  Messages use OpenAI format.
        """
        self._check("llm_call", "")
        gateway = getattr(self._llm_host, "llm_gateway", None) if self._llm_host is not None else None
        if gateway is None:
            raise RuntimeError("host LLM gateway is not available (no active loop yet)")
        return await gateway(list(messages), max_tokens=max_tokens, system=system)
