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

"""Tests for Encre backup migration.

Covers the export/import round-trip of the data directory: secrets are
written as ciphertext on disk but materialised as plaintext inside the
archive, and are re-encrypted with the target key on import. Also
covers exclusion of log artifacts, the manifest that records which files
were encrypted and in which mode (text vs raw bytes), and rejection of
archives that do not carry the hidden Encre backup marker.
"""

import json
import zipfile
from pathlib import Path

import pytest
from encre.crypto import decrypt, encrypt, encrypt_raw, ensure_keyfile


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the Encre data directory into a temp folder.

    Points ENCRE_DATA_DIR at a throwaway path so crypto and migration
    calls never touch the real data store.
    """
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ENCRE_DATA_DIR", str(d))
    return d


def test_export_all_roundtrip(data_dir: Path, tmp_path: Path) -> None:
    """Export a mixed data directory and import it back into a fresh one.

    The archive must contain decrypted (plaintext) settings plus a manifest
    that records which files were encrypted and how. On import, secrets are
    re-encrypted with the target key while plaintext files are copied as-is.
    """
    ensure_keyfile()

    # Text-mode encrypted settings file: base64 ciphertext on disk.
    secret = "api_key_that_must_not_leak"
    (data_dir / "settings.json").write_text(encrypt(secret), encoding="utf-8")
    # Raw-byte encrypted file: the payload is binary and must not be decoded.
    raw_secret = b"\x00\x01binary\xff\xfe"
    (data_dir / "browser_key").write_bytes(encrypt_raw(raw_secret))
    # Unencrypted config file that must survive the round-trip byte-for-byte.
    (data_dir / "keybinds.json").write_text('{"k": 1}', encoding="utf-8")
    # Log artifacts must be excluded from the export regardless of location.
    (data_dir / "logs").mkdir()
    (data_dir / "logs" / "debug.log").write_text("noise", encoding="utf-8")
    (data_dir / "yimd.log").write_text("noise", encoding="utf-8")
    (data_dir / "model").mkdir()
    (data_dir / "model" / "config.toml").write_text(encrypt('{"model": "gpt"}'), encoding="utf-8")

    zip_path = Path(str(tmp_path / "backup.zip"))
    from encre.migration import export_all

    out = export_all(zip_path)
    assert out == zip_path and zip_path.exists()

    names = zipfile.ZipFile(zip_path).namelist()
    assert "logs/debug.log" not in names
    assert "yimd.log" not in names
    assert "settings.json" in names
    assert "model/config.toml" in names

    with zipfile.ZipFile(zip_path) as zf:
        # Inside the archive every file is stored as plaintext.
        assert zf.read("settings.json").decode("utf-8") == secret
        assert zf.read("browser_key") == raw_secret
        assert zf.read("keybinds.json").decode("utf-8") == '{"k": 1}'
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["version"] == 1
        assert manifest["encrypted"].get("settings.json") == "text"
        assert manifest["encrypted"].get("browser_key") == "raw"
        assert "keybinds.json" not in manifest["encrypted"]

    # Import into a FRESH data dir and verify re-encryption + copy.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch_fresh = pytest.MonkeyPatch()
    monkeypatch_fresh.setenv("ENCRE_DATA_DIR", str(fresh))

    from encre.migration import import_all

    result = import_all(zip_path)
    assert result["files"] == 4
    assert result["restored"] == 4
    assert result["skipped"] == 0
    assert result["overwritten"] == 0
    assert result["kept"] == 0

    # Re-encrypted: the on-disk ciphertext differs from the plaintext secret.
    on_disk = (fresh / "settings.json").read_text(encoding="utf-8").strip()
    assert on_disk != secret
    assert decrypt(on_disk) == secret
    # The raw re-encrypted file must still decrypt back to the original bytes.
    assert (fresh / "browser_key").read_bytes() != raw_secret
    from encre.crypto import decrypt_raw

    assert decrypt_raw((fresh / "browser_key").read_bytes()) == raw_secret
    # Plaintext files are copied verbatim without re-encryption.
    assert (fresh / "keybinds.json").read_text(encoding="utf-8") == '{"k": 1}'
    assert (fresh / "model" / "config.toml").exists()

    monkeypatch_fresh.undo()


def test_import_all_rejects_invalid_zip(data_dir: Path, tmp_path: Path) -> None:
    """Reject archives that are not genuine Encre backups.

    A real export carries a hidden marker and imports successfully. A
    hand-crafted zip without the marker must raise, and must not wipe the
    target directory even when the caller asks for replace mode.
    """
    ensure_keyfile()
    (data_dir / "a.json").write_text(encrypt('{"v": "x"}'), encoding="utf-8")
    valid_zip = Path(str(tmp_path / "backup.zip"))
    from encre.migration import export_all, import_all

    export_all(valid_zip)

    # A zip carrying the hidden marker imports fine.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    mp = pytest.MonkeyPatch()
    mp.setenv("ENCRE_DATA_DIR", str(fresh))
    assert import_all(valid_zip)["restored"] == 1
    mp.undo()

    # Hand-crafted zip WITHOUT the marker must be rejected (and, critically,
    # must NOT wipe the target dir under "replace").
    import zipfile

    bad_zip = Path(str(tmp_path / "bad.zip"))
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("nope.txt", "not an encre backup")
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    mp = pytest.MonkeyPatch()
    mp.setenv("ENCRE_DATA_DIR", str(target))
    import pytest as _pytest

    with _pytest.raises(ValueError, match="not a valid Encre backup"):
        import_all(bad_zip, mode="replace")
    mp.undo()
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_import_all_modes(data_dir: Path, tmp_path: Path) -> None:
    """Exercise the skip, replace and overwrite restore modes.

    Each mode is applied against a target directory that already contains a
    conflicting file plus a local-only file, so we can assert which entries
    survive the import and which get overwritten by the backup.
    """
    ensure_keyfile()
    # Build a backup with two files: one encrypted, one plaintext.
    (data_dir / "a.json").write_text(encrypt('{"v": "backup"}'), encoding="utf-8")
    (data_dir / "b.json").write_text("plaintext", encoding="utf-8")
    zip_path = Path(str(tmp_path / "backup.zip"))
    from encre.migration import export_all, import_all

    export_all(zip_path)

    def _setup(target: Path) -> None:
        """Prepare a target dir with a conflicting backup entry and a local-only file.

        The conflicting entry mirrors the backup's ``a.json`` with different
        contents so restore modes have something real to keep or overwrite.
        """
        target.mkdir(parents=True, exist_ok=True)
        # Local "old" file that the backup would overwrite.
        (target / "a.json").write_text(encrypt('{"v": "local"}'), encoding="utf-8")
        # Local-only file, absent from the backup.
        (target / "local.json").write_text("local", encoding="utf-8")

    # "skip" keeps the existing file and the local-only file.
    skip_dir = tmp_path / "skip"
    _setup(skip_dir)
    mp = pytest.MonkeyPatch()
    mp.setenv("ENCRE_DATA_DIR", str(skip_dir))
    r = import_all(zip_path, mode="skip")
    assert r["kept"] == 1  # a.json existed
    assert decrypt((skip_dir / "a.json").read_text(encoding="utf-8").strip()) == '{"v": "local"}'
    assert (skip_dir / "b.json").exists()  # new file added
    assert (skip_dir / "local.json").exists()  # local-only kept
    mp.undo()

    # "replace" wipes the dir, so local-only files vanish and a.json is restored.
    repl_dir = tmp_path / "replace"
    _setup(repl_dir)
    mp = pytest.MonkeyPatch()
    mp.setenv("ENCRE_DATA_DIR", str(repl_dir))
    r = import_all(zip_path, mode="replace")
    assert r["restored"] == 2
    assert not (repl_dir / "local.json").exists()  # wiped
    assert decrypt((repl_dir / "a.json").read_text(encoding="utf-8").strip()) == '{"v": "backup"}'
    mp.undo()

    # "overwrite" restores the file and keeps local-only files.
    ov_dir = tmp_path / "overwrite"
    _setup(ov_dir)
    mp = pytest.MonkeyPatch()
    mp.setenv("ENCRE_DATA_DIR", str(ov_dir))
    r = import_all(zip_path, mode="overwrite")
    assert r["overwritten"] == 1
    assert decrypt((ov_dir / "a.json").read_text(encoding="utf-8").strip()) == '{"v": "backup"}'
    assert (ov_dir / "local.json").exists()  # local-only kept
    mp.undo()
