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

"""Keyboard shortcut configuration.

Follows the same pattern as ``settings_manager.py``:
- Hardcoded defaults in :func:`default_keybinds`.
- Encrypted persistence at ``<data_dir>/keybinds.json``.
- First use auto-creates the encrypted file from defaults.
- Future: users can edit the file to customize shortcuts.
"""

import json
import logging
from pathlib import Path
from typing import Any

from encre.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

_KEYBINDS_FILE = "keybinds.json"


def _config_path() -> Path:
    from encre.config import get_data_dir
    return get_data_dir() / _KEYBINDS_FILE


def default_keybinds() -> dict[str, Any]:
    """Return the built-in default keybinding set."""
    return {
        "version": 1,
        "keybinds": [
            # ── Application ──────────────────────────────────────────
            {
                "id": "quit",
                "keys": ["ctrlcmd+q"],
                "category": "application",
                "description": "Quit application",
            },
            {
                "id": "devtools",
                "keys": ["ctrlcmd+shift+i"],
                "category": "application",
                "description": "Toggle developer tools",
            },
            {
                "id": "reload",
                "keys": ["ctrlcmd+r"],
                "category": "application",
                "description": "Refresh sessions / workspace list",
            },
            {
                "id": "fullscreen",
                "keys": ["f11"],
                "category": "application",
                "description": "Toggle fullscreen",
            },

            # ── Session Management ───────────────────────────────────
            {
                "id": "new_session",
                "keys": ["ctrlcmd+l"],
                "category": "session",
                "description": "New session",
            },
            {
                "id": "new_temp_chat",
                "keys": ["ctrlcmd+shift+n"],
                "category": "session",
                "description": "New temporary chat",
            },
            {
                "id": "next_session",
                "keys": ["ctrlcmd+tab"],
                "category": "session",
                "description": "Next session",
            },
            {
                "id": "prev_session",
                "keys": ["ctrlcmd+shift+tab"],
                "category": "session",
                "description": "Previous session",
            },
            {
                "id": "delete_session",
                "keys": ["ctrlcmd+shift+d"],
                "category": "session",
                "description": "Delete current session",
            },
            {
                "id": "rename_session",
                "keys": ["ctrlcmd+shift+e"],
                "category": "session",
                "description": "Rename current session",
            },
            {
                "id": "export_session",
                "keys": ["ctrlcmd+shift+x"],
                "category": "session",
                "description": "Export session as Markdown",
            },

            # ── Message Operations ───────────────────────────────────
            {
                "id": "edit_last_message",
                "keys": ["ctrlcmd+up"],
                "category": "messages",
                "description": "Edit last message",
            },
            {
                "id": "copy_last_response",
                "keys": ["ctrlcmd+shift+c"],
                "category": "messages",
                "description": "Copy last AI response",
            },
            {
                "id": "undo_message",
                "keys": ["ctrlcmd+z"],
                "category": "messages",
                "description": "Undo / retract last message",
            },
            {
                "id": "delete_message",
                "keys": ["ctrlcmd+shift+backspace"],
                "category": "messages",
                "description": "Delete selected message",
            },
            {
                "id": "retry",
                "keys": ["ctrlcmd+shift+t"],
                "category": "messages",
                "description": "Retry (normal)",
            },
            {
                "id": "retry_detailed",
                "keys": ["ctrlcmd+shift+s"],
                "category": "messages",
                "description": "Retry (detailed)",
            },
            {
                "id": "retry_concise",
                "keys": ["ctrlcmd+shift+o"],
                "category": "messages",
                "description": "Retry (concise)",
            },

            # ── Input Area ───────────────────────────────────────────
            {
                "id": "attach_file",
                "keys": ["ctrlcmd+shift+g"],
                "category": "input",
                "description": "Attach file",
            },
            {
                "id": "upload_file",
                "keys": ["ctrlcmd+u"],
                "category": "input",
                "description": "Upload file",
            },
            {
                "id": "history_prev",
                "keys": ["ctrlcmd+alt+up"],
                "category": "input",
                "description": "Previous input history",
            },
            {
                "id": "history_next",
                "keys": ["ctrlcmd+down"],
                "category": "input",
                "description": "Next input history",
            },

            # ── Mode Switching ───────────────────────────────────────
            {
                "id": "toggle_plan_mode",
                "keys": ["ctrlcmd+shift+p"],
                "category": "modes",
                "description": "Toggle Plan mode",
            },
            {
                "id": "toggle_spec_mode",
                "keys": ["ctrlcmd+shift+l"],
                "category": "modes",
                "description": "Toggle Spec mode",
            },
            {
                "id": "cancel",
                "keys": ["escape"],
                "category": "modes",
                "description": "Cancel / stop generation",
            },

            # ── Sidebar / Navigation ─────────────────────────────────
            {
                "id": "toggle_sidebar",
                "keys": ["ctrlcmd+b"],
                "category": "navigation",
                "description": "Toggle sidebar",
            },
            {
                "id": "view_chat",
                "keys": ["ctrlcmd+1"],
                "category": "navigation",
                "description": "Chat view",
            },
            {
                "id": "view_automation",
                "keys": ["ctrlcmd+4"],
                "category": "navigation",
                "description": "Automation view",
            },

            # ── Search ───────────────────────────────────────────────
            {
                "id": "search_global",
                "keys": ["ctrlcmd+k"],
                "category": "search",
                "description": "Global search",
            },
            {
                "id": "search_settings",
                "keys": ["ctrlcmd+f"],
                "category": "search",
                "description": "Search in settings",
            },

            # ── Settings ─────────────────────────────────────────────
            {
                "id": "open_settings",
                "keys": ["ctrlcmd+,"],
                "category": "settings",
                "description": "Open settings",
            },
            {
                "id": "settings_general",
                "keys": ["ctrlcmd+shift+,"],
                "category": "settings",
                "description": "Settings - General",
            },
            {
                "id": "settings_models",
                "keys": ["ctrlcmd+shift+m"],
                "category": "settings",
                "description": "Settings - Model management",
            },
            {
                "id": "settings_skills",
                "keys": ["ctrlcmd+shift+k"],
                "category": "settings",
                "description": "Settings - Skills & commands",
            },
            {
                "id": "settings_mcp",
                "keys": ["ctrlcmd+shift+h"],
                "category": "settings",
                "description": "Settings - MCP servers",
            },
            {
                "id": "settings_agent",
                "keys": ["ctrlcmd+shift+a"],
                "category": "settings",
                "description": "Settings - Agent config",
            },
            {
                "id": "settings_library",
                "keys": ["ctrlcmd+alt+i"],
                "category": "settings",
                "description": "Settings - Library",
            },
            {
                "id": "settings_assist",
                "keys": ["ctrlcmd+shift+r"],
                "category": "settings",
                "description": "Settings - Assist panels",
            },
            {
                "id": "settings_memory",
                "keys": ["ctrlcmd+shift+y"],
                "category": "settings",
                "description": "Settings - Memory & rules",
            },
            {
                "id": "settings_usage",
                "keys": ["ctrlcmd+shift+u"],
                "category": "settings",
                "description": "Settings - Usage statistics",
            },
            {
                "id": "settings_about",
                "keys": ["ctrlcmd+shift+q"],
                "category": "settings",
                "description": "Settings - About",
            },
            {
                "id": "settings_gateway",
                "keys": ["ctrlcmd+alt+g"],
                "category": "settings",
                "description": "Settings - Gateway",
            },
            {
                "id": "settings_plugins",
                "keys": ["ctrlcmd+alt+p"],
                "category": "settings",
                "description": "Settings - Plugins",
            },
            {
                "id": "settings_shortcuts",
                "keys": ["ctrlcmd+alt+k"],
                "category": "settings",
                "description": "Settings - Keyboard shortcuts",
            },
            {
                "id": "settings_storage",
                "keys": ["ctrlcmd+alt+u"],
                "category": "settings",
                "description": "Settings - Storage",
            },
            {
                "id": "settings_developer",
                "keys": ["ctrlcmd+alt+d"],
                "category": "settings",
                "description": "Settings - Developer",
            },

            # ── Session Inner Panels ─────────────────────────────────
            {
                "id": "toggle_terminal",
                "keys": ["ctrlcmd+`"],
                "category": "panels",
                "description": "Toggle terminal panel",
            },
            {
                "id": "toggle_browser",
                "keys": ["ctrlcmd+shift+b"],
                "category": "panels",
                "description": "Toggle browser panel",
            },
            {
                "id": "toggle_editor",
                "keys": ["ctrlcmd+shift+w"],
                "category": "panels",
                "description": "Toggle editor panel",
            },
            {
                "id": "toggle_review",
                "keys": ["ctrlcmd+shift+v"],
                "category": "panels",
                "description": "Toggle review panel",
            },
            {
                "id": "toggle_info",
                "keys": ["ctrlcmd+shift+j"],
                "category": "panels",
                "description": "Toggle info panel",
            },
            {
                "id": "toggle_files",
                "keys": ["ctrlcmd+shift+f"],
                "category": "panels",
                "description": "Toggle files panel",
            },
            {
                "id": "prev_tab",
                "keys": ["ctrlcmd+["],
                "category": "panels",
                "description": "Previous panel tab",
            },
            {
                "id": "next_tab",
                "keys": ["ctrlcmd+]"],
                "category": "panels",
                "description": "Next panel tab",
            },

            # ── Automation ───────────────────────────────────────────
            {
                "id": "automation_open",
                "keys": ["ctrlcmd+alt+a"],
                "category": "automation",
                "description": "Open automation panel",
            },
            {
                "id": "automation_new",
                "keys": ["ctrlcmd+alt+n"],
                "category": "automation",
                "description": "New automation task",
            },
            {
                "id": "automation_run",
                "keys": ["ctrlcmd+alt+enter"],
                "category": "automation",
                "description": "Run automation task",
            },
            {
                "id": "automation_toggle",
                "keys": ["ctrlcmd+alt+s"],
                "category": "automation",
                "description": "Toggle pause/resume",
            },
            {
                "id": "automation_history",
                "keys": ["ctrlcmd+alt+h"],
                "category": "automation",
                "description": "View automation history",
            },

            # ── Workspace ────────────────────────────────────────────
            {
                "id": "workspace_open",
                "keys": ["ctrlcmd+alt+o"],
                "category": "workspace",
                "description": "Open workspace",
            },
            {
                "id": "workspace_close",
                "keys": ["ctrlcmd+alt+c"],
                "category": "workspace",
                "description": "Close workspace",
            },

            # ── Notifications ────────────────────────────────────────
            {
                "id": "notifications_open",
                "keys": ["ctrlcmd+alt+m"],
                "category": "notifications",
                "description": "Open notifications",
            },
            {
                "id": "notifications_clear",
                "keys": ["ctrlcmd+alt+x"],
                "category": "notifications",
                "description": "Clear all notifications",
            },

            # ── Theme & Language ─────────────────────────────────────
            {
                "id": "toggle_theme",
                "keys": ["ctrlcmd+alt+t"],
                "category": "appearance",
                "description": "Cycle theme (dark/light/system)",
            },
            {
                "id": "toggle_language",
                "keys": ["ctrlcmd+alt+l"],
                "category": "appearance",
                "description": "Cycle UI language",
            },
            {
                "id": "show_shortcuts",
                "keys": ["ctrlcmd+/"],
                "category": "general",
                "description": "Show keyboard shortcuts reference",
            },

        ],
    }


