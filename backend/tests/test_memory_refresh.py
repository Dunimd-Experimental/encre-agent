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

"""Tests for :meth:`EncreMemorySystem.refresh`, the memory ingestion trigger.

The production loop calls ``memory_system.refresh()`` after a successful
compaction so that the next turn can see the memories written during the
current one.  Because ``EncreMemorySystem`` is now pure file I/O, ``refresh``
is the ingestion trigger itself: it feeds the ``.md`` memory directory into the
single :class:`MemoryLayerStore`, after which retrieval flows through the single
:class:`RecallEngine` over that store.

Every component is real and nothing is stubbed: a memory file written to disk
after the store was constructed only becomes retrievable once ``refresh()`` has
ingested it, and re-running the trigger never duplicates the entry.
"""

from pathlib import Path

import pytest

from encre.memdir.layers import MemoryLayerStore
from encre.memdir.recall import RecallEngine
from encre.memdir.system import EncreMemorySystem

# A memory file in the plaintext front-matter format the facade parses.
_MEMORY_FILE = "refresh_probe.md"
_MEMORY_CONTENT = "\n".join([
    "---",
    "description: Refresh probe memory about the zanzibar subsystem",
    "type: semantic",
    "tags:",
    "  - zanzibar",
    "  - probe",
    "---",
    "",
    "The zanzibar subsystem pins the port to 8080 and never uses 3000.",
    "",
])

# The recall query used to probe whether the memory is reachable.
_QUERY = "zanzibar port"


@pytest.fixture
def memory_dir(tmp_path, monkeypatch) -> Path:
    """Isolated memory directory plus an isolated data root for every test."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    directory = tmp_path / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _make_system(memory_dir: Path, store: MemoryLayerStore | None = None) -> EncreMemorySystem:
    """Construct a real memory facade rooted at an isolated temp directory."""
    return EncreMemorySystem(str(memory_dir), layer_store=store)


def test_refresh_exists_and_is_idempotent(memory_dir):
    """``refresh`` is callable and can run repeatedly without raising."""
    store = MemoryLayerStore(root=memory_dir)
    system = _make_system(memory_dir, store)
    assert callable(getattr(system, "refresh", None))

    # Calling it twice back to back must stay safe (idempotent ingestion).
    system.refresh()
    system.refresh()
    assert store.long_term() == []


def test_refresh_without_store_is_a_harmless_noop(memory_dir):
    """An unattached facade never raises, so the call site needs no guarding."""
    system = _make_system(memory_dir)
    assert system.layer_store is None
    (memory_dir / _MEMORY_FILE).write_text(_MEMORY_CONTENT, encoding="utf-8")

    system.refresh()
    # Nothing is ingested anywhere, and no layer data appears on disk.
    assert not (memory_dir / "layers").exists()


def test_refresh_ingests_new_memory_into_the_single_store(memory_dir):
    """A file written after the store is built only becomes retrievable after refresh."""
    store = MemoryLayerStore(root=memory_dir)
    system = _make_system(memory_dir, store)
    engine = RecallEngine(store)

    # The store starts empty: the memory file does not exist yet.
    assert engine.recall(_QUERY) == []

    (memory_dir / _MEMORY_FILE).write_text(_MEMORY_CONTENT, encoding="utf-8")

    # The ingestion trigger really pulls the file into the single store.
    system.refresh()

    entries = store.long_term()
    assert [entry.source for entry in entries] == [f"md:{_MEMORY_FILE}"]
    assert "zanzibar" in entries[0].content

    # ...and the single retrieval path can now find it.
    hits = engine.recall(_QUERY)
    assert [hit.entry.source for hit in hits] == [f"md:{_MEMORY_FILE}"]
    assert "port to 8080" in hits[0].entry.content


def test_refresh_is_idempotent_after_ingestion(memory_dir):
    """Re-running the trigger does not duplicate the ingested entry."""
    (memory_dir / _MEMORY_FILE).write_text(_MEMORY_CONTENT, encoding="utf-8")
    store = MemoryLayerStore(root=memory_dir)
    system = _make_system(memory_dir, store)

    system.refresh()
    first_ids = [entry.id for entry in store.long_term()]
    assert len(first_ids) == 1

    system.refresh()
    assert [entry.id for entry in store.long_term()] == first_ids
