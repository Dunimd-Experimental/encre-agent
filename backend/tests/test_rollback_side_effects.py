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

"""Rollback side effects: files, memory nodes and plugin lifecycle ops.

Conversation rollback is only honest when the world the discarded turns built
goes with them.  Three substrates carry that, and each one here had a distinct
way of silently doing nothing:

* ``file_snapshots`` -- keyed by the raw model argument while the tools wrote
  to the path-guard-resolved file, and never persisted by the directory store;
* ``metadata["manage_ops"]`` -- tagged in the 0-based turn space while the
  rollback boundary is the restored ``turn_count``;
* the memory graph -- tagged the same way, so the rolled-back turn's own nodes
  survived the purge.

These tests exercise the real :class:`EncreSession`, the real path guard, the
real :class:`MemoryLayerStore` and the real directory persistence.  Only the
plugin registry inverse (``revert_manage_op``) is stubbed, because activating
and deactivating real plugins needs an installed package.
"""

import base64
import json
import os

import pytest

from encre.config import EncreConfig
from encre.memdir.judgment import MemoryCandidate
from encre.memdir.layers import MemoryLayerStore
from encre.session import (
    EncreSession,
    _extract_file_paths_from_messages,
)
from encre.tools.path_guard import resolve_write_target
from encre.utils.loop_helpers import extract_write_paths


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A workspace-rooted session with the data dir isolated."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    config = EncreConfig()
    config.workspace = str(ws)
    return ws, config


def _write_turn(session: EncreSession, file_path: str, content: str) -> str:
    """Record one user turn plus the assistant write that follows it.

    Mirrors the loop: the snapshot the execute phase captures BEFORE the tool
    runs, then the user message, the assistant message carrying the tool call
    and its result.  The caller writes the file itself -- that is the side
    effect under test.  Returns the user message id (the rollback point).
    """
    session.capture_file_snapshot(file_path)
    session.add_message("user", f"write {file_path}")
    user_id = session.messages[-1]["id"]
    session.add_message(
        "assistant",
        "done",
        tool_calls=[
            {
                "id": f"call_{session.turn_count}_0",
                "type": "function",
                "function": {
                    "name": "file_write",
                    "arguments": json.dumps(
                        {"file_path": file_path, "content": content}
                    ),
                },
            }
        ],
    )
    session.add_message("tool", f"Successfully wrote {len(content)} characters to {file_path}")
    return user_id


# ----------------------------------------------------------------------
# 1. Snapshot keys are the file the tool actually wrote
# ----------------------------------------------------------------------


def test_snapshot_key_is_the_resolved_workspace_path(workspace):
    """A relative argument snapshots the workspace file, not a CWD sibling."""
    ws, config = workspace
    target = ws / "notes.md"
    target.write_text("OLD", encoding="utf-8")
    session = EncreSession(config)

    session.capture_file_snapshot("notes.md")

    assert list(session.file_snapshots) == [str(resolve_write_target("notes.md", session))]
    assert list(session.file_snapshots) == [str(target)]


def test_message_extraction_matches_the_capture_key(workspace):
    """The rollback side derives the same keys the capture side stored."""
    ws, config = workspace
    session = EncreSession(config)
    _write_turn(session, "notes.md", "NEW")

    removed = [m for m in session.messages if m.get("role") == "assistant"]
    extracted = _extract_file_paths_from_messages(removed, session)

    assert extracted == {str(ws / "notes.md")}


def test_extraction_covers_plugin_write_tools(workspace):
    """Write tools outside the builtin name set are rollback targets too."""
    _, config = workspace
    session = EncreSession(config)
    session.add_message(
        "assistant",
        "",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "canvas_save",
                    "arguments": json.dumps({"path": "art/canvas.png"}),
                },
            }
        ],
    )

    extracted = _extract_file_paths_from_messages(session.messages, session)

    assert extracted  # not silently dropped for being a non-builtin name
    assert extracted == {str(resolve_write_target("art/canvas.png", session))}


