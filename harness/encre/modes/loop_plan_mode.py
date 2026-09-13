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

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from encre.logging_config import get_logger
from encre.utils.loop_helpers import _WRITE_TOOL_NAMES
from encre.utils.types import (
    AgentEvent,
    PlanModeChanged,
    PlanProposal,
    create_plan_mode_changed,
    create_plan_proposal,
    create_plan_resolved,
)

logger = get_logger(__name__)


class PlanModeManager:
    """Plan mode, permission, and proposal management for EncreLoop.

    Encapsulates all state and logic for plan mode interception (write-class
    tools proposed as previews), permission request/response, and user
    question resolution.  Composed into :class:`EncreLoop` via delegation.
    """

    _VALID_MODES: tuple[str, ...] = ("", "plan", "spec")

    def __init__(
        self,
        config: Any,
        state_mgr: Any,
        safety: Any,
        cancel_event: asyncio.Event,
    ) -> None:
        self._config = config
        self._state_mgr = state_mgr
        self._safety = safety
        self._cancel_event = cancel_event

        # 鈹€鈹€ Permission state 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
        self._permission_event: asyncio.Event | None = None
        self._permission_decision: bool = False
        self._pending_tool_name: str = ""

        # 鈹€鈹€ Question state 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
        self._question_event: asyncio.Event | None = None
        self._question_answers: str = ""

        # 鈹€鈹€ Plan mode state 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
        self._plan_event: asyncio.Event | None = None
        self._plan_decision: bool = False
        self._plan_proposals: dict[str, dict[str, Any]] = {}
        self._plan_decision_timed_out: bool = False

    # 鈹€鈹€ Permission API 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    async def request_permission(self, tool_name: str) -> bool:
        """Encapsulated setup + wait + teardown for a permission request.

        Sets ``_pending_tool_name``, creates the event, waits for the
        user's decision, then cleans up the event.  Returns ``True``
        when the user allows the tool.
        """
        self._pending_tool_name = tool_name
        self._permission_event = asyncio.Event()
        self._permission_decision = False
        result = await self.wait_for_permission_decision(tool_name)
        self._permission_event = None
        return result

    async def wait_for_permission_decision(self, tool_name: str) -> bool:
        """Wait for a permission decision or cancel signal.

        Returns ``True`` only when the user explicitly allows the request.
        Cancellation is treated as denial.  Never times out.
        """
        if self._permission_event is None:
            return False

        permission_waiter = asyncio.create_task(self._permission_event.wait())
        cancel_waiter = asyncio.create_task(self._cancel_event.wait())

        async def _drain(tasks: list[asyncio.Task[Any]]) -> None:
            for task in tasks:
                if task.done():
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

        try:
            done, pending = await asyncio.wait(
                [permission_waiter, cancel_waiter],
                return_when=asyncio.FIRST_COMPLETED,
            )
            await _drain(pending)

            if cancel_waiter in done:
                logger.info(
                    "Permission request cancelled for tool '%s'", tool_name,
                )
                return False
            return self._permission_decision
        except asyncio.CancelledError:
            await _drain([permission_waiter, cancel_waiter])
            raise

    def resolve_permission(self, decision: bool, remember: bool = False) -> None:
        """Called by the agent owner to approve or deny a pending permission request.

        ``remember=True`` (always-allow this session) persists the verdict
        into the session policy table; a one-off allow is not recorded.
        """
        self._permission_decision = decision
        if remember and self._pending_tool_name and self._safety is not None:
            try:
                self._safety.record_permission_decision(
                    self._pending_tool_name, decision
                )
            except Exception as _e:
                logger.debug("record_permission_decision failed: {_e}")
        if self._permission_event is not None:
            self._permission_event.set()

    def resolve_question(self, answers: str) -> None:
        """Called when the user answers a pending question."""
        self._question_answers = answers
        if self._question_event is not None:
            self._question_event.set()

    # 鈹€鈹€ Plan mode API 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    @property
    def plan_mode_active(self) -> bool:
        return getattr(self._config, "slash_command_mode", "") == "plan"

    def set_mode(self, mode: str) -> None:
        mode = mode if mode in self._VALID_MODES else ""
        prev = getattr(self._config, "slash_command_mode", "")
        self._config.slash_command_mode = mode
        self._state_mgr.slash_command_mode = mode
        self._state_mgr.plan_mode_active = (mode == "plan")
        if prev == "plan" and mode != "plan" and self._plan_event is not None:
            self._plan_event.set()

    def enter_plan_mode(self, reason: str = "") -> PlanModeChanged:
        self.set_mode("plan")
        return create_plan_mode_changed(True, reason=reason)

    def exit_plan_mode(self, reason: str = "") -> PlanModeChanged:
        self.set_mode("")
        if self._plan_event is not None:
            self._plan_event.set()
        return create_plan_mode_changed(False, reason=reason)

    def approve_plan(self, proposal_id: str = "") -> None:
        self._plan_decision = True
        if proposal_id:
            entry = self._plan_proposals.get(proposal_id)
            if entry is not None:
                entry["approved"] = True
        if self._plan_event is not None:
            self._plan_event.set()

    def reject_plan(self, proposal_id: str = "") -> None:
        self._plan_decision = False
        if proposal_id:
            entry = self._plan_proposals.get(proposal_id)
            if entry is not None:
                entry["approved"] = False
        if self._plan_event is not None:
            self._plan_event.set()

    def get_pending_proposals(self) -> list[dict[str, Any]]:
        return [dict(v) for v in self._plan_proposals.values()]

    # 鈹€鈹€ Proposal building 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def build_plan_proposal(
        self,
        proposal_id: str,
        tool_call_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> PlanProposal | None:
        preview = ""
        diff_text = ""
        file_path = ""
        original = ""
        proposed = ""
        added = 0
        removed = 0
        risk = "low"

        if tool_name in ("file_write", "write_file", "writeFile"):
            file_path = str(tool_args.get("file_path", "") or "")
            proposed = str(tool_args.get("content", "") or "")
            try:
                from encre.native import compute_diff as _native_diff
                from encre.native import read_file as _native_read
                try:
                    original = _native_read(file_path, 0, 0) if file_path else ""
                except Exception:
                    original = ""
                if file_path or proposed:
                    diff_text = _native_diff(original or "", proposed or "")
                    added = sum(1 for ln in diff_text.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
                    removed = sum(1 for ln in diff_text.splitlines() if ln.startswith("-") and not ln.startswith("---"))
            except Exception:
                diff_text = ""
            preview = (
                f"Create/overwrite {file_path or '(new file)'} "
                f"(+{added} -{removed}, {len(proposed)} chars)"
            )
            risk = "medium" if file_path and original else "low"
        elif tool_name in ("file_edit",):
            file_path = str(tool_args.get("file_path", "") or "")
            try:
                from encre.native import compute_diff as _native_diff
                from encre.native import read_file as _native_read
                try:
                    original = _native_read(file_path, 0, 0) if file_path else ""
                except Exception:
                    original = ""
                edits = tool_args.get("edits")
                if isinstance(edits, list) and edits:
                    content = original
                    for e in edits:
                        if not isinstance(e, dict):
                            continue
                        old_s = str(e.get("old_str", "") or "")
                        new_s = str(e.get("new_str", "") or "")
                        if old_s and old_s in content:
                            content = content.replace(old_s, new_s, 1)
                    proposed = content
                else:
                    old_s = str(tool_args.get("old_str", "") or "")
                    new_s = str(tool_args.get("new_str", "") or "")
                    proposed = (
                        original.replace(old_s, new_s, 1) if old_s and old_s in original else original
                    )
                if file_path or original or proposed:
                    diff_text = _native_diff(original or "", proposed or "")
                    added = sum(1 for ln in diff_text.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
                    removed = sum(1 for ln in diff_text.splitlines() if ln.startswith("-") and not ln.startswith("---"))
            except Exception:
                diff_text = ""
            preview = (
                f"Edit {file_path or '(file)'} (+{added} -{removed})"
            )
            risk = "medium"
        elif tool_name == "apply_patch":
            patch = str(tool_args.get("patch", "") or "")
            file_hints: list[str] = []
            for ln in patch.splitlines():
                if ln.startswith("+++ "):
                    p = ln[4:].strip()
                    if p and p != "/dev/null":
                        file_hints.append(p.lstrip("b/"))
            file_path = ", ".join(file_hints[:3])
            diff_text = patch[:4000]
            added = sum(1 for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
            removed = sum(1 for ln in patch.splitlines() if ln.startswith("-") and not ln.startswith("---"))
            preview = (
                f"Apply patch to {file_path or '(multi-file)'} "
                f"(+{added} -{removed})"
            )
            risk = "medium" if len(file_hints) > 1 else "low"
        elif tool_name == "bash":
            command = str(tool_args.get("command", "") or "")
            preview = f"Run shell command: {command[:200]}"
            risk = "high"
        else:
            preview = f"Execute {tool_name}"

        return create_plan_proposal(
            proposal_id=proposal_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_args=dict(tool_args),
            preview=preview,
            diff_text=diff_text,
            file_path=file_path,
            original=original,
            proposed=proposed,
            added=added,
            removed=removed,
            risk=risk,
        )

    # 鈹€鈹€ Plan decision waiting 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    async def await_plan_decision(
        self,
        proposal: PlanProposal,
        timeout: float = 300.0,
    ) -> bool:
        self._plan_proposals[proposal.proposal_id] = {
            "proposal_id": proposal.proposal_id,
            "tool_call_id": proposal.tool_call_id,
            "tool_name": proposal.tool_name,
            "tool_args": proposal.tool_args,
            "preview": proposal.preview,
            "risk": proposal.risk,
            "approved": False,
        }
        self._plan_event = asyncio.Event()
        self._plan_decision = False
        self._plan_decision_timed_out = False
        try:
            await asyncio.wait_for(self._plan_event.wait(), timeout=timeout)
        except TimeoutError:
            logger.warning(
                f"Plan proposal '{proposal.proposal_id}' timed out after {timeout}s -- auto-rejecting",
            )
            self._plan_decision = False
            self._plan_decision_timed_out = True
        self._plan_event = None
        decision = self._plan_decision
        self._plan_proposals.pop(proposal.proposal_id, None)
        return decision

    # 鈹€鈹€ Plan mode interception (async generator) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    async def intercept_plan_mode(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str,
        _client_id: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        interceptable = tool_name in _WRITE_TOOL_NAMES or tool_name == "bash"
        if not self.plan_mode_active or not interceptable:
            return
        proposal_id = f"plan-{uuid.uuid4().hex[:12]}"
        proposal = self.build_plan_proposal(
            proposal_id, tool_call_id, tool_name, tool_args,
        )
        if proposal is None:
            return
        yield proposal
        approved = await self.await_plan_decision(proposal)
        yield create_plan_resolved(proposal.proposal_id, tool_call_id, approved)
