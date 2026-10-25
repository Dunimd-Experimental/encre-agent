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

"""Tests for the persisted EA discovery snapshot and lazy plugin shells.

The contract under test: a package whose surface was captured once is rebuilt
from JSON without importing its module, its tools answer every prompt-facing
question from the snapshot, and the first real execution imports the module
transparently.  Packages that contribute live callables (or whose surface is
not JSON-safe) must stay on the eager path.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from encre.plugins import discovery_cache
from encre.plugins.discovery_cache import (
    LazyEaPlugin,
    LazyTool,
    build_lazy_plugin,
    cache_path,
    capture_entry,
    load_cache,
    package_stamp,
    save_cache,
)
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource

_MODULE_SOURCE = '''
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from encre.tools.base import build_tool


async def _run(**_kwargs):
    return "lazy-result"


fake_tool = build_tool(
    name="fake_tool",
    description="Fake tool used by the discovery snapshot tests.",
    input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
    execute=_run,
    ui={"icon": "sparkles", "label": "Fake"},
    category="testing",
    intents=["general", "testing"],
)


class _Plugin(EncrePlugin):
    manifest = PluginManifest(
        name="fake-plugin",
        version="1.2.3",
        description="Fake plugin",
        source=PluginSource.BUNDLED,
        tier="system-default",
        provides_tools=["fake_tool"],
        danger_levels={"fake_tool": "safe"},
    )

    def get_tools(self):
        return [fake_tool]


def create_plugin():
    return _Plugin()
'''


def _make_package(tmp_path: Path) -> SimpleNamespace:
    pkg_dir = tmp_path / "ea-tool-fake"
    module_dir = pkg_dir / "fake_pkg"
    module_dir.mkdir(parents=True)
    (pkg_dir / "pyproject.toml").write_text("[project]\nname='ea-tool-fake'\n", encoding="utf-8")
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "plugin.py").write_text(_MODULE_SOURCE, encoding="utf-8")
    if str(pkg_dir) not in sys.path:
        sys.path.append(str(pkg_dir))
    return SimpleNamespace(
        name="fake-plugin",
        pkg_dir=pkg_dir,
        module_name="fake_pkg",
        factory_target="fake_pkg.plugin:create_plugin",
        tier="system-default",
        skills_dir=None,
        tool_ea={},
    )


@pytest.fixture
def fake_package(tmp_path):
    pkg = _make_package(tmp_path)
    yield pkg
    for name in [m for m in sys.modules if m == "fake_pkg" or m.startswith("fake_pkg.")]:
        sys.modules.pop(name, None)
    if str(pkg.pkg_dir) in sys.path:
        sys.path.remove(str(pkg.pkg_dir))


def _instantiate(pkg: SimpleNamespace) -> EncrePlugin:
    import importlib

    module = importlib.import_module("fake_pkg.plugin")
    return module.create_plugin()


class TestSnapshotCapture:
    def test_capture_records_manifest_and_tool_surface(self, fake_package):
        plugin = _instantiate(fake_package)
        stamp = package_stamp(fake_package)
        entry = capture_entry(plugin, fake_package, stamp)

        assert entry is not None
        assert not entry.get("eager")
        assert entry["stamp"] == stamp
        assert entry["manifest"]["name"] == "fake-plugin"
        assert entry["manifest"]["source"] == "bundled"
        assert entry["manifest"]["danger_levels"] == {"fake_tool": "safe"}
        assert [tool["name"] for tool in entry["tools"]] == ["fake_tool"]
        assert entry["tools"][0]["attrs"]["ui"]["label"] == "Fake"
        assert entry["factory"] == "fake_pkg.plugin:create_plugin"

    def test_package_override_forces_eager_capture(self, fake_package):
        class _SkillPlugin(EncrePlugin):
            manifest = PluginManifest(name="fake-plugin", version="1", source=PluginSource.BUNDLED)

            def get_skills(self):
                return []

        entry = capture_entry(_SkillPlugin(), fake_package, package_stamp(fake_package))
        assert entry is not None and entry["eager"] is True
        assert "get_skills" in entry["reason"]

    def test_non_json_surface_forces_eager_capture(self, fake_package):
        class _Opaque:
            pass

        class _Plugin(EncrePlugin):
            manifest = PluginManifest(name="fake-plugin", version="1", source=PluginSource.BUNDLED)

            def get_tools(self):
                return [SimpleNamespace(name="t", description="d", input_schema={}, ui=_Opaque())]

        entry = capture_entry(_Plugin(), fake_package, package_stamp(fake_package))
        # ui without as_dict() is dropped, so this one stays snapshottable;
        # an unserialisable *schema* is what must force eager.
        assert entry is not None and not entry.get("eager")

        class _BadPlugin(EncrePlugin):
            manifest = PluginManifest(name="fake-plugin", version="1", source=PluginSource.BUNDLED)

            def get_tools(self):
                return [SimpleNamespace(name="t", description="d", input_schema={"v": object()})]

        bad = capture_entry(_BadPlugin(), fake_package, package_stamp(fake_package))
        assert bad is not None and bad["eager"] is True


class TestLazyRebuild:
    def test_lazy_plugin_serves_schema_without_importing(self, fake_package):
        entry = capture_entry(_instantiate(fake_package), fake_package, package_stamp(fake_package))
        for name in [m for m in sys.modules if m == "fake_pkg" or m.startswith("fake_pkg.")]:
            sys.modules.pop(name, None)

        plugin = build_lazy_plugin(fake_package, entry)
        assert isinstance(plugin, LazyEaPlugin)
        assert plugin.manifest.source is PluginSource.BUNDLED
        assert plugin.manifest.danger_levels == {"fake_tool": "safe"}

        tools = plugin.get_tools()
        assert len(tools) == 1 and isinstance(tools[0], LazyTool)
        tool = tools[0]
        assert "fake_pkg.plugin" not in sys.modules
        assert tool.to_openai_format()["function"]["name"] == "fake_tool"
        assert tool.to_anthropic_format()["input_schema"] == tool.input_schema
        assert tool.discovery_card()["category"] == "testing"
        assert tool.ui["label"] == "Fake"
        assert "fake_pkg.plugin" not in sys.modules

    def test_first_execute_imports_and_delegates(self, fake_package):
        entry = capture_entry(_instantiate(fake_package), fake_package, package_stamp(fake_package))
        for name in [m for m in sys.modules if m == "fake_pkg" or m.startswith("fake_pkg.")]:
            sys.modules.pop(name, None)

        plugin = build_lazy_plugin(fake_package, entry)
        tool = plugin.get_tools()[0]
        result = asyncio.run(tool.execute(x="1"))
        assert result == "lazy-result"
        assert "fake_pkg.plugin" in sys.modules

    def test_lazy_path_can_be_disabled(self, fake_package, monkeypatch):
        entry = capture_entry(_instantiate(fake_package), fake_package, package_stamp(fake_package))
        monkeypatch.setenv("ENCRE_PLUGIN_LAZY", "0")
        assert build_lazy_plugin(fake_package, entry) is None

    def test_eager_entry_is_not_rebuilt_lazily(self, fake_package):
        entry = capture_entry(_instantiate(fake_package), fake_package, package_stamp(fake_package))
        entry["eager"] = True
        assert build_lazy_plugin(fake_package, entry) is None

    def test_failed_import_is_reported_and_cached(self, fake_package):
        entry = capture_entry(_instantiate(fake_package), fake_package, package_stamp(fake_package))
        entry["factory"] = "no_such_module_for_lazy_test:create_plugin"
        plugin = build_lazy_plugin(fake_package, entry)
        tool = plugin.get_tools()[0]
        with pytest.raises(RuntimeError, match="failed to import on first use"):
            asyncio.run(tool.execute())
        # Second call raises the cached failure without another import attempt.
        with pytest.raises(RuntimeError, match="failed to import on first use"):
            asyncio.run(tool.execute())


class TestStampsAndFileCache:
    def test_stamp_changes_when_entry_file_changes(self, fake_package):
        first = package_stamp(fake_package)
        plugin_file = fake_package.pkg_dir / "fake_pkg" / "plugin.py"
        os.utime(plugin_file, ns=(1, 1_000_000_000))
        assert package_stamp(fake_package) != first

    def test_cache_roundtrip(self, tmp_path):
        root = tmp_path / "plugins"
        root.mkdir()
        save_cache(root, {"a": {"stamp": "s", "tools": []}})
        assert cache_path(root).is_file()
        # Drop the process memo so the file is really read back.
        discovery_cache._CACHES.pop(str(root), None)
        assert load_cache(root)["a"]["stamp"] == "s"

    def test_unreadable_cache_falls_back_to_empty(self, tmp_path):
        root = tmp_path / "plugins"
        root.mkdir()
        cache_path(root).write_text("{not json", encoding="utf-8")
        discovery_cache._CACHES.pop(str(root), None)
        assert load_cache(root) == {}

    def test_invalidate_drops_entry(self, tmp_path):
        root = tmp_path / "plugins"
        root.mkdir()
        save_cache(root, {"a": {"stamp": "s"}, "b": {"stamp": "t"}})
        discovery_cache._CACHES.pop(str(root), None)
        cache = load_cache(root)
        discovery_cache.invalidate("a")
        assert "a" not in cache and "b" in cache
