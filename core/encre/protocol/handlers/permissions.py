#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

"""Permission / plan / mode / spec approval handlers.

The interactive consent surface between agent and client.  Extracted
verbatim from ``encre.transport.ws`` (architecture refactor Stage 3 /
Task 5.2); behaviour is unchanged, only the layout moved.
"""

import logging
from typing import Any

from encre.server.protocol import (
    ClientEngineInstallResponse,
    ClientPlanApprove,
    ClientPlanReject,
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
from encre.tools.runtime import set_browser_session_id as set_session_id, set_cdp_url

logger = logging.getLogger("encre.transport.ws")


class PermissionHandlers:
    """Permission, plan, mode and spec approval handlers."""

    async def _h_respond_permission(self, ws: Any, msg: ClientRespondPermission) -> None:
        session = (
            self._manager.get_session(self._current_session_id)
            if self._current_session_id else None
        )
        if session is None:
            session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        session.agent.respond_permission(msg.decision, msg.remember)
        # The agent loop was blocked inside this consent gate; the user just
        # released it, so the session is active again.  Without this the state
        # would stay AWAITING_APPROVAL for the rest of the turn, so any
        # resume/listing refresh would keep reporting "waiting for consent"
        # while the agent is actually working.
        if session.state == SessionState.AWAITING_APPROVAL:
            self._manager.set_session_state(session.session_id, SessionState.RUNNING)

    async def _h_respond_plan(self, ws: Any, msg: ClientRespondPlan) -> None:
        session = (
            self._manager.get_session(self._current_session_id)
            if self._current_session_id else None
        )
        if session is None:
            session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        session.agent.loop.approve_plan(msg.proposal_id) if msg.approved else session.agent.loop.reject_plan(msg.proposal_id)

    async def _h_set_plan_mode(self, ws: Any, msg: ClientSetPlanMode) -> None:
        session = (
            self._manager.get_session(self._current_session_id)
            if self._current_session_id else None
        )
        if session is None:
            session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        # Legacy ``set_plan_mode``: map the boolean onto the
        # unified mode state via the single entry point and
        # broadcast ``mode_changed``/``plan_mode_changed`` so the
        # frontend toolbar chip and plan panel stay in sync (this
        # path previously mutated the bool without notifying).
        await self._apply_mode(ws, session, "plan" if msg.active else "")

    async def _h_set_mode(self, ws: Any, msg: ClientSetMode) -> None:
        sid = msg.session_id or self._current_session_id
        session = (
            self._manager.get_session(sid)
            if sid else None
        )
        if session is None:
            session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        logger.info("[set_mode] received mode='%s' session=%s",
                    msg.mode, session.session_id[:8])
        # Drive every persistent-mode change through the single
        # entry point.  ``_apply_mode`` normalises the value,
        # updates config + metadata mirror + derived flag, and
        # broadcasts ``mode_changed`` / ``plan_mode_changed`` so
        # the desktop toolbar chip and plan panel reflect it.
        _mode = await self._apply_mode(ws, session, msg.mode)
        if not _mode:
            logger.info("[set_mode] mode cleared for session=%s", session.session_id[:8])

    async def _h_set_cdp_url(self, ws: Any, msg: ClientSetCdpUrl) -> None:
        if msg.url:
            sid = msg.session_id or self._current_session_id or ""
            if sid:
                set_session_id(sid)
            set_cdp_url(msg.url)
            logger.info("[browser] CDP URL set to %s for session %s", msg.url, sid or "?")
        else:
            logger.warning("[browser] received empty CDP URL")

    async def _h_set_command(self, ws: Any, msg: ClientSetCommand) -> None:
        sid = msg.session_id or self._current_session_id
        session = (
            self._manager.get_session(sid)
            if sid else None
        )
        if session is None:
            session = self._get_or_create_session()
        self._manager.touch(session.session_id)
        # Activate / clear the sticky slash command through the
        # single entry point.  Stores the command in config +
        # session.metadata so it survives restart, re-injects its
        # ``command_instructions`` block every turn, and broadcasts
        # ``command_changed`` so the command chip stays in sync.
        await self._apply_command(
            ws, session, msg.name, msg.prompt, msg.icon, msg.title,
        )

    async def _h_engine_install_response(self, ws: Any, msg: ClientEngineInstallResponse) -> None:
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
        session = self._get_or_create_session()
        if session and msg.review_id:
            await self._send(ws, "plan_review",
                             review={"review_id": msg.review_id},
                             status="approved",
                             session_id=msg.session_id or self._current_session_id or "")
            logger.info("[plan] review approved by user: %s", msg.review_id)

    async def _h_plan_reject(self, ws: Any, msg: ClientPlanReject) -> None:
        await self._send(ws, "plan_review",
                         review={"review_id": msg.review_id},
                         status="rejected",
                         feedback=msg.feedback or "",
                         session_id=msg.session_id or self._current_session_id or "")
        logger.info("[plan] review rejected by user: %s feedback: %s",
                    msg.review_id, msg.feedback[:80] if msg.feedback else "(none)")
