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
LINE Messaging API package for Encre's IM gateway layer.

This package is the surface of the ``ea-connector-line`` connector plugin.
Its single adapter module, :mod:`ea_connector_line.adapter`, implements
:class:`encre.gateway.platforms.base.BasePlatformAdapter` for the LINE
Messaging API. It runs an aiohttp webhook endpoint that verifies every
delivery with ``X-Line-Signature`` (HMAC-SHA256 keyed by the channel secret),
converts LINE webhook events into
:class:`~encre.gateway.platforms.base.MessageEvent` objects, and ships agent
output back over the reply and push endpoints, whose
:class:`~encre.gateway.platforms.base.SendResult` values are consumed by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.

Registration is declarative: ``ea_connector_line.plugin`` bridges a
``GatewayAdapterSpec`` into the platform registry at plugin activation, which
makes the ``line`` platform discoverable to
:class:`encre.gateway.run.GatewayRunner`, to ``encre gateway status``, and to
the cron / send-message standalone-send paths.

The adapter is deliberately self-contained: it needs only ``aiohttp`` at
runtime, reads its credentials from ``LINE_CHANNEL_ACCESS_TOKEN`` and
``LINE_CHANNEL_SECRET`` (or ``config.yaml`` ``platforms.line.extra``), and
exposes ``check_requirements``, ``validate_config``, ``is_connected``,
``_env_enablement``, ``_standalone_send``, ``_build_adapter``,
``interactive_setup`` and ``_apply_yaml_config``-style hooks under the names
the gateway core probes.

Public API (re-exported for the platform registry):
    - :class:`LineAdapter` -- the adapter class registered for platform
      ``line``.
    - :func:`check_requirements` -- module-import / baseline dependency probe.
    - :func:`validate_config` -- config-shape probe.
    - ``_build_adapter`` -- registry factory constructing :class:`LineAdapter`.
"""

from .adapter import (
    LineAdapter,
    _build_adapter,
    check_requirements,
    validate_config,
)

__all__ = [
    "LineAdapter",
    "_build_adapter",
    "check_requirements",
    "validate_config",
]
