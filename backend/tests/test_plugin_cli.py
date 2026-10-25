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

"""Tests for the plugin central-repository CLI.

Neither ``ea-plugin-cli`` nor ``ea-cwh`` is pip-installed in the dev
environment, so the repo source trees are put on ``sys.path`` directly.
Covers the pure publish-policy rules, catalog entry assembly/merge, the
``init``/``catalog`` command round-trips, and the registry integration:
the PR flow is exercised against a simulated local git remote, and the
registry CI's ``tools/regen_catalog.py`` runs with a stubbed PyPI.
``build``/``verify``/``publish`` upload halves shell out to
`python -m build`, venv and twine and are exercised manually.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
for _pkg in (_ROOT / "ea-cwh", _ROOT / "cli"):
    if str(_pkg) not in sys.path:
        sys.path.insert(0, str(_pkg))

import ea_plugin_cli.__main__ as cli_main_mod  # noqa: E402
from ea_plugin_cli.__main__ import main as cli_main  # noqa: E402
from ea_plugin_cli.rules import (  # noqa: E402
    build_catalog_entry,
    find_ea_entrypoint,
    merge_catalog,
    normalize_module,
    read_pyproject,
    validate_for_publish,
)


def _py(dist="ea-plugin-demo", version="0.1.0", *, ep=True, ui_data=False) -> dict:
    project: dict = {"name": dist, "version": version}
    if ep:
        project["entry-points"] = {"ea.plugins": {"demo": "ea_plugin_demo.plugin:create_plugin"}}
    py: dict = {"project": project}
    if ui_data:
        py["tool"] = {"setuptools": {"package-data": {"ea_plugin_demo": ["ui/**/*"]}}}
    return py


def _manifest(**overrides) -> dict:
    m = {
        "name": "demo",
        "version": "0.1.0",
        "description": "A demo plugin",
        "author": "Dunimd Team",
        "license": "Apache-2.0",
        "tier": "user",
        "min_ea_version": "0.5.0",
        "permissions": ["tools:register"],
        "tags": ["demo"],
        "dependencies": [],
        "provides_tools": ["demo_tool"],
    }
    m.update(overrides)
    return m


class TestRules:
    """Publish-policy checks in ``validate_for_publish``."""

    def test_verify_clean_package_passes(self):
        assert validate_for_publish(_py(), _manifest()) == []

    def test_verify_bad_prefix_flagged(self):
        problems = validate_for_publish(_py(dist="demo"), _manifest())
        assert any("ea-plugin-" in p for p in problems)

    def test_verify_missing_entry_point_flagged(self):
        problems = validate_for_publish(_py(ep=False), _manifest())
        assert any("entry-points" in p for p in problems)

    def test_verify_entry_point_key_must_equal_manifest_name(self):
        # py ep key "demo" == manifest name; flip the manifest name to break it.
        problems = validate_for_publish(_py(), _manifest(name="other"))
        assert any("must equal manifest.name" in p for p in problems)

    def test_verify_multi_plugin_wheel_flagged(self):
        assert any("multi-plugin" in p for p in validate_for_publish(_py(), _manifest(), extra_entry_points=1))

    def test_verify_version_mismatch_flagged(self):
        problems = validate_for_publish(_py(version="0.1.0"), _manifest(version="0.2.0"))
        assert any("pyproject version" in p for p in problems)

    def test_verify_non_user_tier_flagged(self):
        problems = validate_for_publish(_py(), _manifest(tier="system-default"))
        assert any("tier" in p for p in problems)

    def test_verify_ui_without_prebuilt_assets_flagged(self):
        problems = validate_for_publish(_py(), _manifest(provides_ui=["demo.panel"]))
        assert any("prebuilt" in p for p in problems)

    def test_verify_ui_with_assets_passes(self):
        assert validate_for_publish(_py(ui_data=True), _manifest(provides_ui=["demo.panel"])) == []

    def test_verify_find_entry_point(self):
        assert find_ea_entrypoint(_py()) == ("demo", "ea_plugin_demo.plugin:create_plugin")
        assert find_ea_entrypoint(_py(ep=False)) is None

    def test_verify_normalize_module(self):
        assert normalize_module("ea-plugin-pg-vector") == "ea_plugin_pg_vector"


class TestCatalogEntry:
    """Entry assembly and document merging."""

    def test_verify_build_entry_maps_fields(self):
        entry = build_catalog_entry(
            _manifest(), pypi_package="ea-plugin-demo",
            artifacts={"d.whl": {"sha256": "a" * 64, "size": 10}}, published_at="2026-10-01T00:00:00+00:00")
        assert entry["name"] == "demo"
        assert entry["pypi_package"] == "ea-plugin-demo"
        assert entry["provides"] == {"tools": ["demo_tool"]}
        assert entry["min_encre_version"] == "0.5.0"
        assert entry["yanked"] is False

    def test_verify_merge_sorts_and_stamps_schema(self):
        doc = merge_catalog([{"name": "z"}, {"name": "a"}])
        assert [p["name"] for p in doc["plugins"]] == ["a", "z"]
        from ea_cwh import SCHEMA_VERSION

        assert doc["schema_version"] == SCHEMA_VERSION


class TestInitCommand:
    """``encre-plugin init`` scaffolds a package the rules accept."""

    def test_verify_scaffold_loads_and_passes_entry_point_check(self, tmp_path):
        cli_main(["init", "demo", "--dir", str(tmp_path)])
        pkg = tmp_path / "ea-plugin-demo"
        assert (pkg / "pyproject.toml").exists()
        assert (pkg / "ea_plugin_demo" / "plugin.py").exists()
        assert (pkg / "ea_plugin_demo" / "ui").is_dir()
        assert (pkg / "ea_plugin_demo" / "ui" / "src" / "index.ts").is_file()
        assert (pkg / "ea_plugin_demo" / "ui" / "vendor" / "encre-plugin-ui-sdk" / "contract.json").is_file()
        py = read_pyproject(pkg)
        assert py["project"]["name"] == "ea-plugin-demo"
        assert find_ea_entrypoint(py) == ("demo", "ea_plugin_demo.plugin:create_plugin")


class TestCatalogCommand:
    """``encre-plugin catalog regen`` validates before writing."""

    def _registry(self, tmp_path: Path, entry: dict) -> Path:
        reg = tmp_path / "registry"
        (reg / "catalog.d").mkdir(parents=True)
        (reg / "catalog.d" / f"{entry['name']}.json").write_text(json.dumps(entry), encoding="utf-8")
        return reg

    def test_verify_regen_writes_valid_document(self, tmp_path):
        entry = build_catalog_entry(
            _manifest(), pypi_package="ea-plugin-demo",
            artifacts={"d.whl": {"sha256": "a" * 64, "size": 10}}, published_at="2026-10-01T00:00:00+00:00")
        reg = self._registry(tmp_path, entry)
        out = tmp_path / "catalog.json"
        cli_main(["catalog", "regen", "--registry", str(reg), "--out", str(out)])
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["plugins"][0]["pypi_package"] == "ea-plugin-demo"

    def test_verify_regen_refuses_invalid_entry(self, tmp_path):
        entry = build_catalog_entry(
            _manifest(), pypi_package="ea-plugin-demo",
            artifacts={"d.whl": {"sha256": "not-a-hash", "size": 10}}, published_at="2026-10-01T00:00:00+00:00")
        reg = self._registry(tmp_path, entry)
        with pytest.raises(SystemExit):
            cli_main(["catalog", "regen", "--registry", str(reg), "--out", str(tmp_path / "catalog.json")])


# ── registry integration: CLI PR flow + CI tooling ───────────────────


def _entry(name="demo", version="0.1.0", pkg="ea-plugin-demo") -> dict:
    return build_catalog_entry(
        _manifest(name=name, version=version), pypi_package=pkg,
        artifacts={"d.whl": {"sha256": "a" * 64, "size": 10}},
        published_at="2026-10-01T00:00:00+00:00")


def _git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, check=True)
    return r.stdout


@pytest.fixture()
def origin_and_work(tmp_path):
    """A bare 'origin' with one commit on main plus a working clone."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "--bare", "--initial-branch=main", str(origin), cwd=tmp_path)
    work = tmp_path / "work"
    _git("clone", "--quiet", str(origin), str(work), cwd=tmp_path)
    (work / "catalog.d").mkdir()
    (work / "catalog.d" / ".gitkeep").write_text("", encoding="utf-8")
    _git("config", "user.name", "Test", cwd=work)
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "seed", cwd=work)
    _git("push", "--quiet", "origin", "main", cwd=work)
    return origin, work


