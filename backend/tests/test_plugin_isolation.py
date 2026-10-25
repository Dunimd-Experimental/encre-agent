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

"""Isolation-boundary tests for the plugin system.

These cover the load-bearing safety controls that had no direct coverage:
the ``guard`` exception-absorption contract, the fail-closed permission
gate on :class:`~encre.plugins.context.PluginContext`, integrity-hash
persistence, and the process-transport reconstruction of skills/hooks.
"""

import asyncio
import json
from pathlib import Path

import pytest
from encre.plugins import integrity as integrity_mod
from encre.plugins.context import (
    SURFACE_PERMISSIONS,
    _permission_granted,
    build_context,
)
from encre.plugins.guard import FATAL, absorb, absorb_async, is_fatal
from encre.plugins.host import PluginHost
from encre.plugins.registry import PluginRegistry
from encre.plugins.transport import _hook_from_meta, _skill_from_meta
from encre.plugins.types import PluginManifest, PluginState


class TestGuardAbsorb:
    """``guard.absorb`` must swallow third-party failures but never FATAL ones."""

    def test_returns_value_on_success(self):
        assert absorb(lambda: 42) == 42

    def test_absorbs_exception_returns_default(self):
        def boom():
            raise RuntimeError("plugin exploded")

        assert absorb(boom, default="fallback") == "fallback"

    def test_default_is_none_when_missing(self):
        def boom():
            raise ValueError("nope")

        assert absorb(boom) is None

    def test_absorbs_system_exit(self):
        """A plugin calling ``sys.exit`` must not tear down the host."""

        def quit_():
            raise SystemExit(3)

        assert absorb(quit_, default="safe") == "safe"

    def test_reraises_cancelled_error(self):
        async def coro():
            raise asyncio.CancelledError

        c = coro()

        def cancel():
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            absorb(cancel)
        c.close()

    def test_reraises_keyboard_interrupt(self):
        def interrupt():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            absorb(interrupt)

    def test_passes_args_and_kwargs(self):
        assert absorb(lambda a, b=0: a + b, 3, b=4) == 7


class TestGuardAsync:
    """``guard.absorb_async`` mirrors the sync contract over awaitables."""

    @pytest.mark.asyncio
    async def test_returns_value(self):
        async def value():
            return "ok"

        assert await absorb_async(value()) == "ok"

    @pytest.mark.asyncio
    async def test_absorbs_exception(self):
        async def boom():
            raise RuntimeError("async fail")

        assert await absorb_async(boom(), default="d") == "d"

    @pytest.mark.asyncio
    async def test_timeout_returns_default(self):
        async def slow():
            await asyncio.sleep(10)

        assert await absorb_async(slow(), timeout=0.01, default="timeout") == "timeout"

    @pytest.mark.asyncio
    async def test_reraises_cancelled(self):
        async def cancel():
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await absorb_async(cancel())


class TestIsFatal:
    """``is_fatal`` identifies only the two always-propagating exceptions."""

    def test_cancelled_is_fatal(self):
        assert is_fatal(asyncio.CancelledError())

    def test_keyboard_interrupt_is_fatal(self):
        assert is_fatal(KeyboardInterrupt())

    def test_runtime_error_not_fatal(self):
        assert not is_fatal(RuntimeError())

    def test_fatal_tuple_contents(self):
        assert asyncio.CancelledError in FATAL
        assert KeyboardInterrupt in FATAL


