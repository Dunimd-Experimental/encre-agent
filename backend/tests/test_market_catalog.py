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

"""Tests for the market catalog service (core/encre/market_catalog.py).

The service is exercised with a fake ``ea_cwh`` module injected into
``sys.modules`` and ``ENCRE_DATA_DIR`` pointed at a tmp path, so the
read → cache → fallback ladder, fingerprint change detection and the
frozen-build upgrade guard are all covered without network or pip.
"""

import asyncio
import io
import json
import subprocess
import sys
import types
import zipfile

import pytest
from encre import market_catalog


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Isolate the data tree (cache file) per test."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def no_index(monkeypatch):
    """Ensure the real ea_cwh module is never picked up (tests fake it)."""
    monkeypatch.setitem(sys.modules, "ea_cwh", None)


def _fake_ea_cwh(monkeypatch, plugins, version="0.1.0", generated="2026-10-01T00:00:00+00:00", invalid=False):
    mod = types.ModuleType("ea_cwh")

    def catalog():
        if invalid:
            raise ValueError("invalid market catalog: boom")
        return {"schema_version": 1, "generated_at": generated, "plugins": plugins}

    mod.catalog = catalog  # type: ignore[attr-defined]
    mod.index_version = lambda: version  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ea_cwh", mod)
    return mod


class TestReadIndex:
    """``read_index`` wraps the installed-package read."""

    def test_verify_returns_none_without_package(self):
        assert market_catalog.read_index() is None

    def test_verify_returns_none_on_invalid_document(self, monkeypatch):
        _fake_ea_cwh(monkeypatch, [], invalid=True)
        assert market_catalog.read_index() is None

    def test_verify_returns_payload(self, monkeypatch):
        _fake_ea_cwh(monkeypatch, [{"name": "demo"}])
        out = market_catalog.read_index()
        assert out == {"index_version": "0.1.0", "generated_at": "2026-10-01T00:00:00+00:00", "plugins": [{"name": "demo"}]}


class TestLoadCatalogLadder:
    """index → cache → empty, with the cache mirrored on success."""

    def test_verify_index_source_writes_cache(self, monkeypatch, data_dir):
        _fake_ea_cwh(monkeypatch, [{"name": "demo"}])
        payload = market_catalog.load_catalog()
        assert payload["source"] == "index"
        cache = data_dir / "market_catalog.json"
        assert json.loads(cache.read_text(encoding="utf-8"))["plugins"][0]["name"] == "demo"

    def test_verify_falls_back_to_cache(self, data_dir):
        (data_dir / "market_catalog.json").write_text(
            json.dumps({"index_version": "0.0.9", "generated_at": "g", "plugins": [{"name": "cached"}]}),
            encoding="utf-8")
        payload = market_catalog.load_catalog()
        assert payload["source"] == "cache"
        assert payload["plugins"][0]["name"] == "cached"

    def test_verify_empty_when_nothing_available(self):
        payload = market_catalog.load_catalog()
        assert payload == {"source": "none", "index_version": "", "generated_at": "", "plugins": []}

    def test_verify_corrupt_cache_is_ignored(self, data_dir):
        (data_dir / "market_catalog.json").write_text("{not json", encoding="utf-8")
        assert market_catalog.load_catalog()["source"] == "none"


class TestRefresh:
    """Upgrade guard and fingerprint change detection."""

    def test_verify_frozen_build_never_shells_out_to_pip(self, monkeypatch):
        # Frozen refresh goes through the HTTPS channel; a network failure
        # must simply report "not updated" and must NOT run pip.
        monkeypatch.setattr(sys, "frozen", True, raising=False)

        def boom(_url):
            raise OSError("network down")

        monkeypatch.setattr(market_catalog, "_http_get_json", boom)
        monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("pip was invoked"))
        assert market_catalog.refresh_index() is False

    def test_verify_fingerprint_tracks_version_pair(self):
        payload = {"index_version": "1", "generated_at": "2"}
        assert market_catalog._fingerprint(payload) == ("1", "2")

    @pytest.mark.asyncio
    async def test_verify_on_update_fires_only_on_change(self, monkeypatch):
        calls: list[dict] = []

        async def on_update(payload):
            calls.append(payload)

        refresher = market_catalog.MarketCatalogRefresher(
            on_update=on_update, interval=0.01, initial_delay=0.0)
        _fake_ea_cwh(monkeypatch, [{"name": "a"}], version="1")
        await refresher.start()
        try:
            # Same fingerprint: no broadcast even after a refresh cycle.
            monkeypatch.setattr(market_catalog, "refresh_index", lambda: True)
            await asyncio.sleep(0.1)
            assert calls == []
            # New fingerprint: exactly one broadcast.
            _fake_ea_cwh(monkeypatch, [{"name": "a"}], version="2", generated="later")
            await _sleep_until(lambda: len(calls) == 1)
            assert calls[0]["index_version"] == "2"
        finally:
            await refresher.stop()