def test_execute_phase_resolves_capture_paths(workspace):
    """The capture side keys snapshots where the tool will actually write."""
    from encre.loop.phases.execute import PhaseExecuteMixin

    ws, config = workspace
    (ws / "art").mkdir()

    class _LoopShim(PhaseExecuteMixin):
        def __init__(self, session):
            self.session = session

    loop = _LoopShim(EncreSession(config))

    assert loop._write_tool_paths("file_write", {"file_path": "art/a.png"}) == [
        str(ws / "art" / "a.png")
    ]
    assert loop._write_tool_paths("apply_patch", {
        "files": [{"old_path": "art/a.png", "new_path": "art/b.png"}]
    }) == [str(ws / "art" / "a.png"), str(ws / "art" / "b.png")]
    # A path the guard would refuse is a path no tool can write.
    assert loop._write_tool_paths("file_write", {"file_path": "../escape.txt"}) == []


def test_extract_write_paths_skips_read_tools():
    """A read call carries a path but never a rollback target."""
    assert extract_write_paths("file_read", {"file_path": "a.txt"}) == []
    assert extract_write_paths("web_search", {"query": "a"}) == []
    assert extract_write_paths("file_write", {"file_path": "a.txt"}) == ["a.txt"]
    assert extract_write_paths("apply_patch", {"files": [{"old_path": "a", "new_path": "b"}]}) == ["a", "b"]


# ----------------------------------------------------------------------
# 2. Files come back
# ----------------------------------------------------------------------


def test_rollback_restores_a_modified_file(workspace):
    """Reverting a turn puts the previous content back on disk."""
    ws, config = workspace
    target = ws / "notes.md"
    target.write_text("OLD", encoding="utf-8")
    session = EncreSession(config)
    user_id = _write_turn(session, "notes.md", "NEW")
    target.write_text("NEW", encoding="utf-8")
    session.turn_count = 1

    _removed, _branch, found = session.rollback_to(session.active_branch_id, user_id)

    assert found
    assert target.read_text(encoding="utf-8") == "OLD"


def test_rollback_deletes_a_file_created_by_the_discarded_turn(workspace):
    """A file that did not exist at the rollback point is removed, not left."""
    ws, config = workspace
    session = EncreSession(config)
    user_id = _write_turn(session, "fresh.md", "CREATED")
    (ws / "fresh.md").write_text("CREATED", encoding="utf-8")
    session.turn_count = 1

    session.rollback_to(session.active_branch_id, user_id)

    assert not (ws / "fresh.md").exists()


def test_rollback_restores_a_session_files_dir_write(tmp_path, monkeypatch):
    """General mode (no workspace) writes into the session files dir.

    That is a third resolution rule on top of absolute and workspace-relative;
    it used to be the one that silently produced nothing, because the snapshot
    was keyed under the server's working directory instead.
    """
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.chdir(str(elsewhere))
    config = EncreConfig()
    config.workspace = ""
    session = EncreSession(config)

    target = resolve_write_target("out.md", session)
    assert target.startswith(str(tmp_path / "data"))
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("OLD")
    user_id = _write_turn(session, "out.md", "NEW")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("NEW")
    session.turn_count = 1

    session.rollback_to(session.active_branch_id, user_id)

    with open(target, encoding="utf-8") as fh:
        assert fh.read() == "OLD"


def test_snapshots_survive_the_directory_round_trip(workspace):
    """Reloaded sessions still roll their files back.

    The directory store (``data/sessions/<id>/``) is what the server reloads,
    so a snapshot table that only lived in memory meant every rollback after a
    session switch or an app restart restored nothing.
    """
    ws, config = workspace
    target = ws / "notes.md"
    target.write_text("OLD", encoding="utf-8")
    session = EncreSession(config)
    user_id = _write_turn(session, "notes.md", "NEW")
    target.write_text("NEW", encoding="utf-8")
    session.turn_count = 1

    session_dir = ws.parent / "session-dir"
    session.save_to_dir(str(session_dir))
    reloaded = EncreSession.load_from_dir(str(session_dir), config)
    assert reloaded.file_snapshots == session.file_snapshots

    reloaded.rollback_to(reloaded.active_branch_id, user_id)

    assert target.read_text(encoding="utf-8") == "OLD"
    # The snapshot is consumed by the restore and must not linger on disk.
    assert reloaded.file_snapshots == {}
    reloaded.save_to_dir(str(session_dir))
    again = EncreSession.load_from_dir(str(session_dir), config)
    assert again.file_snapshots == {}
    assert not (session_dir / "snapshots.json").exists()


