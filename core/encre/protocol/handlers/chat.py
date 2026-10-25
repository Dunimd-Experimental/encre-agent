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

"""Chat-domain handlers: interrupting, steering and editing a live turn.

Owned client-message domain: the frames that act on an in-flight or finished
conversation instead of starting one.  :class:`ChatHandlers` is mixed into
:class:`encre.transport.ws.EncreWSHandler` and its methods back the
``ClientCancel`` / ``ClientToolStop`` / ``ClientSteer`` /
``ClientRespondQuestion`` / ``ClientEditMessage`` / ``ClientDeleteMessage``
entries of that handler's ``_DISPATCH`` table.

The control primitives come from the agent loop inside
:class:`encre.server.session_manager.SessionInfo.agent`, which wraps the Rust
core loop.  Three different interruption granularities are exposed here and it
is worth keeping them straight:

- ``cancel`` stops the whole turn.  It cancels the asyncio task, tells the
  loop to cancel, and force-closes the backend HTTP client so an in-flight
  model request aborts at once rather than running to its timeout.
- ``tool_stop`` aborts only the currently running tool.  The loop observes the
  tool-stop request, surfaces a normal error result to the model, and the turn
  keeps going with that result in context.
- ``steer`` does not interrupt at all: it pushes a mid-turn instruction into
  the loop's steering queue, which the loop drains between steps.

Routing into :class:`encre.server.session_manager.SessionManager` is uniform:
look up the session by the id carried by the frame, fall back to the
connection's current session, ``touch`` it so idle cleanup keeps it alive, and
reconcile its ``SessionState`` -- a cancelled or stale session must end up
``IDLE`` and must release the concurrency slot it held, otherwise the slot
leaks and eventually no session can run.

Message editing goes deeper: it does not merely rewrite text but also commits
a rollback checkpoint before mutating, restores the files that the truncated
turns had touched, and truncates the branch. See
:meth:`ChatHandlers._edit_message` and :meth:`ChatHandlers._delete_message`.

Public API:

- :class:`ChatHandlers`
"""

import contextlib
import logging
import time
from typing import Any

from encre.server.protocol import (
    ClientCancel,
    ClientDeleteMessage,
    ClientEditMessage,
    ClientRespondQuestion,
    ClientSteer,
    ClientToolStop,
)
from encre.server.session_manager import SessionState
from encre.tools.runtime import get_bash_abort_event

logger = logging.getLogger("encre.transport.ws")


