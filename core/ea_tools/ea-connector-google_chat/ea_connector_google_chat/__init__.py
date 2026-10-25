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

"""Google Chat connector plugin package: flat re-export surface.

This module is the package surface of the ea-connector-google_chat
connector plugin. It exposes the public symbols of the Google Chat
adapter at a single import level so callers can write
``from ea_connector_google_chat import GoogleChatAdapter`` without naming
a sub-module.

The package owns one adapter: Google Chat (Chat API v1), implemented by
:class:`GoogleChatAdapter`, plus the per-user OAuth helper
:mod:`.oauth` that backs native attachment delivery (Google Chat's
``media.upload`` endpoint rejects service-account authentication
outright, so files are uploaded as the requesting user).

Ownership and data flow:

* :class:`GoogleChatAdapter` implements
  :class:`encre.gateway.platforms.base.BasePlatformAdapter` and is
  instantiated, started, and torn down by
  :class:`encre.gateway.run.GatewayRunner`.
* Inbound path: Cloud Pub/Sub stream (or an HTTP callback carrying a
  verified Google ID token) -> ``_on_pubsub_message`` /
  ``dispatch_http_event`` -> dedup -> asyncio loop ->
  :class:`encre.gateway.platforms.base.MessageEvent` ->
  :class:`encre.gateway.run.GatewayRunner` -> Encre Agent Core agent.
* Outbound path: agent reply ->
  :class:`encre.gateway.stream_consumer.GatewayStreamConsumer` -> adapter
  ``send()`` / ``edit_message()`` / ``send_card()`` / media variants ->
  Google Chat REST calls executed through ``asyncio.to_thread``.

Importing :mod:`.adapter` eagerly resolves the dynamic
``Platform("google_chat")`` pseudo-member, so ``Platform.GOOGLE_CHAT``
works before any adapter instance exists.

Sub-modules re-exported here:

* ``adapter`` -- the adapter: Pub/Sub + HTTP-callback ingest, retry with
  bounded backoff, typing-marker lifecycle, cardsV2 rendering, attachment
  download/upload, and the SDK-free standalone send hook.
* ``oauth`` -- intentionally NOT re-exported: it is consumed directly as
  ``from .oauth import ...`` inside ``adapter.py``, and its ``main()``
  CLI entrypoint is reachable through the module path.

Configuration: set ``GOOGLE_CHAT_*`` environment variables or place the
service-account JSON under the ``google_chat`` platform section of
``config.yaml``.

Public API:
    :class:`GoogleChatAdapter`, :func:`check_google_chat_requirements`,
    :func:`card_spec_to_cards_v2`.

Reference: https://developers.google.com/chat
"""

# Adapter surface: the platform class, the optional-dependency probe, and
# the card-spec converter consumed by the plugin gateway-adapter spec.
from .adapter import (
    GoogleChatAdapter,
    card_spec_to_cards_v2,
    check_google_chat_requirements,
)

# Explicit public surface for ``dir()`` and star-import consumers.
__all__ = [
    "GoogleChatAdapter",
    "card_spec_to_cards_v2",
    "check_google_chat_requirements",
]
