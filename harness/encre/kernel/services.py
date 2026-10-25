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

"""Process-wide service locator owned by the kernel.

Shared registries used to live in :mod:`encre.agent` -- the *surface* layer.
The loop imported them from there, which made the kernel depend on the agent
facade and made standalone embedding awkward.  This module owns that state
instead.  ``encre.agent`` delegates to it (and keeps its historical accessor
names as aliases), and the loop imports from here.

Design rules
============
* **One slot per service.**  A registry is created lazily and shared by every
  agent in the process, matching the previous semantics.
* **Discovery is explicit.**  :func:`shared_plugin_registry_raw` never scans;
  :func:`shared_plugin_registry` scans exactly once under a lock.  Callers that
  must not block (deferred construction) use the raw accessor.
* **Resettable for tests.**  :func:`reset_shared_services` clears the slots; it
  is the supported way for a test to isolate itself from process-wide state.
"""

from __future__ import annotations

import threading
from typing import Any

__all__ = [
    "PROJECT_SKILLS_CANDIDATE_DIRS",
    "PROJECT_SUB_AGENT_CANDIDATE_DIRS",
    "install_shared_plugin_registry",
    "reset_shared_services",
    "shared_plugin_registry",
    "shared_plugin_registry_raw",
    "shared_skill_registry_raw",
]

#: Project-level skill directories scanned under the active workspace.  The
#: registry's priority/override rules decide which copy of a same-named skill
#: wins, so co-locating native and Claude Code layouts is safe.
PROJECT_SKILLS_CANDIDATE_DIRS: tuple[str, ...] = (
    ".encre/skills",
    ".claude/skills",
)

#: Project-level sub-agent directories.  Native, Claude Code, and Codex
#: layouts are merged so an existing workspace keeps working.
PROJECT_SUB_AGENT_CANDIDATE_DIRS: tuple[str, ...] = (
    ".encre/agents",
    ".claude/agents",
    ".codex/agents",
)

_LOCK = threading.Lock()
_PLUGIN_DISCOVERY_LOCK = threading.Lock()
_PLUGINS_DISCOVERED = False
_SHARED_PLUGIN_REGISTRY: Any | None = None
_SHARED_SKILL_REGISTRY: Any | None = None


def shared_skill_registry_raw() -> Any:
    """Return the shared skill registry **without** loading any skills.

    The populated variant lives in :func:`encre.agent._get_shared_skill_registry`
    because loading bundled/user skills is an agent-surface concern; the kernel
    only needs the stable object identity.
    """
    global _SHARED_SKILL_REGISTRY
    with _LOCK:
        if _SHARED_SKILL_REGISTRY is None:
            from encre.skills.registry import EncreSkillRegistry

            _SHARED_SKILL_REGISTRY = EncreSkillRegistry()
        return _SHARED_SKILL_REGISTRY


def shared_plugin_registry_raw() -> Any:
    """Return the shared plugin registry without triggering discovery."""
    global _SHARED_PLUGIN_REGISTRY
    with _LOCK:
        if _SHARED_PLUGIN_REGISTRY is None:
            from encre.plugins.registry import PluginRegistry

            _SHARED_PLUGIN_REGISTRY = PluginRegistry()
        return _SHARED_PLUGIN_REGISTRY


def shared_plugin_registry() -> Any:
    """Return the shared plugin registry, running discovery exactly once."""
    registry = shared_plugin_registry_raw()
    ensure_plugins_discovered(registry)
    return registry


def ensure_plugins_discovered(registry: Any) -> None:
    """Run ``registry.discover_all()`` exactly once per process.

    Separated from :func:`shared_plugin_registry` so a caller that owns a
    custom registry (an embedder or a test) can use the same process-wide
    once-only guard without replacing the shared slot.
    """
    global _PLUGINS_DISCOVERED
    if _PLUGINS_DISCOVERED:
        return
    with _PLUGIN_DISCOVERY_LOCK:
        if not _PLUGINS_DISCOVERED:
            registry.discover_all()
            _PLUGINS_DISCOVERED = True


def install_shared_plugin_registry(registry: Any, *, discovered: bool = True) -> None:
    """Install ``registry`` as the process-wide plugin registry.

    Embedders and tests use this to substitute a registry without triggering
    filesystem/entry-point discovery.  Passing ``discovered=True`` (the
    default) records that the caller has already populated it.
    """
    global _SHARED_PLUGIN_REGISTRY, _PLUGINS_DISCOVERED
    with _LOCK:
        _SHARED_PLUGIN_REGISTRY = registry
        _PLUGINS_DISCOVERED = bool(discovered)


def reset_shared_services() -> None:
    """Clear every shared slot (test isolation; not for production use)."""
    global _SHARED_PLUGIN_REGISTRY, _SHARED_SKILL_REGISTRY, _PLUGINS_DISCOVERED
    with _LOCK:
        _SHARED_PLUGIN_REGISTRY = None
        _SHARED_SKILL_REGISTRY = None
        _PLUGINS_DISCOVERED = False
