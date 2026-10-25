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

"""Notification / webhook tool.

This module implements the ``notify`` tool that the Encre Agent dispatches
whenever the model wants to push a message somewhere outside the current chat
turn. Four delivery backends are supported and selected by the ``action``
field on the tool input:

* ``webhook`` -- arbitrary HTTP request (POST/PUT/PATCH) with a JSON body.
* ``slack``   -- Slack incoming-webhook, with optional channel / emoji /
                 username overrides.
* ``discord`` -- Discord webhook, payload restricted to a text message and
                 a display name.
* ``desktop`` -- native OS toast: MessageBox on Windows, ``osascript``
                 notification on macOS, ``notify-send`` on Linux.

Design notes
------------

All four backends ultimately block on network or subprocess I/O, so each
branch runs its blocking call inside the default ``asyncio`` executor via
``loop.run_in_executor``. This keeps the tool's coroutine ``await``-able
without ever blocking the event loop.

The webhook body is intentionally echoed back to the caller (truncated to
2000 characters) so the model can visually confirm the destination accepted
the payload without opening a browser. Slack and Discord return a compact
success/failure string instead because their wire protocol only exposes a
status code and an optional body on error.

Third-party imports (``plyer``, ``subprocess``, ``ctypes``) are deferred
into the inner ``_notify`` closure so that installing this plugin on a
system that has none of them still allows ``import ea_tool_notify.tool``
to succeed -- failures surface only when the ``desktop`` action is
actually invoked.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

from encre.tools.base import build_tool


async def _notify_execute(**kwargs: Any) -> str:
    """Dispatch a notification to the backend named by ``kwargs["action"]``.

    The function is intentionally written as one flat dispatch rather than
    delegating to per-backend helpers: each branch is short, uses a
    different subset of ``kwargs``, and shares only the surrounding
    ``run_in_executor`` shape. Splitting them out would add indirection
    without shortening any of the branches.

    All network I/O is performed inside a nested ``_send*`` closure so
    that the outer coroutine stays ``async`` while the underlying
    ``urllib.request.urlopen`` call (which is blocking) runs on the
    default thread-pool executor. This avoids starving other coroutines
    on the same event loop during a slow webhook call.

    Args:
        kwargs: Tool input keyed by the JSON schema declared in
            ``notify_tool.input_schema`` below. See the schema for the
            full set of fields. The ``action`` field is required; every
            other field is optional and interpreted only by the matching
            action branch.

    Returns:
        A human-readable status string suitable for surfacing directly in
        the model's tool-result transcript. Errors are converted to
        ``"..."failed..."`` strings rather than raised so a misconfigured
        webhook never kills an otherwise successful agent turn.
    """
    action = kwargs.get("action", "")

    if action == "webhook":
        url = kwargs.get("url", "")
        method = kwargs.get("method", "POST")
        headers = kwargs.get("headers", {})
        body = kwargs.get("body", "")
        content_type = kwargs.get("content_type", "application/json")

        if not url:
            return "Missing required field: url"

        # Copy the header dict so a caller-supplied default (e.g. a
        # shared ``{}`` object reused across calls) is not mutated.
        headers = dict(headers) if isinstance(headers, dict) else {}
        if content_type and "Content-Type" not in headers:
            headers["Content-Type"] = content_type

        data = body.encode("utf-8") if body else None

        loop = asyncio.get_event_loop()

        def _send() -> str:
            """Blocking HTTP dispatch used by the ``webhook`` action.

            The response body is truncated to 2000 characters because a
            large body would dominate the tool result and, more
            importantly, could contain sensitive tokens echoed back by a
            misconfigured endpoint. The truncate window is comfortably
            larger than any real status payload and small enough to keep
            the transcript readable.
            """
            try:
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers=headers,
                    method=method,
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    response_body = resp.read().decode("utf-8", errors="replace")[:2000]
                    return f"Webhook sent. Status: {resp.status} {resp.reason}\nResponse: {response_body}"
            except urllib.error.HTTPError as e:
                # HTTPError carries the response body on ``e.read()``;
                # read-and-truncate so 4xx/5xx diagnostics still surface.
                return f"Webhook failed. Status: {e.code} {e.reason}\nBody: {e.read().decode('utf-8', errors='replace')[:1000]}"
            except Exception as e:
                return f"Webhook failed: {e}"

        return await loop.run_in_executor(None, _send)

    elif action == "slack":
        webhook_url = kwargs.get("webhook_url", "")
        message = kwargs.get("message", "")
        channel = kwargs.get("channel", "")
        username = kwargs.get("username", "Encre Bot")
        icon_emoji = kwargs.get("icon_emoji", "")

        if not webhook_url or not message:
            return "Missing required fields: webhook_url, message"

        # Slack only accepts a non-empty ``channel`` when it differs from
        # the webhook's default channel, so we gate the field on truthiness.
        payload: dict[str, Any] = {"text": message, "username": username}
        if channel:
            payload["channel"] = channel
        if icon_emoji:
            payload["icon_emoji"] = icon_emoji

        body = json.dumps(payload)
        headers = {"Content-Type": "application/json"}

        loop = asyncio.get_event_loop()

        def _send_slack() -> str:
            """Blocking Slack dispatch.

            Slack returns a bare ``ok`` body on success, so we only
            look at the HTTP status code rather than parsing the
            response body.
            """
            try:
                req = urllib.request.Request(
                    webhook_url,
                    data=body.encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if resp.status == 200:
                        return "Slack notification sent successfully"
                    return f"Slack webhook returned status {resp.status}"
            except urllib.error.HTTPError as e:
                return f"Slack webhook failed: {e.code} {e.reason}"
            except Exception as e:
                return f"Slack webhook failed: {e}"

        return await loop.run_in_executor(None, _send_slack)

    elif action == "discord":
        webhook_url = kwargs.get("webhook_url", "")
        message = kwargs.get("message", "")
        username = kwargs.get("username", "Encre Bot")

        if not webhook_url or not message:
            return "Missing required fields: webhook_url, message"

        payload = {"content": message, "username": username}
        body = json.dumps(payload)
        headers = {"Content-Type": "application/json"}

        loop = asyncio.get_event_loop()

        def _send_discord() -> str:
            """Blocking Discord dispatch.

            Discord's webhook API returns a small JSON payload on
            success; we only surface the status code to keep the tool
            result terse.
            """
            try:
                req = urllib.request.Request(
                    webhook_url,
                    data=body.encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return f"Discord notification sent (status {resp.status})"
            except urllib.error.HTTPError as e:
                # Include a truncated response body because Discord's
                # error JSON often carries a validation error the caller
                # needs to see.
                body_text = e.read().decode("utf-8", errors="replace")[:500]
                return f"Discord webhook failed: {e.code} {e.reason} - {body_text}"
            except Exception as e:
                return f"Discord webhook failed: {e}"

        return await loop.run_in_executor(None, _send_discord)

    elif action == "desktop":
        title = kwargs.get("title", "Encre Notification")
        message = kwargs.get("message", "")

        if not message:
            return "Missing required field: message"

        loop = asyncio.get_event_loop()

        def _notify() -> str:
            """Blocking desktop toast dispatch, dispatched per-OS.

            The three OS branches are kept inline (rather than extracted
            to platform-specific modules) because the fallback chain --
            try ``plyer`` first on Windows, fall back to Win32 MessageBox
            -- has to live in one place so it reads linearly. Third-party
            imports are placed inside the ``try`` block on purpose: if
            ``plyer`` is missing we want the import error caught by the
            same ``except ImportError`` rather than propagating out of
            the ``run_in_executor`` wrapper.
            """
            try:
                if os.name == "nt":
                    try:
                        from plyer import notification
                        notification.notify(title=title, message=message, timeout=5)
                        return f"Desktop notification sent: {title}"
                    except ImportError:
                        # Last-resort Win32 fallback: a plain MessageBoxW
                        # is always available on Windows but lacks the
                        # balloon semantics of a real toast.
                        import ctypes
                        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x1000)
                        return f"Desktop notification (MessageBox): {title}"
                elif os.uname().sysname == "Darwin":
                    # osascript is the canonical macOS notification path
                    # that does not require third-party tooling.
                    subprocess_args = ["osascript", "-e",
                                       f'display notification "{message}" with title "{title}"']
                    import subprocess
                    subprocess.run(subprocess_args, capture_output=True, timeout=10)
                    return f"macOS notification sent: {title}"
                else:
                    try:
                        import subprocess
                        subprocess.run(
                            ["notify-send", title, message],
                            capture_output=True, timeout=10,
                        )
                        return f"Linux notification sent: {title}"
                    except FileNotFoundError:
                        return "notify-send not found. Install libnotify-bin."
            except Exception as e:
                return f"Desktop notification failed: {e}"

        return await loop.run_in_executor(None, _notify)

    return f"Unknown action: {action}. Supported: webhook, slack, discord, desktop"


notify_tool = build_tool(
    name="notify",
    description=(
        "Send a notification to the user or an external channel via webhook, "
        "Slack, Discord, or a native desktop toast.\n\n"
        "WHEN to use: surface progress on long-running work to the user, "
        "alert a monitoring channel when a job finishes or fails, or ping a "
        "chat webhook as part of an automation.\n"
        "WHEN NOT to use: to ask the user a question that needs an answer use "
        "the question tool; for in-conversation status updates just reply in "
        "the chat.\n"
        "TIPS: for Slack/Discord pass the incoming-webhook URL in "
        "webhook_url; keep messages concise (chat clients truncate long "
        "text); desktop toasts work cross-platform but require OS support.\n"
        "PITFALLS: webhooks that need auth must include credentials in the "
        "headers or URL; desktop notifications on Linux need libnotify-bin "
        "installed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["webhook", "slack", "discord", "desktop"],
                "description": "Notification channel: 'webhook' (arbitrary HTTP POST), 'slack' (Slack incoming webhook), 'discord' (Discord webhook), or 'desktop' (native OS toast/message box).",
            },
            "url": {"type": "string", "description": "Target URL for the 'webhook' action (the endpoint that will receive the POST/PUT request)."},
            "method": {"type": "string", "description": "HTTP method for the 'webhook' action (default POST). Use PUT/PATCH when the target endpoint expects it."},
            "headers": {"type": "object", "description": "Custom HTTP headers for the 'webhook' action (e.g. Authorization, X-Api-Key). Content-Type is added automatically if not provided."},
            "body": {"type": "string", "description": "Raw request body for the 'webhook' action. For JSON endpoints, pass a JSON-serialized string."},
            "content_type": {"type": "string", "description": "Content-Type header value for the 'webhook' action (default application/json). Override for form-encoded or text payloads."},
            "webhook_url": {"type": "string", "description": "Incoming webhook URL for the 'slack' or 'discord' action (from the Slack/Discord app config)."},
            "message": {"type": "string", "description": "Notification message text. Required for every action. Keep it concise -- chat clients truncate long messages."},
            "channel": {"type": "string", "description": "Slack channel override for the 'slack' action (e.g. '#alerts' or a user ID). Optional; the webhook's default channel is used if omitted."},
            "username": {"type": "string", "description": "Bot display name for the 'slack' or 'discord' action (default 'Encre Bot')."},
            "icon_emoji": {"type": "string", "description": "Slack emoji for the bot avatar, e.g. ':robot_face:' (for the 'slack' action)."},
            "title": {"type": "string", "description": "Title shown in the desktop notification (for the 'desktop' action, default 'Encre Notification')."},
        },
        "required": ["action", "message"],
    },
    execute=_notify_execute,
    intents=["general", "system"],
    category="communication",
    semantic_type="network",
    cost_level="low",
    retryability="auto",
    # Webhook sends and desktop toasts never touch local filesystem state,
    # so concurrent invocations are safe to interleave.
    is_concurrency_safe=lambda _: True,
    # Marked destructive because an agent calling this tool can post to an
    # external channel on the user's behalf; the tool registry uses this
    # flag to prompt for confirmation in destructive-aware UIs.
    is_destructive=True,
)
