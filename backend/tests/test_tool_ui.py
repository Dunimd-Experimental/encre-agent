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

"""Tests for the tool UI metadata catalog and its merge precedence.

The catalog is the only place in the codebase that knows how a tool looks.
These tests pin the three properties the renderer depends on:

  * every entry carries a legal card / body / result value,
  * precedence is catalog < builtin tool < plugin, with field-level merging,
  * the six file-mutation tools keep the exact shapes the renderer hardcodes.
"""


import pytest
from encre.plugins.tool_meta import (
    SOURCE_CATALOG,
    SOURCE_TOOL,
    _sanitize,
    collect_tool_meta,
    tool_meta_by_name,
)
from encre.tools.builtin import register_builtin_tools
from encre.tools.registry import ToolRegistry
from encre.tools.tool_ui import (
    BUILTIN_TOOL_UI,
    RESULT_DISCOVER,
    RESULT_FILE,
    RESULT_FILE_COUNT,
    RESULT_JSON,
    RESULT_MATCH_COUNT,
    RESULT_TEXT,
    RESULT_URL,
    all_ui_entries,
    ui_dict,
)

# The card and body vocabularies the renderer understands. The result set is
# assembled from the server's own constants so this file cannot drift behind a
# newly added strategy again (``url`` was missing here).
CARDS = {"strip", "item", "file", "agent", "question", "info", "hidden"}
BODIES = {"pre", "terminal", "diff", "web-results", "web-fetched"}
RESULTS = {
    RESULT_TEXT, RESULT_JSON, RESULT_FILE, RESULT_MATCH_COUNT,
    RESULT_FILE_COUNT, RESULT_DISCOVER, RESULT_URL,
}


def _table(tools=(), plugin_entries=()):
    """Collect merged tool metadata into a name-keyed table for easy assertions.

    Wraps collect_tool_meta so individual tests can look entries up by tool
    name instead of scanning a flat list every time.
    """
    return {e["name"]: e for e in collect_tool_meta(tools=tools, plugin_entries=plugin_entries)}


class _FakeManifest:
    """Stand-in for ``PluginManifest`` exposing only ``name``."""

    name = "obj-plugin"


class _FakePlugin:
    """Declarative plugin exposing one tool UI contribution."""

    def get_tool_meta(self):
        """Return the single 'party' entry this fake plugin contributes."""
        return [{"name": "party", "meta": {"icon": "party-popper"}}]


def test_catalog_is_populated_and_well_formed():
    """Assert the shipped table covers the catalog and every entry uses a legal vocabulary.

    The table is the static catalog PLUS every builtin tool's own ``ui=``
    declaration, so it is a superset of ``BUILTIN_TOOL_UI`` -- a tool that only
    declares itself next to its implementation (``submit_review``) must still
    reach the renderer, which is exactly what an equality assertion here used
    to hide. Each meta must carry card / body / result values from the known
    sets, and keys must always be a list of strings so the renderer can
    iterate them safely.
    """
    table = _table()
    assert len(table) >= len(BUILTIN_TOOL_UI) >= 80
    for entry in table.values():
        meta = entry["meta"]
        assert meta["card"] in CARDS
        assert meta["body"] in BODIES
        assert meta["result"] in RESULTS
        assert isinstance(meta.get("keys", []), list)
        assert all(isinstance(k, str) for k in meta.get("keys", []))
    assert {e["name"] for e in all_ui_entries()} <= set(table)


def test_hidden_tools_also_carry_the_hidden_card():
    """Assert the hidden flag and the 'hidden' card are used in lockstep.

    The renderer used to hide tools by hardcoding a name list, so the set of
    hidden tools must be a superset of that legacy family and every hidden
    entry must also carry the hidden card value.
    """
    table = _table()
    hidden = {n for n, e in table.items() if e["meta"].get("hidden") is True}
    assert hidden == {n for n, e in table.items() if e["meta"]["card"] == "hidden"}
    # The family the renderer used to hide by name, plus the core tools that
    # declare themselves hidden next to their implementation.
    assert {"todo", "find_tool", "lsp", "task", "task_", "memory", "memory_", "cron_",
            "submit_review"} <= hidden


def test_file_mutation_tools_keep_their_hardcoded_shapes():
    """Assert the file-mutation tools keep the exact shapes the renderer hardcodes.

    These six tools drive the frontend's own hardcoded table, so they must not
    drift; apply_patch stays on the compact item card rather than the diff strip
    because it is not a direct write/edit.
    """
    table = _table()
    assert table["write"]["meta"]["card"] == "strip"
    assert table["write"]["meta"]["body"] == "diff"
    assert table["write"]["meta"]["result"] == "file"
    assert table["edit"]["meta"]["body"] == "diff"
    assert table["delete_file"]["meta"]["card"] == "file"
    # apply_patch is one of the six but is not a direct write/edit, so it keeps
    # the compact item card instead of the diff strip.
    assert table["apply_patch"]["meta"]["card"] == "item"
    assert table["apply_patch"]["meta"]["body"] == "pre"


