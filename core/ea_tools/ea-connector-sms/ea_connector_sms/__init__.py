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

"""Package surface of the ea-connector-sms connector plugin.

This module is the package surface of the SMS (Twilio) connector plugin.
It re-exports the public symbols of the adapter submodule so callers can
write ``from ea_connector_sms import SmsAdapter`` without naming a
sub-module.

The adapter bridges the cellular SMS network into Encre Agent through a
Twilio account: it opens a local HTTP webhook for inbound deliveries and
calls the Twilio REST API for outbound sends. It implements
:class:`encre.gateway.platforms.base.BasePlatformAdapter` and is wired into
the gateway runner through the ``GatewayAdapterSpec`` declared in
:mod:`ea_connector_sms.plugin`, which the plugin system bridges into the
platform registry at activation time.

Configuration: set ``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN`` and
``TWILIO_PHONE_NUMBER`` in the environment. Gateway-side settings:
``SMS_WEBHOOK_PORT``, ``SMS_WEBHOOK_HOST``, ``SMS_WEBHOOK_URL``,
``SMS_INSECURE_NO_SIGNATURE``, ``SMS_ALLOWED_USERS``,
``SMS_ALLOW_ALL_USERS``, ``SMS_HOME_CHANNEL``.
"""

from .adapter import (
    MAX_SMS_LENGTH,
    SmsAdapter,
    check_sms_requirements,
)

# Explicit public surface for ``dir()`` and star-import consumers.
__all__ = [
    "MAX_SMS_LENGTH",
    "SmsAdapter",
    "check_sms_requirements",
]
