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

"""Tests for the market's per-skill read (get_skill_detail).

The boot catalogue hides bundled and managed skills, so the plugin market asks
for a skill's own text by name.  These tests pin that reply: the skill's own
description and ``SKILL.md`` body go out untouched, and a name the registry
does not know comes back as an error frame rather than a silent drop.
"""

from types import SimpleNamespace

import pytest
from encre.protocol.handlers.skills import SkillsHandlers
from encre.server.protocol import ClientGetSkillDetail


class _Frames:
    """A composed handler that only records what the reply would send."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def _send(self, _ws, msg_type: str, **kwargs) -> None:
        self.sent.append((msg_type, kwargs))


def _registry(monkeypatch, skills: dict) -> None:
    """Point the shared skill registry at *skills* for one test."""
    import encre.agent as agent

    monkeypatch.setattr(agent, "_get_shared_skill_registry", lambda: SimpleNamespace(_skills=skills))


async def _ask(handler, name: str) -> tuple[str, dict]:
    """Run one request and return the frame it sent."""
    await SkillsHandlers._h_get_skill_detail(handler, None, ClientGetSkillDetail(name=name))
    assert len(handler.sent) == 1
    return handler.sent[0]


@pytest.mark.asyncio
async def test_verify_skill_detail_returns_the_skills_own_text(monkeypatch):
    """The reply carries the skill's description and body verbatim."""
    _registry(
        monkeypatch,
        {
            "demo": SimpleNamespace(
                description="what demo does",
                body="# Demo body" + chr(10),
                file_path="",
                when_to_use="",
                argument_hint="",
                source="bundled",
                license="",
                compatibility="",
            )
        },
    )
    handler = _Frames()
    msg_type, payload = await _ask(handler, "demo")
    assert msg_type == "skill_detail"
    assert payload["name"] == "demo"
    assert payload["description"] == "what demo does"
    assert payload["body"] == "# Demo body" + chr(10)
    assert payload["source"] == "bundled"
    assert "error" not in payload


@pytest.mark.asyncio
async def test_verify_skill_detail_reads_the_file_when_body_is_uncached(monkeypatch):
    """A skill whose body is not cached is still readable from disk."""
    _registry(
        monkeypatch,
        {
            "demo": SimpleNamespace(
                description="",
                body="",
                file_path="/skills/demo/SKILL.md",
                when_to_use="",
                argument_hint="",
                source="bundled",
                license="",
                compatibility="",
            )
        },
    )
    import encre.skills.registry as skills_registry

    monkeypatch.setattr(skills_registry, "_read_skill_source", lambda _p: "read from file")
    handler = _Frames()
    _msg_type, payload = await _ask(handler, "demo")
    assert payload["body"] == "read from file"


@pytest.mark.asyncio
async def test_verify_skill_detail_reports_unknown_skill(monkeypatch):
    """An unknown name answers with an error rather than dropping the request."""
    _registry(monkeypatch, {})
    handler = _Frames()
    msg_type, payload = await _ask(handler, "nope")
    assert msg_type == "skill_detail"
    assert payload["error"]
    assert not payload.get("body")


@pytest.mark.asyncio
async def test_verify_skill_detail_rejects_an_empty_name(monkeypatch):
    """A request without a name is refused on the same channel."""
    _registry(monkeypatch, {})
    handler = _Frames()
    msg_type, payload = await _ask(handler, "   ")
    assert msg_type == "skill_detail"
    assert payload["error"]
