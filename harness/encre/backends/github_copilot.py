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

"""
GitHub Copilot backend -- uses GitHub Copilot subscription via OpenAI-compatible API.

GitHub Copilot provides an OpenAI-compatible chat completion API for
subscribers.  Authentication is handled via a GitHub token (OAuth device
code flow) or a COPILOT_GITHUB_TOKEN environment variable.

Note: This backend expects a valid GitHub token.  The OAuth device code
flow must be completed externally before using this backend.

Authentication (priority order):
1. Explicit ``api_key`` parameter
2. ``COPILOT_GITHUB_TOKEN`` environment variable
3. ``GH_TOKEN`` environment variable
4. Output of ``gh auth token`` (CLI)

Base URL: https://api.githubcopilot.com (determined dynamically)
"""

import os
import subprocess
import time
from typing import Any

import httpx

from encre.backends.openai_sse import OpenAISSEBackend

# Copilot API tokens issued by the token exchange are short-lived; refresh
# this many seconds before their documented expiry.
_COPILOT_TOKEN_TTL_MARGIN = 300
# Module-level cache: GitHub token -> (Copilot API token, expires_at epoch).
_COPILOT_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


class GitHubCopilotBackend(OpenAISSEBackend):
    """GitHub Copilot backend for Copilot subscribers.

    Uses GitHub Copilot's OpenAI-compatible chat API.  Authentication
    requires a GitHub token with Copilot access; OAuth tokens are exchanged
    for a short-lived Copilot API token automatically.  The API also
    requires the ``Editor-Version`` and ``Copilot-Integration-Id`` headers
    on every request (requests without them are rejected outright).
    """

    DEFAULT_BASE_URL = "https://api.githubcopilot.com"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "gpt-5.6-copilot",
        **kwargs: Any,
    ) -> None:
        """Initialize the GitHub Copilot backend.

        Args:
            api_key: Explicit GitHub token. When empty, a token is resolved
                from environment variables or the ``gh`` CLI.
            base_url: API endpoint; defaults to the Copilot API URL.
            model: Default Copilot model identifier.
            **kwargs: Additional options forwarded to the parent backend.
        """
        if not base_url:
            # No explicit endpoint given: use the Copilot API URL.
            base_url = self.DEFAULT_BASE_URL
        # Resolve the GitHub token from env / CLI when not passed explicitly,
        # then swap OAuth tokens for a short-lived Copilot API token.
        resolved_key = api_key or self._resolve_github_token()
        resolved_key = self._exchange_copilot_token(resolved_key)
        # Mandatory Copilot API headers; caller-supplied values win.
        required_headers = {
            "Editor-Version": "vscode/1.99.2",
            "Editor-Plugin-Version": "copilot-chat/0.26.2",
            "Copilot-Integration-Id": "vscode-chat",
        }
        required_headers.update(kwargs.pop("extra_headers", None) or {})
        super().__init__(
            api_key=resolved_key,
            base_url=base_url,
            model=model,
            extra_headers=required_headers,
            **kwargs,
        )

    @staticmethod
    def _resolve_github_token() -> str:
        """Resolve a GitHub token for Copilot authentication.

        Resolution order:
            1. ``COPILOT_GITHUB_TOKEN`` environment variable.
            2. ``GH_TOKEN`` environment variable.
            3. The output of ``gh auth token`` (GitHub CLI).

        Returns:
            The resolved token string, or an empty string if none found.
        """
        # Prefer explicit environment variables for the token.
        token = os.environ.get("COPILOT_GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        # Fall back to the GitHub CLI if no env token is set.
        if not token:
            try:
                # Ask the gh CLI for the currently authenticated token.
                result = subprocess.run(
                    ["gh", "auth", "token"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    # Strip trailing newline from the CLI output.
                    token = result.stdout.strip()
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                # gh not installed / timed out / other OS error: no token.
                pass
        return token

    @staticmethod
    def _exchange_copilot_token(token: str) -> str:
        """Exchange a GitHub OAuth token for a short-lived Copilot API token.

        The Copilot API rejects raw GitHub OAuth tokens (``gho_``/``ghu_``)
        with 401; they must be swapped at
        ``api.github.com/copilot_internal/v2/token`` for a short-lived
        bearer token first.  PATs (``ghp_``/``github_pat_`` with the
        copilot scope) connect directly.  Exchanged tokens are cached in
        memory until shortly before expiry.
        """
        if not token or not token.startswith(("gho_", "ghu_")):
            return token
        cached = _COPILOT_TOKEN_CACHE.get(token)
        if cached and cached[1] > time.time() + _COPILOT_TOKEN_TTL_MARGIN:
            return cached[0]
        try:
            resp = httpx.post(
                "https://api.github.com/copilot_internal/v2/token",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
            api_token = str(payload.get("token", "") or "")
            if api_token:
                expires_at = float(payload.get("expires_at", 0) or 0)
                if expires_at <= time.time():
                    expires_at = time.time() + 1800
                _COPILOT_TOKEN_CACHE[token] = (api_token, expires_at)
                return api_token
        except Exception:
            # Exchange failed: fall back to the raw token so the API error
            # surfaces with its real message instead of being masked here.
            pass
        return token

    def context_window_size(self) -> int:
        """Return the context window size (in tokens) for Copilot models."""
        return 128000
