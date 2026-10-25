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

"""Encrypted-at-rest file I/O -- the single write channel for data-tree files.

Every file under the data directory that carries user data must be written
through these helpers so it lands on disk as AES-256-GCM ciphertext.  Before
this module existed each subsystem rolled its own ``open(..., "w")`` and the
encryption coverage was inconsistent (see
``docs/architecture/data-storage-refactor-plan.md`` §P2-1).

Design
======
* **Write always encrypts.**  ``write_text`` / ``write_json`` / ``write_bytes``
  never store plaintext.
* **Read is tolerant.**  AES-GCM carries an authentication tag, so a value
  that is not ours fails to decrypt with overwhelming probability.  When that
  happens the raw text is returned unchanged, which keeps data written by an
  older plaintext build readable.  The next write re-encrypts it.
* **Writes are atomic.**  A sibling temp file is populated and then
  ``os.replace``-d over the target, so a crash mid-write cannot truncate an
  existing file.
"""


import errno
import json as _json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

__all__ = [
    "append_jsonl",
    "is_encrypted",
    "read_bytes",
    "read_json",
    "read_jsonl",
    "read_text",
    "write_bytes",
    "write_json",
    "write_text",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decrypt_or_none(raw: str) -> str | None:
    """Return the decrypted text, or ``None`` when *raw* is not ciphertext."""
    if not raw:
        return None
    try:
        from encre.crypto import decrypt

        return decrypt(raw)
    except Exception:
        return None


def _clear_stray_directory(dst: str) -> None:
    """Remove *dst* when it is an empty directory blocking an atomic replace.

    ``os.replace`` can never move a file onto a directory: Windows answers
    ``ERROR_ACCESS_DENIED`` (WinError 5, indistinguishable from the transient
    locks the retry loop below exists for) and POSIX answers ``EISDIR``.  A
    data-tree path that some build created as a *directory* under its own file
    name therefore failed every subsequent write to it, so an empty leftover is
    cleared here and the real file takes its place.  A non-empty directory is a
    genuine conflict: it is reported, never deleted.
    """
    if not os.path.isdir(dst):
        return
    try:
        os.rmdir(dst)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise IsADirectoryError(
            errno.EISDIR,
            f"cannot write {dst!r}: blocked by a non-empty directory",
        ) from exc


def _os_replace_with_retry(src: str, dst: str, *, attempts: int = 10) -> None:
    """``os.replace`` with a short retry loop for Windows transient locks.

    On Windows, ``os.replace`` raises ``PermissionError`` (WinError 5 access
    denied / WinError 32 sharing violation) when the destination is briefly
    held open by antivirus, the search indexer or a concurrent reader.  That
    lock almost always releases within milliseconds, so a small backoff turns
    the spurious crash into a short pause.  A persistent lock (bad ACLs,
    destination is a directory, ...) still raises after the final attempt.

    A destination that exists as a directory is not a lock, so it is resolved
    up front by :func:`_clear_stray_directory` -- retrying it would only burn
    the backoff before the same deterministic failure.
    """
    _clear_stray_directory(dst)
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(min(0.02 * (attempt + 1), 0.1))


def _atomic_write_text(path: Path, text: str, encoding: str) -> None:
    """Write *text* to *path* atomically (temp file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp-", suffix=path.suffix or ".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        _os_replace_with_retry(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            logger.debug("exception suppressed", exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def is_encrypted(raw: str) -> bool:
    """Return True when *raw* decrypts with the active master key."""
    return bool(raw) and _decrypt_or_none(raw.strip()) is not None


def read_text(path: Any, *, encoding: str = "utf-8", default: str = "") -> str:
    """Read *path*, decrypting when it holds ciphertext.

    Returns *default* when the file is missing or unreadable.  Plaintext from
    a legacy build is returned unchanged.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding=encoding)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return default
    if not raw:
        return raw
    plain = _decrypt_or_none(raw.strip())
    return plain if plain is not None else raw


def write_text(path: Any, content: str, *, encoding: str = "utf-8") -> None:
    """Encrypt *content* and write it to *path* atomically."""
    from encre.crypto import encrypt

    _atomic_write_text(Path(path), encrypt(content), encoding)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def read_json(path: Any, *, default: Any = None) -> Any:
    """Read and parse a JSON file, decrypting it first when needed."""
    text = read_text(path, default=None)  # type: ignore[arg-type]
    if text is None:
        return default
    try:
        return _json.loads(text)
    except (ValueError, TypeError):
        return default


def write_json(path: Any, data: Any, *, indent: int = 2) -> None:
    """Serialise *data*, encrypt it and write to *path* atomically."""
    write_text(path, _json.dumps(data, ensure_ascii=False, indent=indent))


# ---------------------------------------------------------------------------
# Raw bytes
# ---------------------------------------------------------------------------

def read_bytes(path: Any, *, default: bytes | None = None) -> bytes | None:
    """Read *path* as bytes, decrypting when it holds our packed ciphertext."""
    from encre.crypto import decrypt_raw

    p = Path(path)
    try:
        raw = p.read_bytes()
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return default
    if not raw:
        return raw
    try:
        return decrypt_raw(raw)
    except Exception:
        return raw


def write_bytes(path: Any, data: bytes) -> None:
    """Encrypt *data* and write it to *path* atomically."""
    from encre.crypto import encrypt_raw

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(p.parent), prefix=".tmp-", suffix=p.suffix or ".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encrypt_raw(data))
        _os_replace_with_retry(tmp, str(p))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            logger.debug("exception suppressed", exc_info=True)
        raise


# ---------------------------------------------------------------------------
# JSON Lines (one independently-encrypted record per line)
# ---------------------------------------------------------------------------

def append_jsonl(path: Any, record: Any) -> None:
    """A

from encre.logging_config import get_logger


logger = get_logger(__name__)
ppend a single encrypted JSON *record* as a new line."""
    from encre.crypto import encrypt

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = encrypt(_json.dumps(record, ensure_ascii=False))
    with open(p, "a", encoding="utf-8", newline="") as fh:
        fh.write(line + "\n")


def read_jsonl(path: Any) -> list[Any]:
    """Read a JSONL file whose records may each be encrypted."""
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return []
    out: list[Any] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        plain = _decrypt_or_none(line)
        text = plain if plain is not None else line
        try:
            out.append(_json.loads(text))
        except (ValueError, TypeError):
            continue
    return out
