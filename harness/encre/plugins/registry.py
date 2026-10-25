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

import asyncio
import contextlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from encre.logging_config import get_logger
from encre.plugins.context import _permission_granted
from encre.plugins.context import build_context as _build_context
from encre.plugins.guard import absorb, is_fatal
from encre.plugins.host import PluginHost
from encre.plugins.integrity import verify_integrity as _verify_integrity
from encre.plugins.manifest import (
    MANIFEST_FILENAME as _MANIFEST_FILENAME,
)
from encre.plugins.manifest import (
    DeclarativePlugin as _DeclarativePlugin,
)
from encre.plugins.manifest import (
    is_valid_plugin_name as _is_valid_plugin_name,
)
from encre.plugins.manifest import (
    load_manifest as _load_manifest,
)
from encre.plugins.tool_meta import collect_tool_meta as _collect_tool_meta
from encre.plugins.transport import (
    PluginTransport,
    RemoteToolProxy,
    create_transport,
)
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource, PluginState

logger = get_logger("encre.plugins")

_ENTRY_POINT_GROUP = "encre.plugins"
_EA_ENTRY_POINT_GROUP = "ea.plugins"


#: Resolved once per process: ``register()`` consulted this for every
#: discovered plugin, and each call re-read + re-parsed ``pyproject.toml``
#: (hundreds of redundant disk reads on a single startup).
_HARNESS_VERSION_CACHE: str | None = None

#: Entry points for the current discovery pass.  The two discovery methods
#: ask for different groups, and each ``entry_points()`` call re-scans every
#: installed distribution; one scan is shared and reset at the start of each
#: :meth:`PluginRegistry.discover_all` so new installs are still seen.
_ENTRY_POINTS_CACHE: list[Any] | None = None


def _entry_points_in(group: str) -> list[Any]:
    """Entry points of *group* from the per-discovery-pass scan."""
    global _ENTRY_POINTS_CACHE
    if _ENTRY_POINTS_CACHE is None:
        try:
            eps = importlib.metadata.entry_points()
            _ENTRY_POINTS_CACHE = (
                [ep for items in eps.values() for ep in items]
                if isinstance(eps, dict)  # Python < 3.12 SelectableGroups
                else list(eps)
            )
        except Exception:
            _ENTRY_POINTS_CACHE = []
    return [ep for ep in _ENTRY_POINTS_CACHE if getattr(ep, "group", "") == group]


def _harness_version() -> str:
    """Return the running harness version (resolved once per process)."""
    global _HARNESS_VERSION_CACHE
    if _HARNESS_VERSION_CACHE is None:
        _HARNESS_VERSION_CACHE = _resolve_harness_version()
    return _HARNESS_VERSION_CACHE


def _resolve_harness_version() -> str:
    """Resolve the running harness version.

    Source checkouts are authoritative: their ``pyproject.toml`` always
    matches the running code, while pip metadata (``encre-harness`` /
    legacy ``encre-core``) can go stale after code updates without a
    reinstall -- a stale version made the plugin engine gate reject every
    plugin, silently dropping tools like ``memory_*`` from sessions.
    Resolution order: source ``pyproject.toml`` -> installed
    ``encre-harness`` distribution -> legacy ``encre-core`` distribution
    -> ``"0.0.0"``.
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        import tomllib

        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        version = str(data.get("project", {}).get("version", "")).strip()
        if version:
            return version
    except Exception:
        pass
    for dist in ("encre-harness", "encre-core"):
        try:
            return importlib.metadata.version(dist)
        except Exception:
            continue
    return "0.0.0"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a version string into a comparable int tuple ("0.5.0" -> (0, 4, 3))."""
    parts: list[int] = []
    for chunk in version.strip().split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def _parse_dependency(spec: Any) -> tuple[str, str, str] | None:
    """Parse ``name`` or ``name>=1.2`` into ``(name, op, version)``.

    A bare name means "installed and activated", no version constraint.
    Malformed constraints are rejected by the caller (fail-closed).
    """
    text = str(spec or "").strip()
    if not text:
        return None
    for op in (">=", "<=", "==", "!=", ">", "<", "~=", "^"):
        if op in text:
            name, _, version = text.partition(op)
            name, version = name.strip(), version.strip()
            if not name or not version:
                return None
            return name, op, version
    return text, "", ""


def _dependency_satisfied(op: str, wanted: str, actual: str) -> bool:
    a, w = _version_tuple(actual), _version_tuple(wanted)
    if not op:
        return True
    if op == ">=":
        return a >= w
    if op == "<=":
        return a <= w
    if op == "==":
        return a == w
    if op == "!=":
        return a != w
    if op == ">":
        return a > w
    if op == "<":
        return a < w
    if op == "~=":
        # Compatible release: same leading component, at least wanted.
        return a >= w and a[: max(1, len(w) - 1)] == w[: max(1, len(w) - 1)]
    if op == "^":
        return a >= w and a[0] == w[0]
    return False


def _engine_satisfied(spec: str, current: str) -> bool:
    """Check a manifest engine spec (``>=0.4``, ``0.5.0``, ``>0.3``...) against *current*.

    Supported operators: ``>=`` (default when omitted), ``>``, ``==``.
    Malformed specs are treated as satisfied (fail-open) so a typo in a
    manifest cannot brick an otherwise working plugin.
    """
    spec = (spec or "").strip()
    if not spec:
        return True
    try:
        if spec.startswith(">="):
            return _version_tuple(current) >= _version_tuple(spec[2:])
        if spec.startswith(">"):
            return _version_tuple(current) > _version_tuple(spec[1:])
        if spec.startswith("=="):
            return _version_tuple(current) == _version_tuple(spec[2:])
        return _version_tuple(current) >= _version_tuple(spec)
    except Exception:
        return True


def _tier_to_source(tier: str) -> PluginSource:
    """Map an EA package tier (from ``[tool.ea]`` metadata) to a plugin source.

    * ``mandatory``      -> MANDATORY  (bundled auto-activation tier; grants
      no extra privileges — a plugin declaring it can still be disabled,
      deactivated, and uninstalled like any other)
    * ``system-default`` -> BUNDLED    (ships with app, can be uninstalled)
    * anything else      -> INSTALLED  (user / third-party, pip-managed)
    """
    if tier == "mandatory":
        return PluginSource.MANDATORY
    if tier == "system-default":
        return PluginSource.BUNDLED
    return PluginSource.INSTALLED


class _PendingPlugin(EncrePlugin):
    """Inert shell for a third-party plugin awaiting user confirmation.

    Carries only the statically-derived manifest (from ``pyproject.toml``
    or distribution metadata) — no plugin code is imported or executed
    while a plugin is in the PENDING state (dynamic authorisation).
    """

    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest

    def get_tools(self) -> list[Any]:
        return []

    def get_skills(self) -> list[Any]:
        return []


class _HybridPlugin:
    """Code plugin layered with the declarative views of its JSON manifest.

    When real code replaces a declarative shell on the lazy-activation
    path, JSON-only declarations would be lost: ``contributes.skills`` in
    particular has no code-side equivalent (``get_skills()`` on a plain
    code class returns nothing, while the shell read the SKILL.md files
    from disk).  The wrapper forwards the lifecycle and capability hooks
    to the code plugin and serves commands/templates/skills from the JSON
    manifest.
    """

    def __init__(self, code: Any, json_manifest: Any, plugin_dir: Path) -> None:
        self._code = code
        self._declarative = _DeclarativePlugin(json_manifest, plugin_dir)

    @property
    def manifest(self) -> Any:
        return self._code.manifest

    # Declarative views (JSON manifest is authoritative here)
    def get_skills(self) -> list[Any]:
        return absorb(self._declarative.get_skills, default=[], context="hybrid get_skills")

    def get_commands(self) -> list[Any]:
        return absorb(self._declarative.get_commands, default=[], context="hybrid get_commands")

    def get_templates(self) -> list[Any]:
        return absorb(self._declarative.get_templates, default=[], context="hybrid get_templates")

    # Code views
    def get_tools(self) -> list[Any]:
        return absorb(self._code.get_tools, default=[], context="hybrid get_tools")

    def get_hooks(self) -> list[Any]:
        return absorb(self._code.get_hooks, default=[], context="hybrid get_hooks")

    def get_backends(self) -> list[Any]:
        return absorb(self._code.get_backends, default=[], context="hybrid get_backends")

    def get_context_providers(self) -> list[Any]:
        return absorb(self._code.get_context_providers, default=[], context="hybrid get_context_providers")

    def get_ui_providers(self) -> list[Any]:
        return absorb(self._code.get_ui_providers, default=[], context="hybrid get_ui_providers")

    def get_gateway_adapters(self) -> list[Any]:
        return absorb(self._code.get_gateway_adapters, default=[], context="hybrid get_gateway_adapters")

    def get_memory_stores(self) -> list[Any]:
        return absorb(self._code.get_memory_stores, default=[], context="hybrid get_memory_stores")

    def get_tool_meta(self) -> list[dict[str, Any]]:
        return absorb(self._code.get_tool_meta, default=[], context="hybrid get_tool_meta")

    def on_activate(self, context: Any) -> None:
        return self._code.on_activate(context)

    def on_deactivate(self) -> None:
        return self._code.on_deactivate()