class TestRegistryPR:
    """``_registry_pr`` / ``_resolve_registry`` against a simulated remote."""

    def test_verify_pr_branch_carries_only_the_entry(self, origin_and_work):
        origin, work = origin_and_work
        # Dirty worktree state must NOT leak into the PR: the branch is cut
        # from origin/main before the entry is written.
        (work / "stray.txt").write_text("junk", encoding="utf-8")
        cli_main_mod._registry_pr(work, _entry())
        refs = _git("ls-remote", "--heads", str(origin), cwd=origin)
        assert "refs/heads/publish/demo-0.1.0" in refs
        shown = json.loads(_git("show", "publish/demo-0.1.0:catalog.d/demo.json", cwd=work))
        assert shown["pypi_package"] == "ea-plugin-demo"
        assert "stray.txt" not in _git("ls-tree", "--name-only", "publish/demo-0.1.0", cwd=work)

    def test_verify_resolve_registry_rejects_non_url(self):
        with pytest.raises(SystemExit):
            cli_main_mod._resolve_registry(
                SimpleNamespace(registry_dir=None, registry_url="evil repo; rm"))

    def test_verify_resolve_registry_rejects_foreign_dir(self, tmp_path):
        with pytest.raises(SystemExit):
            cli_main_mod._resolve_registry(
                SimpleNamespace(registry_dir=str(tmp_path), registry_url="https://example.com/r.git"))

    def test_verify_resolve_registry_clones_then_resyncs(self, origin_and_work, tmp_path, monkeypatch):
        origin, _work = origin_and_work
        cache = tmp_path / "cache" / "registry"
        monkeypatch.setattr(cli_main_mod, "_registry_cache_dir", lambda: cache)
        args = SimpleNamespace(registry_dir=None, registry_url=f"file:///{origin.as_posix()}")
        reg = cli_main_mod._resolve_registry(args)
        assert reg == cache
        assert (cache / "catalog.d" / ".gitkeep").exists()
        # A second call force-resyncs the cached clone instead of reusing
        # stale local edits.
        (cache / "catalog.d" / ".gitkeep").write_text("dirty", encoding="utf-8")
        reg2 = cli_main_mod._resolve_registry(args)
        assert (reg2 / "catalog.d" / ".gitkeep").read_text(encoding="utf-8") == ""