class TestPermissionGate:
    """The context permission gate is fail-closed for untrusted tiers."""

    def test_granted_exact_token(self):
        assert _permission_granted("tools", ["tools:register"])

    def test_granted_surface_wildcard(self):
        assert _permission_granted("tools", ["tools:*"])

    def test_granted_universal_wildcard(self):
        assert _permission_granted("tools", ["*"])

    def test_denied_without_token(self):
        assert not _permission_granted("tools", ["commands:register"])

    def test_unknown_surface_allowed(self):
        assert _permission_granted("nonexistent-surface", [])

    def test_all_declared_surfaces_have_tokens(self):
        assert "tools" in SURFACE_PERMISSIONS
        assert "ws_handlers" in SURFACE_PERMISSIONS

    def test_trusted_tier_skips_gate(self):
        """``system-default`` tier is exempt, so register reaches the host."""
        calls = []

        class Host:
            def plugin_register_tool(self, tool, source=""):  # noqa: ARG002
                calls.append(tool)
                return lambda: None

        manifest = PluginManifest(name="trusted", version="1", tier="system-default")
        ctx = build_context("trusted", host={"tools": Host()}, manifest=manifest)
        ctx.tools.register("a-tool")
        assert calls == ["a-tool"]

    def test_user_tier_without_permission_is_blocked(self):
        calls = []

        class Host:
            def plugin_register_tool(self, tool, source=""):  # noqa: ARG002
                calls.append(tool)
                return lambda: None

        manifest = PluginManifest(name="user-plugin", version="1", tier="user")
        ctx = build_context("user-plugin", host={"tools": Host()}, manifest=manifest)
        ctx.tools.register("a-tool")
        assert calls == []

    def test_user_tier_with_permission_reaches_host(self):
        calls = []

        class Host:
            def plugin_register_tool(self, tool, source=""):  # noqa: ARG002
                calls.append(tool)
                return lambda: None

        manifest = PluginManifest(
            name="user-plugin", version="1", tier="user", permissions=["tools:register"]
        )
        ctx = build_context("user-plugin", host={"tools": Host()}, manifest=manifest)
        ctx.tools.register("a-tool")
        assert calls == ["a-tool"]

    def test_absent_host_degrades_to_noop(self):
        manifest = PluginManifest(
            name="user-plugin", version="1", tier="user", permissions=["tools:register"]
        )
        ctx = build_context("user-plugin", host={}, manifest=manifest)
        undo = ctx.tools.register("a-tool")
        assert callable(undo)

    def test_undo_sink_receives_registrations(self):
        recorded = []

        class Host:
            def plugin_register_tool(self, tool, source=""):  # noqa: ARG002
                return lambda: None

        manifest = PluginManifest(
            name="user-plugin", version="1", tier="user", permissions=["tools:register"]
        )
        ctx = build_context(
            "user-plugin",
            host={"tools": Host()},
            manifest=manifest,
            undo_sink=lambda name, _undo: recorded.append(name),
        )
        ctx.tools.register("a-tool")
        assert recorded == ["user-plugin"]


class TestIntegrityPersistence:
    """Integrity hashes must persist across runs to be meaningful."""

    @pytest.fixture(autouse=True)
    def _isolate_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(integrity_mod, "_CACHE_DIR", tmp_path)
        monkeypatch.setattr(integrity_mod, "_CACHE_FILE", tmp_path / "integrity.json")
        monkeypatch.delenv("ENCRE_INTEGRITY_CHECK", raising=False)
        yield

    def _pkg(self, tmp_path: Path) -> Path:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "plugin.py").write_text("x = 1\n", "utf-8")
        return pkg

    def test_first_run_persists_hash(self, tmp_path):
        pkg = self._pkg(tmp_path)
        manifest = PluginManifest(name="p1", version="1", tier="user")
        assert integrity_mod.verify_integrity(manifest, pkg) is True
        cache = json.loads(integrity_mod._CACHE_FILE.read_text("utf-8"))
        assert "p1" in cache

    def test_mismatch_is_detected(self, tmp_path):
        pkg = self._pkg(tmp_path)
        manifest = PluginManifest(name="p2", version="1", tier="user")
        assert integrity_mod.verify_integrity(manifest, pkg) is True
        (pkg / "plugin.py").write_text("x = 2\n", "utf-8")
        assert integrity_mod.verify_integrity(manifest, pkg) is False

    def test_trusted_tier_exempt(self, tmp_path):
        pkg = self._pkg(tmp_path)
        manifest = PluginManifest(name="p3", version="1", tier="system-default")
        assert integrity_mod.verify_integrity(manifest, pkg) is True
        assert not integrity_mod._CACHE_FILE.exists()

    def test_env_disable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENCRE_INTEGRITY_CHECK", "0")
        pkg = self._pkg(tmp_path)
        manifest = PluginManifest(name="p4", version="1", tier="user")
        assert integrity_mod.verify_integrity(manifest, pkg) is True
        assert not integrity_mod._CACHE_FILE.exists()


