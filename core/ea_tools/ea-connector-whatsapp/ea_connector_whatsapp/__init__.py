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

"""Package surface of the ea-connector-whatsapp connector plugin.

This module is the package surface of the WhatsApp connector plugin. It
re-exports the public symbols of the adapter submodules so callers can
write ``from ea_connector_whatsapp import WhatsAppAdapter`` without naming
a sub-module.

The adapter connects to WhatsApp through a Node.js bridge subprocess
(whatsapp-web.js / Baileys) and implements
:class:`encre.gateway.platforms.base.BasePlatformAdapter`. It is wired into
the gateway runner through the ``GatewayAdapterSpec`` declared in
:mod:`ea_connector_whatsapp.plugin`, which the plugin system bridges into
the platform registry at activation time.

Sub-modules:

* ``whatsapp`` -- the adapter: bridge process lifecycle, health/staleness
  handshake, media handling, and outbound delivery.
* ``whatsapp_common`` -- transport-agnostic WhatsApp behavior shared by the
  bridge adapter and Cloud API variants (allow-list/DM/group gating, mention
  detection, markdown conversion, chunk budgeting, bridge dir resolution).
* ``whatsapp_identity`` -- same shared behavior surface, kept alongside the
  adapter for parity with the built-in layout; the shared JID/identity
  helpers remain in :mod:`encre.gateway.whatsapp_identity`.

Configuration: set ``WHATSAPP_ENABLED=true`` in the environment. Optional
env vars: ``WHATSAPP_MODE``, ``WHATSAPP_ALLOWED_USERS``,
``WHATSAPP_ALLOW_ALL_USERS``, ``WHATSAPP_GROUP_POLICY``,
``WHATSAPP_GROUP_ALLOWED_USERS``, ``WHATSAPP_REQUIRE_MENTION``,
``WHATSAPP_MENTION_PATTERNS``, ``WHATSAPP_FREE_RESPONSE_CHATS``,
``WHATSAPP_REPLY_PREFIX``.

Requires a Node.js runtime for the bridge subprocess.
"""

from .whatsapp import (
    WhatsAppAdapter,
    check_whatsapp_requirements,
)
from .whatsapp_common import WhatsAppBehaviorMixin

# Explicit public surface for ``dir()`` and star-import consumers.
__all__ = [
    "WhatsAppAdapter",
    "WhatsAppBehaviorMixin",
    "check_whatsapp_requirements",
]
