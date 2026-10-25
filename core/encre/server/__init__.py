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

"""Public facade of the ``encre.server`` package.

This package owns the network-facing surface of the Python ``encre`` layer: it
is the hop between :mod:`encre.transport.ws` and the live agent runtime, so
desktop clients (and headless drivers) reach the same agent core through here
that platform adapters reach through :class:`encre.gateway.run.GatewayRunner`.

The submodules it groups:

* :mod:`encre.server.app` -- :class:`EncreServer`, the orchestrator that
  owns the managers and serves both the agent WebSocket and the HTTP admin
  surface.
* :mod:`encre.server.protocol` -- the typed wire model framing messages
  between a client and the server.
* :mod:`encre.server.session_manager` -- the live-session store and the
  ``data/sessions/<uuid>/`` on-disk state.
* :mod:`encre.server.admin` -- the operational HTTP routes.
* :mod:`encre.server.datacenter` -- data-root statistics for the desktop
  data-center page.
* :mod:`encre.server.service` -- the background daemon lifecycle wrapper.

Public API
    * :class:`EncreServer` (lazy)
    * :class:`ClientMessage`
    * :func:`parse_client_message`
    * :func:`encode_server_message`

:class:`EncreServer` is exposed lazily through PEP 562
:meth:`module.__getattr__` so importing :mod:`encre.server` does not import the
heavy gateway and runtime stack.  The desktop main process imports this package
early, before the Python backend it spawns is ready, and the frame-level
helpers are all that it needs at that point.
"""


def __getattr__(name: str):
    """Resolve :class:`EncreServer` on first access instead of at import time.

    PEP 562 lets a module define ``__getattr__`` for attribute lookups that
    would otherwise fail; that is what makes ``from encre.server import
    EncreServer`` work here while keeping :mod:`encre.server.app`
    (and therefore :mod:`encre.gateway.run`, :mod:`encre.plugins.host` and
    the native core) out of the import graph of anything that only wants the
    protocol framing.

    Args:
        name: The attribute name requested on this module.

    Returns:
        The :class:`EncreServer` class when ``name`` is ``"EncreServer"``.

    Raises:
        AttributeError: For every other name, which preserves normal
            attribute-lookup semantics including ``hasattr``.
    """
    if name == "EncreServer":
        from encre.server.app import EncreServer
        return EncreServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
from encre.server.protocol import (
    ClientMessage,
    encode_server_message,
    parse_client_message,
)

__all__ = [
    "ClientMessage",
    "EncreServer",
    "encode_server_message",
    "parse_client_message",
]
