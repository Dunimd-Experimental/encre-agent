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

"""Email connector plugin package: flat re-export surface for the Email adapter.

Attribution: inspired by the Hermes Agent project
(https://github.com/NousResearch/hermes-agent.git).

This module is the package surface of the ea-connector-email connector
plugin. It exposes the public symbols of the Email adapter at a single
import level so callers can write
``from ea_connector_email import EmailAdapter`` without naming a sub-module.

Ownership and data flow:

* :class:`EmailAdapter` implements
  :class:`encre.gateway.platforms.base.BasePlatformAdapter` and is
  instantiated, started, and torn down by
  :class:`encre.gateway.run.GatewayRunner`.
* Inbound path: IMAP INBOX poll (unseen messages) -> sender
  authentication (SPF/DKIM/DMARC via Authentication-Results) -> allowlist
  gating -> :class:`encre.gateway.platforms.base.MessageEvent` ->
  :class:`encre.gateway.run.GatewayRunner` (authorization, session
  resolution) -> Encre Agent Core agent.
* Outbound path: agent reply ->
  :class:`encre.gateway.stream_consumer.GatewayStreamConsumer` -> adapter
  :meth:`~encre.gateway.platforms.base.BasePlatformAdapter.send` -> TLS
  SMTP delivery with In-Reply-To/References threading.

Sub-modules re-exported here:

* ``adapter`` -- the adapter: IPv4-first SMTP connect (implicit TLS on
  port 465, STARTTLS otherwise), IMAP polling with UID deduplication,
  automated-sender filtering, attachment caching, MIME send helpers, and
  the standalone SMTP delivery hook.

Configuration: set ``EMAIL_ADDRESS`` and ``EMAIL_PASSWORD`` in the
environment, or place ``address`` / ``imap_host`` / ``smtp_host`` under the
``email`` platform section of ``config.yaml``.

Public API:
    :class:`EmailAdapter`, :data:`MAX_MESSAGE_LENGTH`,
    :func:`check_email_requirements`.

Reference: RFC 2971 (IMAP ID), GHSA-rxqh-5572-8m77 (From: spoofing).
"""

# Adapter surface: the platform class, its per-message size cap, and the
# dependency/config probe consumed by the plugin gateway-adapter spec.
from .adapter import (
    MAX_MESSAGE_LENGTH,
    EmailAdapter,
    check_email_requirements,
)

# Explicit public surface for ``dir()`` and star-import consumers.
__all__ = [
    "MAX_MESSAGE_LENGTH",
    "EmailAdapter",
    "check_email_requirements",
]
