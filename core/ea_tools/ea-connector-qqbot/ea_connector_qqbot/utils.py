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

"""Shared HTTP and configuration helpers for the QQ Bot platform adapter.

Attribution: inspired by the Hermes Agent project
(https://github.com/NousResearch/hermes-agent.git).

This module sits at the bottom of the
:mod:`ea_connector_qqbot` dependency graph. It imports only the
standard library plus :const:`QQBOT_VERSION` from
:mod:`ea_connector_qqbot.constants`, so it can be reached by
:mod:`ea_connector_qqbot.adapter`,
:mod:`ea_connector_qqbot.onboard`, and the desktop setup flow
without creating a cycle.

It exposes three groups of helpers:

* :func:`build_user_agent` -- composes a descriptive ``User-Agent`` string
  carrying the adapter version, the running Python version, the operating
  system, and the installed Encre version.
* :func:`get_api_headers` -- returns the standard JSON request/response
  header set used by every qqbot HTTP call.
* :func:`coerce_list` -- normalises heterogeneous YAML config values into a
  trimmed ``list[str]``.

Public API:
    :func:`build_user_agent`
    :func:`get_api_headers`
    :func:`coerce_list`
"""

import platform
import sys
from typing import Any, Dict, List

from .constants import QQBOT_VERSION


# ---------------------------------------------------------------------------
# User-Agent
# ---------------------------------------------------------------------------

def _get_encre_version() -> str:
    """Return the installed ``encre`` package version, or ``'dev'``.

    Reads the version through :func:`importlib.metadata.version`, which looks
    up the installed distribution's metadata. A development checkout may not
    have the package installed, and metadata lookups can fail in editable or
    wheel-less environments, so every failure mode collapses to ``'dev'``
    rather than propagating.

    The result feeds the trailing ``Encre/<ver>`` segment of the User-Agent
    built by :func:`build_user_agent`, letting QQ-side telemetry distinguish
    adapter releases from Encre releases.

    Returns:
        A version string such as ``"0.9.0"``, or ``"dev"`` when the package
        version cannot be determined.
    """
    try:
        from importlib.metadata import version
        return version("encre")
    except Exception:
        return "dev"


def build_user_agent() -> str:
    """Build the descriptive User-Agent string sent with every request.

    QQ's APIs inspect the User-Agent to identify clients and correlate
    support incidents. Emitting a stable, structured string makes the Encre
    client findable among many third-party bots.

    Format::

        QQBotAdapter/<qqbot_version> (Python/<py_version>; <os>; Encre/<encre_version>)

    Example::

        QQBotAdapter/1.1.0 (Python/3.11.15; darwin; Encre/0.9.0)

    The Python segment uses ``sys.version_info`` (not ``platform.python_version``)
    so the build number stays stable across patch releases of the same
    minor. The OS segment is lowercased via
    :func:`platform.system`, yielding ``windows``, ``linux``, or ``darwin``.

    Returns:
        The formatted User-Agent string.
    """
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    os_name = platform.system().lower()
    encre_version = _get_encre_version()
    return f"QQBotAdapter/{QQBOT_VERSION} (Python/{py_version}; {os_name}; Encre/{encre_version})"


def get_api_headers() -> Dict[str, str]:
    """Return the standard HTTP header set for qqbot portal and API calls.

    The ``Accept: application/json`` header is load-bearing: without it the
    portal serves a JavaScript anti-bot challenge page instead of JSON,
    which then blows up at ``resp.json()`` with an unhelpful parse error.
    Both ``Content-Type`` and ``Accept`` are therefore pinned to
    ``application/json`` on every request.

    The ``User-Agent`` is evaluated at call time rather than cached, so
    header dicts constructed before an upgrade still advertise the current
    client version.

    Returns:
        A dict with keys ``"Content-Type"``, ``"Accept"``, and
        ``"User-Agent"``.

    Note:
        Callers that need an ``Authorization`` header merge it in themselves;
        this factory is deliberately credential-free so it can be used by the
        onboard flow before any token exists.
    """
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": build_user_agent(),
    }


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def coerce_list(value: Any) -> List[str]:
    """Coerce a heterogeneous config value into a trimmed list of strings.

    YAML configs for the gateway arrive in several shapes depending on how
    the operator wrote them, and Encre accepts all of them. This helper is
    the single normalisation point used for allowlists such as
    ``allow_from`` and ``group_allow_from``.

    Accepted inputs:

    * ``None`` -- empty config key, yields ``[]``.
    * A comma-separated string ``"a, b, c"`` -- split and stripped.
    * ``list`` / ``tuple`` / ``set`` -- each element is stringified.
    * Any other scalar, e.g. ``42`` -- wrapped as ``["42"]``.

    Every element is stripped of surrounding whitespace, and elements that
    are empty after stripping are dropped. That keeps a stray ``""`` or
    `"  "` entry from matching a wildcard or a real openid.

    Args:
        value: The raw config value, of any type.

    Returns:
        A list of non-empty, whitespace-stripped strings.

    Example::

        >>> coerce_list("a, b, c")
        ['a', 'b', 'c']
        >>> coerce_list(["a", "  ", "b"])
        ['a', 'b']
        >>> coerce_list(None)
        []
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []
