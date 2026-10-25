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

"""Tests for the standing goal that drives an approved plan/spec to the end.

An approval is the first step, not the last: the reviewed artifacts are
installed as the session's goal (``session.metadata['active_goal']``), the
stop path refuses to end the run while checklist items remain, and the mode
is left only once every item is done, the drive has genuinely stalled, or the
user exits the mode by hand.

Coverage: item extraction from every supported source, progress/push-budget
bookkeeping, activation on review approval for both modes, the three
stop-path outcomes (push / complete / stall), goal lifetime against user
exit and session reload, the re-enabled verification ladder, and the
working-set checklist the model is shown while the goal is live.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

from encre.config import EncreConfig
from encre.loop import EncreLoop
from encre.loop.turn_ctx import TurnContext, TurnExit
from encre.loop_error import PostStreamAction, PostStreamDecision
from encre.modes.goal import (
    ACTIVE,
    DONE,
    GOAL_META_KEY,
    MAX_ITEMS,
    PENDING,
    ActiveGoal,
    GoalItem,
    build_continue_message,
    build_incomplete_message,
    extract_items,
    normalize_status,
)
from encre.session import EncreSession
from encre.utils.types import Finish

# ── Harness ──────────────────────────────────────────────────────────


def _make_loop(session: EncreSession | None = None) -> EncreLoop:
    """Build an EncreLoop whose backend is a mock, so no provider is contacted."""
    config = EncreConfig(
        model="gpt-5.6",
        backend_type="openai",
        permission_mode="default",
        max_turns=10,
        max_tokens=4096,
        log_level="ERROR",
        enable_prompt_caching=False,
    )
    session = session or EncreSession(config)
    with patch("encre.loop.create_backend") as mock_create_backend:
        mock_backend = MagicMock()
        mock_backend.supports_tool_calling.return_value = True
        mock_backend.supports_prompt_caching.return_value = False
        mock_backend.context_window_size.return_value = 128000
        mock_create_backend.return_value = mock_backend
        return EncreLoop(config=config, session=session)


def _seed_plan_items(loop: EncreLoop, statuses: list[str]) -> None:
    loop.session.plan_items = [
        {"id": f"t{i}", "text": f"Item {i}", "status": status} for i, status in enumerate(statuses)
    ]


def _activate(loop: EncreLoop, statuses: list[str], kind: str = "plan") -> None:
    """Enter *kind* mode and approve a review, installing the standing goal."""
    loop.set_mode(kind)
    _seed_plan_items(loop, statuses)
    loop.session.metadata["reviews"] = [{
        "review_id": "rv1",
        "mode": kind,
        "status": "review",
        "content": f"My {kind}",
        "files": [],
    }]
    loop.resolve_review("rv1", True)


def _make_turn(loop: EncreLoop, text: str = "All set.") -> TurnContext:
    t = TurnContext("do the work", None, "", loop.config.slash_command_mode, None)
    t.text_parts = [text]
    return t


async def _drive_stop(loop: EncreLoop, t: TurnContext) -> list:
    """Run the real post-stream phase and collect what it emitted.

    Only the two inputs that gate the reach of the goal check are stubbed --
    the post-stream classifier (there is no model to classify) and the
    verification ledger (nothing was touched on disk in these tests).  The
    goal gate itself, the mode transitions and the session writes are real.
    """
    loop._error_orch.handle_post_stream = (
        lambda **_kwargs: PostStreamDecision(PostStreamAction.FALL_THROUGH)
    )
    events: list = []
    with patch.object(loop._verif_ledger, "unverified_files", return_value=[]):
        try:
            async for event in loop._phase_post_stream(t):
                events.append(event)
        except TurnExit:
            pass
    return events


def _finish_of(events: list) -> Finish | None:
    return next((e for e in events if isinstance(e, Finish)), None)


def _last_system_message(loop: EncreLoop) -> str:
    for message in reversed(loop.session.messages):
        if message.get("role") == "system":
            return str(message.get("content") or "")
    return ""


# ── Extraction ───────────────────────────────────────────────────────


class TestGoalItemExtraction:
    """Every supported source of checklist items, and their priority order."""

    def test_verify_plan_items_win_over_artifact_checkboxes(self):
        """The live todo list outranks the artifact when both exist.

        The todo list is session-persisted with current statuses, so it
        describes the work as it actually stands; the artifact is the
        original draft.  Reading the newer source keeps the gate honest.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("## Checklist\n- [ ] from the file\n")
            items = extract_items(
                files=[path],
                plan_items=[{"id": "t1", "text": "from the tool", "status": "active"}],
            )
        assert [i.text for i in items] == ["from the tool"]
        assert items[0].status == ACTIVE

    def test_verify_checkbox_states_map_to_item_status(self):
        """``[x]`` is done, ``[~]`` is active, and an empty box is pending."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("- [x] finished thing\n- [~] current thing\n- [ ] later thing\n")
            items = extract_items(files=[path])
        assert [(i.text, i.status) for i in items] == [
            ("finished thing", DONE),
            ("current thing", ACTIVE),
            ("later thing", PENDING),
        ]

    def test_verify_spec_sections_become_pending_items(self):
        """A spec with no checklist still yields its section titles as work."""
        sections = [{"title": "Auth"}, {"title": "Storage"}]
        assert [i.text for i in extract_items(spec_sections=sections)] == ["Auth", "Storage"]

    def test_verify_headings_skip_structural_sections(self):
        """Document furniture is not a deliverable.

        Overview/Checklist/Notes are scaffolding around the work; treating
        them as items would leave the gate permanently unfinished.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spec.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("## Overview\nprose\n## Checklist\n- [ ] real item\n## Implement parser\n")
            items = extract_items(files=[path])
        assert [i.text for i in items] == ["real item"]

    def test_verify_numbered_list_is_the_last_resort(self):
        """A prose plan with no checkboxes still becomes a checklist."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("1. Wire the router\n2. Add the handler\n")
            assert [i.text for i in extract_items(files=[path])] == [
                "Wire the router", "Add the handler",
            ]

    def test_verify_duplicate_items_collapse_and_size_is_capped(self):
        """Repeated spellings are one item, and the list is bounded.

        An unbounded checklist would make the stop gate meaningless and the
        prompt block enormous, so extraction caps at ``MAX_ITEMS``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("- [ ] Same item.\n- [ ] same item\n")
                for i in range(MAX_ITEMS + 20):
                    fh.write(f"- [ ] distinct item {i}\n")
            items = extract_items(files=[path])
        assert len(items) == MAX_ITEMS
        assert sum(1 for i in items if i.text.lower().startswith("same")) == 1

    def test_verify_no_source_declares_no_items(self):
        """Nothing on disk and nothing in the todo list means no invention.

        The caller falls back to the historical single-pass behaviour instead
        of being handed a fabricated objective.
        """
        assert extract_items() == []
        assert extract_items(files=[], plan_items=[], spec_sections=[])[:0] == []

    def test_verify_status_aliases_cover_both_vocabularies(self):
        """The todo tool's spellings and the goal's own map onto three states."""
        assert normalize_status("in_progress") == ACTIVE
        assert normalize_status("completed") == DONE
        assert normalize_status("todo") == PENDING
        assert normalize_status("nonsense") == PENDING


