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

"""
Raft chat protocol package for Encre's IM gateway layer.

This package is the surface of the ``ea-connector-raft`` connector plugin.
Its single adapter module, :mod:`ea_connector_raft.adapter`, implements
:class:`encre.gateway.platforms.base.BasePlatformAdapter` as a local webhook
server. It hosts an aiohttp ``POST /wake`` endpoint that the
``raft agent bridge`` process calls to wake the adapter, converts each wake
event into a :class:`~encre.gateway.platforms.base.MessageEvent`, and drives
outbound delivery through the Raft CLI over
:class:`~encre.gateway.platforms.base.SendResult` values consumed by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer`. A
``POST /activity`` endpoint plus a bounded
:class:`~ea_connector_raft.adapter.ActivityQueue` let the bridge report agent
activity back without the adapter polling Raft.

Registration is declarative: ``ea_connector_raft.plugin`` bridges a
``GatewayAdapterSpec`` into the platform registry at plugin activation, which
makes the ``raft`` platform discoverable to
:class:`encre.gateway.run.GatewayRunner`, to ``encre gateway status``, and to
the cron / send-message standalone-send paths.

Raft delivery is asynchronous by design: the adapter returns a
:class:`~encre.gateway.platforms.base.SendResult` as soon as the Raft CLI
accepts a send request, so a ``success`` result does not by itself guarantee
that the message reached the counterparty's inbox.

Public API (re-exported for the platform registry):
    - :class:`RaftAdapter` -- the adapter class registered for platform
      ``raft``.
    - :class:`ActivityQueue` -- bounded, thread-safe activity event queue.
    - :func:`check_raft_requirements` -- runtime dependency probe (aiohttp
      plus a ``raft`` executable on ``PATH``).
    - ``_build_adapter`` -- registry factory constructing :class:`RaftAdapter`.
"""

from .adapter import (
    ActivityQueue,
    RaftAdapter,
    _build_adapter,
    check_raft_requirements,
)

__all__ = [
    "ActivityQueue",
    "RaftAdapter",
    "_build_adapter",
    "check_raft_requirements",
]
