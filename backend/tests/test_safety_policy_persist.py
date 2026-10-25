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

"""Persistence regression tests for the learned safety-policy chain.

``EncreSafetyPolicyReflow`` resolved its default document through
``get_data_dir("safety", "learned_policies.json")`` -- a helper that *creates*
every path it returns.  The file name was therefore created as an empty
directory, and every atomic write then failed on ``os.replace`` with
``PermissionError: [WinError 5]`` (Windows refuses to replace a directory with
a file; POSIX refuses with ``EISDIR``).  The turn-tail hook logged the failure
at debug level and carried on, so learned policies lived only in memory and
vanished on restart.

Covered here:

1. the default storage path is a file path, never a directory;
2. back-to-back persists overwrite -- the document holds the *second* payload;
3. a stray directory left at the target by the old build is healed, and the
   write still succeeds twice in a row;
4. the same healing for the ``write_bytes`` atomic-replace site;
5. a non-empty directory is reported, never silently deleted;
6. a persist failure propagates out of ``persist`` and is reported loudly by
   the turn-tail hook instead of being swallowed.

Nothing is mocked except ``os.replace`` (to inject a failure) and the hook's
logger (to assert the level).  ``ENCRE_DATA_DIR`` points the data root at
``tmp_path``; the production resolver is used untouched.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from encre.loop.phases.end_of_turn import PhaseEndOfTurnMixin
from encre.safety_policy import EncreSafetyPolicyReflow, PolicyEntry
from encre.secure_io import read_bytes, read_json, write_bytes

POLICY_NAME = "learned_policies.json"


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """Redirect the data tree at ``tmp_path`` and return its safety subdir."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path))
    return tmp_path / "safety"


def _entry(scope: str) -> PolicyEntry:
    """A deny entry scoped to *scope*."""
    return PolicyEntry(
        tool="file_read", path_scope=scope, policy="deny",
        reason=f"never read {scope}", rule=f"learned:file_read:deny:{scope}",
    )


def _stray_document(path: Path) -> None:
    """Reproduce the poisoned target: a directory named like the document."""
    path.mkdir(parents=True, exist_ok=True)
    assert path.is_dir()


def _leftover_temps(directory: Path) -> list[str]:
    """Names of temp files the atomic writer failed to clean up."""
    return [p.name for p in directory.glob(".tmp-*")]


# ---------------------------------------------------------------------------
# 1. The path itself
# ---------------------------------------------------------------------------


def test_default_storage_path_is_a_file_path_not_a_directory(data_root):
    """The default document path must be writable as a file, not a dir."""
    reflow = EncreSafetyPolicyReflow()

    assert reflow.storage_path == data_root / POLICY_NAME
    # ``get_data_dir`` is expected to create the *directory* holding the file.
    assert reflow.storage_path.parent.is_dir()
    assert not reflow.storage_path.is_dir(), (
        "the storage path was created as a directory -- os.replace() over it "
        "fails with WinError 5 and nothing can ever be persisted"
    )

    reflow.persist([_entry("/srv/private")])

    assert reflow.storage_path.is_file()


def test_wired_reflow_storage_path_matches_the_default(data_root):
    """A learner-supplied reflow keeps the same (healed) default location."""
    learner = MagicMock()
    learner.iter_records.return_value = []
    reflow = EncreSafetyPolicyReflow(learner)

    assert reflow.storage_path == data_root / POLICY_NAME
    reflow.persist([_entry("/home/user/.secrets")])
    assert reflow.storage_path.is_file()


# ---------------------------------------------------------------------------
# 2. Consecutive persists really overwrite
# ---------------------------------------------------------------------------


def test_consecutive_persists_overwrite_with_the_second_payload(data_root):
    """Two persists in a row: the second document is what lands on disk."""
    reflow = EncreSafetyPolicyReflow(storage_path=data_root / POLICY_NAME)

    reflow.persist([_entry("/first")])
    first = read_json(reflow.storage_path)
    assert [e["path_scope"] for e in first["entries"]] == ["/first"]

    reflow.persist([_entry("/second"), _entry("/third")])
    second = read_json(reflow.storage_path)
    assert [e["path_scope"] for e in second["entries"]] == ["/second", "/third"]
    assert second["version"] == reflow.SCHEMA_VERSION

    # Read-back path used by startup restore.
    reloaded = EncreSafetyPolicyReflow(storage_path=reflow.storage_path).load()
    assert [e.path_scope for e in reloaded] == ["/second", "/third"]

    assert _leftover_temps(data_root) == []