def test_unknown_tool_keys_are_dropped():
    """Assert plugin metadata is sanitized down to the recognized keys only.

    Unknown keys such as 'bogus' are removed, and a non-list 'keys' value is
    dropped outright so the renderer never sees malformed key metadata.
    """
    table = _table(plugin_entries=[{"name": "weird", "meta": {"icon": "x", "bogus": 1, "keys": "bad"}}])
    meta = table["weird"]["meta"]
    assert meta["icon"] == "x"
    assert "bogus" not in meta
    # A non-list `keys` value is dropped outright, so the renderer sees no keys.
    assert "keys" not in meta


def test_sanitize_drops_unknown_keys():
    """Assert _sanitize removes unrecognized keys while keeping valid ones."""
    assert _sanitize({"icon": "x", "nope": 1}) == {"icon": "x"}


def test_tool_instance_ui_wins_over_catalog():
    """Assert a builtin tool's own UI declaration overrides the static catalog entry.

    Registered builtin tools are tagged SOURCE_TOOL, while catalog-only entries
    such as apply_patch keep the SOURCE_CATALOG tag.
    """
    registry = ToolRegistry()
    register_builtin_tools(registry)
    table = _table(tools=registry.all())
    for name in ("bash", "file_read", "file_write", "file_edit", "web_search", "web_fetch"):
        assert table[name]["source"] == SOURCE_TOOL
    assert table["bash"]["meta"]["body"] == "terminal"
    assert table["bash"]["meta"]["icon"] == "terminal"
    # Entries no builtin module declares stay catalog-sourced.
    assert table["apply_patch"]["source"] == SOURCE_CATALOG


def test_plugin_wins_field_by_field_over_catalog_and_builtin():
    """Assert plugin metadata merges field by field on top of builtin and catalog entries.

    The plugin wins the fields it declares (icon, label, source) while fields it
    leaves untouched (body, card, keys) are inherited from the builtin declaration.
    """
    registry = ToolRegistry()
    register_builtin_tools(registry)
    table = _table(
        tools=registry.all(),
        plugin_entries=[
            {"name": "web_search", "meta": {"icon": "sparkles", "label": "Webbie"}, "source": "test-plugin"}
        ],
    )
    meta = table["web_search"]["meta"]
    assert table["web_search"]["source"] == "test-plugin"
    assert meta["icon"] == "sparkles"
    assert meta["label"] == "Webbie"
    # Untouched fields inherit from the builtin declaration.
    assert meta["body"] == "web-results"
    assert meta["card"] == "strip"
    assert meta["keys"] == ["query", "q"]


def test_later_plugin_entry_wins():
    """Assert that when two plugin entries name the same tool, the later one wins."""
    table = _table(
        plugin_entries=[
            {"name": "x", "meta": {"icon": "a"}},
            {"name": "x", "meta": {"icon": "b"}},
        ]
    )
    assert table["x"]["meta"]["icon"] == "b"


def test_plugin_accepts_a_tool_object():
    """Assert a plugin object is read through ``manifest.name`` + ``get_tool_meta()``."""
    manifest = _FakeManifest()
    plugin = _FakePlugin()
    plugin.manifest = manifest

    table = _table(plugin_entries=[plugin])
    assert table["party"]["meta"]["icon"] == "party-popper"
    assert table["party"]["source"] == "obj-plugin"


def test_plugin_get_tool_meta_failure_is_skipped():
    """Assert a plugin whose get_tool_meta raises is skipped instead of aborting collection."""
    class _BrokenPlugin:
        """Plugin stub whose metadata hook always raises to simulate a bad plugin."""

        def get_tool_meta(self):
            """Raise to simulate a plugin that crashes while reporting its tools."""
            raise RuntimeError("boom")

    table = _table(plugin_entries=[_BrokenPlugin()])
    assert "party" not in table


def test_include_builtin_false():
    """Assert include_builtin=False hides the catalog and returns only plugin entries."""
    table = {e["name"]: e for e in collect_tool_meta(plugin_entries=[{"name": "x", "meta": {"icon": "a"}}], include_builtin=False)}
    assert set(table) == {"x"}


def test_tool_ui_rejects_unknown_card_and_body():
    """Assert ui_dict validates card and body against their known vocabularies."""
    with pytest.raises(ValueError):
        ui_dict(card="nope")
    with pytest.raises(ValueError):
        ui_dict(body="nope")


def test_hidden_forces_the_hidden_card():
    """Assert the hidden flag overrides the default card to 'hidden'."""
    assert ui_dict(icon="x", hidden=True).card == "hidden"
    assert ui_dict(icon="x").card == "strip"


def test_prefix_families_are_marked():
    """Assert exactly the expected tool-name prefixes are flagged as families."""
    prefixes = {name for name, ui in BUILTIN_TOOL_UI.items() if ui.prefix}
    assert prefixes == {"cron_", "mcp__", "memory_", "task_"}


def test_tool_meta_by_name_is_a_convenience_view():
    """Assert tool_meta_by_name returns the same keys and payloads as the merged table."""
    registry = ToolRegistry()
    register_builtin_tools(registry)
    keyed = tool_meta_by_name(tools=registry.all())
    listed = _table(tools=registry.all())
    assert set(keyed) == set(listed)
    assert keyed["bash"] == listed["bash"]["meta"]
