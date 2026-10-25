#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
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

"""Persisted EA discovery snapshot with lazy plugin shells.

Startup used to import every EA package (hundreds of modules) just to learn
its manifest, tool schemas and tool UI metadata.  This module records that
surface once, keyed by a cheap per-package stamp, so later startups rebuild
the whole plugin registry from data instead of code: a package's module is
imported only when one of its tools is actually called.

Only data that is static by contract is snapshotted (manifest, tool
names/descriptions/schemas/UI, declarative tool metadata).  A package that
contributes live callables or classes the registry must aggregate up front
(hooks, backends, skills, context/UI/gateway/memory providers, activation
hooks) is marked *eager* and keeps the old import-at-discovery path.

The snapshot is a cache, never a source of truth: an unreadable, stale or
incompatible cache simply falls back to importing, and every entry is
stamped so any change to the package's pyproject/entry module invalidates it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import inspect
import json
import os
import threading
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource

logger = get_logger("encre.plugins.discovery_cache")

_CACHE_FILENAME = ".discovery.json"
_FORMAT_VERSION = 1

#: Everything a tool object exposes that the registry, discovery index and
#: prompt payload read without ever executing the tool.
_TOOL_ATTRS = (
    "name",
    "description",
    "input_schema",
    "intents",
    "category",
    "triggers",
    "always_available",
    "max_result_size_chars",
    "semantic_type",
    "cost_level",
    "retryability",
    "safe_fallback",
    "source",
    "ui",
)

#: Methods whose overrides need the plugin instance at discovery time
#: (live callables/classes cannot be snapshotted as JSON).
_UNSAFE_METHODS = (
    "get_skills",
    "get_hooks",
    "get_backends",
    "get_context_providers",
    "get_ui_providers",
    "get_gateway_adapters",
    "get_memory_stores",
    "get_tool_meta",
    "on_activate",
    "on_deactivate",
)

#: Soft switch: ``ENCRE_PLUGIN_LAZY=0`` restores import-at-discovery.
_LAZY_ENV = "ENCRE_PLUGIN_LAZY"

#: root string -> in-memory snapshot (same dict the file round-trips).
_CACHES: dict[str, dict[str, dict[str, Any]]] = {}
_LOCK = threading.RLock()


def _lazy_enabled() -> bool:
    return os.environ.get(_LAZY_ENV, "1") != "0"


def package_stamp(pkg: Any) -> str:
    """Cheap change stamp for one EA package directory.

    Covers the package metadata plus the entry package's top-level files:
    pyproject edits (tier, entry point), a redeploy (directory timestamps),
    and in-place edits of ``plugin.py`` / ``tool.py`` / sibling modules.
    A nested helper module edited in place is caught by the hot-reload path
    (which invalidates the entry) or by ``ENCRE_PLUGIN_LAZY=0``.
    """
    pkg_dir = Path(pkg.pkg_dir)
    module_dir = pkg_dir / str(getattr(pkg, "module_name", "") or "")
    parts: list[str] = []

    def _add(path: Path) -> None:
        try:
            st = path.stat()
            parts.append(f"{path.name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"{path.name}:missing")

    _add(pkg_dir)
    _add(pkg_dir / "pyproject.toml")
    if module_dir.is_dir():
        _add(module_dir)
        try:
            for entry in sorted(module_dir.iterdir(), key=lambda item: item.name):
                if entry.is_file() and entry.suffix == ".py":
                    _add(entry)
        except OSError:
            pass
    else:
        _add(pkg_dir / "plugin.py")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def cache_path(root: Path) -> Path:
    return Path(root) / _CACHE_FILENAME


def load_cache(root: Path) -> dict[str, dict[str, Any]]:
    """Return the in-memory snapshot for *root*, loading it on first use."""
    key = str(Path(root))
    with _LOCK:
        cached = _CACHES.get(key)
        if cached is not None:
            return cached
        entries: dict[str, dict[str, Any]] = {}
        path = cache_path(root)
        try:
            if path.is_file():
                doc = json.loads(path.read_text("utf-8"))
                if isinstance(doc, dict) and doc.get("version") == _FORMAT_VERSION:
                    raw = doc.get("plugins")
                    if isinstance(raw, dict):
                        entries = {
                            str(name): entry
                            for name, entry in raw.items()
                            if isinstance(entry, dict)
                        }
        except Exception:
            logger.debug("EA discovery snapshot unreadable; importing as before", exc_info=True)
            entries = {}
        _CACHES[key] = entries
        return entries


def save_cache(root: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Persist *entries* atomically (best effort; a failure only costs speed)."""
    path = cache_path(root)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"version": _FORMAT_VERSION, "plugins": entries}
        tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), "utf-8")
        os.replace(tmp, path)
    except Exception:
        logger.debug("Could not write EA discovery snapshot", exc_info=True)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def invalidate(name: str) -> None:
    """Drop one package's snapshot entry (hot reload / forced re-import)."""
    with _LOCK:
        for entries in _CACHES.values():
            entries.pop(name, None)


