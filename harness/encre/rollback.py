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

from encre.logging_config import get_logger

"""Git‑style conversation rollback with SHA‑256 content‑addressable commits.

Layout
======

Each session gets a directory under ``<data_dir>/rollback/<session_id>/``::

    refs/heads/master       ->  HEAD commit hash (plain text)
    objects/ab/cdef1234...  ->  single commit blob (encrypted JSON)

The commit graph is a singly‑linked list (parent -> parent -> ... -> root).

A commit blob contains::

    {
        "parent":  "hex hash of parent commit or null",
        "timestamp": 1716200000.0,
        "turn_count": 5,
        "message":  "turn_5",
        "state":    { ... full EncreSession.to_dict() output ... }
    }

The commit hash is ``SHA‑256(compact‑JSON(commit_blob))`` truncated to 40 hex
chars (like a git abbreviated hash) -- unique per content, reproducible.

Usage::

    rb = EncreRollbackGit()
    head = rb.commit(session)             # returns commit hash
    log_entries = rb.log(session_id)      # list the chain
    rb.checkout(session, "a1b2c3d4...")  # restore session state
"""

import hashlib
import json
import logging
import pathlib
import time
from typing import Any

from encre import secure_io

_log = logging.getLogger("encre.rollback")

__all__ = ["CommitEntry", "EncreRollbackGit"]


# ── storage layout ──────────────────────────────────────────────────────────

def _resolve_base() -> pathlib.Path:
    """Rollback store root -- declared in ``encre.lifecycle``.

    The root used to be a second, independent hard-coded path living under a
    *different* data root (``~/.encre``) than everything else
    (``~/.dunimd/encre``), which is why rollback survived session deletion
    unnoticed.  Resolving it through the lifecycle module keeps the on-disk
    layout defined in exactly one place.
    """
    from encre.lifecycle import rollback_root

    return rollback_root()


_BASE = _resolve_base()
_HASH_LEN = 40  # characters -- like a full git hash
_INDEX_FILE = _BASE / "index.json"


def _session_dir(session_id: str) -> pathlib.Path:
    return _BASE / session_id


def _read_file_b64(file_path: str) -> str:
    """Read *file_path* as base64 (8 MiB cap); missing/unreadable -> empty."""
    import base64
    try:
        with open(file_path, "rb") as fh:
            return base64.b64encode(fh.read(8 * 1024 * 1024)).decode("ascii")
    except OSError:
        return ""


def _objects_dir(session_id: str) -> pathlib.Path:
    return _session_dir(session_id) / "objects"


def _refs_dir(session_id: str) -> pathlib.Path:
    return _session_dir(session_id) / "refs" / "heads"


def _head_path(session_id: str) -> pathlib.Path:
    return _refs_dir(session_id) / "master"


def _obj_path(session_id: str, commit_hash: str) -> pathlib.Path:
    """``objects/<hash[:2]>/<hash[2:]>``"""
    return _objects_dir(session_id) / commit_hash[:2] / commit_hash[2:]


# ── commit object ───────────────────────────────────────────────────────────

class CommitEntry:
    """A lightweight snapshot of a commit, suitable for log output."""

    __slots__ = ("commit_hash", "message", "parent", "timestamp", "turn_count")

    def __init__(
        self,
        commit_hash: str,
        parent: str | None,
        timestamp: float,
        turn_count: int,
        message: str,
    ) -> None:
        self.commit_hash = commit_hash
        self.parent = parent
        self.timestamp = timestamp
        self.turn_count = turn_count
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash": self.commit_hash,
            "parent": self.parent,
            "timestamp": self.timestamp,
            "turn_count": self.turn_count,
            "message": self.message,
        }


# ── manager ─────────────────────────────────────────────────────────────────

