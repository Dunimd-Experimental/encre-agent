#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 漏 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

from typing import Any

import httpx
from markdownify import markdownify as md

from encre.search.manager import EncreSearchManager
from encre.tools.base import build_tool

_MAX_BODY = 5 * 1024 * 1024


async def _httpx_fetch(url: str, fmt: str, timeout: int) -> str:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0),
                                     follow_redirects=True, max_redirects=5) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "")
                is_html = "text/html" in ct or "application/xhtml+xml" in ct
                size, chunks = 0, []
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_BODY:
                        chunks.append(b"... (truncated)"); break
                    chunks.append(chunk)
                text = b"".join(chunks).decode("utf-8", errors="replace")[:50000]
                if fmt == "html":
                    return text
                if is_html:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(text, "lxml")
                    for tag in soup(["script", "style", "nav", "footer", "header"]):
                        tag.decompose()
                    if fmt == "markdown":
                        return md(str(soup), heading_style="ATX")[:50000]
                    lines = [l.strip() for l in soup.get_text(separator="\n", strip=True).split("\n") if l.strip()]
                    return "\n".join(lines[:500])
                return text
    except httpx.TimeoutException:
        return f"Error: Request timed out fetching {url}"
    except httpx.HTTPStatusError as e:
        return f"Error HTTP {e.response.status_code}: {url}"
    except Exception as e:
        return f"Error fetching {url}: {e}"


async def _web_fetch_execute(**kwargs: Any) -> str:
    url = kwargs.get("url", "")
    fmt = (kwargs.get("format") or "text").strip().lower()
    try:
        timeout = min(max(5, int(kwargs.get("timeout", 30))), 120)
    except (TypeError, ValueError):
        timeout = 30
    if not url:
        return "Error: No URL provided."
    if fmt == "html":
        return await _httpx_fetch(url, "html", timeout)
    manager = EncreSearchManager()
    body = await manager.fetch(url)
    if body:
        return body
    return await _httpx_fetch(url, fmt, timeout)


web_fetch_tool = build_tool(
    name="web_fetch",
    description=(
        "Fetch a single URL and return its content as text or markdown. "
        "Uses Exa crawler (JS rendering) with httpx fallback. "
        "Use after web_search finds a URL; use web_fetch for known URLs."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute URL including scheme (required)."},
            "format": {"type": "string", "enum": ["text", "markdown", "html"],
                       "description": "Output format (default: text)."},
            "timeout": {"type": "integer", "description": "httpx fallback timeout seconds (default 30)."},
        },
        "required": ["url"],
    },
    execute=_web_fetch_execute,
    intents=["general", "research"], category="web", semantic_type="network",
    is_concurrency_safe=lambda _: True, is_readonly=True,
)
