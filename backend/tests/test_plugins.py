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

"""Tests for the plugin system: manifest, plugin protocol, and registry.

The plugin stack is exercised layer by layer.  First the ``PluginManifest``
data model: its defaults, its optional fields and its ``to_dict``
serialisation.  Second the ``EncrePlugin`` base class contract for
declaring tools, skills, hooks and backends.  Third the ``PluginRegistry``
lifecycle: registration, activation, lookup, aggregation and reset.
Fourth the market-facing truth in :meth:`PluginRegistry.snapshot_states` —
what counts as installed on this machine, and the enable/disable ledger
adoption that decision implies.
"""

from encre.plugins.registry import PluginRegistry
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource, PluginState


class TestPluginManifest:
    """Tests for ``PluginManifest`` construction and serialisation.

    Covers the defaults applied to a minimal manifest, the preservation of
    every optional field on a fully specified one, the string value of each
    ``PluginSource`` member, and the faithfulness of ``to_dict``.
    """

    def test_verify_manifest_create_minimal(self):
        """A minimal manifest applies the INSTALLED default source."""
        m = PluginManifest(name="test-plugin", version="0.1.0")
        assert m.name == "test-plugin"
        assert m.version == "0.1.0"
        assert m.source == PluginSource.INSTALLED

    def test_verify_manifest_create_full(self):
        """A fully specified manifest preserves every optional field."""
        m = PluginManifest(
            name="full-plugin",
            version="1.0.0",
            description="A full plugin",
            author="Test Author",
            license="Apache-2.0",
            homepage="https://example.com",
            source=PluginSource.PROJECT,
            dependencies=["dep1", "dep2"],
            min_ea_version="0.2.0",
            tags=["database", "tools"],
            provides_tools=["db_query"],
            provides_skills=["db_skill"],
            provides_hooks=["on_session_start"],
            provides_backends=["postgres"],
        )
        assert m.name == "full-plugin"
        assert m.author == "Test Author"
        assert m.license == "Apache-2.0"
        assert m.source == PluginSource.PROJECT
        assert "dep1" in m.dependencies
        assert "database" in m.tags
        assert "db_query" in m.provides_tools

    def test_verify_manifest_plugin_source_enum(self):
        """Each ``PluginSource`` member maps to its documented string value."""
        assert PluginSource.BUNDLED.value == "bundled"
        assert PluginSource.INSTALLED.value == "installed"
        assert PluginSource.PROJECT.value == "project"
        assert PluginSource.USER.value == "user"

    def test_verify_manifest_to_dict(self):
        """``to_dict`` serialises the manifest faithfully."""
        m = PluginManifest(name="dict-test", version="0.1.0")
        d = m.to_dict()
        assert d["name"] == "dict-test"
        assert d["version"] == "0.1.0"
        assert d["source"] == "installed"


class TestEncrePlugin:
    """Tests for the ``EncrePlugin`` base class contract.

    Confirms that a subclass can declare tools and hooks, that the base
    class supplies empty defaults for interfaces a subclass does not
    override, and that the manifest attribute is carried through to the
    instance.
    """

    def test_verify_plugin_with_tools(self):
        """A plugin subclass can declare and return tools."""
        class MyPlugin(EncrePlugin):
            """Plugin that advertises a single fake tool."""

            manifest = PluginManifest(name="my-plugin", version="1.0.0")

            def get_tools(self):
                """Return the tools provided by this plugin."""
                return ["fake_tool"]

        plugin = MyPlugin()
        assert plugin.manifest.name == "my-plugin"
        assert plugin.get_tools() == ["fake_tool"]

    def test_verify_plugin_with_hooks(self):
        """A plugin subclass can declare and return hooks."""
        class HookPlugin(EncrePlugin):
            """Plugin that advertises a single session-start hook."""

            manifest = PluginManifest(name="hook-plugin", version="1.0.0")

            def get_hooks(self):
                """Return the hooks provided by this plugin."""
                return [("on_session_start", lambda: None)]

        plugin = HookPlugin()
        hooks = plugin.get_hooks()
        assert len(hooks) == 1
        assert hooks[0][0] == "on_session_start"

    def test_verify_plugin_default_returns_empty(self):
        """Unimplemented interface methods return sensible empty defaults."""
        class EmptyPlugin(EncrePlugin):
            """Plugin that overrides nothing and relies on every base default."""

            manifest = PluginManifest(name="empty", version="0.1.0")

        plugin = EmptyPlugin()
        assert plugin.get_tools() == []
        assert plugin.get_skills() == []
        assert plugin.get_hooks() == []
        assert plugin.get_backends() == {}