# ── Progress and push budget ─────────────────────────────────────────


class TestGoalProgress:
    """Completion arithmetic and the no-progress budget that stops a stall."""

    def test_verify_progress_tracks_done_items(self):
        """``drives`` is only true while the goal is approved and non-empty."""
        goal = ActiveGoal(kind="plan", items=[
            GoalItem("a", "one", DONE), GoalItem("b", "two", PENDING),
        ])
        assert goal.total == 2 and goal.done_count() == 1
        assert goal.next_pending().text == "two"
        assert goal.is_complete() is False
        assert goal.drives is True
        goal.items[1].status = DONE
        assert goal.is_complete() is True

    def test_verify_empty_goal_never_drives(self):
        """An approved-but-itemless goal is stored but gates nothing."""
        assert ActiveGoal(kind="spec").drives is False
        assert ActiveGoal(kind="spec", approved=False, items=[GoalItem("a", "x")]).drives is False

    def test_verify_sync_follows_statuses_without_dropping_items(self):
        """Checking an item off in the todo tool advances the goal, and a
        trimmed todo list cannot silently shorten the approved objective."""
        goal = ActiveGoal(kind="plan", items=[
            GoalItem("t1", "one", PENDING), GoalItem("t2", "two", PENDING),
        ])
        assert goal.sync_items([{"id": "t1", "text": "one", "status": "done"}]) is True
        assert goal.done_count() == 1
        assert goal.total == 2, "a dropped todo entry must not drop the goal item"

    def test_verify_sync_adopts_new_work_as_pending(self):
        """Work added to the todo list joins the objective and must be done."""
        goal = ActiveGoal(kind="plan", items=[GoalItem("t1", "one", DONE)])
        assert goal.sync_items([{"id": "t9", "text": "new work", "status": "pending"}]) is True
        assert goal.is_complete() is False

    def test_verify_push_budget_resets_on_progress(self):
        """Pushes are bounded only while nothing gets done.

        A long objective keeps being driven for as long as it advances; only
        a model that made no progress over a full budget is treated as stalled.
        """
        goal = ActiveGoal(kind="plan", items=[GoalItem("a", "one", PENDING)])
        assert goal.nudge_budget == 8
        for _ in range(goal.nudge_budget):
            assert goal.note_push() is True
        assert goal.note_push() is False, "budget spent with no progress"
        goal.items[0].status = DONE
        assert goal.note_push() is True, "progress must refill the budget"
        assert goal.nudges == 1

    def test_verify_messages_name_the_next_item_and_the_stall(self):
        """The continue nudge is actionable; the stall report is explicit."""
        goal = ActiveGoal(kind="plan", items=[
            GoalItem("a", "one", DONE), GoalItem("b", "two", PENDING),
        ])
        message = build_continue_message(goal)
        assert "1/2" in message and "two" in message
        assert "do not ask whether to continue" in message
        stalled = build_incomplete_message(goal)
        assert stalled.startswith("goal_incomplete:") and "1 of 2" in stalled


