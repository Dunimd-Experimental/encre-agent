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

"""Tests for ``.md`` memory-file ingestion into the single layered memory store.

Every component under test is real and nothing is mocked: the store is a real
:class:`MemoryLayerStore` rooted in a ``tmp_path`` directory, persistence goes
through the real encrypted :mod:`encre.secure_io` layer log, the front matter is
parsed by the real file facade, and retrieval runs through the real
:class:`RecallEngine`.  Together the tests pin down that a human-edited
``.md`` memory lands in the store exactly once, that re-ingesting an unchanged
tree is a no-op, that editing a body revises the same logical entry rather than
duplicating it, and that ``MEMORY.md`` stays out of ingestion but still reaches
the production recall prompt verbatim as the curated index.

The data root is redirected to ``tmp_path`` through ``ENCRE_DATA_DIR`` and the
store is rooted inside the temp tree, so the real user data directory is never
touched.
"""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from encre.config import EncreConfig
from encre.loop_context import ContextBuilder
from encre.memdir.layers import (
    INGEST_CONTENT_HASH_KEY,
    MemoryLayerStore,
    MemoryType,
)
from encre.memdir.recall import RecallEngine
from encre.memdir.system import EncreMemorySystem
from encre.secure_io import read_jsonl


@pytest.fixture
def memory_dir(tmp_path, monkeypatch) -> Path:
    """Isolated memory directory plus an isolated data root for every test."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    directory = tmp_path / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_memory(
    directory: Path,
    name: str,
    *,
    description: str,
    body: str,
    memory_type: str = "fact",
    tags: tuple[str, ...] = (),
) -> Path:
    """Write a single human-editable memory file made of front matter and a body."""
    lines = ["---", f"description: {description}", f"type: {memory_type}"]
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {tag}" for tag in tags)
    lines.append("---")
    path = directory / name
    path.write_text("\n".join(lines) + "\n" + body, encoding="utf-8")
    return path


def _record_count(store: MemoryLayerStore) -> int:
    """Count the encrypted records written to the semantic layer log."""
    return len(read_jsonl(store.layers_dir / f"{MemoryType.FACT.value}.jsonl"))


# ----------------------------------------------------------------------
# 1. A .md memory file becomes a retrievable entry
# ----------------------------------------------------------------------


def test_md_file_is_ingested_and_recallable(memory_dir):
    """A front-mattered memory file lands in the store and recall finds it."""
    _write_memory(
        memory_dir,
        "gateway-port.md",
        description="Gateway port decision",
        tags=("networking", "config"),
        body=(
            "# Gateway port\n\n"
            "The gateway always listens on port 8080 in every environment.\n"
        ),
    )
    store = MemoryLayerStore(root=memory_dir)

    assert store.ingest_memory_dir() == 1

    entries = store.long_term()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.source == "md:gateway-port.md"
    assert entry.type == MemoryType.FACT.value
    assert "port 8080" in entry.content
    # The front-matter summary becomes part of the retrievable content.
    assert "Gateway port decision" in entry.content
    # The YAML delimiter belongs to metadata, so it never enters the memory text.
    assert "---" not in entry.content
    assert entry.metadata["tags"] == ["networking", "config"]
    assert entry.metadata["path"] == str(memory_dir / "gateway-port.md")
    assert entry.metadata[INGEST_CONTENT_HASH_KEY]

    hits = RecallEngine(store).recall("gateway port 8080")
    assert [hit.entry.source for hit in hits] == ["md:gateway-port.md"]
    assert hits[0].entry.id == entry.id


# ----------------------------------------------------------------------
# 2. Idempotency: an unchanged directory creates nothing new
# ----------------------------------------------------------------------


def test_reingesting_unchanged_directory_is_idempotent(memory_dir):
    """A second ingest of identical content writes neither a record nor an entry."""
    _write_memory(
        memory_dir,
        "release-checklist.md",
        description="Release checklist",
        body="# Release\n\nThe release checklist has seven steps.\n",
    )
    store = MemoryLayerStore(root=memory_dir)

    assert store.ingest_memory_dir() == 1
    assert _record_count(store) == 1
    entry_id = store.long_term()[0].id

    # Unchanged content: nothing created, nothing updated, nothing appended.
    assert store.ingest_memory_dir() == 0
    assert [entry.id for entry in store.long_term()] == [entry_id]
    assert _record_count(store) == 1

    # The no-op also survives a reload, since nothing was persisted in between.
    store.reload()
    assert [entry.id for entry in store.long_term()] == [entry_id]
    assert _record_count(store) == 1


# ----------------------------------------------------------------------
# 3. A changed body appends a revision of the same entry
# ----------------------------------------------------------------------


def test_changed_body_appends_a_revision_not_a_second_entry(memory_dir):
    """Editing the file updates the single logical entry by appending a revision."""
    path = _write_memory(
        memory_dir,
        "deploy-steps.md",
        description="Deploy steps",
        body="# Deploy\n\nThe deploy pipeline has seven steps.\n",
    )
    store = MemoryLayerStore(root=memory_dir)

    assert store.ingest_memory_dir() == 1
    original = store.long_term()[0]
    original_id = original.id
    original_version = original.version
    assert _record_count(store) == 1

    path.write_text(
        "---\ndescription: Deploy steps\ntype: semantic\n---\n"
        "# Deploy\n\nThe deploy pipeline has nine steps.\n",
        encoding="utf-8",
    )

    assert store.ingest_memory_dir() == 1

    entries = store.long_term()
    assert len(entries) == 1
    assert entries[0].id == original_id
    assert "nine steps" in entries[0].content
    assert "seven steps" not in entries[0].content
    assert entries[0].version == original_version + 1
    # The revision is appended to the append-only log and never creates a twin.
    assert _record_count(store) == 2

    # The newest record wins on reload: still exactly one logical entry.
    store.reload()
    entries = store.long_term()
    assert len(entries) == 1
    assert entries[0].id == original_id
    assert "nine steps" in entries[0].content

    # Re-ingesting the already-edited file is again a no-op.
    assert store.ingest_memory_dir() == 0
    assert _record_count(store) == 2


# ----------------------------------------------------------------------
# 4. MEMORY.md is skipped; the explicit directory argument is honoured
# ----------------------------------------------------------------------


def test_entrypoint_is_skipped_and_directory_argument_is_honoured(memory_dir):
    """The entrypoint is never ingested and an explicit directory is scanned."""
    (memory_dir / "MEMORY.md").write_text(
        "# Entrypoint\n\nThis is the entrypoint, not a memory.\n",
        encoding="utf-8",
    )
    _write_memory(
        memory_dir,
        "port.md",
        description="Port convention",
        body="# Port\n\nThe project always uses port 8080.\n",
    )
    store = MemoryLayerStore(root=memory_dir)

    assert store.ingest_memory_dir(memory_dir) == 1

    entries = store.long_term()
    assert [entry.source for entry in entries] == ["md:port.md"]
    assert all("Entrypoint" not in entry.content for entry in entries)

    # A directory that does not exist ingests nothing.
    assert store.ingest_memory_dir(memory_dir / "missing") == 0
    assert len(store.long_term()) == 1


# ----------------------------------------------------------------------
# 5. End-to-end: a file on disk reaches the production prompt path
# ----------------------------------------------------------------------


def test_ingested_memory_reaches_the_recall_prompt(memory_dir):
    """A memory written to disk and ingested is rendered by build_recall_prompt.

    Every piece of the chain is real: the facade's ``refresh()`` trigger, the
    ``MemoryLayerStore`` with encrypted persistence, the ``RecallEngine``, and
    the ``ContextBuilder`` that the production loop uses to enrich the system
    prompt.
    """
    _write_memory(
        memory_dir,
        "gateway-port.md",
        description="Gateway port decision",
        tags=("networking",),
        body="# Gateway port\n\nThe gateway always listens on port 8080.\n",
    )
    store = MemoryLayerStore(root=memory_dir)
    facade = EncreMemorySystem(str(memory_dir), layer_store=store)

    builder = ContextBuilder(
        EncreConfig(model="gpt-5.6"),
        SimpleNamespace(messages=[], active_branch_id=""),
        cache_fresh=lambda built_at, ttl=30.0: (time.time() - built_at) < ttl,
        memory_system=facade,
        recall_engine=RecallEngine(store),
    )

    # Before the ingestion trigger fires nothing is associated with the query.
    assert builder.build_recall_prompt("gateway port 8080") == ""

    facade.refresh()

    prompt = builder.build_recall_prompt("gateway port 8080")
    assert prompt
    assert "The gateway always listens on port 8080." in prompt
    # The rendered block is a real prompt block that names the memory type.
    assert "## Relevant Memories" in prompt
    assert f"[{MemoryType.FACT.value}]" in prompt

    # An unrelated query recalls nothing, so the block is not rendered.
    assert builder.build_recall_prompt("watercolour painting techniques") == ""


def test_curated_entrypoint_memory_reaches_build_recall_prompt(memory_dir):
    """The human-curated ``MEMORY.md`` index still reaches the prompt.

    ``MEMORY.md`` is deliberately excluded from ingestion, so it is not
    reachable through the ``RecallEngine``.  It must instead be rendered
    verbatim as the curated index ahead of any recall hits; otherwise the
    hand-maintained entrypoint would silently stop reaching the model, which
    would be a regression from the legacy facade's ``build_prompt()``.
    """
    curated = "# MEMORY\n\nTeam conventions:\n- gateway always on 8080\n"
    (memory_dir / "MEMORY.md").write_text(curated, encoding="utf-8")
    store = MemoryLayerStore(root=memory_dir)
    facade = EncreMemorySystem(str(memory_dir), layer_store=store)
    builder = ContextBuilder(
        EncreConfig(model="gpt-5.6"),
        SimpleNamespace(messages=[], active_branch_id=""),
        cache_fresh=lambda built_at, ttl=30.0: (time.time() - built_at) < ttl,
        memory_system=facade,
        recall_engine=RecallEngine(store),
    )

    # There are no recall hits, yet the curated index is still present.
    prompt = builder.build_recall_prompt("pipeline")
    assert prompt
    assert "gateway always on 8080" in prompt
    assert "## Curated Memory Index" in prompt

    # It coexists with recall hits once a memory has been ingested.
    _write_memory(
        memory_dir,
        "pipeline.md",
        description="Pipline note",
        body="# Pipeline\n\nThe build pipeline has three stages.\n",
    )
    facade.refresh()
    prompt = builder.build_recall_prompt("pipeline build")
    assert "gateway always on 8080" in prompt
    assert "## Relevant Memories" in prompt
    assert "The build pipeline has three stages." in prompt


def test_curated_entrypoint_survives_a_degraded_recall_engine(memory_dir):
    """The curated index is rendered even with no recall engine attached.

    The memory subsystem degrades to ``recall_engine = None`` when the layered
    store cannot be built.  The hand-maintained entrypoint must not disappear
    with it.
    """
    (memory_dir / "MEMORY.md").write_text(
        "# MEMORY\n\nTeam conventions:\n- gateway always on 8080\n",
        encoding="utf-8",
    )
    facade = EncreMemorySystem(str(memory_dir))
    builder = ContextBuilder(
        EncreConfig(model="gpt-5.6"),
        SimpleNamespace(messages=[], active_branch_id=""),
        cache_fresh=lambda built_at, ttl=30.0: (time.time() - built_at) < ttl,
        memory_system=facade,
        recall_engine=None,
    )

    prompt = builder.build_recall_prompt("anything")
    assert prompt
    assert "gateway always on 8080" in prompt
    assert "## Curated Memory Index" in prompt
    assert "## Relevant Memories" not in prompt
