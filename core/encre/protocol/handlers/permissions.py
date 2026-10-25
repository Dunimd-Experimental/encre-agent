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

"""Permission, plan, mode and spec approval handlers.

Owned client-message domain for every interactive consent surface between the
agent and the client: tool permissions, plan proposals, spec documents, slash
command modes, and third-party plugin activation. :class:`PermissionHandlers`
is mixed into :class:`encre.transport.ws.EncreWSHandler` and backs the
``ClientRespondPermission`` / ``ClientRespondPlan`` / ``ClientPlanApprove`` /
``ClientSpec*`` / ``ClientSetMode`` / ``ClientSetPlanMode`` /
``ClientSetCommand`` / ``ClientSetCdpUrl`` / ``ClientPluginConfirm`` /
``ClientEngineInstallResponse`` entries of that handler's ``_DISPATCH`` table.

The unifying idea is that the agent loop parks itself on a pending decision and
these handlers are the wake-up call. The frame carries a decision; the handler
hands it to the object that is blocked on it -- the loop's permission future,
its plan queue, the spec engine, or the plugin registry -- and then reconciles
the server-side state that observed the decision:

- a released consent gate moves the session from
  ``AWAITING_APPROVAL`` back to ``RUNNING``, otherwise the session would
  report "waiting for consent" forever;
- a mode or command change is pushed through the single entry points
  (:meth:`~encre.protocol.handlers.base.HandlerBase._apply_mode` and
  ``_apply_command``) so config, session metadata and the UI broadcast can
  never disagree.

Only :class:`_h_plugin_confirm` and the spec handlers operate outside a single
session -- plugins are process-wide, so the approval result is broadcast to
every client rather than sent to the requesting socket.

Public API:

- :class:`PermissionHandlers`
"""

import logging
from typing import Any

from encre.server.protocol import (
    ClientEngineInstallResponse,
    ClientPlanApprove,
    ClientPlanReject,
    ClientPluginConfirm,
    ClientRespondPermission,
    ClientRespondPlan,
    ClientSetCdpUrl,
    ClientSetCommand,
    ClientSetMode,
    ClientSetPlanMode,
    ClientSpecApprove,
    ClientSpecReject,
)
from encre.server.session_manager import SessionState
from encre.tools.runtime import (
    clear_cdp_url,
    set_cdp_url,
)
from encre.tools.runtime import (
    set_browser_session_id as set_session_id,
)

logger = logging.getLogger("encre.transport.ws")


