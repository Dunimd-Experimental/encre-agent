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

"""Regression tests for closing a plan/spec mode with a pending review.

``HandlerBase._apply_mode`` must dismiss a pending ``submit_review`` record
whenever the session LEAVES plan/spec.  The authoritative "what mode is the
session actually in" answer after a disk reload lives in the
``session.metadata["slash_command_mode"]`` mirror (the same slot
``session_ready`` and ``mode_changed`` replay from), while
``agent.config.slash_command_mode`` starts at the clone default and only
catches up after the next run echo.  A close that happens before that echo
previously read the stale empty config, skipped ``_dismiss_pending_review``,
and left the pending record on disk -- so switching sessions rebuilt the
review card that the user had just closed.
"""

import asyncio
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


class _StubHandler:
    """Minimal stand-in exposing what ``_apply_mode`` touches on ``self``."""

    def __init__(self) -> None:
        from encre.protocol.handlers.base import HandlerBase
        self._apply_mode = HandlerBase._apply_mode.__get__(self)
        self._dismiss_pending_review = HandlerBase._dismiss_pending_review.__get__(self)
        self.frames: list[tuple[str, dict]] = []
        self.saved: list[str] = []

    async def _send(self, _ws, kind: str, **payload) -> None:
        self.frames.append((kind, payload))

    class _Manager:
        def __init__(self, owner: "_StubHandler") -> None:
            self._owner = owner

        async def _save_session_async(self, info) -> None:
            self._owner.saved.append(info.session_id)

    def __getattr__(self, name: str):
        if name == "_manager":
            self._manager = _StubHandler._Manager(self)
            return self._manager
        raise AttributeError(name)


def _make_info(loop: EncreLoop) -> SimpleNamespace:
    """Wrap the live loop in the SessionInfo shape ``_apply_mode`` reads."""
    agent = SimpleNamespace(loop=loop, config=loop.config, session=loop.session)
    return SimpleNamespace(session_id="regression-close-session", agent=agent)


def _pending_review(mode: str = "plan") -> dict:
    return {
        "review_id": "abc123",
        "content": "draft plan",
        "file_path": "/tmp/plan.md",
        "dir_path": "/tmp/reviews/abc123",
        "mode": mode,
        "status": "review",
        "files": [{"path": "/tmp/plan.md", "label": "plan.md"}],
        "feedback": "",
    }


def test_verify_close_after_reload_dismisses_pending_review():
    """A close on a freshly reloaded session (config drifted to "" while the
    metadata mirror still says "plan") must still reject the pending review.

    This is the exact user flow: reopen a session that had a pending
    submit_review, click the mode close button without sending any message,
    then switch sessions.  If the record is not resolved in metadata,
    ``session_ready`` rebuilds the card and it "comes back".
    """
    loop = _make_loop()
    info = _make_info(loop)
    handler = _StubHandler()

    loop.set_mode("plan")
    loop.session.metadata["reviews"] = [_pending_review("plan")]
    # Simulate the disk reload: session_ready resumed the session with a
    # fresh clone of the default config, so the in-memory config string is
    # empty again -- only metadata carries the truth.
    loop.config.slash_command_mode = ""

    asyncio.run(handler._apply_mode(None, info, ""))

    record = loop.session.metadata["reviews"][0]
    assert record["status"] == "rejected", (
        "closing the mode left the review pending in metadata; the card "
        "will be rebuilt on the next session_ready"
    )
    dismissals = [f for f in handler.frames if f[0] == "plan_review" and f[1].get("review") is None]
    assert dismissals, "client was never told to drop the review card"


def test_verify_close_dismisses_pending_review_with_synced_config():
    """The already-working case: config and metadata agree on "plan", so the
    close must dismiss the pending review through the same code path.
    """
    loop = _make_loop()
    info = _make_info(loop)
    handler = _StubHandler()

    loop.set_mode("plan")
    loop.session.metadata["reviews"] = [_pending_review("plan")]

    asyncio.run(handler._apply_mode(None, info, ""))

    assert loop.session.metadata["reviews"][0]["status"] == "rejected"


def test_verify_close_without_pending_review_leaves_history_intact():
    """Closing a mode whose review is already resolved must not rewrite the
    record: approved/rejected history is immutable, only the pending
    (review/draft) status is transitional.
    """
    loop = _make_loop()
    info = _make_info(loop)
    handler = _StubHandler()

    loop.set_mode("plan")
    loop.config.slash_command_mode = ""  # reload drift, like the first test
    loop.session.metadata["reviews"] = [
        {**_pending_review("plan"), "status": "approved"},
    ]

    asyncio.run(handler._apply_mode(None, info, ""))

    assert loop.session.metadata["reviews"][0]["status"] == "approved"