def _eager_reason(plugin: EncrePlugin) -> str:
    """Why *plugin* cannot be snapshotted, or ``""`` when it can."""
    cls = type(plugin)
    for method in _UNSAFE_METHODS:
        if getattr(cls, method, None) is not getattr(EncrePlugin, method, None):
            return f"overrides {method}"
    return ""


def _tool_spec(tool: Any) -> dict[str, Any] | None:
    name = getattr(tool, "name", "")
    if not isinstance(name, str) or not name:
        return None
    attrs: dict[str, Any] = {}
    for key in _TOOL_ATTRS:
        attrs[key] = getattr(tool, key, None)
    attrs["name"] = name
    if not isinstance(attrs.get("description"), str):
        attrs["description"] = ""
    schema = attrs.get("input_schema")
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    attrs["input_schema"] = schema
    ui = attrs.get("ui")
    if ui is not None and not isinstance(ui, dict):
        as_dict = getattr(ui, "as_dict", None)
        attrs["ui"] = as_dict() if callable(as_dict) else None
    minified = getattr(tool, "_minified_schema", None)
    if not isinstance(minified, dict) or not minified:
        try:
            from encre.tools.base import _minify_schema

            minified = _minify_schema(schema)
        except Exception:
            minified = schema
    return {"name": name, "attrs": attrs, "minified": minified}


def capture_entry(plugin: EncrePlugin, pkg: Any, stamp: str) -> dict[str, Any] | None:
    """Build the snapshot entry for a freshly imported *plugin*.

    Returns ``None`` when the package must stay on the eager path (or when
    its surface cannot be represented as JSON).
    """
    reason = _eager_reason(plugin)
    if reason:
        logger.debug("EA package '%s' stays eager: %s", pkg.name, reason)
        return {"stamp": stamp, "eager": True, "reason": reason}
    try:
        tools = []
        for tool in plugin.get_tools() or ():
            spec = _tool_spec(tool)
            if spec is None:
                return {"stamp": stamp, "eager": True, "reason": "unnameable tool"}
            tools.append(spec)
        tool_meta = list(plugin.get_tool_meta() or ())
    except Exception:
        logger.debug("EA package '%s' surface not snapshottable", pkg.name, exc_info=True)
        return {"stamp": stamp, "eager": True, "reason": "inspection failed"}
    entry = {
        "stamp": stamp,
        "factory": str(getattr(pkg, "factory_target", "") or plugin.manifest.factory),
        "tier": str(getattr(pkg, "tier", "") or plugin.manifest.tier),
        "manifest": plugin.manifest.to_dict(),
        "tools": tools,
        "tool_meta": tool_meta,
    }
    try:
        json.dumps(entry, ensure_ascii=False)
    except Exception:
        logger.debug("EA package '%s' surface is not JSON-safe", pkg.name, exc_info=True)
        return {"stamp": stamp, "eager": True, "reason": "not JSON-safe"}
    return entry


