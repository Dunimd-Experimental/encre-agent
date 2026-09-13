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
import importlib
import importlib.metadata
import importlib.util
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger
from encre.plugins.auto_installer import ensure_ea_tools_installed
from encre.plugins.context import build_context as _build_context
from encre.plugins.integrity import verify_integrity as _verify_integrity
from encre.plugins.manifest import (
    MANIFEST_FILENAME as _MANIFEST_FILENAME,
)
from encre.plugins.manifest import (
    DeclarativePlugin as _DeclarativePlugin,
)
from encre.plugins.manifest import (
    load_manifest as _load_manifest,
)
from encre.plugins.transport import (
    PluginTransport,
    RemoteToolProxy,
    create_transport,
)
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource

logger = get_logger("encre.plugins")

_ENTRY_POINT_GROUP = "encre.plugins"
_EA_ENTRY_POINT_GROUP = "ea.plugins"


def _tier_to_source(tier: str) -> PluginSource:
    """Map an EA package tier (from ``[tool.ea]`` metadata) to a plugin source.

    * ``mandatory``      -> MANDATORY  (auto-activated, cannot be uninstalled)
    * ``system-default`` -> BUNDLED    (ships with app, can be uninstalled)
    * anything else      -> INSTALLED  (user / third-party, pip-managed)
    """
    if tier == "mandatory":
        return PluginSource.MANDATORY
    if tier == "system-default":
        return PluginSource.BUNDLED
    return PluginSource.INSTALLED


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
        self._activated: set[str] = set()
        self._failed: dict[str, str] = {}
        # Reversible side-effect undo stacks (LIFO rollback on deactivate)
        self._undo_stack: dict[str, list] = {}
        # Lazy plugins: name -> (plugin_dir, source, manifest); code is
        # only imported when an activation event fires (Task 9.4)
        self._lazy: dict[str, tuple[Path, PluginSource, PluginManifest]] = {}
        # Distributed plugin transports (name -> PluginTransport).
        # Populated during activate() when manifest.transport != "local".
        self._transports: dict[str, PluginTransport] = {}
        # Tool metadata cache: avoids re-calling get_tools() / RPC on
        # every get_all_tools() invocation.
        self._tool_cache: list[Any] | None = None
        self._skill_cache: list[Any] | None = None
        self._hook_cache: dict[str, list[Any]] | None = None
        self._backend_cache: dict[str, type[Any]] | None = None
        # S6-S9: extension surface caches
        self._context_cache: list[Any] | None = None
        self._ui_cache: list[Any] | None = None
        self._gateway_cache: list[Any] | None = None
        self._memory_cache: list[Any] | None = None

    @property
    def count(self) -> int:
        return len(self._plugins)

    @property
    def active_count(self) -> int:
        return len(self._activated)

    def pending_lazy(self) -> list[str]:
        """Return names of plugins waiting for an activation event."""
        return sorted(self._lazy)

    def register(self, plugin: EncrePlugin) -> None:
        """Register a plugin instance directly."""
        name = plugin.manifest.name
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
            # Mandatory plugins always win regardless of discovery order.
            if getattr(existing.manifest, "tier", "user") == "mandatory":
                return
        self._plugins[name] = plugin
        self._manifests[name] = plugin.manifest

    def unregister(self, name: str) -> bool:
        """Remove a plugin from the registry and deactivate it if needed."""
        if name in self._activated:
            self.deactivate(name)
        removed = self._plugins.pop(name, None) is not None
        self._manifests.pop(name, None)
        self._failed.pop(name, None)
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
        return sorted(self._activated)

    def list_failed(self) -> dict[str, str]:
        """Return a mapping of plugin name to the error that caused load failure."""
        return dict(self._failed)

    # ── Discovery ────────────────────────────────────────────────鈹€鈹€鈹€

    def discover_all(self) -> int:
        """Run all discovery mechanisms. Returns count of newly found plugins."""
        before = len(self._plugins)
        # Make bundled EA packages importable (directory-driven, no pip needed)
        ensure_ea_tools_installed()
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

    def _activate_auto_plugins(self) -> None:
        """Auto-activate mandatory and system-default plugins.

        These ship with the app and must be available without any explicit
        activation step.  User-installed (third-party) plugins stay lazy and
        are activated on demand or via activation events.
        """
        for name, plugin in list(self._plugins.items()):
            if name in self._activated:
                continue
            tier = getattr(plugin.manifest, "tier", "user")
            if plugin.manifest.source == PluginSource.MANDATORY or tier in ("mandatory", "system-default"):
                self.activate(name)

    def _discover_ea_packages(self) -> None:
        """Discover bundled EA packages by scanning the ``ea_tools/`` tree.

        Each package directory is auto-scanned (see :mod:`encre.plugins.ea_scan`);
        its tier, taken from ``pyproject.toml`` ``[tool.ea]``, decides the plugin
        source.  This works identically in development and in PyInstaller frozen
        bundles — no package name is hardcoded here.

        The per-package module import is the dominant cost (hundreds of
        packages), so factories are imported concurrently in a bounded thread
        pool and registered sequentially afterwards to keep discovery order
        deterministic.  Importing distinct modules in parallel is safe: the
        interpreter serialises concurrent first-imports of the same shared
        dependency via its per-module import lock.
        """
        from encre.plugins.ea_scan import iter_ea_packages

        packages = [pkg for pkg in iter_ea_packages() if pkg.name not in self._plugins]
        if not packages:
            return

        workers = int(os.environ.get("ENCRE_PLUGIN_IMPORT_WORKERS", "0")) or min(32, max(4, (os.cpu_count() or 4) * 4))
        results: list[tuple[Any, Any]] = []
        if workers > 1 and len(packages) > 1:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ea-import") as pool:
                futures = [(pkg, pool.submit(self._instantiate_ea_plugin, pkg)) for pkg in packages]
                for pkg, future in futures:
                    try:
                        results.append((pkg, future.result()))
                    except Exception as e:
                        results.append((pkg, e))
        else:
            for pkg in packages:
                try:
                    results.append((pkg, self._instantiate_ea_plugin(pkg)))
                except Exception as e:
                    results.append((pkg, e))

        for pkg, outcome in results:
            if isinstance(outcome, BaseException):
                logger.warning(f"Failed to load EA package '{pkg.name}': {outcome}")
                self._failed[pkg.name] = str(outcome)
                continue
            plugin = outcome
            if not isinstance(plugin, EncrePlugin):
                logger.warning(f"EA package '{pkg.name}' did not return an EncrePlugin instance")
                continue
            plugin.manifest.source = _tier_to_source(pkg.tier)
            # Keep the tier on the manifest for uninstall / manager decisions.
            plugin.manifest.tier = pkg.tier
            # S5: verify code integrity for non-trusted tiers.
            if not _verify_integrity(plugin.manifest, pkg.pkg_dir):
                logger.warning(
                    "Integrity check failed for '%s'; skipping",
                    pkg.name,
                )
                self._failed[pkg.name] = "integrity verification failed"
                continue
            self.register(plugin)
            logger.info(
                f"Discovered EA plugin '{plugin.manifest.name}' (tier={pkg.tier}) "
                f"via directory scan of {pkg.pkg_dir}"
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
        try:
            entry_points = importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP)
        except TypeError:
            # Python < 3.12
            try:
                entry_points = importlib.metadata.entry_points().get(_ENTRY_POINT_GROUP, [])
            except Exception:
                return
        except Exception:
            return

        for ep in entry_points:
            try:
                plugin_factory = ep.load()
                plugin = plugin_factory()
                if not isinstance(plugin, EncrePlugin):
                    # Skip entry points that don't yield a valid plugin object
                    logger.warning(f"Entry point '{ep.name}' did not return a EncrePlugin instance")
                    continue
                plugin.manifest.source = PluginSource.INSTALLED
                self.register(plugin)
                logger.info(f"Discovered plugin '{plugin.manifest.name}' via entry point '{ep.name}'")
            except Exception as e:
                logger.warning(f"Failed to load plugin from entry point '{ep.name}': {e}")
                self._failed[ep.name] = str(e)

    def _discover_ea_entry_points(self) -> None:
        """Discover third-party EA plugins registered via the ``ea.plugins`` entry-point group.

        Only user-installed plugins are expected here — bundled (mandatory /
        system-default) EA packages are discovered by the directory scan in
        :meth:`_discover_ea_packages` and are skipped if already registered.
        """
        entry_points = []
        try:
            entry_points = list(importlib.metadata.entry_points(group=_EA_ENTRY_POINT_GROUP))
        except TypeError:
            # Python < 3.12
            try:
                entry_points = list(importlib.metadata.entry_points().get(_EA_ENTRY_POINT_GROUP, []))
            except Exception:
                entry_points = []
        except Exception:
            entry_points = []

        for ep in entry_points:
            try:
                plugin_factory = ep.load()
                plugin = plugin_factory()
                if not isinstance(plugin, EncrePlugin):
                    logger.warning(f"EA entry point '{ep.name}' did not return an EncrePlugin instance")
                    continue
                if plugin.manifest.name in self._plugins:
                    # Already registered via the directory scan (bundled) or a
                    # higher-priority source — do not overwrite.
                    continue
                plugin.manifest.source = _tier_to_source(getattr(plugin.manifest, "tier", "user"))
                self.register(plugin)
                logger.info(f"Discovered EA plugin '{plugin.manifest.name}' via entry point '{ep.name}'")
            except Exception as e:
                logger.warning(f"Failed to load EA plugin from entry point '{ep.name}': {e}")
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
        鈫?``plugin.py`` 鈫?``__init__.py``.  When the manifest declares
        ``activationEvents`` and a ``plugin.py`` exists, the code module is
        NOT imported at discovery time 鈥?the plugin registers lazily with
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
                    self._lazy[manifest.name] = (path, source, manifest)
                    self.register(_DeclarativePlugin(manifest, path))
                    logger.info("Lazy plugin '%s' registered (static contributions only)",
                                manifest.name)
                    return
                # Declarative-only track
                self.register(_DeclarativePlugin(manifest, path))
                logger.info("Declarative plugin '%s' loaded from %s/%s",
                            manifest.name, source.value, path.stem)
                return

        if plugin_file.exists():
            self._load_module_from_file(str(plugin_file), path.stem, source)
        elif init_file.exists():
            self._load_plugin_package(str(path), source)

    @staticmethod
    def _merge_manifest(target: PluginManifest, overrides: PluginManifest) -> None:
        """Fill empty fields of a code-built manifest from the JSON one."""
        if not target.description:
            target.description = overrides.description
        if not target.author:
            target.author = overrides.author
        if not target.activation_events:
            target.activation_events = overrides.activation_events
        if not target.permissions:
            target.permissions = overrides.permissions
        if not target.engines:
            target.engines = overrides.engines
        if not target.contributes:
            target.contributes = overrides.contributes

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
            except Exception:
                pass

    # ── Lifecycle ────────────────────────────────────────────────鈹€鈹€鈹€鈹€

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
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        if name in self._activated:
            return True
        manifest = plugin.manifest
        transport_mode = getattr(manifest, "transport", "local")
        if transport_mode != "local":
            pkg_dir = Path(getattr(manifest, "homepage", "")) or Path(".")
            transport = create_transport(manifest, plugin=plugin, pkg_dir=pkg_dir)
            try:
                if not transport.activate():
                    logger.error("Transport activation failed for '%s'", name)
                    self._failed[name] = "transport activation failed"
                    return False
                self._transports[name] = transport
                self._activated.add(name)
                self._invalidate_cache()
                return True
            except Exception as exc:
                logger.error("Transport activation error for '%s': %s", name, exc)
                self._failed[name] = str(exc)
                return False
        context = _build_context(
            name, host, undo_sink=self._record_undo, manifest=plugin.manifest
        )
        try:
            plugin.on_activate(context)
            self._activated.add(name)
            self._invalidate_cache()
            return True
        except Exception as e:
            logger.error(f"Failed to activate plugin '{name}': {e}")
            self._failed[name] = str(e)
            return False

    def _invalidate_cache(self) -> None:
        """Clear aggregated caches so the next call rebuilds them."""
        self._tool_cache = None
        self._skill_cache = None
        self._hook_cache = None
        self._backend_cache = None
        self._context_cache = None
        self._ui_cache = None
        self._gateway_cache = None
        self._memory_cache = None

    def _record_undo(self, plugin_name: str, undo: Any) -> None:
        """Push a plugin side-effect undo onto its LIFO rollback stack."""
        self._undo_stack.setdefault(plugin_name, []).append(undo)

    def deactivate(self, name: str) -> bool:
        """Deactivate a plugin: LIFO-undo side effects, then its hook."""
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        if getattr(plugin.manifest, "tier", "user") == "mandatory":
            # Mandatory plugins are hardcoded core — they cannot be disabled.
            logger.warning(f"Refusing to deactivate mandatory plugin '{name}'")
            return False
        if name not in self._activated:
            return True
        # Close transport for distributed plugins
        transport = self._transports.pop(name, None)
        if transport is not None:
            try:
                transport.deactivate()
                transport.close()
            except Exception:
                logger.warning("Transport close failed for '%s'", name, exc_info=True)
        try:
            for undo in reversed(self._undo_stack.pop(name, [])):
                try:
                    undo()
                except Exception:
                    logger.warning("Undo failed while deactivating '%s'",
                                   name, exc_info=True)
            plugin.on_deactivate()
            self._activated.discard(name)
            self._invalidate_cache()
            return True
        except Exception as e:
            logger.error(f"Error deactivating plugin '{name}': {e}")
            return False

    def emit_activation_event(
        self, event: str, host: dict[str, Any] | None = None
    ) -> list[str]:
        """Fire an activation event; lazily load+activate matching plugins.

        Matching supports ``*`` (any event), exact names, and trailing
        wildcards (``category:*`` / ``prefix.*``).  ``host`` is the
        capability mapping forwarded to :meth:`activate`.  Returns the
        names of plugins activated by this event.
        """
        activated_now: list[str] = []
        for name, (plugin_dir, source, manifest) in list(self._lazy.items()):
            if not _event_matches(event, manifest.activation_events):
                continue
            self._lazy.pop(name, None)
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
                    self._merge_manifest(self._plugins[new_name].manifest, manifest)
            if name in self._plugins and self.activate(name, host=host):
                activated_now.append(name)
                logger.info("Lazy plugin '{}' activated on event '{}'", name, event)
        return activated_now

    def activate_all(self) -> dict[str, bool]:
        """Activate every registered plugin; return per-name success flags."""
        results: dict[str, bool] = {}
        for name in self._plugins:
            results[name] = self.activate(name)
        return results

    async def aactivate_all(self) -> dict[str, bool]:
        """Async variant of :meth:`activate_all` (runs in a worker thread)."""
        return await asyncio.to_thread(self.activate_all)

    def deactivate_all(self) -> None:
        """Deactivate all currently active plugins."""
        for name in list(self._activated):
            self.deactivate(name)

    # 鈹€鈹€ Tool/Skill/Hook aggregation 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def get_all_tools(self) -> list[Any]:
        """Return deduplicated tools from every activated plugin.

        For distributed plugins (``transport`` = ``process``/``remote``),
        tool metadata is fetched over the transport and wrapped in
        :class:`~encre.plugins.transport.RemoteToolProxy` so the agent
        loop is unaware of transport details.

        Results are cached until the activated set changes.
        """
        if self._tool_cache is not None:
            return self._tool_cache
        tools: list[Any] = []
        seen: set[str] = set()
        for name in self._activated:
            transport = self._transports.get(name)
            if transport is not None:
                # Distributed plugin: fetch metadata over transport
                for meta in transport.get_tool_metas():
                    if meta.name and meta.name not in seen:
                        seen.add(meta.name)
                        tools.append(RemoteToolProxy(meta, transport))
                continue
            # Local plugin: direct in-process call
            plugin = self._plugins[name]
            for tool in plugin.get_tools():
                tool_name = getattr(tool, "name", "")
                if tool_name and tool_name not in seen:
                    seen.add(tool_name)
                    tools.append(tool)
        self._tool_cache = tools
        return tools

    def get_all_skills(self) -> list[Any]:
        """Return deduplicated skills from every activated plugin."""
        if self._skill_cache is not None:
            return self._skill_cache
        skills: list[Any] = []
        seen: set[str] = set()
        for name in self._activated:
            transport = self._transports.get(name)
            if transport is not None:
                for skill_meta in transport.get_skill_metas():
                    skill_name = skill_meta.get("name", "") if isinstance(skill_meta, dict) else ""
                    if skill_name and skill_name not in seen:
                        seen.add(skill_name)
                        skills.append(skill_meta)
                continue
            plugin = self._plugins[name]
            for skill in plugin.get_skills():
                skill_name = getattr(skill, "name", "")
                if skill_name and skill_name not in seen:
                    seen.add(skill_name)
                    skills.append(skill)
        self._skill_cache = skills
        return skills

    def get_all_hooks(self) -> dict[str, list[Any]]:
        """Return event_type -> list of handlers across activated plugins."""
        if self._hook_cache is not None:
            return self._hook_cache
        hooks: dict[str, list[Any]] = {}
        for name in self._activated:
            transport = self._transports.get(name)
            if transport is not None:
                for event_type, hook_meta in transport.get_hook_metas():
                    hooks.setdefault(event_type, []).append(hook_meta)
                continue
            plugin = self._plugins[name]
            for event_type, handler in plugin.get_hooks():
                hooks.setdefault(event_type, []).append(handler)
        self._hook_cache = hooks
        return hooks

    def get_all_backends(self) -> dict[str, type[Any]]:
        """Return name -> backend class mappings from activated plugins."""
        if self._backend_cache is not None:
            return self._backend_cache
        backends: dict[str, type[Any]] = {}
        for name in self._activated:
            transport = self._transports.get(name)
            if transport is not None:
                # Distributed plugins cannot register backend classes
                # (they live in a separate process/remote host)
                continue
            plugin = self._plugins[name]
            backends.update(plugin.get_backends())
        self._backend_cache = backends
        return backends

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
        for name in self._activated:
            plugin = self._plugins[name]
            providers.extend(plugin.get_context_providers())
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
        for name in self._activated:
            plugin = self._plugins[name]
            providers.extend(plugin.get_ui_providers())
        self._ui_cache = providers
        return providers

    def get_all_gateway_adapters(self) -> list[Any]:
        """Return IM platform gateway adapters (S8).

        Each adapter implements ``connect()`` / ``disconnect()`` /
        ``send_message()`` / ``receive_message()``.
        """
        if self._gateway_cache is not None:
            return self._gateway_cache
        adapters: list[Any] = []
        for name in self._activated:
            plugin = self._plugins[name]
            adapters.extend(plugin.get_gateway_adapters())
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
        for name in self._activated:
            plugin = self._plugins[name]
            stores.extend(plugin.get_memory_stores())
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
        self._activated.clear()
        self._failed.clear()
        self._undo_stack.clear()
        self._lazy.clear()
        self._transports.clear()
        self._invalidate_cache()


def _event_matches(event: str, activation_events: list[str]) -> bool:
    """Return ``True`` when *event* satisfies any of ``activation_events``.

    Supported patterns: ``*`` (everything), exact names, and trailing
    wildcards 鈥?both ``category:*`` (colon form) and ``prefix.*`` (dot
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
