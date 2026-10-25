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

"""History domain handlers: conversation search, rollback, branching, replay.

Owned client-message domain for everything that reaches backwards into a
conversation: the desktop search box, the rollback timeline, branch switching,
and the telemetry replay viewer. :class:`HistoryHandlers` is mixed into
:class:`encre.transport.ws.EncreWSHandler` and backs the
``ClientSearch`` / ``ClientRollback*`` / ``ClientSwitchBranch`` /
``ClientReplayGetSession`` entries of that handler's ``_DISPATCH`` table.

There are two distinct storage systems in play here, and the handlers have to
treat them differently:

- **Session history.** Conversation turns live as ``turn_*.json`` files under
  ``<data_dir>/sessions/<sid>/`` (sometimes encrypted at rest), and the
  rollback tree is a separate git-like store keyed by session id. Reads are
  done straight from disk rather than through the
  :class:`encre.server.session_manager.SessionManager` cache, because search
  needs to scan *every* session including ones the manager has not loaded, and
  because a scan through the cache would pin unrelated sessions in memory.
- **Telemetry.** ``data/telemetry/<session_id>.jsonl`` is an append-only event
  log written as the run happened, which is what replay reads back.

Routing into :class:`encre.server.session_manager.SessionManager`: mutation
paths (checkout, branch switch, branch rollback) do go through it -- they
cancel a running agent, mutate ``info.agent.session``, and persist via
``_save_session_async``. Read-only paths (search, log, replay) touch only the
disk stores.

Public API:

- :class:`HistoryHandlers`
- :func:`_build_search_texts`
"""

import asyncio
import contextlib
import json
import logging
import os
from typing import Any

from encre.server.protocol import (
    ClientReplayGetSession,
    ClientRollbackBranch,
    ClientRollbackCheckout,
    ClientRollbackLog,
    ClientSearch,
    ClientSwitchBranch,
)
from encre.server.session_manager import SessionState

logger = logging.getLogger("encre.transport.ws")


def _message_text(m: dict[str, Any]) -> str:
    """Return the plain text of a session message for content comparison.

    Content is either a plain string or an OpenAI-style block list; only
    ``text`` blocks carry user prose, everything else (images, tool blocks)
    contributes nothing to duplicate detection.
    """
    content = m.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _build_search_texts(dpath: str) -> list[dict[str, Any]]:
    """Extract searchable user/assistant texts from one session directory.

    Reads every ``turn_*.json`` in *dpath* in filename order and returns the
    plain text of each user and assistant message, pre-lowercased for
    case-insensitive substring matching by the caller.

    Args:
        dpath: The session directory to scan. May contain either plaintext or
            encrypted turn files.

    Returns:
        One dict per searchable message with ``role``, ``text`` and a
        lowercased ``low`` mirror used only for matching. Empty if nothing is
        readable.

    Note:
        Turn files are JSON arrays of message dicts; anything that is not a
        list of dicts, that fails to parse, or that fails to decrypt is
        skipped rather than raised, because one corrupt session must not make
        the whole search come back empty. Content can also be a list of
        content blocks, in which case only the ``text`` blocks are joined --
        image and tool blocks carry no searchable prose.
    """
    import pathlib

    from encre.crypto import decrypt

    msgs: list[dict[str, Any]] = []
    for fpath in sorted(pathlib.Path(dpath).iterdir()):
        if not fpath.name.startswith("turn_"):
            continue
        try:
            raw = fpath.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not raw:
            continue
        if not raw.startswith("["):
            with contextlib.suppress(Exception):
                raw = decrypt(raw)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, list):
            continue
        for m in data:
            role = m.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = m.get("content")
            if content is None:
                continue
            text = content if isinstance(content, str) else " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if text:
                msgs.append({"role": role, "text": text, "low": text.lower()})
    return msgs


