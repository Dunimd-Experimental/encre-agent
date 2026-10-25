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

"""IRC connector plugin package: flat re-export surface.

This module is the package surface of the ea-connector-irc connector
plugin. It exposes the public symbols of the IRC adapter at a single
import level so callers can write
``from ea_connector_irc import IRCAdapter`` without naming a sub-module.

The adapter is dependency-free: the IRC protocol (RFC 1459 style) is
implemented directly on top of Python's standard-library asyncio sockets.
Inbound, the receive loop answers PING with PONG, watches for the ``001``
RPL_WELCOME reply, and routes PRIVMSG traffic into
:class:`encre.gateway.platforms.base.MessageEvent`. Outbound, agent text
is stripped of markdown, split by byte count to respect the IRC line
limit, and paced to stay clear of server flood limits.

Ownership and data flow:

* :class:`IRCAdapter` implements
  :class:`encre.gateway.platforms.base.BasePlatformAdapter` and is
  instantiated, started, and torn down by
  :class:`encre.gateway.run.GatewayRunner`.
* Inbound path: PRIVMSG protocol lines -> nick allowlist + channel
  addressing -> :class:`encre.gateway.platforms.base.MessageEvent` ->
  :class:`encre.gateway.run.GatewayRunner` -> Encre Agent Core agent.
* Outbound path: agent reply ->
  :class:`encre.gateway.stream_consumer.GatewayStreamConsumer` -> adapter
  :meth:`~encre.gateway.platforms.base.BasePlatformAdapter.send` -> paced
  PRIVMSG lines on the shared socket.

Sub-modules re-exported here:

* ``adapter`` -- the adapter: registration handshake, nick-collision
  recovery, CTCP handling, byte-aware line splitting, the readiness and
  config probes, the ``encre gateway setup`` wizard hook, and the
  ephemeral-connection standalone send hook.

Configuration: set ``IRC_SERVER``, ``IRC_NICKNAME`` and ``IRC_CHANNEL``
in the environment, or place ``server`` / ``nickname`` / ``channel``
under the ``irc`` platform section of ``config.yaml``.

Public API:
    :class:`IRCAdapter`, :func:`check_requirements`,
    :func:`validate_config`, :func:`interactive_setup`.
"""

# Adapter surface: the platform class and the readiness/config probes
# consumed by the plugin gateway-adapter spec.
from .adapter import (
    IRCAdapter,
    check_requirements,
    validate_config,
)

# Explicit public surface for ``dir()`` and star-import consumers.
__all__ = [
    "IRCAdapter",
    "check_requirements",
    "validate_config",
]
