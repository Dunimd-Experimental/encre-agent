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

"""Domain handler mixins for the Encre WebSocket protocol.

Every module in this package is a mixin class that owns exactly one
client-message domain of the Encre protocol.  No module here owns a
connection, parses frames, or formats JSON: it supplies only ``_h_*``
methods (one per client message type it owns) plus the private snapshot
builders those methods need, and it assumes the shared state and helpers
declared in :class:`~encre.protocol.handlers.base.HandlerBase` are already
present on ``self``.

The composition happens in :mod:`encre.transport.ws`, where
:class:`encre.transport.ws.EncreWSHandler` inherits from
:class:`~encre.protocol.handlers.base.HandlerBase` and every domain mixin in
one MRO, then declares the ``_DISPATCH`` table that maps a parsed
``Client*`` message type from :mod:`encre.server.protocol` to the owning
unbound method (``ChatHandlers._h_cancel``, ``SkillsHandlers._h_install_skill``,
...).  The table is the single source of truth for routing, so a domain owns a
message if and only if the table points at one of its classes -- the mixin
inheritance order is only there to place the methods on the handler's MRO.

Modules:

- :mod:`~encre.protocol.handlers.base` -- per-connection state, the single
  ``_send`` frame path, session bootstrap, and the unified full-data
  snapshot push.  The shared foundation, not a domain.
- :mod:`~encre.protocol.handlers.sessions` / ``run`` / ``workspaces`` /
  ``settings`` -- conversation list and lifecycle, agent-run submission and
  retry, workspace registration and indexing, config/model/MCP/agent
  surfaces.
- :mod:`~encre.protocol.handlers.chat` -- interactive control of an in-flight
  turn: question answers, cancel, tool stop, mid-turn steering, and
  message edit/delete with rollback commits.
- :mod:`~encre.protocol.handlers.permissions` -- the interactive consent
  surface: permission, plan, spec and mode/slash-command approvals.
- :mod:`~encre.protocol.handlers.skills` -- user-skill lifecycle and the
  encrypted skill index.
- :mod:`~encre.protocol.handlers.history` -- conversation search, rollback
  log/checkout, branching, and telemetry replay.
- :mod:`~encre.protocol.handlers.profile` -- settings-panel data planes:
  memory, rules, hooks, documents, usage stats.
- :mod:`~encre.protocol.handlers.automation` -- scheduled-job CRUD,
  execution history, and the scheduler callback broadcasts.
- :mod:`~encre.protocol.handlers.terminal` -- the per-connection embedded
  terminal: shell discovery, spawn, stdin write, kill.
- :mod:`~encre.protocol.handlers.events` -- the inbound-to-wire bridge that
  turns agent events into server frames; composed last because it is a
  producer path, not a client-message domain.

Data flow for a request: :mod:`encre.server.protocol` decodes the frame into
a typed ``Client*`` instance, the ``handle`` loop resolves
``_DISPATCH[type(msg)]``, and the owning mixin method drives the request.
From there the data plane runs through
:class:`encre.server.session_manager.SessionManager` -- the shared store of
:class:`~encre.server.session_manager.SessionInfo` records, each wrapping an
:class:`~encre.agent.EncreAgent` whose ``loop`` is the Encre Agent Core agent loop
-- and results come back out through
:meth:`encre.protocol.handlers.base.HandlerBase._send`.  Mutating handlers
touch the session (``self._manager.touch``) so idle cleanup keeps the entry
warm, and persist through ``self._manager._save_session_async`` so a crash
never loses the turn that was just committed.

Package layout is namespace-style: there is no re-export here, so callers
import the concrete mixin module by full path.
"""
