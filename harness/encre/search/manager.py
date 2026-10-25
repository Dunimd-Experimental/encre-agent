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

from encre.logging_config import get_logger

"""Hidden MCP-powered search engine.

Replaces the original DuckDuckGo backend with an MCP-based search service.
The MCP server URL is embedded in this module and auto-encrypted to disk on
first use, so users -- and the model -- cannot discover the endpoint address.

Encrypted config on disk: ``<data_dir>/dsp_cache.bin``

API key
-------
Authenticates with an embedded Exa API key (free tier). The anonymous MCP
endpoint rate-limits hard after a few calls; the embedded key lifts that. It
is injected as an ``Authorization: Bearer`` header at connect time.
"""

import asyncio
import json
import logging
import pathlib
import re
from typing import Any

from encre.crypto import decrypt, encrypt
from encre.tools.mcp import HttpTransport, MCPClient

logger = logging.getLogger("encre.search")

# ── MCP search server identity ─────────────────────────────────────────
# Hardcoded here; auto-encrypted to a machine-bound file on first use so
# the endpoint never appears on disk in plaintext.

_MCP_SEARCH_URL = "https://mcp.exa.ai/mcp"
_MCP_SEARCH_HEADERS: dict[str, str] = {}
_MCP_SEARCH_TIMEOUT: float = 60.0

# Obfuscated filename -- looks like a generic DSP/embedding cache
_MCP_SEARCH_CONFIG_FILE = "dsp_cache.bin"

# Personal Exa API key (free tier). Hardcoded by design so search works out
# of the box; the free quota is enough for development and a key leak is
# harmless on a free plan. Rotate if it ever gets abused.
_EXA_API_KEY = "6388f17a-4954-4bc5-9de0-dd5d16164f68"


def _config_path() -> pathlib.Path:
    from encre.config import get_data_dir
    return get_data_dir() / _MCP_SEARCH_CONFIG_FILE