class TestPluginRegistry:
    """Tests for the ``PluginRegistry`` lifecycle operations.

    Covers the empty-registry invariants, registration and duplicate-name
    suppression, activation and deactivation, unregistration, lookup by
    name, full manifest listing, aggregate tool/skill/hook/backend
    collection from active plugins, and the full clear performed by
    reset.
    """

    def test_verify_registry_empty(self):
        """A new registry has zero count and zero active count."""
        registry = PluginRegistry()
        assert registry.count == 0
        assert registry.active_count == 0

    def test_verify_registry_register_plugin(self):
        """Registering a plugin increments the count."""
        class TestPlugin(EncrePlugin):
            """Plain plugin used to check that registration is counted."""

            manifest = PluginManifest(name="reg-test", version="1.0.0")

        registry = PluginRegistry()
        registry.register(TestPlugin())
        assert registry.count == 1

    def test_verify_registry_register_duplicate_name(self):
        """Duplicate registration under the same name is suppressed."""
        class DupPlugin(EncrePlugin):
            """Plugin name reused on purpose to trigger duplicate suppression."""

            manifest = PluginManifest(name="dup", version="1.0.0")

        registry = PluginRegistry()
        registry.register(DupPlugin())
        registry.register(DupPlugin())
        assert registry.count == 1

    def test_verify_registry_activate_deactivate(self):
        """Activate and deactivate toggle the active count correctly."""
        class ActPlugin(EncrePlugin):
            """Plugin used to exercise the activation toggle."""

            manifest = PluginManifest(name="act-test", version="1.0.0")

        registry = PluginRegistry()
        plugin = ActPlugin()
        registry.register(plugin)
        assert registry.activate("act-test") is True
        assert registry.active_count == 1
        assert registry.deactivate("act-test") is True
        assert registry.active_count == 0

    def test_verify_registry_unregister(self):
        """Unregister removes the plugin and decrements the count."""
        class UnregPlugin(EncrePlugin):
            """Plugin used to check that unregistration decrements the count."""

            manifest = PluginManifest(name="unreg-test", version="1.0.0")

        registry = PluginRegistry()
        registry.register(UnregPlugin())
        assert registry.unregister("unreg-test") is True
        assert registry.count == 0

    def test_verify_registry_get(self):
        """``get`` and ``get_manifest`` return the registered plugin."""
        class GetPlugin(EncrePlugin):
            """Plugin used to check the name-based lookup helpers."""

            manifest = PluginManifest(name="get-test", version="1.0.0")

        registry = PluginRegistry()
        plugin = GetPlugin()
        registry.register(plugin)
        assert registry.get("get-test") is not None
        assert registry.get_manifest("get-test") is not None

    def test_verify_registry_list_all(self):
        """``list_all`` returns every registered manifest."""
        class ListPlugin(EncrePlugin):
            """Plugin used to check the manifest listing helper."""

            manifest = PluginManifest(name="list-test", version="1.0.0")

        registry = PluginRegistry()
        registry.register(ListPlugin())
        manifests = registry.list_all()
        assert len(manifests) == 1
        assert manifests[0].name == "list-test"

    def test_verify_registry_get_all_tools(self):
        """``get_all_tools`` collects tools from active plugins."""
        class ToolPlugin(EncrePlugin):
            """Plugin advertising two tools for the aggregation test."""

            manifest = PluginManifest(name="tool-plugin", version="1.0.0")

            def get_tools(self):
                """Return the tools provided by this plugin."""
                return ["tool1", "tool2"]

        registry = PluginRegistry()
        plugin = ToolPlugin()
        registry.register(plugin)
        registry.activate("tool-plugin")
        tools = registry.get_all_tools()
        assert isinstance(tools, list)

    def test_verify_registry_get_all_skills(self):
        """``get_all_skills`` collects skills from active plugins."""
        class SkillPlugin(EncrePlugin):
            """Plugin advertising one skill for the aggregation test."""

            manifest = PluginManifest(name="skill-plugin", version="1.0.0")

            def get_skills(self):
                """Return the skills provided by this plugin."""
                return ["skill1"]

        registry = PluginRegistry()
        plugin = SkillPlugin()
        registry.register(plugin)
        registry.activate("skill-plugin")
        skills = registry.get_all_skills()
        assert isinstance(skills, list)

    def test_verify_registry_get_all_hooks(self):
        """``get_all_hooks`` collects hooks from active plugins."""
        class HookPlugin(EncrePlugin):
            """Plugin advertising one pre-tool hook for the aggregation test."""

            manifest = PluginManifest(name="hook-plugin2", version="1.0.0")

            def get_hooks(self):
                """Return the hooks provided by this plugin."""
                return [("pre_tool_exec", lambda: None)]

        registry = PluginRegistry()
        plugin = HookPlugin()
        registry.register(plugin)
        registry.activate("hook-plugin2")
        hooks = registry.get_all_hooks()
        assert isinstance(hooks, dict)

    def test_verify_registry_get_all_backends(self):
        """``get_all_backends`` collects backends from active plugins."""
        class BackendPlugin(EncrePlugin):
            """Plugin advertising one custom backend for the aggregation test."""

            manifest = PluginManifest(name="be-plugin", version="1.0.0")

            def get_backends(self):
                """Return the backends provided by this plugin."""
                return {"custom": "FakeBackend"}

        registry = PluginRegistry()
        plugin = BackendPlugin()
        registry.register(plugin)
        registry.activate("be-plugin")
        backends = registry.get_all_backends()
        assert isinstance(backends, dict)
        assert "custom" in backends

    def test_verify_registry_reset(self):
        """Reset clears all state, including the active count."""
        class ResetPlugin(EncrePlugin):
            """Plugin registered and activated before the reset under test."""

            manifest = PluginManifest(name="reset-test", version="1.0.0")

        registry = PluginRegistry()
        registry.register(ResetPlugin())
        registry.activate("reset-test")
        registry.reset()
        assert registry.count == 0
        assert registry.active_count == 0


