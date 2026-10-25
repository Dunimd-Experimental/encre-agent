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

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any


class PluginSource(Enum):
    """Where a plugin is loaded from, also driving discovery priority."""

    BUNDLED = "bundled"  # Ships with encre
    MANDATORY = "mandatory"  # Hardcoded core tools, cannot be disabled
    INSTALLED = "installed"  # pip-installed third-party
    PROJECT = "project"  # Project-local plugin
    USER = "user"  # User-local plugin


class PluginState(Enum):
    """Explicit lifecycle state of a plugin inside the registry.

    Transitions::

        DISCOVERED -> ACTIVATED   (activate)
        ACTIVATED  -> DISCOVERED  (deactivate; side effects rolled back)
        DISCOVERED -> FAILED      (activation error; error text in registry)
        PENDING    -> ACTIVATED   (confirm_activation; code imported only
                                   after the user approved the plugin)
        PENDING    -> DISCOVERED  (deny_activation; shell dropped, code
                                   never imported)
        LAZY                  -- registered shell, code deferred until an
                                 activation event fires (then -> ACTIVATED)

    A failed plugin never re-enters ACTIVATED without a successful
    re-registration (e.g. via hot reload), so a broken plugin cannot
    silently look healthy to the rest of the system.
    """

    DISCOVERED = "discovered"
    ACTIVATED = "activated"
    FAILED = "failed"
    LAZY = "lazy"
    PENDING = "pending"