class PluginRegistry:
    """Discovers, loads, and manages lifecycle of encre plugins.

    Discovery order (later overrides earlier):
    1. Bundled plugins (encre/plugins/bundled/)
    2. pip-installed plugins (entry point: encre.plugins)
    3. EA extension packages (entry point: ea.plugins)
    4. Project-local plugins (./.encre/plugins/)
    5. User-local plugins (~/.dunimd/encre/plugins/)
    """

    def __init__(self) -> None:
        self._plugins: dict[str, EncrePlugin] = {}
        self._manifests: dict[str, PluginManifest] = {}
        # Explicit lifecycle state per plugin (see PluginState).  ``_failed``
        # holds the error text for plugins whose state is FAILED.
        self._states: dict[str, PluginState] = {}
        self._failed: dict[str, str] = {}
        # The registry's own PluginHost: a single capability object that
        # captures every plugin side-effect registered through its
        # context during auto-activation.  The embedding server reads
        # ws_handlers, http_routes and lifecycle hooks from here.
        self._host: PluginHost | None = None
        # Reversible side-effect undo stacks (LIFO rollback on deactivate)
        self._undo_stack: dict[str, list] = {}
        # Lazy plugins: name -> (plugin_dir, source, manifest); code is
        # only imported when an activation event fires (Task 9.4)
        self._lazy: dict[str, tuple[Path, PluginSource, PluginManifest]] = {}
        # Already-instantiated plugins held dormant for event activation:
        # EA packages import their code at discovery, so their
        # ``activation_events`` can only defer *activation*, not import.
        # Cleared per-name when activate() runs.
        self._lazy_registered: set[str] = set()
        # Third-party plugins awaiting user confirmation: name ->
        # {"loader": callable, "pkg": EaPackageInfo | None}.  Their code is
        # NOT imported at discovery time (dynamic authorisation): an inert
        # shell built from static metadata is registered instead, and the
        # real module is only imported by :meth:`confirm_activation`.
        self._pending: dict[str, dict[str, Any]] = {}
        # Names queued by the hot-reloader to re-activate after the next
        # discover_all() (non-mandatory plugins that were active before).
        self._reactivate_queue: set[str] = set()
        # Distributed plugin transports (name -> PluginTransport).
        # Populated during activate() when manifest.transport != "local".
        self._transports: dict[str, PluginTransport] = {}
        # Tool metadata cache: avoids re-calling get_tools() / RPC on
        # every get_all_tools() invocation.
        self._tool_cache: list[Any] | None = None
        self._core_tool_cache: list[Any] | None = None
        self._deferred_tool_cache: list[Any] | None = None
        #: Collision-resolved ``(manifest, owner, tool)`` triples, built once
        #: per cache epoch by :meth:`_resolve_tools`.
        self._resolved_tools: list[tuple[PluginManifest, str, Any]] | None = None
        self._skill_cache: list[Any] | None = None
        self._hook_cache: dict[str, list[Any]] | None = None
        self._backend_cache: dict[str, type[Any]] | None = None
        # Declarative contributions parsed from the manifest at activation:
        # plugin name -> [{event, handler, handler_ref, priority, timeout}]
        # and plugin name -> {backend_name: class}.
        self._decl_hooks: dict[str, list[dict[str, Any]]] = {}
        self._decl_backends: dict[str, dict[str, type[Any]]] = {}
        # S6-S9: extension surface caches
        self._context_cache: list[Any] | None = None
        self._ui_cache: list[Any] | None = None
        self._gateway_cache: list[Any] | None = None
        self._memory_cache: list[Any] | None = None
        self._tool_meta_cache: list[Any] | None = None
        # Installed-ledger memo: ``installed.json`` was re-read from disk on
        # every snapshot_states()/is_plugin_disabled() call.  Validated by
        # the file's ``st_mtime_ns`` so external edits are still picked up;
        # invalidated (mtime reset) by every local write.
        self._ledger_mtime: int | None = None
        self._ledger_cache: dict[str, dict[str, Any]] = {}
        # tool_owner memo: the permission hot path used to re-scan every
        # activated plugin's tool list on each lookup.  Rebuilt on
        # :meth:`_invalidate_cache` (activate/deactivate/register).
        self._owner_map: dict[str, str] | None = None
        # Per-plugin activation mutexes: the ACTIVATED check inside
        # :meth:`activate` and the state flip at its end are not atomic,
        # so two racing callers (auto wave + manual) could both run
        # ``on_activate`` and double-register every contribution.
        self._activation_locks: dict[str, threading.RLock] = {}
        self._activation_locks_guard = threading.Lock()

    def _activation_lock(self, name: str) -> threading.RLock:
        """Return the per-plugin activation mutex (created on first use)."""
        lock = self._activation_locks.get(name)
        if lock is None:
            with self._activation_locks_guard:
                lock = self._activation_locks.get(name)
                if lock is None:
                    lock = threading.RLock()
                    self._activation_locks[name] = lock
        return lock

    @property
    def count(self) -> int:
        return len(self._plugins)

    @property
    def active_count(self) -> int:
        return sum(1 for s in self._states.values() if s == PluginState.ACTIVATED)

    def plugin_state(self, name: str) -> PluginState | None:
        """Return the explicit lifecycle state of a plugin, or ``None``."""
        return self._states.get(name)

    @property
    def host(self) -> PluginHost:
        """The registry-owned plugin host.

        All plugins auto-activated by this registry register their side
        effects (tools, ws_handlers, http_routes, lifecycle hooks, ...)
        into this host, so an embedding server can read them back after
        :meth:`discover_all` / warmup without re-activating anything.
        """
        if self._host is None:
            self._host = PluginHost()
        return self._host

    def attach_host(self, host: PluginHost) -> None:
        """Attach an application-owned PluginHost (e.g. the server's).

        Must be called *before* :meth:`discover_all` so auto-activation
        registers plugin side effects into the embedding application's
        host rather than a private one.  This is how the server surfaces
        (ws_handlers / http_routes / lifecycle / gateway adapters ...)
        become populated.
        """
        self._host = host

    #: Host of the most recently loaded agent (set by ``Agent``).  The
    #: prompt phase reads context providers from here *and* from the
    #: shared host, because plugins activated on either path register
    #: into their respective host.
    agent_host: PluginHost | None = None

    def default_host_dict(self) -> dict[str, Any]:
        """Build the surface→host mapping for the registry's own host."""
        return self.host.build_host_dict()

    def pending_lazy(self) -> list[str]:
        """Return names of plugins waiting for an activation event."""
        return sorted(set(self._lazy) | self._lazy_registered)

    def register(self, plugin: EncrePlugin) -> None:
        """Register a plugin instance directly.

        The manifest's engine requirement (``engines["encre"]`` /
        ``min_ea_version``) is validated against the running harness version
        *before* registration: an incompatible plugin is recorded as FAILED
        and never becomes activatable.
        """
        name = plugin.manifest.name
        if not _engine_satisfied(plugin.manifest.encre_engine, _harness_version()):
            reason = f"requires harness {plugin.manifest.encre_engine}, running {_harness_version()}"
            logger.warning("Plugin {!r} rejected: {}", name, reason)
            self._failed[name] = reason
            self._states[name] = PluginState.FAILED
            return
        existing = self._plugins.get(name)
        if existing is not None:
            # Respect discovery priority: keep the higher-priority source.
            # Equal priority keeps the existing registration — discovery
            # order (directory scan before entry points) wins.
            existing_source_priority = _source_priority(existing.manifest.source)
            new_source_priority = _source_priority(plugin.manifest.source)
            if new_source_priority <= existing_source_priority:
                # New source is not higher priority — skip
                return
        self._plugins[name] = plugin
        self._manifests[name] = plugin.manifest
        # A (re-)registered instance is not activated yet, even if a plugin
        # with the same name was active before — activation is explicit.
        # Plugins declaring ``activation_events`` stay dormant (LAZY) until
        # a matching event fires via emit_activation_event(); auto-activation
        # paths skip them.
        if plugin.manifest.activation_events:
            self._states[name] = PluginState.LAZY
            self._lazy_registered.add(name)
        else:
            self._states[name] = PluginState.DISCOVERED
        self._failed.pop(name, None)

    def unregister(self, name: str) -> bool:
        """Remove a plugin from the registry and deactivate it if needed."""
        if self._states.get(name) == PluginState.ACTIVATED:
            self.deactivate(name)
        self._pending.pop(name, None)
        removed = self._plugins.pop(name, None) is not None
        self._manifests.pop(name, None)
        self._failed.pop(name, None)
        self._states.pop(name, None)
        self._lazy_registered.discard(name)
        if self._decl_hooks.pop(name, None) or self._decl_backends.pop(name, None):
            self._invalidate_cache()
        return removed

    def get(self, name: str) -> EncrePlugin | None:
        """Return the registered plugin with the given name, or None."""
        return self._plugins.get(name)

    def get_manifest(self, name: str) -> PluginManifest | None:
        """Return the manifest for a registered plugin, or None."""
        return self._manifests.get(name)

    def list_all(self) -> list[PluginManifest]:
        """Return manifests for every registered plugin."""
        return list(self._manifests.values())

    def list_activated(self) -> list[str]:
        """Return the sorted names of currently activated plugins."""
        return sorted(name for name, state in self._states.items() if state == PluginState.ACTIVATED)

    def list_failed(self) -> dict[str, str]:
        """Return a mapping of plugin name to the error that caused load failure."""
        return dict(self._failed)

    def _present_on_disk(self, manifest: PluginManifest) -> bool:
        """True when *manifest* describes a package that is really on disk.

        The ledger only records what the market installed, so it can never
        answer "is this plugin present on this machine": the 247 vendor
        packages and every entry-point/pip install live on disk WITHOUT a
        record, and a record can outlive its directory.  Presence is
        therefore read from the package itself — the scanned ``pkg_dir``
        when discovery found one, else the source that produced the
        manifest (bundled / installed / project / user are all disk- or
        distribution-backed; only the hardcoded ``mandatory`` core has no
        package of its own).
        """
        pkg_dir = str(getattr(manifest, "pkg_dir", "") or "")
        if pkg_dir:
            return Path(pkg_dir).is_dir()
        return manifest.source is not PluginSource.MANDATORY

    def snapshot_states(self) -> list[dict[str, Any]]:
        """Return a JSON-friendly lifecycle snapshot of every registered plugin.

        Each entry carries ``name``, ``version``, ``tier``, ``source``,
        ``state``, ``installed`` and ``enabled`` — this is the payload
        broadcast to the frontend as the ``plugin_changed`` frame after a hot
        reload or lifecycle change.

        ``installed`` is presence, not bookkeeping: the plugin has a package
        on this machine (see :meth:`_present_on_disk`) and is not a PENDING
        shell awaiting the user's authorisation.  ``enabled`` is the ledger's
        switch — an absent record means "nobody ever disabled it", so
        everything on disk reads as enabled until the user says otherwise.
        Ledger membership alone no longer decides ``installed``: it used to,
        which made the market's "Installed" tab empty on a machine holding
        hundreds of working plugins, because only market/zip installs are
        recorded.

        The author contact block (``author``/``email``/``homepage``/
        ``repository``/``docs``) rides along so the desktop fault card can
        point the user at the plugin's own support channel without a second
        round-trip.  Empty strings are dropped, so an undeclared contact
        simply does not appear on the wire.
        """
        ledger = self._load_installed_ledger()
        snapshot: list[dict[str, Any]] = []
        for name, manifest in self._manifests.items():
            rec = ledger.get(name)
            state = self._states.get(name, PluginState.DISCOVERED)
            # Presence on this machine — what the market's "Installed" tab
            # and the fault card mean by "installed".  A PENDING shell is not
            # installed yet: its code was never imported and the user may
            # still deny it.
            present = state is not PluginState.PENDING and self._present_on_disk(manifest)
            entry: dict[str, Any] = {
                "name": name,
                "version": manifest.version,
                "description": getattr(manifest, "description", ""),
                "tier": getattr(manifest, "tier", "user"),
                "source": manifest.source.value,
                "state": state.value,
                "installed": rec is not None or present,
                "enabled": not (rec is not None and rec.get("enabled") is False),
            }
            contact = {
                "author": getattr(manifest, "author", ""),
                # Short human-friendly name for user-facing surfaces; blank
                # stays off the wire and the UI falls back to ``name``.
                "display_name": getattr(manifest, "display_name", ""),
                "email": getattr(manifest, "email", ""),
                "homepage": getattr(manifest, "homepage", ""),
                "repository": getattr(manifest, "repository", ""),
                "docs": getattr(manifest, "docs", ""),
                # The plugin's own glyph, so every surface that names this
                # plugin (cards, lists, the fault header) shows its icon
                # rather than a generic one.
                "icon": getattr(manifest, "icon", ""),
            }
            entry.update({k: v for k, v in contact.items() if v})
            extra = {
                "license": getattr(manifest, "license", ""),
                "tags": list(getattr(manifest, "tags", None) or []),
                # Declared permission strings — the market detail view lists
                # them before install so the user sees the grant surface.
                "permissions": list(getattr(manifest, "permissions", None) or []),
                # The two other layers of the grant model travel with the
                # entry so a detail page can state what the plugin may
                # actually touch: `resources` is the fail-closed engine
                # vocabulary (file_read/file_write/network/process_spawn/…)
                # and `ui_permissions` the renderer surfaces it may reach.
                "resources": dict(getattr(manifest, "resources", None) or {}),
                "ui_permissions": dict(getattr(manifest, "ui_permissions", None) or {}),
                "activation": getattr(manifest, "activation", "deferred"),
                "transport": getattr(manifest, "transport", "local"),
                # Declarative capability listings — let consumers (plugin
                # market detail views, fault cards) show what the plugin
                # provides without probing its code.  Empty lists are
                # dropped so undeclared surfaces do not appear on the wire.
                "provides": {
                    key: values
                    for key, values in {
                        "tools": list(getattr(manifest, "provides_tools", None) or []),
                        "skills": list(getattr(manifest, "provides_skills", None) or []),
                        "hooks": list(getattr(manifest, "provides_hooks", None) or []),
                        "backends": list(getattr(manifest, "provides_backends", None) or []),
                        "context": list(getattr(manifest, "provides_context", None) or []),
                        "ui": list(getattr(manifest, "provides_ui", None) or []),
                        "gateways": list(getattr(manifest, "provides_gateways", None) or []),
                        "memory": list(getattr(manifest, "provides_memory", None) or []),
                        "mcp": list(getattr(manifest, "provides_mcp", None) or []),
                        "agents": list(getattr(manifest, "provides_agent", None) or []),
                    }.items()
                    if values
                },
            }
            entry.update({k: v for k, v in extra.items() if v})
            snapshot.append(entry)
        return snapshot

    def tool_owner(self, tool_name: str) -> str | None:
        """Name of the activated plugin that provides ``tool_name``.

        Served from a memoised ``tool -> owner`` map (rebuilt when the
        activation set changes via :meth:`_invalidate_cache`); the permission
        hot path used to re-scan every activated plugin on each lookup.
        Returns ``None`` for core tools and for names no active plugin claims.
        """
        if not tool_name:
            return None
        if self._owner_map is None:
            self._owner_map = self._build_owner_map()
        return self._owner_map.get(tool_name)

    def _build_owner_map(self) -> dict[str, str]:
        """Build ``tool_name -> owner`` across all activated plugins.

        Ownership is read from :meth:`_resolve_tools`, the exact list the
        agent injects, so the permission engine can never audit one plugin for
        a call the model routed to another.  Declared ``provides_tools`` then
        fills names no activated plugin live-provides (an activated shell whose
        code has not been imported yet); it never overrides a real provider.
        """
        owners: dict[str, str] = {}
        for _manifest, owner, tool in self._resolve_tools():
            owners[tool.name] = owner
        for name in self.list_activated():
            manifest = self._manifests.get(name)
            if manifest is None:
                continue
            for tool_name in manifest.provides_tools or ():
                owners.setdefault(str(tool_name), name)
        return owners

    def contact(self, name: str) -> dict[str, str]:
        """The plugin's declared support channels, empty strings omitted."""
        manifest = self._manifests.get(name)
        if manifest is None:
            return {}
        out = {
            "author": getattr(manifest, "author", ""),
            "email": getattr(manifest, "email", ""),
            "homepage": getattr(manifest, "homepage", ""),
            "repository": getattr(manifest, "repository", ""),
            "docs": getattr(manifest, "docs", ""),
        }
        return {k: v for k, v in out.items() if v}

    # ── Installed-plugin ledger (installed.json) ─────────────────

    @staticmethod
    def _installed_ledger_path() -> Path:
        """Path of the authoritative installed-plugin ledger."""
        from encre.paths import get_data_dir

        return get_data_dir("plugins", "installed.json", ensure=False)

    def _load_installed_ledger(self) -> dict[str, dict[str, Any]]:
        """Load ``{name: record}`` from ``installed.json`` (missing -> empty).

        Memoised per process against the file's ``st_mtime_ns``: the
        snapshot broadcast and the per-plugin disabled checks used to
        re-read the JSON on every call.  Returns a copy so callers that
        mutate the result (``record_installed`` / ``record_uninstalled``)
        never poison the memo.
        """
        path = self._installed_ledger_path()
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            self._ledger_mtime = None
            self._ledger_cache = {}
            return {}
        if self._ledger_mtime == mtime:
            return dict(self._ledger_cache)
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            self._ledger_mtime = mtime
            self._ledger_cache = {}
            return {}
        plugins = raw.get("plugins") if isinstance(raw, dict) else None
        self._ledger_cache = plugins if isinstance(plugins, dict) else {}
        self._ledger_mtime = mtime
        return dict(self._ledger_cache)

    def _save_installed_ledger(self, records: dict[str, dict[str, Any]]) -> None:
        """Persist the installed-plugin ledger atomically enough for crash safety."""
        path = self._installed_ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "plugins": records}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        # Drop the memo so the next read sees the freshly written content
        # even when the write lands within the same mtime tick.
        self._ledger_mtime = None

    def record_installed(
        self,
        name: str,
        version: str,
        *,
        source: str = "user",
        pkg_dir: str = "",
        sha256: str = "",
    ) -> None:
        """Add (or refresh) a plugin's entry in the installed ledger.

        An existing record's ``enabled`` flag is preserved so reinstalling
        a disabled plugin does not silently re-enable it.
        """
        records = self._load_installed_ledger()
        prev = records.get(name)
        records[name] = {
            "version": version,
            "source": source,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "enabled": True if prev is None else prev.get("enabled", True),
            "pkg_dir": pkg_dir,
            "sha256": sha256,
        }
        self._save_installed_ledger(records)

    def record_uninstalled(self, name: str) -> None:
        """Drop a plugin's entry from the installed ledger (no-op if absent).

        Its persistent state file is deleted too: an uninstalled plugin must
        not keep data on the user's machine.
        """
        records = self._load_installed_ledger()
        if name in records:
            records.pop(name)
            self._save_installed_ledger(records)
        try:
            from encre.plugins.state import state_path

            state_path(name).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove plugin state for '%s': %s", name, exc)

    def installed_records(self) -> dict[str, dict[str, Any]]:
        """Return the raw ``{name: record}`` installed ledger."""
        return self._load_installed_ledger()

    def is_plugin_disabled(self, name: str) -> bool:
        """True when the installed ledger marks *name* as disabled."""
        rec = self._load_installed_ledger().get(name)
        return rec is not None and rec.get("enabled") is False

    def set_plugin_enabled(self, name: str, enabled: bool) -> bool:
        """Persist the enabled flag and apply it to the live registry.

        Disabling deactivates the plugin immediately (side effects rolled
        back) and the flag survives restarts because activation paths skip
        disabled plugins.  Enabling re-activates through the registry's own
        host (same host as the ``plugin_confirm`` flow).  Returns ``False``
        only when *name* has nothing to switch — no manifest, or a manifest
        whose package is not on this machine.

        A plugin that is present but has no ledger record yet (the app's own
        ``system-default`` packages and entry-point installs never get one
        until the market installs them) is adopted into the ledger on the
        spot: the switch has to persist somewhere, and refusing used to make
        every one of those plugins answer "plugin 'x' is not installed" when
        the market's "Installed" tab offered an enable/disable toggle.
        """
        records = self._load_installed_ledger()
        rec = records.get(name)
        if rec is None:
            manifest = self._manifests.get(name)
            if manifest is None or not self._present_on_disk(manifest):
                return False
            self.record_installed(
                name,
                manifest.version,
                source=manifest.source.value,
                pkg_dir=str(getattr(manifest, "pkg_dir", "") or ""),
            )
            records = self._load_installed_ledger()
            rec = records.get(name)
            if rec is None:  # ledger write failed — nothing to persist into
                return False
        rec["enabled"] = bool(enabled)
        self._save_installed_ledger(records)
        if not enabled:
            if self._states.get(name) == PluginState.ACTIVATED:
                self.deactivate(name)
        elif self._states.get(name) != PluginState.PENDING:
            # Enabling re-activates through the registry's own host (same
            # host as the ``plugin_confirm`` flow).  PENDING plugins are
            # refused by activate() (dynamic authorisation) — leave them
            # awaiting confirm_activation instead of half-applying.
            self.activate(name, host=self.default_host_dict())
        return True

    # ── Discovery ───────────────────────────────────────────────────

    def discover_all(self) -> int:
        """Run all discovery mechanisms. Returns count of newly found plugins."""
        global _ENTRY_POINTS_CACHE
        _ENTRY_POINTS_CACHE = None
        before = len(self._plugins)
        # ``_discover_ea_packages`` walks the same tree and appends each
        # package directory to sys.path as it goes, so the old separate
        # "ensure importable" pre-pass was a second full pyproject scan
        # (and, historically, a second full import pass) for nothing.
        # Order matters: later sources can override earlier, lower-priority ones
        self._discover_ea_packages()
        self._discover_entry_points()
        self._discover_ea_entry_points()
        self._discover_directory("./.encre/plugins/", PluginSource.PROJECT)
        self._discover_directory("~/.dunimd/encre/plugins/", PluginSource.USER)
        # Auto-activate mandatory + system-default plugins immediately
        self._activate_auto_plugins()
        return len(self._plugins) - before

    async def adiscover_all(self) -> int:
        """Async variant of :meth:`discover_all`.

        Runs discovery in a worker thread so the event loop (and therefore the
        UI / request handling) is never blocked while hundreds of plugin
        modules are imported.
        """
        return await asyncio.to_thread(self.discover_all)

    def set_reactivate_on_discover(self, name: str) -> None:
        """Queue *name* for re-activation after the next :meth:`discover_all`.

        Used by the hot-reloader to restore plugins that were active before
        their sources changed (mandatory / system-default tiers re-activate
        unconditionally and do not need this queue).
        """
        self._reactivate_queue.add(name)

    def _activate_auto_plugins(self) -> None:
        """Auto-activate mandatory and system-default plugins.

        These ship with the app and must be available without any explicit
        activation step.  User-installed (third-party) plugins stay lazy and
        are activated on demand or via activation events.  Names queued via
        :meth:`set_reactivate_on_discover` are restored as well (hot reload).

        Activation passes the registry's own :attr:`host` so side effects are
        recorded even when the embedding application has not supplied one --
        this is what lets the server surfaces (ws_handlers, http_routes,
        lifecycle) populate during warmup.

        Candidates are grouped into dependency-safe waves
        (:meth:`_activation_waves`) and each wave activates concurrently --
        with dozens of bundled plugins this turns the serial activation
        chain into a handful of parallel bursts.
        """
        auto_host: dict[str, Any] | None = self.default_host_dict()
        auto_names = [
            name
            for name, plugin in list(self._plugins.items())
            if self._states.get(name) != PluginState.ACTIVATED
            and (
                plugin.manifest.source == PluginSource.MANDATORY
                or getattr(plugin.manifest, "tier", "user") in ("mandatory", "system-default")
            )
            # Disabled (installed.json ledger) bundled plugins stay dormant;
            # the historical mandatory exemption is retired — the tier
            # grants no special treatment.
            and not self.is_plugin_disabled(name)
            # ``activation="manual"`` and ``activation_events`` opt-outs
            # apply to every tier: manual waits for manage(plugin_activate),
            # event-declared plugins stay in the lazy pool (register()).
            and getattr(plugin.manifest, "activation", "deferred") != "manual"
            and not plugin.manifest.activation_events
        ]
        for wave in self._activation_waves(auto_names):
            self._run_activation_wave(wave, auto_host)
        queued, self._reactivate_queue = self._reactivate_queue, set()
        for name in queued:
            if name in self._plugins and self._states.get(name) != PluginState.ACTIVATED and self.activate(name, host=auto_host):
                logger.info("Re-activated plugin '%s' after hot reload", name)

    def _discover_ea_packages(self) -> None:
        """Discover bundled EA packages by scanning the ``ea_tools/`` tree.

        Each package directory is auto-scanned (see :mod:`encre.plugins.ea_scan`);
        its tier, taken from ``pyproject.toml`` ``[tool.ea]``, decides the plugin
        source.  This works identically in development and in PyInstaller frozen
        bundles — no package name is hardcoded here.

        A persisted discovery snapshot (:mod:`encre.plugins.discovery_cache`)
        makes the steady state import-free: unchanged packages are rebuilt
        from their captured manifest + tool surface and their modules are
        imported only when a tool is first executed.  Packages whose surfaces
        are not data (overridden contribution methods, non-JSON metadata)
        keep the eager path, where factories are imported concurrently in a
        bounded thread pool and registered sequentially afterwards to keep
        discovery order deterministic.
        """
        from encre.plugins import discovery_cache
        from encre.plugins.ea_scan import TIER_USER, iter_ea_packages

        packages = [pkg for pkg in iter_ea_packages() if pkg.name not in self._plugins]
        if not packages:
            return

        # Dynamic authorisation: third-party (tier "user") EA packages
        # never run code at discovery time.  Register an inert shell built
        # from the static pyproject metadata; the module is imported only
        # by :meth:`confirm_activation` after the user approves it.
        third_party = [pkg for pkg in packages if pkg.tier == TIER_USER]
        for pkg in third_party:
            self._register_pending_ea(pkg)
        packages = [pkg for pkg in packages if pkg.tier != TIER_USER]
        if not packages:
            return

        root = packages[0].pkg_dir.parent
        snapshot = discovery_cache.load_cache(root)
        stamps = {pkg.name: discovery_cache.package_stamp(pkg) for pkg in packages}

        # Snapshot hits: rebuild manifest + tool surface from data, import
        # nothing.  A stale/incompatible entry is imported below.  Nothing
        # is registered yet -- registration must keep the scanner's package
        # order so every downstream listing stays deterministic regardless
        # of which packages happened to be cache hits.
        lazy_plugins: dict[str, Any] = {}
        eager: list[Any] = []
        for pkg in packages:
            entry = snapshot.get(pkg.name)
            if entry and entry.get("stamp") == stamps[pkg.name]:
                plugin = discovery_cache.build_lazy_plugin(pkg, entry)
                if plugin is not None:
                    lazy_plugins[pkg.name] = plugin
                    continue
            eager.append(pkg)

        outcomes: dict[str, Any] = {}
        if eager:
            # GIL-bound imports: a handful of threads is as fast as 32 and
            # leaves scheduling room for the event loop (see
            # auto_installer._import_workers).
            workers = int(os.environ.get("ENCRE_PLUGIN_IMPORT_WORKERS", "0")) or 4
            if workers > 1 and len(eager) > 1:
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ea-import") as pool:
                    futures = [(pkg, pool.submit(self._instantiate_ea_plugin, pkg)) for pkg in eager]
                    for pkg, future in futures:
                        try:
                            outcomes[pkg.name] = future.result()
                        except Exception as e:
                            outcomes[pkg.name] = e
            else:
                for pkg in eager:
                    try:
                        outcomes[pkg.name] = self._instantiate_ea_plugin(pkg)
                    except Exception as e:
                        outcomes[pkg.name] = e

        captured: dict[str, dict[str, Any]] = {}
        reused = 0
        for pkg in packages:
            lazy = lazy_plugins.get(pkg.name)
            if lazy is not None:
                self.register(lazy)
                if self._plugins.get(lazy.manifest.name) is lazy:
                    reused += 1
                    logger.debug(
                        "Discovered EA plugin '%s' (tier=%s) from snapshot; code deferred",
                        lazy.manifest.name,
                        pkg.tier,
                    )
                continue
            outcome = outcomes.get(pkg.name)
            if isinstance(outcome, BaseException):
                logger.warning(f"Failed to load EA package '{pkg.name}': {outcome}")
                self._failed[pkg.name] = str(outcome)
                continue
            if not isinstance(outcome, EncrePlugin):
                logger.warning(f"EA package '{pkg.name}' did not return an EncrePlugin instance")
                continue
            if not self._finalize_ea_plugin(outcome, pkg):
                continue
            entry = discovery_cache.capture_entry(outcome, pkg, stamps[pkg.name])
            if entry is not None:
                captured[pkg.name] = entry
            logger.info(f"Discovered EA plugin '{outcome.manifest.name}' (tier={pkg.tier}) via directory scan of {pkg.pkg_dir}")

        if captured:
            snapshot.update(captured)
            discovery_cache.save_cache(root, snapshot)
        if reused:
            logger.info(
                "EA discovery: %d plugin(s) restored from snapshot, %d imported",
                reused,
                len(captured),
            )

    def _finalize_ea_plugin(self, plugin: EncrePlugin, pkg: Any) -> bool:
        """Stamp EA package metadata onto a plugin instance and register it.

        Shared by the immediate discovery path and
        :meth:`confirm_activation`.  Returns ``True`` when the plugin is
        registered (or already registered from a higher-priority source).
        """
        name = pkg.name
        plugin.manifest.source = _tier_to_source(pkg.tier)
        # Keep the tier on the manifest for uninstall / manager decisions.
        plugin.manifest.tier = pkg.tier
        # Record the package's factory target + on-disk location so a
        # ``transport = "process"`` declaration can spawn a worker with
        # the right ``ENCRE_PLUGIN_FACTORY`` and PYTHONPATH.
        plugin.manifest.factory = pkg.factory_target or plugin.manifest.factory
        plugin.manifest.pkg_dir = str(pkg.pkg_dir) or plugin.manifest.pkg_dir
        # Mirror the ``[tool.ea]`` table onto the manifest so a package
        # declares transport mode, permissions and activation events in
        # pyproject.toml alone.
        tool_ea = getattr(pkg, "tool_ea", {}) or {}
        for key in (
            "transport",
            "allow_local",
            "endpoint",
            "resource_limits",
            "permissions",
            "activation_events",
            "provides_tools",
            "provides_skills",
            "provides_hooks",
            "provides_backends",
            "activation",
            "danger_levels",
            "resources",
            "ui_permissions",
            "provides_mcp",
            "provides_agent",
            # The packaged declaration wins over whatever the factory's own
            # manifest literal says: ``[tool.ea]`` is also what the central
            # index publishes and what a pip-free install re-renders, so an
            # icon declared twice and drifting apart is the same bug in three
            # surfaces.  A package that omits it keeps its factory value.
            "icon",
            # Same packaged-declaration-wins contract for the user-facing
            # display name (the pip-free install re-renders it from the
            # central index, so drift here would undo that).
            "display_name",
        ):
            value = tool_ea.get(key)
            if value not in (None, "") and hasattr(plugin.manifest, key):
                setattr(plugin.manifest, key, value)
        # Skills the package ships on disk always count as provided, even when
        # ``[tool.ea]`` declares a shorter list (a package can add a skill
        # without editing its manifest).  Union, declared order first: a
        # manifest that under-declared used to hide the extra skills from the
        # market's capability section.
        from encre.plugins.ea_scan import packaged_skills

        packaged = packaged_skills(pkg)
        if packaged:
            plugin.manifest.provides_skills = list(dict.fromkeys(
                [*(plugin.manifest.provides_skills or []), *packaged]))
        elif not plugin.manifest.provides_skills:
            plugin.manifest.provides_skills = []
        # Mirror declarative contributions declared directly in [tool.ea]:
        # either a full ``contributes`` table or a top-level
        # ``ui_contributions`` list.  Both are merged into the manifest's
        # ``contributes`` dict (plugin.json entries win) so
        # ``_register_manifest_contributions`` picks them up on activate.
        ea_contributes = tool_ea.get("contributes")
        if isinstance(ea_contributes, dict) and ea_contributes:
            merged = dict(plugin.manifest.contributes or {})
            for ckey, cval in ea_contributes.items():
                merged.setdefault(ckey, cval)
            plugin.manifest.contributes = merged
        ea_ui = tool_ea.get("ui_contributions")
        if isinstance(ea_ui, list) and ea_ui:
            merged = dict(plugin.manifest.contributes or {})
            existing = merged.get("ui_contributions")
            merged["ui_contributions"] = list(existing) + list(ea_ui) if isinstance(existing, list) else list(ea_ui)
            plugin.manifest.contributes = merged
        # S5: verify code integrity for non-trusted tiers.  Re-checked on
        # the confirmation path too, catching tampering between discovery
        # and user approval.
        if not _verify_integrity(plugin.manifest, pkg.pkg_dir):
            logger.warning(
                "Integrity check failed for '%s'; skipping",
                name,
            )
            self._failed[name] = "integrity verification failed"
            return False
        self.register(plugin)
        # register() may reject the plugin (e.g. engine version mismatch);
        # the reason is already in _failed / the log.
        return self._plugins.get(plugin.manifest.name) is plugin

    def _register_pending_ea(self, pkg: Any) -> None:
        """Register a third-party EA package as PENDING (no code execution).

        Builds the manifest purely from the already-parsed ``pyproject.toml``
        metadata, verifies package integrity on disk, and stores a loader
        closure for :meth:`confirm_activation`.
        """
        if not _is_valid_plugin_name(str(pkg.name)):
            logger.warning("EA package %r has an invalid plugin name; skipped", pkg.name)
            self._failed[str(pkg.name)] = "invalid plugin name"
            return
        tool_ea = getattr(pkg, "tool_ea", {}) or {}
        engines_decl = dict(tool_ea.get("engines") or {})
        min_ea = str(tool_ea.get("min_ea_version") or engines_decl.get("encre") or "0.1.0")
        manifest = PluginManifest(
            name=str(pkg.name),
            version=str(pkg.version),
            description=str(pkg.description),
            display_name=str(tool_ea.get("display_name") or ""),
            icon=str(tool_ea.get("icon") or ""),
            author=str(pkg.author),
            license=str(tool_ea.get("license") or ""),
            tags=[str(t) for t in (tool_ea.get("tags") or [])],
            source=PluginSource.INSTALLED,
            tier=pkg.tier,
            dependencies=[str(d) for d in (tool_ea.get("dependencies") or [])],
            min_ea_version=min_ea,
            engines=engines_decl,
            activation=str(tool_ea.get("activation") or "deferred"),
            activation_events=[str(e) for e in (tool_ea.get("activation_events") or [])],
            permissions=[str(p) for p in (tool_ea.get("permissions") or [])],
            factory=pkg.factory_target,
            pkg_dir=str(pkg.pkg_dir),
        )
        for key in (
            "transport",
            "allow_local",
            "endpoint",
            "resource_limits",
            "permissions",
            "activation_events",
            "provides_tools",
            "provides_skills",
            "provides_hooks",
            "provides_backends",
            "activation",
            "danger_levels",
            "resources",
            "ui_permissions",
        ):
            value = tool_ea.get(key)
            if value not in (None, "") and hasattr(manifest, key):
                setattr(manifest, key, value)
        # A package that ships its skills without declaring them (every
        # official EA package does this today) still HAS them on disk: read the
        # names off the real ``skills/`` tree so the market can say what a
        # plugin provides instead of showing an empty capability section.  The
        # declared list and the on-disk list are unioned — a manifest may
        # legitimately declare only some of the skills it ships.
        from encre.plugins.ea_scan import packaged_skills

        packaged = packaged_skills(pkg)
        if packaged:
            manifest.provides_skills = list(dict.fromkeys(
                [*(manifest.provides_skills or []), *packaged]))
        # Integrity is file-hash verification — no plugin code runs here.
        if not _verify_integrity(manifest, pkg.pkg_dir):
            logger.warning("Integrity check failed for pending '%s'; skipping", pkg.name)
            self._failed[pkg.name] = "integrity verification failed"
            return
        shell = _PendingPlugin(manifest)
        self.register(shell)
        if self._plugins.get(manifest.name) is not shell:
            # register() rejected the shell (engine gate / higher-priority
            # registration already present).
            return
        self._states[manifest.name] = PluginState.PENDING
        self._pending[manifest.name] = {
            "loader": lambda pkg=pkg: self._instantiate_ea_plugin(pkg),
            "pkg": pkg,
        }
        logger.info(
            "Third-party EA package '%s' registered as pending confirmation",
            manifest.name,
        )

    def _register_pending_entry_point(self, ep: Any) -> None:
        """Register a pip entry-point plugin as PENDING (no code execution).

        ``ep.load()`` imports the plugin module, so it must not run during
        discovery.  Static metadata comes from the distribution metadata
        (version/summary/author) which is data, not code.

        Plugin identity is the entry-point KEY, not the distribution name:
        the central repository's contract (enforced by ``encre-plugin
        build``) is one plugin per wheel with ``ep key == manifest.name``,
        and the market addresses plugins by that catalog name.  Keying the
        shell by the dist name (``ea-plugin-foo``) instead of ``foo`` made
        confirm/lookup miss the pending entry after a central install.
        """
        name = str(ep.name)
        version = "0.0.0"
        description = ""
        author = ""
        license = ""
        homepage = ""
        dist = getattr(ep, "dist", None)
        if dist is not None:
            try:
                metadata = dist.metadata
                version = str(metadata.get("Version", "") or version)
                description = str(metadata.get("Summary", "") or "")
                author = str(metadata.get("Author", "") or metadata.get("Author-email", "") or "")
                license = str(metadata.get("License", "") or metadata.get("License-Expression", "") or "")
                homepage = str(metadata.get("Project-URL", "") or metadata.get("Home-page", "") or "")
            except Exception:
                pass
        manifest = PluginManifest(
            name=name,
            version=version,
            description=description,
            icon="",
            author=author,
            license=license,
            tags=[],
            source=PluginSource.INSTALLED,
            tier="user",
            dependencies=[],
            min_ea_version="0.1.0",
            engines={},
            activation="deferred",
            activation_events=[],
            permissions=[],
            homepage=homepage,
        )
        shell = _PendingPlugin(manifest)
        self.register(shell)
        if self._plugins.get(manifest.name) is not shell:
            return
        self._states[manifest.name] = PluginState.PENDING
        self._pending[manifest.name] = {
            "loader": lambda ep=ep: ep.load()(),
            "pkg": None,
        }
        logger.info(
            "Plugin '%s' from entry point '%s' registered as pending confirmation",
            manifest.name,
            ep.name,
        )

    @staticmethod
    def _instantiate_ea_plugin(pkg: Any) -> Any:
        """Import an EA package's factory target and instantiate its plugin."""
        module_path, _, attr = pkg.factory_target.partition(":")
        module = importlib.import_module(module_path)
        plugin_factory = getattr(module, attr) if attr else module
        return plugin_factory()

    def _discover_entry_points(self) -> None:
        """Discover plugins registered via pip entry points."""
        for ep in _entry_points_in(_ENTRY_POINT_GROUP):
            # Dynamic authorisation: pip-installed plugins are third-party
            # code.  ``ep.load()`` would import the module, so discovery
            # only registers a pending shell built from static metadata;
            # the real import happens in :meth:`confirm_activation`.
            try:
                self._register_pending_entry_point(ep)
            except Exception as e:
                logger.warning(f"Failed to register pending plugin from entry point '{ep.name}': {e}")
                self._failed[ep.name] = str(e)

    def _discover_ea_entry_points(self) -> None:
        """Discover third-party EA plugins registered via the ``ea.plugins`` entry-point group.

        Only user-installed plugins are expected here — bundled (mandatory /
        system-default) EA packages are discovered by the directory scan in
        :meth:`_discover_ea_packages` and are skipped if already registered.
        """
        for ep in _entry_points_in(_EA_ENTRY_POINT_GROUP):
            # Same dynamic-authorisation rule as ``encre.plugins``: never
            # import third-party code at discovery (see
            # :meth:`_register_pending_entry_point`).
            try:
                self._register_pending_entry_point(ep)
            except Exception as e:
                logger.warning(f"Failed to register EA plugin from entry point '{ep.name}': {e}")
                self._failed[ep.name] = str(e)

    def _discover_directory(self, dir_path: str, source: PluginSource) -> None:
        """Scan a directory for plugin packages and single-file plugins."""
        path = Path(dir_path).expanduser().resolve()
        if not path.is_dir():
            return

        for entry in sorted(path.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                self._load_plugin_from_dir(entry, source)
            elif entry.suffix == ".py" and entry.stem != "__init__":
                self._load_plugin_from_file(entry, source)

    def _load_plugin_from_dir(self, path: Path, source: PluginSource) -> None:
        """Load a plugin from a directory (declarative-first, 9.1 dual track).

        Priority: ``encre-plugin.json`` manifest (declarative and/or hybrid)
        → ``plugin.py`` → ``__init__.py``.  When the manifest declares
        ``activationEvents`` and a ``plugin.py`` exists, the code module is
        NOT imported at discovery time — the plugin registers lazily with
        its static contributions visible and code deferred until the event
        fires (Task 9.4).
        """
        manifest_file = path / _MANIFEST_FILENAME
        plugin_file = path / "plugin.py"
        init_file = path / "__init__.py"

        if manifest_file.is_file():
            manifest = _load_manifest(manifest_file)
            if manifest is not None:
                manifest.source = source
                if plugin_file.exists() and not manifest.is_lazy:
                    # Hybrid track: manifest + immediate code load
                    before = set(self._plugins)
                    self._load_module_from_file(str(plugin_file), path.stem, source)
                    for new_name in set(self._plugins) - before:
                        self._merge_manifest(self._plugins[new_name].manifest, manifest)
                    return
                if plugin_file.exists() and manifest.is_lazy:
                    if self._states.get(manifest.name) == PluginState.ACTIVATED:
                        # Re-discovery hit on an already-live lazy plugin: the
                        # real code instance replaced the shell on activation.
                        # Touch nothing -- re-adding it to the lazy pool or
                        # resetting its state here would evict a live plugin's
                        # tools, hooks and providers mid-flight.
                        return
                    self._lazy[manifest.name] = (path, source, manifest)
                    self.register(_DeclarativePlugin(manifest, path))
                    self._states[manifest.name] = PluginState.LAZY
                    logger.info("Lazy plugin '%s' registered (static contributions only)", manifest.name)
                    return
                # Declarative-only track
                self.register(_DeclarativePlugin(manifest, path))
                logger.info("Declarative plugin '%s' loaded from %s/%s", manifest.name, source.value, path.stem)
                return

        if plugin_file.exists():
            self._load_module_from_file(str(plugin_file), path.stem, source)
        elif init_file.exists():
            self._load_plugin_package(str(path), source)

    @staticmethod
    def _merge_manifest(target: PluginManifest, overrides: PluginManifest) -> None:
        """Fill empty fields of a code-built manifest from the JSON one.

        The JSON manifest is authoritative for anything the code manifest
        leaves at its default — otherwise declarations like
        ``activation``/``transport``/``danger_levels`` in
        ``encre-plugin.json`` would be silently ignored on the hybrid track.
        """
        if not target.description:
            target.description = overrides.description
        if not target.author:
            target.author = overrides.author
        if not target.icon:
            target.icon = overrides.icon
        if not target.homepage:
            target.homepage = overrides.homepage
        for field in ("email", "repository", "docs"):
            if not getattr(target, field, ""):
                setattr(target, field, getattr(overrides, field, ""))
        if not target.tags:
            target.tags = overrides.tags
        if not target.activation_events:
            target.activation_events = overrides.activation_events
        if not target.permissions:
            target.permissions = overrides.permissions
        if not target.engines:
            target.engines = overrides.engines
        if not target.contributes:
            target.contributes = overrides.contributes
        if not target.tools_meta:
            target.tools_meta = overrides.tools_meta
        if not target.danger_levels:
            target.danger_levels = overrides.danger_levels
        # Resource and UI-surface grants are fail-closed in the engine, so a
        # hybrid plugin that declares them only in ``encre-plugin.json`` must
        # still reach the push at activation or every ``context.resources``
        # call it makes is denied.
        if not target.resources:
            target.resources = overrides.resources
        if not target.ui_permissions:
            target.ui_permissions = overrides.ui_permissions
        # The code-side default for ``activation`` is "deferred"; a JSON
        # "core"/"manual" declaration must win over that default.
        if target.activation == "deferred":
            target.activation = overrides.activation
        # transport defaults to "local"; tier defaults to "user".
        if getattr(target, "transport", "local") == "local" and overrides.transport:
            target.transport = overrides.transport
        if not getattr(target, "endpoint", "") and overrides.endpoint:
            target.endpoint = overrides.endpoint
        if not getattr(target, "resource_limits", None):
            target.resource_limits = overrides.resource_limits
        if getattr(target, "tier", "user") == "user":
            target.tier = overrides.tier
        for field in ("provides_tools", "provides_skills", "provides_hooks", "provides_backends", "provides_mcp", "provides_agent"):
            if not getattr(target, field, None):
                setattr(target, field, getattr(overrides, field, None))

    def _load_plugin_from_file(self, path: Path, source: PluginSource) -> None:
        """Load a single-file plugin module from ``path``."""
        self._load_module_from_file(str(path), path.stem, source)

    def _load_plugin_package(self, package_path: str, source: PluginSource) -> None:
        """Import a plugin package via its ``__init__.py`` and extract the plugin."""
        try:
            spec = importlib.util.spec_from_file_location(
                f"yim_plugin_{Path(package_path).stem}",
                os.path.join(package_path, "__init__.py"),
            )
            if spec is None or spec.loader is None:
                return
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            self._extract_plugin(module, Path(package_path).stem, source)
        except Exception as e:
            logger.warning(f"Failed to load plugin package '{package_path}': {e}")
            self._failed[Path(package_path).stem] = str(e)

    def _load_module_from_file(self, filepath: str, name: str, source: PluginSource) -> None:
        """Load an arbitrary module file and extract any plugin it defines."""
        try:
            spec = importlib.util.spec_from_file_location(f"yim_plugin_{name}", filepath)
            if spec is None or spec.loader is None:
                return
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            self._extract_plugin(module, name, source)
        except Exception as e:
            logger.warning(f"Failed to load plugin '{name}' from '{filepath}': {e}")
            self._failed[name] = str(e)

    def _extract_plugin(self, module: Any, name: str, source: PluginSource) -> None:
        """Find a plugin inside a loaded module and register it.

        Tries, in order: a subclass of :class:`EncrePlugin`, a plugin
        instance attribute, and finally a ``create_plugin()`` factory.
        """
        # Look for a class that extends EncrePlugin
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, type) and issubclass(attr, EncrePlugin) and attr is not EncrePlugin:
                plugin = attr()
                plugin.manifest.source = source
                self.register(plugin)
                logger.info(f"Loaded plugin '{plugin.manifest.name}' from {source.value}/{name}")
                return
        # Look for a plugin instance
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, EncrePlugin):
                attr.manifest.source = source
                self.register(attr)
                return
        # Look for a create_plugin() factory
        if hasattr(module, "create_plugin"):
            try:
                plugin = module.create_plugin()
                if isinstance(plugin, EncrePlugin):
                    plugin.manifest.source = source
                    self.register(plugin)
                    return
            except Exception as e:
                # A failing factory is a real plugin failure — surface it
                # instead of silently dropping the plugin.
                logger.warning(f"create_plugin() failed for plugin '{name}': {e}")
                self._failed[name] = str(e)

    # ── Lifecycle ────────────────────────────────────────────────────

    def pending_plugins(self) -> list[dict[str, Any]]:
        """Return third-party plugins awaiting user confirmation (PENDING).

        Each entry carries name/version/description/tier/source/permissions/
        transport — the payload the frontend renders in the
        activation-confirmation popup (dynamic authorisation).
        """
        out: list[dict[str, Any]] = []
        for name, manifest in self._manifests.items():
            if self._states.get(name) is not PluginState.PENDING:
                continue
            out.append(
                {
                    "name": name,
                    "version": manifest.version,
                    "description": manifest.description,
                    "author": manifest.author,
                    "tier": getattr(manifest, "tier", "user"),
                    "source": manifest.source.value,
                    "permissions": list(manifest.permissions),
                    "transport": getattr(manifest, "transport", "local"),
                    "state": PluginState.PENDING.value,
                }
            )
        return out

    def confirm_activation(self, name: str, host: dict[str, Any] | None = None) -> bool:
        """Import and activate a pending third-party plugin after approval.

        This is the *only* path that executes a third-party plugin's code:
        discovery registers inert shells (PENDING) and leaves the module
        unimported.  For EA packages the integrity hash is re-verified
        here, so tampering between discovery and user approval is caught.

        Returns the :meth:`activate` result (``False`` when the import or
        activation failed — the error is recorded in :attr:`_failed`).
        """
        pending = self._pending.pop(name, None)
        if pending is None:
            # Not pending (or already confirmed) — plain activation.
            return self.activate(name, host=host)
        try:
            plugin = pending["loader"]()
        except BaseException as exc:
            if is_fatal(exc):
                raise
            logger.error("Failed to import pending plugin '%s': %s", name, exc)
            self._failed[name] = str(exc)
            self._states[name] = PluginState.FAILED
            return False
        if not isinstance(plugin, EncrePlugin):
            logger.warning("Pending plugin '%s' did not return an EncrePlugin instance", name)
            self._failed[name] = "factory did not return an EncrePlugin"
            self._states[name] = PluginState.FAILED
            return False
        pkg = pending.get("pkg")
        if pkg is not None:
            self._finalize_ea_plugin(plugin, pkg)
        else:
            plugin.manifest.source = PluginSource.INSTALLED
            self.register(plugin)
        if self._plugins.get(name) is None:
            return False
        logger.info("Pending plugin '%s' confirmed by user; activating", name)
        return self.activate(name, host=host)

    def deny_activation(self, name: str) -> bool:
        """Drop a pending third-party plugin without ever importing its code.

        The inert shell is removed from every registry structure; the
        plugin can re-appear (PENDING again) after a later re-discovery.
        """
        if self._pending.pop(name, None) is None:
            return False
        self._plugins.pop(name, None)
        self._manifests.pop(name, None)
        self._states.pop(name, None)
        self._failed.pop(name, None)
        self._lazy_registered.discard(name)
        self._invalidate_cache()
        logger.info("Pending plugin '%s' denied by user (code never imported)", name)
        return True

    def _push_tool_danger_levels(self, manifest: Any) -> None:
        """Push the plugin's declared per-tool danger levels into the Rust
        permission engine.

        The engine's decision pipeline consults this dynamic table for the
        unconfigured default of plugin tools.  Direct native call: the
        compiled extension is a mandatory runtime component and the policy
        table lives inside the engine, not in Python.
        """
        levels = getattr(manifest, "danger_levels", None)
        if not levels:
            return
        from encre import native as _native

        _native.permission_set_tool_danger_levels(dict(levels))

    def _push_plugin_resources(self, manifest: Any) -> None:
        """Push the plugin's manifest-declared resource grants into the Rust
        permission engine and record the undo.

        Fail-closed: only what ``[tool.ea.resources]`` declares (plus the
        plugin's own package directory, implicitly granted for read and
        write) is allowed for this plugin.  The plugin's controlled
        ``net`` / ``fs`` / ``system`` context APIs consult the engine before
        every operation, so the grants cannot be bypassed via Python state.
        """
        name = str(getattr(manifest, "name", "") or "")
        if not name:
            return
        payload = manifest.resources_payload()
        from encre import native as _native

        _native.permission_set_plugin_resources(name, payload)
        self._record_undo(name, lambda: self._remove_plugin_resources(name))

    def _push_plugin_ui(self, manifest: Any) -> None:
        """Push the plugin's frontend surface grants into the engine.

        Trusted tiers (bundled / mandatory, i.e. shipped with the package)
        default to allow-everything; every third-party tier defaults to
        deny-everything and must declare ``[tool.ea.ui_permissions]``.  The
        WS layer consults the engine before sending any plugin UI data, so
        ungranted surfaces never reach the renderer.
        """
        name = str(getattr(manifest, "name", "") or "")
        if not name:
            return
        source = getattr(manifest, "source", None)
        trusted = str(getattr(source, "value", source)) in ("bundled", "mandatory")
        from encre import native as _native

        _native.permission_set_plugin_ui(name, manifest.ui_permissions_payload(), trusted)
        self._record_undo(name, lambda: self._remove_plugin_ui(name))

    def _remove_plugin_ui(self, name: str) -> None:
        from encre import native as _native

        _native.permission_remove_plugin_ui(name)

    def _remove_plugin_resources(self, name: str) -> None:
        from encre import native as _native

        _native.permission_remove_plugin_resources(name)

    def _dependency_problem(self, manifest: Any) -> str:
        """Return a blocking reason for an unsatisfied hard dependency.

        Dependencies are now a contract, not a hint: missing/inactive
        plugins, version mismatches and malformed constraints reject
        activation.  Set ``ENCRE_PLUGIN_ALLOW_BROKEN_DEPS=1`` only for
        development recovery.
        """
        if os.environ.get("ENCRE_PLUGIN_ALLOW_BROKEN_DEPS", "0") == "1":
            return ""
        for raw in getattr(manifest, "dependencies", None) or []:
            parsed = _parse_dependency(raw)
            if parsed is None:
                return f"malformed dependency requirement: {raw!r}"
            dep_name, op, wanted = parsed
            dep = self._manifests.get(dep_name)
            if dep is None:
                return f"requires missing plugin '{dep_name}'"
            if self._states.get(dep_name) != PluginState.ACTIVATED:
                return f"requires plugin '{dep_name}', which is not activated"
            if op and not _dependency_satisfied(op, wanted, dep.version):
                return f"requires '{dep_name}{op}{wanted}', running {dep.version}"
        return ""

    def _active_dependents(self, name: str) -> list[str]:
        """Active plugins whose hard dependency is *name*."""
        out: list[str] = []
        for other, manifest in self._manifests.items():
            if other == name or self._states.get(other) != PluginState.ACTIVATED:
                continue
            for raw in getattr(manifest, "dependencies", None) or []:
                parsed = _parse_dependency(raw)
                if parsed and parsed[0] == name:
                    out.append(other)
                    break
        return sorted(out)

    def activate(self, name: str, host: dict[str, Any] | None = None) -> bool:
        """Activate a registered plugin, invoking its ``on_activate`` hook.

        ``host`` optionally maps capability keys (``tools``/``backends``/
        ``loop``/``events``/``permissions``/``commands``/``sub_agents``/
        ``templates``) to the embedding application's registry objects; a
        :class:`~encre.plugins.context.PluginContext` is built from it and
        passed to ``on_activate``.  All side effects the plugin performs
        through the context are recorded as an undo stack (Task 9.2).

        Distributed plugins (``manifest.transport`` = ``process`` or
        ``remote``) are activated through a :class:`PluginTransport`:
        the plugin code runs in a subprocess or at a remote endpoint,
        and tool calls are transparently proxied.
        """
        # Serialise concurrent activations of the same plugin (see
        # ``_activation_locks``); the body runs under the per-name lock.
        with self._activation_lock(name):
            return self._activate_locked(name, host)

    def _activate_locked(self, name: str, host: dict[str, Any] | None) -> bool:
        """Activation body, serialised per plugin by :meth:`activate`."""
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        if name in self._pending:
            # Dynamic authorisation: third-party plugins must be confirmed
            # by the user (confirm_activation) before their code runs.
            logger.warning(
                "Plugin '%s' is pending user confirmation; activation refused (use confirm_activation)",
                name,
            )
            return False
        if self.is_plugin_disabled(name):
            # The installed.json ledger is authoritative: a disabled plugin
            # must not come alive through the manual / event-driven paths
            # either (only set_plugin_enabled flips it back).
            logger.warning("Plugin '%s' is disabled; activation refused", name)
            return False
        if self._states.get(name) == PluginState.ACTIVATED:
            return True
        manifest = plugin.manifest
        # Hard dependency contract: no silent half-working plugins.
        dep_problem = self._dependency_problem(manifest)
        if dep_problem:
            logger.error("Plugin '%s' rejected: %s", name, dep_problem)
            self._failed[name] = dep_problem
            self._states[name] = PluginState.FAILED
            return False
        tier = str(getattr(manifest, "tier", "user") or "user")
        trusted = tier in ("mandatory", "system-default")
        transport_mode = getattr(manifest, "transport", "local")
        # Strong-isolation default: third-party plugins run out of process
        # unless they explicitly opt into in-process execution.  Trusted
        # tiers ship with the application and keep the fast local path.
        if transport_mode == "local" and not trusted:
            if getattr(manifest, "allow_local", False):
                logger.warning(
                    "Plugin '%s' opted into in-process execution (allow_local=True); "
                    "it is not isolated from the host process",
                    name,
                )
            elif getattr(manifest, "pkg_dir", "") or getattr(manifest, "factory", ""):
                transport_mode = "process"
            else:
                # Registry-only manifest without a package on disk: there is
                # no worker to spawn, so keep local and say so.  Real
                # installed plugins always carry pkg_dir.
                logger.warning(
                    "Plugin '%s' has no package directory; running in-process without isolation",
                    name,
                )
        if transport_mode != "local":
            pkg_dir_str = getattr(manifest, "pkg_dir", "") or ""
            pkg_dir = Path(pkg_dir_str) if pkg_dir_str else Path(".")
            transport = create_transport(manifest, plugin=plugin, pkg_dir=pkg_dir, mode=transport_mode)
            try:
                if not transport.activate():
                    logger.error("Transport activation failed for '%s'", name)
                    self._failed[name] = "transport activation failed"
                    self._states[name] = PluginState.FAILED
                    return False
                self._transports[name] = transport
                # Declarative manifest entries are host-side data and remain
                # available to process plugins; code-only surfaces cannot
                # cross the boundary and are rejected inside activate().
                self._register_manifest_contributions(name, manifest, host)
                self._states[name] = PluginState.ACTIVATED
                self._lazy_registered.discard(name)
                self._invalidate_cache()
                self._push_tool_danger_levels(manifest)
                self._push_plugin_resources(manifest)
                self._push_plugin_ui(manifest)
                return True
            except BaseException as exc:
                if is_fatal(exc):
                    raise
                logger.error("Transport activation error for '%s': %s", name, exc)
                self._failed[name] = str(exc)
                self._states[name] = PluginState.FAILED
                return False
        context = _build_context(name, host, undo_sink=self._record_undo, manifest=plugin.manifest)
        try:
            plugin.on_activate(context)
            self._register_manifest_contributions(name, manifest, host)
            self._states[name] = PluginState.ACTIVATED
            self._lazy_registered.discard(name)
            self._invalidate_cache()
            self._push_tool_danger_levels(manifest)
            self._push_plugin_resources(manifest)
            self._push_plugin_ui(manifest)
            return True
        except BaseException as e:
            if is_fatal(e):
                raise
            logger.error(f"Failed to activate plugin '{name}': {e}")
            self._failed[name] = str(e)
            self._states[name] = PluginState.FAILED
            return False

    _UI_INLINE_MAX = 512 * 1024

    def _materialize_ui_spec(self, manifest: Any, kind: str, spec: dict[str, Any], name: str) -> dict[str, Any] | None:
        """Resolve file-backed UI specs into the inline shape the wire uses.

        The CLI compiles a plugin's frontend to ``ui/plugin.js`` and a manifest
        declares it as ``{"kind": "js_module", "spec": {"path": "ui/plugin.js"}}``.
        Reading the file here keeps the renderer wire format unchanged while
        letting plugins ship compiled assets instead of embedding source in
        JSON.  Paths are confined to the plugin package directory.
        """
        base_raw = str(getattr(manifest, "pkg_dir", "") or "")
        if not base_raw:
            return spec
        base = Path(base_raw).resolve()

        def read_asset(rel: Any) -> str | None:
            if not isinstance(rel, str) or not rel.strip():
                return None
            target = (base / rel).resolve()
            if target != base and base not in target.parents:
                logger.warning("Plugin '%s' UI asset escapes its package: %s", name, rel)
                return None
            try:
                if not target.is_file() or target.stat().st_size > self._UI_INLINE_MAX:
                    logger.warning("Plugin '%s' UI asset missing or too large: %s", name, rel)
                    return None
                return target.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("Plugin '%s' UI asset unreadable (%s): %s", name, rel, exc)
                return None

        if kind == "js_module" and not spec.get("code"):
            code = read_asset(spec.get("path"))
            if code is None:
                return None
            return {**spec, "code": code}
        if kind == "ui_override" and not spec.get("template"):
            template = read_asset(spec.get("template_path"))
            if template is None:
                return None
            return {**spec, "template": template}
        return spec

    def _register_manifest_contributions(self, name: str, manifest: Any, host: dict[str, Any] | None) -> None:
        """Register declarative ``contributes`` entries from a manifest.

        Declaration format (plugin.json, mirrored into ``[tool.ea]``):

            "contributes": {
                "ui_contributions": [
                    {"kind": "theme", "spec": {"css": ":root{--bg:#000}"}},
                    {"kind": "settings", "spec": {"page": "general", "rows": [...]}}
                ],
                "commands": [{"name": "hello", "description": "..."}],
                "templates": [{"name": "greet", "template": "Hi {name}"}]
            }

        Each entry goes through the same host surface as the Python API, so
        undo on deactivate and hot reload behave identically.  Commands and
        templates previously had no registration path at all (declared
        entries were silently dropped); they now go through the
        ``commands`` / ``templates`` host surfaces, gated by the same
        permission tokens the Python API enforces.
        """
        if not isinstance(host, dict):
            return
        contributes = getattr(manifest, "contributes", None)
        if not isinstance(contributes, dict):
            return
        trusted = getattr(manifest, "tier", "user") in ("mandatory", "system-default")
        permissions = list(getattr(manifest, "permissions", None) or [])

        contrib_host = host.get("ui_contributions")
        entries = contributes.get("ui_contributions")
        if entries and not trusted and not _permission_granted("ui_contributions", permissions):
            logger.warning(
                "Plugin '%s' declares ui_contributions without the 'ui:register' permission; skipped",
                name,
            )
            entries = None
        if contrib_host is not None and isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                kind = str(entry.get("kind", "")).strip()
                spec = entry.get("spec")
                if not kind or not isinstance(spec, dict):
                    logger.warning("Plugin '%s' shipped a malformed ui_contributions entry", name)
                    continue
                try:
                    spec = self._materialize_ui_spec(manifest, kind, spec, name)
                    if spec is None:
                        continue
                    undo = contrib_host.plugin_register_contribution(kind, spec, source=name)
                    self._record_undo(name, undo)
                except Exception as exc:
                    logger.warning("Contribution registration failed for '%s': %s", name, exc)

        # Declarative iframe providers (``contributes.ui_providers``).  The
        # provider object only crosses as data; the renderer mounts it in an
        # isolated sandbox frame.
        provider_host = host.get("ui_providers")
        provider_entries = contributes.get("ui_providers")
        if provider_entries and not trusted and not _permission_granted("ui_providers", permissions):
            logger.warning(
                "Plugin '%s' declares ui_providers without the 'ui:register' permission; skipped",
                name,
            )
            provider_entries = None
        if provider_host is not None and isinstance(provider_entries, list):
            for entry in provider_entries:
                if not isinstance(entry, dict):
                    continue
                provider = SimpleNamespace(
                    name=str(entry.get("name", "") or ""),
                    title=str(entry.get("title", "") or entry.get("name", "") or ""),
                    description=str(entry.get("description", "") or ""),
                    entry_point=str(entry.get("entry_point", "") or ""),
                    mount_point=str(entry.get("mount_point", "panel") or "panel"),
                )
                if not provider.name or not provider.entry_point:
                    logger.warning("Plugin '%s' shipped a malformed ui_providers entry", name)
                    continue
                try:
                    undo = provider_host.plugin_register_ui_provider(provider, source=name)
                    self._record_undo(name, undo)
                except Exception as exc:
                    logger.warning("UI provider registration failed for '%s': %s", name, exc)

        cmd_host = host.get("commands")
        entries = contributes.get("commands")
        if cmd_host is not None and isinstance(entries, list):
            if not trusted and not _permission_granted("commands", permissions):
                logger.warning(
                    "Plugin '%s' declares commands without the 'commands:register' permission; skipped",
                    name,
                )
            else:
                for cmd in entries:
                    if not isinstance(cmd, dict) or not str(cmd.get("name", "")).strip():
                        logger.warning("Plugin '%s' shipped a malformed command entry", name)
                        continue
                    try:
                        undo = cmd_host.plugin_register_command(dict(cmd), source=name)
                        self._record_undo(name, undo)
                    except Exception as exc:
                        logger.warning("Command registration failed for '%s': %s", name, exc)

        tpl_host = host.get("templates")
        entries = contributes.get("templates")
        if tpl_host is not None and isinstance(entries, list):
            if not trusted and not _permission_granted("templates", permissions):
                logger.warning(
                    "Plugin '%s' declares templates without the 'templates:register' permission; skipped",
                    name,
                )
            else:
                for tpl in entries:
                    if not isinstance(tpl, dict) or not str(tpl.get("name", "")).strip():
                        logger.warning("Plugin '%s' shipped a malformed template entry", name)
                        continue
                    try:
                        undo = tpl_host.plugin_register_template(str(tpl["name"]), tpl.get("template", ""), source=name)
                        self._record_undo(name, undo)
                    except Exception as exc:
                        logger.warning("Template registration failed for '%s': %s", name, exc)

        # Declarative hook registrations (``contributes.hooks`` mirrored
        # onto ``provides_hooks``).  The handler reference is a
        # ``"module:func"`` string imported at activation; the resolved
        # descriptors live in ``_decl_hooks`` and are consumed by
        # ``get_all_hooks()`` when the agent wires the arbitration chain.
        transported_untrusted = not trusted and name in self._transports
        entries = list(getattr(manifest, "provides_hooks", None) or [])
        if transported_untrusted and entries:
            logger.warning(
                "Plugin '%s' runs out of process; declarative hooks require in-process code and were skipped",
                name,
            )
            entries = []
        if entries:
            if not trusted and not _permission_granted("loop", permissions):
                logger.warning(
                    "Plugin '%s' declares hooks without the 'loop:intercept' permission; skipped",
                    name,
                )
            else:
                resolved: list[dict[str, Any]] = []
                for hook in entries:
                    if not isinstance(hook, dict):
                        continue
                    handler_ref = str(hook.get("handler", "")).strip()
                    event = str(hook.get("event", "")).strip()
                    if not handler_ref or ":" not in handler_ref:
                        logger.warning("Plugin '%s' shipped a malformed hook handler ref %r", name, handler_ref)
                        continue
                    try:
                        module_name, _, attr_path = handler_ref.partition(":")
                        obj: Any = importlib.import_module(module_name)
                        for part in attr_path.split("."):
                            obj = getattr(obj, part)
                        handler = obj
                    except Exception as exc:
                        logger.warning("Hook handler import failed for '%s' (%s): %s", name, handler_ref, exc)
                        continue
                    resolved.append(
                        {
                            "event": event,
                            "handler": handler,
                            "handler_ref": handler_ref,
                            "priority": hook.get("priority", 100),
                            "timeout": hook.get("timeout", 5),
                            "source": name,
                        }
                    )
                if resolved:
                    self._decl_hooks[name] = resolved
                    self._invalidate_cache()

        # Declarative backend classes (``contributes.backends`` is a
        # ``{name: "module:Class"}`` mapping).  Resolved classes are
        # surfaced through ``get_all_backends()`` so the agent's
        # ``register_backend`` pass picks them up like method-provided ones.
        backends_map = contributes.get("backends")
        if transported_untrusted and isinstance(backends_map, dict) and backends_map:
            logger.warning(
                "Plugin '%s' runs out of process; declarative backends require in-process imports and were skipped",
                name,
            )
            backends_map = {}
        if isinstance(backends_map, dict) and backends_map:
            if not trusted and not _permission_granted("backends", permissions):
                logger.warning(
                    "Plugin '%s' declares backends without the 'backends:register' permission; skipped",
                    name,
                )
            else:
                resolved_backends: dict[str, type[Any]] = {}
                for backend_name, class_ref in backends_map.items():
                    class_ref_str = str(class_ref)
                    if not class_ref_str or ":" not in class_ref_str:
                        logger.warning("Plugin '%s' shipped a malformed backend class ref %r", name, class_ref_str)
                        continue
                    try:
                        module_name, _, attr_path = class_ref_str.partition(":")
                        obj: Any = importlib.import_module(module_name)
                        for part in attr_path.split("."):
                            obj = getattr(obj, part)
                        backend_cls = obj
                        if not isinstance(backend_cls, type):
                            raise TypeError("not a class")
                        resolved_backends[str(backend_name)] = backend_cls
                    except Exception as exc:
                        logger.warning("Backend class import failed for '%s' (%s): %s", name, class_ref_str, exc)
                if resolved_backends:
                    self._decl_backends[name] = resolved_backends
                    self._invalidate_cache()

    def _invalidate_cache(self) -> None:
        """Clear aggregated caches so the next call rebuilds them."""
        self._tool_cache = None
        self._core_tool_cache = None
        self._deferred_tool_cache = None
        self._resolved_tools = None
        self._skill_cache = None
        self._hook_cache = None
        self._backend_cache = None
        self._context_cache = None
        self._ui_cache = None
        self._gateway_cache = None
        self._memory_cache = None
        self._tool_meta_cache = None
        self._owner_map = None

    def _record_undo(self, plugin_name: str, undo: Any) -> None:
        """Push a plugin side-effect undo onto its LIFO rollback stack."""
        self._undo_stack.setdefault(plugin_name, []).append(undo)

    def deactivate(self, name: str) -> bool:
        """Deactivate a plugin: LIFO-undo side effects, then its hook.

        Every tier deactivates the same way — the historical ``mandatory``
        un-deactivatable privilege is retired (``tier`` grants no special
        treatment anymore).
        """
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        if self._states.get(name) != PluginState.ACTIVATED:
            return True
        # Engine-side first: flip the plugin's tool ownership to inactive
        # so its tools are denied immediately (zero window), before the
        # slower LIFO undo removes them from the registries.
        try:
            from encre import native as _native

            _native.permission_set_plugin_tools_active(name, False)
        except Exception as exc:
            if is_fatal(exc):
                raise
            logger.warning("Engine tool-ownership flip failed for '%s'", name, exc_info=True)
        # Close transport for distributed plugins
        transport = self._transports.pop(name, None)
        if transport is not None:
            try:
                transport.deactivate()
                transport.close()
            except BaseException as exc:
                if is_fatal(exc):
                    raise
                logger.warning("Transport close failed for '%s'", name, exc_info=True)
        try:
            for undo in reversed(self._undo_stack.pop(name, [])):
                try:
                    undo()
                except BaseException as exc:
                    if is_fatal(exc):
                        raise
                    logger.warning("Undo failed while deactivating '%s'", name, exc_info=True)
            plugin.on_deactivate()
            if plugin.manifest.activation_events:
                # Event-declared plugins return to the lazy pool so a later
                # event can re-activate them.  Without this they would sit
                # in limbo after a deactivate: excluded from every
                # auto-activation path AND absent from the pool the event
                # dispatcher consults — "uninstalled" but never restorable.
                self._lazy_registered.add(name)
                self._states[name] = PluginState.LAZY
            else:
                self._states[name] = PluginState.DISCOVERED
            self._invalidate_cache()
            # Hard dependencies cascade: leaving a dependent active would
            # guarantee a broken runtime, so deactivate it as well.
            for child in self._active_dependents(name):
                logger.warning(
                    "Deactivating '%s' because its dependency '%s' was deactivated",
                    child,
                    name,
                )
                try:
                    self.deactivate(child)
                except Exception as exc:
                    logger.error("Cascade deactivation of '%s' failed: %s", child, exc)
            return True
        except Exception as e:
            logger.error(f"Error deactivating plugin '{name}': {e}")
            # Partial rollback: the plugin must not be trusted as active or
            # cleanly deactivated — mark it FAILED so the state is visible.
            self._states[name] = PluginState.FAILED
            self._failed[name] = f"deactivation error: {e}"
            return False

    def emit_activation_event(self, event: str, host: dict[str, Any] | None = None) -> list[str]:
        """Fire an activation event; lazily load+activate matching plugins.

        Matching supports ``*`` (any event), exact names, and trailing
        wildcards (``category:*`` / ``prefix.*``).  ``host`` is the
        capability mapping forwarded to :meth:`activate`.  Returns the
        names of plugins activated by this event.
        """
        activated_now: list[str] = []
        # Already-instantiated dormant plugins (EA packages, hybrid plugins
        # whose declarative shell was replaced by real code, etc.): the
        # instance is registered, only activation was deferred.
        for name in list(self._lazy_registered):
            manifest = self._manifests.get(name)
            if manifest is None or not _event_matches(event, manifest.activation_events):
                continue
            if name in self._lazy:
                # Directory lazy plugins live in BOTH pools (the shell is a
                # registered instance AND holds deferred code).  The
                # directory loop below must own them, otherwise the plugin
                # activates twice and every manifest contribution (theme,
                # commands, ...) is registered in duplicate.
                continue
            if self.is_plugin_disabled(name):
                continue
            self._lazy_registered.discard(name)
            if self.activate(name, host=host):
                activated_now.append(name)
                logger.info("Lazy plugin '{}' activated on event '{}'", name, event)
        for name, (plugin_dir, source, manifest) in list(self._lazy.items()):
            if not _event_matches(event, manifest.activation_events):
                continue
            self._lazy.pop(name, None)
            self._states.pop(name, None)
            # Import the deferred code module, then merge its contributions.
            # Drop the declarative shell first so the real plugin instance
            # can replace it regardless of source priority.
            self._plugins.pop(name, None)
            self._manifests.pop(name, None)
            plugin_file = plugin_dir / "plugin.py"
            if plugin_file.exists():
                before = set(self._plugins)
                self._load_module_from_file(str(plugin_file), plugin_dir.stem, source)
                for new_name in set(self._plugins) - before:
                    merged = self._plugins[new_name]
                    self._merge_manifest(merged.manifest, manifest)
                    if isinstance(merged, _DeclarativePlugin):
                        continue
                    # Wrap the code plugin with its JSON declarations so
                    # manifest-only contributions (skills above all — they
                    # have no code-side registration path) survive the
                    # shell replacement.
                    self._plugins[new_name] = _HybridPlugin(merged, manifest, plugin_dir)
            if name not in self._plugins:
                # Code extraction failed (no create_plugin()/subclass found,
                # import error, wrong factory name — e.g. a typo like
                # ``get_plugin_class``).  Re-register the declarative shell
                # instead of letting the plugin silently evaporate: without
                # this, the shell was popped first and a code-loading
                # failure left the plugin absent from every list with no
                # FAILED state and no way to recover short of a restart.
                logger.warning(
                    "Lazy code load failed for plugin '{}'; falling back to its declarative shell",
                    name,
                )
                self.register(_DeclarativePlugin(manifest, plugin_dir))
            if self.activate(name, host=host):
                activated_now.append(name)
                logger.info("Lazy plugin '{}' activated on event '{}'", name, event)
        return activated_now

    def activate_all(self, host: dict[str, Any] | None = None) -> dict[str, bool]:
        """Activate every registered plugin; return per-name success flags.

        ``host`` optionally maps capability keys to the embedding
        application's registry objects; a :class:`PluginContext` is built
        from it and passed to each plugin's ``on_activate``.

        Activation respects declared ``dependencies`` (topologically
        grouped into waves, dependencies first).  Plugins inside one wave
        are mutually independent and activate **concurrently** — this is the
        dominant startup win when process-transport plugins each spawn a
        worker subprocess.  Cycles and unknown dependency names are logged
        and the affected plugins fall back to registration order.
        """
        results: dict[str, bool] = {}
        # Uniform rule: an installed plugin activates at startup unless the
        # installed.json ledger marks it disabled or its manifest opts out
        # via ``activation="manual"`` (explicit manage(plugin_activate) only).
        # The historical ``mandatory`` exemption is retired — the tier grants
        # no special treatment.
        # Plugins declaring ``activation_events`` never reach this path:
        # they are held in the LAZY pool at discovery time and activate on
        # their matching event instead.
        candidates = [
            name
            for name in self._plugins
            if getattr(self._manifests.get(name), "activation", "deferred") != "manual"
            and not (self._manifests.get(name) and self._manifests[name].activation_events)
            and not self.is_plugin_disabled(name)
        ]
        for wave in self._activation_waves(candidates):
            results.update(self._run_activation_wave(wave, host))
        return results

    def _activation_waves(self, names: list[str]) -> list[list[str]]:
        """Group candidate plugin *names* into dependency-safe waves.

        Level-BFS over ``manifest.dependencies``: wave 0 holds every
        candidate whose dependencies are absent, already activated, or
        outside the candidate set; wave N holds candidates whose
        dependencies all sit in earlier waves.  Plugins within one wave can
        be activated in parallel.  Cycles fall back to a final wave in
        registration order (with a warning) so nothing is silently skipped.
        """
        name_set = set(names)
        indegree: dict[str, int] = {}
        dependents: dict[str, list[str]] = {name: [] for name in names}
        for name in names:
            satisfied = 0
            for raw in self._manifests[name].dependencies or []:
                parsed = _parse_dependency(raw)
                if parsed is None:
                    continue
                dep = parsed[0]
                if dep == name or dep not in self._manifests:
                    continue  # unknown dependency names are rejected at activate()
                if self._states.get(dep) == PluginState.ACTIVATED:
                    continue  # already running: no need to wait for it
                if dep in name_set:
                    satisfied += 1
                    dependents[dep].append(name)
            indegree[name] = satisfied
        current = [name for name in names if indegree[name] == 0]
        waves: list[list[str]] = []
        placed: set[str] = set()
        while current:
            waves.append(current)
            placed.update(current)
            nxt: list[str] = []
            for name in current:
                for dependent in dependents[name]:
                    indegree[dependent] -= 1
                    if indegree[dependent] == 0:
                        nxt.append(dependent)
            current = nxt
        if len(placed) < len(names):
            cyclic = sorted(name for name in names if name not in placed)
            logger.warning(
                "Dependency cycle detected between plugins: %s; falling back to registration order for them",
                ", ".join(cyclic),
            )
            waves.append([name for name in names if name not in placed])
        return waves

    def _run_activation_wave(self, wave: list[str], host: dict[str, Any] | None) -> dict[str, bool]:
        """Activate every plugin in *wave* — concurrently when it has several.

        A single-member wave runs inline (no pool overhead).  Multi-member
        waves run in a bounded thread pool: subprocess spawns and blocking
        ``on_activate`` work release the GIL, so waves shrink real startup
        time roughly to their critical path.  Shared-state mutations inside
        :meth:`activate` are dict writes / list appends (GIL-atomic), and
        the Rust danger-level push is behind the engine's own write lock.
        """
        results: dict[str, bool] = {}
        if len(wave) == 1:
            name = wave[0]
            try:
                results[name] = self.activate(name, host=host)
            except Exception as exc:
                logger.error("Activation of plugin '%s' crashed: %s", name, exc)
                results[name] = False
            return results
        workers = min(len(wave), max(4, (os.cpu_count() or 4) * 2))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ea-activate") as pool:
            futures = {pool.submit(self.activate, name, host=host): name for name in wave}
            for future, name in futures.items():
                try:
                    results[name] = bool(future.result())
                except Exception as exc:
                    logger.error("Activation of plugin '%s' crashed: %s", name, exc)
                    results[name] = False
        return results

    async def aactivate_all(self) -> dict[str, bool]:
        """Async variant of :meth:`activate_all` (runs in a worker thread)."""
        return await asyncio.to_thread(self.activate_all)

    def deactivate_all(self) -> None:
        """Deactivate all currently active plugins."""
        for name in list(self.list_activated()):
            self.deactivate(name)

    def reload_plugin(self, name: str) -> bool:
        """Tear a plugin down so a later re-discovery can pick up new code.

        Deactivates the plugin if active, then drops its registration,
        manifest and undo stack.  Intended for the hot-reload path: the
        caller re-runs ``discover_all`` afterwards, which re-registers and
        re-activates the plugin from its updated sources.
        """
        was_active = self._states.get(name) == PluginState.ACTIVATED
        if was_active:
            self.deactivate(name)
        existed = self._plugins.pop(name, None) is not None
        self._manifests.pop(name, None)
        self._states.pop(name, None)
        self._failed.pop(name, None)
        self._undo_stack.pop(name, None)
        self._lazy_registered.discard(name)
        # A forced reload must re-import even when the on-disk stamp did not
        # change (e.g. a code change the cheap stamp cannot see); dropping the
        # snapshot entry makes the next discovery take the eager path.
        with contextlib.suppress(Exception):
            from encre.plugins.discovery_cache import invalidate as _invalidate_discovery

            _invalidate_discovery(name)
        self._invalidate_cache()
        return existed or was_active

    # ── Tool/Skill/Hook aggregation ───────────────────────────────

    def _gather_tool_candidates(self) -> list[tuple[PluginManifest, str, Any]]:
        """Every activated plugin's tools as ``(manifest, owner, tool)`` triples.

        Transport-backed plugins contribute :class:`RemoteToolProxy` wrappers
        built from the advertised metadata; local plugins contribute their live
        objects.  A tool failing :func:`_validate_plugin_tool` is dropped here
        with a warning, so a malformed plugin can neither put a blank name in
        the model-facing payload nor smuggle an unusable schema past it.
        """
        candidates: list[tuple[PluginManifest, str, Any]] = []
        for name in self.list_activated():
            manifest = self._manifests.get(name)
            if manifest is None:
                continue
            transport = self._transports.get(name)
            if transport is not None:
                metas = absorb(
                    transport.get_tool_metas,
                    default=(),
                    context=f"transport {name!r} get_tool_metas",
                )
                items: Any = [RemoteToolProxy(meta, transport) for meta in metas or ()]
            else:
                items = absorb(
                    self._plugins[name].get_tools,
                    default=(),
                    context=f"plugin {name!r} get_tools",
                )
            for tool in items or ():
                problem = _validate_plugin_tool(tool)
                if problem:
                    logger.warning("Plugin %r tool dropped: %s", name, problem)
                    continue
                candidates.append((manifest, name, tool))
        return candidates

    def _resolve_tools(self) -> list[tuple[PluginManifest, str, Any]]:
        """Collision-free ``(manifest, owner, tool)`` list in stable order.

        Memoised for the cache epoch; both tool selection and tool ownership
        read it so the name the model can call and the plugin the permission
        engine checks can never disagree.
        """
        if self._resolved_tools is not None:
            return self._resolved_tools
        winners: dict[str, tuple[int, int, tuple[PluginManifest, str, Any]]] = {}
        for order, (manifest, owner, tool) in enumerate(self._gather_tool_candidates()):
            key = (_tool_trust_rank(manifest), order)
            current = winners.get(tool.name)
            if current is None:
                winners[tool.name] = (key[0], key[1], (manifest, owner, tool))
            elif key < (current[0], current[1]):
                logger.warning(
                    "Tool name conflict %r: %r shadows %r (higher-trust source wins)",
                    tool.name, owner, current[2][1],
                )
                winners[tool.name] = (key[0], key[1], (manifest, owner, tool))
            else:
                logger.warning(
                    "Tool name conflict %r: %r is ignored, %r already provides it",
                    tool.name, owner, current[2][1],
                )
        self._resolved_tools = [winners[name][2] for name in sorted(winners, key=lambda k: winners[k][1])]
        return self._resolved_tools

    def _select_tools(self, activation: str | None) -> list[Any]:
        """Tools from activated plugins, optionally restricted to one tier.

        ``activation`` selects the manifest ``activation`` value to include
        (``"core"`` or ``"deferred"``); ``None`` spans both.
        """
        return [
            tool
            for manifest, _owner, tool in self._resolve_tools()
            if activation is None or manifest.activation == activation
        ]

    def get_core_tools(self) -> list[Any]:
        """Return tools from activated plugins declaring ``activation="core"``.

        The tier is a grouping label only: :meth:`get_all_tools` injects both
        tiers, so these are model-visible from the first turn either way.
        """
        if self._core_tool_cache is None:
            self._core_tool_cache = self._select_tools("core")
        return self._core_tool_cache

    def get_deferred_tools(self) -> list[Any]:
        """Return tools from activated plugins declaring ``activation="deferred"``.

        Kept for the manifest tier vocabulary: with register-as-activate the
        split is informational only — the agent injects every activated
        plugin's tools via :meth:`get_all_tools`, and all of them join the
        model-facing payload immediately.
        """
        if self._deferred_tool_cache is None:
            self._deferred_tool_cache = self._select_tools("deferred")
        return self._deferred_tool_cache

    def get_all_tools(self) -> list[Any]:
        """Return every tool from activated plugins, collisions resolved.

        This is the injection source for the agent's tool registry:
        registration IS activation, so every tool returned here becomes
        active and model-visible immediately.
        """
        if self._tool_cache is None:
            self._tool_cache = self._select_tools(None)
        return self._tool_cache

    def get_all_skills(self) -> list[Any]:
        """Return deduplicated skills from every activated plugin."""
        if self._skill_cache is not None:
            return self._skill_cache
        skills: list[Any] = []
        seen: set[str] = set()
        for name in self.list_activated():
            transport = self._transports.get(name)
            if transport is not None:
                for skill in absorb(transport.get_skill_metas, default=(), context=f"transport {name!r} get_skill_metas"):
                    skill_name = getattr(skill, "name", "") if not isinstance(skill, dict) else skill.get("name", "")
                    if skill_name and skill_name not in seen:
                        seen.add(skill_name)
                        skills.append(skill)
                continue
            plugin = self._plugins[name]
            for skill in absorb(plugin.get_skills, default=(), context=f"plugin {name!r} get_skills"):
                skill_name = getattr(skill, "name", "")
                if skill_name and skill_name not in seen:
                    seen.add(skill_name)
                    skills.append(skill)
        self._skill_cache = skills
        return skills

    def get_all_hooks(self) -> dict[str, list[dict[str, Any]]]:
        """Return event_type -> hook descriptors across activated plugins.

        Each descriptor dict carries the live ``handler`` callable plus
        registration metadata (``priority`` / ``timeout`` / ``source`` /
        ``handler_ref``).  Sources: the plugin's ``get_hooks()`` method
        (plain ``(event, handler)`` pairs get an empty metadata dict),
        transport-advertised hook metas, and declarative manifest
        ``provides_hooks`` entries resolved at activation.
        """
        if self._hook_cache is not None:
            return self._hook_cache
        hooks: dict[str, list[dict[str, Any]]] = {}
        for name in self.list_activated():
            transport = self._transports.get(name)
            if transport is not None:
                for event_type, hook_meta in absorb(transport.get_hook_metas, default=(), context=f"transport {name!r} get_hook_metas"):
                    handler = hook_meta.get("handler") if isinstance(hook_meta, dict) else hook_meta
                    if handler is not None:
                        desc = dict(hook_meta) if isinstance(hook_meta, dict) else {"handler": handler}
                        desc["handler"] = handler
                        desc.setdefault("source", name)
                        hooks.setdefault(event_type, []).append(desc)
                continue
            plugin = self._plugins[name]
            for event_type, handler in absorb(plugin.get_hooks, default=(), context=f"plugin {name!r} get_hooks"):
                hooks.setdefault(event_type, []).append({"handler": handler, "source": name})
        # Declarative manifest hooks (resolved handlers with priority /
        # timeout metadata), keyed per plugin so deactivation drops them.
        for name, descs in self._decl_hooks.items():
            if self._states.get(name) != PluginState.ACTIVATED:
                continue
            for desc in descs:
                hooks.setdefault(str(desc["event"]), []).append(dict(desc))
        self._hook_cache = hooks
        return hooks

    def get_all_backends(self) -> dict[str, type[Any]]:
        """Return name -> backend class mappings from activated plugins."""
        if self._backend_cache is not None:
            return self._backend_cache
        backends: dict[str, type[Any]] = {}
        for name in self.list_activated():
            transport = self._transports.get(name)
            if transport is not None:
                # Distributed plugins cannot register backend classes
                # (they live in a separate process/remote host)
                continue
            plugin = self._plugins[name]
            backends.update(absorb(plugin.get_backends, default=(), context=f"plugin {name!r} get_backends"))
        # Declarative manifest backends (``contributes.backends`` name ->
        # "module:Class", resolved at activation).
        for name, decl in self._decl_backends.items():
            if self._states.get(name) == PluginState.ACTIVATED:
                backends.update(decl)
        self._backend_cache = backends
        return backends

    def get_all_mcp_servers(self) -> list[dict[str, Any]]:
        """Merge MCP server configs from every activated plugin (dedup by name).

        Sources are the manifest's ``provides_mcp`` entries (declarative
        ``contributes.mcp_servers`` or ``[tool.ea]`` mirror).  Entries are
        injected into ``config.mcp_servers`` by the agent's plugin loader;
        the aggregation is intentionally uncached because the MCP set is
        reconciled on every plugin reload.
        """
        servers: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in self.list_activated():
            manifest = self._manifests.get(name)
            for entry in getattr(manifest, "provides_mcp", None) or []:
                if not isinstance(entry, dict):
                    continue
                srv_name = str(entry.get("name", ""))
                if srv_name and srv_name not in seen:
                    seen.add(srv_name)
                    servers.append(dict(entry))
        return servers

    def get_all_sub_agent_defs(self) -> list[dict[str, Any]]:
        """Merge sub-agent definitions from every activated plugin (dedup by name).

        Sources are the manifest's ``provides_agent`` entries.  The agent
        converts them into ``SubAgentConfig`` objects; plugin sub-agents
        run alongside the main agent without altering its identity.
        """
        defs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in self.list_activated():
            manifest = self._manifests.get(name)
            for entry in getattr(manifest, "provides_agent", None) or []:
                if not isinstance(entry, dict):
                    continue
                agent_name = str(entry.get("name", ""))
                if agent_name and agent_name not in seen:
                    seen.add(agent_name)
                    defs.append(dict(entry))
        return defs

    # S6-S9: Extension surface aggregation ─────────────────────────────

    def get_all_context_providers(self) -> list[Any]:
        """Return context providers from every activated plugin (S6).

        Each provider has ``name``, ``priority``, and ``provide(state)``
        that returns a list of message dicts injected into the agent
        context before each turn.
        """
        if self._context_cache is not None:
            return self._context_cache
        providers: list[Any] = []
        for name in self.list_activated():
            plugin = self._plugins[name]
            providers.extend(absorb(plugin.get_context_providers, default=(), context=f"plugin {name!r} get_context_providers"))
        self._context_cache = providers
        return providers

    def get_all_ui_providers(self) -> list[Any]:
        """Return UI providers from every activated plugin (S7).

        Each provider has ``name``, ``entry_point`` (JS module path),
        and ``mount_point`` (sidebar/panel/settings/chat-overlay).
        """
        if self._ui_cache is not None:
            return self._ui_cache
        providers: list[Any] = []
        for name in self.list_activated():
            plugin = self._plugins[name]
            providers.extend(absorb(plugin.get_ui_providers, default=(), context=f"plugin {name!r} get_ui_providers"))
        self._ui_cache = providers
        return providers

    def get_all_tool_meta(self) -> list[Any]:
        """Return the merged tool UI metadata catalog (v0.6+).

        Combines the built-in catalog from :mod:`encre.tools.tool_ui` with
        every activated plugin's declarations, plugins winning over builtins
        field-by-field.  A plugin tool's own ``ui=`` attribute is picked up
        here too, so presentation can travel next to the implementation just as
        it does for builtins — the plugin's explicit ``tools_meta`` still
        overrides it.  Each entry is ``{"name", "meta", "source"}`` and drives
        the frontend tool cards, so no tool name is hardcoded in the renderer.
        """
        if self._tool_meta_cache is not None:
            return self._tool_meta_cache
        contributions: dict[str, dict[str, Any]] = {}
        for _manifest, owner, tool in self._resolve_tools():
            ui = getattr(tool, "ui", None)
            if isinstance(ui, dict) and ui:
                contributions[tool.name] = {"name": tool.name, "meta": ui, "source": owner}
        for name in self.list_activated():
            declared = absorb(
                self._plugins[name].get_tool_meta,
                default=(),
                context=f"plugin {name!r} get_tool_meta",
            )
            for item in declared or ():
                if not isinstance(item, dict):
                    continue
                tool_name = str(item.get("name", ""))
                if not tool_name:
                    continue
                meta = dict(contributions.get(tool_name, {}).get("meta", {}))
                meta.update(item.get("meta") or {})
                contributions[tool_name] = {"name": tool_name, "meta": meta, "source": name}
        entries = _collect_tool_meta(
            plugin_entries=[contributions[key] for key in sorted(contributions)],
            include_builtin=True,
        )
        self._tool_meta_cache = entries
        return entries

    def get_all_gateway_adapters(self) -> list[Any]:
        """Return IM platform gateway adapters (S8).

        Each adapter implements ``connect()`` / ``disconnect()`` /
        ``send_message()`` / ``receive_message()``.
        """
        if self._gateway_cache is not None:
            return self._gateway_cache
        adapters: list[Any] = []
        for name in self.list_activated():
            plugin = self._plugins[name]
            adapters.extend(absorb(plugin.get_gateway_adapters, default=(), context=f"plugin {name!r} get_gateway_adapters"))
        self._gateway_cache = adapters
        return adapters

    def get_all_memory_stores(self) -> list[Any]:
        """Return alternative memory storage backends (S9).

        Each store implements ``store()`` / ``retrieve()`` / ``search()``
        / ``delete()``.
        """
        if self._memory_cache is not None:
            return self._memory_cache
        stores: list[Any] = []
        for name in self.list_activated():
            plugin = self._plugins[name]
            stores.extend(absorb(plugin.get_memory_stores, default=(), context=f"plugin {name!r} get_memory_stores"))
        self._memory_cache = stores
        return stores

    def close_all_transports(self) -> None:
        """Close every active distributed plugin transport."""
        for name, transport in list(self._transports.items()):
            try:
                transport.deactivate()
                transport.close()
            except Exception:
                logger.warning("Failed to close transport '%s'", name, exc_info=True)
        self._transports.clear()

    def reset(self) -> None:
        """Deactivate everything and clear all registry state."""
        self.deactivate_all()
        self.close_all_transports()
        self._plugins.clear()
        self._manifests.clear()
        self._states.clear()
        self._failed.clear()
        self._undo_stack.clear()
        self._lazy.clear()
        self._lazy_registered.clear()
        self._pending.clear()
        self._transports.clear()
        self._invalidate_cache()


