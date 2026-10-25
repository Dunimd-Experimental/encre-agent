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

"""Transport layer: the wire surfaces that carry the Encre client protocol.

This package owns the transports for the server's OWN endpoints -- the sockets
and HTTP servers an Encre process listens on so that clients can reach the
agent runtime.  Every transport here implements the shared
:class:`encre.channels.base.Channel` contract, which is what lets
:class:`encre.server.app.EncreServer` construct one
:class:`encre.protocol.session_router.EventRouter` and share it across several
channels.  The router turns a prompt into an
:class:`~encre.utils.types.AgentEvent` stream; this package owns only the
framing around that stream, never the run itself.

Each transport follows the same three-step loop the base contract defines:
read and frame client bytes, dispatch the decoded request to the router, then
encode the returned events and write them back.  The generic event model that
crosses the wire is :mod:`encre.protocol`; the typed client/server message
dataclasses that the Electron renderer speaks are
:mod:`encre.server.protocol`.

    * :mod:`encre.transport.ws` -- :class:`EncreWSHandler`, the per-connection
      WebSocket handler that parses each frame with :mod:`encre.server.protocol`
      and dispatches it to the domain handlers mixed in from
      :mod:`encre.protocol.handlers`.  It carries the full typed desktop protocol,
      including the optional AES-256-GCM frame encryption.
      the standalone RFC 6455 channel: one agent session per connection, plain
      JSON frames, no encryption layer.
    * :mod:`encre.transport.http` -- :class:`~encre.transport.http.channel.HTTPChannel`
      for request/response REST plus NDJSON streaming, and the OpenAI-compatible
      API surface in :mod:`encre.transport.http.openai_api`.

Gateway PLATFORM adapters (telegram, discord, wechat, ...) are deliberately NOT
part of this package.  They are outbound bridges to third-party IM vendors, not
client endpoints of this server; they live under
:mod:`encre.gateway.platforms` and follow each vendor's own protocol.

Public API:
    EncreWSHandler -- the desktop WebSocket message handler.
    ``HTTPChannel`` is not re-exported from here.  Import it from
    :mod:`encre.transport.http`, which keeps this package's import list to the
    two WebSocket surfaces the desktop path always needs and lets the HTTP
    stack be pulled in only by callers that actually use it.
"""

from encre.transport.ws import EncreWSHandler

__all__ = [
    "EncreWSHandler",
]