class TestRegistryTool:
    """``ea-cwh/tools/regen_catalog.py`` — the CI's zero-dependency brain."""

    @pytest.fixture(scope="class")
    def tool(self):
        path = _ROOT / "ea-cwh" / "tools" / "regen_catalog.py"
        spec = importlib.util.spec_from_file_location("regen_catalog", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_verify_merge_produces_valid_document(self, tool, tmp_path):
        ed = tmp_path / "catalog.d"
        ed.mkdir()
        (ed / "demo.json").write_text(json.dumps(_entry()), encoding="utf-8")
        doc = tool.merge(ed)
        from ea_cwh import validate_catalog

        assert validate_catalog(doc) == []
        assert doc["plugins"][0]["name"] == "demo"

    def test_verify_check_pypi_subset_hits_only_requested_entries(self, tool, monkeypatch):
        doc = {"plugins": [
            _entry(),
            _entry(name="other", pkg="ea-plugin-other"),
        ]}
        seen: list[str] = []
        payload = {"urls": [{"filename": "d.whl", "digests": {"sha256": "a" * 64}}]}

        class _Resp:
            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(url, timeout=None):
            del timeout  # accepted for urlopen signature compatibility
            seen.append(str(url))
            return _Resp()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert tool.check_pypi(doc, only=["demo"]) == []
        assert len(seen) == 1 and "ea-plugin-demo" in seen[0]

    def test_verify_check_pypi_digest_mismatch_reported(self, tool, monkeypatch):
        doc = {"plugins": [_entry()]}
        payload = {"urls": [{"filename": "d.whl", "digests": {"sha256": "b" * 64}}]}

        class _Resp:
            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def _stub(url, timeout=None):
            del url, timeout  # urlopen signature compatibility
            return _Resp()

        monkeypatch.setattr("urllib.request.urlopen", _stub)
        problems = tool.check_pypi(doc, only=["demo"])
        assert len(problems) == 1 and "digest mismatch" in problems[0]


class TestPluginUiSdk:
    """Frontend compilation and contract validation."""

    def test_sdk_command_materializes_package(self, tmp_path):
        out = tmp_path / "sdk"
        cli_main(["sdk", "--out", str(out)])
        assert (out / "index.js").is_file()
        contract = json.loads((out / "contract.json").read_text(encoding="utf-8"))
        assert contract["sdk_version"]
        assert contract["classes"]["ea-ui-card"] == "settings-card"
        assert "session.row" in contract["slots"].values()

    def test_contract_rejects_unknown_class(self, tmp_path):
        out = tmp_path / "plugin.js"
        src = tmp_path / "index.ts"
        src.write_text('const html = `<div class="ea-ui-nope"></div>`;', encoding="utf-8")
        out.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        with pytest.raises(SystemExit):
            cli_main_mod._validate_ui_contract(tmp_path, src, out)

    def test_contract_accepts_known_class_and_rpc(self, tmp_path):
        out = tmp_path / "plugin.js"
        src = tmp_path / "index.ts"
        src.write_text('const html = `<div class="ea-ui-card"></div>`; call("dialog.confirm", "t", "m");', encoding="utf-8")
        out.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        report = cli_main_mod._validate_ui_contract(tmp_path, src, out)
        assert "ea-ui-card" in report["classes"]
        assert "dialog.confirm" in report["rpc"]

    def test_ui_reference_missing_asset_fails(self, tmp_path):
        manifest = {"contributes": {"ui_contributions": [{"kind": "js_module", "spec": {"path": "ui/plugin.js"}}]}}
        with pytest.raises(SystemExit):
            cli_main_mod._validate_ui_references(manifest, tmp_path)

    def test_contract_rejects_unknown_rpc(self, tmp_path):
        out = tmp_path / "plugin.js"
        src = tmp_path / "index.ts"
        src.write_text('call("dialog.nope", "t");', encoding="utf-8")
        out.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        with pytest.raises(SystemExit):
            cli_main_mod._validate_ui_contract(tmp_path, src, out)

    def test_wheel_ui_asset_check(self, tmp_path):
        wheel = tmp_path / "demo.whl"
        with __import__("zipfile").ZipFile(wheel, "w") as zf:
            zf.writestr("ea_plugin_demo/ui/plugin.js", "ok")
            zf.writestr("ea_plugin_demo/ui/row.html", "<div></div>")
        manifest = {"contributes": {"ui_contributions": [
            {"kind": "js_module", "spec": {"path": "ui/plugin.js"}},
            {"kind": "ui_override", "spec": {"slot": "session.row", "template_path": "ui/row.html"}},
        ]}}
        cli_main_mod._verify_wheel_ui_assets(wheel, manifest)

    def test_ui_reference_finds_module_asset(self, tmp_path):
        module = tmp_path / "ea_plugin_demo"
        (module / "ui").mkdir(parents=True)
        (module / "ui" / "plugin.js").write_text("ok", encoding="utf-8")
        manifest = {"contributes": {"ui_contributions": [{"kind": "js_module", "spec": {"path": "ui/plugin.js"}}]}}
        cli_main_mod._validate_ui_references(manifest, tmp_path)