class TestScannerAcceptsCentralInstalls:
    """``ea_scan`` must see unpacked ``ea-plugin-*`` dirs as tier-user packages."""

    def test_verify_iter_yields_deployed_central_package(self, monkeypatch, data_dir):
        from pathlib import Path

        from encre.plugins import ea_scan

        monkeypatch.setattr(ea_scan, "_bundled_source", lambda: None)
        pkg = Path(str(data_dir)) / "plugins" / "ea-plugin-demo"
        (pkg / "ea_plugin_demo").mkdir(parents=True)
        (pkg / "ea_plugin_demo" / "plugin.py").write_text(
            "def create_plugin():\n    return None\n", encoding="utf-8")
        (pkg / "pyproject.toml").write_text(market_catalog._gen_pyproject(
            _catalog_entry("a" * 64), "demo", "ea_plugin_demo.plugin:create_plugin"),
            encoding="utf-8")
        found = [p for p in ea_scan.iter_ea_packages() if p.name == "demo"]
        assert len(found) == 1
        info = found[0]
        assert info.tier == ea_scan.TIER_USER
        assert info.module_name == "ea_plugin_demo"
        assert info.factory_target == "ea_plugin_demo.plugin:create_plugin"
        assert info.pkg_dir == pkg


class TestUpdateDetection:
    """``version_gt`` / ``has_update`` — the market's update discriminator."""

    @pytest.mark.parametrize(("a", "b", "gt"), [
        ("1.2.0", "1.1.9", True),
        ("1.10", "1.9", True),
        ("1.2", "1.2.0", False),
        ("1.2.1", "1.2.2", False),
        ("2.0", "1.9.9", True),
        ("", "", False),
    ])
    def test_verify_version_gt(self, a, b, gt):
        assert market_catalog.version_gt(a, b) is gt

    def test_verify_has_update_returns_newer_entry(self, monkeypatch):
        plugins = [{"name": "demo", "version": "2.0.0", "pypi_package": "ea-plugin-demo"}]
        _fake_ea_cwh(monkeypatch, plugins)
        assert market_catalog.has_update("demo", "1.0.0") == plugins[0]
        assert market_catalog.has_update("demo", "2.0.0") is None
        assert market_catalog.has_update("absent", "1.0.0") is None

    def test_verify_yanked_never_counts_as_update(self, monkeypatch):
        _fake_ea_cwh(monkeypatch, [{"name": "demo", "version": "9.9.9", "yanked": True}])
        assert market_catalog.has_update("demo", "1.0.0") is None


_WHEEL_NAME = "ea_plugin_demo-0.1.0-py3-none-any.whl"


