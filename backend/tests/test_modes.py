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

"""Tests for plan / spec mode switching -- the single-source-of-truth model.

``loop.plan_mode_active`` is derived from ``config.slash_command_mode``, so
the boolean flag and the mode string cannot disagree. :meth:`EncreLoop.set_mode`
is the only transition entry point and keeps config, the session.metadata
mirror, and the derived flag consistent.
These tests also guard two regressions: exiting a mode clears the persisted
metadata slot so a one-off ``/plan`` cannot replay on later normal messages,
and ``spec`` mode never activates write-tool interception (only ``plan`` does).
"""

from unittest.mock import MagicMock, patch

import pytest
from encre.config import EncreConfig
from encre.loop import EncreLoop
from encre.session import EncreSession


def _make_loop() -> EncreLoop:
    """Build an EncreLoop with a mocked backend (no real API calls).

    Patches encre.loop.create_backend so the loop constructor receives a
    MagicMock instead of opening a network connection to any model provider.
    """
    config = EncreConfig(
        model="gpt-5.6",
        backend_type="openai",
        permission_mode="default",
        max_turns=10,
        max_tokens=4096,
        log_level="ERROR",
        enable_prompt_caching=False,
    )
    session = EncreSession(config)
    with patch("encre.loop.create_backend") as mock_create_backend:
        mock_backend = MagicMock()
        mock_backend.supports_tool_calling.return_value = True
        mock_backend.supports_prompt_caching.return_value = False
        mock_backend.context_window_size.return_value = 128000
        mock_create_backend.return_value = mock_backend
        return EncreLoop(config=config, session=session)


def _invariant(loop: EncreLoop) -> None:
    """Assert the flag and the mode string never disagree.

    The derived property must be True exactly when the canonical mode
    string is "plan"; any drift indicates a broken transition.
    """
    assert loop.plan_mode_active == (loop.config.slash_command_mode == "plan")


class TestModeSwitchingInvariants:
    """Verify that the plan-mode boolean flag and the slash-command-mode
    string stay in sync across every single transition.

    This test class exercises 6 scenarios: default state, enter plan,
    enter spec, exit to normal, normalize an invalid value, and
    enter/exit plan via the dedicated wrappers. The design follows the
    single-source-of-truth model where ``config.slash_command_mode`` is
    the canonical value and ``plan_mode_active`` is a derived property,
    so any drift between them indicates a bug in the transition logic.
    """

    def test_verify_default_state_is_normal_with_no_mode(self):
        """Validate that a freshly constructed loop has an empty slash_command_mode
        and plan_mode_active is False, confirming the normal starting point.

        The test exercises a fresh loop and asserts the invariant holds because
        the default state must be well-defined so that no mode is active
        until the user explicitly requests one.
        """
        loop = _make_loop()
        assert loop.config.slash_command_mode == ""
        assert loop.plan_mode_active is False
        _invariant(loop)

    def test_verify_enter_plan_mode_activates_interception_and_syncs_metadata(self):
        """Validate that set_mode('plan') sets the mode string, activates
        the interception flag, and mirrors the state into session.metadata.

        The test exercises the transition and asserts all three state sources
        match because the per-message path reads session.metadata as a
        fallback, so it must stay in sync with config.
        """
        loop = _make_loop()
        loop.set_mode("plan")
        assert loop.config.slash_command_mode == "plan"
        assert loop.plan_mode_active is True
        assert loop.session.metadata.get("slash_command_mode") == "plan"
        assert loop.session.metadata.get("plan_mode_active") is True
        _invariant(loop)

    def test_verify_enter_spec_mode_does_not_activate_write_tool_interception(self):
        """Validate that set_mode('spec') sets the mode string but leaves
        plan_mode_active False, confirming spec does not trigger write-tool interception.

        The test exercises the transition and asserts interception stays off
        because spec mode is a strict-output mode, not a write-gating mode;
        confusing the two would silently allow unchecked edits.
        """
        loop = _make_loop()
        loop.set_mode("spec")
        assert loop.config.slash_command_mode == "spec"
        # spec is a strict-output mode but does NOT gate write tools --
        # only plan mode does.
        assert loop.plan_mode_active is False
        assert loop.session.metadata.get("slash_command_mode") == "spec"
        assert loop.session.metadata.get("plan_mode_active") is False
        _invariant(loop)

    def test_verify_exit_mode_clears_persistent_metadata_slot(self):
        """Validate that set_mode('') after entering plan clears the persisted
        metadata slot so nothing can sticky-restore the old mode on a later
        normal message.

        The test exercises enter-plan then exit and asserts the metadata slot
        is gone and plan_mode_active is False because the sticky-restore bug
        was caused by the per-message path reading a stale metadata value.
        """
        loop = _make_loop()
        loop.set_mode("plan")
        assert "slash_command_mode" in loop.session.metadata
        loop.set_mode("")
        # Clearing the slot is what prevents a stale "plan" value from
        # being read back on the next normal turn.
        assert loop.session.metadata.get("slash_command_mode", "") == ""
        assert loop.config.slash_command_mode == ""
        assert loop.plan_mode_active is False
        assert loop.session.metadata.get("plan_mode_active") is False
        _invariant(loop)

    def test_verify_set_mode_normalizes_invalid_values_to_empty(self):
        """Validate that set_mode with an unknown mode string falls back to
        empty (normal), confirming input sanitization is enforced.

        The test exercises enter-plan then set an invalid mode and asserts
        the mode resets to empty because accepting arbitrary strings would
        let the model receive unexpected mode instructions from corrupted
        session metadata.
        """
        loop = _make_loop()
        loop.set_mode("plan")
        loop.set_mode("not-a-real-mode")
        assert loop.config.slash_command_mode == ""
        assert loop.plan_mode_active is False
        _invariant(loop)

    def test_verify_enter_exit_plan_mode_wrappers_maintain_invariant(self):
        """Validate that the dedicated enter_plan_mode and exit_plan_mode
        wrapper methods maintain the invariant across the full cycle.

        The test exercises enter-then-exit and asserts plan_mode_active and
        the metadata slot are cleaned up because the wrappers are the
        user-facing API and must not leave residual state.
        """
        loop = _make_loop()
        loop.enter_plan_mode(reason="test")
        assert loop.plan_mode_active is True
        assert loop.config.slash_command_mode == "plan"
        _invariant(loop)
        loop.exit_plan_mode(reason="done")
        assert loop.plan_mode_active is False
        assert loop.config.slash_command_mode == ""
        assert loop.session.metadata.get("slash_command_mode", "") == ""
        _invariant(loop)


