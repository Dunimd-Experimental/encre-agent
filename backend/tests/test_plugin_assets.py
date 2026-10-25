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

"""Tests for the ``/plugin-assets`` route (encre.server.app.EncreServer).

The route is how a plugin's own shipped files reach the renderer, and the only
place that decides what a third-party package may expose over it: its ``ui/``
front-end bundle, and the pictures under its package root (where a declared
``icon = "assets/icon.svg"`` lives).  These tests pin the roots, the allow-list that
keeps the icon root from becoming a way to read a package's source, the
traversal refusal, and the unknown-plugin fall-through.
"""

from pathlib import Path

from encre.server.app import EncreServer


class _Served:
    """The asset-serving half of the server, with a hand-built allow-list.

    ``_serve_plugin_asset`` reads only the name -> package directory map and
    the two allow-lists, so a stub holding those is the whole surface under
    test -- no port, no plugin registry, no event loop.
    """

    _ASSET_IMAGE_SUFFIXES = EncreServer._ASSET_IMAGE_SUFFIXES
    _serve_plugin_asset = EncreServer._serve_plugin_asset

    def __init__(self, dirs: dict[str, Path]) -> None:
        self._plugin_asset_dirs = dirs


def _fixture(tmp_path: Path) -> tuple[_Served, Path]:
    """One plugin package with a ui bundle, an icon, a README and decoys."""
    pkg = tmp_path / "ea-skill-demo"
    (pkg / "ui").mkdir(parents=True)
    (pkg / "assets").mkdir(parents=True)
    (pkg / "ui" / "index.html").write_text("<html></html>", encoding="utf-8")
    (pkg / "assets" / "icon.svg").write_text("<svg></svg>", encoding="utf-8")
    (pkg / "assets" / "notes.txt").write_text("not a picture", encoding="utf-8")
    (pkg / "CHANGELOG.md").write_text("not the readme", encoding="utf-8")
    (pkg / "README.md").write_text("# Demo skill\n", encoding="utf-8")
    (pkg / "plugin.py").write_text("SECRET = 1", encoding="utf-8")
    return _Served({"ea-skill-demo": pkg}), pkg


class TestPluginAssetRoute:
    """What one route is allowed to hand out."""

    def test_verify_ui_bundle_still_served(self, tmp_path):
        """The pre-existing ui/ root keeps its ui-relative addressing."""
        served, _ = _fixture(tmp_path)
        status, body, headers = served._serve_plugin_asset("/plugin-assets/ea-skill-demo/index.html")
        assert status == 200
        assert body == b"<html></html>"
        assert headers["Content-Type"] == "text/html"

    def test_verify_shipped_icon_image_is_served(self, tmp_path):
        """A plugin's own icon file resolves through the package root."""
        served, _ = _fixture(tmp_path)
        status, body, headers = served._serve_plugin_asset("/plugin-assets/ea-skill-demo/assets/icon.svg")
        assert status == 200
        assert body == b"<svg></svg>"
        assert headers["Content-Type"] == "image/svg+xml"

    def test_verify_markdown_is_not_offered(self, tmp_path):
        """No text file is readable from the package root, README included."""
        served, _ = _fixture(tmp_path)
        for relpath in ("README.md", "CHANGELOG.md"):
            status, _body, _headers = served._serve_plugin_asset(f"/plugin-assets/ea-skill-demo/{relpath}")
            assert status == 404, relpath

    def test_verify_non_image_in_assets_root_is_not_offered(self, tmp_path):
        """Only pictures are readable from the icon root, never other files."""
        served, _ = _fixture(tmp_path)
        status, _body, _headers = served._serve_plugin_asset("/plugin-assets/ea-skill-demo/assets/notes.txt")
        assert status == 404
        status, _body, _headers = served._serve_plugin_asset("/plugin-assets/ea-skill-demo/plugin.py")
        assert status == 404

    def test_verify_traversal_is_forbidden(self, tmp_path):
        """Escaping the served root is refused, not silently clamped."""
        (tmp_path / "leak.png").write_bytes(b"not inside the package")
        served, _ = _fixture(tmp_path)
        status, _body, _headers = served._serve_plugin_asset("/plugin-assets/ea-skill-demo/assets/../../leak.png")
        assert status == 403

    def test_verify_unknown_plugin_falls_through(self, tmp_path):
        """A name outside the allow-list is not a chance to name a directory."""
        served, _ = _fixture(tmp_path)
        assert served._serve_plugin_asset("/plugin-assets/not-installed/assets/icon.svg") is None

    def test_verify_missing_file_is_404(self, tmp_path):
        """An absent file inside a served root reports missing, not forbidden."""
        served, _ = _fixture(tmp_path)
        status, _body, _headers = served._serve_plugin_asset("/plugin-assets/ea-skill-demo/assets/gone.svg")
        assert status == 404
