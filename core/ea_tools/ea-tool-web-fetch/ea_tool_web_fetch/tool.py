#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
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

"""EA web_fetch tool.

Fetches a single URL and returns its content as plain text, markdown, or
raw HTML.  Uses the Exa crawler (with JS rendering) as the primary path and
an SSRF-guarded httpx client as the fallback.  Moved out of the harness
builtin set: the native base now only carries the primitives every agent
needs offline (terminal, file read/write/edit, web search); this tool lives
in the deferred plugin zone and is discovered via ``manage(discover)``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx
from encre.search.manager import EncreSearchManager
from encre.ssrf import EncreSSRFGuard
from encre.tools.base import build_tool
from encre.tools.tool_ui import ui_dict
from markdownify import markdownify as md

_MAX_BODY = 5 * 1024 * 1024
_MAX_REDIRECTS = 5

# Module-level guard: DNS cache survives across fetches.
_ssrf_guard = EncreSSRFGuard()


async def _httpx_fetch(url: str, fmt: str, timeout: int) -> str:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False
        ) as client:
            # Manual redirect following: every hop (initial URL and each
            # redirect target) is re-validated by the SSRF guard before the
            # request is sent.  httpx's built-in follow_redirects=True would
            # bypass validation for redirect targets.
            current_url = url
            for _ in range(_MAX_REDIRECTS + 1):
                if not _ssrf_guard.validate_url(current_url):
                    return f"Error: URL blocked by SSRF guard: {current_url}"
                resp = await client.send(
                    client.build_request("GET", current_url), stream=True
                )
                if not resp.is_redirect:
                    break
                next_url = str(urljoin(str(resp.url), resp.headers.get("location", "")))
                await resp.aclose()
                current_url = next_url
            else:
                return f"Error: Too many redirects fetching {url}"
            try:
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "")
                is_html = "text/html" in ct or "application/xhtml+xml" in ct
                size, chunks = 0, []
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_BODY:
                        chunks.append(b"... (truncated)")
                        break
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
                    lines = [ln.strip() for ln in soup.get_text(separator="\n", strip=True).split("\n") if ln.strip()]
                    return "\n".join(lines[:500])
                return text
            finally:
                await resp.aclose()
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
    # Tool-entry validation: refuse internal/metadata targets before any
    # outbound request (crawler or httpx) is made.
    if not _ssrf_guard.validate_url(url):
        return f"Error: URL blocked by SSRF guard: {url}"
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
    ui=ui_dict(icon="globe", label="Web Fetch", card="item", body="web-fetched", keys=("url", "uri")).as_dict(),
    intents=["general", "research"], category="web", semantic_type="network",
    is_concurrency_safe=lambda _: True, is_readonly=True,
)
