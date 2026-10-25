"""Regression tests for the file-version rollback path.

These pin the four behaviours the user reported as unstable in the
rollback-edit feature (see ``docs/audit/harness-baseline.md`` and the
rollback-fix history):

* a file written across two turns and then rolled back to turn 1 is restored
  to its **turn-1 commit state**, not deleted (the pre-fix algorithm restored
  the first write's pre-state, which for a new file was ``""``);
* a file created *only* in the rolled-back turn is deleted;
* a file touched *only* in a kept turn is left untouched;
* when there is no commit chain to consult, the pre-write snapshot is the
  fallback (so a brand-new file written in a never-committed turn is removed);
* a descendant branch is wiped on rollback to its fork point;
* the target message itself is retained (rollback removes ``seq > target``,
  not ``>= target``).
"""

from __future__ import annotations

import json
from pathlib import Path

from encre.config import EncreConfig
from encre.rollback import EncreRollbackGit
from encre.session import EncreSession, _extract_file_paths_from_messages


def _new_session() -> EncreSession:
    cfg = EncreConfig(model="x", backend_type="openai")
    sess = EncreSession(cfg)
    sess.ensure_undo_stack()
    return sess


def _write_turn(
    sess: EncreSession,
    git: EncreRollbackGit,
    seq: int,
    writes: list[tuple[Path, str]],
) -> str:
    """Simulate one turn: snapshot, write, append user+assistant, commit."""
    for path, content in writes:
        sess.capture_file_snapshot(str(path))
        path.write_text(content, encoding="utf-8")
        sess.files_touched_since_commit.add(str(path))
    sess.add_message(role="user", content=f"t{seq}", branch_id=sess.active_branch_id)
    sess.add_message(
        role="assistant",
        content=f"done {seq}",
        branch_id=sess.active_branch_id,
        tool_calls=[
            {
                "id": f"c{seq}_{i}",
                "type": "function",
                "function": {
                    "name": "file_write",
                    "arguments": json.dumps({"file_path": str(p), "content": c}),
                },
            }
            for i, (p, c) in enumerate(writes)
        ],
    )
    sess.turn_count = sum(1 for m in sess.messages if m.get("role") == "assistant")
    git.commit(sess, f"turn_{sess.turn_count}")
    return next(
        m["id"]
        for m in sess.messages
        if m.get("role") == "assistant" and m.get("content") == f"done {seq}"
    )


def _assistant_by_content(sess: EncreSession, content: str) -> dict:
    return next(
        m
        for m in sess.messages
        if m.get("role") == "assistant" and m.get("content") == content
    )


def test_shared_file_restored_to_rollback_point(tmp_path: Path) -> None:
    """Turn 1 writes notes.txt=TURN1; turn 2 overwrites to TURN2.

    Rolling back to turn-1 assistant must leave notes.txt with the turn-1
    commit content (TURN1), not delete it (the pre-fix bug).
    """
    sess = _new_session()
    git = EncreRollbackGit()
    notes = tmp_path / "notes.txt"
    t1 = _write_turn(sess, git, 1, [(notes, "TURN1\n")])
    _write_turn(sess, git, 2, [(notes, "TURN2\n")])

    sess.rollback_to(sess.active_branch_id, t1)

    assert notes.read_text(encoding="utf-8") == "TURN1\n"


def test_file_created_only_in_rolled_back_turn_is_deleted(tmp_path: Path) -> None:
    """A file that first appears in the rolled-back turn is removed."""
    sess = _new_session()
    git = EncreRollbackGit()
    notes = tmp_path / "notes.txt"
    only_t2 = tmp_path / "only_t2.txt"
    t1 = _write_turn(sess, git, 1, [(notes, "TURN1\n")])
    _write_turn(sess, git, 2, [(notes, "TURN2\n"), (only_t2, "T2-ONLY\n")])

    sess.rollback_to(sess.active_branch_id, t1)

    assert not only_t2.exists()


def test_file_touched_only_in_kept_turn_untouched(tmp_path: Path) -> None:
    """A file written in turn 1 and never touched again stays at turn-1 content."""
    sess = _new_session()
    git = EncreRollbackGit()
    keep = tmp_path / "keep.txt"
    other = tmp_path / "other.txt"
    t1 = _write_turn(sess, git, 1, [(keep, "KEEP1\n"), (other, "OTHER1\n")])
    _write_turn(sess, git, 2, [(other, "OTHER2\n")])

    sess.rollback_to(sess.active_branch_id, t1)

    assert keep.read_text(encoding="utf-8") == "KEEP1\n"
    assert other.read_text(encoding="utf-8") == "OTHER1\n"


