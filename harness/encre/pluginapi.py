#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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

"""Frozen public plugin API for third-party Encre plugins.

This module is the **stability contract** for the plugin ecosystem: a
third-party plugin should import only from ``encre.pluginapi`` (plus
``encre.tools.base`` for tool construction).  The harness team may refactor
``encre.plugins.*`` internals freely; the names re-exported here follow
semantic versioning with a deprecation window of two minor releases.

Stability contract (v0.5+):
    - Names listed in ``__all__`` will not be removed or change signature
      incompatibly within a major version.
    - New names may be added in any minor release.
    - Breaking changes require a major version bump.

The authoritative implementations continue to live in
``encre.plugins.types`` and ``encre.plugins.context``; this module only
re-exports them so import paths stay stable across internal refactors.

Distributed plugin support (v0.5+):
    Plugins can declare ``transport = "process"`` or ``"remote"`` in
    their manifest to run in a subprocess sandbox or at a remote HTTP
    endpoint.  The transport types are re-exported here so third-party
    plugins and hosts can reference them without importing internals.

Extension surfaces (v0.5+):
    Plugins can provide four additional capability surfaces beyond the
    original nine (tools/backends/loop/events/permissions/commands/
    sub_agents/templates):

    - **context_providers** — inject messages into the agent context
      window before each turn (S6).  Declare ``"context:register"`` in
      manifest permissions.
    - **ui_providers** — inject UI components into the desktop renderer
      (S7).  Declare ``"ui:register"``.
    - **gateway_adapters** — register IM platform adapters like Telegram
      or WeChat (S8).  Declare ``"gateways:register"``.
    - **memory_stores** — register alternative memory storage backends
      (S9).  Declare ``"memory:register"``.

    Each surface has a corresponding ``get_*`` method on
    :class:`~encre.pluginapi.EncrePlugin` and a
    ``get_all_*`` method on the registry.
"""

from __future__ import annotations

from encre.plugins.context import PluginContext, build_context
from encre.plugins.transport import (
    LocalTransport,
    PluginTransport,
    ProcessTransport,
    RemoteToolProxy,
    RemoteTransport,
    ToolMetadata,
    create_transport,
)
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource

__all__ = [
    "EncrePlugin",
    "LocalTransport",
    "PluginContext",
    "PluginManifest",
    "PluginSource",
    "PluginTransport",
    "ProcessTransport",
    "RemoteToolProxy",
    "RemoteTransport",
    "ToolMetadata",
    "build_context",
    "create_transport",
]
