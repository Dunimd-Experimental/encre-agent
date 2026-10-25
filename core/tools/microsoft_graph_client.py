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

"""Thin Microsoft Graph REST client.

Wraps the Graph v1.0 endpoint with bearer-token injection from a
pluggable token provider (see :mod:`tools.microsoft_graph_auth`).
Only the JSON verbs the Teams adapter needs are implemented.

Each request constructs a fresh :class:`httpx.AsyncClient` because the
client's transport (including any injected test transport) must be
respected and httpx clients cannot be shared across tasks once their
connection pool is closed. Callers that need a persistent client
should supply a custom ``transport``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class MicrosoftGraphClient:
    """Minimal async Graph client bound to a token provider.

    The token provider is duck-typed: any object exposing either an
    async ``get_access_token()`` method or a synchronous
    ``access_token`` attribute will work. That lets tests inject a
    stub without pulling in the full Azure AD client-credentials flow.
    """

    def __init(
        self,
        token_provider: Any,
        *,
        transport: Any = None,
        base_url: str = _GRAPH_BASE,
        timeout: float = 30.0,
    ) -> None:
        self._token_provider = token_provider
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def _headers(self) -> dict[str, str]:
        """Build request headers, injecting the current bearer token.

        The token is fetched fresh on every request so a stale token
        cannot be used even if the provider's cache expires between
        calls. ``Accept`` is pinned to JSON so the client never has to
        handle multipart or binary responses.
        """
        token = ""
        if self._token_provider is not None:
            getter = getattr(self._token_provider, "get_access_token", None)
            if getter is not None:
                token = await getter()
            elif getattr(self._token_provider, "access_token", None):
                token = str(self._token_provider.access_token)
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _client(self) -> httpx.AsyncClient:
        """Construct a fresh async HTTP client for one request.

        Reusing a client across calls would couple test transports to
        a specific connection pool, so each request gets its own
        client and closes it in a context manager.
        """
        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def _url(self, path: str) -> str:
        """Resolve *path* against the base URL.

        Absolute URLs (``http://`` or ``https://``) are returned
        unchanged so callers can point at Graph-like test services
        without overriding ``base_url``.
        """
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    async def post_json(self, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST *json_body* to *path* and return the parsed JSON response.

        Empty bodies are coerced to ``{}`` because the Graph API
        rejects a missing body on POST endpoints that require one; a
        ``None`` body would produce a request with ``Content-Length: 0``
        and a ``400``.

        Args:
            path: The Graph resource path (relative or absolute).
            json_body: The JSON body to send.

        Returns:
            The parsed JSON response, or ``{}`` when the response has
            no body (e.g. a ``204 No Content``).

        Raises:
            httpx.HTTPStatusError: On any non-2xx status.
        """
        headers = await self._headers()
        async with self._client() as client:
            resp = await client.post(self._url(path), json=json_body or {}, headers=headers)
            resp.raise_for_status()
            if not resp.content:
                return {}
            return resp.json()

    async def patch_json(self, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        """PATCH *json_body* to *path* and return the parsed JSON response.

        Same body semantics as :meth:`post_json`: PATCH is used by the
        Graph API for partial updates, but the client still sends an
        empty body when none is supplied rather than omitting the
        content type.
        """
        headers = await self._headers()
        async with self._client() as client:
            resp = await client.patch(self._url(path), json=json_body or {}, headers=headers)
            resp.raise_for_status()
            if not resp.content:
                return {}
            return resp.json()

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET *path* and return the parsed JSON response.

        Args:
            path: The Graph resource path (relative or absolute).
            params: Optional query-string parameters.

        Returns:
            The parsed JSON response, or ``{}`` when the response has
            no body.
        """
        headers = await self._headers()
        async with self._client() as client:
            resp = await client.get(self._url(path), params=params, headers=headers)
            resp.raise_for_status()
            if not resp.content:
                return {}
            return resp.json()

    async def get(self, path: str, **kwargs: Any) -> Any:
        """Alias for :meth:`get_json` (interface parity with httpx clients).

        The extra ``**kwargs`` is accepted but only ``params`` is
        forwarded — callers that pass httpx-specific options such as
        ``headers`` or ``timeout`` silently drop them because this
        client owns those settings.
        """
        return await self.get_json(path, params=kwargs.get("params"))