def _event_matches(event: str, activation_events: list[str]) -> bool:
    """Return ``True`` when *event* satisfies any of ``activation_events``.

    Supported patterns: ``*`` (everything), exact names, and trailing
    wildcards — both ``category:*`` (colon form) and ``prefix.*`` (dot
    form, e.g. ``onCommand:demo.*`` matching ``onCommand:demo.run``).
    """
    for pattern in activation_events:
        if pattern == "*" or pattern == event:
            return True
        if pattern.endswith("*") and event.startswith(pattern[:-1]):
            return True
    return False


def _source_priority(source: PluginSource) -> int:
    """Return the discovery priority of a plugin source (higher overrides)."""
    _order = {
        PluginSource.BUNDLED: 0,
        PluginSource.MANDATORY: 1,
        PluginSource.INSTALLED: 1,
        PluginSource.PROJECT: 2,
        PluginSource.USER: 3,
    }
    # Higher number => discovered later => higher precedence on collision
    return _order.get(source, 1)


def _tool_trust_rank(manifest: PluginManifest) -> int:
    """Precedence for tool-name collisions — lower rank wins.

    A tool name is a single unprefixed key in the model-facing payload, so the
    resolution rule has to protect the core surface: an unrelated third-party
    plugin must not be able to shadow ``file_read`` just because its name
    sorts first.  :func:`_source_priority` ranks bundled lowest and user
    highest, so using it directly makes the most-trusted source win. Note this
    is deliberately the *opposite* of :meth:`PluginRegistry.register`, where a
    same-named local copy intentionally overrides the shipped one.
    """
    return _source_priority(manifest.source)


def _validate_plugin_tool(tool: Any) -> str:
    """Return ``""`` when *tool* can be exposed to the model, else the reason.

    Plugin tools reach the payload builder through bare ``getattr`` calls, so
    without this check an unnamed tool lands as a blank entry and a
    non-object ``input_schema`` is forwarded to the provider untouched.
    """
    name = getattr(tool, "name", None)
    if not isinstance(name, str) or not name.strip():
        return "has no usable 'name'"
    if not callable(getattr(tool, "execute", None)):
        return f"tool {name!r} has no callable 'execute'"
    if not isinstance(getattr(tool, "description", ""), str):
        return f"tool {name!r} has a non-string 'description'"
    schema = getattr(tool, "input_schema", None)
    if schema is None or not isinstance(schema, dict):
        return f"tool {name!r} has a non-dict 'input_schema'"
    if schema and schema.get("type") != "object":
        return f"tool {name!r} 'input_schema' must describe an object, got {schema.get('type')!r}"
    return ""
