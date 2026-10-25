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

"""Encoding-neutral byte handling for subprocess / shell output.

Windows shells (cmd.exe / powershell) and locale-driven external programs
exchange text in the **system ANSI code page** — GBK (936) on zh-CN, Shift_JIS
(932) on ja-JP, EUC-KR (949) on ko-KR, cp1251 (1251) on ru-RU, … — *not* UTF-8.
Decoding such output as UTF-8 yields replacement characters the model cannot
use, and encoding commands as UTF-8 garbles non-ASCII input (often producing
no output at all).

Every tool that reads bytes from a subprocess should call :func:`decode_bytes`
instead of ``raw.decode("utf-8", errors="replace")``, and every tool that
writes text into a shell should use :func:`encode_text`.

Decode strategy (in order):
1. strict UTF-8 — the modern default (Linux/macOS, ``PYTHONUTF8``-aware tools,
   UTF-8 system locales);
2. the Windows ANSI code page via ``GetACP()`` — what the child actually wrote;
3. a small set of common legacy code pages as a safety net;
4. lossy UTF-8 so the caller always receives a string.
"""

import sys



from encre.logging_config import get_logger
logger = get_logger(__name__)


def get_windows_acp() -> str | None:
    """Return the system ANSI code page as a Python codec name (e.g. ``cp936``).

    This is what cmd.exe / locale-driven external programs actually use.
    ``locale.getpreferredencoding()`` is unreliable for this purpose (it may
    report ``utf-8`` even when the ANSI code page is 936).
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetACP()
        return f"cp{cp}"
    except Exception:
        return None


# Legacy code pages tried after the system ANSI code page.  Covers the
# common CJK / Cyrillic / Western European cases when a child program writes
# in a code page different from the system one.
_LEGACY_FALLBACKS = ("gb18030", "big5", "shift_jis", "euc_kr", "cp1251", "cp1252")


def decode_bytes(raw: bytes) -> str:
    """Best-effort, encoding-neutral decode of subprocess output bytes."""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.debug("exception suppressed", exc_info=True)
    if sys.platform == "win32":
        acp = get_windows_acp()
        if acp:
            try:
                return raw.decode(acp)
            except (UnicodeDecodeError, LookupError):
                logger.debug("exception suppressed", exc_info=True)
        for enc in _LEGACY_FALLBACKS:
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
    return raw.decode("utf-8", errors="replace")


def encode_text(text: str, *, terminal: str | None = None) -> bytes:
    """Encode text for a shell stdin stream.

    ``terminal`` hints the target shell: ``bash`` / ``pwsh`` are natively
    UTF-8 even on Windows; everything else on Windows uses the ANSI code page.
    """
    if not text:
        return b""
    if terminal in ("bash", "pwsh"):
        return text.encode("utf-8", errors="replace")
    if sys.platform == "win32":
        acp = get_windows_acp()
        if acp:
            try:
                return text.encode(acp, errors="replace")
            except LookupError:
                logger.debug("exception suppressed", exc_info=True)
    return text.encode("utf-8", errors="replace")


__all__ = ["decode_bytes", "encode_text", "get_windows_acp"]
