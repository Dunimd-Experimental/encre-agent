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

"""``config_patch`` must never split ``models`` from ``active_model_index``.

The patch is a per-key diff of the config snapshot, so a field that did not
change simply is not sent.  ``models`` and ``active_model_index`` travel as
one logical snapshot, though: when a receiver got a new model list without an
index it had to guess, and the renderer's guess was ``0`` -- so picking a
reasoning level (a change to one model entry, index untouched) silently
activated the first model in the list.

Covered here:

1. a patch whose diff touches ``models`` only still carries the active index;
2. an unchanged snapshot sends nothing;
3. a patch that changes the index sends the new value, not a stale one.
"""

import asyncio
from types import SimpleNamespace

import pytest
from encre.protocol.handlers.base import HandlerBase


class _PatchProbe:
    """Minimal stand-in exposing only what ``_send_config_patch`` touches."""

    def __init__(self, before: dict, after: dict) -> None:
        self._before = before
        self._after = after
        self.sent: list[tuple[str, dict]] = []

    def _build_config_data(self, _info) -> dict:
        return self._after

    async def _send(self, _ws, kind: str, **payload) -> None:
        self.sent.append((kind, payload))

    async def _push_all_data(self, _ws) -> None:  # pragma: no cover - guard
        raise AssertionError("full push must not be used when the diff succeeds")


def _drive(before: dict, after: dict) -> list[tuple[str, dict]]:
    """Run the patch builder over a *before*/*after* snapshot pair."""
    probe = _PatchProbe(before, after)
    bound = HandlerBase.__new__(HandlerBase)
    bound._build_config_data = probe._build_config_data  # type: ignore[attr-defined]
    bound._send = probe._send  # type: ignore[attr-defined]
    bound._push_all_data = probe._push_all_data  # type: ignore[attr-defined]
    info = SimpleNamespace(session_id="s-1")
    asyncio.run(HandlerBase._send_config_patch(bound, object(), info, before))
    return probe.sent


def test_models_only_change_still_carries_active_index():
    """A ``models``-only diff must pin the active index alongside it."""
    before = {"models": [{"model_id": "a"}, {"model_id": "b"}], "active_model_index": 1}
    after = {"models": [{"model_id": "a"}, {"model_id": "b", "temperature": 1.0}],
             "active_model_index": 1}

    sent = _drive(before, after)

    assert [kind for kind, _ in sent] == ["config_patch"]
    patch = sent[0][1]["config"]
    assert patch["models"] == after["models"]
    assert patch["active_model_index"] == 1


def test_unchanged_snapshot_pushes_nothing():
    """An empty diff stays silent -- pairing must not fabricate a patch."""
    snapshot = {"models": [{"model_id": "a"}], "active_model_index": 0}

    assert _drive(dict(snapshot), dict(snapshot)) == []


def test_index_change_travels_with_the_new_model_list():
    """A genuine index change sends the new value, never a stale one."""
    before = {"models": [{"model_id": "a"}, {"model_id": "b"}], "active_model_index": 0}
    after = {"models": [{"model_id": "a"}, {"model_id": "b"}], "active_model_index": 1}

    sent = _drive(before, after)

    assert [kind for kind, _ in sent] == ["config_patch"]
    assert sent[0][1]["config"]["active_model_index"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-header"]))
