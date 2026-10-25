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

import asyncio
import re
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from encre.logging_config import get_logger
from encre.modes.goal import (
    ActiveGoal,
    GoalItem,
    build_complete_message,
    build_continue_message,
    build_incomplete_message,
    extract_items,
)
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

# ── Read-only shell exploration exemption (plan mode) ──────────────────
#
# Plan mode must not gate the tool calls that BUILD the plan: listing
# directories, searching files and inspecting state are how the agent
# gathers context before proposing anything.  Only commands whose every
# pipeline segment is a known read-only verb skip the proposal gate;
# anything with a redirect, substitution, or a mutating verb still gets
# proposed.  This is a deliberately narrow allowlist -- unknown commands
# stay intercepted (fail closed).

_RE_SEGMENT_SPLIT = re.compile(r"\s*(?:\|\||&&|[|\n;&])\s*")
# Stderr redirects are read-safe noise; strip before the redirect scan.
_RE_STDERR_REDIRECT = re.compile(r"(?:2>\s*(?:&1|/dev/null|>\s*&1|\$null|NUL))", re.IGNORECASE)
_RE_SUBSTITUTION = re.compile(r"\$\(|`|<\(")

_READ_ONLY_HEADS: frozenset[str] = frozenset({
    # POSIX / shells
    "ls", "ll", "cat", "head", "tail", "wc", "stat", "file", "find", "fd",
    "grep", "rg", "ag", "tree", "du", "df", "pwd", "which", "where",
    "whereis", "whoami", "id", "uname", "hostname", "date", "uptime",
    "env", "printenv", "echo", "printf", "sort", "uniq", "cut", "tr",
    "diff", "cmp", "less", "more", "type", "basename", "dirname", "realpath",
    "readlink", "strings", "md5sum", "sha1sum", "sha256sum", "cksum",
    "columns", "nl", "od", "hexdump", "xxd", "jq", "yq",
    # Windows cmd built-ins (note: bare ``set`` is PowerShell's Set-Variable
    # alias -- mutating -- so it is deliberately absent)
    "dir", "findstr", "path", "ver", "vol", "free",
    # Common CLIs, subcommand-gated below
    "git", "docker", "kubectl", "pip", "pip3", "npm", "pnpm", "yarn",
})

# Subcommand allowlists for multi-verb tools; a bare entry means every
# invocation of that head is read-only.
_READ_ONLY_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset({"status", "log", "diff", "show", "blame", "ls-files",
                      "ls-tree", "rev-parse", "describe", "shortlog", "cat-file"}),
    "docker": frozenset({"ps", "images", "inspect", "logs", "version", "top", "stats"}),
    "kubectl": frozenset({"get", "describe", "logs", "version", "top"}),
    "pip": frozenset({"list", "show", "freeze", "check"}),
    "pip3": frozenset({"list", "show", "freeze", "check"}),
    "npm": frozenset({"ls", "list", "view", "outdated", "whoami"}),
    "pnpm": frozenset({"ls", "list", "view", "outdated"}),
    "yarn": frozenset({"list", "info", "why"}),
}

# PowerShell approved-verb prefixes that only retrieve/format pipeline data.
_READ_ONLY_PS_VERBS: tuple[str, ...] = ("get-", "select-", "format-", "measure-",
                                        "group-", "sort-", "compare-", "test-", "resolve-")

# Tokens that disqualify an otherwise read-only head (in-place / exec flags).
_READ_ONLY_TOXIC_FLAGS: tuple[str, ...] = ("-exec", "-execdir", "-delete", "-ok",
                                           "-fprint", "-fprintf", "-fls", "--in-place")


