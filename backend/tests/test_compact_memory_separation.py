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

"""Guard tests: context compaction must never write into the memory system.

The core invariant is **context != memory**. Compaction manages the temporary
reasoning window and may archive the pre-compact messages, but it must never
create persistent memory entries; only the agent's explicit memory tools
(``memory_create`` / ``memory_update``) are allowed to do that.

The tests drive the real :class:`CompactEngine` against a real
:class:`EncreMemorySystem` on a temporary memory directory, stubbing only the
external backend dependency (which merely streams a valid compact summary).
"""

import os
import shutil
import tempfile

import pytest

from encre.compact.engine import CompactEngine
from encre.memdir.layers import MemoryLayerStore
from encre.memdir.recall import RecallEngine
from encre.memdir.system import EncreMemorySystem
from encre.utils.types import BackendText

# A distinctive token embedded in every archived message. It must never become
# retrievable through the memory system.
_NEEDLE = "zqxneedle9917"

_SUMMARY = """<analysis>
scratchpad that the engine strips before building the summary
</analysis>
<summary>
### 1. Primary Request and Intent
Continue the user's task without losing context after compaction.

### 2. Files and Code Sections
- harness/encre/compact/engine.py

### 3. Key Decisions and Changes Made
None.

### 4. Errors and Fixes
None.

### 5. Current State
Mid-task, context was compressed.

### 6. Pending Tasks
Finish the remaining work.

### 7. User Messages
Question 0 through Question 11.

### 8. Workspace Context
d:/encre

### 9. Next Step (Optional)
Continue.
</summary>"""


class _StubSummaryBackend:
    """Stub for the external backend dependency used by ``CompactEngine``.

    It implements the real backend protocol surface the engine calls:
    ``context_window_size()`` and an async ``chat()`` generator that streams a
    valid 9-section compact summary. ``CompactEngine`` and
    ``EncreMemorySystem`` stay as the real project objects under test.
    """

    def __init__(self) -> None:
        """Initialize the stub with a zeroed counter of chat() invocations."""
        self.chat_calls = 0

    def context_window_size(self) -> int:
        """Return a fixed 128K-token context window size."""
        return 128_000

    async def chat(self, messages=None, **kwargs):  # noqa: ANN001, ANN003
        """Stream the canned compact summary and count the call."""
        self.chat_calls += 1
        yield BackendText(text=_SUMMARY)


def _make_messages() -> list[dict]:
    """Build a 12-turn conversation long enough to compact, carrying the needle token."""
    msgs: list[dict] = [{"role": "system", "content": "You are an assistant."}]
    for i in range(12):
        msgs.append({"role": "user", "content": f"Question {i} about the task {_NEEDLE}"})
        msgs.append({"role": "assistant", "content": f"Answer {i} with detail {_NEEDLE}"})
    return msgs


@pytest.fixture
def memory_dir():
    """Provide an isolated temporary memory directory, removed after each test."""
    d = tempfile.mkdtemp()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _write_memory(directory: str, name: str, content: str) -> None:
    """Write a memory markdown file into the given directory."""
    with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
        f.write(content)


async def test_compact_does_not_create_memory_entries(memory_dir):
    """A full compaction must leave the memory entry count unchanged."""
    _write_memory(memory_dir, "auth.md", "The login system uses OAuth2 with JWT tokens.")
    memory = EncreMemorySystem(memory_dir)
    store = MemoryLayerStore(root=memory_dir)
    memory.layer_store = store
    memory.refresh()

    files_before = sorted(os.listdir(memory_dir))
    count_before = len(memory.scan())
    assert count_before == 1
    entries_before = [entry.id for entry in store.long_term()]

    engine = CompactEngine()
    messages = _make_messages()
    compacted = await engine.compact(
        messages,
        backend=_StubSummaryBackend(),
        turn_count=5,
        session_id="sess-sep-1",
    )

    # Compaction really happened: the history shrank.
    assert compacted is not None
    assert len(compacted) < len(messages)

    # ... but it produced no memory: same entry count, same files on disk, and
    # the single store was not touched.
    assert len(memory.scan()) == count_before
    assert sorted(os.listdir(memory_dir)) == files_before
    assert [entry.id for entry in store.long_term()] == entries_before


async def test_archive_exists_but_is_not_retrievable_as_memory(memory_dir):
    """The compact archive must exist, yet never surface through recall."""
    _write_memory(memory_dir, "auth.md", "The login system uses OAuth2 with JWT tokens.")
    memory = EncreMemorySystem(memory_dir)
    store = MemoryLayerStore(root=memory_dir)
    memory.layer_store = store
    memory.refresh()

    engine = CompactEngine()
    messages = _make_messages()
    compacted = await engine.compact(
        messages,
        backend=_StubSummaryBackend(),
        turn_count=5,
        session_id="sess-sep-2",
    )
    assert compacted is not None

    # The pre-compact archive was captured (archiving did happen).
    archive = engine.get_archive("sess-sep-2")
    assert archive is not None
    assert len(archive) == len(messages)
    assert _NEEDLE in str(archive)

    # The archived content must not be retrievable as memory: the single
    # retrieval path sees only the ingested ``auth.md`` entry.
    hits = RecallEngine(store).recall(_NEEDLE, top_k=10)
    assert all(_NEEDLE not in hit.entry.content for hit in hits)

    # Nor may the archive have been written into the memory directory.
    for name in os.listdir(memory_dir):
        path = os.path.join(memory_dir, name)
        if os.path.isdir(path):
            continue
        with open(path, encoding="utf-8") as f:
            assert _NEEDLE not in f.read()
