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

"""Microsoft Graph token provider (client-credentials flow).

Fetches and caches an application access token from Azure AD using the
``TEAMS_CLIENT_ID`` / ``TEAMS_CLIENT_SECRET`` / ``TEAMS_TENANT_ID``
environment trio.

The provider is intentionally single-tenant: Encre Agent expects one
Graph app per gateway instance, so per-call tenant routing would add
complexity without benefit. The cached token is refreshed proactively
by subtracting a fixed skew from the server-reported ``expires_in``,
so an in-flight request never races a token boundary.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

# Azure AD v2.0 token endpoint (v1 endpoints are deprecated by
# Microsoft as of 2025 and no longer return refreshable tokens).
_TOKEN_ENDPOINT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
# Application-only scope; user-delegated scopes would require the
# interactive flow which Encre Agent does not support.
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
# Refresh a little before the server-reported expiry to avoid sending
# a request with a token that expires mid-flight. 120s is well within
# the Graph API's typical grace period.
_EXPIRY_SKEW_SECONDS = 120


class MicrosoftGraphTokenProvider:
    """Caches a Microsoft Graph app token obtained via client credentials.

    Not thread-safe against concurrent ``get_access_token`` calls: two
    coroutines racing on expiry would each fetch a fresh token and one
    would overwrite the other's cached value. That is a benign race
    (both tokens are valid) but callers that need strict single-fetch
    semantics should serialize access to a single provider instance.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tenant_id: str,
        *,
        scope: str = _GRAPH_SCOPE,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._tenant_id = tenant_id
        self._scope = scope
        self._token: str | None = None
        self._expires_at: float = 0.0

    @classmethod
    def from_env(cls) -> "MicrosoftGraphTokenProvider":
        """Build a provider from the ``TEAMS_*`` environment variables.

        Missing values are passed through as empty strings rather than
        raising so that :meth:`get_access_token` can emit a single
        clear log message about which credentials are missing.
        """
        return cls(
            client_id=os.getenv("TEAMS_CLIENT_ID", ""),
            client_secret=os.getenv("TEAMS_CLIENT_SECRET", ""),
            tenant_id=os.getenv("TEAMS_TENANT_ID", ""),
        )

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid access token, refreshing it when necessary.

        The cache check runs before the credential check so a caller
        that forgot to configure credentials does not repeatedly
        incur a network round trip after a prior successful fetch.

        Args:
            force_refresh: Ignore the cached token and fetch a fresh
                one. Used by the client when the Graph API returns a
                401 for a call that already had a cached token.

        Returns:
            The bearer token string, or ``""`` when credentials are
            missing or the token endpoint is unreachable.
        """
        if not force_refresh and self._token and time.time() < self._expires_at:
            return self._token
        if not (self._client_id and self._client_secret and self._tenant_id):
            logger.warning("MicrosoftGraphTokenProvider: missing TEAMS_* credentials")
            return ""

        url = _TOKEN_ENDPOINT.format(tenant=self._tenant_id)
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": self._scope,
            "grant_type": "client_credentials",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, data=data)
            resp.raise_for_status()
            payload = resp.json()
        self._token = str(payload.get("access_token") or "")
        expires_in = float(payload.get("expires_in") or 3600)
        self._expires_at = time.time() + max(0.0, expires_in - _EXPIRY_SKEW_SECONDS)
        return self._token
