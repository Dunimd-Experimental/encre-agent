#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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

"""Hot-reload for plugin directories.

Watches ``ea_tools/``, ``./.encre/plugins/``, and
``~/.dunimd/encre/plugins/`` for filesystem changes.  When a plugin's
files are created/modified/deleted, the affected plugin is deactivated,
re-discovered, and re-activated — without restarting the agent.

Uses :mod:`watchfiles` (already a harness dependency) for efficient
inotify/kqueue-based file watching.  A debounce timer prevents thrashing
during multi-file edits (e.g. ``git checkout`` touching dozens of files).

Usage::

    from encre.plugins.hot_reload import PluginHotReloader
    reloader = PluginHotReloader(registry, on_reload=agent.reload_tools)
    reloader.start()   # background asyncio task
    ...
    reloader.stop()

Flagship: a plugin developer can edit a tool's source and the agent
picks it up automatically — no restart, no manual reload.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.plugins.hot_reload")

_DEBOUNCE_S = 1.0


class PluginHotReloader:
    """Watch plugin directories and reload on change.

    The watcher runs as an asyncio background task.  On each filesystem
    event batch (after a short debounce), the reloader:
    1. Determines which plugin packages changed (by directory)
    2. Deactivates affected plugins
    3. Re-discovers them
    4. Re-activates them
    5. Calls ``on_reload`` so the host can refresh its tool registry
    """

    def __init__(
        self,
        registry: Any,
        on_reload: Callable[[], None] | None = None,
        watch_paths: list[Path] | None = None,
    ) -> None:
        self._registry = registry
        self._on_reload = on_reload
        self._task: asyncio.Task | None = None
        self._background_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._pending: set[str] = set()
        self._last_fire = 0.0
        if watch_paths is None:
            watch_paths = self._default_paths()
        self._watch_paths = [p for p in watch_paths if p.is_dir()]

    @staticmethod
    def _default_paths() -> list[Path]:
        from encre.plugins.ea_scan import ea_tools_root
        return [
            ea_tools_root(),
            Path(".encre/plugins"),
            Path.home() / ".dunimd" / "encre" / "plugins",
        ]

    async def start(self) -> None:
        """Start watching.  No-op if already running."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        if importlib.util.find_spec("watchfiles") is None:
            logger.warning("watchfiles not installed; hot-reload disabled")
            return
        self._task = asyncio.ensure_future(self._run())
        logger.info("Hot-reload watching %d paths", len(self._watch_paths))

    def start_sync(self) -> None:
        """Schedule :meth:`start` on the running event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
        self._background_task = loop.create_task(self.start())

    async def stop(self) -> None:
        """Stop watching."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._background_task is not None:
            self._background_task.cancel()
            self._background_task = None

    async def _run(self) -> None:
        try:
            from watchfiles import awatch
        except ImportError:
            return
        while not self._stop_event.is_set():
            try:
                async for changes in awatch(*self._watch_paths, stop_event=self._stop_event):
                    await self._handle_changes(changes)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Hot-reload watcher error", exc_info=True)
                await asyncio.sleep(5)

    async def _handle_changes(self, changes: set[tuple[Any, str]]) -> None:
        affected: set[str] = set()
        for _change_type, path_str in changes:
            path = Path(path_str)
            pkg_dir = self._find_package_dir(path)
            if pkg_dir is not None:
                affected.add(str(pkg_dir))
        if not affected:
            return
        # Debounce: wait for more changes to accumulate
        now = time.monotonic()
        if now - self._last_fire < _DEBOUNCE_S:
            await asyncio.sleep(_DEBOUNCE_S - (now - self._last_fire))
        self._last_fire = time.monotonic()
        await self._reload_affected(affected)

    @staticmethod
    def _find_package_dir(path: Path) -> Path | None:
        """Walk up from *path* to the containing ``ea-tool-*``/``ea-skill-*`` dir."""
        for parent in [path, *path.parents]:
            if parent.name.startswith(("ea-tool-", "ea-skill-")):
                return parent
            if parent.name in ("plugins", "ea_tools"):
                return None
            if parent.parent and parent.parent.name in (".encre", "encre"):
                return parent
        return None

    async def _reload_affected(self, pkg_dirs: set[str]) -> None:
        """Deactivate, re-discover, and re-activate changed plugins."""
        for pkg_dir_str in pkg_dirs:
            pkg_dir = Path(pkg_dir_str)
            plugin_name = self._find_plugin_name(pkg_dir)
            if plugin_name is None:
                continue
            logger.info("Hot-reloading plugin '%s' (dir=%s)", plugin_name, pkg_dir)
            # Deactivate if active
            if plugin_name in self._registry._activated:
                await asyncio.to_thread(self._registry.deactivate, plugin_name)
                self._registry._plugins.pop(plugin_name, None)
                self._registry._manifests.pop(plugin_name, None)
            # Clear manifest cache for this package
            with contextlib.suppress(Exception):
                from encre.plugins.manifest_cache import put_cached_pyproject
                pyproject = pkg_dir / "pyproject.toml"
                if pyproject.exists():
                    put_cached_pyproject(pyproject, {})  # invalidate
        # Re-discover
        await asyncio.to_thread(self._registry.discover_all)
        # Notify host
        if self._on_reload is not None:
            try:
                self._on_reload()
            except Exception:
                logger.warning("on_reload callback failed", exc_info=True)
        logger.info("Hot-reload complete: %d plugins reloaded", len(pkg_dirs))

    @staticmethod
    def _find_plugin_name(pkg_dir: Path) -> str | None:
        """Derive the plugin name from a package directory."""
        try:
            import tomllib
        except ModuleNotFoundError:
            try:
                import tomli as tomllib
            except ImportError:
                return None
        pyproject = pkg_dir / "pyproject.toml"
        if not pyproject.exists():
            return None
        try:
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            return data.get("project", {}).get("name", pkg_dir.name)
        except Exception:
            return None