# ── Activation on approval ───────────────────────────────────────────


class TestApprovalInstallsGoal:
    """Approving a review turns the artifacts into the session's objective."""

    def test_verify_plan_approval_keeps_mode_and_releases_the_gate(self):
        """Approval releases writes for the implementation pass without
        dropping the mode -- a half-finished plan must stay visible."""
        loop = _make_loop()
        _activate(loop, [PENDING, PENDING], kind="plan")
        assert loop.config.slash_command_mode == "plan"
        assert loop.plan_mode_active is True
        assert loop.plan_gate_released is True
        assert loop._plan_mode.goal_implementing is True
        goal = loop._plan_mode.active_goal
        assert goal is not None and goal.kind == "plan" and goal.total == 2

    def test_verify_spec_approval_installs_goal_and_opens_the_write_gate(self):
        """The spec engine's approval is in-memory and dies with a reload, so
        an approved spec goal must count as approval on its own."""
        loop = _make_loop()
        _activate(loop, [PENDING], kind="spec")
        assert loop._plan_mode.spec_write_allowed(None) is True
        assert loop._plan_mode.active_goal.kind == "spec"

    def test_verify_goal_is_persisted_on_the_session(self):
        """The objective lives on ``session.metadata``, not in a run local."""
        loop = _make_loop()
        _activate(loop, [PENDING], kind="plan")
        raw = loop.session.metadata.get(GOAL_META_KEY)
        assert isinstance(raw, dict) and raw["kind"] == "plan"
        assert ActiveGoal.from_dict(raw).total == 1

    def test_verify_itemless_approval_keeps_the_legacy_single_pass(self):
        """An artifact declaring no checklist still approves for one pass,
        but there is nothing for the gate to drive."""
        loop = _make_loop()
        loop.set_mode("plan")
        loop.session.metadata["reviews"] = [{
            "review_id": "rv1", "mode": "plan", "status": "review",
            "content": "A plan with no checklist", "files": [],
        }]
        loop.resolve_review("rv1", True)
        assert loop.plan_gate_released is True
        assert loop._plan_mode.goal_implementing is False

    def test_verify_rejection_installs_no_goal(self):
        """A rejected review leaves the objective unset."""
        loop = _make_loop()
        loop.set_mode("plan")
        _seed_plan_items(loop, [PENDING])
        loop.session.metadata["reviews"] = [{
            "review_id": "rv1", "mode": "plan", "status": "review",
            "content": "My plan", "files": [],
        }]
        loop.resolve_review("rv1", False)
        assert loop._plan_mode.active_goal is None
        assert loop._plan_mode.goal_implementing is False


