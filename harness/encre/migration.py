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

"""Full data-dir export/import for migrating an Encre install between machines.

The whole ``~/.dunimd/encre`` data directory is bundled into a *plaintext* zip
(with the exception of ``logs``, ``yimd.log`` and ``yimd.pid``).  Every file
that was AES-256-GCM encrypted with the *source* machine's key is decrypted
first so the resulting archive contains only readable plaintext.  A
``manifest.json`` inside the archive records which files were encrypted and in
which wire format (``text`` = base64 string, ``raw`` = ``nonce||ct||tag``
bytes), so :func:`import_all` can re-encrypt exactly the same set with the
*target* machine's freshly-generated key (see ``encre.crypto.ensure_keyfile``).

Because AES-GCM verification uses a 16-byte authentication tag, a successful
decrypt is cryptographically conclusive: the file was encrypted by this
machine's key.  A failed decrypt therefore means the file was plaintext on disk
and it is copied verbatim.
"""

import json
import re
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from encre.config import get_data_dir
from encre.crypto import decrypt_bytes, decrypt_raw, encrypt_bytes, encrypt_raw, ensure_keyfile


from encre.logging_config import get_logger


logger = get_logger(__name__)

__all__ = ["export_all", "import_all"]

# A ``Callable[[int, int, str], None]`` that receives ``(done, total, current)``
# progress, where ``current`` is the archive name of the file being processed.
Progress = Callable[[int, int, str], None]

_MANIFEST_NAME = "manifest.json"
_MANIFEST_VERSION = 1

# Hidden marker embedded in the zip *archive comment* (not a file entry) so the
# archive can be authenticated as an Encre backup.  ``import_all`` rejects any
# zip whose comment does not start with this magic, guarding against importing
# arbitrary or corrupt files.
_ZIP_MAGIC = b"ENCRE-ZIP\x01"

# Logs are intentionally excluded from backups (the user asked to skip them).
# The keyfile directory is excluded too: a plaintext backup must never carry
# key material -- even machine-wrapped -- because the archive travels between
# machines; import_all re-encrypts against the target machine's own key.
_EXCLUDED_DIRS = frozenset({"logs", "keys"})
_EXCLUDED_FILES = frozenset({"yimd.log", "yimd.pid"})
# Prior backups must not be re-exported inside a newer archive.
_BACKUP_PREFIX = "encre-backup-"

# Base64 alphabet -- used to cheaply pre-screen "text"-format ciphertext.
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=]+$")


def _fix_session_workspace(data: bytes) -> bytes:
    """Strip absolute workspace paths that don't exist on the target machine."""
    try:
        obj = json.loads(data.decode("utf-8"))
        ws = obj.get("metadata", {}).get("workspace")
        if isinstance(ws, str) and ws and Path(ws).is_absolute() and not Path(ws).exists():
            obj["metadata"]["workspace"] = ""
            return json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except Exception:
        logger.debug("exception suppressed", exc_info=True)
    return data


def _is_excluded(rel: Path) -> bool:
    """Return True when the relative path names a log artefact to skip."""
    if rel.name in _EXCLUDED_FILES:
        return True
    if rel.parts and rel.parts[0] in _EXCLUDED_DIRS:
        return True
    if rel.name.startswith(_BACKUP_PREFIX) and rel.name.endswith(".zip"):
        return True
    return False


def _decrypt_or_copy(data: bytes) -> tuple[str | None, bytes]:
    """Return ``(format, plaintext_bytes)`` for one on-disk file.

    ``format`` is ``"text"`` (base64 string ciphertext), ``"raw"``
    (``nonce||ct||tag`` bytes) or ``None`` for plaintext files.
    """
    # Prefer the base64 "text" format first (settings.json, model/config.toml,
    # memory/*.md, keybinds.json, ...).  The file must look like base64.
    try:
        text = data.decode("utf-8").strip()
        if text and _BASE64_RE.fullmatch(text):
            return "text", decrypt_bytes(text)
    except Exception:
        logger.debug("exception suppressed", exc_info=True)
    # Fall back to the raw binary format.
    try:
        return "raw", decrypt_raw(data)
    except Exception:
        logger.debug("exception suppressed", exc_info=True)
    return None, data