class EncreSearchManager:
    """Search engine that routes queries through an MCP service.

    The MCP server address is loaded from an encrypted config file on first
    use.  The connection is established lazily so callers can construct this
    object synchronously.
    """

    def __init__(self) -> None:
        self._client: MCPClient | None = None
        self._search_tool: str = ""
        self._tool_schema: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_config() -> dict[str, Any]:
        path = _config_path()
        if path.is_file():
            try:
                raw = path.read_text(encoding="utf-8")
                return json.loads(decrypt(raw))
            except Exception as exc:
                logger.warning("Failed to decrypt MCP search config, rebuilding: %s", exc)

        # First use -- encrypt hardcoded config to disk
        cfg: dict[str, Any] = {
            "url": _MCP_SEARCH_URL,
            "timeout": _MCP_SEARCH_TIMEOUT,
        }
        if _MCP_SEARCH_HEADERS:
            cfg["headers"] = dict(_MCP_SEARCH_HEADERS)

        plain = json.dumps(cfg, ensure_ascii=False)
        encrypted = encrypt(plain)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encrypted, encoding="utf-8")
        path.chmod(0o600)
        logger.info("MCP search config auto-created at %s", path)
        return cfg

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> None:
        if self._client is not None and self._client.is_initialized:
            return
        async with self._lock:
            if self._client is not None and self._client.is_initialized:
                return

            config = self._load_config()
            url = config.get("url", "")
            if not url:
                raise RuntimeError("MCP search config missing 'url'")

            headers = dict(config.get("headers", {}))
            # Always authenticate with the embedded Exa key -- the anonymous
            # endpoint rate-limits hard after a few calls. Injected at connect
            # time so it works even against a stale encrypted config cache.
            headers["Authorization"] = f"Bearer {_EXA_API_KEY}"
            timeout = float(config.get("timeout", 60.0))

            transport = HttpTransport(url, timeout=timeout, headers=headers)
            client = MCPClient(transport)
            try:
                await client.initialize()
                tools = await client.list_tools()
            except Exception:
                await client.close()
                raise

            if not tools:
                await client.close()
                raise RuntimeError("MCP search server exposes no tools")

            self._search_tool = _pick_search_tool(tools)
            # Store the schema for dynamic parameter mapping
            for t in tools:
                if t.get("name") == self._search_tool:
                    self._tool_schema = t.get("inputSchema", {})
                    break
            logger.info(
                "MCP search connected, tool=%s, server=%s",
                self._search_tool, url,
            )
            self._client = client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.info("MCP search disconnected")

    # ------------------------------------------------------------------
    # Public API -- matches the original DuckDuckGo interface exactly
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        num: int = 10,
        language: str = "",
        categories: str = "general",
        content: bool = True,
    ) -> dict[str, Any]:
        """Execute a search query via the MCP service.

        Returns the same dict shape as the original DuckDuckGo backend::

            {"results": [{title, url, content}], "suggestions": []}

        Args:
            content: When True (default), request page content / full text from
                the search server so results are self-contained and the model
                rarely needs a follow-up ``web_fetch`` (which anti-crawling
                sites often block). Set False for link-only results.
        """
        if not query:
            return {"results": [], "suggestions": []}

        await self._ensure_connected()
        assert self._client is not None

        # Build arguments dynamically from the tool's input schema
        args: dict[str, Any] = _map_search_args(
            query=query, num=num, language=language,
            categories=categories, schema=self._tool_schema,
            content=content,
        )

        try:
            content_resp = await self._client.call_tool(self._search_tool, args)
        except Exception as exc:
            logger.warning("MCP search failed: %s", exc)
            await self.close()
            return {"results": [], "suggestions": [], "_error": f"Search failed: {exc}"}

        normalized = _normalize_mcp_response(content_resp)
        results = normalized.get("results", [])

        # The Exa MCP ``web_search_exa`` tool only returns title + URL (its
        # schema rejects a ``contents`` flag). When content is requested and
        # the search response carried no inline content, fetch page bodies in
        # one batched ``web_fetch_exa`` call. That call uses Exa's own crawler,
        # which handles JS rendering and anti-scraping (ctrip/fliggy/...) that
        # plain httpx cannot -- the main reason a model would otherwise end up
        # with nothing useful.
        if content and results and not any(r.get("content") for r in results):
            await self._fetch_contents(results)

        return normalized

    async def _fetch_contents(
        self,
        results: list[dict[str, Any]],
        *,
        max_chars_per_url: int = 4000,
    ) -> None:
        """Fetch page bodies for result URLs in one batched call, in place.

        Uses ``web_fetch_exa`` (Exa's crawler) which handles JS rendering and
        anti-scraping. Failures degrade gracefully: a URL that cannot be
        fetched keeps its empty content, so the result stays link-only rather
        than failing the whole search.
        """
        if self._client is None or not self._client.is_initialized:
            return
        url_to_result: dict[str, dict[str, Any]] = {}
        ordered_urls: list[str] = []
        for r in results:
            url = r.get("url", "")
            if url and url not in url_to_result:
                url_to_result[url] = r
                ordered_urls.append(url)
        if not ordered_urls:
            return
        try:
            resp = await self._client.call_tool(
                "web_fetch_exa",
                {"urls": ordered_urls, "maxCharacters": max_chars_per_url},
            )
        except Exception as exc:
            logger.warning("web_fetch_exa batch failed: %s", exc)
            return
        content_map = _parse_fetch_response(resp)
        for url, r in url_to_result.items():
            body = content_map.get(url, "")
            if body:
                r["content"] = body

    async def search_batch(
        self,
        queries: list[str],
        *,
        num: int = 5,
        language: str = "",
    ) -> list[dict[str, Any]]:
        tasks = [self.search(q, num=num, language=language) for q in queries]
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def fetch(self, url: str, *, max_chars: int = 50000) -> str:
        """Fetch a single URL as clean markdown via Exa's crawler.

        Handles JS rendering and anti-scraping (ctrip/fliggy/...) that plain
        httpx cannot. Returns ``""`` when the page cannot be fetched so the
        caller can fall back or report. Used by the ``web_fetch`` tool so a
        user-supplied URL gets the same anti-scraping power as search results.
        """
        if not url:
            return ""
        await self._ensure_connected()
        if self._client is None:
            return ""
        try:
            resp = await self._client.call_tool(
                "web_fetch_exa",
                {"urls": [url], "maxCharacters": max_chars},
            )
        except Exception as exc:
            logger.warning("web_fetch_exa failed for %s: %s", url, exc)
            return ""
        content_map = _parse_fetch_response(resp)
        return content_map.get(url, "")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _map_search_args(
    query: str,
    num: int = 10,
    language: str = "",
    categories: str = "general",
    schema: dict[str, Any] | None = None,
    content: bool = True,
) -> dict[str, Any]:
    """Map standard search parameters to whatever the MCP tool's schema expects.

    Different MCP search servers use different parameter names:
    - ``open-webSearch`` uses ``query``, ``max_results``
    - ``web_search_prime`` (Zhipu) uses ``search_query``, ``content_size``
    - Exa (``web_search_exa``) uses ``query``, ``numResults``, and a ``contents``
      object whose ``text`` / ``highlights`` sub-flags control whether page
      content is returned at all. Without enabling these the server returns
      only title + URL + a short snippet -- forcing a follow-up web_fetch that
      anti-crawling sites block. When ``content=True`` we detect and enable
      whichever content switch the schema exposes.

    Args:
        content: When True, enable any schema-declared content/text flags so
            results carry page content inline.
    """
    if not schema:
        return {"query": query}

    props = schema.get("properties", {})
    required = set(schema.get("required", []))

    args: dict[str, Any] = {}

    for prop_name, prop_def in props.items():
        prop_def.get("type", "string")
        pdesc = (prop_def.get("description", "") + " " + prop_name).lower()

        if ("query" in pdesc or "search" in pdesc or "keyword" in pdesc) and prop_name in required:
            args[prop_name] = query

    # If no schema match found, fall back to our standard parameter
    if not args:
        args["query"] = query

    # Pass optional params if the tool supports them
    for pname, pdef in props.items():
        low = pname.lower()
        is_count_param = (
            ("max" in low and ("result" in low or "limit" in low or "count" in low))
            or ("num" in low and "result" in low)  # numResults, num_results
            or low in {"count", "limit", "num", "top_k", "k",
                       "maxresults", "max_results", "n_results"}
        )
        if is_count_param:
            args[pname] = num
        if ("language" in low or "locale" in low or "region" in low) and language:
            args[pname] = language
        if ("category" in low or "source" in low or "engine" in low) and categories != "general":
            args[pname] = categories

    # ── Content / full-text switches ────────────────────────────────────
    # Without these the server returns title+URL only; the model then has to
    # web_fetch each URL, which anti-crawling sites (ctrip, fliggy, ...) block.
    if content:
        for pname, pdef in props.items():
            low = pname.lower()
            ptype = pdef.get("type", "string")
            # Exa-style nested "contents" object: {text: true, highlights: true}
            if low == "contents" and ptype == "object":
                sub_props = pdef.get("properties", {}) or {}
                contents_val: dict[str, Any] = {}
                if "text" in sub_props:
                    contents_val["text"] = True
                if "highlights" in sub_props:
                    contents_val["highlights"] = True
                if contents_val:
                    args[pname] = contents_val
            # Flat "text" boolean (some Exa versions / other servers)
            elif low == "text" and ptype == "boolean":
                args[pname] = True
            # "livecrawl" / "live_crawl" forces a fresh fetch instead of cache
            elif ("livecrawl" in low or "live_crawl" in low) and ptype == "boolean":
                args[pname] = True

    return args