class EncreRollbackGit:
    """Git‑style versioned conversation history.

    Each ``commit()`` snapshots the full message list of a session and
    chains it via a parent hash.  ``checkout()`` restores any commit.
    """

    def __init__(self) -> None:
        self._session_index: dict[str, str] = {}  # session_id -> path
        self._load_index()

    # ── index (session cross‑reference) ─────────────────────────────────

    def _load_index(self) -> None:
        try:
            if _INDEX_FILE.exists():
                self._session_index = secure_io.read_json(_INDEX_FILE, default={})
        except Exception:
            self._session_index = {}

    def _save_index(self) -> None:
        secure_io.write_json(_INDEX_FILE, self._session_index)

    # ── object I/O ──────────────────────────────────────────────────────

    @staticmethod
    def _hash_object(obj: dict[str, Any]) -> str:
        """SHA‑256 of the compact JSON representation."""
        payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_HASH_LEN]

    @staticmethod
    def _read_head(session_id: str) -> str | None:
        p = _head_path(session_id)
        if not p.exists():
            return None
        return secure_io.read_text(p, default="").strip() or None

    @staticmethod
    def _write_head(session_id: str, commit_hash: str) -> None:
        secure_io.write_text(_head_path(session_id), commit_hash)

    @staticmethod
    def _write_object(session_id: str, commit_hash: str, data: dict[str, Any]) -> None:
        secure_io.write_json(_obj_path(session_id, commit_hash), data)

    @staticmethod
    def _read_object(session_id: str, commit_hash: str) -> dict[str, Any] | None:
        p = _obj_path(session_id, commit_hash)
        if not p.exists():
            return None
        return secure_io.read_json(p, default=None)

    # ── public API ──────────────────────────────────────────────────────

    def commit(
        self,
        session: Any,  # EncreSession -- duck‑typed for decoupling
        message: str = "",
    ) -> str:
        """Snapshot the current session state and append to the commit chain.

        Returns
        -------
        str
            40‑hex‑char commit hash.
        """
        session_id = session.id
        parent = self._read_head(session_id)

        # Commit-time file states: post-turn content of every file touched
        # since the previous commit.  Checkout resolves these along the
        # target's ancestor chain to restore files that were written by both
        # kept and removed turns, where the pre-write snapshot is too old.
        file_states: dict[str, str] = {}
        touched = getattr(session, "files_touched_since_commit", None)
        if isinstance(touched, set):
            for file_path in touched:
                file_states[file_path] = _read_file_b64(file_path)
            touched.clear()

        blob: dict[str, Any] = {
            "parent": parent,
            "timestamp": time.time(),
            "turn_count": getattr(session, "turn_count", 0),
            "message": message or f"turn_{getattr(session, 'turn_count', 0)}",
            "state": getattr(session, "to_dict", lambda: {})(),
            "file_states": file_states,
        }

        commit_hash = self._hash_object(blob)
        self._write_object(session_id, commit_hash, blob)
        self._write_head(session_id, commit_hash)

        # Update index
        self._session_index[session_id] = str(_session_dir(session_id))
        self._save_index()

        return commit_hash

    def log(self, session_id: str, max_count: int = 50) -> list[CommitEntry]:
        """Walk the commit chain from HEAD back to root."""
        entries: list[CommitEntry] = []
        current = self._read_head(session_id)
        visited: set[str] = set()

        while current and len(entries) < max_count:
            if current in visited:
                break
            visited.add(current)
            obj = self._read_object(session_id, current)
            if obj is None:
                break
            entries.append(CommitEntry(
                commit_hash=current,
                parent=obj.get("parent"),
                timestamp=obj.get("timestamp", 0),
                turn_count=obj.get("turn_count", 0),
                message=obj.get("message", ""),
            ))
            current = obj.get("parent")

        return entries

    def checkout(self, session: Any, commit_hash: str) -> bool:
        """Restore session state to a specific commit.

        The in‑memory ``session.messages``, ``session.turn_count``, etc.
        are replaced with the values from the commit snapshot.

        After checkout the commit chain is NOT truncated -- the restored
        commit remains HEAD; a subsequent ``commit()`` will fork from it.

        Returns
        -------
        bool
            ``True`` if the commit was found and applied.
        """
        session_id = session.id
        obj = self._read_object(session_id, commit_hash)
        if obj is None:
            return False

        state = obj.get("state", {})
        if not state:
            return False

        # Scoped file restore, BEFORE the state is applied (the frontend may
        # re-render as soon as the session changes).  Only files touched by
        # the messages being discarded are reverted -- the previous blanket
        # ``restore_file_snapshots()`` also reverted files written by kept
        # turns, silently destroying work the rollback was supposed to keep.
        try:
            from encre.session import _extract_file_paths_from_messages
            kept_ids = {
                m.get("id", "") for m in (state.get("messages") or [])
                if isinstance(m, dict)
            }
            removed_msgs = [
                m for m in session.messages if m.get("id", "") not in kept_ids
            ]
            removed_paths = _extract_file_paths_from_messages(removed_msgs, session)
        except Exception as exc:
            _log.warning("[checkout] removed-path extraction failed: %s", exc)
            removed_paths = set()
        # Content as of the target commit: walk the target's own ancestor
        # chain and keep the nearest (newest) file state per path.  Falls
        # back to the pre-write snapshot for paths never committed (created
        # after the target commit), where empty means "delete".
        states = self._collect_file_states(session_id, commit_hash)
        restored = 0
        apply_snapshot = getattr(session, "apply_file_snapshot", None)
        for file_path in removed_paths:
            content = states.get(file_path)
            if content is None:
                content = session.file_snapshots.get(file_path)
            if content is None:
                continue
            if apply_snapshot is not None and apply_snapshot(file_path, content):
                restored += 1
        if restored:
            _log.info("[checkout] restored %d file(s) from snapshots", restored)

        # Restore session fields
        # Manage-tool lifecycle ops recorded after the target commit are
        # inverted BEFORE metadata is replaced: the journal is append-only,
        # so the suffix beyond the target's own journal is exactly what must
        # be undone (uninstalling plugins the target commit never saw).
        target_meta = state.get("metadata")
        target_ops = (
            target_meta.get("manage_ops")
            if isinstance(target_meta, dict) else None
        )
        current_meta = getattr(session, "metadata", None)
        current_ops = (
            current_meta.get("manage_ops")
            if isinstance(current_meta, dict) else None
        )
        if isinstance(target_ops, list) and isinstance(current_ops, list) \
                and len(current_ops) > len(target_ops):
            session.revert_manage_ops(keep=len(target_ops))
        session.messages = state.get("messages", [])
        session.turn_count = state.get("turn_count", 0)
        session.tool_call_count = state.get("tool_call_count", 0)
        session.metadata = state.get("metadata", {})
        session.plan_items = state.get("plan_items", [])
        session.artifacts = state.get("artifacts", [])
        session.references = state.get("references", [])
        session.file_snapshots = dict(state.get("file_snapshots", {}))
        # The pre-compaction archive is part of the snapshotted state; a
        # checkout that skips it loses the only copy of the pre-compact
        # history (the commit may predate the compaction that replaced the
        # branch messages).
        session.compact_archive = state.get("compact_archive")
        # The commit also snapshots the BRANCH TREE and the active pointer.
        # Restoring messages without the tree is what made the model go
        # "fresh session" after a rollback: the kept in-memory tree could
        # point at a branch created AFTER the commit (e.g. a fork from the
        # aborted turn), so get_branch_messages() resolved the restored
        # messages to an empty lineage and the request went out with a bare
        # system prompt while the UI still showed the full transcript.
        # Restore both, with fallbacks that keep the pointer always valid.
        restored_branches = {}
        for k, v in (state.get("branches") or {}).items():
            if isinstance(v, dict):
                try:
                    from encre.session import BranchMeta
                    restored_branches[k] = BranchMeta(**v)
                except Exception:
                    continue
            else:
                restored_branches[k] = v
        if restored_branches:
            session.branches = restored_branches
        if not session.branches:
            from encre.session import BranchMeta
            session.branches[session.active_branch_id] = BranchMeta(
                id=session.active_branch_id, created_at=session.created_at,
            )
        _active = state.get("active_branch_id")
        if _active and _active in session.branches:
            session.active_branch_id = _active
        elif session.active_branch_id not in session.branches:
            session.active_branch_id = next(iter(session.branches))
        session._branch_counter = max(
            int(state.get("_branch_counter", 0) or 0), session._branch_counter,
        )
        # History was rewritten by the checkout: the compacted-context
        # overlay no longer matches the restored messages.
        session.compacted_view = None
        # Rebuild the runtime caches (branch last-seq / last-message-id and
        # the context cache) against the RESTORED message list -- the stale
        # pre-checkout values would otherwise chain new messages onto
        # removed parents.
        session.rebuild_runtime_caches()
        # The restored metadata still carries summary state recorded during
        # the commit's own turn (turn summaries are written mid-turn while the
        # commit happens at turn end), so prune entries at/after the restored
        # turn -- that turn is about to be redone.
        session.prune_derived_state(session.turn_count, inclusive=True)
        # Memory nodes judged out of the discarded turns (inclusive: the
        # restored turn is about to be redone) must stop being recalled.
        # Git semantics: the checkout rewinds to this commit, so anything
        # ingested into memory after the commit's timestamp belongs to the
        # discarded future.  The timestamp rule also catches legacy nodes
        # that predate turn tagging.
        _target_ts = obj.get("timestamp")
        session.purge_memory_after_turn(
            session.turn_count,
            inclusive=True,
            created_after=(
                float(_target_ts)
                if isinstance(_target_ts, (int, float))
                and not isinstance(_target_ts, bool)
                and _target_ts > 0
                else None
            ),
        )
        session.updated_at = time.time()

        # Point HEAD at the restored commit.  Without this the head kept
        # referencing the newest commit, so every subsequent commit (turn
        # end, message edit/delete checkpoint) chained onto the DISCARDED
        # commits and the rollback tree kept recording the rolled-back
        # conversation.  Moving the head orphans the discarded commits;
        # future commits fork from the restored point instead.
        self._write_head(session_id, commit_hash)

        return True

    def _collect_file_states(
        self,
        session_id: str,
        start_hash: str | None,
        max_turn: int | None = None,
        before_ts: float | None = None,
    ) -> dict[str, str]:
        """Merge ``file_states`` deltas along a commit chain.

        Walks from *start_hash* back to the root, skipping commits excluded
        by the as-of filters and keeping the FIRST state seen per path -- the
        nearest one to the start, i.e. the newest included state.

        Two mutually usable as-of filters:

        - ``max_turn``: skip commits whose ``turn_count`` exceeds it. Weak
          across branches: ``turn_count`` is recounted per branch after a
          rollback, while historical commits recorded the count of the branch
          that was active when they were taken, so the two disagree.
        - ``before_ts``: skip commits whose wall-clock timestamp is after it.
          This is the Git-aligned ordering (commit sequence), stable no matter
          which branch was HEAD when each commit was written.
        """
        states: dict[str, str] = {}
        current = start_hash
        visited: set[str] = set()
        while current and current not in visited:
            visited.add(current)
            obj = self._read_object(session_id, current)
            if obj is None:
                break
            _ts = obj.get("timestamp", 0)
            _included = True
            if max_turn is not None and obj.get("turn_count", 0) > max_turn:
                _included = False
            if before_ts is not None and (
                not isinstance(_ts, (int, float))
                or isinstance(_ts, bool)
                or _ts > before_ts
            ):
                _included = False
            if _included:
                raw = obj.get("file_states")
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(v, str) and k not in states:
                            states[k] = v
            current = obj.get("parent")
        return states

    def file_states_asof_turn(self, session_id: str, max_turn: int) -> dict[str, str]:
        """Per-path file content as of the end of turn *max_turn*.

        Used by message-level rollbacks (branch rollback, edit, delete) to
        restore files that were written by both kept and removed turns with
        the exact content the rollback point had.
        """
        return self._collect_file_states(
            session_id, self._read_head(session_id), max_turn=max_turn,
        )

    def file_states_asof(
        self, session_id: str, before_ts: float | None = None,
    ) -> dict[str, str]:
        """Per-path file content as of wall-clock time *before_ts*.

        The branch-rollback restore path uses this with the target message's
        creation time: every commit written after the rollback point belongs
        to the discarded future and must not contribute file state, while
        commits taken on sibling branches before it still do -- a turn-count
        filter cannot express that (counts are per-branch and get recounted).
        ``before_ts=None`` means no time filter (full HEAD state); callers
        pass it when the target message carries no usable timestamp.
        """
        return self._collect_file_states(
            session_id, self._read_head(session_id), before_ts=before_ts,
        )

    def head(self, session_id: str) -> str | None:
        """Return the current HEAD commit hash (or None)."""
        return self._read_head(session_id)

    def head_entry(self, session_id: str) -> CommitEntry | None:
        """Return a ``CommitEntry`` for HEAD (or None)."""
        h = self._read_head(session_id)
        if h is None:
            return None
        obj = self._read_object(session_id, h)
        if obj is None:
            return None
        return CommitEntry(
            commit_hash=h,
            parent=obj.get("parent"),
            timestamp=obj.get("timestamp", 0),
            turn_count=obj.get("turn_count", 0),
            message=obj.get("message", ""),
        )

    def tree(self, session_id: str) -> list[dict[str, Any]]:
        """Return the full commit tree for a session (convenience)."""
        return [e.to_dict() for e in self.log(session_id)]

    def list_sessions_with_rollback(self) -> list[str]:
        """Return session IDs that have rollback history."""
        result: list[str] = []
        try:
            for entry in _BASE.iterdir():
                if entry.is_dir() and entry.name != "." and entry.name != "..":
                    h = self._read_head(entry.name)
                    if h:
                        result.append(entry.name)
        except OSError:
            logger.debug("exception suppressed", exc_info=True)
        return result
