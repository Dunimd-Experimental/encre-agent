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

"""Core-side protocol layer for the Encre WebSocket server.

This package holds everything that translates between the wire protocol
spoken by the Electron/React desktop renderer (and any headless driver that
connects over the same socket) and the live agent runtime.  It sits in the
Python ``encre`` layer, one level above the Encre Agent Core that actually executes
agent turns and below :mod:`encre.transport`, which owns the raw WebSocket
frames.

Contents:

- :mod:`encre.protocol.handlers` -- one mixin per client-message domain
  (sessions, run, chat, permissions, skills, history, workspaces, profile,
  automation, terminal, settings) plus the shared
  :class:`~encre.protocol.handlers.base.HandlerBase` and the agent-event
  bridge :mod:`~encre.protocol.handlers.events`.  Each mixin is inherited
  by :class:`encre.transport.ws.EncreWSHandler` and its methods are wired
  into that handler's ``_DISPATCH`` table, so a client message type maps to
  exactly one owning domain.
- :mod:`encre.protocol.render` -- turns a stored session's raw message list
  into the renderer-shaped payloads (per-branch message view, tool-call and
  thought rendering) that the settings, history and profile panels consume.
  Kept out of the domain handlers so every panel renders identically.
- :mod:`encre.protocol.session_router` -- routes per-session agent events to
  the WebSocket connection(s) that are attached to that session.

Data flow: an incoming frame is decoded by :mod:`encre.server.protocol`
into a typed ``Client*`` message, looked up in ``_DISPATCH``, and executed by
the owning domain handler.  That handler reaches down into
:class:`encre.server.session_manager.SessionManager` (the single store of
:class:`~encre.server.session_manager.SessionInfo` records, each wrapping an
:class:`~encre.agent.EncreAgent` around the Encre Agent Core loop) and writes back
through :meth:`HandlerBase._send`, the only frame write path.  Outbound
agent events travel the reverse way: the agent loop emits a typed
:class:`encre.utils.types.AgentEvent`, the router hands it to
:meth:`encre.protocol.handlers.events.EventDispatchMixin._dispatch_event`,
which projects it into a server frame and applies the server-side side
effects it implies.

Public API:

- :mod:`encre.protocol.handlers`
- :mod:`encre.protocol.render`
- :mod:`encre.protocol.session_router`
"""
