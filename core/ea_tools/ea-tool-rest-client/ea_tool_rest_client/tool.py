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

"""REST client tool.

Issues an HTTP request against an arbitrary REST or GraphQL endpoint
using ``httpx`` and returns the status code, headers, and parsed body.
The tool is the model-facing equivalent of ``curl``: any endpoint the
user can reach from the shell can be reached from here.

Design notes
------------

The response body is truncated at 50K characters. This cap exists
because a naive ``GET /all`` against a large public API can return
tens of megabytes, which would overwhelm both the model's context
window and the tool-result transcript. The truncation marker
(``"... (truncated to 50K chars)"``) is included so the model can
decide whether to request a narrower slice of the data.

JSON responses are detected by content-type OR by a leading
``{`` / ``[`` in the body. The dual check exists because some
endpoints serve valid JSON without setting ``Content-Type``
correctly, and the JSON shape is a stronger signal than the header.

``follow_redirects=True`` is enabled unconditionally because most
public APIs (e.g. auth token endpoints) use 302/307 to move clients
to a canonical host. Not following would make the tool unusable
for a large class of legitimate use cases.

Non-GET methods are flagged ``is_destructive`` so destructive-aware
UIs can prompt for confirmation. This mirrors the semantics of
``curl`` / ``wget``: reading data is safe, writing data is not.
"""

import json
from typing import Any

import httpx

from encre.tools.base import build_tool


async def _rest_client_execute(**kwargs: Any) -> str:
    """Issue an HTTP request and return a JSON or plain-text result.

    The function is ``async`` because ``httpx.AsyncClient`` is the
    canonical way to make non-blocking HTTP calls from within an
    asyncio event loop. A fresh ``AsyncClient`` is created per call
    (rather than a module-level singleton) so a hung connection on
    one call cannot affect subsequent calls; the trade-off is a
    small per-call handshake cost that is negligible compared to
    the network I/O.

    Args:
        kwargs: Tool input keyed by the JSON schema declared on
            ``rest_tool.input_schema`` below.

    Returns:
        On success, either a JSON-encoded ``{"status": ...,
        "headers": {...}, "body": <parsed>}`` when the response
        looks like JSON, or a plain ``"HTTP <status>\\n<body>"``
        string otherwise. Errors are returned as
        ``"Error: ..."`` strings rather than raised so a network
        failure never kills an otherwise successful agent turn.
    """
    method = kwargs.get("method", "GET")
    url = kwargs.get("url", "")
    headers = kwargs.get("headers", {}) or {}
    body = kwargs.get("body", "")
    timeout = kwargs.get("timeout", 30)

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(float(timeout)),
            follow_redirects=True,
        ) as client:
            resp = await client.request(
                method=method,
                url=url,
                headers={k: str(v) for k, v in headers.items()},
                content=body if body else None,
            )

            status = resp.status_code
            content_type = resp.headers.get("content-type", "")
            response_body = resp.text

            if len(response_body) > 50000:
                response_body = response_body[:50000] + "\n... (truncated to 50K chars)"

            if "application/json" in content_type or response_body.strip().startswith(("{", "[")):
                try:
                    parsed = json.loads(response_body)
                    return json.dumps({"status": status, "headers": dict(resp.headers), "body": parsed}, indent=2, default=str)
                except (json.JSONDecodeError, ValueError):
                    pass

            return f"HTTP {status}\n{response_body}"

    except httpx.TimeoutException:
        return f"Error: Request timed out after {timeout}s for {url}"
    except httpx.InvalidURL:
        return f"Error: Invalid URL: {url}"
    except Exception as e:
        return f"Error making {method} request to {url}: {e}"


rest_tool = build_tool(
    name="rest_client",
    description=(
        "Make an HTTP request to a REST or GraphQL API endpoint and return the "
        "status code, headers, and parsed body.\n\n"
        "WHEN to use: call a documented REST/GraphQL API by its URL, integrate "
        "with a third-party service, or fetch a JSON resource that is not a "
        "human-readable web page.\n"
        "WHEN NOT to use: for fetching a web page to read, use web_fetch "
        "(renders JS, strips boilerplate); for browser interactions (clicks, "
        "form fills, logins) use the browser tool.\n"
        "TIPS: pass the body as a JSON string for JSON APIs and set the "
        "Content-Type header accordingly; JSON responses are auto-parsed and "
        "returned as a structured {status, headers, body} object.\n"
        "PITFALLS: response bodies over 50K chars are truncated; non-GET "
        "methods (POST/PUT/PATCH/DELETE) are flagged as destructive -- make "
        "sure the user actually wants the side effect."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                "description": "HTTP method. GET is safe/read-only; POST/PUT/PATCH/DELETE mutate state and are flagged as destructive.",
            },
            "url": {
                "type": "string",
                "description": "Absolute URL of the API endpoint, including scheme and query string (e.g. https://api.example.com/v1/users?page=1).",
            },
            "headers": {
                "type": "object",
                "description": "HTTP request headers as key-value pairs. Always include Content-Type (e.g. application/json) and any Authorization header the API requires.",
            },
            "body": {
                "type": "string",
                "description": "Raw request body as a string. For JSON APIs, pass a JSON-serialized string; values are sent verbatim (no automatic encoding).",
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds (default: 30). Increase for slow endpoints, decrease to fail fast on unreachable hosts.",
            },
        },
        "required": ["method", "url"],
    },
    execute=_rest_client_execute,
    intents=["coding", "system"],
    # HTTP requests are independent per call; ``httpx`` uses
    # connection pooling internally but this tool does not share
    # state across invocations.
    is_concurrency_safe=lambda _: True,
    category="web",
    semantic_type="network",
    # Only reading data is considered safe; writes are flagged
    # destructive so the frontend can prompt for confirmation.
    is_destructive=lambda args: args.get("method", "GET").upper() in ("POST", "PUT", "DELETE", "PATCH"),
)
