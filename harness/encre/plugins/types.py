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

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any


class PluginSource(Enum):
    """Where a plugin is loaded from, also driving discovery priority."""
    BUNDLED = "bundled"       # Ships with encre
    MANDATORY = "mandatory"   # Hardcoded core tools, cannot be disabled
    INSTALLED = "installed"   # pip-installed third-party
    PROJECT = "project"       # Project-local plugin
    USER = "user"             # User-local plugin


@dataclass
class PluginManifest:
    """Metadata describing a plugin."""
    name: str
    version: str
    description: str = ""
    author: str = ""
    license: str = "MIT"
    homepage: str = ""
    source: PluginSource = PluginSource.INSTALLED
    # Plugin tier: "mandatory" (cannot be uninstalled), "system-default"
    # (ships with the app, removable/reinstallable), "user" (third-party).
    tier: str = "user"
    dependencies: list[str] = field(default_factory=list)
    min_ea_version: str = "0.1.0"
    max_yim_version: str = ""
    tags: list[str] = field(default_factory=list)

    # What this plugin provides
    provides_tools: list[str] = field(default_factory=list)
    provides_skills: list[str] = field(default_factory=list)
    provides_hooks: list[str] = field(default_factory=list)
    provides_backends: list[str] = field(default_factory=list)

    # Declarative manifest (encre-plugin.json) extensions.  All default
    # empty so hand-built manifests keep working unchanged (dual track).
    activation_events: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    engines: dict[str, str] = field(default_factory=dict)
    contributes: dict[str, Any] = field(default_factory=dict)

    # Distributed plugin transport (v0.5+).  Determines where the plugin
    # code executes and how the host communicates with it.
    #   "local"   — in-process (default, backward compatible)
    #   "process" — subprocess sandbox (JSON-RPC over stdio)
    #   "remote"  — network plugin at ``endpoint`` URL (HTTP POST)
    transport: str = "local"
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

    # S6-S9: Extension surfaces (v0.5+).  Each list names the capability
    # the plugin provides, mirroring the existing provides_tools pattern.
    provides_context: list[str] = field(default_factory=list)
    provides_ui: list[str] = field(default_factory=list)
    provides_gateways: list[str] = field(default_factory=list)
    provides_memory: list[str] = field(default_factory=list)

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
            "author": self.author,
            "license": self.license,
            "homepage": self.homepage,
            # Serialise the enum as its string value for portability
            "source": self.source.value,
            "tier": self.tier,
            "dependencies": self.dependencies,
            "min_ea_version": self.min_ea_version,
            "max_yim_version": self.max_yim_version,
            "tags": self.tags,
            "provides_tools": self.provides_tools,
            "provides_skills": self.provides_skills,
            "provides_hooks": self.provides_hooks,
            "provides_backends": self.provides_backends,
            "activation_events": self.activation_events,
            "permissions": self.permissions,
            "engines": self.engines,
            "contributes": self.contributes,
            "transport": self.transport,
            "endpoint": self.endpoint,
            "resource_limits": self.resource_limits,
            "integrity": self.integrity,
            "factory": self.factory,
            "provides_context": self.provides_context,
            "provides_ui": self.provides_ui,
            "provides_gateways": self.provides_gateways,
            "provides_memory": self.provides_memory,
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
