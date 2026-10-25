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

"""Toolset configuration helpers.

Resolves which toolsets a given platform exposes, which of those are
configurable, and whether their required credentials are present.  Backed by
the harness toolset registry, so it never duplicates the tool catalogue.
"""


import logging
import subprocess
import sys
from typing import Any

logger = logging.getLogger(__name__)

# Human-facing labels/descriptions for the configurable toolsets.
_TOOLSET_METADATA: dict[str, tuple[str, str]] = {
    "file": ("File", "Read, write, and edit workspace files."),
    "shell": ("Shell", "Run shell commands in the workspace."),
    "web": ("Web", "Fetch and search the web."),
    "browser": ("Browser", "Drive a headless browser."),
    "media": ("Media", "Generate and inspect images and audio."),
    "memory": ("Memory", "Persistent agent memory."),
    "delegation": ("Delegation", "Spawn delegated sub-agents."),
    "mcp": ("MCP", "Model Context Protocol servers."),
}

# Toolsets whose required credentials live under these config keys.
_TOOLSET_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "browser": ("browser",),
    "mcp": ("mcp_servers",),
}


def _available_sets() -> list[str]:
    """Return the toolset names known to the harness registry."""
    from encre.tools.toolset import list_available_sets

    return list(list_available_sets())


def _get_effective_configurable_toolsets() -> list[tuple[str, str, str]]:
    """Return ``(name, label, description)`` for every configurable toolset."""
    result: list[tuple[str, str, str]] = []
    for name in _available_sets():
        label, desc = _TOOLSET_METADATA.get(name, (name.title(), f"{name} toolset."))
        result.append((name, label, desc))
    return result


def _get_platform_tools(
    config: dict[str, Any] | None,
    platform: str,
    include_default_mcp_servers: bool = True,
) -> set[str]:
    """Return the set of toolset names enabled for *platform*.

    Args:
        config: The loaded Encre configuration mapping (may be ``None``).
        platform: Platform identifier (e.g. ``"api_server"``).
        include_default_mcp_servers: Kept for interface parity.

    Returns:
        A set of enabled toolset names.  Falls back to every available set
        when the platform has no explicit toolset configuration.
    """
    available = set(_available_sets())
    if not isinstance(config, dict):
        return available

    platform_cfg: Any = None
    platforms = config.get("platforms")
    if isinstance(platforms, dict):
        platform_cfg = platforms.get(platform)
    if platform_cfg is None:
        platform_cfg = config.get(platform)

    enabled: Any = None
    if isinstance(platform_cfg, dict):
        enabled = platform_cfg.get("toolsets")
    if enabled is None:
        enabled = config.get("toolsets")

    if enabled is None:
        return available
    if isinstance(enabled, str):
        enabled = [enabled]
    try:
        requested = {str(item) for item in enabled}
    except TypeError:
        return available
    # ``["all"]`` or ``["*"]`` opts into the full catalogue.
    if requested & {"all", "*"}:
        return available
    return requested & available if requested else available


def _toolset_has_keys(name: str, config: dict[str, Any] | None) -> bool:
    """Return ``True`` when *name*'s required config keys are present."""
    required = _TOOLSET_REQUIRED_KEYS.get(name)
    if not required:
        return True
    if not isinstance(config, dict):
        return False
    return all(config.get(key) for key in required)


def _pip_install(package: str, *, quiet: bool = True) -> bool:
    """Install *package* via pip. Returns ``True`` on success."""
    cmd = [sys.executable, "-m", "pip", "install", package]
    if quiet:
        cmd.append("--quiet")
    try:
        subprocess.check_call(
            cmd,
            stdout=subprocess.DEVNULL if quiet else None,
            stderr=subprocess.DEVNULL if quiet else None,
        )
        return True
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("tools_config: pip install %s failed: %s", package, exc)
        return False