def test_no_commit_chain_falls_back_to_pre_write_snapshot(tmp_path: Path) -> None:
    """Without a commit chain, the pre-write snapshot is the only source.

    Turn 1 has no file writes; turn 2 creates a brand-new file (snapshot is the
    FileNotFoundError marker ``""``).  No ``EncreRollbackGit.commit()`` is ever
    called, so ``file_states_asof_turn`` is empty and rollback falls back to the
    pre-write snapshot, which deletes the file.
    """
    sess = _new_session()
    # Turn 1: no file writes, no commit.
    sess.add_message(role="user", content="t1", branch_id=sess.active_branch_id)
    sess.add_message(role="assistant", content="done 1", branch_id=sess.active_branch_id)
    sess.turn_count = 1
    t1_id = _assistant_by_content(sess, "done 1")["id"]

    # Turn 2: create a file that did not exist before, still no commit.
    new_file = tmp_path / "uncommitted.txt"
    sess.capture_file_snapshot(str(new_file))
    new_file.write_text("UNCOMMITTED\n", encoding="utf-8")
    sess.add_message(role="user", content="t2", branch_id=sess.active_branch_id)
    sess.add_message(
        role="assistant",
        content="done 2",
        branch_id=sess.active_branch_id,
        tool_calls=[
            {
                "id": "c2_0",
                "type": "function",
                "function": {
                    "name": "file_write",
                    "arguments": json.dumps(
                        {"file_path": str(new_file), "content": "UNCOMMITTED\n"}
                    ),
                },
            }
        ],
    )
    sess.turn_count = 2

    removed, _, found = sess.rollback_to(sess.active_branch_id, t1_id)

    assert found is True
    assert removed  # the turn-2 messages were removed
    assert not new_file.exists()


def test_descendant_branch_wiped_on_rollback(tmp_path: Path) -> None:
    """A fork off turn 1 is purged when rolling back to the turn-1 assistant."""
    sess = _new_session()
    git = EncreRollbackGit()
    notes = tmp_path / "notes.txt"
    t1_assistant_id = _write_turn(sess, git, 1, [(notes, "TURN1\n")])
    _write_turn(sess, git, 2, [(notes, "TURN2\n")])

    # Fork a child branch off the turn-1 assistant message.
    child = sess.create_branch(sess.active_branch_id, t1_assistant_id)
    sess.switch_branch(child.id)
    sess.add_message(role="user", content="child-q", branch_id=child.id)
    sess.add_message(role="assistant", content="child-a", branch_id=child.id)
    sess.switch_branch(
        sess.branches[sess.active_branch_id].parent_branch_id
        or child.parent_branch_id
    )

    # Rollback to turn-1 assistant on the original branch.
    _, _, found = sess.rollback_to(
        sess.active_branch_id, t1_assistant_id
    )

    assert found is True
    assert child.id not in sess.branches
    assert all(m.get("branch_id") != child.id for m in sess.messages)


def test_target_message_is_retained(tmp_path: Path) -> None:
    """Rollback removes messages with ``seq > target``, not ``>= target``."""
    sess = _new_session()
    git = EncreRollbackGit()
    notes = tmp_path / "notes.txt"
    t1 = _write_turn(sess, git, 1, [(notes, "TURN1\n")])
    _write_turn(sess, git, 2, [(notes, "TURN2\n")])

    sess.rollback_to(sess.active_branch_id, t1)

    ids = [m.get("id") for m in sess.messages]
    assert t1 in ids
    # The turn-2 assistant message is gone.
    assert all(
        m.get("content") != "done 2" for m in sess.messages
    )


def test_extract_file_paths_handles_apply_patch(tmp_path: Path) -> None:
    """``_extract_file_paths_from_messages`` covers apply_patch old/new paths."""
    sess = _new_session()
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    msg = {
        "role": "assistant",
        "branch_id": sess.active_branch_id,
        "tool_calls": [
            {
                "id": "ap1",
                "type": "function",
                "function": {
                    "name": "apply_patch",
                    "arguments": json.dumps(
                        {
                            "files": [
                                {"old_path": str(a), "new_path": str(b)},
                                {"old_path": str(b), "new_path": str(b)},
                            ]
                        }
                    ),
                },
            }
        ],
    }
    paths = _extract_file_paths_from_messages([msg], sess)
    assert str(a) in paths
    assert str(b) in paths