class _FakeTransport:
    """Records ``call_rpc`` invocations for proxy reconstruction tests."""

    def __init__(self):
        self.calls = []

    def call_rpc(self, method, params=None):
        self.calls.append((method, params))
        return {"echo": method}


class TestTransportReconstruction:
    """Process/remote skills and hooks become live proxies, not dead dicts."""

    def test_skill_from_meta_builds_definition(self):
        transport = _FakeTransport()
        meta = {
            "name": "my-skill",
            "description": "does things",
            "aliases": ["ms"],
            "context": "inline",
            "source": "bundled",
            "body": "prompt body",
        }
        skill = _skill_from_meta(meta, transport)
        assert skill.name == "my-skill"
        assert skill.description == "does things"
        assert skill.aliases == ["ms"]
        assert skill.body == "prompt body"

    @pytest.mark.asyncio
    async def test_skill_prompt_proxies_to_rpc(self):
        transport = _FakeTransport()
        skill = _skill_from_meta({"name": "s1", "description": "d"}, transport)
        result = await skill.get_prompt_for_command("arg", {})
        assert result == {"echo": "get_skill_prompt"}
        method, params = transport.calls[0]
        assert method == "get_skill_prompt"
        assert params["name"] == "s1"

    def test_hook_from_meta_returns_callable(self):
        transport = _FakeTransport()
        handler = _hook_from_meta("run_started", {"handler_ref": "pkg:on_run"}, transport)
        assert callable(handler)
        assert handler.__name__ == "plugin_hook_run_started"
        assert handler.__qualname__ == "pkg:on_run"

    def test_hook_invocation_proxies_to_rpc(self):
        transport = _FakeTransport()
        handler = _hook_from_meta("run_started", {}, transport)
        handler("a", k="v")
        method, params = transport.calls[0]
        assert method == "execute_hook"
        assert params["event_type"] == "run_started"
        assert params["args"] == ["a"]
        assert params["kwargs"] == {"k": "v"}


class TestPluginWsHandlerIsolation:
    """WS handlers are namespaced per plugin.

    A UI action names its owning plugin; without this boundary the last
    registration for a message type silently replaced every earlier plugin's
    handler, so one plugin's action could execute another plugin's code.
    """

    def test_same_message_type_does_not_collide(self):
        host = PluginHost()
        a = lambda *_: "a"  # noqa: E731
        b = lambda *_: "b"  # noqa: E731
        undo_a = host.plugin_register_ws_handler("plugin_ui_action", a, source="plugin-a")
        undo_b = host.plugin_register_ws_handler("plugin_ui_action", b, source="plugin-b")

        assert host.get_ws_handler("plugin_ui_action", "plugin-a") is a
        assert host.get_ws_handler("plugin_ui_action", "plugin-b") is b
        assert host.get_ws_handler("plugin_ui_action", "plugin-c") is None

        undo_a()
        assert host.get_ws_handler("plugin_ui_action", "plugin-a") is None
        assert host.get_ws_handler("plugin_ui_action", "plugin-b") is b
        undo_b()
        assert host.get_ws_handler("plugin_ui_action", "plugin-b") is None

    def test_frames_without_plugin_keep_legacy_lookup(self):
        host = PluginHost()
        handler = lambda *_: None  # noqa: E731
        undo = host.plugin_register_ws_handler("custom_message", handler)

        assert host.get_ws_handler("custom_message") is handler
        assert host.get_ws_handler("custom_message", "plugin-a") is None
        undo()
        assert host.get_ws_handler("custom_message") is None

    def test_owner_undo_cannot_remove_another_plugin(self):
        host = PluginHost()
        a = lambda *_: "a"  # noqa: E731
        b = lambda *_: "b"  # noqa: E731
        undo_a = host.plugin_register_ws_handler("shared", a, source="plugin-a")
        host.plugin_register_ws_handler("shared", b, source="plugin-b")
        # A stale undo captured from plugin A must only touch A's namespace.
        undo_a()
        assert host.get_ws_handler("shared", "plugin-b") is b

    def test_ui_provider_entries_keep_source_identity(self):
        host = PluginHost()

        class Provider:
            name = "same-name"
            entry_point = "ui/index.html"
            mount_point = "panel"

        host.plugin_register_ui_provider(Provider(), source="plugin-a")
        host.plugin_register_ui_provider(Provider(), source="plugin-b")
        entries = host.ui_provider_entries

        assert [(e["source"], e["provider"].name) for e in entries] == [
            ("plugin-a", "same-name"),
            ("plugin-b", "same-name"),
        ]


