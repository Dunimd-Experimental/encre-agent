#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (c) 2025-2026 Wenze Wei. All Rights Reserved.
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

"""Process-wide network proxy resolution.

The General -> Network settings panel offers three connection modes that
govern every outbound request this backend makes: LLM API calls, IM
gateway adapters, and tool-level HTTP.  Instead of threading a proxy
argument through every HTTP client, the chosen mode is mirrored onto the
standard proxy environment variables (``HTTP_PROXY`` / ``HTTPS_PROXY`` /
``ALL_PROXY`` / ``NO_PROXY`` and their lowercase twins).  Every HTTP stack
in the process already honours those variables: httpx (the OpenAI-
compatible backends run with ``trust_env=True``),
:func:`encre.gateway.platforms.base.resolve_proxy_url` (every IM adapter),
and ``requests``/``urllib`` based tools.
every outbound request the backend makes -- LLM calls, gateway adapters,
and tool HTTP.  Rather than threading a proxy argument through each HTTP
stack, the chosen mode is mirrored onto the standard proxy environment
variables (``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY``
and their lowercase twins), which everything in the process already
honours: httpx (the OpenAI-compatible backends run with ``trust_env``),
:func:`encre.gateway.platforms.base.resolve_proxy_url` (every IM
adapter), and ``requests``/``urllib`` based tools.

Modes
-----
``system`` (default)
    Probe the OS proxy configuration via :func:`urllib.request.getproxies`
    (Windows registry / macOS system config / inherited environment on
    Linux) and mirror it into the managed variables.
``manual``
    Build the URL from the scheme/host/port/credentials fields.
``direct``
    Clear every managed variable so connections go out raw.

The launch environment is snapshotted once before the first mutation, so
switching back to ``system`` restores exactly what the process was
started with, layered with the OS probe.
"""

from __future__ import annotations

import logging
import os
import urllib.request
from typing import Any, Mapping
from urllib.parse import quote, urlunsplit

logger = logging.getLogger("encre.proxy")

#: Both spellings of every variable this module owns.  Shells and libraries
#: read either casing, so a partial update would leave a stale proxy behind
#: for whichever spelling was not touched.
_MANAGED_VARS: tuple[str, ...] = (
    "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy",
    "NO_PROXY", "no_proxy",
)

#: Snapshot of the launch environment, captured before the first mutation
#: so "system" mode can restore what the process was started with.
_LAUNCH_ENV: dict[str, str] = {}
_SNAPSHOT_DONE = False


def _snapshot_launch_env() -> None:
    """Record the proxy variables the process inherited at startup.

    The launch values are the restoration baseline for ``system`` mode.
    Running this more than once is harmless: it is a no-op after the first
    successful snapshot, so a later, transient ``export HTTPS_PROXY=...``
    can never be mistaken for the original launch state.
    """
    global _SNAPSHOT_DONE
    if _SNAPSHOT_DONE:
        return
    for var in _MANAGED_VARS:
        val = os.environ.get(var)
        if val:
            _LAUNCH_ENV[var] = val
    _SNAPSHOT_DONE = True


def _clear_managed() -> None:
    """Unset every proxy variable this module owns.

    Both casings are cleared, otherwise a client that reads the casing the
    other client does not would still see the old proxy.  Called before
    every rewrite so a mode change never leaves a stale variable behind.
    """
    for var in _MANAGED_VARS:
        os.environ.pop(var, None)


def _redact(url: str) -> str:
    """Mask the password in a proxy URL for logging."""
    if "@" not in url:
        return url
    head, _, tail = url.rpartition("@")
    idx = head.find("://")
    prefix = head[: idx + 3] if idx >= 0 else ""
    creds = head[idx + 3:] if idx >= 0 else head
    user = creds.split(":", 1)[0]
    return f"{prefix}{user}:***@{tail}"


def build_manual_proxy_url(settings: Mapping[str, Any]) -> str | None:
    """Assemble the proxy URL from the manual settings fields.

    Args:
        settings: Flat settings mapping holding ``network_proxy_*`` keys.

    Returns:
        The URL (e.g. ``http://user:pass@127.0.0.1:7890``), or ``None``
        when no host is configured.  Credentials are percent-encoded so a
        password containing ``@`` or ``:`` cannot corrupt the URL.
    """
    host = str(settings.get("network_proxy_host") or "").strip()
    if not host:
        return None
    scheme = str(settings.get("network_proxy_scheme") or "http").strip() or "http"
    try:
        port = int(settings.get("network_proxy_port") or 0)
    except (TypeError, ValueError):
        port = 0
    user = str(settings.get("network_proxy_username") or "").strip()
    password = str(settings.get("network_proxy_password") or "")
    auth = ""
    if user:
        auth = quote(user, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    netloc = f"{auth}{host}:{port}" if port else f"{auth}{host}"
    return urlunsplit((scheme, netloc, "", "", ""))


def apply_network_proxy(settings: Mapping[str, Any] | None) -> None:
    """Apply the configured proxy mode to the process environment.

    Idempotent and failure-tolerant: called at startup and on every
    ``configure`` patch touching a ``network_proxy_*`` key, so an invalid
    manual entry must never crash the caller -- the worst outcome is a
    logged warning and direct connectivity.

    Args:
        settings: Flat settings mapping (``load_settings()`` view) holding
            ``network_proxy_mode`` and the manual fields; ``None`` is
            treated as "all defaults", i.e. system mode.
    """
    _snapshot_launch_env()
    cfg = settings or {}
    mode = str(cfg.get("network_proxy_mode") or "system").strip() or "system"
    try:
        if mode == "direct":
            _clear_managed()
            logger.info("[proxy] mode=direct -- proxy environment cleared")
            return

        if mode == "manual":
            url = build_manual_proxy_url(cfg)
            if not url:
                # Host not filled in yet -- behave like direct instead of
                # writing a broken URL that fails every request.
                _clear_managed()
                logger.info("[proxy] mode=manual without host -- connecting directly")
                return
            _clear_managed()
            for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
                os.environ[var] = url
            bypass = str(cfg.get("network_proxy_bypass") or "").strip()
            if bypass:
                for var in ("NO_PROXY", "no_proxy"):
                    os.environ[var] = bypass
            logger.info("[proxy] mode=manual url=%s bypass=%r", _redact(url), bypass)
            return

        # Default branch: ``system`` mode.  Restore the launch snapshot,
        # then mirror whatever the OS reports on top of it.  setdefault()
        # is used rather than assignment so a value getproxies() merely
        # echoes back cannot overwrite the launch environment.
        _clear_managed()
        for var, val in _LAUNCH_ENV.items():
            os.environ[var] = val
        try:
            proxies: dict[str, str] = urllib.request.getproxies() or {}
        except Exception as exc:  # pragma: no cover - platform quirk
            logger.warning("[proxy] OS proxy probe failed: %s", exc)
            proxies = {}
        for scheme, url in proxies.items():
            if scheme == "http":
                os.environ.setdefault("HTTP_PROXY", url)
                os.environ.setdefault("http_proxy", url)
            elif scheme == "https":
                os.environ.setdefault("HTTPS_PROXY", url)
                os.environ.setdefault("https_proxy", url)
            elif scheme == "no":
                os.environ.setdefault("NO_PROXY", url)
                os.environ.setdefault("no_proxy", url)
            elif scheme == "socks":
                os.environ.setdefault("ALL_PROXY", url)
                os.environ.setdefault("all_proxy", url)
        logger.info("[proxy] mode=system -- env mirrors OS proxies (%s)", sorted(proxies))
    except Exception as exc:
        logger.warning("[proxy] apply failed: %s", exc)
