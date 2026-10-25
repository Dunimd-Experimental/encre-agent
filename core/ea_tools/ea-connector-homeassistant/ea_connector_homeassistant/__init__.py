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

"""Home Assistant connector plugin package: flat re-export surface.

This module is the package surface of the ea-connector-homeassistant
connector plugin. It exposes the public symbols of the Home Assistant
adapter at a single import level so callers can write
``from ea_connector_homeassistant import HomeAssistantAdapter`` without
naming a sub-module.

The adapter treats Home Assistant as a bidirectional local-device
channel: the inbound leg holds a WebSocket to ``/api/websocket`` and
watches ``state_changed`` events (domain/entity allow-list, deny-list,
per-entity cooldown), while the outbound leg posts persistent
notifications on a separate REST session so a write never contends with
the socket being read.

Ownership and data flow:

* :class:`HomeAssistantAdapter` implements
  :class:`encre.gateway.platforms.base.BasePlatformAdapter` and is
  instantiated, started, and torn down by
  :class:`encre.gateway.run.GatewayRunner`.
* Inbound path: ``state_changed`` WebSocket events -> filter chain ->
  :class:`encre.gateway.platforms.base.MessageEvent` ->
  :class:`encre.gateway.run.GatewayRunner` -> Encre Agent Core agent.
* Outbound path: agent reply ->
  :class:`encre.gateway.stream_consumer.GatewayStreamConsumer` -> adapter
  :meth:`~encre.gateway.platforms.base.BasePlatformAdapter.send` ->
  ``persistent_notification/create`` REST service.

Sub-modules re-exported here:

* ``adapter`` -- the adapter: WebSocket handshake/subscribe, reconnect
  backoff ladder, event filtering and prose rendering, REST send,
  standalone ``notify.notify`` delivery hook, and the config probes.

Configuration: set ``HASS_TOKEN`` (long-lived access token) and optionally
``HASS_URL`` in the environment, or place ``token`` / ``url`` under the
``homeassistant`` platform section of ``config.yaml``.

Public API:
    :class:`HomeAssistantAdapter`, :func:`check_ha_requirements`,
    :func:`validate_ha_config`.

Reference: https://www.home-assistant.io/docs/authentication/
"""

# Adapter surface: the platform class and the dependency/config probes
# consumed by the plugin gateway-adapter spec.
from .adapter import (
    HomeAssistantAdapter,
    check_ha_requirements,
    validate_ha_config,
)

# Explicit public surface for ``dir()`` and star-import consumers.
__all__ = [
    "HomeAssistantAdapter",
    "check_ha_requirements",
    "validate_ha_config",
]
