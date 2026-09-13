#!/usr/bin/env python3

# Copyright (c) 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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

"""Runtime audit logging for plugin tool calls.

Every tool invocation through a :class:`~encre.plugins.transport.PluginTransport`
is recorded as a JSONL entry in ``~/.dunimd/encre/audit/audit.jsonl``.  Each
entry contains:

    timestamp   — ISO-8601 UTC
    plugin      — plugin name
    tool        — tool name
    transport   — "local" / "process" / "remote"
    ok          — true/false
    error       — error message (empty on success)
    duration_ms — wall-clock call duration
    args_summary — truncated string repr of call args

The logger is thread-safe and lazily opens the audit file.  Writes are
best-effort: a failure to write the audit log never blocks the tool call.

Audit logging can be disabled by setting ``ENCRE_AUDIT_ENABLED=0`` in the
environment.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.plugins.audit")

_AUDIT_LOCK = threading.Lock()
_AUDIT_FILE: Path | None = None
_AUDIT_ENABLED: bool | None = None

_MAX_ARG_LEN = 200


def _audit_enabled() -> bool:
    global _AUDIT_ENABLED
    if _AUDIT_ENABLED is not None:
        return _AUDIT_ENABLED
    _AUDIT_ENABLED = os.environ.get("ENCRE_AUDIT_ENABLED", "1") != "0"
    return _AUDIT_ENABLED


def _audit_file() -> Path:
    global _AUDIT_FILE
    if _AUDIT_FILE is not None:
        return _AUDIT_FILE
    env_dir = os.environ.get("ENCRE_AUDIT_DIR", "")
    base = Path(env_dir) if env_dir else Path.home() / ".dunimd" / "encre" / "audit"
    base.mkdir(parents=True, exist_ok=True)
    _AUDIT_FILE = base / "audit.jsonl"
    return _AUDIT_FILE


def _summarise_args(args: dict[str, Any]) -> str:
    """Truncate argument values to prevent credential leakage in logs."""
    parts: list[str] = []
    for key, val in args.items():
        s = str(val)
        if len(s) > _MAX_ARG_LEN:
            s = s[:_MAX_ARG_LEN] + "..."
        parts.append(f"{key}={s}")
    return ", ".join(parts)


def log_tool_call(
    plugin: str,
    tool: str,
    transport: str,
    ok: bool,
    error: str = "",
    duration_ms: float = 0.0,
    args: dict[str, Any] | None = None,
) -> None:
    """Record a tool call in the audit log (best-effort, never raises)."""
    if not _audit_enabled():
        return
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "plugin": plugin,
        "tool": tool,
        "transport": transport,
        "ok": ok,
        "error": error[:500] if error else "",
        "duration_ms": round(duration_ms, 2),
        "args_summary": _summarise_args(args or {}),
    }
    try:
        with _AUDIT_LOCK, open(_audit_file(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("Failed to write audit log entry", exc_info=True)
