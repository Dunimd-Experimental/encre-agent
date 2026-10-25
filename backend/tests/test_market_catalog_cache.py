"""Catalog memo and persisted check stamp."""

from __future__ import annotations

import encre.market_catalog as mc


def test_load_catalog_is_memoized_until_invalidated(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_read_index():
        calls["n"] += 1
        return {
            "index_version": str(calls["n"]),
            "generated_at": "g",
            "plugins": [{"name": "a"}],
        }

    monkeypatch.setattr(mc, "read_index", fake_read_index)
    monkeypatch.setattr(mc, "_cache_file", lambda: tmp_path / "catalog.json")
    monkeypatch.setattr(mc, "_read_cache", lambda: None)
    monkeypatch.setattr(mc, "_write_cache", lambda index: None)
    mc.invalidate_catalog_cache()

    first = mc.load_catalog()
    second = mc.load_catalog()
    assert calls["n"] == 1, "repeat loads must not re-read/validate the index"
    assert first is second

    mc.invalidate_catalog_cache()
    third = mc.load_catalog()
    assert calls["n"] == 2
    assert third["index_version"] == "2"


def test_check_stamp_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "_check_stamp_file", lambda: tmp_path / "checked.json")
    assert mc.read_last_check() == 0.0
    mc.write_last_check(123.5)
    assert mc.read_last_check() == 123.5