class TestPluginInstalledTruth:
    """``snapshot_states`` presence and the enable/disable ledger adoption.

    The market's 已安装 tab reads ``installed`` straight off this snapshot, so
    these two fields decide what a user sees when they open it: presence on
    this machine (not ``installed.json`` membership, which only market/zip
    installs ever wrote), and a switch that persists for anything present.
    """

    @staticmethod
    def _registry(tmp_path, monkeypatch):
        """A registry whose installed ledger lives under ``tmp_path``."""
        ledger = tmp_path / "installed.json"
        monkeypatch.setattr(
            PluginRegistry, "_installed_ledger_path",
            staticmethod(lambda: ledger),
        )
        return PluginRegistry()

    @staticmethod
    def _register(registry, name, **fields):
        """Register one plain plugin whose manifest carries ``fields``."""
        manifest = PluginManifest(name=name, version="1.0.0", **fields)

        class _Plugin(EncrePlugin):
            """No-op plugin used to exercise the installed/enabled fields."""

        _Plugin.manifest = manifest
        registry.register(_Plugin())

    def _entry(self, registry: PluginRegistry, name: str) -> dict:
        """The one snapshot entry named ``name``."""
        return next(e for e in registry.snapshot_states() if e["name"] == name)

    def test_verify_on_disk_plugin_without_ledger_is_installed(self, tmp_path, monkeypatch):
        """A package present on disk is installed even with no ledger record."""
        registry = self._registry(tmp_path, monkeypatch)
        pkg = tmp_path / "plugins" / "ea-skill-demo"
        pkg.mkdir(parents=True)
        self._register(
            registry, "ea-skill-demo",
            source=PluginSource.BUNDLED, tier="system-default", pkg_dir=str(pkg),
        )
        entry = self._entry(registry, "ea-skill-demo")
        assert entry["installed"] is True
        # Nobody ever disabled it, so the absent record means enabled.
        assert entry["enabled"] is True

    def test_verify_entry_point_install_without_pkg_dir_is_installed(self, tmp_path, monkeypatch):
        """A pip/entry-point install has no scanned dir but is installed."""
        registry = self._registry(tmp_path, monkeypatch)
        self._register(registry, "pip-plugin", source=PluginSource.INSTALLED)
        assert self._entry(registry, "pip-plugin")["installed"] is True

    def test_verify_pending_shell_is_not_installed(self, tmp_path, monkeypatch):
        """A third-party shell awaiting authorisation is not installed yet."""
        registry = self._registry(tmp_path, monkeypatch)
        pkg = tmp_path / "plugins" / "guest"
        pkg.mkdir(parents=True)
        self._register(registry, "guest", pkg_dir=str(pkg))
        registry._states["guest"] = PluginState.PENDING
        assert self._entry(registry, "guest")["installed"] is False

    def test_verify_mandatory_core_is_not_installed(self, tmp_path, monkeypatch):
        """Hardcoded core has no package of its own, so it is not installed."""
        registry = self._registry(tmp_path, monkeypatch)
        self._register(
            registry, "core-tool",
            source=PluginSource.MANDATORY, tier="mandatory",
        )
        assert self._entry(registry, "core-tool")["installed"] is False

    def test_verify_disable_adopts_plugin_without_record(self, tmp_path, monkeypatch):
        """Disabling a present-but-unrecorded plugin persists, not errors."""
        registry = self._registry(tmp_path, monkeypatch)
        pkg = tmp_path / "plugins" / "vendor"
        pkg.mkdir(parents=True)
        self._register(
            registry, "vendor",
            source=PluginSource.BUNDLED, tier="system-default", pkg_dir=str(pkg),
        )
        assert registry.set_plugin_enabled("vendor", False) is True
        assert registry.is_plugin_disabled("vendor") is True
        # The adoption really went to disk: a fresh registry reads it back.
        reopened = PluginRegistry()
        assert reopened.installed_records()["vendor"]["enabled"] is False

    def test_verify_enable_adopts_plugin_without_record(self, tmp_path, monkeypatch):
        """Enabling the same kind of plugin is the mirror image of disabling."""
        registry = self._registry(tmp_path, monkeypatch)
        pkg = tmp_path / "plugins" / "vendor2"
        pkg.mkdir(parents=True)
        self._register(registry, "vendor2", pkg_dir=str(pkg), factory="demo:create_plugin", allow_local=True)
        assert registry.set_plugin_enabled("vendor2", True) is True
        assert registry.is_plugin_disabled("vendor2") is False

    def test_verify_switch_rejects_unknown_plugin(self, tmp_path, monkeypatch):
        """A name with no manifest has nothing to switch."""
        registry = self._registry(tmp_path, monkeypatch)
        assert registry.set_plugin_enabled("ghost", False) is False
        assert not (tmp_path / "installed.json").exists()

    def test_verify_switch_rejects_missing_package(self, tmp_path, monkeypatch):
        """A manifest pointing at a directory that is gone cannot be switched."""
        registry = self._registry(tmp_path, monkeypatch)
        self._register(
            registry, "gone",
            source=PluginSource.USER, pkg_dir=str(tmp_path / "no-such-dir"),
        )
        assert registry.set_plugin_enabled("gone", False) is False