# ── The stop gate ────────────────────────────────────────────────────


class TestStopGateDrivesTheGoal:
    """A clean text-only finish is not an ending while items remain."""

    async def test_verify_stop_with_items_left_pushes_the_next_turn(self):
        """The turn is continued instead of ended, with the next item named."""
        loop = _make_loop()
        _activate(loop, [PENDING, PENDING], kind="plan")
        t = _make_turn(loop)
        await _drive_stop(loop, t)
        assert t.do_continue is True
        message = _last_system_message(loop)
        assert "GOAL PLAN" in message and "Item 0" in message
        assert loop.config.slash_command_mode == "plan"
        assert loop._plan_mode.goal_implementing is True

    async def test_verify_stop_with_every_item_done_closes_the_goal(self):
        """Completion ends the run, clears the goal and leaves the mode."""
        loop = _make_loop()
        _activate(loop, [DONE, DONE], kind="plan")
        t = _make_turn(loop)
        events = await _drive_stop(loop, t)
        assert t.do_continue is False
        finish = _finish_of(events)
        assert finish is not None and finish.reason == "stop" and not finish.error
        assert loop.config.slash_command_mode == ""
        assert loop._plan_mode.active_goal is None
        assert loop._plan_mode.goal_completed is True

    async def test_verify_completed_item_is_not_pushed_again(self):
        """One outstanding item keeps the drive alive after the first is done."""
        loop = _make_loop()
        _activate(loop, [DONE, PENDING, DONE], kind="plan")
        t = _make_turn(loop)
        await _drive_stop(loop, t)
        assert t.do_continue is True
        assert "2/3" in _last_system_message(loop)

    async def test_verify_stalled_drive_blocks_the_finish_and_keeps_the_mode(self):
        """No progress over a full budget is reported, not hidden.

        Ending cleanly here would hand back a partial result as if it were
        the delivered objective; instead the finish carries ``goal_incomplete``
        and the mode stays on so the user can still see what is in flight.
        """
        loop = _make_loop()
        _activate(loop, [PENDING, PENDING], kind="plan")
        budget = loop._plan_mode.active_goal.nudge_budget
        for _ in range(budget):
            assert _finish_of(await _drive_stop(loop, _make_turn(loop))) is None
        events = await _drive_stop(loop, _make_turn(loop))
        finish = _finish_of(events)
        assert finish is not None
        assert finish.error_code == "goal_incomplete"
        assert finish.error_category == "goal"
        assert finish.error.startswith("goal_incomplete:")
        assert loop.config.slash_command_mode == "plan"
        assert loop._plan_mode.goal_implementing is True

    async def test_verify_drafting_a_plan_is_not_driven(self):
        """With no approved goal, plan mode still ends on the document turn.

        Drafting the plan is the deliverable in that phase; treating its
        text-only finish as an unfinished objective would loop forever.
        """
        loop = _make_loop()
        loop.set_mode("plan")
        t = _make_turn(loop, "Here is the plan I propose.")
        events = await _drive_stop(loop, t)
        assert t.do_continue is False
        finish = _finish_of(events)
        assert finish is not None and finish.reason == "stop" and not finish.error

    async def test_verify_spec_mode_is_driven_too(self):
        """The drive is not plan-only: an approved spec is an objective."""
        loop = _make_loop()
        _activate(loop, [PENDING], kind="spec")
        t = _make_turn(loop)
        await _drive_stop(loop, t)
        assert t.do_continue is True
        assert "GOAL SPEC" in _last_system_message(loop)


# ── Lifetime: user exit, mode switch, reload ─────────────────────────


