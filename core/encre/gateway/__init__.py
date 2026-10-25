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

"""Encre gateway package: the IM-platform bridge layer.

This is the Python layer that binds third-party messaging vendors to the Encre
agent runtime.  Each vendor has an adapter under
:mod:`encre.gateway.platforms` that normalises inbound events into
:class:`encre.gateway.platforms.base.MessageEvent` and exposes a
:meth:`BasePlatformAdapter.send` method for outbound delivery.  Inbound events
are funnelled by :class:`encre.gateway.run.GatewayRunner`, which authorises the
sender, resolves a durable session key, invokes the Rust agent core, and pushes
result deltas back through :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.

Two execution topologies are supported:

* In-process -- the adapter lives in this process next to the runner.  This is
  the default and the fast path, because a platform message and the agent call
  never cross a process boundary.
* Remote -- adapters run in a separate host and speak the :mod:`encre.gateway.ws_bridge`
  protocol over WebSocket.  :class:`GatewayMessage` and :class:`GatewayOp` are the
  wire frames for that protocol; they are re-exported here so callers only import
  the package root.

Data flow (inbound): adapter event -> runner -> authorizer/session resolver ->
Encre Agent Core -> stream consumer -> adapter ``send()``.  Desktop UI clients bypass
this package entirely and reach the same Encre Agent Core through
:mod:`encre.server` and :mod:`encre.protocol`.

This module is intentionally a shallow re-export surface.  It imports nothing
heavy at package-import time beyond the registry, the base adapter, the runner
entry point, and the bridge wire types, so that ``import encre.gateway`` is cheap
and side-effect free (no adapters are instantiated, no sockets are opened).

Public API:
    * :class:`GatewayRunner` -- unified lifecycle manager for adapters.
    * :class:`PlatformRegistry` / :func:`platform_registry` -- adapter registry.
    * :class:`BasePlatformAdapter` -- abstract base for all adapters.
    * :class:`GatewayMessage` / :class:`GatewayOp` -- WS bridge wire protocol.
    * :class:`GatewayConfig` / :class:`Platform` / :class:`PlatformConfig` -- typed config.
    * :class:`MessageEvent` / :class:`SendResult` / :class:`PlatformEntry`.
"""

from encre.gateway.config import GatewayConfig, Platform, PlatformConfig
from encre.gateway.platform_registry import PlatformEntry, PlatformRegistry, platform_registry
from encre.gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from encre.gateway.run import GatewayRunner
from encre.gateway.ws_bridge.protocol import GatewayMessage, GatewayOp

__all__ = [
    "BasePlatformAdapter",
    "GatewayConfig",
    "GatewayMessage",
    "GatewayOp",
    "GatewayRunner",
    "MessageEvent",
    "Platform",
    "PlatformConfig",
    "PlatformEntry",
    "PlatformRegistry",
    "SendResult",
    "platform_registry",
]