class HistoryHandlers:
    """Conversation search, rollback log/checkout, branching and replay.

    Mixed into :class:`encre.transport.ws.EncreWSHandler`; carries no state of
    its own and reads ``_manager``, ``_default_config``, ``_info``,
    ``_current_session_id``, ``_renderer_session_messages`` and ``_send`` from
    the composed handler.

    Lifecycle: stateless, constructed once per server.

    Invariants:

    - Any path that mutates a session while an agent task may be running
      cancels that task *and* closes its backend client first, and only then
      touches the session. A rollback racing a live turn would otherwise let
      the turn's ``finally`` block overwrite the restored state.
    - Rollback and checkout are session-destructive operations, so a session
      left empty by one of them is deleted from disk and the connection
      unbound; otherwise an empty conversation resurfaces in the sidebar.
    - Rollback invalidates the compact summary, since it summarised work that
      no longer exists on this branch.

    Args:
        None. Inherits ``__init__`` from
        :class:`~encre.protocol.handlers.base.HandlerBase` through the composed
        handler.
    """

    def _do_search(self, query: str) -> list[dict[str, Any]]:
        """Search all sessions by name and by message content.

        Returns at most about 60 results tagged ``conversation`` so the
        desktop search box can jump straight to a past turn.

        Args:
            query: The user's free text; compared case-insensitively against
                both the session name and the message bodies.

        Returns:
            Matching results, most recently active session first, capped at 60.
            Each carries a ``snippet`` (the matched text with a little context
            around it) and a ``preview`` (the session's opening turn).

        Note:
            Session directories are ordered by ``meta.json`` last-active time
            rather than scanned alphabetically, so a relevant old session never
            wins a page of recent noise. Workspace *file* search is entirely
            client-side against registered workspaces, so this handler never
            walks ``os.getcwd()`` and cannot surface files from a directory the
            user has not registered.
        """
        from encre.session import EncreSession

        q = query.strip().lower()
        if not q:
            return []

        sessions_dir = self._manager._get_sessions_dir()
        entries: list[tuple[float, str, str]] = []
        for entry in os.scandir(sessions_dir):
            if entry.is_dir() and not entry.name.startswith("."):
                last = EncreSession.read_meta_last_active(entry.path, 0.0)
                entries.append((last, entry.name, entry.path))
        entries.sort(key=lambda x: x[0], reverse=True)

        results: list[dict[str, Any]] = []
        for _last, sid, dpath in entries:
            if len(results) >= 60:
                break
            # The name the user typed for a session is stored in the manager's
            # session index, not in any turn file, so it has to be matched
            # explicitly here. This is the whole reason "search by session
            # name" works at all.
            name = ""
            try:
                name = str((self._manager._index.get(sid) or {}).get("name", "") or "")
            except Exception:
                name = ""
            if name and q in name.lower():
                preview = EncreSession.load_preview(dpath) or "Empty session"
                results.append({
                    "kind": "conversation",
                    "session_id": sid,
                    "role": "user",
                    "name": name,
                    "snippet": preview[:120],
                    "preview": preview,
                })
                continue
            msgs = _build_search_texts(dpath)
            hit: dict[str, Any] | None = None
            for msg in msgs:
                if q in msg["low"]:
                    idx = msg["low"].index(q)
                    text = msg["text"]
                    start = max(0, idx - 40)
                    end = min(len(text), idx + len(q) + 80)
                    hit = {
                        "kind": "conversation",
                        "session_id": sid,
                        "role": msg["role"],
                        "name": name,
                        "snippet": text[start:end].strip()[:120],
                    }
                    break
            if hit:
                hit["preview"] = EncreSession.load_preview(dpath) or "Empty session"
                results.append(hit)
        return results[:60]

    async def _h_search(self, ws: Any, msg: ClientSearch) -> None:
        """Answer a conversation search request.

        Args:
            ws: The connection to answer.
            msg: ``ClientSearch`` with the query text and a client-supplied
                ``seq`` sequence number.

        Returns:
            None. Sends ``search_results``.

        Note:
            The request's ``seq`` is echoed back unchanged. The desktop search
            box fires a request per keystroke, so the client discards any
            response whose ``seq`` is behind the one it is waiting on. Without
            the echo, a slow old search could overwrite the results of a newer
            one.
        """
        results = self._do_search(msg.query)
        await self._send(ws, "search_results", results=results, seq=msg.seq)

    async def _h_rollback_log(self, ws: Any, msg: ClientRollbackLog) -> None:
        """Return the rollback commit tree for a session.

        Args:
            ws: The connection to answer.
            msg: ``ClientRollbackLog``; ``session_id`` may be omitted to mean
                the connection's current session.

        Returns:
            None. Sends ``rollback_log`` with the commit tree, or an ``error``
            frame with code ``no_session`` when there is no session to inspect.

        Note:
            Read-only and purely disk-based. The rollback store is a separate
            git-like index over session snapshots, so this never touches the
            live :class:`~encre.server.session_manager.SessionInfo`.
        """
        sid = msg.session_id or self._current_session_id
        if not sid:
            await self._send(ws, "error", message="No active session", code="no_session")
            return
        from encre.rollback import EncreRollbackGit
        rb = EncreRollbackGit()
        commits = rb.tree(sid)
        await self._send(ws, "rollback_log", session_id=sid, commits=commits)

    async def _h_rollback_checkout(self, ws: Any, msg: ClientRollbackCheckout) -> None:
        """Restore a session to one specific rollback commit.

        Cancels any running agent first, checks the commit out of the rollback
        store, persists the restored session, and returns the resulting
        conversation together with the text of the last user message so the
        desktop input box can be pre-filled for re-editing.

        Args:
            ws: The connection to answer.
            msg: ``ClientRollbackCheckout`` with the target ``commit_hash``;
                ``session_id`` may be omitted to mean the current session.

        Returns:
            None. Sends ``rollback_checkout`` with the restored messages, turn
            count, sidebars and ``user_input``, or an ``error`` frame with code
            ``invalid_request`` / ``not_found``.

        Raises:
            Never.

        Note:
            The cancel-then-restore ordering is the load-bearing part. A live
            turn would run its ``finally`` block after the checkout and write
            its in-memory state back over the restored snapshot, silently
            reverting the rollback. The backend client is also closed so an
            in-flight model request cannot race the restore, and the slot is
            released so the concurrency counter stays consistent. The task is
            awaited with a 0.5 s cap so a stuck finally block cannot hang the
            response, and any failure to wait is swallowed on purpose.
        """
        sid = msg.session_id or self._current_session_id
        if not sid or not msg.commit_hash:
            await self._send(ws, "error",
                message="Missing session_id or commit_hash",
                code="invalid_request", session_id=sid or "")
            return
        session = self._manager.load_or_create_session(sid, config=self._default_config)
        # Cancel any live agent task before restoring, otherwise the task's
        # finally block writes its in-memory state back over the snapshot this
        # checkout just restored.
        if session.agent_task and not session.agent_task.done():
            self._manager.set_session_state(session.session_id, SessionState.IDLE)
            session.agent.loop.cancel()
            session.agent_task.cancel()
            with contextlib.suppress(Exception):
                await session.agent.loop.backend.aclose()
            self._manager.release_slot()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(session.agent_task, timeout=0.5)
            session.agent_task = None
        from encre.rollback import EncreRollbackGit
        rb = EncreRollbackGit()
        ok = rb.checkout(session.agent.loop.session, msg.commit_hash)
        if not ok:
            await self._send(ws, "error",
                message=f"Commit not found: {msg.commit_hash[:8]}...",
                code="not_found", session_id=sid)
            return
        self._current_session_id = sid
        self._info = session
        # Drop in-memory loop state (sub-agent follow-ups) recorded for the
        # turns this checkout just discarded; metadata-derived state was
        # already pruned inside checkout itself.
        rb_loop = getattr(session, "agent", None)
        rb_loop = getattr(rb_loop, "loop", None) if rb_loop is not None else None
        if rb_loop is not None and hasattr(rb_loop, "clear_transient_state"):
            rb_loop.clear_transient_state()
        await self._manager._save_session_async(session)
        s = session.agent.loop.session
        msgs = [m for m in s.messages if m.get("role") != "system"]
        # Reconstruct the text the user is most likely to re-type: the last
        # user message before the checkout point is returned so the desktop
        # can restore it into the input box.
        user_input = ""
        for m in reversed(s.messages):
            if m.get("role") == "user":
                c = m.get("content", "")
                user_input = c if isinstance(c, str) else ""
                break
        await self._send(ws, "rollback_checkout",
            session_id=sid, commit_hash=msg.commit_hash, messages=msgs,
            turn_count=s.turn_count,
            plan_items=s.plan_items,
            artifacts=s.artifacts,
            references=s.references,
            user_input=user_input)

    async def _h_switch_branch(self, ws: Any, msg: ClientSwitchBranch) -> None:
        """Make a different branch of the conversation the active one.

        Switching is cheap and non-destructive: every branch keeps its
        messages, only the active pointer moves. Because no turn is interrupted
        this handler does not cancel the agent, unlike
        :meth:`_h_rollback_checkout` and :meth:`_h_rollback_branch`.

        Args:
            ws: The connection to answer.
            msg: ``ClientSwitchBranch`` naming the branch; ``session_id`` may
                be omitted to mean the current session.

        Returns:
            None. Sends ``branch_switched`` with the branch's rendered messages
            and the full branch list, or an ``error`` frame with code
            ``no_session`` / ``branch_not_found``.

        Note:
            The session is loaded from disk when it is not already cached, so a
            branch switch works after a restart without having opened the
            session in a chat view first. Token counts are reported as zeros:
            they are recomputed by the loop on the next run, and switching
            branches does not change the actual prompt size until then.
        """
        sid = msg.session_id or self._current_session_id
        if not sid:
            await self._send(ws, "error", message="No active session", code="no_session")
            return
        info = self._manager.get_session(sid)
        if info is None:
            info = self._manager.load_or_create_session(sid, config=self._default_config)
        self._manager.touch(sid)
        sess = info.agent.session
        if msg.branch_id not in sess.branches:
            await self._send(ws, "error", message=f"Branch not found: {msg.branch_id}", code="branch_not_found",
                             session_id=sid)
            return
        sess.switch_branch(msg.branch_id)
        msgs = self._renderer_session_messages(sess)
        branches_list = [b.__dict__ for b in sess.branches.values()]
        await self._send(ws, "branch_switched",
            session_id=sid,
            branch_id=sess.active_branch_id,
            messages=msgs,
            branches=branches_list,
            artifacts=sess.artifacts,
            references=sess.references,
            tokens={"input": 0, "output": 0, "total": 0},
        )

    async def _h_rollback_branch(self, ws: Any, msg: ClientRollbackBranch) -> None:
        """Roll back the active branch to an earlier message.

        Cancels a running agent first, rolls the branch back with
        ``session.rollback_to`` (which removes the target message itself in
        the same pass), clears the now-stale compact summary, and reports the
        result as a branch switch so the client refetches nothing but redraws.

        Args:
            ws: The connection to answer.
            msg: ``ClientRollbackBranch`` with the branch id and the message id
                to roll back to; ``session_id`` may be omitted to mean the
                current session.

        Returns:
            None. Sends ``session_deleted`` if the session becomes empty,
            ``branch_switched`` otherwise, or an ``error`` frame with code
            ``no_session`` / ``rollback_error``.

        Raises:
            Never.

        Note:
            The session id is rebound to this session because a rollback can
            follow a switch on a different session, and the next request from
            this connection should continue here.
        """
        sid = msg.session_id or self._current_session_id
        if not sid:
            await self._send(ws, "error", message="No active session", code="no_session")
            return
        info = self._manager.get_session(sid)
        if info is None:
            info = self._manager.load_or_create_session(sid, config=self._default_config)
        self._manager.touch(sid)
        # Cancel any live agent task before rolling back; otherwise the task's
        # finally block would write its in-memory state back over the restored
        # branch.
        agent_survived = False
        if info.agent_task and not info.agent_task.done():
            self._manager.set_session_state(sid, SessionState.IDLE)
            info.agent.loop.cancel()
            info.agent_task.cancel()
            with contextlib.suppress(Exception):
                await info.agent.loop.backend.aclose()
            self._manager.release_slot()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(info.agent_task, timeout=0.5)
            # The task MUST be quiesced before the truncation below. A task
            # that survives the cancel window keeps running in the background
            # and appends to the session AFTER the rollback deleted its turns,
            # resurrecting exactly the user message the client is about to
            # re-send -- the adjacent duplicate user turns the renderer then
            # collapses. Drop the discarded reply's text first, then wait in
            # bounded polling steps; a task stuck in an un-cancellable section
            # gets logged and swept defensively after the truncation.
            if not info.agent_task.done():
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 5.0
                while not info.agent_task.done() and loop.time() < deadline:
                    await asyncio.sleep(0.05)
            if not info.agent_task.done():
                agent_survived = True
                logger.warning(
                    "[rollback] agent task still alive %.1fs after cancel for session=%s -- sweeping resurrected copies after truncation",
                    5.0,
                    sid,
                )
            info.agent_task = None
        self._info = info
        self._current_session_id = sid
        sess = info.agent.session
        # Bracket the rollback with rollback commits like message edit/delete
        # do: the discarded state is only recoverable (and only off the HEAD
        # chain) if it was checkpointed before the truncation.
        #
        # Grab the target message dict BEFORE rollback_to removes it: the
        # defensive sweep (agent_survived path) needs its text to recognise
        # resurrected copies of the rolled-back turn.
        target_msg = next(
            (
                m
                for m in sess.messages
                if m.get("id", "") == msg.message_id
                or m.get("id", "").endswith(":M:" + msg.message_id)
            ),
            None,
        )
        if target_msg is not None:
            info.agent.loop.rollback.commit(sess, f"before_rollback_{msg.message_id[:8]}")
        # include_target: the client removes the target locally and loads its
        # text into the input box for re-editing; the same removal happens
        # inside rollback_to now, so a page refresh cannot resurrect the
        # message the user just rolled back. Legacy messages may carry no
        # ``branch_id`` at all; the identity-based removal inside rollback_to
        # does not depend on it.
        _removed, target_branch_id, target_found = sess.rollback_to(
            msg.branch_id, msg.message_id, include_target=True)
        if not target_found:
            await self._send(ws, "error", message="Message not found", code="rollback_error",
                             session_id=sid)
            return
        # Rollback undoes work the compact summary described, so the summary
        # is now wrong about this branch. Drop it and let the next compaction
        # regenerate from the restored history.
        sess.metadata.pop("user_requirements_summary", None)
        # Defensive sweep: a task that survived the cancel window may have
        # appended a copy of the rolled-back user turn between the truncation
        # points. Any surviving non-synthetic user message carrying the exact
        # target text is such a resurrected copy -- remove it too, otherwise
        # the re-sent replacement lands next to it and the renderer logs an
        # adjacent-duplicate warning on every subsequent reconciliation.
        if agent_survived:
            _target_text = _message_text(target_msg)
            before = len(sess.messages)
            sess.messages = [
                m
                for m in sess.messages
                if not (
                    m.get("role") == "user"
                    and not m.get("is_synthetic")
                    and m.get("id") != (target_msg.get("id") or "")
                    and m.get("branch_id", "") in ("", target_branch_id)
                    and _message_text(m) == _target_text
                )
            ]
            swept = before - len(sess.messages)
            if swept:
                logger.warning(
                    "[rollback] swept %d resurrected copy/copies of the rolled-back turn for session=%s",
                    swept,
                    sid,
                )
        # The raw reassignment above bypasses every cache invalidation:
        # without an explicit dirty flag the context cache would keep
        # serving the removed message to the next model request.
        # rebuild_runtime_caches also re-synchronises _branch_last_seq /
        # _branch_last_message_id, which rollback_to had already pointed at
        # the now-removed target message -- leaving them stale chained the
        # next sent message onto a deleted parent.
        sess.rebuild_runtime_caches()
        # A branch rolled back to nothing is a session with no turns. It is
        # deleted from disk so it cannot resurface as an empty "ghost" entry
        # in the sidebar after a restart. A non-empty compact archive means the
        # pre-compaction history is still recoverable and the next save would
        # persist it, so such a session must survive.
        if not sess.messages and not sess.has_compact_archive():
            self._manager.delete_session_from_disk(sid)
            await self._send(ws, "session_deleted", session_id=sid)
            return
        try:
            _t = asyncio.ensure_future(self._manager._save_session_async(info))
            self._tasks.add(_t)
        except Exception:
            pass
        # Second bracket: the state the rollback landed on.  Its file states
        # are the restored content, so a later overlap restore for this
        # boundary reads the right bytes instead of the discarded turn's.
        info.agent.loop.rollback.commit(sess, f"rollback_{msg.message_id[:8]}")
        msgs = self._renderer_session_messages(sess)
        branches_list = [b.__dict__ for b in sess.branches.values()]
        await self._send(ws, "branch_switched",
            session_id=sid,
            branch_id=sess.active_branch_id,
            messages=msgs,
            branches=branches_list,
            artifacts=sess.artifacts,
            references=sess.references,
            tokens={"input": 0, "output": 0, "total": 0},
        )

    async def _h_replay_get_session(self, ws: Any, msg: ClientReplayGetSession) -> None:
        """Serve the recorded telemetry for a session to the replay viewer.

        Reads ``data/telemetry/<session_id>.jsonl`` through
        :class:`encre.replay.ReplayPlayer` and returns the whole event stream,
        a summary, and the turn boundaries the viewer uses to split the run
        into readable steps.

        Args:
            ws: The connection to answer.
            msg: ``ClientReplayGetSession`` naming the session to replay.

        Returns:
            None. Always sends ``replay_session``, on success or failure.

        Note:
            A missing or unreadable log is not an ``error`` frame. The replay
            panel is written to render an empty stream plus an ``error`` field,
            which is friendlier than a dead panel: the session may simply have
            had no telemetry recorded. Failures are logged with a traceback so
            real corruption is still visible in server logs.
        """
        try:
            from encre.replay import ReplayPlayer
            _player = ReplayPlayer(msg.session_id)
            await self._send(ws, "replay_session",
                session_id=msg.session_id,
                summary=_player.summary(),
                events=[ev.to_dict() for ev in _player.event_stream()],
                turn_boundaries=_player.turn_boundaries(),
            )
        except Exception as _replay_exc:
            logger.warning("[ws] replay failed: %s", _replay_exc, exc_info=True)
            await self._send(ws, "replay_session",
                session_id=getattr(msg, "session_id", ""),
                summary={},
                events=[],
                turn_boundaries=[],
                error=str(_replay_exc),
            )
