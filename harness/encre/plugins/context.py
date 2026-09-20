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
    "gateway_adapters": "gateways:register",
    "memory_stores": "memory:register",
}

#: Tiers exempt from permission gating: they ship with the application and
#: their code is reviewed together with the core.
_TRUSTED_TIERS = frozenset({"mandatory", "system-default"})


def _permission_granted(surface: str, permissions: list[str]) -> bool:
    """Return True when *permissions* authorises *surface*.

    Accepts the exact token (``tools:register``) or the surface wildcard
    (``tools:*``).  The universal wildcard ``*`` is also honoured so a
    plugin can explicitly opt into full power.
    """
    required = SURFACE_PERMISSIONS.get(surface)
    if required is None:
        return True
    wildcard = f"{surface}:*"
    return (
        "*" in permissions
        or required in permissions
        or wildcard in permissions
    )


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
        gateway_registry: Any = None,
        memory_registry: Any = None,
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
                self._permissions = list(
                    getattr(manifest, "permissions", None) or []
                )
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
        self.gateway_adapters = _mk(_GatewayAdaptersApi, gateway_registry, plugin_name, self)
        self.memory_stores = _mk(_MemoryStoresApi, memory_registry, plugin_name, self)

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
            "Plugin '%s' used context.%s without declaring '%s'; "
            "blocked (add it to manifest permissions)",
            self.plugin_name, surface, required,
        )
        return False

    def record_undo(self, undo: _UNDO) -> _UNDO:
        """Record *undo* on the plugin's rollback stack and return it.

        With a sink (the live registry), the undo lands on the plugin's
        stack immediately 鈥?so registrations made after ``on_activate``
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
        gateway_registry=host.get("gateway_adapters"),
        memory_registry=host.get("memory_stores"),
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
        logger.debug("Plugin '%s': %s host not available in this embedding",
                     self._plugin, self._label)
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
        return self._ctx.record_undo(self._host.plugin_register_backend(name, backend_class))


class _LoopApi(_ApiBase):
    """``encre.loop`` — intercept loop phases via the event backbone."""

    _label = "loop"

    def intercept(self, phase: str, handler: Callable[..., Any]) -> _UNDO:
        """Register *handler* as a phase hook on the backbone; returns undo."""
        if not self._ctx._gate("loop"):
            return _noop_undo
        if self._host is None:
            return self._absent()
        self._host.on(phase, handler)
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
            logger.debug("Plugin '%s' emit %s failed", self._plugin, event_type,
                         exc_info=True)


class _PermissionsApi(_ApiBase):
    """``encre.permissions`` 鈥?register permission policies."""

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
