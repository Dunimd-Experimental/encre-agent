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

"""WebSocket bridge that lets platform adapters run in another process.

This subpackage is the process-boundary half of the
:mod:`encre.gateway` layer.  Built-in adapters
(:mod:`encre.gateway.platforms`) run in-process and talk to
:class:`encre.gateway.run.GatewayRunner` directly, so they never touch
this package; it exists purely to extend the gateway beyond the local
process.  Typical deployments put the adapter in a plugin host, a
sandboxed container, or a different machine, dial in over WebSocket, and
the gateway treats the remote side exactly like a local adapter.

Topology::

    remote adapter (plugin) <--WebSocket--> WsBridgeServer <--in-process--> GatewayRunner
              |                                      |
      GatewayClient (SDK)             RemotePlatformAdapter (server-side proxy)

Inbound direction: the remote adapter sends an agent turn request over
the socket, the bridge resolves a session in the runner, and the runner
streams the reply back as gateway wire messages.  Outbound direction:
the bridge wraps each live socket in a
:class:`~encre.gateway.ws_bridge.remote_adapter.RemotePlatformAdapter`
so
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer` can deliver
an agent reply to the remote side through the same ``send()`` contract
used for local adapters.

Modules:

* :mod:`encre.gateway.ws_bridge.protocol` -- the bridge's own message
  vocabulary: :class:`~encre.gateway.ws_bridge.protocol.GatewayOp`
  opcodes and the :class:`~encre.gateway.ws_bridge.protocol.GatewayMessage`
  envelope.  This is distinct from
  :mod:`encre.server.protocol`, which defines the desktop client
  protocol; the two are unrelated wire formats that happen to share a
  "protocol" name.
* :mod:`encre.gateway.ws_bridge.server` --
  :class:`~encre.gateway.ws_bridge.server.WsBridgeServer`, the agent
  side.  Owns the listening socket, the HELLO handshake, the connection
  cap, heartbeat acknowledgements, and request dispatch into the runner.
* :mod:`encre.gateway.ws_bridge.client` --
  :class:`~encre.gateway.ws_bridge.client.GatewayClient`, the
  adapter-side dialer with reconnect/backoff, heartbeats, and
  request/response correlation.
* :mod:`encre.gateway.ws_bridge.remote_adapter` --
  :class:`~encre.gateway.ws_bridge.remote_adapter.RemotePlatformAdapter`,
  the server-side proxy presenting one socket as a local
  :class:`encre.gateway.platforms.base.BasePlatformAdapter`.

The wire format is defined exactly once in
:mod:`encre.gateway.ws_bridge.protocol` and imported by both ends, so
client and server cannot drift apart.

Public API: :class:`GatewayMessage`, :class:`GatewayOp`,
:class:`WsBridgeServer`.  :class:`~encre.gateway.ws_bridge.client.GatewayClient`
and :class:`~encre.gateway.ws_bridge.remote_adapter.RemotePlatformAdapter`
are exported by their own modules; they are consumed by plugin hosts
(adapter side) and by the bridge server (proxy side) respectively, not by
the package root.
"""

from encre.gateway.ws_bridge.protocol import GatewayMessage, GatewayOp
from encre.gateway.ws_bridge.server import WsBridgeServer

__all__ = [
    "GatewayMessage",
    "GatewayOp",
    "WsBridgeServer",
]
