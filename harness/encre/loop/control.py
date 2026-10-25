#!/usr/bin/env python3

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
import time
from collections.abc import AsyncGenerator
from typing import Any

from encre.context_compiler import compile_steering_messages
from encre.logging_config import get_logger
from encre.loop.state import LoopState as LoopRunState
from encre.loop.state import LoopTransitionError
from encre.utils.types import (
    AgentEvent,
    PlanModeChanged,
    PlanProposal,
    create_plan_review_data,
)

logger = get_logger(__name__)


def _plugin_hosts_for(reg: Any) -> list:
    """Plugin hosts to consult: the agent host first, then the shared one."""
    hosts: list = []
    agent_host = getattr(reg, "agent_host", None)
    if agent_host is not None:
        hosts.append(agent_host)
    shared_host = getattr(reg, "host", None)
    if shared_host is not None and shared_host not in hosts:
        hosts.append(shared_host)
    return hosts


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

    # ── PlanModeManager bridge properties ──────────────────────────
    # External code (executor.py, _run_impl inline sites) accesses
    # these plan-mode attributes directly on EncreLoop.  The properties
    # forward to PlanModeManager so the old access patterns still work.

    # --- Loop runtime state & steering inbox --------------------------------

    #: Upper bound on queued steering messages; beyond this, new enqueues
    #: are rejected (the UI surfaces the rejection) instead of silently
    #: growing an unbounded buffer.
    _STEERING_INBOX_MAX = 100

    def _transition_loop(self, new_state: LoopRunState, payload: Any = None) -> None:
        """Move the loop runtime state machine, logging illegal transitions.

        The state machine (:class:`~encre.loop.state.LoopStateMachine`) is
        created by the wiring mixin; bookkeeping must never crash the agent
        loop, so a rejected transition is logged and dropped here instead of
        being propagated to the caller.

        Args:
            new_state: Target runtime state (idle/running/waiting_*/...).
            payload: Optional JSON-serialisable context for the event.

        Returns:
            None.
        """
        sm = getattr(self, "_loop_sm", None)
        if sm is None:
            return
        try:
            sm.transition(new_state, payload)
        except LoopTransitionError as exc:
            logger.warning("[loop-state] %s", exc)

    def enqueue_user_message(self, text: str, source: str = "steering") -> bool:
        """Queue a mid-run user (steering) message -- thread-safe.

        Callable from the UI / protocol thread while the loop is busy.  The
        message is drained at the next turn boundary or tool-execution
        boundary (:meth:`drain_pending_messages`) and injected into the
        prompt context as a user message labelled with its source.  Every
        enqueue is recorded on the loop event stream (``inbox_enqueue``).

        Args:
            text: The user message text.
            source: Free-form origin tag (e.g. ``"steering"``, ``"queued"``).

        Returns:
            ``True`` when queued; ``False`` for empty text or when the
            bounded inbox is full.
        """
        if not text or not text.strip():
            return False
        tag = source or "steering"
        with self._steering_inbox_lock:
            if len(self._steering_inbox) >= self._STEERING_INBOX_MAX:
                logger.warning("[inbox] steering inbox full -- dropping message")
                return False
            self._steering_inbox.append({
                "text": text,
                "source": tag,
                "ts": time.time(),
            })
        sm = getattr(self, "_loop_sm", None)
        if sm is not None:
            sm.record("inbox_enqueue", {"source": tag, "preview": text[:200]})
        logger.info("[inbox] queued steering message from %s: %s", tag, text[:80])
        return True

    def drain_pending_messages(self) -> list[dict[str, Any]]:
        """Atomically drain all queued steering messages.

        Every drain is recorded on the loop event stream (``inbox_drain``).

        Returns:
            The queued messages (each a dict with ``text``/``source``/``ts``)
            in arrival order; empty when nothing is pending.
        """
        with self._steering_inbox_lock:
            if not self._steering_inbox:
                return []
            items: list[dict[str, Any]] = list(self._steering_inbox)
            self._steering_inbox.clear()
        sm = getattr(self, "_loop_sm", None)
        if sm is not None:
            sm.record("inbox_drain", {
                "count": len(items),
                "sources": sorted({str(m.get("source", "")) for m in items}),
            })
        return items

    def _inject_drained_messages(self, t: Any = None) -> int:
        """Drain steering messages and inject them as user messages.

        Format and token budget are owned by the context compiler
        (:func:`encre.context_compiler.compile_steering_messages`): it
        renders the canonical ``[steering|<source>] <text>`` content and
        truncates an overflowing batch to the steering budget.  This
        method only transports the compiled contents: a session-level
        synthetic user message (the same shape the end-of-turn grace
        message uses, so it flows into the next refreshed context) and,
        when a live turn context is supplied, an appended ``role: user``
        backend entry (the same shape the /steer injection in the model
        phase uses).

        Args:
            t: Optional live :class:`~encre.loop.turn_ctx.TurnContext`; when
                given, the messages are also appended to its backend list.

        Returns:
            The number of injected messages.
        """
        items = self.drain_pending_messages()
        contents = compile_steering_messages(items)
        injected = 0
        for _item, content in zip(items, contents, strict=False):
            if not content:
                continue
            self.session.add_message("user", content, is_synthetic=True)
            backend = getattr(t, "backend_messages", None)
            if isinstance(backend, list):
                backend.append({"role": "user", "content": content})
            injected += 1
        if injected:
            logger.info("[inbox] injected %d steering message(s)", injected)
        return injected

    # --- Tool stop ----------------------------------------------------------

    def request_tool_stop(self) -> bool:
        """Abort the currently running tool without killing the whole turn.

        Driven by the desktop UI's per-tool stop button (ws ``tool_stop``).
        Returns ``True`` when the stop signal has been raised; the running
        tool-execution site observes it and converts the abort into a tool
        error result.  Always ``True`` here: the event is cleared by the
        execution sites before each new tool starts.
        """
        self._tool_stop_event.set()
        return True

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
    def plan_gate_released(self) -> bool:
        """True while an approved plan authorises writes without a per-write
        proposal.  Lives on the shared state manager because both the loop and
        :class:`PlanModeManager` read it."""
        return bool(getattr(self._state_mgr, "plan_gate_released", False))

    @plan_gate_released.setter
    def plan_gate_released(self, value: bool) -> None:
        self._state_mgr.plan_gate_released = bool(value)

    @property
    def _question_event(self) -> asyncio.Event | None:
        """Expose the question/answer await event (read accessor)."""
        return self._plan_mode._question_event

    @_question_event.setter
    def _question_event(self, value: asyncio.Event | None) -> None:
        """Expose the question/answer await event (write accessor)."""
        self._plan_mode._question_event = value

    @property
    def _question_answers(self) -> str | list[str]:
        """Expose the accumulated question answers text (read accessor)."""
        return self._plan_mode._question_answers

    @_question_answers.setter
    def _question_answers(self, value: str | list[str]) -> None:
        """Expose the accumulated question answers text (write accessor)."""
        self._plan_mode._question_answers = value

    async def _wait_for_permission_decision(self, tool_name: str) -> bool:
        """Forward a permission wait to the plan-mode manager.

        The loop runtime state moves to ``waiting_permission`` for the
        duration of the wait and back to ``running`` once the decision (or a
        cancel) resolves it.

        Args:
            tool_name: The tool whose execution is awaiting approval.

        Returns:
            ``True`` if the user approved, ``False`` if denied.
        """
        self._transition_loop(LoopRunState.WAITING_PERMISSION, {"tool": tool_name})
        try:
            return await self._plan_mode.wait_for_permission_decision(tool_name)
        finally:
            self._transition_loop(LoopRunState.RUNNING, {"tool": tool_name})

    async def _request_permission(self, tool_name: str) -> bool:
        """Run the full permission request cycle with state tracking.

        This is the entry point the tool phases call (it wraps
        ``PlanModeManager.request_permission``: setup, wait, teardown).  The
        state machine records the ``waiting_permission`` window so a crash
        mid-wait is visible in the persisted event stream and can be
        recovered on restart.

        Args:
            tool_name: The tool requesting approval.

        Returns:
            ``True`` when the user allows the tool.
        """
        self._transition_loop(LoopRunState.WAITING_PERMISSION, {"tool": tool_name})
        try:
            # Plugin permission policies vote before the interactive flow:
            # any ``deny`` verdict blocks the tool outright (fail-closed);
            # an ``allow`` only short-circuits when no policy denied it.
            # Policies are declarative rules evaluated inside the Rust
            # permission engine (deny beats allow, wildcards included).
            verdict = await self._plugin_permission_verdict(tool_name)
            if verdict == "deny":
                return False
            if verdict == "allow":
                return True
            return await self._plan_mode.request_permission(tool_name)
        finally:
            self._transition_loop(LoopRunState.RUNNING, {"tool": tool_name})

    async def _plugin_permission_verdict(self, tool_name: str) -> str:
        """Ask plugin permission policies for a verdict on *tool_name*.

        Policies are declarative rules arbitrated inside the Rust permission
        engine.  Returns ``"deny"`` when any policy voted False, ``"allow"``
        when at least one voted True and none denied, ``"abstain"`` when
        there are no policies or all of them passed.  Never raises.
        """
        try:
            from encre.kernel.services import shared_plugin_registry_raw as _get_shared_plugin_registry_raw
            reg = _get_shared_plugin_registry_raw()
        except Exception:
            return "abstain"
        if reg is None:
            return "abstain"
        votes: list[bool] = []
        for host in _plugin_hosts_for(reg):
            try:
                votes.extend(await host.collect_permission_votes(tool_name))
            except Exception:
                logger.debug("[permission] plugin policy vote failed", exc_info=True)
        if any(v is False for v in votes):
            return "deny"
        if any(v is True for v in votes):
            return "allow"
        return "abstain"

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

    def resolve_question(self, answers: str | list[str]) -> None:
        """Submit answers to a pending question/answer request.

        Args:
            answers: The user's answer text -- a single string, or a list with
                one entry per question when the tool asked several at once.

        Returns:
            None.
        """
        self._plan_mode.resolve_question(answers)

    # --- Plan mode API ------------------------------------------------------
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

    @property
    def permission_pending(self) -> bool:
        """Report whether a tool-permission request is awaiting a decision."""
        return self._plan_mode.permission_pending

    # --- Tool set name -------------------------------------------------------

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
        without first calling ``manage(discover)``.  This fixes the structural
        gap where browser/computer/docker etc. were only reachable through the
        model self-selecting discovery -- most models never did, so advanced
        tools were effectively dead.
        """
        base = "default"
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
        # Lazy plugins may listen for mode switches (``mode_changed:plan``).
        self._emit_plugin_event(f"mode_changed:{mode or 'normal'}")

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

    # --- Active command API --------------------------------------------------
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

    # ── Review submission (submit_review tool) ──────────────────────────

    def _build_review_from_args(self, args: dict, mode: str, call_id: str = ""):
        """Validate ``submit_review`` args into a persisted PlanReviewData.

        Artifact paths are chosen by the model; only files that exist on
        disk make it onto the card.  The record is appended to
        ``session.metadata["reviews"]`` so resuming the session rebuilds
        the review card.  ``call_id`` is the renderer-side id of this
        ``submit_review`` call -- the card anchors on it so it renders in
        the turn that asked for the decision.  In spec mode the artifacts
        are also registered with the spec engine (status REVIEW) so the
        write gate opens on approval.  Returns ``None`` when no readable
        file was provided.
        """
        import os
        import uuid as _uuid

        from encre.utils.types import create_plan_review_data

        files_raw = args.get("files") or []
        if isinstance(files_raw, str):
            import json as _json
            try:
                files_raw = _json.loads(files_raw)
            except Exception:
                files_raw = []
        workspace = self.session.metadata.get("workspace", "") or ""
        resolved: list[dict[str, str]] = []
        for entry in files_raw if isinstance(files_raw, list) else []:
            path = str(entry.get("path", "")).strip() if isinstance(entry, dict) else str(entry).strip()
            if not path:
                continue
            if not os.path.isabs(path) and workspace:
                path = os.path.join(workspace, path)
            abspath = os.path.normpath(path)
            if not os.path.isfile(abspath):
                continue
            # The card titles each artifact with its real file name. A
            # model-supplied label is decoration ("(one page)" and friends), so
            # the schema no longer asks for one and the record ignores it.
            resolved.append({"path": abspath, "label": os.path.basename(abspath)})
        if not resolved:
            return None

        summary = str(args.get("summary", "")).strip()
        review_id = _uuid.uuid4().hex[:12]
        dir_path = self._get_review_dir(mode, review_id)
        review = create_plan_review_data(
            review_id=review_id,
            content=summary,
            file_path=resolved[0]["path"],
            dir_path=dir_path,
            mode=mode,
            status="review",
            files=resolved,
            call_id=call_id,
        )
        reviews = list(self.session.metadata.get("reviews") or [])
        reviews.append(review.__dict__)
        self.session.metadata["reviews"] = reviews[-20:]
        self._current_review = review

        if mode == "spec" and self.spec_engine is not None:
            try:
                texts = []
                for f in resolved:
                    try:
                        with open(f["path"], encoding="utf-8") as fh:
                            texts.append(fh.read())
                    except OSError:
                        continue
                if texts:
                    doc = self.spec_engine.parse_spec(
                        title=summary[:80] or "Specification",
                        llm_output="\n\n".join(texts),
                    )
                    from encre.modes.spec.engine import SpecStatus as _SpecStatus
                    doc.status = _SpecStatus.REVIEW
            except Exception as e:
                logger.warning("[review] spec engine registration failed: %s", e)
        return review

    def live_pending_review(self) -> dict | None:
        """The review this loop is ACTUALLY blocked on right now, else None.

        A ``reviews`` record left at ``status="review"`` by a killed or
        cancelled run is not a pending decision -- nobody is waiting on it.
        Every surface that decides whether to show (or accept) a review card
        has to ask this instead of reading the last record off disk, which is
        how a stale card ended up rendered in a fresh turn whose model had
        not written anything yet.
        """
        if getattr(self, "_review_event", None) is None:
            return None
        review = getattr(self, "_current_review", None)
        if review is None or getattr(review, "status", "") not in ("review", "draft"):
            return None
        return dict(review.__dict__)

    def _activate_goal_from_review(self, mode: str, record: dict[str, Any] | None) -> None:
        """Install an approved review as the session's standing goal.

        The checklist prefers the live todo list -- the model's own task list,
        session-persisted and already shown on the plan card -- and falls back
        to the reviewed artifacts when the model kept none.  A failure here
        must never cost the user the approval itself, so it is logged and
        swallowed.
        """
        try:
            record = record or {}
            files = [
                str(f.get("path", ""))
                for f in (record.get("files") or [])
                if isinstance(f, dict) and f.get("path")
            ]
            content = str(record.get("content") or "").strip()
            title = content.splitlines()[0] if content else ""
            sections = None
            if mode == "spec":
                current = getattr(self.spec_engine, "current_spec", None)
                sections = getattr(current, "sections", None)
            self._plan_mode.activate_goal(
                kind=mode,
                title=title or f"{mode} goal",
                files=files,
                plan_items=getattr(self.session, "plan_items", None),
                spec_sections=sections,
            )
        except Exception:
            logger.warning("[goal] activation from review failed", exc_info=True)

    def resolve_review(self, review_id: str, approved: bool, feedback: str = "") -> bool:
        """Apply a review decision made by the user.

        Updates the persisted review record, opens the spec gate / releases
        the plan write gate on approval, installs the reviewed artifacts as
        the session's standing goal (both modes), and wakes a blocked
        ``submit_review`` call.  Returns ``True`` when a waiting tool call was
        woken; otherwise the decision is only recorded (the turn had already
        ended on timeout).
        """
        mode = ""
        record: dict[str, Any] | None = None
        reviews = self.session.metadata.get("reviews") or []
        for entry in reversed(reviews):
            if isinstance(entry, dict) and entry.get("review_id") == review_id:
                entry["status"] = "approved" if approved else "rejected"
                entry["feedback"] = feedback
                mode = str(entry.get("mode", ""))
                record = entry
                break
        current = getattr(self, "_current_review", None)
        if current is not None and current.review_id == review_id:
            current.status = "approved" if approved else "rejected"
            current.feedback = feedback
            mode = mode or current.mode
            if record is None:
                record = dict(current.__dict__)

        if approved:
            if mode == "spec" and self.spec_engine is not None:
                try:
                    self.spec_engine.approve()
                except Exception:
                    logger.debug("[review] spec approve failed", exc_info=True)
            elif mode == "plan":
                # Approval releases the per-write gate but keeps the session
                # IN plan mode: the plan is a promise to do the work, and
                # dropping the mode before the work exists would leave the
                # user with no indicator of a half-finished task.  The mode
                # outlives the run; it is left when the standing goal closes
                # or the user exits the mode.
                self.plan_gate_released = True
            # An approval is a commitment, not a one-off decision.  The
            # reviewed artifacts become the session's standing goal, and the
            # stop gate keeps driving the run until every checklist item is
            # done -- dropping the mode only when the goal closes or the user
            # leaves the mode by hand.
            if mode in ("plan", "spec"):
                self._activate_goal_from_review(mode, record)
        else:
            if mode == "spec" and self.spec_engine is not None:
                try:
                    self.spec_engine.reject(feedback=feedback)
                except Exception:
                    logger.debug("[review] spec reject failed", exc_info=True)

        event = getattr(self, "_review_event", None)
        if event is not None:
            self._review_decision = (approved, feedback)
            event.set()
            return True
        return False

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
            if isinstance(event, PlanProposal):
                # The proposal is now shown to the user; the manager awaits
                # the review decision inside this generator.  Track that
                # window as the loop's ``waiting_plan_review`` runtime state.
                self._transition_loop(
                    LoopRunState.WAITING_PLAN_REVIEW,
                    {"proposal_id": event.proposal_id, "tool": tool_name},
                )
            yield event
        # The generator finished: the decision resolved (or nothing was
        # proposed), so the loop is driving the pipeline again.
        sm = getattr(self, "_loop_sm", None)
        if sm is not None and sm.state is LoopRunState.WAITING_PLAN_REVIEW:
            self._transition_loop(
                LoopRunState.RUNNING,
                {"tool": tool_name, "phase": "plan_review_done"},
            )
