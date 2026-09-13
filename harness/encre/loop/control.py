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

"""Control surface mixin: plan mode, permissions, commands, reviews.

Holds the external control API of :class:`~encre.loop.runner.EncreLoop` --
everything the desktop UI / protocol handlers drive: mode transitions,
plan proposals, permission & question resolution, active slash commands,
tool-set selection, and spec/plan review persistence.  Method bodies are
unchanged from the original monolith; they were moved verbatim.
"""

import asyncio
import os
from collections.abc import AsyncGenerator
from typing import Any

from encre.logging_config import get_logger
from encre.utils.types import (
    AgentEvent,
    PlanModeChanged,
    PlanProposal,
    create_plan_review_data,
)

logger = get_logger(__name__)


class LoopControlMixin:
    """Plan-mode, permission, command, and review control surface for the agent loop.

    This mixin exposes the external API that the desktop UI and protocol
    handlers drive.  It bridges to three internal managers:

    - :attr:`_plan_mode` -- owns plan-mode state machine (normal / plan /
      spec), intercepts write-class tools as :class:`~encre.utils.types.PlanProposal`
      events, and gates permission/question waits.
    - :attr:`_cmd_mgr` -- tracks the active slash command and its prompt.
    - :attr:`spec_engine` -- parses specification outputs and persists
      plan/spec review artifacts (``plan.md``, ``steps.md``, ``checklist.md``)
      to disk.

    Mode transitions are atomic through :meth:`set_mode`: changing mode
    recalculates the active tool set so the model always sees the right
    tools for the current workflow.  Plan proposals yield
    ``AgentEvent`` streams (``__plan_review__`` / ``__spec_data__``) that
    the renderer displays as inline diffs with accept/reject buttons.
    """

    # 鈹€鈹€ PlanModeManager bridge properties 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    # External code (executor.py, _run_impl inline sites) accesses
    # these plan-mode attributes directly on EncreLoop.  The properties
    # forward to PlanModeManager so the old access patterns still work.

    @property
    def _permission_event(self) -> asyncio.Event | None:
        """Expose the plan-mode permission event (read accessor)."""
        return self._plan_mode._permission_event

    @_permission_event.setter
    def _permission_event(self, value: asyncio.Event | None) -> None:
        """Expose the plan-mode permission event (write accessor)."""
        self._plan_mode._permission_event = value

    @property
    def _permission_decision(self) -> bool:
        """Expose the plan-mode permission decision (read accessor)."""
        return self._plan_mode._permission_decision

    @_permission_decision.setter
    def _permission_decision(self, value: bool) -> None:
        """Expose the plan-mode permission decision (write accessor)."""
        self._plan_mode._permission_decision = value

    @property
    def _pending_tool_name(self) -> str:
        """Expose the tool name awaiting plan-mode approval (read accessor)."""
        return self._plan_mode._pending_tool_name

    @_pending_tool_name.setter
    def _pending_tool_name(self, value: str) -> None:
        """Expose the tool name awaiting plan-mode approval (write accessor)."""
        self._plan_mode._pending_tool_name = value

    @property
    def _plan_decision(self) -> bool:
        """Expose the plan approval decision (read accessor)."""
        return self._plan_mode._plan_decision

    @_plan_decision.setter
    def _plan_decision(self, value: bool) -> None:
        """Expose the plan approval decision (write accessor)."""
        self._plan_mode._plan_decision = value

    @property
    def _plan_decision_timed_out(self) -> bool:
        """Expose whether the last plan decision wait timed out (read accessor)."""
        return self._plan_mode._plan_decision_timed_out

    @property
    def _plan_event(self) -> asyncio.Event | None:
        """Expose the plan-mode await event (read accessor)."""
        return self._plan_mode._plan_event

    @_plan_event.setter
    def _plan_event(self, value: asyncio.Event | None) -> None:
        """Expose the plan-mode await event (write accessor)."""
        self._plan_mode._plan_event = value

    @property
    def _plan_proposals(self) -> dict[str, dict[str, Any]]:
        """Expose the pending plan proposals map (read accessor)."""
        return self._plan_mode._plan_proposals

    @_plan_proposals.setter
    def _plan_proposals(self, value: dict[str, dict[str, Any]]) -> None:
        """Expose the pending plan proposals map (write accessor)."""
        self._plan_mode._plan_proposals = value

    @property
    def _question_event(self) -> asyncio.Event | None:
        """Expose the question/answer await event (read accessor)."""
        return self._plan_mode._question_event

    @_question_event.setter
    def _question_event(self, value: asyncio.Event | None) -> None:
        """Expose the question/answer await event (write accessor)."""
        self._plan_mode._question_event = value

    @property
    def _question_answers(self) -> str:
        """Expose the accumulated question answers text (read accessor)."""
        return self._plan_mode._question_answers

    @_question_answers.setter
    def _question_answers(self, value: str) -> None:
        """Expose the accumulated question answers text (write accessor)."""
        self._plan_mode._question_answers = value

    async def _wait_for_permission_decision(self, tool_name: str) -> bool:
        """Forward a permission wait to the plan-mode manager.

        Args:
            tool_name: The tool whose execution is awaiting approval.

        Returns:
            ``True`` if the user approved, ``False`` if denied.
        """
        return await self._plan_mode.wait_for_permission_decision(tool_name)

    def resolve_permission(self, decision: bool, remember: bool = False) -> None:
        """Resolve a pending permission request with the given decision.

        Args:
            decision: ``True`` to approve the pending tool, ``False`` to deny.
            remember: When True, persist an approval into the session policy
                table so the same tool is not asked again this session.

        Returns:
            None.
        """
        self._plan_mode.resolve_permission(decision, remember)

    def resolve_question(self, answers: str) -> None:
        """Submit answers to a pending question/answer request.

        Args:
            answers: The user's answer text.

        Returns:
            None.
        """
        self._plan_mode.resolve_question(answers)

    # 鈹€鈹€ Plan mode API 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    #
    # Plan mode intercepts write-class tools and turns them into
    # ``PlanProposal`` events that the desktop UI renders as inline
    # diffs / previews with explicit accept / reject buttons.  This
    # makes the agent a true "plan first" workflow: it proposes the
    # change, the user inspects it, and only then does the tool run.
    #
    # The three modes -- ``""`` (normal), ``"plan"``, ``"spec"`` -- are
    # all funnelled through :meth:`set_mode`, the single transition
    # entry point, so ``config.slash_command_mode`` (string),
    # ``session.metadata`` mirror, and the derived
    # ``plan_mode_active`` flag can never disagree.  ``plan_mode_active``
    # is ``True`` only for ``"plan"``; ``"spec"`` is a separate strict
    # mode that does NOT intercept write tools (it enforces its own
    # approval gate via ``self.spec_engine`` instead).

    @property
    def plan_mode_active(self) -> bool:
        """Report whether the loop is currently in plan mode (read accessor)."""
        return self._plan_mode.plan_mode_active

    # 鈹€鈹€ Tool set name 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    @property
    def tool_set_name(self) -> str:
        """Active tool set name (e.g. ``"default"``, ``"coding"``, ``"plan"``).

        Changing this value resets the tool payload cache so the next
        API call uses the new tool set.
        """
        return self._tool_set_name

    @tool_set_name.setter
    def tool_set_name(self, value: str) -> None:
        self._tool_set_name = value

    def _resolve_tool_set_for_mode(self, intents: list[str] | None = None) -> str:
        """Map the current mode + detected intents to a tool set name.

        Base tool set comes from ``config.slash_command_mode`` (restricted in
        plan/spec), then expands to the intent-matched sets so the model can
        reach advanced tools (lsp, git, test_run, notebook, chart, ...)
        without first calling ``find_tool``.  This fixes the structural gap
        where browser/computer/docker etc. were only reachable through the
        model self-selecting find_tool -- most models never did, so advanced
        tools were effectively dead.
        """
        mode = self.config.slash_command_mode or ""
        base = "default" if mode in ("", "plan", "spec") else "default"
        intent_sets = {"coding": "coding", "research": "research", "data": "data"}
        picks: list[str] = [base]
        # Per-profile base tool sets (WORKSPACE always has the coding
        # toolchain, AUTOMATION stays bare) merge in before intent sets.
        for ts in getattr(self._profile, "tool_sets", ()) or ():
            if ts and ts not in picks:
                picks.append(ts)
        for intent in (intents or []):
            ts = intent_sets.get(intent)
            if ts and ts not in picks:
                picks.append(ts)
        # "all" would blow up context; a curated union is safer.
        return "+".join(picks) if len(picks) > 1 else base

    def set_mode(self, mode: str) -> None:
        """Transition the loop to a new command mode atomically.

        Args:
            mode: One of ``""`` (normal), ``"plan"`` or ``"spec"``.

        Returns:
            None.
        """
        self._plan_mode.set_mode(mode)
        # Sync tool set when mode changes
        self._tool_set_name = self._resolve_tool_set_for_mode()

    def enter_plan_mode(self, reason: str = "") -> PlanModeChanged:
        """Enter plan mode, returning a ``PlanModeChanged`` event.

        Args:
            reason: Optional human-readable reason for entering plan mode.

        Returns:
            The emitted :class:`~encre.utils.types.PlanModeChanged` event.
        """
        return self._plan_mode.enter_plan_mode(reason)

    def exit_plan_mode(self, reason: str = "") -> PlanModeChanged:
        """Exit plan mode, returning a ``PlanModeChanged`` event.

        Args:
            reason: Optional human-readable reason for exiting plan mode.

        Returns:
            The emitted :class:`~encre.utils.types.PlanModeChanged` event.
        """
        return self._plan_mode.exit_plan_mode(reason)

    # 鈹€鈹€ Active command API 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    # Delegates to :class:`CommandManager`.

    def set_command(self, name: str, prompt: str, icon: str = "",
                    title: str = "") -> None:
        """Register or replace the active slash command for this loop.

        Args:
            name: Command name.
            prompt: The prompt body associated with the command.
            icon: Optional icon hint for the UI.
            title: Optional display title.

        Returns:
            None.
        """
        self._cmd_mgr.set_command(name, prompt, icon=icon, title=title)

    def clear_command(self) -> None:
        """Clear the active slash command.

        Returns:
            None.
        """
        self._cmd_mgr.clear_command()

    @property
    def active_command_name(self) -> str:
        """Expose the name of the currently active slash command."""
        return self._cmd_mgr.active_command_name

    def approve_plan(self, proposal_id: str = "") -> None:
        """Approve a pending plan proposal (or the only proposal if omitted).

        Args:
            proposal_id: The proposal to approve; defaults to the pending one.

        Returns:
            None.
        """
        self._plan_mode.approve_plan(proposal_id)

    def reject_plan(self, proposal_id: str = "") -> None:
        """Reject a pending plan proposal (or the only proposal if omitted).

        Args:
            proposal_id: The proposal to reject; defaults to the pending one.

        Returns:
            None.
        """
        self._plan_mode.reject_plan(proposal_id)

    def get_pending_proposals(self) -> list[dict[str, Any]]:
        """Return the list of plan proposals still awaiting a decision.

        Returns:
            A list of proposal dictionaries from the plan-mode manager.
        """
        return self._plan_mode.get_pending_proposals()

    def _build_plan_proposal(
        self,
        proposal_id: str,
        tool_call_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> PlanProposal | None:
        """Build a plan proposal for an intercepted write-class tool call.

        Args:
            proposal_id: Unique id for the proposal.
            tool_call_id: The originating tool-call id.
            tool_name: The intercepted tool name.
            tool_args: The tool arguments (the change to preview).

        Returns:
            A :class:`~encre.utils.types.PlanProposal`, or ``None`` when the
            plan-mode manager declines to build one.
        """
        return self._plan_mode.build_plan_proposal(
            proposal_id, tool_call_id, tool_name, tool_args,
        )

    async def _await_plan_decision(
        self,
        proposal: PlanProposal,
        timeout: float = 300.0,
    ) -> bool:
        """Wait for the user to approve or reject a plan proposal.

        Args:
            proposal: The proposal awaiting a decision.
            timeout: Maximum seconds to wait before giving up.

        Returns:
            ``True`` if approved, ``False`` on rejection or timeout.
        """
        return await self._plan_mode.await_plan_decision(proposal, timeout)

    def _get_review_dir(self, mode: str, review_id: str) -> str:
        """Resolve the directory for a plan/spec review's 3 output files.

        Workspace mode: ``{workspace}/.encre/{mode}/{review_id}/``
        Normal mode: ``~/.dunimd/encre/session/{session_id}/{mode}/{review_id}/``

        Inside the directory: plan.md, steps.md, checklist.md
        """
        _workspace = self.session.metadata.get("workspace", "")
        _channel = self.session.metadata.get("channel", "")
        if _workspace and _channel == "iwork":
            return os.path.join(_workspace, ".encre", mode, review_id)
        from encre.config import get_data_dir
        return os.path.join(
            str(get_data_dir()), "session",
            self.session.id or "default", mode, review_id,
        )

    @staticmethod
    def _parse_review_sections(text: str) -> dict[str, str]:
        """Split markdown text by ``## Plan``, ``## Steps``, ``## Checklist``
        headers and return each section's content (without the header line).
        Missing sections get an empty string.
        """
        import re
        sections: dict[str, str] = {"plan": "", "steps": "", "checklist": ""}
        pattern = re.compile(
            r'^##\s+(Plan|Steps|Checklist)\s*$',
            re.MULTILINE | re.IGNORECASE,
        )
        parts = pattern.split(text)
        # parts layout: [before_first_header, "Plan", plan_content, "Steps", steps_content, ...]
        if len(parts) < 3:
            sections["plan"] = text.strip()
            return sections
        header_order = ["Plan", "Steps", "Checklist"]
        for i in range(1, len(parts), 2):
            hdr = parts[i].strip().lower()
            if hdr in ("plan", "steps", "checklist"):
                sections[hdr] = parts[i + 1].strip() if i + 1 < len(parts) else ""
        return sections

    def _save_review_output(self, full_text: str, mode: str, prompt: str) -> list:
        """Save spec/plan output to 3 markdown files and return SystemMessages to emit.

        For ``mode="spec"``: parses via ``self.spec_engine`` and writes plan.md, steps.md,
        checklist.md.  For ``mode="plan"``: writes the same 3 files parsed by
        ``_parse_review_sections``.  Returns a list of ``SystemMessage`` events for the
        caller to ``yield``.
        """
        import json as _json
        import uuid as _uuid
        from encre.utils.types import SystemMessage as _SM

        events: list = []

        if mode == "spec" and full_text.strip() and self.spec_engine:
            _looks_like_spec = any(
                line.lstrip().startswith("## ") and not line.lstrip().startswith("### ")
                for line in full_text.splitlines()
            )
            if not _looks_like_spec:
                return events
            try:
                from encre.modes.spec.engine import SpecStatus as _SpecStatus
                spec_doc = self.spec_engine.parse_spec(
                    title=prompt[:80] if prompt else "Specification",
                    llm_output=full_text,
                )
                spec_doc.status = _SpecStatus.REVIEW
                spec_data = spec_doc.to_dict()

                _review_id = _uuid.uuid4().hex[:12]
                _review_dir = self._get_review_dir("spec", _review_id)
                try:
                    os.makedirs(_review_dir, exist_ok=True)
                    _sections = self._parse_review_sections(full_text)
                    plan_path = os.path.join(_review_dir, "plan.md")
                    steps_path = os.path.join(_review_dir, "steps.md")
                    checklist_path = os.path.join(_review_dir, "checklist.md")
                    with open(plan_path, "w", encoding="utf-8") as _f:
                        _f.write(_sections["plan"] or spec_doc.to_markdown())
                    with open(steps_path, "w", encoding="utf-8") as _f:
                        _f.write(_sections["steps"] or "# Steps\n\nImplementation steps derived from the specification.\n")
                    with open(checklist_path, "w", encoding="utf-8") as _f:
                        _f.write(_sections["checklist"] or "# Checklist\n\nVerification checklist for this specification.\n")
                    spec_data["file_path"] = plan_path
                    logger.info("[spec] saved 3 files to %s", _review_dir)
                except Exception as _e:
                    logger.warning("[spec] failed to save spec files: %s", _e)

                events.append(_SM(content=f"__spec_data__:{_json.dumps(spec_data)}", kind="spec"))
                logger.info("[spec] parsed spec with %d sections, status=review", len(spec_doc.sections))
            except Exception as e:
                logger.warning("[spec] failed to parse spec: %s", e)

        elif mode == "plan" and full_text.strip():
            try:
                _review_id = _uuid.uuid4().hex[:12]
                _review_dir = self._get_review_dir("plan", _review_id)
                os.makedirs(_review_dir, exist_ok=True)
                _sections = self._parse_review_sections(full_text)
                plan_path = os.path.join(_review_dir, "plan.md")
                steps_path = os.path.join(_review_dir, "steps.md")
                checklist_path = os.path.join(_review_dir, "checklist.md")
                with open(plan_path, "w", encoding="utf-8") as _f:
                    _f.write(_sections["plan"] or full_text)
                with open(steps_path, "w", encoding="utf-8") as _f:
                    _f.write(_sections["steps"] or "# Steps\n\nOrdered implementation steps.\n")
                with open(checklist_path, "w", encoding="utf-8") as _f:
                    _f.write(_sections["checklist"] or "# Checklist\n\nVerification checklist for this plan.\n")
                _review = create_plan_review_data(
                    review_id=_review_id,
                    content=full_text,
                    file_path=plan_path,
                    dir_path=_review_dir,
                    mode="plan",
                    status="review",
                )
                events.append(_SM(
                    content=f"__plan_review__:{_json.dumps(_review.__dict__)}",
                    kind="plan",
                ))
                logger.info("[plan] saved 3 files to %s for review", _review_dir)
            except Exception as e:
                logger.warning("[plan] failed to save plan files: %s", e)

        return events

    async def _intercept_plan_mode(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str,
        _client_id: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Delegate write-class tool interception to the plan-mode manager.

        Yields the same stream of plan-proposal / preview events that the
        underlying :class:`~encre.loop_plan_mode.PlanModeManager` produces, so
        callers can ``async for`` over this method transparently.

        Args:
            tool_name: The intercepted tool name.
            tool_args: The intercepted tool arguments.
            tool_call_id: The originating tool-call id.
            _client_id: Opaque client-side id forwarded to the manager.

        Yields:
            :class:`~encre.utils.types.AgentEvent` items from plan mode.
        """
        async for event in self._plan_mode.intercept_plan_mode(
            tool_name, tool_args, tool_call_id, _client_id,
        ):
            yield event