class TestModeSwitchScenarios:
    """Validate the user-reported flow: enter plan, exit, then enter another
    mode, ensuring the session never gets stuck.

    This test class exercises 5 scenarios covering the full exit-re-enter
    cycle, direct plan <-> spec switching, sticky-restore regression guard,
    idempotent re-entry, and safe exit-from-normal. The design follows the
    invariant that every mode transition must be stateless at the metadata
    level so that the session can recover to normal from any state.
    """

    def test_verify_plan_exit_spec_exit_normal_cycle_completes_without_stuck_state(self):
        """Validate that the sequence enter-plan -> exit -> enter-spec -> exit
        leaves the loop in a clean normal state with no sticky metadata.

        The test exercises the full four-step cycle and asserts the final
        state is empty mode and False flag because the reported bug was that
        exiting plan left the metadata slot in place, causing plan to
        resurrect on the next normal message.
        """
        loop = _make_loop()
        # enter plan
        loop.set_mode("plan")
        assert loop.plan_mode_active is True
        _invariant(loop)
        # exit to normal
        loop.set_mode("")
        assert loop.plan_mode_active is False
        assert loop.session.metadata.get("slash_command_mode", "") == ""
        _invariant(loop)
        # enter spec -- must still work after the plan cycle
        loop.set_mode("spec")
        assert loop.config.slash_command_mode == "spec"
        assert loop.plan_mode_active is False
        _invariant(loop)
        # exit to normal again
        loop.set_mode("")
        assert loop.config.slash_command_mode == ""
        assert loop.plan_mode_active is False
        assert loop.session.metadata.get("slash_command_mode", "") == ""
        _invariant(loop)

    def test_verify_plan_to_spec_direct_switch_flips_interception_correctly(self):
        """Validate that switching plan -> spec -> plan flips the interception
        flag off and on correctly without leaving residual state.

        The test exercises three transitions and asserts the flag and mode
        string are consistent after each because direct mode switches must
        not depend on an explicit exit step -- the target mode fully
        replaces the source mode.
        """
        loop = _make_loop()
        loop.set_mode("plan")
        assert loop.plan_mode_active is True
        _invariant(loop)
        # plan -> spec: interception turns off
        loop.set_mode("spec")
        assert loop.plan_mode_active is False
        assert loop.config.slash_command_mode == "spec"
        _invariant(loop)
        # spec -> plan: interception turns back on
        loop.set_mode("plan")
        assert loop.plan_mode_active is True
        assert loop.config.slash_command_mode == "plan"
        _invariant(loop)

    def test_verify_no_sticky_restore_after_exit_from_plan(self):
        """Validate that after exiting plan, the persisted metadata slot is
        empty so a later normal run message cannot resurrect the old mode.

        The test exercises enter-plan, exit, then reads the persisted mode
        as the per-message path would and asserts it is empty because the
        sticky-restore bug was caused by reading a stale 'plan' value from
        metadata on every subsequent normal turn.
        """
        loop = _make_loop()
        loop.set_mode("plan")
        assert loop.session.metadata.get("slash_command_mode") == "plan"
        loop.set_mode("")
        # Simulate a later normal run arriving with no explicit mode: the
        # persisted mode it would have restored from is now empty.
        persisted = loop.session.metadata.get("slash_command_mode", "") or ""
        assert persisted == ""
        assert loop.plan_mode_active is False

    def test_verify_repeated_enter_plan_is_idempotent(self):
        """Validate that calling set_mode('plan') multiple times in a row
        is a no-op after the first call, confirming idempotency.

        The test exercises triple-enter and asserts the state is unchanged
        because idempotency prevents accidental state corruption from
        duplicate slash-command broadcasts.
        """
        loop = _make_loop()
        loop.set_mode("plan")
        loop.set_mode("plan")
        loop.set_mode("plan")
        assert loop.config.slash_command_mode == "plan"
        assert loop.plan_mode_active is True
        _invariant(loop)

    def test_verify_exit_plan_from_normal_state_is_safe(self):
        """Validate that calling exit_plan_mode when not currently in plan
        does not raise and leaves the state clean.

        The test exercises exit from normal and asserts the mode is empty
        and the flag is False because the wrapper is part of the user-facing
        API and must be safe to call idempotently.
        """
        loop = _make_loop()
        # Exiting from normal must not raise and must leave state clean.
        loop.exit_plan_mode(reason="no-op")
        assert loop.config.slash_command_mode == ""
        assert loop.plan_mode_active is False
        _invariant(loop)


