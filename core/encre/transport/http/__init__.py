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

"""HTTP transport stack for the server's own endpoints.

:mod:`encre.transport.http` holds the HTTP-based surfaces of
:class:`encre.channels.base.Channel`.  Because HTTP has no persistent channel
of its own, the package splits into two shapes that differ in how they borrow
one:

    * :mod:`encre.transport.http.channel` -- :class:`HTTPChannel`, the
      stdlib-only request/response channel.  One TCP connection carries exactly
      one transaction, so the client must name the conversation itself via
      ``session_id`` rather than inheriting it from the connection.  ``POST
      /chat`` returns either the whole reply as JSON or an NDJSON stream of
      deltas, and the ``/sessions`` endpoints mirror the session-management
      actions the desktop client performs over WebSocket.
    * :mod:`encre.transport.http.openai_api` -- the OpenAI-compatible API
      server adapter, built on ``aiohttp``.  It presents
      ``/v1/chat/completions``, ``/v1/responses``, ``/v1/runs`` (SSE),
      ``/v1/models`` and ``/v1/capabilities`` so third-party front ends can
      drive the agent without speaking Encre's own protocol, and exposes the
      same agent sessions again under ``/api/sessions``.  It authenticates with
      ``API_SERVER_KEY`` and self-registers into the gateway platform registry
      under the platform name ``"api_server"`` the moment it is imported, which
      :func:`encre.gateway.platforms.discover_platforms` does by module path.

The adapter is deliberately NOT imported here.  It requires ``aiohttp``, an
optional extra, while :class:`HTTPChannel` is built on
:func:`asyncio.start_server` and needs nothing beyond the standard library.
Importing the adapter from this package would turn an optional dependency into
a hard requirement for every consumer of :class:`HTTPChannel`.

Data flow: :class:`HTTPChannel` parses the request and hands the prompt to
:meth:`encre.protocol.session_router.EventRouter.submit` or
:meth:`~encre.protocol.session_router.EventRouter.submit_stream`, tagged with
``channel.name == "http_api"``; events come back over the same writer.  The
adapter keeps a separate aiohttp application for its own request handling but
submits runs through the same router and session manager, so a session created
over ``/v1/chat/completions`` and one created over WebSocket are the same
record on disk.

Public API:
    HTTPChannel -- the REST / NDJSON channel.
"""

from encre.transport.http.channel import HTTPChannel

__all__ = [
    "HTTPChannel",
]