class LazyTool:
    """Schema-complete stand-in that imports its plugin on first execution.

    The attributes and format conversions the prompt build reads are served
    from the snapshot; only real work (execute, permission predicates that
    depend on arguments) resolves the module.
    """

    def __init__(self, owner: LazyEaPlugin, spec: dict[str, Any]) -> None:
        self._owner = owner
        attrs = spec.get("attrs") or {}
        self.name = str(spec.get("name") or attrs.get("name") or "")
        self.description = attrs.get("description") or ""
        self.input_schema = attrs.get("input_schema") or {}
        self.intents = attrs.get("intents") or ["general"]
        self.category = attrs.get("category") or "general"
        self.triggers = attrs.get("triggers") or []
        self.always_available = bool(attrs.get("always_available", False))
        self.max_result_size_chars = attrs.get("max_result_size_chars", 100_000)
        self.semantic_type = attrs.get("semantic_type") or "general"
        self.cost_level = attrs.get("cost_level") or "medium"
        self.retryability = attrs.get("retryability") or "auto"
        self.safe_fallback = attrs.get("safe_fallback") or ""
        self.source = attrs.get("source") or "user"
        self.ui = attrs.get("ui")
        self._minified_schema = spec.get("minified") or self.input_schema

    # -- resolved view ---------------------------------------------------
    def _resolve(self) -> Any:
        return self._owner.resolve_tool(self.name)

    def __getattr__(self, item: str) -> Any:
        # Only reached for members not served by the snapshot (rare
        # plugin-specific attributes) -- resolving here is correct, the
        # caller is about to use the real object anyway.
        if item.startswith("__"):
            raise AttributeError(item)
        return getattr(self._resolve(), item)

    # -- prompt/format surface (no import) -------------------------------
    def to_openai_format(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._minified_schema,
            },
        }

    def to_anthropic_format(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self._minified_schema,
        }

    def discovery_card(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "parameters": self.input_schema,
            "semantic_type": self.semantic_type,
            "cost_level": self.cost_level,
            "retryability": self.retryability,
            "safe_fallback": self.safe_fallback,
        }

    # -- execution / argument-dependent predicates -----------------------
    async def execute(self, **kwargs: Any) -> Any:
        if self._owner.resolved:
            tool = self._owner.resolve_tool(self.name)
        else:
            # First call imports the package: keep that off the event loop
            # so a cold tool invocation cannot stall streaming or the UI.
            tool = await asyncio.to_thread(self._owner.resolve_tool, self.name)
        result = tool.execute(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def is_concurrency_safe(self, input_data: dict[str, Any]) -> bool:
        return bool(self._resolve().is_concurrency_safe(input_data))

    def is_readonly(self, input_data: dict[str, Any]) -> bool:
        return bool(self._resolve().is_readonly(input_data))

    def is_destructive(self, input_data: dict[str, Any]) -> bool:
        fn = getattr(self._resolve(), "is_destructive", None)
        return bool(fn(input_data)) if callable(fn) else False

    def validate_input(self, args: dict[str, Any]) -> str | None:
        fn = getattr(self._resolve(), "validate_input", None)
        return fn(args) if callable(fn) else None

    def backfill_observable_input(self, args: dict[str, Any]) -> dict[str, Any]:
        fn = getattr(self._resolve(), "backfill_observable_input", None)
        return fn(args) if callable(fn) else args

    def should_defer(self, context: dict[str, Any]) -> bool:
        fn = getattr(self._resolve(), "should_defer", None)
        return bool(fn(context)) if callable(fn) else False

    def get_effective_path(self, args: dict[str, Any]) -> str | None:
        fn = getattr(self._resolve(), "get_effective_path", None)
        return fn(args) if callable(fn) else None


class LazyEaPlugin(EncrePlugin):
    """Registered plugin shell whose code is imported on first tool call."""

    def __init__(self, pkg: Any, entry: dict[str, Any]) -> None:
        manifest = PluginManifest.from_dict(dict(entry.get("manifest") or {}))
        try:
            manifest.source = PluginSource(entry["manifest"]["source"])
        except Exception:
            manifest.source = PluginSource.INSTALLED
        self.manifest = manifest
        self._pkg = pkg
        self._factory = str(entry.get("factory") or getattr(pkg, "factory_target", ""))
        self._tool_specs = list(entry.get("tools") or ())
        self._tool_meta = list(entry.get("tool_meta") or ())
        self._lock = threading.RLock()
        self._real: EncrePlugin | None = None
        self._real_error: str = ""
        self._real_tools: dict[str, Any] | None = None
        self._tools: list[LazyTool] | None = None

    def get_tools(self) -> list[Any]:
        if self._tools is None:
            with self._lock:
                if self._tools is None:
                    self._tools = [LazyTool(self, spec) for spec in self._tool_specs]
        return list(self._tools)

    def get_tool_meta(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._tool_meta]

    @property
    def resolved(self) -> bool:
        return self._real is not None

    def _ensure_real(self) -> EncrePlugin:
        if self._real_error:
            raise RuntimeError(self._real_error)
        if self._real is None:
            with self._lock:
                if self._real_error:
                    raise RuntimeError(self._real_error)
                if self._real is None:
                    try:
                        module_path, _, attr = self._factory.partition(":")
                        module = importlib.import_module(module_path)
                        factory = getattr(module, attr) if attr else module
                        plugin = factory()
                        if not isinstance(plugin, EncrePlugin):
                            raise TypeError(
                                f"factory {self._factory!r} did not return an EncrePlugin"
                            )
                    except Exception as exc:
                        # Cache the failure: a broken package must not pay a
                        # fresh import attempt on every tool call.  Re-raising
                        # a fixed message also keeps the error actionable.
                        self._real_error = (
                            f"plugin {self.manifest.name!r} failed to import on first use: {exc}"
                        )
                        logger.warning(self._real_error)
                        raise RuntimeError(self._real_error) from exc
                    self._real = plugin
                    logger.debug("Lazy EA plugin '%s' imported on first use", self.manifest.name)
        return self._real

    def resolve_tool(self, name: str) -> Any:
        if self._real_tools is not None:
            tool = self._real_tools.get(name)
            if tool is not None:
                return tool
        real = self._ensure_real()
        with self._lock:
            if self._real_tools is None:
                self._real_tools = {
                    str(getattr(tool, "name", "")): tool
                    for tool in (real.get_tools() or ())
                }
            tool = self._real_tools.get(name)
        if tool is not None:
            return tool
        raise LookupError(
            f"tool {name!r} is not provided by plugin {self.manifest.name!r} "
            "after import (its snapshot may be stale)"
        )


def build_lazy_plugin(pkg: Any, entry: dict[str, Any]) -> EncrePlugin | None:
    """Rebuild a plugin shell from a snapshot entry, or ``None`` if unusable."""
    if not _lazy_enabled() or entry.get("eager"):
        return None
    if not isinstance(entry.get("manifest"), dict):
        return None
    try:
        plugin = LazyEaPlugin(pkg, entry)
    except Exception:
        logger.debug("Could not rebuild lazy EA plugin for %r", getattr(pkg, "name", "?"), exc_info=True)
        return None
    if not plugin.manifest.name:
        return None
    return plugin