class ChatHandlers:
    """Interrupt, steer, answer and edit the active conversation.

    Mixed into :class:`encre.transport.ws.EncreWSHandler`; carries no state of
    its own and uses the per-connection attributes installed by
    :class:`~encre.protocol.handlers.base.HandlerBase` (``_manager``,
    ``_current_session_id``, ``_info``, ``_adapter_manager``, ``_iclaw_task``).

    Lifecycle: stateless, constructed once per server along with the composed
    handler and shared across every socket.

    Invariants:

    - Every interruption of a running turn must leave
      :class:`encre.server.session_manager.SessionState` at ``IDLE`` and must
      call ``release_slot()``. Forgetting either leaves the session reporting
      "running" forever or leaks a concurrency slot.
    - ``_current_session_id`` / ``_info`` are only cleared here when the
      session actually disappears from disk, so the connection never points at
      a ghost session.
    - Editing and deleting a message always record a rollback commit before
      mutating, so the user can undo the edit from the rollback tree.

    Args:
        None. Inherits ``__init__`` from
        :class:`~encre.protocol.handlers.base.HandlerBase` through the composed
        handler.
    """

    async def _h_respond_question(self, ws: Any, msg: ClientRespondQuestion) -> None:
        """Answer a clarifying question the agent asked mid-turn.

        The loop suspends itself while a question is outstanding and resumes
        only when :meth:`EncreLoop.resolve_question` is called with the user's
        answers, so this handler is the unblocking step -- it does not send a
        response frame, the next ``ask``/``finish`` event produced by the loop
        is what the client sees.

        Args:
            ws: The connection that asked; unused beyond the dispatch contract.
            msg: ``ClientRespondQuestion`` carrying the ``answers`` value(s) the
                loop is waiting on -- a single string, or a list with one
                entry per question when the tool asked several at once.

        Returns:
            None.

        Note:
            If the session is not already live it is created on demand, so a
            late-arriving answer cannot be dropped because the session was
            cleaned up. Creating a session just to record the answer is safe:
            the loop will reject answers that match no pending question.
        """
        session = (
            self._manager.get_session(self._current_session_id)
            if self._current_session_id else None
        )
        if session is None:
            session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        session.agent.loop.resolve_question(msg.answers)

    async def _cancel_local_session(self, ws: Any, session: Any) -> None:
        """Cancel one local-manager session's run and kick its queue drain.

        Shared body of every local cancel path, so an explicitly-named
        session and the current-session fallback cannot drift apart.  The
        queue drain is kicked on both branches: a cancelled run frees the
        conversation, and its queued prompts must move up whether the run
        was actually stopped or had already gone idle before the cancel
        arrived.

        Args:
            ws: The connection to answer; receives the ``finish`` frame.
            session: The :class:`~encre.server.session_manager.SessionInfo`
                to cancel.
        """
        self._manager.touch(session.session_id)
        # The owning backlog, not always the desktop one: an iclaw-channel
        # session keeps its queue in the router's backlog.
        backlog = self._queue_backlog_for(session.session_id) or self._submit_backlog
        if session.agent_task and not session.agent_task.done():
            logger.info("[cancel] stopping desktop run session=%s", session.session_id)
            self._manager.set_session_state(session.session_id, SessionState.IDLE)
            session.agent.loop.cancel()
            session.agent_task.cancel()
            # Force-closing the backend client aborts any in-flight model
            # request immediately instead of letting the run block until the
            # backend's ~120 s request timeout. The client is rebuilt on the
            # next run, so dropping it here is cheap.
            with contextlib.suppress(Exception):
                await session.agent.loop.backend.aclose()
            self._manager.release_slot()
            # Delegate the history close-out: the cancelled run's asyncgen
            # finally (orphan tool_use tombstones + partial stream persist)
            # only runs when the loop's asyncgen finalizer gets around to it,
            # which can be deferred indefinitely.  The delegated task closes
            # the history promptly and persists it, without touching the
            # session state the cancel path already settled.
            self._manager.schedule_cancel_finalization(session)
            # Kick the queue drain explicitly.  The agent task's own
            # ``finally`` also calls schedule_drain, but if its unwinding
            # is slow (asyncgen finalizer defers until GC, backend HTTP
            # mid-flight, etc.) the queued prompts would otherwise sit
            # idle with the session state already IDLE.  The grace window
            # gives the agent task time to unwind so _h_run's wait loop
            # sees a clean start; duplicate schedule_drain calls are a
            # no-op because _drain_waiters tracks the pending task.
            with contextlib.suppress(Exception):
                backlog.schedule_drain(session.session_id, grace=0.8)
            await self._send(ws, "finish", reason="cancelled", session_id=session.session_id)
            return
        logger.info("[cancel] session=%s already idle; kicking queue drain", session.session_id)
        self._manager.set_session_state(session.session_id, SessionState.IDLE)
        self._manager.release_slot()
        # Session was already IDLE when the cancel arrived (nothing to
        # cancel), but a queued prompt may still be sitting there
        # waiting on a run that never started.  Trigger the drain so
        # the queue advances even without a prior live turn.
        with contextlib.suppress(Exception):
            backlog.schedule_drain(session.session_id, grace=0.0)
        await self._send(ws, "finish", reason="cancelled", session_id=session.session_id)

    async def _h_cancel(self, ws: Any, msg: ClientCancel) -> None:
        """Abort the currently running turn for a session.

        Four entry points are tried in order, because a run can live in any
        of several places and the client cannot know which one:

        1. A session explicitly named by ``session_id`` that resolves in the
           local :class:`SessionManager` -- the authoritative desktop path.
           The fallbacks below must never intercept it: a live (often stale)
           iclaw task on the same connection used to swallow the cancel, so
           the UI flipped to "stopped" while the desktop turn kept running
           server-side and the queued prompts sat undrained behind it.
        2. An iClaw task on this connection -- the in-CLI client run, which
           executes in its own asyncio task. Its agent loop is cancelled first
           so the gateway EventRouter stops forwarding events promptly.
        3. A gateway session known to the adapter manager's EventRouter -- the
           path used by IM platform traffic, which is not tracked in the local
           session store at all.
        4. The connection's current local session -- cancelled together with
           the backend HTTP client.

        Args:
            ws: The connection to answer; receives the ``finish`` frame.
            msg: ``ClientCancel``; ``session_id`` is optional and falls back
                to the connection's current session.

        Returns:
            None. Sends ``finish`` with ``reason="cancelled"`` on every path
            that had something to cancel.

        Note:
            When the local session's agent task is live, the backend HTTP
            client is force-closed so an in-flight model request aborts
            immediately instead of blocking until its ~120 s timeout. The
            backend is rebuilt on the next run, so dropping it is cheap. The
            concurrency slot is released on both branches -- the agent-task
            branch and the already-idle branch -- since the slot is held for
            the duration of a run, not for the duration of the task object.
        """
        # Named local session first: the id resolving in this server's own
        # manager is the authoritative target (entry point 1).
        if msg.session_id:
            named = self._manager.get_session(msg.session_id)
            if named is not None:
                await self._cancel_local_session(ws, named)
                return

        # iClaw task cancel (runs concurrently in its own task)
        if self._iclaw_task and not self._iclaw_task.done():
            # Also cancel the agent loop so the EventRouter stops promptly
            sid = msg.session_id or self._current_session_id or ""
            if sid and self._adapter_manager and self._adapter_manager.router:
                info = self._adapter_manager.router.session_manager.get_session(sid)
                if info and info.agent.loop:
                    info.agent.loop.cancel()
            self._iclaw_task.cancel()
            await self._send(ws, "finish", reason="cancelled", session_id=sid)
            return

        # Fallback for adapter/gateway flows, where the session is owned by
        # the gateway EventRouter rather than this server's SessionManager.
        if self._adapter_manager and self._adapter_manager.router:
            sid = msg.session_id or self._current_session_id or ""
            if sid and self._adapter_manager.router.cancel_session(sid):
                await self._send(ws, "finish", reason="cancelled", session_id=sid)
                return

        session = None
        if msg.session_id:
            session = self._manager.get_session(msg.session_id)
        if session is None and self._current_session_id:
            session = self._manager.get_session(self._current_session_id)
        if session is None:
            return
        await self._cancel_local_session(ws, session)

    async def _h_tool_stop(self, ws: Any, msg: ClientToolStop) -> None:
        """Abort the tool that is running right now, keeping the turn alive.

        Coarser than cancel and finer than nothing: the loop's tool-stop
        request makes the execution sites cancel the tool task and hand the
        model a normal error result, so the model can react ("that command
        failed, let me try differently") instead of the whole turn ending.

        Args:
            ws: The connection to answer.
            msg: ``ClientToolStop``; ``session_id`` is optional and falls back
                to the connection's current session.

        Returns:
            None. Always sends ``tool_stopped``, even when no session or no
            tool is live, so the client's spinner can reset in every case.

        Note:
            The session is resolved from ``msg.session_id`` first and then from
            the connection's current session, matching the other chat handlers.
        """
        sid = msg.session_id or self._current_session_id or ""
        info = self._manager.get_session(sid) if sid else None
        if info is None and self._current_session_id:
            info = self._manager.get_session(self._current_session_id)
        if info is not None and info.agent is not None and info.agent.loop is not None:
            loop = info.agent.loop
            loop.request_tool_stop()
            # The bash tool is a long-lived subprocess outside the loop's
            # control, so it needs its own abort event. That event is keyed by
            # the loop's internal session id (its own UUID), not by the id
            # SessionManager handed out, so the lookup goes through the loop's
            # session first and only then falls back to the manager id.
            inner_sid = ""
            loop_session = getattr(loop, "session", None)
            if loop_session is not None:
                inner_sid = getattr(loop_session, "id", "") or ""
            evt = get_bash_abort_event(inner_sid) or get_bash_abort_event(sid)
            if evt is not None:
                evt.set()
        else:
            evt = get_bash_abort_event(sid)
            if evt is not None:
                evt.set()
        await self._send(ws, "tool_stopped", session_id=sid)

    async def _h_steer(self, ws: Any, msg: ClientSteer) -> None:
        """Queue a mid-turn instruction for a running session.

        Steering is the non-interrupting counterpart of cancel: the prompt is
        pushed into the loop's steering queue and picked up between steps, so
        the current work finishes and the extra guidance is injected into the
        next model call.

        Args:
            ws: The connection to answer.
            msg: ``ClientSteer`` with the instruction text in ``prompt``.

        Returns:
            None. Sends ``steer_queued`` either way; when there is no live
            session the frame carries ``error="no_active_session"`` so the
            client can surface it rather than silently swallowing the input.

        Note:
            A missing session id resolves to a freshly created session instead
            of an error, so a steering request from an unbound connection is
            never dropped -- but without a running loop the queue push is
            skipped and the ``no_active_session`` frame is sent.
        """
        sid = msg.session_id or self._current_session_id or ""
        if sid:
            info = self._manager.get_session(sid)
        else:
            info = self._get_or_create_session()
        if info and info.agent and info.agent.loop:
            info.agent.loop._steer_queue.push(msg.prompt or "")
            logger.info("[steer] queued instruction for session=%s", info.session_id)
            await self._send(ws, "steer_queued", session_id=info.session_id)
        else:
            await self._send(ws, "steer_queued", session_id=sid, error="no_active_session")

    async def _h_edit_message(self, ws: Any, msg: ClientEditMessage) -> None:
        """Rewrite a user message and truncate everything after it.

        Editing a mid-conversation message is a real branch operation, not a
        string swap: the truncated turns are dropped, the files those turns
        touched are restored from snapshots, and two rollback commits bracket
        the change. See :meth:`_edit_message`.

        Args:
            ws: The connection to answer.
            msg: ``ClientEditMessage`` with the user-role index (or, preferred,
                the server message id) and replacement text.

        Returns:
            None. Sends ``messages_updated`` with the surviving messages, the
            new rollback head, and the plan/artifact/reference sidebars.

        Raises:
            Never. An unknown index or any other failure is reported as an
            ``error`` frame with code ``edit_error``.

        Note:
            Unlike :meth:`_h_delete_message` this handler does not gate on
            ``SessionState.IDLE``; editing a session while a turn is running is
            left to fail naturally rather than being refused up front.
        """
        session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        try:
            await self._edit_message(
                session, msg.message_index, msg.new_content, msg.message_id)
            msgs = [m for m in session.agent.session.messages if m.get("role") != "system"]
            head = session.agent.loop.rollback.head(session.agent.session.id) or ""
            await self._send(ws, "messages_updated", messages=msgs,
                             session_id=session.session_id, commit_hash=head,
                             plan_items=session.agent.session.plan_items,
                             artifacts=session.agent.session.artifacts,
                             references=session.agent.session.references)
        except Exception as e:
            logger.error(f"Edit message failed: {e}")
            await self._send(ws, "error", message=str(e), code="edit_error",
                             session_id=session.session_id)

    async def _h_delete_message(self, ws: Any, msg: ClientDeleteMessage) -> None:
        """Delete a user message and everything after it.

        Args:
            ws: The connection to answer.
            msg: ``ClientDeleteMessage`` with the user-role index.

        Returns:
            None. Sends ``session_deleted`` when the session becomes empty,
            ``messages_updated`` otherwise, or an ``error`` frame with code
            ``busy`` / ``delete_error``.

        Note:
            Deletion is refused while the session is running (``code="busy"``)
            because a live turn would keep appending to the very branch that is
            being truncated. If the last message is removed the session is
            deleted from disk and the connection's session binding is cleared,
            so the empty conversation cannot resurface in the sidebar after a
            restart.
        """
        session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        if session.state != SessionState.IDLE:
            # Same reconciliation as the run handler: a force-cancel can leave
            # the flag set with the agent task already dead; without this the
            # session refuses edits/deletes until a restart.
            task_alive = session.agent_task is not None and not session.agent_task.done()
            if task_alive:
                await self._send(ws, "error", message="Session is running, cannot delete messages", code="busy",
                                 session_id=session.session_id)
                return
            self._manager.set_session_state(session.session_id, SessionState.IDLE)
        try:
            await self._delete_message(session, msg.message_index, msg.message_id)
            # Deleting the final message leaves a session with no turns. It is
            # removed from disk and the connection is unbound, otherwise it
            # would reappear as an empty "ghost" entry in the sidebar.
            if not session.agent.session.messages:
                sid = session.session_id
                self._manager.delete_session_from_disk(sid)
                self._current_session_id = None
                self._info = None
                await self._send(ws, "session_deleted", session_id=sid)
                return
            msgs = [m for m in session.agent.session.messages if m.get("role") != "system"]
            head = session.agent.loop.rollback.head(session.agent.session.id) or ""
            await self._send(ws, "messages_updated", messages=msgs,
                             session_id=session.session_id, commit_hash=head,
                             plan_items=session.agent.session.plan_items,
                             artifacts=session.agent.session.artifacts,
                             references=session.agent.session.references)
        except Exception as e:
            logger.error(f"Delete message failed: {e}")
            await self._send(ws, "error", message=str(e), code="delete_error",
                             session_id=session.session_id)

    @staticmethod
    def _resolve_user_target(sess: Any, index: int, message_id: str) -> int:
        """Resolve a renderer-visible user-message locator to a raw position.

        ``message_id`` wins: server message ids are stable across compaction,
        sibling branches and synthetic plumbing. Without it, ``index`` is
        resolved against the RENDERER projection (the exact list the frontend
        numbers its ``data-user-idx`` on), never the raw stored list -- the
        raw list also holds sibling branches' user copies and compaction
        blocks, so counting it directly drifted the resolved target one turn
        above the message the user actually clicked.

        Args:
            sess: The agent session exposing ``messages`` and the renderer
                projection used by the desktop timeline.
            index: The user-visible ordinal of the target (fallback).
            message_id: The server message id of the target (preferred).

        Returns:
            The position of the target inside ``sess.messages``, or ``-1``
            when no message matches either locator.
        """
        msgs = list(sess.messages)
        if message_id:
            for i, m in enumerate(msgs):
                mid = m.get("id", "")
                if mid == message_id or mid.endswith(":M:" + message_id):
                    return i
        from encre.protocol.render import renderer_session_messages
        target_id = ""
        user_idx = -1
        for m in renderer_session_messages(sess):
            role = m.get("role", "")
            # The projection splices UI notices in as their own roles and
            # strips synthetic plumbing, so everything left with role "user"
            # is exactly what the frontend counts as a real user turn.
            if role != "user":
                continue
            user_idx += 1
            if user_idx == index:
                target_id = m.get("id", "")
                break
        if not target_id:
            return -1
        for i, m in enumerate(msgs):
            mid = m.get("id", "")
            if mid == target_id or mid.endswith(":M:" + target_id):
                return i
        return -1

    async def _edit_message(self, session: Any, index: int, new_content: str, message_id: str = "") -> None:
        """Rewrite one user message and discard every turn after it.

        This is a branch-level operation. The target is resolved by
        ``message_id`` when present, otherwise ``index`` is matched against
        the renderer projection (0 is the first user message), which mirrors
        the frontend's ``data-user-idx``.

        Steps, in order:

        1. Commit the pre-edit state as a rollback checkpoint, so the edit can
           be undone from the rollback tree.
        2. Restore the file snapshots for paths touched only by the turns that
           are being thrown away. Files that the surviving messages still touch
           are left exactly as they are -- restoring those would revert work
           the user chose to keep.
        3. Rewrite the message content, truncate ``messages`` to keep it and
           everything before it, and fix up ``turn_count`` and ``updated_at``.
        4. Rebuild the session's runtime caches, then commit the post-edit
           state as a second checkpoint.
        5. Persist asynchronously, unless this is a scratch ``temp_chat``.

        Args:
            session: The :class:`~encre.server.session_manager.SessionInfo` to
                edit.
            index: The user-visible ordinal of the message to rewrite.
            new_content: The replacement text.
            message_id: The server message id of the message to rewrite
                (preferred over ``index``).

        Returns:
            None.

        Raises:
            ValueError: When no user message at that index/id exists.

        Note:
            ``turn_count`` is clamped to at least 1 so an edit of the first
            message does not leave the session reporting zero turns.
        """
        sess = session.agent.session
        msgs = list(sess.messages)
        i = self._resolve_user_target(sess, index, message_id)
        if i < 0:
            raise ValueError(f"Message index {index} not found")
        target = msgs[i]
        from encre.session import _extract_file_paths_from_messages
        session.agent.loop.rollback.commit(
            sess, f"before_edit_msg_{index}")
        # Only paths that are exclusively owned by the truncated
        # turns get restored. A file that an earlier surviving
        # turn also wrote keeps its current on-disk content.
        removed_files = _extract_file_paths_from_messages(msgs[i + 1:], sess)
        kept_files = _extract_file_paths_from_messages(msgs[:i + 1], sess)
        files_to_restore = removed_files - kept_files
        restored = sess.restore_file_snapshots_for_paths(files_to_restore)
        if restored:
            logger.info("[edit_msg] restored %d file(s) from snapshots", restored)
        target["content"] = new_content
        sess.messages = msgs[:i + 1]
        sess.turn_count = max(1, index + 1)
        # The redone turn's own summary still holds the pre-edit
        # prompt text; prune it together with everything after.
        sess.prune_derived_state(sess.turn_count, inclusive=True)
        # Manage-tool lifecycle ops performed after the edit point
        # (plugin installs/uninstalls) are undone as part of the
        # rollback so the conversation and the runtime agree.
        #
        # The boundary is the number of surviving assistant turns:
        # that is the numbering those records and the memory nodes
        # are tagged in (one increment per loop iteration, not per
        # user message). Using the user ordinal above instead left
        # a multi-iteration turn's installs and memories behind.
        kept_turns = sum(
            1 for kept in msgs[:i + 1] if kept.get("role") == "assistant"
        )
        sess.revert_manage_ops_after_turn(kept_turns)
        # Memory judged out of the edited turn (and later ones)
        # must stop being recalled; the turn is about to be redone.
        sess.purge_memory_after_turn(
            kept_turns,
            created_after=(target.get("created_at", 0) / 1000.0) or None,
        )
        # The compaction overlay was built from the pre-rewrite
        # history; a message rewrite invalidates it (same rule
        # as rollback_to / checkout).
        sess.compacted_view = None
        ws_loop = getattr(session, "agent", None)
        ws_loop = getattr(ws_loop, "loop", None) if ws_loop is not None else None
        if ws_loop is not None and hasattr(ws_loop, "clear_transient_state"):
            ws_loop.clear_transient_state()
        sess.updated_at = time.time()
        sess.rebuild_runtime_caches()
        session.agent.loop.rollback.commit(
            sess, f"edit_msg_{index}")
        if not sess.metadata.get("temp_chat"):
            await self._manager._save_session_async(session)

    async def _delete_message(self, session: Any, index: int, message_id: str = "") -> None:
        """Drop one user message together with every turn after it.

        Same structure as :meth:`_edit_message` except the target message is
        removed too.

        Target resolution: ``message_id`` (the server message id) takes
        priority -- ids are stable across compaction and synthetic-message
        injection. Without it, ``index`` is resolved as a *renderer-visible*
        user ordinal: compact markers count as user turns (the frontend
        renders them inside the user timeline) and synthetic loop plumbing
        (``is_synthetic`` user messages) does not. Counting raw user roles
        here instead desynchronises the two sides after compaction and
        deletes the wrong turn, leaving the rolled-back message alive
        server-side so the next send shows it twice.

        Steps:

        1. Commit a rollback checkpoint of the pre-delete state.
        2. Restore file snapshots for paths touched only by the deleted turns.
        3. Truncate ``messages`` before the target, fix up ``turn_count`` and
           ``updated_at``, and rebuild the runtime caches.
        4. Commit the post-delete state, then persist asynchronously unless
           this is a scratch ``temp_chat``.

        Args:
            session: The :class:`~encre.server.session_manager.SessionInfo` to
                mutate.
            index: The user-visible ordinal of the message to remove (fallback).
            message_id: The server message id of the message to remove (preferred).

        Returns:
            None.

        Raises:
            ValueError: When no user message at that index/id exists.

        Note:
            ``turn_count`` is clamped to at least 1. Deletion of the last user
            message is allowed here; the caller is responsible for removing the
            now-empty session.
        """
        sess = session.agent.session
        msgs = list(sess.messages)
        target_i = self._resolve_user_target(sess, index, message_id)
        if target_i < 0:
            raise ValueError(f"Message index {index} not found")
        target = msgs[target_i]
        from encre.session import _extract_file_paths_from_messages
        session.agent.loop.rollback.commit(
            sess, f"before_delete_msg_{index}")
        # Restore only file snapshots from the deleted turns; leave files
        # touched by kept messages untouched.
        removed_files = _extract_file_paths_from_messages(msgs[target_i:], sess)
        kept_files = _extract_file_paths_from_messages(msgs[:target_i], sess)
        files_to_restore = removed_files - kept_files
        restored = sess.restore_file_snapshots_for_paths(files_to_restore)
        if restored:
            logger.info("[delete_msg] restored %d file(s) from snapshots", restored)
        sess.messages = msgs[:target_i]
        sess.turn_count = max(1, index)
        # Summaries of the deleted turns would keep feeding the removed
        # prompts back to the model; drop them.
        sess.prune_derived_state(sess.turn_count, inclusive=False)
        # Manage-tool lifecycle ops performed after the delete point
        # (plugin installs/uninstalls) are undone as part of the rollback.
        # Boundary = surviving assistant turns, the numbering those records
        # and the memory nodes carry (see ``_edit_message``).
        kept_turns = sum(
            1 for kept in msgs[:target_i] if kept.get("role") == "assistant"
        )
        sess.revert_manage_ops_after_turn(kept_turns)
        # Memory judged out of the deleted turns must stop being recalled.
        sess.purge_memory_after_turn(
            kept_turns,
            created_after=(target.get("created_at", 0) / 1000.0) or None,
        )
        # The compaction overlay was built from the pre-rewrite history;
        # a message deletion invalidates it (same rule as rollback_to).
        sess.compacted_view = None
        ws_loop = getattr(session, "agent", None)
        ws_loop = getattr(ws_loop, "loop", None) if ws_loop is not None else None
        if ws_loop is not None and hasattr(ws_loop, "clear_transient_state"):
            ws_loop.clear_transient_state()
        sess.updated_at = time.time()
        sess.rebuild_runtime_caches()
        session.agent.loop.rollback.commit(
            sess, f"delete_msg_{index}")
        if not sess.metadata.get("temp_chat"):
            await self._manager._save_session_async(session)
