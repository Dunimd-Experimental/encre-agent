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

"""Tests for the ea-cwh central market index package.

The package is not pip-installed in the dev environment, so the module is
loaded straight from its source file (it is stdlib-only, and its
``catalog.json`` lookup keys off ``__file__``, which the file-location
loader preserves).  Covers the shipped document's validity, the entry
schema checks, and the lookup helpers against a temporary catalog.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "ea-cwh" / "ea_cwh" / "__init__.py"


def _load_ea_cwh():
    spec = importlib.util.spec_from_file_location("ea_cwh", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ea_cwh = _load_ea_cwh()


def _entry(**overrides):
    entry = {
        "name": "pgvector",
        "pypi_package": "ea-plugin-pgvector",
        "version": "1.2.0",
        "description": "Vector search",
        "author": "Dunimd Team",
        "tags": ["database"],
        "permissions": ["tools:register"],
        "provides": {"tools": ["vector_search"]},
        "artifacts": {
            "ea_plugin_pgvector-1.2.0-py3-none-any.whl": {
                "sha256": "a" * 64,
                "size": 123,
            }
        },
    }
    entry.update(overrides)
    return entry


def _doc(*entries):
    return {
        "schema_version": ea_cwh.SCHEMA_VERSION,
        "generated_at": "2026-10-01T00:00:00+00:00",
        "registry": "https://pypi.org",
        "plugins": list(entries),
    }


class TestShippedCatalog:
    """The catalog.json that ships inside the wheel must be valid."""

    def test_verify_shipped_catalog_parses_and_validates(self):
        doc = ea_cwh.catalog()
        assert doc["schema_version"] == ea_cwh.SCHEMA_VERSION
        assert isinstance(doc["plugins"], list)

    def test_verify_catalog_path_points_into_package(self):
        assert ea_cwh.catalog_path().name == "catalog.json"
        assert ea_cwh.catalog_path().exists()


class TestValidateCatalog:
    """``validate_catalog`` enforces the supply-chain entry contract."""

    def test_verify_valid_entry_passes(self):
        assert ea_cwh.validate_catalog(_doc(_entry())) == []

    def test_verify_bad_schema_version_flagged(self):
        doc = _doc(_entry())
        doc["schema_version"] = 99
        problems = ea_cwh.validate_catalog(doc)
        assert any("schema_version" in p for p in problems)

    def test_verify_missing_name_flagged(self):
        entry = _entry()
        del entry["name"]
        problems = ea_cwh.validate_catalog(_doc(entry))
        assert any("name is required" in p for p in problems)

    def test_verify_wrong_pypi_prefix_flagged(self):
        problems = ea_cwh.validate_catalog(_doc(_entry(pypi_package="evil-pgvector")))
        assert any("ea-plugin-" in p for p in problems)

    def test_verify_unpinned_version_flagged(self):
        problems = ea_cwh.validate_catalog(_doc(_entry(version=">=1.0")))
        assert any("version" in p for p in problems)

    def test_verify_missing_or_bad_sha256_flagged(self):
        entry = _entry(artifacts={"x.whl": {"size": 1}})
        problems = ea_cwh.validate_catalog(_doc(entry))
        assert any("sha256" in p for p in problems)

    def test_verify_empty_artifacts_flagged(self):
        problems = ea_cwh.validate_catalog(_doc(_entry(artifacts={})))
        assert any("artifacts" in p for p in problems)

    def test_verify_duplicate_names_flagged(self):
        problems = ea_cwh.validate_catalog(_doc(_entry(), _entry()))
        assert any("duplicate" in p for p in problems)

    def test_verify_non_dict_document_flagged(self):
        assert ea_cwh.validate_catalog([]) == ["catalog document must be a JSON object"]


class TestLookupHelpers:
    """``get`` / ``by_pypi_package`` / ``plugins`` over a temp catalog."""

    @pytest.fixture(autouse=True)
    def populated(self, tmp_path, monkeypatch):
        doc = _doc(
            _entry(),
            _entry(name="slack-kit", pypi_package="ea-plugin-slack-kit", version="0.3.1", yanked=True),
        )
        target = tmp_path / "catalog.json"
        target.write_text(json.dumps(doc), encoding="utf-8")
        monkeypatch.setattr(ea_cwh, "_CATALOG_FILE", target)
        return doc

    def test_verify_get_by_name(self):
        assert ea_cwh.get("pgvector")["pypi_package"] == "ea-plugin-pgvector"
        assert ea_cwh.get("nope") is None

    def test_verify_by_pypi_package(self):
        assert ea_cwh.by_pypi_package("ea-plugin-pgvector")["name"] == "pgvector"

    def test_verify_plugins_hides_yanked_by_default(self):
        assert [p["name"] for p in ea_cwh.plugins()] == ["pgvector"]
        assert len(ea_cwh.plugins(include_yanked=True)) == 2

    def test_verify_catalog_rejects_invalid_temp_doc(self):
        bad = _doc(_entry(pypi_package="not-prefixed"))
        ea_cwh._CATALOG_FILE.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError, match="ea-plugin-"):
            ea_cwh.catalog()