class TestGoalLifetime:
    """The goal ends when the work ends or the user leaves the mode."""

    def test_verify_user_exit_abandons_the_goal(self):
        """Leaving the mode by hand is the user's override of the drive."""
        loop = _make_loop()
        _activate(loop, [PENDING, PENDING], kind="plan")
        assert loop._plan_mode.goal_implementing is True
        loop.set_mode("")
        assert loop._plan_mode.active_goal is None
        assert loop._plan_mode.goal_implementing is False

    def test_verify_switching_between_modes_abandons_the_previous_goal(self):
        """Plan and spec are separate objectives; switching ends the old one."""
        loop = _make_loop()
        _activate(loop, [PENDING], kind="plan")
        loop.set_mode("spec")
        assert loop._plan_mode.active_goal is None

    def test_verify_goal_survives_a_session_reload(self):
        """The objective outlives the process that approved it.

        A reloaded session must still be driven: the mode is restored and the
        goal is read back from metadata, so an interrupted implementation
        continues instead of silently becoming a normal session.
        """
        loop = _make_loop()
        _activate(loop, [DONE, PENDING], kind="plan")
        data = loop.session.to_dict()

        config = EncreConfig()
        restored = EncreSession.from_dict(data, config)
        with patch("encre.loop.create_backend") as mock_create_backend:
            mock_create_backend.return_value = MagicMock()
            revived = EncreLoop(config=config, session=restored)
        revived.set_mode("plan")
        assert revived._plan_mode.goal_implementing is True
        assert revived._plan_mode.active_goal.done_count() == 1

    def test_verify_resuming_the_same_mode_does_not_drop_the_goal(self):
        """Restoring a persisted mode is not a mode change: the goal stays."""
        loop = _make_loop()
        _activate(loop, [PENDING], kind="plan")
        loop.set_mode("plan")
        assert loop._plan_mode.goal_implementing is True

    async def test_verify_reloaded_goal_still_drives_the_stop_path(self):
        """A resumed goal is enforced exactly like a freshly approved one."""
        loop = _make_loop()
        _activate(loop, [PENDING], kind="plan")
        data = loop.session.to_dict()

        config = EncreConfig()
        restored = EncreSession.from_dict(data, config)
        with patch("encre.loop.create_backend") as mock_create_backend:
            mock_create_backend.return_value = MagicMock()
            revived = EncreLoop(config=config, session=restored)
        revived.set_mode("plan")
        t = _make_turn(revived)
        await _drive_stop(revived, t)
        assert t.do_continue is True


# ── Verification ladder and context ──────────────────────────────────


class TestVerifyLadderAndContext:
    """Approval re-arms verification, and the model is shown the checklist."""

    def test_verify_ladder_reopens_for_the_implementation_pass(self):
        """Drafting suspends verification (there is no code yet); the
        implementation pass is exactly where the ladder matters."""
        loop = _make_loop()
        pm = loop._plan_mode
        assert pm.verify_ladder_allowed("plan") is False
        assert pm.verify_ladder_allowed("spec") is False
        _activate(loop, [PENDING], kind="plan")
        assert pm.verify_ladder_allowed("plan") is True
        assert pm.verify_ladder_allowed("") is True

    def test_verify_working_set_shows_the_approved_checklist(self):
        """The live goal wins in the working set: it is the contract the stop
        gate enforces, so the model sees it with current statuses."""
        loop = _make_loop()
        _activate(loop, [DONE, PENDING], kind="plan")
        ws = loop._working_set.build_working_set()
        assert [p["description"] for p in ws["plans"]] == ["[x] Item 0", "[ ] Item 1"]
        prompt = loop._build_working_set_prompt()
        assert "Checklist:" in prompt and "[x] Item 0" in prompt
        assert "1/2" in prompt

    def test_verify_working_set_falls_back_to_the_todo_list(self):
        """Without a goal the session's own task list is still shown."""
        loop = _make_loop()
        _seed_plan_items(loop, [PENDING, ACTIVE])
        ws = loop._working_set.build_working_set()
        assert [p["description"] for p in ws["plans"]] == ["[ ] Item 0", "[~] Item 1"]
