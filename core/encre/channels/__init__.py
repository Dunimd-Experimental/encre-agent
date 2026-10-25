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

"""Channel package: the interface between a client surface and the agent runtime.

A *channel* is a transport surface that bridges one client endpoint -- a
terminal, a WebSocket peer, an HTTP client -- to the shared agent runtime.
Each channel owns the framing in one direction (bytes on the wire) and
hands decoded requests to the router, which hands response events back for
the channel to serialise.

This package is deliberately small. It holds only the channel *interface*,
the in-process terminal implementation, and a re-export of the router. The
two production transports live in :mod:`encre.transport`:
The unified encrypted WebSocket carries the
protocol to the Electron renderer and to headless drivers, and
:class:`encre.transport.http.channel.HTTPChannel` serves the HTTP/OpenAI
surface. They are not re-exported here so that importing
``encre.channels`` never pulls the websocket stack for a consumer that only
needs the ABC.

Router placement: :class:`encre.protocol.session_router.EventRouter` is the
one component that maps an inbound message to an agent session and back. It
is defined in :mod:`encre.protocol.session_router` alongside
:class:`encre.protocol.session_router.SessionRouter` and re-exported from
this package so that ``from encre.channels import EventRouter`` keeps
working for long-standing call sites. The ``encre.channels`` path and the
``encre.protocol.session_router`` path refer to the same class object.

Data flow through a channel: client bytes -> channel decodes ->
:mod:`encre.protocol.session_router` resolves a session key -> agent
executes (Encre Agent Core) -> response events stream back -> channel encodes ->
client.

Public API:
    :class:`Channel` -- abstract channel interface.
    :class:`EventRouter` -- multi-session router re-exported from
        :mod:`encre.protocol.session_router`.
    :class:`TerminalChannel` -- interactive stdin/stdout REPL; retained for
        compatibility and now deprecated.
"""

from encre.channels.base import Channel
from encre.channels.terminal import TerminalChannel
from encre.protocol.session_router import EventRouter

__all__ = [
    "Channel",
    "EventRouter",
    "TerminalChannel",
]