class TestPlanModeWaiterWake:
    """Validate that leaving plan mode wakes any coroutine parked on the
    pending PlanProposal event, and that entering plan does not spuriously
    wake unrelated waiters.

    This test class exercises 2 scenarios: wake on exit and no-wake on
    enter, to confirm the event signaling is correctly scoped. The design
    follows the invariant that the plan event is only set when transitioning
    out of plan so that a pending proposal resolver unblocks exactly when
    the mode that created it is terminated.
    """

    def test_verify_set_mode_clear_wakes_pending_plan_event(self):
        """Validate that set_mode('') after entering plan sets the internal
        _plan_event so any waiter unblocks.

        The test exercises planting an event, entering plan, then exiting,
        and asserts the event is set because the waiter (e.g. a PlanProposal
        resolver) must be woken to prevent it from hanging indefinitely.
        """
        import asyncio
        loop = _make_loop()
        loop.set_mode("plan")
        # Plant a waiter as if a PlanProposal is pending.
        loop._plan_event = asyncio.Event()
        loop.set_mode("")
        # Transitioning out of plan must set the event so the waiter unblocks.
        assert loop._plan_event.is_set() is True

    def test_verify_enter_plan_does_not_spuriously_wake_event(self):
        """Validate that set_mode('plan') does not set _plan_event when no
        proposal is pending, confirming there is no spurious wake.

        The test exercises planting an event and then entering plan and
        asserts the event remains unset because entering a mode should not
        unblock waiters that belong to a different mode transition.
        """
        import asyncio
        loop = _make_loop()
        loop._plan_event = asyncio.Event()
        loop.set_mode("plan")
        # Entering plan should not trip a waiter (there is none pending).
        assert loop._plan_event.is_set() is False


