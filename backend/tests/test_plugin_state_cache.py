"""Persisted installed-plugin snapshot (the market's fast read path)."""

from __future__ import annotations

import encre.plugin_state_cache as cache


def test_round_trip_and_empty_snapshot_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_path", lambda: tmp_path / "plugin_states.json")

    assert cache.read_plugin_states() is None

    rows = [{"name": "alpha", "installed": True, "state": "activated"}]
    cache.write_plugin_states(rows)
    assert cache.read_plugin_states() == rows

    # An empty live snapshot (broken warmup) must not erase a good index.
    cache.write_plugin_states([])
    assert cache.read_plugin_states() == rows
