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

"""Tests for reading a skill's own files (skill_detail.files / get_skill_file).

A ``SKILL.md`` is the entry point of a tree: the documents it cites
(``references/…``, templates, scripts) are the rest of the skill.  These tests
pin that the market's read reaches the whole package — the detail frame lists
the sibling files, and ``get_skill_file`` returns one of them while refusing
anything that leaves the skill's own directory, is missing, is not text, or is
too large to display.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from encre.protocol.handlers.skills import (
    _MAX_SKILL_FILE_BYTES,
    SkillsHandlers,
    _list_skill_files,
    _read_skill_file,
)
from encre.server.protocol import ClientGetSkillDetail, ClientGetSkillFile


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


def _skill_dir(root: Path) -> Path:
    """Build a skill directory with a SKILL.md, a nested doc and build noise."""
    skill = root / "skills" / "demo"
    (skill / "references").mkdir(parents=True)
    (skill / "__pycache__").mkdir()
    (skill / ".hidden").mkdir()
    (skill / "SKILL.md").write_text("---" + chr(10) + "name: demo" + chr(10) + "---" + chr(10) + "# Demo" + chr(10), "utf-8")
    (skill / "references" / "guide.md").write_text("# Guide" + chr(10), "utf-8")
    (skill / "notes.txt").write_text("plain notes", "utf-8")
    (skill / "__pycache__" / "junk.pyc").write_bytes(b"\x00\x01")
    (skill / ".hidden" / "secret.md").write_text("hidden", "utf-8")
    return skill


@pytest.mark.asyncio
async def test_verify_skill_detail_lists_the_packages_own_files(monkeypatch, tmp_path):
    """The detail frame says what else the skill carries besides SKILL.md."""
    skill = _skill_dir(tmp_path)
    _registry(monkeypatch, {"demo": SimpleNamespace(
        description="", body="", file_path=str(skill / "SKILL.md"),
        when_to_use="", argument_hint="", source="bundled", license="", compatibility="",
    )})
    handler = _Frames()
    await SkillsHandlers._h_get_skill_detail(handler, None, ClientGetSkillDetail(name="demo"))
    msg_type, payload = handler.sent[0]
    assert msg_type == "skill_detail"
    assert payload["files"] == ["notes.txt", "references/guide.md"]


def test_verify_list_skill_files_is_empty_without_a_directory():
    """A skill held only in memory lists nothing rather than guessing."""
    assert _list_skill_files(SimpleNamespace(file_path="")) == []


def test_verify_skill_file_reader_returns_nested_content(tmp_path):
    """A skill-relative path inside the skill reads back whole."""
    skill = _skill_dir(tmp_path)
    result = _read_skill_file(skill, "references/guide.md")
    assert result == {"content": "# Guide" + chr(10), "path": "references/guide.md"}


def test_verify_skill_file_reader_normalizes_separators(tmp_path):
    """A Windows-style path from a link still resolves inside the skill."""
    skill = _skill_dir(tmp_path)
    assert _read_skill_file(skill, "references\\guide.md")["content"] == "# Guide" + chr(10)


def test_verify_skill_file_reader_blocks_traversal(tmp_path):
    """A ``..`` path leaving the skill directory is denied, not read."""
    (tmp_path / "outside.md").write_text("secret", "utf-8")
    skill = _skill_dir(tmp_path / "pkg")
    result = _read_skill_file(skill, "../outside.md")
    assert result["content"] == ""
    assert "escape" in result["error"]


def test_verify_skill_file_reader_reports_missing_and_unreadable(tmp_path):
    """Missing, binary and oversized files each answer with an error."""
    skill = _skill_dir(tmp_path)
    assert "not found" in _read_skill_file(skill, "references/nope.md")["error"]
    (skill / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    assert "not a text file" in _read_skill_file(skill, "blob.bin")["error"]
    (skill / "huge.md").write_text("x" * (_MAX_SKILL_FILE_BYTES + 1), "utf-8")
    assert "too large" in _read_skill_file(skill, "huge.md")["error"]


@pytest.mark.asyncio
async def test_verify_get_skill_file_replies_with_content(monkeypatch, tmp_path):
    """The frame carries name, path and the file's own text."""
    skill = _skill_dir(tmp_path)
    _registry(monkeypatch, {"demo": SimpleNamespace(file_path=str(skill / "SKILL.md"))})
    handler = _Frames()
    await SkillsHandlers._h_get_skill_file(
        handler, None, ClientGetSkillFile(name="demo", path="notes.txt"))
    msg_type, payload = handler.sent[0]
    assert msg_type == "skill_file"
    assert payload == {"name": "demo", "path": "notes.txt", "content": "plain notes"}
    assert "error" not in payload


@pytest.mark.asyncio
async def test_verify_get_skill_file_answers_errors_on_the_same_channel(monkeypatch, tmp_path):
    """Unknown skill, or no path, comes back as an error frame rather than silence."""
    skill = _skill_dir(tmp_path)
    _registry(monkeypatch, {"demo": SimpleNamespace(file_path=str(skill / "SKILL.md"))})
    handler = _Frames()
    await SkillsHandlers._h_get_skill_file(handler, None, ClientGetSkillFile(name="nope", path="notes.txt"))
    await SkillsHandlers._h_get_skill_file(handler, None, ClientGetSkillFile(name="demo", path=""))
    assert handler.sent[0][1]["error"]
    assert handler.sent[1][1]["error"]
    assert all("content" in sent[1] for sent in handler.sent)


def test_verify_client_message_round_trips():
    """``get_skill_file`` decodes from wire JSON with both fields."""
    msg = ClientGetSkillFile.from_dict({"name": "demo", "path": "references/guide.md"})
    assert msg.type == "get_skill_file"
    assert msg.name == "demo"
    assert msg.path == "references/guide.md"