class TestStagePromptNotAMode:
    """Validate that the internal work-phase prompt uses wording that cannot
    be confused with a user-facing mode declaration.

    This test class exercises 1 scenario: building the stage prompt when
    task_stage is 'discover', to confirm the wording uses 'Work Phase' and
    explicitly disclaims being a mode. The design follows the regression
    guard that the model previously answered 'discover mode' instead of
    the real slash-command mode, so the prompt wording was changed to
    prevent that ambiguity.
    """

    def test_verify_stage_prompt_uses_work_phase_wording_and_disclaims_mode_status(self):
        """Validate that _build_stage_prompt contains 'Work Phase' and a
        disclaimer that it is not a mode, while excluding the misleading
        'Current stage:' phrase.

        The test exercises prompt construction with task_stage='discover'
        and asserts the presence and absence of specific phrases because
        the wording is the only defense against the model conflating the
        internal work phase with a user mode.
        """
        loop = _make_loop()
        loop.session.metadata["task_stage"] = "discover"
        prompt = loop._build_stage_prompt()
        # The old wording "Current stage: discover" was read as a mode
        # declaration.  The new wording uses "Work Phase" and "work phase"
        # and explicitly disclaims being a mode.
        assert "Work Phase" in prompt
        assert "work phase" in prompt
        assert "not a mode" in prompt.lower() or "not a user mode" in prompt.lower()
        # The misleading bare "Current stage:" phrase must be gone.
        assert "Current stage:" not in prompt


class TestSpecDataRouting:
    """Validate that ``__spec_data__:`` SystemMessages are intercepted and
    re-routed as spec_update events instead of leaking as visible bubble
    messages in the conversation.

    This test class exercises 2 scenarios: spec-data reroute and plain
    system message passthrough, to confirm the WebSocket handler's prefix
    detection works correctly. The design follows the invariant that spec
    JSON must never render as raw text in the chat because it is consumed
    by the frontend spec-card renderer, not the message stream.
    """

    def _make_handler(self):
        """Build a bare EncreWSHandler with a capturing _send.

        Skips __init__ and replaces _send with a recorder so the test can
        inspect exactly which WebSocket event types the dispatcher emitted.
        """
        from encre.transport.ws import EncreWSHandler
        handler = EncreWSHandler.__new__(EncreWSHandler)
        sent: list[tuple[str, dict]] = []

        async def _send(ws, msg_type, **kwargs):
            """Record every dispatched event instead of touching a real socket.

            Stores the event type and keyword payload in order so the test
            can assert which WebSocket messages the dispatcher produced.
            """
            sent.append((msg_type, kwargs))
        handler._send = _send  # type: ignore[assignment]
        return handler, sent

    def test_verify_spec_data_is_rerouted_as_spec_update_and_not_leaked(self):
        """Validate that a SystemMessage with '__spec_data__:' prefix is
        re-routed as a spec_update event and does not also emit a
        system_message bubble.

        The test exercises the handler with a spec payload and asserts the
        first sent event is spec_update with the parsed payload and that no
        system_message event was emitted because the prefix interception
        must be exclusive -- the same data must not appear in both channels.
        """
        import asyncio
        import json
        from encre.utils.types import SystemMessage

        handler, sent = self._make_handler()
        spec_payload = {"title": "Demo", "sections": [], "status": "review"}
        event = SystemMessage(
            content="__spec_data__:" + json.dumps(spec_payload),
            kind="spec",
        )
        info = MagicMock()
        info.session_id = "s1"

        asyncio.run(handler._dispatch_event(ws=object(), _info=info, event=event))

        # The spec data must be re-routed as spec_update with the parsed spec.
        assert sent and sent[0][0] == "spec_update"
        assert sent[0][1]["spec"] == spec_payload
        assert sent[0][1]["status"] == "review"
        assert sent[0][1]["session_id"] == "s1"
        # And it must NOT also leak as a system_message bubble.
        assert not any(t == "system_message" for t, _ in sent)

    def test_verify_plain_system_message_still_bubbles_as_system_message(self):
        """Validate that a SystemMessage without the '__spec_data__:' prefix
        is still emitted as a system_message bubble, confirming the
        interception only targets the prefix and not all system messages.

        The test exercises the handler with a friendly notice and asserts
        the sent event is system_message with the original content because
        non-spec system messages must continue to render in the chat stream.
        """
        import asyncio
        from encre.utils.types import create_system_message

        handler, sent = self._make_handler()
        event = create_system_message(
            "Specification generated. Review it.", kind="spec",
        )
        info = MagicMock()
        info.session_id = "s1"

        asyncio.run(handler._dispatch_event(ws=object(), _info=info, event=event))

        # A friendly (non __spec_data__) notice still renders as a bubble.
        assert sent and sent[0][0] == "system_message"
        assert sent[0][1]["content"] == "Specification generated. Review it."
        assert sent[0][1]["kind"] == "spec"
