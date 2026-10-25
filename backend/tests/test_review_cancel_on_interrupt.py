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

"""Regression tests for a review that dies with its turn.

``submit_review`` blocks the turn until the user decides. When the turn is
cancelled the blocked call is torn down and ``_finalize_cancelled_turn``
tombstones the orphan tool_use, but the review record itself stayed
``status="review"`` forever -- so the desktop card kept showing an
"waiting for answer" badge with live 取消/执行 buttons for a decision nobody
was waiting on any more.

These cover the three layers of the fix: the loop retires the record and
stashes the notice the run loop echoes, the wire projection carries the
record's real status instead of hardcoding "review", and the request handler
refuses to rewrite an already-decided record.
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from encre.config import EncreConfig
from encre.loop import EncreLoop
from encre.session import EncreSession


def _make_loop() -> EncreLoop:
    """Build an EncreLoop with a mocked backend (no real API calls)."""
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


def _pending_review(status: str = "review") -> dict:
    return {
        "review_id": "abc123",
        "content": "draft plan",
        "file_path": "/tmp/plan.md",
        "dir_path": "/tmp/reviews/abc123",
        "mode": "plan",
        "status": status,
        "files": [{"path": "/tmp/plan.md", "label": "plan.md"}],
        "feedback": "",
    }


def _orphan_tool_call(loop: EncreLoop, name: str, call_id: str = "call_1") -> None:
    """Declare one assistant tool_call that never received a result."""
    loop.session.add_message(
        "assistant",
        None,
        tool_calls=[{"id": call_id, "name": name, "arguments": {}}],
    )


# ── loop: the record retires with the turn ─────────────────────────────


def test_finalize_cancels_pending_review_of_tombstoned_submit():
    """A tombstoned ``submit_review`` must retire the review it was blocking on."""
    loop = _make_loop()
    loop.session.metadata["reviews"] = [_pending_review()]
    _orphan_tool_call(loop, "submit_review")

    assert loop._finalize_cancelled_turn() == 1
    assert loop.session.metadata["reviews"][0]["status"] == "cancelled"
    notice = loop._pending_review_notice
    assert notice and notice["review_id"] == "abc123"
    assert notice["status"] == "cancelled"


def test_finalize_leaves_decided_record_untouched():
    """An already-approved record is history -- cancelling must not rewrite it."""
    loop = _make_loop()
    loop.session.metadata["reviews"] = [_pending_review("approved")]
    _orphan_tool_call(loop, "submit_review")

    loop._finalize_cancelled_turn()

    assert loop.session.metadata["reviews"][0]["status"] == "approved"
    assert loop._pending_review_notice is None


def test_finalize_keeps_review_when_other_tool_died():
    """Only a dead ``submit_review`` retires the card; another orphan tool_call
    must leave a genuinely pending review alone."""
    loop = _make_loop()
    loop.session.metadata["reviews"] = [_pending_review()]
    _orphan_tool_call(loop, "bash")

    loop._finalize_cancelled_turn()

    assert loop.session.metadata["reviews"][0]["status"] == "review"
    assert loop._pending_review_notice is None


def test_cancel_pending_review_drops_the_wakeup_handle():
    """A late click must not be treated as a decision the loop is waiting for."""
    loop = _make_loop()
    loop.session.metadata["reviews"] = [_pending_review()]
    loop._review_decision = None

    entry = loop._cancel_pending_review()

    assert entry is not None and entry["status"] == "cancelled"
    assert loop._review_event is None
    assert loop.resolve_review("abc123", approved=True) is False
    # Nothing pending left: the second call is a no-op.
    assert loop._cancel_pending_review() is None


# ── artifact titles: the file's own name wins ──────────────────────────


def test_artifact_label_is_the_real_file_name(tmp_path):
    """A model-supplied label is decoration -- the card titles each artifact
    with its actual file name, so "（一页）"-style suffixes never reach the UI."""
    md = tmp_path / "plan_probe.md"
    md.write_text("# plan\n", encoding="utf-8")
    loop = _make_loop()

    review = loop._build_review_from_args(
        {"files": [{"path": str(md), "label": "Encre Agent 项目目标计划（一页）"}], "summary": "s"},
        "plan",
        "client-7",
    )

    assert review is not None
    assert review.files == [{"path": os.path.normpath(str(md)), "label": "plan_probe.md"}]
    # The card anchors on the renderer-side call id so it renders in the turn
    # that asked for the decision instead of at the transcript tail.
    assert review.call_id == "client-7"


# ── decision: the click wakes the blocked turn ─────────────────────────


def test_resolve_review_wakes_the_blocked_submit():
    """The card's 执行 must resume the SAME turn immediately, and stay in plan
    mode while the approved work is still being done.

    Approval releases the per-write gate; it does NOT drop the mode. The run
    handler clears the mode once the run that carried the work out finishes --
    leaving the indicator behind is what tells the user the task is in flight.
    """
    import asyncio

    loop = _make_loop()
    loop.session.metadata["reviews"] = [_pending_review("review")]
    loop.set_mode("plan")
    loop._review_event = asyncio.Event()
    loop._review_decision = None

    assert loop.resolve_review("abc123", approved=True) is True
    assert loop._review_event.is_set()
    assert loop._review_decision == (True, "")
    assert loop.session.metadata["reviews"][0]["status"] == "approved"
    assert loop.plan_gate_released is True
    assert loop.config.slash_command_mode == "plan"


def test_re_entering_plan_mode_rearms_the_write_gate():
    """The release authorises one implementation pass, not the mode forever."""
    loop = _make_loop()
    loop.session.metadata["reviews"] = [_pending_review("review")]
    loop.set_mode("plan")
    loop.resolve_review("abc123", approved=True)
    assert loop.plan_gate_released is True

    loop.set_mode("plan")
    assert loop.plan_gate_released is False
    loop.set_mode("")
    assert loop.plan_gate_released is False


def test_resolve_review_without_a_waiter_only_records():
    """A decision that arrives after the turn already ended is recorded but
    reported as not waking anyone -- the caller must not pretend the model
    was resumed."""
    loop = _make_loop()
    loop.session.metadata["reviews"] = [_pending_review("review")]

    assert loop.resolve_review("abc123", approved=False, feedback="nope") is False
    record = loop.session.metadata["reviews"][0]
    assert record["status"] == "rejected" and record["feedback"] == "nope"


def test_live_pending_review_requires_a_blocked_waiter():
    """The single source of truth for "may this card be shown/decided".

    A record left at ``status="review"`` on disk is NOT a pending decision --
    that is what made a card appear in a fresh turn whose model had submitted
    nothing, and made its buttons click against nobody.
    """
    import asyncio

    loop = _make_loop()
    loop.session.metadata["reviews"] = [_pending_review("review")]
    assert loop.live_pending_review() is None

    loop._review_event = asyncio.Event()
    assert loop.live_pending_review() is None  # blocked, but on nothing

    loop._current_review = SimpleNamespace(
        review_id="abc123", status="review",
        __dict__={"review_id": "abc123", "status": "review", "mode": "plan"},
    )
    live = loop.live_pending_review()
    assert live is not None and live["review_id"] == "abc123"

    # Decided while still in memory: no longer a card to show.
    loop._current_review.status = "approved"
    assert loop.live_pending_review() is None


# ── session resolution: the accessor the handlers live or die by ───────


class _StubManager:
    """SessionManager stand-in with the real accessor contract.

    ``list_sessions()`` returns renderer *dicts*; only ``get_session()``
    yields a SessionInfo.  Both review resolvers used to iterate
    ``list_sessions()`` and read ``.session_id`` off each row, which raised
    ``AttributeError: 'dict' object has no attribute 'session_id'`` and made
    every 执行/取消 click die inside the handler.
    """

    def __init__(self, info) -> None:
        self._info = info

    def get_session(self, session_id):
        return self._info if session_id == self._info.session_id else None

    def list_sessions(self):
        return [{"session_id": self._info.session_id, "title": "row"}]


def test_resolve_loop_uses_get_session_not_renderer_rows():
    from encre.protocol.handlers.base import HandlerBase

    loop = _make_loop()
    loop.spec_engine = object()
    info = SimpleNamespace(session_id="s1", agent=SimpleNamespace(loop=loop))
    handler = SimpleNamespace(
        _manager=_StubManager(info),
        _current_session_id="s1",
        _spec_engine_for=lambda sid: None,
    )

    assert HandlerBase._resolve_loop.__get__(handler)("s1") is loop
    assert HandlerBase._resolve_spec_engine.__get__(handler)("s1") is loop.spec_engine
    # An unknown id resolves to nothing instead of raising.
    assert HandlerBase._resolve_loop.__get__(handler)("nope") is None


# ── wire: the echo carries the real status ─────────────────────────────


def test_plan_review_frame_projects_the_record_status():
    """``__plan_review__`` must not hardcode "review" -- the client merges the
    frame status over the record, so a cancelled echo has to say "cancelled"."""
    from encre.events.wire import _project_system_message
    from encre.utils.types import create_system_message

    record = _pending_review("cancelled")
    frame = _project_system_message(create_system_message(f"__plan_review__:{json.dumps(record)}", kind="plan"))

    assert frame is not None
    assert frame.name == "plan_review"
    assert frame.payload["status"] == "cancelled"
    assert frame.payload["review"]["review_id"] == "abc123"


# ── handler: a dead card cannot be decided ─────────────────────────────


class _StubPermissionHandler:
    """Minimal stand-in for what ``_h_plan_approve`` touches on ``self``."""

    def __init__(self, loop) -> None:
        from encre.protocol.handlers.permissions import PermissionHandlers

        self._h_plan_approve = PermissionHandlers._h_plan_approve.__get__(self)
        self._review_record = staticmethod(PermissionHandlers._review_record)
        self._loop = loop
        self._current_session_id = "s1"
        self.frames: list[tuple[str, dict]] = []

    def _resolve_loop(self, _session_id):
        return self._loop

    async def _send(self, _ws, kind: str, **payload) -> None:
        self.frames.append((kind, payload))


def test_approving_a_stale_review_retires_the_card():
    """A decision on a review nobody is blocked on (record still "review"
    after its run was cancelled, or replayed from disk into a fresh turn)
    must retire the card instead of claiming the model was resumed."""
    loop = _make_loop()
    loop.session.metadata["reviews"] = [_pending_review("review")]
    handler = _StubPermissionHandler(loop)

    import asyncio

    asyncio.run(handler._h_plan_approve(None, SimpleNamespace(
        session_id="s1", review_id="abc123", type="plan_approve",
    )))

    assert loop.session.metadata["reviews"][0]["status"] == "review"
    echoes = [f for f in handler.frames if f[0] == "plan_review"]
    assert len(echoes) == 1
    assert echoes[0][1]["review"] is None
    assert not [f for f in handler.frames if f[0] == "mode_changed"]


def test_approving_a_live_review_executes():
    """The live path: while the loop is blocked on the review, the decision
    resolves it and wakes the turn. The mode is NOT announced as cleared --
    that happens in the run handler once the implementation run is done."""
    import asyncio

    loop = _make_loop()
    loop.session.metadata["reviews"] = [_pending_review("review")]
    loop.set_mode("plan")
    loop._review_event = asyncio.Event()
    loop._current_review = SimpleNamespace(
        review_id="abc123", status="review", mode="plan",
        __dict__={"review_id": "abc123", "status": "review", "mode": "plan"},
    )
    handler = _StubPermissionHandler(loop)

    asyncio.run(handler._h_plan_approve(None, SimpleNamespace(
        session_id="s1", review_id="abc123", type="plan_approve",
    )))

    assert loop.session.metadata["reviews"][0]["status"] == "approved"
    echoes = [f for f in handler.frames if f[0] == "plan_review"]
    assert echoes[0][1]["status"] == "approved"
    assert not [f for f in handler.frames if f[0] == "mode_changed"]
    assert loop.config.slash_command_mode == "plan"
    assert loop.plan_gate_released is True