def test_snapshot_blob_is_encrypted_at_rest(workspace):
    """Rollback state is user content: never plaintext on disk."""
    from encre.secure_io import is_encrypted

    ws, config = workspace
    target = ws / "notes.md"
    target.write_text("SECRET OLD CONTENT", encoding="utf-8")
    session = EncreSession(config)
    _write_turn(session, "notes.md", "NEW")
    session_dir = ws.parent / "session-dir"
    session.save_to_dir(str(session_dir))

    blob = (session_dir / "snapshots.json").read_text(encoding="utf-8")

    assert is_encrypted(blob)
    assert "SECRET" not in blob


# ----------------------------------------------------------------------
# 3. Plugin lifecycle ops come back
# ----------------------------------------------------------------------


def test_manage_op_from_the_rolled_back_turn_is_reverted(workspace, monkeypatch):
    """Installs made by the discarded turn are undone.

    The journal entry is tagged with :attr:`EncreSession.active_turn` (the
    numbering the end-of-turn records use).  Tagging the mid-turn ``turn_count``
    put it one index early and the revert skipped the whole turn.
    """
    from encre.tools.builtin import manage

    _, config = workspace
    session = EncreSession(config)
    user_id = _write_turn(session, "notes.md", "NEW")
    session.turn_count = 1
    reverted: list[dict] = []
    monkeypatch.setattr(
        manage,
        "revert_manage_op",
        lambda op: reverted.append(op) is None or True,
    )
    session.metadata["manage_ops"] = [
        {
            "ts": 1.0,
            "turn": session.active_turn,
            "action": "install_tool",
            "name": "scratch_tool",
            "plugin_name": "model-tool-scratch_tool",
        }
    ]

    session.rollback_to(session.active_branch_id, user_id)

    assert [op["action"] for op in reverted] == ["install_tool"]
    assert session.metadata["manage_ops"] == []


def test_manage_op_from_a_kept_turn_survives(workspace, monkeypatch):
    """The boundary only reverts the discarded future, never kept work."""
    from encre.tools.builtin import manage

    _, config = workspace
    session = EncreSession(config)
    # The kept install is journalled during turn 1 (tag 1), the rolled-back
    # one during turn 2 (tag 2); the boundary is the one surviving assistant
    # turn.
    kept = {"ts": 1.0, "turn": session.active_turn, "action": "plugin_disable", "name": "ea-tool-x"}
    _write_turn(session, "kept.md", "KEPT")
    session.turn_count = 1
    user_id = _write_turn(session, "next.md", "NEW")
    session.metadata["manage_ops"] = [
        kept,
        {"ts": 2.0, "turn": session.active_turn, "action": "install_tool", "name": "scratch"},
    ]
    session.turn_count = 2
    reverted: list[dict] = []
    monkeypatch.setattr(
        manage, "revert_manage_op", lambda op: reverted.append(op) is None or True
    )

    session.rollback_to(session.active_branch_id, user_id)

    assert [op["action"] for op in reverted] == ["install_tool"]
    assert session.metadata["manage_ops"] == [kept]


def test_record_manage_op_tags_the_active_turn(workspace):
    """The writer and the rollback boundary share one turn numbering."""
    from encre.tools.builtin.manage import _record_manage_op

    _, config = workspace

    class _Loop:
        def __init__(self, session):
            self.session = session

    session = EncreSession(config)
    session.turn_count = 4
    _record_manage_op(_Loop(session), "plugin_install", name="ea-tool-x")

    assert session.metadata["manage_ops"][0]["turn"] == 5


