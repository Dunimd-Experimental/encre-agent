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

"""Encre hook system: YAML / JSON loader.

Turns user-authored hook files (``.encre/hooks.yaml``, ``.claude/settings.json``)
into registered handlers on an :class:`~encre.hooks.system.EncreHookSystem`.
A :class:`HookEntry` pairs an event type with a regex ``matcher``, a
``command`` / ``python`` hook body, a timeout, and its source path.

The executor (:func:`_run_command_entry`) follows the Claude Code exit-code
contract: exit ``0`` + JSON stdout => structured decision, exit ``2`` =>
block, anything else => non-blocking.  This lets Encre reuse existing
Claude Code hook configs unchanged.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any

from encre.hooks.system import EncreHookSystem
from encre.hooks.types import HookEventType, HookResult
from encre.tools._encoding import decode_bytes

logger = logging.getLogger(__name__)


# Default timeout in milliseconds for a single hook invocation.  Matches
# the value we use elsewhere in the runtime; individual hook entries
# may override it through the ``timeout`` field.
_DEFAULT_HOOK_TIMEOUT_MS = 5000


# Maps Claude Code hook event names to the names used by
# :class:`EncreHookSystem`.  When a user-written ``.encre/hooks.yaml``
# uses the Encre-native names, they pass through unchanged.
_CLAUDE_TO_ENCRE_EVENT: dict[str, str] = {
    "PreToolUse": "pre_tool_exec",
    "PostToolUse": "post_tool_exec",
    "UserPromptSubmit": "pre_model_request",
    "SessionStart": "on_session_start",
    "SessionEnd": "on_session_end",
    "Stop": "on_turn_end",
    "SubagentStop": "post_sub_agent",
}

# Mirrors ``EncreHookSystem._ALL_EVENTS`` but kept as a local tuple so
# we can validate the event name without reaching into a private
# attribute on the system class.
_KNOWN_EVENTS: tuple[str, ...] = (
    "pre_tool_exec", "post_tool_exec", "on_tool_progress", "pre_bash",
    "on_session_start", "on_session_end", "on_checkpoint",
    "on_turn_start", "on_turn_end",
    "pre_model_request", "post_model_response",
    "on_permission_request", "on_permission_response",
    "on_error", "on_backend_error", "on_rate_limit",
    "pre_compact", "post_compact",
    "pre_sub_agent", "post_sub_agent",
    "on_goal_progress",
    "on_file_change",
    "on_telemetry",
)


@dataclass(frozen=True)
class HookEntry:
    """A single hook entry as decoded from the YAML config."""

    event_type: HookEventType
    matcher: re.Pattern[str]
    hook_type: str
    command: str
    timeout_ms: int
    source_path: str

    def matches(self, tool_name: str) -> bool:
        return bool(self.matcher.search(tool_name or ""))


def _normalize_event_name(name: str) -> str:
    return _CLAUDE_TO_ENCRE_EVENT.get(name, name)


def _parse_matcher(value: str | None) -> re.Pattern[str]:
    """Compile the matcher field.  ``None``/empty matches everything."""
    if not value:
        return re.compile(".*")
    try:
        return re.compile(value)
    except re.error:
        # Fall back to literal substring match to avoid aborting the
        # whole load because of one bad regex.
        return re.compile(re.escape(value))


def _decode_entry(
    raw: dict[str, Any], event_type: HookEventType, source_path: str
) -> HookEntry | None:
    hook_type = str(raw.get("type", "command")).strip().lower() or "command"
    command = str(raw.get("command", "")).strip()
    if not command:
        return None
    if hook_type not in ("command", "python"):
        # Only shell / python hooks are supported in v1.
        return None
    timeout_raw = raw.get("timeout", _DEFAULT_HOOK_TIMEOUT_MS)
    try:
        timeout_ms = int(timeout_raw)
    except (TypeError, ValueError):
        timeout_ms = _DEFAULT_HOOK_TIMEOUT_MS
    if timeout_ms <= 0:
        timeout_ms = _DEFAULT_HOOK_TIMEOUT_MS
    return HookEntry(
        event_type=event_type,
        matcher=_parse_matcher(raw.get("matcher")),
        hook_type=hook_type,
        command=command,
        timeout_ms=timeout_ms,
        source_path=source_path,
    )


def _load_yaml(path: str) -> dict[str, Any] | None:
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not available; skipping hook file %s", path)
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Failed to read hook file %s: %s", path, e)
        return None
    except Exception as e:  # yaml.YAMLError etc.
        logger.warning("Invalid YAML in hook file %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _extract_entries(
    raw: dict[str, Any], source_path: str
) -> list[HookEntry]:
    """Walk a decoded YAML object and produce flat hook entries."""
    # Accept both the bare ``{"pre_tool_exec": [...]}`` shape and the
    # Claude Code-style ``{"hooks": {"PreToolUse": [...]}}`` envelope.
    payload: dict[str, Any] = raw["hooks"] if "hooks" in raw and isinstance(raw["hooks"], dict) else {k: v for k, v in raw.items() if k != "hooks"}
    entries: list[HookEntry] = []
    for raw_name, raw_list in payload.items():
        if not isinstance(raw_list, list):
            continue
        normalized = _normalize_event_name(str(raw_name))
        if normalized not in _KNOWN_EVENTS:
            continue
        event_type: HookEventType = normalized  # type: ignore[assignment]
        for raw_entry in raw_list:
            if not isinstance(raw_entry, dict):
                continue
            entry = _decode_entry(raw_entry, event_type, source_path)
            if entry is not None:
                entries.append(entry)
    return entries


async def _run_command_entry(
    entry: HookEntry, context: dict[str, Any]
) -> HookResult:
    """Execute a single command-style hook and translate its output.

    Honours the Claude Code exit-code contract:
      * ``0`` with no JSON on stdout => allow
      * ``0`` with JSON stdout       => structured decision
      * ``2``                       => block (stderr becomes reason)
      * other                       => log and treat as non-blocking
    """
    try:
        payload = json.dumps(context, ensure_ascii=False)
    except (TypeError, ValueError):
        payload = json.dumps({"_unserializable": repr(context)})

    argv: list[str]
    if entry.hook_type == "python":
        argv = ["python", "-c", entry.command]
    else:
        try:
            argv = shlex.split(entry.command, posix=(os.name != "nt"))
        except ValueError:
            argv = [entry.command]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        logger.warning("Hook command not found: %s (%s)", entry.command, e)
        return {}

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(payload.encode("utf-8")),
            timeout=entry.timeout_ms / 1000.0,
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        logger.warning(
            "Hook timed out after %dms: %s", entry.timeout_ms, entry.command,
        )
        return {}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Hook crashed: %s (%s)", entry.command, e)
        return {}

    if proc.returncode == 2:
        reason = decode_bytes(stderr_b or b"").strip()
        if not reason:
            reason = f"Blocked by hook: {entry.event_type}"
        return {"block": True, "block_reason": reason}

    if proc.returncode != 0:
        logger.warning(
            "Hook exited with %d: %s", proc.returncode, entry.command,
        )
        return {}

    out = decode_bytes(stdout_b or b"").strip()
    if not out:
        return {}
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        # Treat as raw extra context so post-tool handlers can still
        # surface useful information to the model.
        return {"extra_context": out}
    if not isinstance(parsed, dict):
        return {}

    result: HookResult = {}
    if parsed.get("block") is True:
        result["block"] = True
        result["block_reason"] = str(
            parsed.get("reason") or parsed.get("block_reason")
            or f"Blocked by hook: {entry.event_type}"
        )
    decision = parsed.get("permissionDecision") or parsed.get("decision")
    if decision in ("allow", "deny", "ask"):
        result["block"] = decision == "deny"
        if decision == "deny":
            result["block_reason"] = str(
                parsed.get("reason") or f"Denied by hook: {entry.event_type}"
            )
    if isinstance(parsed.get("updatedInput"), dict):
        result["modified_input"] = parsed["updatedInput"]
    if parsed.get("extra_context"):
        result["extra_context"] = str(parsed["extra_context"])
    return result


def _make_handler(entry: HookEntry):
    async def _handler(
        tool_name: str, context: dict[str, Any], _state: dict[str, Any] | None
    ) -> HookResult:
        # Honor the matcher against the *target* of the event.  For
        # pre/post_tool_exec the matcher applies to the tool name; for
        # other events the matcher is not meaningful and we let it
        # match everything.
        if (
            entry.event_type in ("pre_tool_exec", "post_tool_exec")
            and not entry.matches(tool_name)
        ):
            return {}
        return await _run_command_entry(entry, context)

    _handler.__name__ = f"_hook_{entry.event_type}_{id(entry)}"
    return _handler


def _register_entries(
    hook_system: EncreHookSystem, entries: list[HookEntry]
) -> int:
    registered = 0
    for entry in entries:
        metadata = {
            "source_path": entry.source_path,
            "matcher": entry.matcher.pattern,
            "command": entry.command,
            "hook_type": entry.hook_type,
            "timeout_ms": entry.timeout_ms,
        }
        try:
            hook_system.register_handler(
                entry.event_type, _make_handler(entry), metadata=metadata,
            )
            registered += 1
        except ValueError as e:
            logger.warning(
                "Skipping invalid hook entry in %s: %s", entry.source_path, e,
            )
    return registered


def load_hooks_file(
    hook_system: EncreHookSystem, path: str
) -> int:
    """Load hook entries from a single YAML file and register them.

    Returns the number of hook handlers registered.  Missing or
    malformed files are logged and treated as no-ops so the agent
    always starts even when the user's config is incomplete.
    """
    if not path or not os.path.isfile(path):
        return 0
    raw = _load_yaml(path)
    if raw is None:
        return 0
    entries = _extract_entries(raw, path)
    return _register_entries(hook_system, entries)


# Project-level hook file locations scanned under the active workspace.
# The ``.claude/settings.json`` shape is supported too -- it shares the
# ``hooks`` envelope with our native YAML, only the syntax differs.
_PROJECT_HOOK_CANDIDATE_FILES: tuple[tuple[str, str], ...] = (
    (".encre/hooks.yaml", "yaml"),
    (".encre/hooks.yml", "yaml"),
    (".claude/settings.json", "json"),
)


def _load_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to parse JSON hook file %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        return None
    return data


def load_project_hooks(
    hook_system: EncreHookSystem, workspace_path: str
) -> int:
    """Load every project-level hook file under *workspace_path*.

    Returns the total number of hook handlers registered.  Files that
    do not exist are silently skipped; malformed files are logged
    once via :func:`load_hooks_file`.
    """
    if not workspace_path:
        return 0
    total = 0
    for rel, fmt in _PROJECT_HOOK_CANDIDATE_FILES:
        full = os.path.join(workspace_path, rel)
        if not os.path.isfile(full):
            continue
        if fmt == "yaml":
            total += load_hooks_file(hook_system, full)
        else:
            raw = _load_json(full)
            if raw is None:
                continue
            entries = _extract_entries(raw, full)
            total += _register_entries(hook_system, entries)
    return total
