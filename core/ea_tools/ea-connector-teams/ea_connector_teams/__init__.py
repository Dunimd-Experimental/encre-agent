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

"""Package surface of the ea-connector-teams connector plugin.

This module is the package surface of the Microsoft Teams connector plugin.
It re-exports the public symbols of the adapter submodule so callers can
write ``from ea_connector_teams import TeamsAdapter`` without naming a
sub-module.

The adapter binds Encre Agent to Microsoft Teams through the Microsoft Bot
Framework connection handler webhook, where Teams pushes conversation
activity events and Encre replies as activity messages and adaptive cards.
It implements :class:`encre.gateway.platforms.base.BasePlatformAdapter` and
is wired into the gateway runner through the ``GatewayAdapterSpec`` declared
in :mod:`ea_connector_teams.plugin`, which the plugin system bridges into
the platform registry at activation time.

Configuration: set ``TEAMS_CLIENT_ID``, ``TEAMS_CLIENT_SECRET`` and
``TEAMS_TENANT_ID`` in the environment, or place ``client_id``,
``client_secret`` and ``tenant_id`` under the ``teams`` platform section of
``config.yaml``. Optional env vars: ``TEAMS_PORT``, ``TEAMS_SERVICE_URL``,
``TEAMS_HOME_CHANNEL``, ``TEAMS_ALLOWED_USERS``, ``TEAMS_ALLOW_ALL_USERS``,
``TEAMS_DELIVERY_MODE``, ``TEAMS_INCOMING_WEBHOOK_URL``,
``TEAMS_GRAPH_ACCESS_TOKEN``, ``TEAMS_TEAM_ID``, ``TEAMS_CHANNEL_ID``,
``TEAMS_CHAT_ID``.

Reference: https://learn.microsoft.com/en-us/microsoftteams/platform/bots/what-are-bots
"""

from .adapter import (
    TeamsAdapter,
    check_requirements,
    validate_config,
)

# Explicit public surface for ``dir()`` and star-import consumers.
__all__ = [
    "TeamsAdapter",
    "check_requirements",
    "validate_config",
]