# ----------------------------------------------------------------------
# 4. Memory comes back
# ----------------------------------------------------------------------


def _candidate(session_id: str, turn: int | None, content: str) -> MemoryCandidate:
    metadata = {"full_content": content}
    if turn is not None:
        metadata["turn"] = turn
    return MemoryCandidate(
        content=content,
        significance=0.8,
        reasons=["user_correction"],
        suggested_type="fact",
        session_id=session_id,
        metadata=metadata,
    )


def test_memory_nodes_from_the_rolled_back_turn_stop_being_recalled(workspace, tmp_path, monkeypatch):
    """A node written by the discarded turn must not be recalled again."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "memdata"))
    _, config = workspace
    session = EncreSession(config)
    # Turn 1 is journalled while its own iteration runs (turn_count still 0),
    # then completes; turn 2 is the one being rolled back.
    kept_tag = session.active_turn
    _write_turn(session, "kept.md", "KEPT")
    session.turn_count = 1
    user_id = _write_turn(session, "next.md", "NEW")
    discarded_tag = session.active_turn
    session.turn_count = 2
    store = MemoryLayerStore(root=tmp_path / "memory")
    session.memory_layers = store
    store.intake(_candidate(session.id, kept_tag, "kept fact"))
    store.intake(_candidate(session.id, discarded_tag, "discarded fact"))

    session.rollback_to(session.active_branch_id, user_id)

    live = {e.content for e in store.buffer()}
    live |= {e.content for e in store.long_term() if not e.decay.archived}
    assert "kept fact" in live
    assert "discarded fact" not in live


def test_untagged_memory_nodes_are_purged_by_the_rollback_clock(workspace, tmp_path, monkeypatch):
    """Legacy nodes without a turn tag still go, by ingestion time."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "memdata"))
    _, config = workspace
    session = EncreSession(config)
    user_id = _write_turn(session, "notes.md", "NEW")
    session.turn_count = 1
    store = MemoryLayerStore(root=tmp_path / "memory")
    session.memory_layers = store
    store.intake(_candidate(session.id, None, "untagged node"))

    session.rollback_to(session.active_branch_id, user_id)

    live = {e.content for e in store.buffer()}
    live |= {e.content for e in store.long_term() if not e.decay.archived}
    assert "untagged node" not in live


def test_memory_tool_tags_the_active_turn(workspace, monkeypatch):
    """``memory_create`` stamps the turn numbering rollback compares against."""
    from encre.tools.builtin import memory

    _, config = workspace
    session = EncreSession(config)
    session.turn_count = 2
    monkeypatch.setattr(memory, "_resolve_session", lambda: session)

    candidate = memory._new_candidate("fact", 0.9, "fact", "reason")

    assert candidate.metadata["turn"] == 3


# ----------------------------------------------------------------------
# 5. Commit-time file states stay addressable
# ----------------------------------------------------------------------


def test_commit_records_file_states_under_the_resolved_key(workspace):
    """Overlap restore looks commit states up by the same key as snapshots."""
    from encre.rollback import EncreRollbackGit

    ws, config = workspace
    target = ws / "notes.md"
    session = EncreSession(config)
    target.write_text("OLD", encoding="utf-8")
    session.capture_file_snapshot("notes.md")
    target.write_text("NEW", encoding="utf-8")
    session.turn_count = 1

    git = EncreRollbackGit()
    commit_hash = git.commit(session, "turn_1")

    obj = git._read_object(session.id, commit_hash)
    assert obj is not None
    assert list(obj["file_states"]) == [str(target)]
    assert base64.b64decode(obj["file_states"][str(target)]).decode() == "NEW"
    assert session.files_touched_since_commit == set()
    # The next commit has nothing new to record: the set was consumed.
    assert os.listdir(ws) == ["notes.md"]
