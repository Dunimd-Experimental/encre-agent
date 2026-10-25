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

"""Build the Slack app manifest for the Encre gateway.

Slack apps are configured declaratively: scopes, event subscriptions, slash
commands, and the assistant view all live in a single JSON manifest that the
user pastes into (or uploads to) the Slack app dashboard.  Generating that JSON
from the same command registry the gateway actually dispatches means the
manifest cannot drift from the running bot -- every slash command the adapter
answers appears in the manifest, and every scope the adapter needs is
requested.

The manifest targets Socket Mode (no public request URL), which is how the
gateway connects; a request-URL flow can be requested via the keyword
arguments for completeness.
"""

from typing import Any, Iterable

__all__ = ["_build_full_manifest", "build_slack_manifest"]

# Bot scopes the adapter relies on.  Grouped by feature in comments so a scope
# regression is easy to spot against the adapter's event handlers.
_DEFAULT_BOT_SCOPES = [
    # Mentions and assistant threads.
    "app_mentions:read",
    "assistant:write",
    # Channel / group / DM messaging.
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "im:write",
    # Group DMs (multi-person IMs) -- required for mpim events and history.
    "mpim:history",
    "mpim:read",
    "mpim:write",
    # Sending and reacting.
    "chat:write",
    "chat:write.public",
    "commands",
    "reactions:read",
    "reactions:write",
    # Files shared into the bot.
    "files:read",
    # User directory lookups.
    "users:read",
    "users:read.email",
]

# Events the gateway subscribes to.  Mirror of the ``@app.event(...)``
# registrations in the Slack adapter.
_DEFAULT_BOT_EVENTS = [
    "app_mention",
    "message.channels",
    "message.groups",
    "message.im",
    "message.mpim",
    "app_home_opened",
    "app_context_changed",
    "file_shared",
    "file_created",
    "file_change",
    "reaction_added",
    "reaction_removed",
    "assistant_thread_started",
    "assistant_thread_context_changed",
]

_SUGGESTED_PROMPTS = [
    {"title": "Plan a task", "message": "Plan how to tackle the following task: "},
    {"title": "Summarise a thread", "message": "Summarise the key points of this thread."},
]

# Slack enforces these limits on manifest string fields.
_MAX_COMMAND_NAME = 32
_MAX_TEXT = 2000


def _slash_commands() -> list[dict[str, str]]:
    """Build the manifest ``slash_commands`` entries from the command registry.

    Always includes the legacy ``/encre`` escape hatch so the bot is reachable
    even if the registry import fails or a command name is filtered out.
    """
    entries: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(raw_name: str, description: str) -> None:
        name = (raw_name or "").lstrip("/").strip().lower()
        name = name[:_MAX_COMMAND_NAME]
        if not name or name in seen:
            return
        seen.add(name)
        entries.append(
            {
                "command": f"/{name}",
                "description": (description or f"/{name}")[:_MAX_TEXT],
                "usage_hint": "[args]",
                "should_escape": False,
            }
        )

    try:
        from encre.commands import slack_native_slashes

        for name, description, _extra in slack_native_slashes():
            _add(name, description)
    except Exception:
        # ``encre.commands`` pulls in the skill registry; when that is
        # unavailable (or the helper is missing) fall back to the slash
        # registry so the manifest still lists every builtin command.
        try:
            from encre.slash_commands import BUILTIN_SLASH_COMMANDS

            for command in BUILTIN_SLASH_COMMANDS:
                _add(getattr(command, "name", ""), getattr(command, "description", ""))
        except Exception:
            pass

    # Guarantee the primary entry point exists regardless of registry content.
    _add("encre", "Talk to Encre Agent")
    return entries


def _build_full_manifest(
    bot_name: str = "Encre",
    bot_description: str = "Your Encre agent on Slack",
    *,
    request_url: str = "",
    interactivity_url: str = "",
    socket_mode_enabled: bool = True,
    extra_bot_scopes: Iterable[str] = (),
    extra_events: Iterable[str] = (),
    extra_slash_commands: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Assemble the Slack app manifest.

    Args:
        bot_name: Display name of the bot user.
        bot_description: Short description shown in Slack surfaces.
        request_url: Event-subscription request URL (empty for Socket Mode).
        interactivity_url: Interactivity request URL (empty for Socket Mode).
        socket_mode_enabled: Whether the app uses Socket Mode.
        extra_bot_scopes: Additional bot scopes to merge in.
        extra_events: Additional bot events to merge in.
        extra_slash_commands: Additional ``(name, description)`` slash commands.

    Returns:
        A JSON-serialisable manifest dict for ``slack-manifest.json``.
    """
    scopes = list(dict.fromkeys([*_DEFAULT_BOT_SCOPES, *extra_bot_scopes]))
    events = list(dict.fromkeys([*_DEFAULT_BOT_EVENTS, *extra_events]))

    commands = _slash_commands()
    if extra_slash_commands:
        seen = {entry["command"] for entry in commands}
        for name, description in extra_slash_commands:
            normalized = f"/{(name or '').lstrip('/').strip().lower()}"[:_MAX_COMMAND_NAME]
            if normalized != "/" and normalized not in seen:
                seen.add(normalized)
                commands.append(
                    {
                        "command": normalized,
                        "description": (description or normalized)[:_MAX_TEXT],
                        "usage_hint": "[args]",
                        "should_escape": False,
                    }
                )

    features: dict[str, Any] = {
        "bot_user": {
            "display_name": bot_name,
            "always_online": True,
        },
        "app_home": {
            "home_tab_enabled": True,
            "messages_tab_enabled": True,
            "messages_tab_read_only_enabled": False,
        },
        "assistant_view": {
            "assistant_description": bot_description,
            "suggested_prompts": list(_SUGGESTED_PROMPTS),
        },
        "slash_commands": commands,
    }

    interactivity: dict[str, Any] = {"is_enabled": True}
    if interactivity_url:
        interactivity["request_url"] = interactivity_url

    return {
        "display_information": {
            "name": bot_name,
            "description": bot_description,
            "background_color": "#1a1a2e",
        },
        "features": features,
        "oauth_config": {
            "scopes": {
                "bot": scopes,
            }
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": events,
                **({"request_url": request_url} if request_url else {}),
            },
            "interactivity": interactivity,
            "org_deploy_enabled": False,
            "socket_mode_enabled": socket_mode_enabled,
            "token_rotation_enabled": False,
        },
    }


# Public alias for callers that prefer a descriptive name.
build_slack_manifest = _build_full_manifest