def load_keybinds() -> dict[str, Any]:
    """Load keybinds from the encrypted config file.

    On first use (file does not exist), the hardcoded defaults are
    written to disk and returned.
    """
    path = _config_path()
    if path.is_file():
        try:
            raw = path.read_text(encoding="utf-8")
            binds = json.loads(decrypt(raw))
            # Migration: drop entries whose id is no longer in the default
            # set.  The persisted file outlives removed features — e.g. an
            # old default set carried workspace_reindex / settings_index /
            # settings_rules after their underlying features were deleted.
            # Pruning by id keeps every user's rebinding of live ids intact.
            entries = binds.get("keybinds", [])
            default_ids = {b["id"] for b in default_keybinds()["keybinds"]}
            kept = [e for e in entries if isinstance(e, dict) and e.get("id") in default_ids]
            if len(kept) != len(entries):
                logger.info(
                    "[keybinds] pruning %d stale entries (feature removed): %s",
                    len(entries) - len(kept),
                    sorted({e.get("id") for e in entries if e not in kept}),
                )
                binds["keybinds"] = kept
                save_keybinds(binds)
            logger.info("[keybinds] loaded %d entries from %s", len(binds.get("keybinds", [])), path)
            return binds
        except Exception as exc:
            logger.warning("[keybinds] failed to load from %s: %s — falling back to defaults", path, exc)

    binds = default_keybinds()
    save_keybinds(binds)
    logger.info("[keybinds] created default keybinds at %s", path)
    return binds


def save_keybinds(binds: dict[str, Any]) -> None:
    """Encrypt and persist keybinds to ``<data_dir>/keybinds.json``.

    Verifies the written data by reading it back immediately.
    """
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(binds, ensure_ascii=False, indent=2)
    encrypted = encrypt(raw)
    path.write_text(encrypted, encoding="utf-8")

    # Readback verification — detect corruption early
    try:
        stored = path.read_text(encoding="utf-8")
        decrypted = decrypt(stored)
        verified = json.loads(decrypted)
        if verified.get("version") != binds.get("version"):
            logger.warning("[keybinds] save verification: version mismatch")
    except Exception as exc:
        logger.error("[keybinds] save verification FAILED: %s — re-saving", exc)
        # Retry once
        path.write_text(encrypted, encoding="utf-8")
        try:
            stored = path.read_text(encoding="utf-8")
            decrypt(stored)
        except Exception as exc2:
            logger.error("[keybinds] retry also failed: %s", exc2)


__all__ = ["default_keybinds", "load_keybinds", "save_keybinds"]