def _wheel_bytes(extra_member: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ea_plugin_demo/__init__.py", "")
        zf.writestr("ea_plugin_demo/plugin.py", "def create_plugin():\n    return None\n")
        zf.writestr("ea_plugin_demo-0.1.0.dist-info/METADATA",
                    "Name: ea-plugin-demo\nVersion: 0.1.0\n")
        zf.writestr("ea_plugin_demo-0.1.0.dist-info/entry_points.txt",
                    "[ea.plugins]\ndemo = ea_plugin_demo.plugin:create_plugin\n")
        if extra_member:
            zf.writestr(extra_member, "x")
    return buf.getvalue()


def _catalog_entry(digest: str) -> dict:
    return {
        "name": "demo", "pypi_package": "ea-plugin-demo", "version": "0.1.0",
        "description": "A demo plugin", "author": "Dunimd Team", "license": "Apache-2.0",
        "tags": ["demo"], "permissions": ["tools:register"],
        "provides": {"tools": ["demo_tool"]}, "min_encre_version": "0.5.0",
        "artifacts": {_WHEEL_NAME: {"sha256": digest, "size": 1}},
    }


class TestFrozenInstall:
    """Pip-less central install: download → double digest gate → unpack."""

    def _setup(self, monkeypatch, wheel: bytes):
        import hashlib

        digest = hashlib.sha256(wheel).hexdigest()
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(market_catalog, "_http_get_json",
                            lambda _url: {"urls": [{"filename": _WHEEL_NAME,
                                                   "digests": {"sha256": digest},
                                                   "url": "https://x/" + _WHEEL_NAME}]})
        monkeypatch.setattr(market_catalog, "_http_get_bytes", lambda _url: wheel)
        return digest

    def test_verify_unpacks_into_scan_layout(self, monkeypatch, data_dir):
        import tomllib
        from pathlib import Path

        wheel = _wheel_bytes()
        entry = _catalog_entry(self._setup(monkeypatch, wheel))
        market_catalog.install_from_index(entry)
        dest = Path(str(data_dir)) / "plugins" / "ea-plugin-demo"
        assert (dest / "ea_plugin_demo" / "plugin.py").is_file()
        py = tomllib.loads((dest / "pyproject.toml").read_text(encoding="utf-8"))
        assert py["project"]["name"] == "demo"
        assert py["project"]["entry-points"]["ea.plugins"]["demo"] == "ea_plugin_demo.plugin:create_plugin"
        assert py["tool"]["ea"]["tier"] == "user"
        assert py["tool"]["ea"]["provides_tools"] == ["demo_tool"]
        # staging dir cleaned up
        assert not any(Path(str(data_dir / "plugins")).glob(".*staging*"))

    def test_verify_digest_mismatch_refuses(self, monkeypatch):
        wheel = _wheel_bytes()
        self._setup(monkeypatch, wheel)
        # The index promises an all-zero digest; PyPI attests the real one.
        with pytest.raises(RuntimeError, match="digest"):
            market_catalog.install_from_index(_catalog_entry("0" * 64))

    def test_verify_pypi_attestation_change_refuses(self, monkeypatch):
        import hashlib

        wheel = _wheel_bytes()
        self._setup(monkeypatch, wheel)
        # The index promises the real digest, but PyPI now serves something else.
        monkeypatch.setattr(market_catalog, "_http_get_json",
                            lambda _url: {"urls": [{"filename": _WHEEL_NAME,
                                                   "digests": {"sha256": "b" * 64},
                                                   "url": "https://x/whl"}]})
        with pytest.raises(RuntimeError, match="PyPI"):
            market_catalog.install_from_index(_catalog_entry(hashlib.sha256(wheel).hexdigest()))

    def test_verify_traversal_member_refuses(self, monkeypatch):
        wheel = _wheel_bytes(extra_member="../evil.py")
        digest = self._setup(monkeypatch, wheel)
        with pytest.raises(RuntimeError, match="unsafe path"):
            market_catalog.install_from_index(_catalog_entry(digest))


class TestFrozenRefresh:
    """HTTPS index refresh writes the cache frozen load_catalog prefers."""

    def _index_wheel(self) -> tuple:
        import hashlib

        doc = {"schema_version": 1, "generated_at": "g1",
               "plugins": [{"name": "demo", "version": "1.0.0"}]}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("ea_cwh/catalog.json", json.dumps(doc))
            zf.writestr("ea_cwh/__init__.py", "")
        raw = buf.getvalue()
        return raw, doc, hashlib.sha256(raw).hexdigest()

    def test_verify_refresh_updates_cache(self, monkeypatch):
        raw, doc, digest = self._index_wheel()
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setitem(sys.modules, "ea_cwh",
                            types.SimpleNamespace(validate_catalog=lambda _d: []))
        monkeypatch.setattr(market_catalog, "_http_get_json", lambda _url: {
            "info": {"version": "9.9.9"},
            "urls": [{"filename": "ea_cwh-9.9.9-py3-none-any.whl",
                      "digests": {"sha256": digest}, "url": "https://x/whl"}]})
        monkeypatch.setattr(market_catalog, "_http_get_bytes", lambda _url: raw)
        assert market_catalog.refresh_index() is True
        payload = market_catalog.load_catalog()
        assert payload["source"] == "cache"
        assert payload["index_version"] == "9.9.9"
        assert payload["plugins"] == doc["plugins"]

    def test_verify_current_version_skips_download(self, monkeypatch, data_dir):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        (data_dir / "market_catalog.json").write_text(
            json.dumps({"index_version": "9.9.9", "generated_at": "g", "plugins": []}),
            encoding="utf-8")
        monkeypatch.setattr(market_catalog, "_http_get_json", lambda _url: {
            "info": {"version": "9.9.9"},
            "urls": [{"filename": "w.whl", "digests": {"sha256": "a" * 64}, "url": "u"}]})
        monkeypatch.setattr(market_catalog, "_http_get_bytes",
                            lambda _url: pytest.fail("should not download"))
        assert market_catalog.refresh_index() is False


async def _sleep_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    pytest.fail("condition not reached in time")