def export_all(zip_path: str | Path | None = None, progress: Progress | None = None) -> Path:
    """Decrypt every file under the data dir and bundle it into a plaintext zip.

    ``progress``, when given, is called as ``progress(done, total, current)`` as
    files are processed so callers can surface a progress bar and the name of
    the file currently being handled.  Returns the path of the created archive.
    """
    data_dir = get_data_dir()
    if zip_path is None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        zip_path = data_dir / f"encre-backup-{stamp}.zip"
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    files = [
        fpath
        for fpath in sorted(data_dir.rglob("*"))
        if fpath.is_file()
        and not _is_excluded(fpath.relative_to(data_dir))
        and fpath.relative_to(data_dir).as_posix() != _MANIFEST_NAME
    ]
    total = len(files)
    manifest: dict[str, Any] = {
        "version": _MANIFEST_VERSION,
        "encrypted": {},
    }
    done = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in files:
            rel = fpath.relative_to(data_dir)
            arc = rel.as_posix()
            if progress:
                progress(done, total, arc)
            try:
                data = fpath.read_bytes()
            except OSError:
                done += 1
                continue
            fmt, plain = _decrypt_or_copy(data)
            if fmt is not None:
                manifest["encrypted"][arc] = fmt
            zf.writestr(arc, plain)
            done += 1
        zf.writestr(_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
        # Stamp the hidden validation marker into the archive comment.
        zf.comment = _ZIP_MAGIC
    return zip_path


def import_all(
    zip_path: str | Path,
    mode: str = "overwrite",
    progress: Progress | None = None,
) -> dict[str, int]:
    """Restore a plaintext backup zip into the current machine's data dir.

    Files recorded as encrypted in the manifest are re-encrypted with a fresh
    key generated for this machine (via :func:`ensure_keyfile`) before being
    written, in their original wire format.  Plaintext files are copied as-is.
    ``progress``, when given, is called as ``progress(done, total, current)``
    per file processed.

    ``mode`` decides how imported files interact with data already on this
    machine:

    * ``"overwrite"`` (default) -- every backup file is written; a local file
      at the same path is overwritten, while local-only files are kept.  This
      merges the backup into the existing data.
    * ``"replace"`` -- the data dir is emptied first, then the backup is
      restored exactly, producing a faithful clone of the source machine.
    * ``"skip"`` -- a file is only written when nothing exists at that path
      yet; existing files are never touched.

    The returned dict reports ``files`` (entries in the archive), ``restored``
    (files written), ``overwritten`` (restored files that replaced an existing
    local file), ``kept`` (existing files left untouched by ``"skip"``) and
    ``skipped`` (entries rejected, e.g. path traversal).
    """
    if mode not in ("overwrite", "replace", "skip"):
        raise ValueError(f"unknown import mode: {mode!r}")
    # Validate the archive BEFORE doing anything destructive (e.g. the
    # "replace" wipe).  Only archives we produced carry the hidden marker.
    with zipfile.ZipFile(Path(zip_path), "r") as zf:
        if not zf.comment.startswith(_ZIP_MAGIC):
            raise ValueError("not a valid Encre backup archive")
    # Generate (or load) the target machine's key BEFORE re-encrypting so every
    # restored secret is bound to this machine.
    ensure_keyfile()
    data_dir = get_data_dir()

    if mode == "replace" and data_dir.exists():
        # Wipe the whole data dir for a clean, faithful restore.  The caller
        # must have confirmed this -- destructive by design.
        for child in list(data_dir.iterdir()):
            if child.is_dir():
                for sub in list(child.rglob("*")):
                    if sub.is_dir():
                        sub.rmdir()
                    else:
                        sub.unlink()
                child.rmdir()
            else:
                child.unlink()

    data_dir.mkdir(parents=True, exist_ok=True)

    encrypted_map: dict[str, str] = {}
    total = 0
    restored = 0
    skipped = 0
    overwritten = 0
    kept = 0
    with zipfile.ZipFile(Path(zip_path), "r") as zf:
        names = zf.namelist()
        if _MANIFEST_NAME in names:
            try:
                mf = json.loads(zf.read(_MANIFEST_NAME))
                encrypted_map = mf.get("encrypted", {}) or {}
            except (json.JSONDecodeError, KeyError, TypeError):
                encrypted_map = {}
        files = [n for n in names if n != _MANIFEST_NAME and not n.endswith("/")]
        total = len(files)
        done = 0
        for name in files:
            if progress:
                progress(done, total, name)
            # Defend against path traversal from a hand-crafted archive.
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts:
                skipped += 1
                done += 1
                continue
            data = zf.read(name)
            if name.startswith("sessions/") and name.endswith("/meta.json"):
                data = _fix_session_workspace(data)
            fmt = encrypted_map.get(name)
            if fmt == "text":
                content = encrypt_bytes(data).encode("utf-8")
            elif fmt == "raw":
                content = encrypt_raw(data)
            else:
                content = data
            target = data_dir.joinpath(*pure.parts)
            if mode == "skip" and target.exists():
                kept += 1
                done += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "overwrite" and target.exists():
                overwritten += 1
            target.write_bytes(content)
            restored += 1
            done += 1
    return {
        "files": total,
        "restored": restored,
        "overwritten": overwritten,
        "kept": kept,
        "skipped": skipped,
    }
