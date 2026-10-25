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

"""Package surface of the ea-connector-simplex connector plugin.

Attribution: inspired by the Hermes Agent project
(https://github.com/NousResearch/hermes-agent.git).

This module is the package surface of the SimpleX Chat connector plugin.
It re-exports the public symbols of the adapter submodule so callers can
write ``from ea_connector_simplex import SimplexAdapter`` without naming a
sub-module.

The adapter class implements
:class:`encre.gateway.platforms.base.BasePlatformAdapter` and is wired into
the gateway runner through the ``GatewayAdapterSpec`` declared in
:mod:`ea_connector_simplex.plugin`, which the plugin system bridges into
the platform registry at activation time.

Configuration: set ``SIMPLEX_WS_URL`` in the environment, or place
``ws_url`` under the ``simplex`` platform section of ``config.yaml``.
Optional env vars: ``SIMPLEX_ALLOWED_USERS``, ``SIMPLEX_ALLOW_ALL_USERS``,
``SIMPLEX_AUTO_ACCEPT``, ``SIMPLEX_GROUP_ALLOWED``, ``SIMPLEX_HOME_CHANNEL``,
``SIMPLEX_HOME_CHANNEL_NAME``, ``ENCRE_SIMPLEX_TEXT_BATCH_DELAY``.

Reference: https://simplex.chat/
"""

from .adapter import (
    MAX_MESSAGE_LENGTH,
    SimplexAdapter,
    check_requirements,
    validate_config,
)

# Explicit public surface for ``dir()`` and star-import consumers.
__all__ = [
    "MAX_MESSAGE_LENGTH",
    "SimplexAdapter",
    "check_requirements",
    "validate_config",
]