def test_document_stays_encrypted(data_root):
    """The healed write still uses the encrypted at-rest format."""
    reflow = EncreSafetyPolicyReflow(storage_path=data_root / POLICY_NAME)
    reflow.persist([_entry("/srv/secret-keys")])

    raw = reflow.storage_path.read_text(encoding="utf-8")
    assert "/srv/secret-keys" not in raw
    assert read_json(reflow.storage_path)["entries"][0]["path_scope"] == "/srv/secret-keys"


# ---------------------------------------------------------------------------
# 3 + 4. A stray directory at the target is healed
# ---------------------------------------------------------------------------


def test_persist_heals_stray_directory_and_overwrites_twice(data_root):
    """The WinError 5 regression: an old poisoned target still gets written."""
    document = data_root / POLICY_NAME
    _stray_document(document)

    reflow = EncreSafetyPolicyReflow(storage_path=document)
    reflow.persist([_entry("/first")])
    reflow.persist([_entry("/second"), _entry("/third")])

    assert document.is_file()
    assert not document.is_dir()
    assert [e["path_scope"] for e in read_json(document)["entries"]] == ["/second", "/third"]
    assert _leftover_temps(data_root) == []


def test_write_bytes_heals_stray_directory(data_root):
    """The other atomic-replace site in secure_io heals the same way."""
    target = data_root / "blob.bin"
    _stray_document(target)

    write_bytes(target, b"first")
    write_bytes(target, b"second")

    assert target.is_file()
    assert read_bytes(target) == b"second"


# ---------------------------------------------------------------------------
# 5. A non-empty directory is a real conflict
# ---------------------------------------------------------------------------


def test_non_empty_directory_is_reported_not_deleted(data_root):
    """Nothing is destroyed to make room: the collision is raised instead."""
    document = data_root / POLICY_NAME
    _stray_document(document)
    (document / "unknown.json").write_text("keep me", encoding="utf-8")

    reflow = EncreSafetyPolicyReflow(storage_path=document)
    with pytest.raises(IsADirectoryError, match="non-empty directory"):
        reflow.persist([_entry("/first")])

    assert document.is_dir()
    assert (document / "unknown.json").read_text(encoding="utf-8") == "keep me"
    assert _leftover_temps(data_root) == []


# ---------------------------------------------------------------------------
# 6. Failures are loud
# ---------------------------------------------------------------------------


def test_persist_failure_propagates_and_cleans_up(data_root, monkeypatch):
    """No silent in-memory fallback: the write error reaches the caller."""

    def _denied(_src, _dst):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(os, "replace", _denied)
    reflow = EncreSafetyPolicyReflow(storage_path=data_root / POLICY_NAME)

    with pytest.raises(PermissionError):
        reflow.persist([_entry("/first")])

    assert _leftover_temps(data_root) == []


def test_turn_tail_hook_reports_a_failed_persist(data_root, monkeypatch):
    """The turn-tail hook surfaces a lost policy document at error level."""
    reflow = EncreSafetyPolicyReflow(storage_path=data_root / POLICY_NAME)
    reflow.persist = MagicMock(side_effect=PermissionError(13, "denied"))

    loop = SimpleNamespace(
        _policy_reflow=reflow,
        feedback=SimpleNamespace(record_count=3),
        session=SimpleNamespace(id="session-1"),
        _last_reflow_record_count=-1,
    )

    logger = MagicMock()
    monkeypatch.setattr("encre.loop.phases.end_of_turn.logger", logger)

    PhaseEndOfTurnMixin._dispatch_safety_reflow(loop)

    logger.error.assert_called_once()
    message = " ".join(str(a) for a in logger.error.call_args.args)
    assert "NOT saved" in message
    assert str(data_root / POLICY_NAME) in message
    logger.debug.assert_not_called()
    # The policy document never reached disk, so the count is not advanced and
    # the next turn retries the reflow.
    assert loop._last_reflow_record_count == -1
