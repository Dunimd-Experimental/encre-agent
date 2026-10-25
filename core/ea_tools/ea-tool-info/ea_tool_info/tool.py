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

"""Module: builtin/info.py

Info-card tool for the Encre tool system.

Provides a generic rich-content container that lets the model render
self-contained HTML/CSS/JS snippets inside the chat timeline.  The payload
is treated as opaque content and passed through to the frontend, which
sandboxes it in an iframe for safety.
"""
import json
from typing import Any

from encre.tools.base import build_tool


async def _info_execute(**kwargs: Any) -> str:
    """Validate and echo the info-card payload back to the frontend.

    The frontend is responsible for rendering the HTML/CSS/JS inside a
    sandboxed iframe.  This executor only performs lightweight validation
    and normalization so the model receives clear feedback on errors.
    """
    content = kwargs.get("content", "")
    if not isinstance(content, str):
        return "Error: 'content' must be a string containing HTML/CSS/JS."

    title = kwargs.get("title", "")

    # Validate optional media array (image/video items)
    media = kwargs.get("media", [])
    if not isinstance(media, list):
        media = []
    for item in media:
        if not isinstance(item, dict) or "type" not in item or "src" not in item:
            return "Error: Each media item must be an object with 'type' and 'src' fields."
        if item["type"] not in {"image", "video"}:
            return "Error: media.type must be 'image' or 'video'."

    # Detect whether the HTML payload looks like a complete document. Fragments
    # and bare <style> blocks are still rendered, but the model is encouraged to
    # provide a full <html>/<body> document for best results.
    is_complete_html = False
    lowered = content.lower().strip()
    is_complete_html = (
        lowered.startswith("<!doctype")
        or lowered.startswith("<html")
        or "<body" in lowered
    )

    display = kwargs.get("display", "base")
    if display not in {"base", "code", "split"}:
        display = "base"

    payload = {
        "display": display,
        "title": title if isinstance(title, str) else "",
        "content": content,
        "is_complete_html": is_complete_html,
    }
    if media:
        payload["media"] = media
    return json.dumps(payload, ensure_ascii=False)


info_tool = build_tool(
    name="info",
    description=(
        "Rich-card visualization tool. Renders a self-contained HTML/CSS/JS "
        "document inside a sandboxed iframe in the chat timeline, or a native "
        "media gallery for image/video display. Use this when the user asks "
        "for a card, dashboard, chart, or any rich visual layout that goes "
        "beyond plain markdown. "
        "Always specify the 'display' parameter. Use display='base' (default) "
        "to render the HTML document; display='code' to show the raw source "
        "without rendering. A complete document including <html>, <head>, "
        "<body> and all required CSS is strongly preferred -- fragments or a "
        "bare <style> block will still be rendered but may look broken. "
        "To display images or videos natively, pass a 'media' array with "
        "{type, src} objects. "
        "TIP: Keep content self-contained (inline CSS/JS, no external "
        "fetches) so the card renders correctly inside the sandboxed iframe. "
        "AVOID: Loading external scripts that require network access -- they "
        "may be blocked by the sandbox."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "display": {
                "type": "string",
                "enum": ["base", "code", "split"],
                "description": "Display mode (required). 'base' renders the HTML/CSS/JS card (default); 'code' shows the source; 'split' is reserved for future use.",
            },
            "title": {
                "type": "string",
                "description": "Optional card title shown above the rendered content.",
            },
            "content": {
                "type": "string",
                "description": (
                    "Self-contained HTML/CSS/JS payload (required). A complete "
                    "document including <html>, <head>, <body> is strongly "
                    "preferred; fragments or a bare <style> block will still "
                    "render but may look broken."
                ),
            },
            "media": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["image", "video"], "description": "Media type (required)."},
                        "src": {"type": "string", "description": "File path or URL to the media resource (required)."},
                        "controls": {"type": "boolean", "description": "For video: show controls (pause/seek/volume). When true, disables autoplay (optional)."},
                    },
                    "required": ["type", "src"],
                },
                "description": "Optional array of media items (images/videos). When present, the card renders a native media gallery inline using the MediaViewer component.",
            },
        },
        "required": ["display", "content"],
    },
    execute=_info_execute,
    intents=["general", "communication", "data", "research"],
    category="communication",
    triggers=[
        "card", "info card",
        "dashboard", "chart", "visualization",
    ],
    semantic_type="read",
    is_concurrency_safe=lambda _: True,
    is_readonly=True,
)
