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

import os

"""Load, store, and truncate the MEMORY.md entrypoint of a memory directory.

The entrypoint is the user-facing anchor of an agent's long-term memory.
This module transparently decrypts encrypted memory files, detects text
encodings (UTF-8/16/32 and BOM variants), and enforces configurable line
and byte size caps so the entrypoint can be injected into model prompts
without exceeding context limits.
"""


def _is_encrypted(filepath: str) -> bool:
    """Check if a file is encrypted (doesn't start with '---' or typical markdown)."""
    try:
        with open(filepath, encoding="utf-8") as f:
            head = f.read(20)
        # Encrypted blobs are opaque base64-like text, not markdown markers
        return not (head.startswith("---") or head.startswith("#") or head.startswith("- "))
    except Exception:
        return False


def _read_file(filepath: str) -> str:
    """Read a file, decrypting if encrypted."""
    if not os.path.isfile(filepath):
        return ""
    try:
        with open(filepath, encoding="utf-8") as f:
            raw = f.read().strip()
    except Exception:
        return ""
    if not raw:
        return ""
    if raw.startswith("---") or raw.startswith("#"):
        # Legacy plaintext markers mean no decryption is needed
        return raw  # legacy plaintext
    try:
        from encre.crypto import decrypt
        return decrypt(raw)
    except Exception:
        return raw  # fallback: return as-is


def _write_file(filepath: str, content: str) -> None:
    """Write a file with encryption (fail closed: no plaintext fallback)."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    from encre.crypto import encrypt
    # Persist ciphertext so memory at rest is not readable as plaintext.
    # If encryption fails the exception propagates -- never write plaintext.
    encrypted = encrypt(content)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(encrypted)


def load_entrypoint_raw(memory_dir: str) -> dict:
    """Read the MEMORY.md entrypoint, capping lines and bytes for prompts.

    Returns a dict describing the entrypoint content together with the
    original line/byte counts and flags indicating whether truncation
    occurred. The default caps are 200 lines and 25,000 bytes.

    Args:
        memory_dir: Directory that contains (or should contain) MEMORY.md.

    Returns:
        Dict with keys: content, line_count, byte_count,
        was_line_truncated, was_byte_truncated.
    """
    file_path = os.path.join(memory_dir, "MEMORY.md")
    if not os.path.isfile(file_path):
        return {
            "content": "",
            "line_count": 0,
            "byte_count": 0,
            "was_line_truncated": False,
            "was_byte_truncated": False,
        }
    raw = _read_file(file_path)
    if not raw:
        return {
            "content": "",
            "line_count": 0,
            "byte_count": 0,
            "was_line_truncated": False,
            "was_byte_truncated": False,
        }
    # Detect encoding only for plaintext files; encrypted blobs are UTF-8.
    # (Encrypted content is always stored as UTF-8 text.)
    encoding = _detect_encoding(file_path) if not _is_encrypted(file_path) else "utf-8"
    byte_count = len(raw.encode(encoding))
    lines = raw.split("\n")
    was_line_truncated = len(lines) > 200
    if was_line_truncated:
        # Hard cap on number of lines to keep the prompt bounded
        lines = lines[:200]
    content = "\n".join(lines)
    encoded = content.encode(encoding)
    was_byte_truncated = len(encoded) > 25_000
    if was_byte_truncated:
        truncated = encoded[:25_000]
        # Roll back to the last whole line so we never cut mid-line
        newline_index = truncated.rfind(b"\n")
        if newline_index > 0:
            truncated = truncated[:newline_index]
        content = truncated.decode(encoding)
    return {
        "content": content,
        "line_count": len(lines),
        "byte_count": byte_count,
        "was_line_truncated": was_line_truncated,
        "was_byte_truncated": was_byte_truncated,
    }


def write_entrypoint(memory_dir: str, content: str) -> None:
    """Persist entrypoint content to MEMORY.md (encrypted on disk)."""
    file_path = os.path.join(memory_dir, "MEMORY.md")
    _write_file(file_path, content)


def _detect_encoding(file_path: str) -> str:
    """Sniff a file's byte-order mark to determine its text encoding.

    Inspects the first four bytes for UTF-32/16 (BE/LE) and UTF-8-SIG
    signatures and returns the matching codec name, defaulting to UTF-8.

    Args:
        file_path: Path to the file whose encoding should be detected.

    Returns:
        One of: "utf-32-be", "utf-32-le", "utf-8-sig", "utf-16-be",
        "utf-16-le", or "utf-8".
    """
    with open(file_path, "rb") as f:
        head = f.read(4)
    # Match the byte-order mark (BOM) of each common Unicode encoding
    if head.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32-be"
    if head.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32-le"
    if head[:
        3] == b"\xef\xbb\xbf":
        return "utf-8-sig"
    if head[:
        2] == b"\xfe\xff":
        return "utf-16-be"
    if head[:
        2] == b"\xff\xfe":
        return "utf-16-le"
    return "utf-8"