@dataclass
class PluginManifest:
    """Metadata describing a plugin.

    ``name``/``version`` stay required (identity must be stated), but every
    other field carries a sensible default: plugin factories written
    against earlier signatures (and older third-party plugins on disk) must
    keep constructing manifests without ``TypeError``.  Strict completeness
    is enforced at the *declaration entrances* — :func:`load_manifest` and
    the EA pending gate reject empty-ish manifests — so hand-written
    manifests in the wild stay backwards compatible while packaged
    declarations remain explicit.
    """

    # -- Required: identity --------------------------------------------------
    name: str
    version: str
    #: One-line human summary.  Falls back to an empty string so older
    #: factory signatures keep loading; the market UI hides blank rows.
    description: str = ""
    #: Short human-friendly name shown to users (market cards, detail
    #: headers, fault cards).  Purely presentational: the agent, the
    #: registry and the central index keep identifying the plugin by its
    #: standard package ``name``.  Blank falls back to ``name`` in the UI.
    display_name: str = ""
    #: The plugin's own glyph, shown wherever the plugin is named.  It is
    #: always supplied by the plugin, never picked for it: a path inside the
    #: package whose icon file it ships (``"assets/icon.svg"``), a lucide icon
    #: name (``"message-circle"``), or a single emoji.  Anything else — and an
    #: undeclared icon — leaves the slot empty.
    icon: str = ""
    author: str = ""
    license: str = ""
    tags: list[str] = field(default_factory=list)

    # -- Provenance & trust ---------------------------------------------------
    source: PluginSource = PluginSource.INSTALLED
    # Plugin tier: "mandatory" (cannot be uninstalled), "system-default"
    # (ships with the app, removable/reinstallable), "user" (third-party).
    tier: str = "user"
    dependencies: list[str] = field(default_factory=list)
    min_ea_version: str = "0.1.0"
    engines: dict[str, str] = field(default_factory=dict)

    # -- Activation semantics --------------------------------------------------
    # Grouping label for the manifest vocabulary: ``"core"`` and
    # ``"deferred"`` populate two separately-cached lists
    # (``get_core_tools`` / ``get_deferred_tools``) that ``get_all_tools``
    # merges and hands to the agent in one pass.  With register-as-activate
    # there is NO deferred zone — every activated plugin's tools are in the
    # model-facing payload from the first turn, so this field costs no prompt
    # tokens either way and no on-demand "discover" unlock loads a tool
    # (see ``agent``'s tool injection comment).
    # "manual" is the only value with teeth: it opts the whole plugin out of
    # automatic startup, so it stays DISCOVERED until an explicit
    # ``manage(plugin_activate)`` or a matching activation event.
    activation: str = "deferred"
    activation_events: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)

    # -- Optional: extended contact & marketing ------------------------------
    homepage: str = ""
    #: Author support contact, in the order the fault card should prefer:
    #: e-mail first, then the official site, then the source repository.
    email: str = ""
    #: Source repository (GitHub/GitLab/...).  Shown as "report an issue".
    repository: str = ""
    #: Self-service troubleshooting docs for this plugin, if it ships any.
    docs: str = ""

    # -- Optional: what this plugin provides ---------------------------------
    provides_tools: list[str] = field(default_factory=list)
    provides_skills: list[str] = field(default_factory=list)
    #: Declarative hook registrations.  Each entry is
    #: ``{"event": "pre_tool_exec", "handler": "module:func",
    #: "priority": 10, "timeout": 5}`` — the handler is imported and
    #: registered onto the arbitration chain at activation, so a plugin
    #: can join hook arbitration without any ``on_activate`` code.
    #: Requires the ``loop:intercept`` permission for non-trusted tiers.
    provides_hooks: list[Any] = field(default_factory=list)
    provides_backends: list[str] = field(default_factory=list)
    # Declarative manifest (encre-plugin.json) contributions.  Empty for
    # hand-built manifests that register everything in code (dual track).
    contributes: dict[str, Any] = field(default_factory=dict)

    # Distributed plugin transport (v0.5+).  Determines where the plugin
    # code executes and how the host communicates with it.
    #   "local"   — in-process (default, backward compatible)
    #   "process" — subprocess sandbox (JSON-RPC over stdio)
    #   "remote"  — network plugin at ``endpoint`` URL (HTTP POST)
    transport: str = "local"
    #: Explicit opt-in for a third-party plugin to run in-process.  The
    #: default for ``tier="user"`` is the process transport; a plugin that
    #: needs in-process-only surfaces (UI providers, gateways, ...) must
    #: declare this flag and accept that it is no longer a sandbox boundary.
    allow_local: bool = False
    #: Endpoint URL for ``transport="remote"`` plugins; ignored otherwise.
    endpoint: str = ""
    #: Resource limits for ``transport="process"`` plugins.
    #: Keys: ``cpu`` (float, core fraction), ``memory_mb`` (int),
    #: ``timeout_s`` (float, per-call timeout).  All optional.
    resource_limits: dict[str, Any] = field(default_factory=dict)

    # S5: integrity hash of the plugin's .py files (SHA-256).  Verified
    # at load time for non-trusted tiers to detect tampering.
    integrity: str = ""
    # Factory target (``module:attr``) for process-transport plugins.
    factory: str = ""
    # On-disk directory of the plugin package (populated by the EA
    # scanner).  The process transport prepends it to PYTHONPATH so the
    # worker subprocess can import the package's modules.
    pkg_dir: str = ""

    # S6-S9: Extension surfaces (v0.5+).  Declarative capability listings
    # shown on the wire (``snapshot_states``) and in the desktop plugin
    # detail views.  The runtime providers themselves are supplied by the
    # ``EncrePlugin.get_context_providers()`` / ``get_ui_providers()`` /
    # ``get_gateway_adapters()`` / ``get_memory_stores()`` methods, which
    # the registry aggregates regardless of these listings.
    provides_context: list[str] = field(default_factory=list)
    provides_ui: list[str] = field(default_factory=list)
    provides_gateways: list[str] = field(default_factory=list)
    provides_memory: list[str] = field(default_factory=list)

    # MCP / agent contributions (v0.7+).  ``provides_mcp`` entries are MCP
    # server configs (``{"name", "command"|"url", ...}``) merged into
    # ``config.mcp_servers`` at activation; ``provides_agent`` entries are
    # sub-agent definitions (``SubAgentConfig`` dict shape) appended to
    # ``config.sub_agents`` — they never touch the main agent's identity.
    provides_mcp: list[dict[str, Any]] = field(default_factory=list)
    provides_agent: list[dict[str, Any]] = field(default_factory=list)

    #: Declarative tool presentation overrides (v0.6+).  Each entry is
    #: ``{"name": str, "meta": {"icon"|"label"|"card"|"body"|"hidden"|"prefix"
    #: |"result"|"keys"|"terminal_labels"}}`` and is merged over the built-in
    #: ``encre.tools.tool_ui`` catalog, plugin values winning.  Lets a plugin
    #: control how its tools render in the desktop chat without shipping a
    #: renderer.
    tools_meta: list[dict[str, Any]] = field(default_factory=list)

    #: Per-tool danger levels declared by the plugin (``tool_name`` ->
    #: ``"safe" | "high" | "critical"``).  Mirrored from the ``[tool.ea]``
    #: table for EA packages and pushed into the Rust permission engine at
    #: activation time so the decision pipeline classifies dynamically
    #: registered plugin tools without hardcoding.  Values other than the
    #: three levels are ignored by the engine; tools without an entry keep
    #: the static default (High for unknown names).
    danger_levels: dict[str, str] = field(default_factory=dict)

    #: Declarative resource grants (``[tool.ea.resources]`` / manifest
    #: ``resources``).  Fail-closed: anything not declared here is denied
    #: for this plugin by the Rust permission engine.
    #: Keys: ``network`` (allowed outbound hosts, ``"api.example.com"`` or
    #: ``"*.example.org"``), ``network_listen`` (listen endpoints,
    #: ``"host:port"`` / ``":port"``), ``file_read`` / ``file_write`` (glob
    #: patterns, ``~`` expanded, ``**`` crosses directories),
    #: ``process_spawn`` (program whitelist: exact, ``prefix*`` or ``*``),
    #: ``environment`` (readable env var names; secret-shaped names are
    #: denied by an engine-side hard floor even when whitelisted),
    #: ``clipboard`` (``"read"`` | ``"write"`` | ``"both"`` | ``false``),
    #: ``screen`` (bool, screen capture), ``memory_read`` / ``memory_write``
    #: (bool, host user-memory / soul files), ``session_read`` (bool,
    #: session metadata), ``llm_call`` (bool, direct LLM invocation),
    #: ``config_read`` (host config key globs) / ``config_write`` (bool) —
    #: secret-shaped keys are denied by an engine-side hard floor for both,
    #: and ``system_info`` (``true`` =
    #: all basic keys, ``false``/absent = none, or a list of whitelisted
    #: keys like ``["platform", "python"]``).
    #: The plugin's own package directory is always implicitly granted for
    #: both read and write.  Pushed into the engine at activation time and
    #: consulted by the plugin's controlled ``resources`` context API
    #: before any operation executes.
    resources: dict[str, Any] = field(default_factory=dict)

    #: Frontend surface grants (``[tool.ea.ui_permissions]`` / manifest
    #: ``ui_permissions``).  Controls which renderer surfaces the plugin's
    #: data may reach inside ``plugin_ui_sync`` — ungranted surfaces are
    #: filtered out server-side and never sent to the client, so the
    #: renderer physically has nothing to mount.
    #: Keys: ``all`` (bool = every surface), ``providers`` (bool — mountable
    #: iframe/assets UI providers), ``tool_meta`` (bool — tool rendering
    #: metadata), ``contributions`` (list of allowed contribution kinds,
    #: e.g. ``["theme", "settings", "context_menu"]``; ``"*"`` = all).
    #: Defaults are tier-based inside the engine: bundled / mandatory
    #: plugins are trusted (unset = allow); third-party plugins are
    #: untrusted (unset = deny) and must declare every surface they use.
    ui_permissions: dict[str, Any] = field(default_factory=dict)

    def resources_payload(self) -> str:
        """Compile the ``resources`` grants into the engine payload (JSON).

        Single source of truth shared by the parent-process engine push and
        the subprocess transport: the plugin's own package directory is
        implicitly granted for read and write, everything else is
        fail-closed from ``[tool.ea.resources]``.
        """
        grants = self.resources if isinstance(self.resources, dict) else {}

        def _strings(key: str) -> list[str]:
            raw = grants.get(key)
            if not isinstance(raw, list | tuple):
                return []
            return [str(item) for item in raw if str(item).strip()]

        file_read = _strings("file_read")
        file_write = _strings("file_write")
        pkg_dir = str(getattr(self, "pkg_dir", "") or "")
        if pkg_dir:
            file_read.append(pkg_dir)
            file_write.append(pkg_dir)
        return json.dumps(
            {
                "network": _strings("network"),
                "network_listen": _strings("network_listen"),
                "file_read": file_read,
                "file_write": file_write,
                "process_spawn": _strings("process_spawn"),
                "environment": _strings("environment"),
                "clipboard": grants.get("clipboard", False),
                "screen": bool(grants.get("screen", False)),
                "memory_read": bool(grants.get("memory_read", False)),
                "memory_write": bool(grants.get("memory_write", False)),
                "session_read": bool(grants.get("session_read", False)),
                "llm_call": bool(grants.get("llm_call", False)),
                "config_read": _strings("config_read"),
                "config_write": bool(grants.get("config_write", False)),
                "system_info": grants.get("system_info", False),
            },
            ensure_ascii=False,
        )

    def ui_permissions_payload(self) -> str:
        """Compile the ``ui_permissions`` declaration into the engine payload.

        Only explicitly declared keys are emitted; the engine applies the
        trusted/untrusted defaults for anything absent.
        """
        perms = self.ui_permissions if isinstance(self.ui_permissions, dict) else {}
        payload: dict[str, Any] = {}
        if "all" in perms:
            payload["all"] = bool(perms.get("all"))
        if "providers" in perms:
            payload["providers"] = bool(perms.get("providers"))
        if "tool_meta" in perms:
            payload["tool_meta"] = bool(perms.get("tool_meta"))
        raw = perms.get("contributions")
        if isinstance(raw, list | tuple):
            payload["contributions"] = [str(item).strip() for item in raw if str(item).strip()]
        return json.dumps(payload, ensure_ascii=False)

    @property
    def encre_engine(self) -> str:
        """Authoritative harness compatibility range (``engines["encre"]``).

        Falls back to the legacy ``min_ea_version`` field when the
        ``engines`` map is unset, so hand-built manifests keep working.
        New manifests should declare ``engines = {"encre": ">=0.5"}``.
        """
        return self.engines.get("encre") or self.min_ea_version

    @encre_engine.setter
    def encre_engine(self, value: str) -> None:
        """Set ``engines["encre"]`` and mirror into legacy fields (dual-write)."""
        self.engines["encre"] = value
        # Keep the legacy fields consistent for consumers that still read
        # them directly (they are deprecated aliases, not authoritative).
        self.min_ea_version = value

    @property
    def is_lazy(self) -> bool:
        """True when code activation must wait for an activation event."""
        return bool(self.activation_events)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the manifest to a JSON-friendly dict (source as string)."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "display_name": self.display_name,
            "icon": self.icon,
            "author": self.author,
            "license": self.license,
            "homepage": self.homepage,
            "email": self.email,
            "repository": self.repository,
            "docs": self.docs,
            # Serialise the enum as its string value for portability
            "source": self.source.value,
            "tier": self.tier,
            "dependencies": self.dependencies,
            "min_ea_version": self.min_ea_version,
            "tags": self.tags,
            "provides_tools": self.provides_tools,
            "provides_skills": self.provides_skills,
            "provides_hooks": self.provides_hooks,
            "provides_backends": self.provides_backends,
            "activation": self.activation,
            "activation_events": self.activation_events,
            "permissions": self.permissions,
            "engines": self.engines,
            "contributes": self.contributes,
            "transport": self.transport,
            "allow_local": self.allow_local,
            "endpoint": self.endpoint,
            "resource_limits": self.resource_limits,
            "integrity": self.integrity,
            "factory": self.factory,
            "pkg_dir": self.pkg_dir,
            "provides_context": self.provides_context,
            "provides_ui": self.provides_ui,
            "provides_gateways": self.provides_gateways,
            "provides_memory": self.provides_memory,
            "provides_mcp": self.provides_mcp,
            "provides_agent": self.provides_agent,
            "tools_meta": self.tools_meta,
            "danger_levels": self.danger_levels,
            "resources": self.resources,
            "ui_permissions": self.ui_permissions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginManifest:
        """Build a manifest from a JSON-friendly dict (unknown keys ignored)."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class EncrePlugin:
    """Protocol for installable plugins.

    A plugin bundles tools, skills, hooks, and backends into a single
    installable package. Plugins are discovered via entry points or
    directory scanning.

    Usage:
        class MyDatabasePlugin(EncrePlugin):
            manifest = PluginManifest(
                name="encre-postgres",
                version="1.0.0",
                description="PostgreSQL tools for encre",
                icon="database",
                author="Dunimd Team",
                license="Apache-2.0",
                tags=["database", "postgres"],
                source=PluginSource.PROJECT,
                tier="system-default",
                dependencies=[],
                min_ea_version="0.5.0",
                engines={"encre": ">=0.5"},
                activation="core",
                activation_events=[],
                permissions=["tools:register"],
                provides_tools=["database_query", "database_schema"],
            )

            def get_tools(self) -> list[EncreTool]:
                return [DatabaseQueryTool(), DatabaseSchemaTool()]

            def get_skills(self) -> list[BundledSkillDefinition]:
                return [load_skill("database.SKILL.md")]
    """

    manifest: PluginManifest

    def get_tools(self) -> list[Any]:
        """Return tools provided by this plugin."""
        return []

    def get_skills(self) -> list[Any]:
        """Return skills provided by this plugin."""
        return []

    def get_hooks(self) -> list[tuple[str, Any]]:
        """Return (event_type, handler) pairs provided by this plugin."""
        return []

    def get_backends(self) -> dict[str, Any]:
        """Return {name: backend_class} provided by this plugin."""
        return {}

    # S6-S9: Extension surface providers (v0.5+).  Each returns a list of
    # capability objects; plugins that don't provide a surface return [].

    def get_context_providers(self) -> list[Any]:
        """Return context providers that inject messages into the agent context."""
        return []

    def get_ui_providers(self) -> list[Any]:
        """Return UI providers that inject components into the desktop renderer."""
        return []

    def get_gateway_adapters(self) -> list[Any]:
        """Return IM platform gateway adapters (Telegram, WeChat, etc.)."""
        return []

    def get_memory_stores(self) -> list[Any]:
        """Return alternative memory storage backends."""
        return []

    def get_tool_meta(self) -> list[dict[str, Any]]:
        """Return tool presentation overrides for this plugin's tools.

        Each entry mirrors ``encre.tools.tool_ui.ToolUi.as_dict()`` plus the
        owning ``name``.  Entries without a ``name`` key are skipped by the
        host.  Defaults to ``manifest.tools_meta`` so a purely declarative
        plugin needs no code at all.
        """
        return list(self.manifest.tools_meta)

    def on_activate(self, context: Any = None) -> None:
        """Called when the plugin is activated.

        ``context`` is a :class:`~encre.plugins.context.PluginContext` when
        the host provides the ``encre.*`` API surface; hand-written plugins
        may ignore it (positional-optional keeps existing overrides working).
        """

    def on_deactivate(self) -> None:
        """Called when the plugin is deactivated."""
        pass

    def get_config_schema(self) -> dict[str, Any] | None:
        """Return JSON Schema for plugin configuration, or None."""
        return None

    def validate_config(self, _config: dict[str, Any]) -> list[str]:
        """Validate plugin configuration. Returns list of errors (empty = valid)."""
        return []