def _is_readonly_command(command: str) -> bool:
    """True when every segment of *command* is a known read-only operation.

    Conservative by design: empty input, unknown executables, redirects to
    files, command substitution, and mutating flags all return ``False`` so
    the plan-mode proposal gate keeps them.
    """
    text = (command or "").strip()
    if not text:
        return False
    stripped = _RE_STDERR_REDIRECT.sub(" ", text)
    if ">" in stripped or "<" in stripped or _RE_SUBSTITUTION.search(stripped):
        return False
    segments = [s.strip() for s in _RE_SEGMENT_SPLIT.split(stripped) if s.strip()]
    if not segments:
        return False
    for seg in segments:
        lowered = seg.lower()
        if any(flag in lowered.split() for flag in _READ_ONLY_TOXIC_FLAGS):
            return False
        tokens = seg.split()
        idx = 0
        # Skip env assignments (FOO=bar cmd ...) and a leading timeout wrapper.
        while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
            idx += 1
        if idx < len(tokens) and tokens[idx].lower() == "timeout":
            idx += 1
            while idx < len(tokens) and tokens[idx][0].isdigit():
                idx += 1
        if idx >= len(tokens):
            return False
        head = tokens[idx].lower().rstrip(";")
        if head.startswith("./") or head.startswith("/") or head.endswith(".ps1") or head.endswith(".sh"):
            return False  # explicit script paths can do anything
        if head in _READ_ONLY_HEADS:
            subs = _READ_ONLY_SUBCOMMANDS.get(head)
            if subs is not None:
                rest = [t.lower() for t in tokens[idx + 1:] if not t.startswith("-")]
                if not rest or rest[0] not in subs:
                    return False
            continue
        if "-" in head and head.startswith(_READ_ONLY_PS_VERBS):
            continue
        return False
    return True


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

        # ── Permission state ───────────────────────────────────────
        self._permission_event: asyncio.Event | None = None
        self._permission_decision: bool = False
        self._pending_tool_name: str = ""

        # ── Question state ─────────────────────────────────────────
        self._question_event: asyncio.Event | None = None
        self._question_answers: str | list[str] = ""

        # ── Plan mode state ────────────────────────────────────────
        self._plan_event: asyncio.Event | None = None
        self._plan_decision: bool = False
        self._plan_proposals: dict[str, dict[str, Any]] = {}
        self._plan_decision_timed_out: bool = False

        # ── Standing goal state ────────────────────────────────────
        # Run-scoped flag: the goal was satisfied during this run, so the
        # protocol layer has to tell the UI that the mode went off.
        self._goal_completed: bool = False

    # ── Permission API ────────────────────────────────────────────────

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
                    logger.debug("exception suppressed", exc_info=True)
                except Exception:
                    logger.debug("exception suppressed", exc_info=True)
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

    def resolve_question(self, answers: str | list[str]) -> None:
        """Called when the user answers a pending question.

        ``answers`` is a single string for a one-question ask, or a list with
        one entry per question (in ask order) for a batched ask.
        """
        self._question_answers = answers
        if self._question_event is not None:
            self._question_event.set()

    # ── Plan mode API ────────────────────────────────────────────────

    @property
    def plan_mode_active(self) -> bool:
        return getattr(self._config, "slash_command_mode", "") == "plan"

    @property
    def permission_pending(self) -> bool:
        """True while a permission request blocks the turn awaiting a decision."""
        return self._permission_event is not None

    def set_mode(self, mode: str) -> None:
        mode = mode if mode in self._VALID_MODES else ""
        prev = getattr(self._config, "slash_command_mode", "")
        if mode != prev:
            goal = self.active_goal
            # Leaving a mode by hand ends its objective, and switching between
            # the two modes ends the previous one.  Restoring the same mode
            # (session reopen) never reaches this branch, so a persisted goal
            # survives a reload instead of being dropped by its own restore.
            if goal is not None and (mode == "" or goal.kind != mode):
                self.abandon_goal(reason=f"mode -> {mode or 'normal'}")
        self._config.slash_command_mode = mode
        self._state_mgr.slash_command_mode = mode
        self._state_mgr.plan_mode_active = (mode == "plan")
        # Every mode transition re-arms the per-write gate.  What opens it
        # again is the approval: the run start derives it from the standing
        # goal, so an approved plan keeps writing without per-write proposals
        # across runs until the goal closes.
        self._state_mgr.plan_gate_released = False
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

    # ── Standing goal API ───────────────────────────────────────────
    #
    # An approved plan/spec is the session's objective, not a one-shot
    # approval.  The goal below is what the stop path reads to keep the run
    # driving towards the items that are still outstanding; it is dropped
    # only when every item is done, or when the user leaves the mode.

    @property
    def active_goal(self) -> ActiveGoal | None:
        """The typed standing goal, or ``None`` when nothing is approved."""
        return ActiveGoal.from_dict(getattr(self._state_mgr, "active_goal", None))

    def _store_goal(self, goal: ActiveGoal | None) -> None:
        self._state_mgr.active_goal = goal.to_dict() if goal is not None else None

    @property
    def goal_implementing(self) -> bool:
        """True while an approved goal still has work outstanding."""
        goal = self.active_goal
        return bool(goal is not None and goal.drives)

    @property
    def goal_completed(self) -> bool:
        """Set for the run in which the goal was satisfied (UI notification)."""
        return bool(self._goal_completed)

    @goal_completed.setter
    def goal_completed(self, value: bool) -> None:
        self._goal_completed = bool(value)

    def activate_goal(
        self,
        kind: str,
        title: str = "",
        files: Any = None,
        plan_items: Any = None,
        spec_sections: Any = None,
    ) -> ActiveGoal:
        """Install the approved artifacts as the session's standing goal.

        The checklist comes from the live todo list when the model keeps one,
        otherwise from the artifacts themselves (checkbox list, spec sections,
        headings).  A goal with no items is stored but never drives -- there is
        nothing to gate on.
        """
        files = [str(f) for f in (files or [])]
        items = extract_items(files=files, plan_items=plan_items, spec_sections=spec_sections)
        goal = ActiveGoal(
            kind=kind,
            title=str(title or "")[:200],
            items=items,
            files=files,
        )
        self._store_goal(goal)
        logger.info(
            "[goal] activated kind=%s items=%d files=%d",
            kind, len(items), len(files),
        )
        return goal

    def abandon_goal(self, reason: str = "") -> None:
        """Drop the standing goal without completing it (user exit / switch)."""
        goal = self.active_goal
        if goal is None:
            return
        self._store_goal(None)
        logger.info(
            "[goal] abandoned (%s) kind=%s items=%d",
            reason or "-", goal.kind, goal.total,
        )

    def complete_goal(self) -> None:
        """Close the goal and mark this run as the one that satisfied it."""
        goal = self.active_goal
        if goal is None:
            return
        self._store_goal(None)
        self._goal_completed = True
        logger.info("[goal] completed kind=%s items=%d", goal.kind, goal.total)

    def sync_goal_plan_items(self, plan_items: Any) -> bool:
        """Merge the live todo list into the goal (statuses follow the tool)."""
        goal = self.active_goal
        if goal is None or not goal.drives:
            return False
        if goal.sync_items(plan_items):
            self._store_goal(goal)
            return True
        return False

    def pending_goal_items(self) -> list[GoalItem]:
        """Checklist entries that are not done yet (empty when no goal)."""
        goal = self.active_goal
        return goal.pending_items() if goal is not None else []

    def note_goal_push(self) -> bool:
        """Record one push; ``False`` once the drive has stalled (no progress)."""
        goal = self.active_goal
        if goal is None:
            return False
        within_budget = goal.note_push()
        self._store_goal(goal)
        return within_budget

    def goal_continue_message(self) -> str:
        goal = self.active_goal
        return build_continue_message(goal) if goal is not None else ""

    def goal_complete_message(self) -> str:
        goal = self.active_goal
        return build_complete_message(goal) if goal is not None else ""

    def goal_incomplete_message(self) -> str:
        goal = self.active_goal
        return build_incomplete_message(goal) if goal is not None else ""

    def verify_ladder_allowed(self, slash_mode: str) -> bool:
        """Whether the verify-on-stop ladder applies to this turn.

        Plan/spec mode suspends the ladder while the artifacts are still being
        drafted -- there is nothing to verify yet, and the deliverable is a
        document.  The implementation pass that follows an approval is exactly
        where the ladder matters most, so an approved goal re-enables it.
        """
        if slash_mode not in ("plan", "spec"):
            return True
        return self.goal_implementing

    def spec_write_allowed(self, spec_engine: Any) -> bool:
        """Whether write tools may run in spec mode.

        The spec engine's status is in-memory and is lost on reload, so an
        approved spec goal counts as approval too -- otherwise a resumed
        objective would be blocked by its own gate.
        """
        goal = self.active_goal
        if goal is not None and goal.kind == "spec" and goal.drives:
            return True
        current = getattr(spec_engine, "current_spec", None) if spec_engine is not None else None
        status = getattr(current, "status", None)
        return str(getattr(status, "value", "") or "") == "approved"

    # ── Proposal building ───────────────────────────────────────────

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

    # ── Plan decision waiting ───────────────────────────────────────

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

    # ── Plan mode interception (async generator) ───────────────────

    async def intercept_plan_mode(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str,
        _client_id: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        interceptable = tool_name in _WRITE_TOOL_NAMES or tool_name == "bash"
        # Markdown artifacts are the plan phase's deliverable: writing them
        # is not "execution" and bypasses the per-write proposal gate
        # (submit_review is what presents them for review).
        if interceptable and tool_name in ("file_write", "write_file", "writeFile", "file_edit"):
            if str(tool_args.get("file_path", "") or "").lower().endswith(".md"):
                interceptable = False
        # Read-only shell exploration is how the plan gathers its context --
        # listing, searching and inspecting never mutate state, so gating
        # them behind a proposal would stall the very turn that has to
        # produce the plan.  Anything that is not provably read-only stays
        # intercepted.
        if interceptable and tool_name == "bash":
            if _is_readonly_command(str(tool_args.get("command", "") or "")):
                interceptable = False
        if not self.plan_mode_active or not interceptable:
            return
        # The user approved a plan: implementation writes go through without a
        # per-write proposal for as long as the standing goal drives.  The mode
        # is left when the goal closes or the user exits it, not with the run.
        if getattr(self._state_mgr, "plan_gate_released", False):
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