class PermissionHandlers:
    """Consent and mode approval handlers for the agent loop.

    Mixed into :class:`encre.transport.ws.EncreWSHandler`; holds no state of
    its own and uses ``_manager``, ``_default_config``, ``_current_session_id``,
    ``_spec_engines`` and ``_send`` from the composed handler.

    Lifecycle: stateless, constructed once per server.

    Invariant: a handler that releases a consent gate must also reconcile the
    session's :class:`SessionState`. The gate and the state machine are two
    representations of the same fact, and letting them disagree is what makes
    the UI report a session as waiting for consent while it is already running.

    Args:
        None. Inherits ``__init__`` from
        :class:`~encre.protocol.handlers.base.HandlerBase` through the composed
        handler.
    """

    async def _h_respond_permission(self, ws: Any, msg: ClientRespondPermission) -> None:
        """Deliver the user's permission decision and release the consent gate.

        Args:
            ws: The connection to answer; unused beyond the dispatch contract.
            msg: ``ClientRespondPermission`` with ``decision`` and ``remember``
                (whether to grant the same permission again silently).

        Returns:
            None.

        Note:
            Releasing the gate must flip the session out of
            ``AWAITING_APPROVAL`` on the same tick. The decision is a
            one-shot wakeup: the loop is blocked inside the consent check and
            resumes as soon as ``respond_permission`` runs, so any later
            session listing or resume would otherwise keep reporting "waiting
            for consent" while the agent is already working.
        """
        session = (
            self._manager.get_session(self._current_session_id)
            if self._current_session_id else None
        )
        if session is None:
            session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        session.agent.respond_permission(msg.decision, msg.remember)
        # Releasing the gate is not enough on its own: the session state
        # machine still says awaiting approval, so a session listing or resume
        # issued immediately afterwards would report a consent wait that no
        # longer exists. Bring the two in step.
        if session.state == SessionState.AWAITING_APPROVAL:
            self._manager.set_session_state(session.session_id, SessionState.RUNNING)

    async def _h_plugin_confirm(self, ws: Any, msg: ClientPluginConfirm) -> None:
        """Apply the user's decision for a pending third-party plugin.

        Plugin activation is the one approval that is process-wide rather than
        session-scoped: once approved, every session in this server can call
        the plugin, so the result is broadcast to every client instead of
        answered on the requesting socket.

        Approving imports the plugin's code for the first time and registers
        it into the server's plugin host. Denying drops the inert shell
        without ever importing the code, which is the whole point of the gate:
        a rejected plugin is never executed.

        Args:
            ws: The connection that decided.
            msg: ``ClientPluginConfirm`` with the plugin name and the
                ``approved`` flag.

        Returns:
            None. Broadcasts the refreshed plugin lifecycle snapshot and the
            plugin UI sync so every renderer converges on the same view.

        Note:
            The registry is fetched lazily from
            :func:`encre.agent._get_shared_plugin_registry_raw` so this mixin
            does not import the agent package at module load time.
        """
        from encre.agent import _get_shared_plugin_registry_raw

        registry = _get_shared_plugin_registry_raw()
        if msg.approved:
            ok = registry.confirm_activation(msg.name, host=registry.default_host_dict())
        else:
            ok = registry.deny_activation(msg.name)
        logger.info("[plugin_confirm] plugin=%s approved=%s applied=%s",
                    msg.name, msg.approved, ok)
        await self.broadcast_plugin_changed(registry.snapshot_states())
        await self.broadcast_plugin_ui_sync()

    async def _h_respond_plan(self, ws: Any, msg: ClientRespondPlan) -> None:
        """Approve or reject a plan proposal the loop is waiting on.

        Args:
            ws: The connection to answer; unused beyond the dispatch contract.
            msg: ``ClientRespondPlan`` with the ``proposal_id`` and the
                ``approved`` flag.

        Returns:
            None. The loop emits its own plan/finish events once it resumes.

        Note:
            Like :meth:`_h_respond_permission` this creates a session on demand
            so a late decision is never lost to session cleanup. Approving an
            unknown proposal id is harmless: the loop has no matching pending
            proposal and simply ignores it.
        """
        session = (
            self._manager.get_session(self._current_session_id)
            if self._current_session_id else None
        )
        if session is None:
            session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        session.agent.loop.approve_plan(msg.proposal_id) if msg.approved else session.agent.loop.reject_plan(msg.proposal_id)

    async def _h_set_plan_mode(self, ws: Any, msg: ClientSetPlanMode) -> None:
        """Toggle plan mode on or off for the current session.

        Args:
            ws: The connection to broadcast the mode change on.
            msg: ``ClientSetPlanMode`` whose boolean ``active`` selects plan
                mode or clears it.

        Returns:
            None.

        Note:
            The boolean is mapped onto the unified mode string and pushed
            through the single entry point, so config, the session metadata
            mirror and the ``mode_changed`` / ``plan_mode_changed`` broadcasts
            all agree. Doing this through the entry point is what keeps the
            legacy boolean message in sync with the newer string-based mode
            API.
        """
        session = (
            self._manager.get_session(self._current_session_id)
            if self._current_session_id else None
        )
        if session is None:
            session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        # Map the legacy boolean onto the unified mode string and let the
        # entry point do the persistence and broadcasting.
        await self._apply_mode(ws, session, "plan" if msg.active else "")

    async def _h_set_mode(self, ws: Any, msg: ClientSetMode) -> None:
        """Change the session's slash-command mode.

        Args:
            ws: The connection to broadcast the mode change on.
            msg: ``ClientSetMode`` with the target ``mode`` string and an
                optional ``session_id``.

        Returns:
            None.

        Note:
            Every persistent-mode change goes through
            :meth:`~encre.protocol.handlers.base.HandlerBase._apply_mode`, which
            normalises the value (anything outside ``""``/``plan``/``spec`` is
            treated as clearing), updates config plus the session metadata
            mirror and the derived plan-active flag, and broadcasts
            ``mode_changed`` and -- only when the plan-active state actually
            flipped -- ``plan_mode_changed``. Keeping the entry point single
            means the toolbar chip and the plan panel cannot drift apart.
        """
        sid = msg.session_id or self._current_session_id
        session = (
            self._manager.get_session(sid)
            if sid else None
        )
        if session is None and sid:
            # The target session may sit in a parked per-directory pool or
            # only on disk (workspace switch, idle eviction).  Resolve it
            # through the manager so the clear lands on the session the
            # client actually closed the mode on, instead of silently
            # creating a fresh one and leaving the stale mode behind.
            session = self._manager.load_or_create_session(sid)
        if session is None:
            session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        logger.info("[set_mode] received mode='%s' session=%s",
                    msg.mode, session.session_id[:8])
        # Normalise, persist and broadcast through the single entry point so
        # the desktop chip and plan panel always reflect the applied mode.
        _mode = await self._apply_mode(ws, session, msg.mode)
        if not _mode:
            logger.info("[set_mode] mode cleared for session=%s", session.session_id[:8])

    async def _h_set_cdp_url(self, ws: Any, msg: ClientSetCdpUrl) -> None:
        """Point the browser tool at a remote CDP endpoint.

        The URL is stored process-wide in the tools runtime, alongside the
        session id that will use it, so a later browser tool call connects to
        the same endpoint instead of launching its own browser.

        Args:
            ws: The connection; unused -- nothing is echoed back.
            msg: ``ClientSetCdpUrl`` with the endpoint URL and an optional
                ``session_id``.

        Returns:
            None.

        Note:
            An empty URL is an explicit clear: the frontend sends it when the
            browser webview is closed or destroyed, so the stale endpoint is
            dropped instead of being retried by the browser tool. The session
            id is registered first so the URL and its owner are never
            half-updated if the connection drops between the two calls.
        """
        sid = msg.session_id or self._current_session_id or ""
        if sid:
            set_session_id(sid)
        if msg.url:
            set_cdp_url(msg.url)
            logger.info("[browser] CDP URL set to %s for session %s", msg.url, sid or "?")
        else:
            clear_cdp_url()
            logger.info("[browser] CDP URL cleared for session %s", sid or "?")

    async def _h_set_command(self, ws: Any, msg: ClientSetCommand) -> None:
        """Set or clear the sticky slash command for a session.

        Args:
            ws: The connection to broadcast the change on.
            msg: ``ClientSetCommand`` with the command ``name`` plus the
                ``prompt``, ``icon`` and ``title`` that render its chip.

        Returns:
            None.

        Note:
            A sticky command survives across turns and restarts, so it is
            stored in both config and session metadata and re-injected as a
            ``command_instructions`` block on every model call.
            :meth:`~encre.protocol.handlers.base.HandlerBase._apply_command` is
            the single writer for all of that and broadcasts
            ``command_changed`` so the command chip in the toolbar stays in
            sync. An empty name clears the command through the same path.
        """
        sid = msg.session_id or self._current_session_id
        session = (
            self._manager.get_session(sid)
            if sid else None
        )
        if session is None:
            session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        # Empty name means clear; either way the entry point writes config,
        # session metadata and the broadcast in one place.
        await self._apply_command(
            ws, session, msg.name, msg.prompt, msg.icon, msg.title,
        )

    async def _h_engine_install_response(self, ws: Any, msg: ClientEngineInstallResponse) -> None:
        """Answer a pending native engine install prompt.

        Some tools need a runtime installed before they can run; the agent
        blocks on that prompt and resolves it here with the user's choice.

        Args:
            ws: The connection to ack on.
            msg: ``ClientEngineInstallResponse`` with the ``request_id`` that
                ties the answer to the outstanding prompt, and the ``choice``.

        Returns:
            None. Always sends ``engine_install_response_ack`` so the client
            can dismiss its own prompt, even if the session has already gone
            away.

        Note:
            The agent capability is probed with ``hasattr`` because engine
            install is optional plumbing; a build without it should still ack
            the frame rather than error.
        """
        session = (
            self._manager.get_session(self._current_session_id)
            if self._current_session_id else None
        )
        agent = getattr(session, "agent", None) if session is not None else None
        if agent is not None and hasattr(agent, "resolve_engine_install"):
            agent.resolve_engine_install(msg.request_id, msg.choice)
        await self._send(ws, "engine_install_response_ack",
            request_id=msg.request_id, choice=msg.choice)

    async def _h_spec_approve(self, ws: Any, msg: ClientSpecApprove) -> None:
        """Approve the current spec and tell the client it may proceed.

        Args:
            ws: The connection to answer.
            msg: ``ClientSpecApprove`` with an optional ``session_id``.

        Returns:
            None. Sends ``spec_update`` with status ``approved`` when a spec is
            present.

        Note:
            The engine is resolved per session so one conversation's approval
            state cannot leak into another's. Approval is sent to the client
            rather than broadcast because the spec gate is per-session and the
            other windows do not need to change their rendering.
        """
        _spec_eng = self._resolve_spec_engine(msg.session_id)
        _spec_eng.approve()
        spec = _spec_eng.current_spec
        if spec:
            logger.info("[spec] approved by user")
            await self._send(ws, "spec_update",
                             spec=spec.to_dict() if spec else None,
                             status="approved",
                             session_id=msg.session_id or self._current_session_id or "")

    async def _h_spec_reject(self, ws: Any, msg: ClientSpecReject) -> None:
        """Reject the current spec and hand the feedback back to the loop.

        Args:
            ws: The connection to answer.
            msg: ``ClientSpecReject`` with optional ``session_id`` and the
                ``feedback`` explaining why the spec was refused.

        Returns:
            None. Sends ``spec_update`` with status ``rejected`` and the
            feedback when a spec is present.

        Note:
            Rejection carries the feedback through to the engine so the agent
            can revise the spec instead of stalling. An empty feedback string
            is a legitimate rejection and is passed through unchanged.
        """
        _spec_eng = self._resolve_spec_engine(msg.session_id)
        _spec_eng.reject(feedback=msg.feedback or "")
        spec = _spec_eng.current_spec
        if spec:
            logger.info("[spec] rejected by user: %s", msg.feedback[:80] if msg.feedback else "(no feedback)")
            await self._send(ws, "spec_update",
                             spec=spec.to_dict() if spec else None,
                             status="rejected",
                             feedback=msg.feedback or "",
                             session_id=msg.session_id or self._current_session_id or "")

    async def _h_plan_approve(self, ws: Any, msg: ClientPlanApprove) -> None:
        """Approve a plan/spec review submitted by the model.

        Wakes a blocked ``submit_review`` tool call (``loop.resolve_review``)
        so the same run can proceed into implementation; the loop also opens
        the spec gate / releases the plan write gate and installs the reviewed
        artifacts as the session's standing goal, which is what keeps the run
        driving until every checklist item is done.
        The updated review record is echoed back in full so the desktop can
        re-render the card and persist the outcome.
        """
        loop = self._resolve_loop(msg.session_id)
        review_dict = self._review_record(loop, msg.review_id)
        sid = msg.session_id or self._current_session_id or ""
        live = loop.live_pending_review() if loop is not None else None
        if live is None or live.get("review_id") != msg.review_id:
            # Nobody is blocked on this review: the card is a stale record
            # replayed from disk, already decided, or left over from a run
            # that died. Retire it instead of recording a decision that can
            # never reach a model.
            await self._send(ws, "plan_review", review=None, status="",
                             session_id=sid)
            logger.info("[plan] ignored decision on non-live review: %s", msg.review_id)
            return
        if loop is not None:
            try:
                loop.resolve_review(msg.review_id, approved=True)
            except Exception:
                logger.warning("[plan] resolve_review failed", exc_info=True)
        await self._send(ws, "plan_review",
                         review=review_dict or {"review_id": msg.review_id},
                         status="approved",
                         session_id=sid)
        # No mode_changed here on purpose: approving releases the write gate
        # but keeps the mode. The mode is dropped by the run handler once the
        # standing goal closes, or by the user exiting it.
        logger.info("[plan] review approved by user: %s", msg.review_id)

    async def _h_plan_reject(self, ws: Any, msg: ClientPlanReject) -> None:
        """Reject a plan/spec review and hand the feedback back to the model.

        Feedback flows through ``loop.resolve_review`` into the blocked
        ``submit_review`` result so the agent can revise the artifacts and
        resubmit instead of stalling.
        """
        loop = self._resolve_loop(msg.session_id)
        review_dict = self._review_record(loop, msg.review_id)
        live = loop.live_pending_review() if loop is not None else None
        if live is None or live.get("review_id") != msg.review_id:
            await self._send(ws, "plan_review", review=None, status="",
                             session_id=msg.session_id or self._current_session_id or "")
            logger.info("[plan] ignored decision on non-live review: %s", msg.review_id)
            return
        if loop is not None:
            try:
                loop.resolve_review(msg.review_id, approved=False, feedback=msg.feedback or "")
            except Exception:
                logger.warning("[plan] resolve_review failed", exc_info=True)
        if review_dict:
            review_dict = dict(review_dict)
            review_dict["status"] = "rejected"
            review_dict["feedback"] = msg.feedback or ""
        await self._send(ws, "plan_review",
                         review=review_dict or {"review_id": msg.review_id},
                         status="rejected",
                         feedback=msg.feedback or "",
                         session_id=msg.session_id or self._current_session_id or "")
        logger.info("[plan] review rejected by user: %s feedback: %s",
                    msg.review_id, msg.feedback[:80] if msg.feedback else "(none)")

    @staticmethod
    def _review_record(loop: Any, review_id: str) -> dict[str, Any] | None:
        """Latest stored review record matching *review_id* (None if absent)."""
        if loop is None:
            return None
        session = getattr(loop, "session", None)
        metadata = getattr(session, "metadata", None) or {}
        for entry in reversed(metadata.get("reviews") or []):
            if isinstance(entry, dict) and entry.get("review_id") == review_id:
                return entry
        return None