def _pick_search_tool(tools: list[dict[str, Any]]) -> str:
    """Pick the best tool from the MCP server's tool list.

    Prefers tools whose name contains "search" or "web_search";
    falls back to the first tool.
    """
    candidates: list[str] = []
    for t in tools:
        name: str = t.get("name", "")
        low = name.lower()
        if "search" in low or "web" in low:
            candidates.append(name)
    if candidates:
        return candidates[0]
    return tools[0].get("name", "")


def _normalize_mcp_response(content: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse MCP tool response into ``{results, suggestions}`` format."""
    results: list[dict[str, Any]] = []

    # Concatenate all text content blocks
    parts: list[str] = []
    for item in content:
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
    raw_text = "".join(parts)

    if not raw_text.strip():
        return {"results": [], "suggestions": []}

    stripped = raw_text.strip()

    # Try top-level JSON array of result objects
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                for entry in parsed:
                    if isinstance(entry, dict):
                        results.append(_normalize_result_entry(entry))
                return {"results": results, "suggestions": []}
        except json.JSONDecodeError:
            logger.debug("exception suppressed", exc_info=True)

    # Try JSON object with "results" key
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                items = parsed.get("results", parsed.get("items", []))
                suggestions = parsed.get("suggestions", parsed.get("related", []))
                if isinstance(items, list):
                    for entry in items:
                        if isinstance(entry, dict):
                            results.append(_normalize_result_entry(entry))
                    return {"results": results, "suggestions": list(suggestions)}
        except json.JSONDecodeError:
            logger.debug("exception suppressed", exc_info=True)

    # Try structured plaintext: blocks separated by "---" with "Title:" / "URL:" lines
    blocks = re.split(r"\n---+\n", stripped)
    if len(blocks) > 1 or "\nTitle: " in stripped:
        for block in blocks:
            entry = _parse_text_block(block.strip())
            if entry:
                results.append(entry)
        if results:
            return {"results": results, "suggestions": []}

    # Fallback: wrap the raw text as a single result
    results.append({
        "title": "Search Result",
        "url": "",
        "content": stripped,
    })
    return {"results": results, "suggestions": []}


_TEXT_BLOCK_RE = re.compile(
    r"^Title:\s*(?P<title>.+)$",
    re.MULTILINE,
)


def _parse_text_block(block: str) -> dict[str, Any] | None:
    """Parse a single text block like::

        Title: asyncio -- Asynchronous I/O
        URL: https://docs.python.org/3/library/asyncio.html
        Published: N/A
        Author: N/A
        Highlights:
        asyncio is a library to write concurrent code...
    """
    lines = block.split("\n")
    title = ""
    url = ""
    content_lines: list[str] = []
    in_highlights = False
    for line in lines:
        if line.startswith("Title: "):
            title = line[6:].strip()
        elif line.startswith("URL: "):
            url = line[4:].strip()
        elif line.startswith("Highlights: "):
            in_highlights = True
        elif in_highlights:
            cleaned = line.strip()
            if cleaned == "[...]":
                cleaned = ""
            content_lines.append(cleaned)
    if title and url:
        return {
            "title": title,
            "url": url,
            "content": "\n".join(c for c in content_lines if c).strip(),
        }
    return None


def _normalize_result_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Map any common result key name to our canonical ``{title, url, content}``.

    Caps ``content`` length so a single full-text page cannot dominate the
    payload when content fetching is enabled.
    """
    raw_content = str(
        entry.get("content")
        or entry.get("snippet")
        or entry.get("description")
        or entry.get("text")
        or entry.get("body")
        or ""
    )
    content = _truncate_content(raw_content, _MAX_RESULT_CONTENT)
    return {
        "title": str(
            entry.get("title")
            or entry.get("name")
            or entry.get("heading")
            or ""
        ),
        "url": str(
            entry.get("url")
            or entry.get("link")
            or entry.get("href")
            or entry.get("source")
            or ""
        ),
        "content": content,
    }


# Cap inline page content per result so N full-text results do not blow up
# the model's context. ~8000 chars ~= 2k tokens; 5 results ~= 10k tokens.
_MAX_RESULT_CONTENT = 8000


def _truncate_content(text: str, limit: int) -> str:
    """Truncate to ``limit`` chars with a visible marker when cut."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…[content truncated]"


# Matches a "URL: <url>" line as emitted by web_fetch_exa.
_FETCH_URL_RE = re.compile(r"^URL:\s*(?P<url>\S+)\s*$", re.MULTILINE)


def _parse_fetch_response(content: list[dict[str, Any]]) -> dict[str, str]:
    """Parse a ``web_fetch_exa`` response into a ``{url: body}`` map.

    The Exa fetch tool concatenates pages into one text block, each prefixed
    with a markdown heading and a URL line::

        # <title>
        URL: <url>

        <page body>
        # <title>
        URL: <url>
        ...

    Returns a mapping from URL to page body (body truncated conservatively).
    URLs that do not appear get no entry (caller leaves them link-only).
    """
    parts: list[str] = []
    for item in content:
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
    raw = "\n".join(parts)
    if not raw.strip():
        return {}
    out: dict[str, str] = {}
    # Split at lines starting with "# " (each fetched page begins with such a
    # heading). Keep the heading with its block.
    blocks = re.split(r"\n(?=# )", raw)
    for block in blocks:
        m = _FETCH_URL_RE.search(block)
        if not m:
            continue
        url = m.group("url").strip()
        body = block[m.end():].strip()
        if body and url not in out:
            out[url] = body
    return out


__all__ = ["EncreSearchManager"]