class TestManifestUiAssets:
    """Declarative UI specs may point at CLI-compiled files."""

    def _manifest(self, root):
        return PluginManifest(name="p", version="1", pkg_dir=str(root))

    def test_js_module_path_is_materialized(self, tmp_path):
        pkg = tmp_path / "pkg"
        (pkg / "ui").mkdir(parents=True)
        (pkg / "ui" / "plugin.js").write_text("globalThis.__loaded = true;", encoding="utf-8")
        reg = PluginRegistry()
        spec = reg._materialize_ui_spec(self._manifest(pkg), "js_module", {"path": "ui/plugin.js"}, "p")
        assert spec is not None
        assert spec["code"] == "globalThis.__loaded = true;"

    def test_template_path_is_materialized(self, tmp_path):
        pkg = tmp_path / "pkg"
        (pkg / "ui").mkdir(parents=True)
        (pkg / "ui" / "row.html").write_text("<div>{{name}}</div>", encoding="utf-8")
        reg = PluginRegistry()
        spec = reg._materialize_ui_spec(self._manifest(pkg), "ui_override", {"slot": "session.row", "template_path": "ui/row.html"}, "p")
        assert spec is not None
        assert spec["template"] == "<div>{{name}}</div>"

    def test_path_escape_is_rejected(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (tmp_path / "secret.js").write_text("secret", encoding="utf-8")
        reg = PluginRegistry()
        spec = reg._materialize_ui_spec(self._manifest(pkg), "js_module", {"path": "../secret.js"}, "p")
        assert spec is None

    def test_missing_asset_returns_none(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        reg = PluginRegistry()
        assert reg._materialize_ui_spec(self._manifest(pkg), "js_module", {"path": "ui/nope.js"}, "p") is None


class TestHardDependencies:
    """Dependencies are now a contract, not an activation hint."""

    def _registry(self):
        reg = PluginRegistry()
        dep = PluginManifest(name="base", version="1.2.0", tier="system-default")
        reg._manifests["base"] = dep
        reg._states["base"] = PluginState.ACTIVATED
        return reg

    def test_satisfied(self):
        reg = self._registry()
        m = PluginManifest(name="child", version="1", dependencies=["base>=1.0.0"])
        assert reg._dependency_problem(m) == ""

    def test_missing(self):
        reg = self._registry()
        m = PluginManifest(name="child", version="1", dependencies=["missing"])
        assert "missing" in reg._dependency_problem(m)

    def test_inactive(self):
        reg = self._registry()
        reg._states["base"] = PluginState.DISCOVERED
        m = PluginManifest(name="child", version="1", dependencies=["base"])
        assert "not activated" in reg._dependency_problem(m)

    def test_version_mismatch(self):
        reg = self._registry()
        m = PluginManifest(name="child", version="1", dependencies=["base>=2"])
        assert "requires" in reg._dependency_problem(m)

    def test_malformed(self):
        reg = self._registry()
        m = PluginManifest(name="child", version="1", dependencies=[">=1.0"])
        assert "malformed" in reg._dependency_problem(m)


class TestSharedNamespaceArbitration:
    """One plugin cannot silently replace another's shared-name entry."""

    def test_command_first_owner_wins(self):
        host = PluginHost()
        undo_a = host.plugin_register_command({"name": "demo"}, source="a")
        host.plugin_register_command({"name": "demo"}, source="b")
        assert host.commands["demo"]["plugin"] == "a"
        undo_a()
        assert "demo" not in host.commands

    def test_backend_first_owner_wins(self):
        host = PluginHost()
        first = type("First", (), {})
        second = type("Second", (), {})
        host.plugin_register_backend("x", first, source="a")
        host.plugin_register_backend("x", second, source="b")
        assert host._backends["x"] is first

    def test_sub_agent_and_template(self):
        host = PluginHost()
        host.plugin_register_sub_agent({"name": "s"}, source="a")
        host.plugin_register_sub_agent({"name": "s"}, source="b")
        host.plugin_register_template("t", "one", source="a")
        host.plugin_register_template("t", "two", source="b")
        assert host._sub_agents["s"] is host._sub_agents["s"]
        assert host._templates["t"] == "one"


class TestEventIsolation:
    """Each subscriber gets an isolated copy with a trusted source."""

    def test_payload_copy_and_source(self):
        host = PluginHost()
        seen: list[dict] = []

        def first(payload):
            payload["value"] = 999

        def second(payload):
            seen.append(dict(payload))

        host.subscribe("demo", first, source="a")
        host.subscribe("demo", second, source="b")
        host.emit("demo", {"value": 1}, source="trusted")
        assert seen == [{"value": 1, "__source__": "trusted"}]


class TestPluginStateStore:
    def test_isolated_files_and_persistence(self, tmp_path):
        from encre.plugins.state import PluginStateStore

        a = PluginStateStore("a", path=tmp_path / "a.json")
        b = PluginStateStore("b", path=tmp_path / "b.json")
        a.set("token", {"v": 1})
        assert b.get("token") is None
        assert PluginStateStore("a", path=tmp_path / "a.json").get("token") == {"v": 1}

    def test_size_cap(self, tmp_path):
        from encre.plugins.state import PluginStateStore

        store = PluginStateStore("big", path=tmp_path / "big.json")
        with pytest.raises(ValueError):
            store.set("blob", "x" * (1024 * 1024 + 1))


class TestToolUndo:
    def test_undo_removes_live_tool_by_name(self):
        class Tool:
            name = "demo-tool"

        class Registry:
            def __init__(self):
                self.items = {}

            def register(self, tool):
                self.items[tool.name] = tool

            def get(self, name):
                return self.items.get(name)

            def unregister(self, name):
                return self.items.pop(name, None)

        registry = Registry()
        host = PluginHost(tool_registry=registry)
        tool = Tool()
        undo = host.plugin_register_tool(tool, source="p")
        assert registry.get("demo-tool") is tool
        undo()
        assert registry.get("demo-tool") is None


class TestIntegrityUiCoverage:
    def test_ui_assets_invalidate_hash(self, tmp_path):
        from encre.plugins.integrity import compute_package_hash

        (tmp_path / "plugin.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "ui").mkdir()
        target = tmp_path / "ui" / "plugin.js"
        target.write_text("console.log(1)\n", encoding="utf-8")
        first = compute_package_hash(tmp_path)
        target.write_text("console.log(2)\n", encoding="utf-8")
        second = compute_package_hash(tmp_path)
        assert first != second


class TestStrongTransportDefault:
    """Untrusted plugins cannot silently fall back to in-process execution."""

    def test_forced_process_without_factory_fails(self):
        from encre.plugins.transport import create_transport

        manifest = PluginManifest(name="untrusted", version="1", tier="user")
        with pytest.raises(RuntimeError):
            create_transport(manifest, plugin=object(), mode="process")

    def test_forced_process_with_factory_returns_process_transport(self):
        from encre.plugins.transport import ProcessTransport, create_transport

        manifest = PluginManifest(
            name="untrusted",
            version="1",
            tier="user",
            factory="demo.plugin:create_plugin",
        )
        transport = create_transport(manifest, plugin=object(), mode="process")
        assert isinstance(transport, ProcessTransport)


class TestDependencyCascade:
    def test_active_dependents_are_found(self):
        reg = PluginRegistry()
        base = PluginManifest(name="base", version="1", tier="system-default")
        child = PluginManifest(name="child", version="1", dependencies=["base>=1"])
        reg._manifests = {"base": base, "child": child}
        reg._states = {"base": PluginState.ACTIVATED, "child": PluginState.ACTIVATED}
        assert reg._active_dependents("base") == ["child"]
