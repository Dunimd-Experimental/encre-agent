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

"""Weixin (WeChat) platform package: flat re-export surface for the iLink adapter.

Attribution: inspired by the Hermes Agent project
(https://github.com/NousResearch/hermes-agent.git).

This module is the package surface of the ``ea-connector-weixin`` connector
plugin. It exposes the public symbols of the Weixin adapter module at a
single import level so callers can write
``from ea_connector_weixin import WeixinAdapter`` without naming a
sub-module.

Ownership and data flow:

* :class:`WeixinAdapter` implements
  :class:`encre.gateway.platforms.base.BasePlatformAdapter` and is
  instantiated, started, and torn down by
  :class:`encre.gateway.run.GatewayRunner`.
* Inbound path: iLink ``ilink/bot/getupdates`` long poll -> adapter event
  handler -> :class:`encre.gateway.platforms.base.MessageEvent` ->
  :class:`encre.gateway.run.GatewayRunner` (authorization, session
  resolution) -> Encre Agent Core agent.
* Outbound path: agent reply ->
  :class:`encre.gateway.stream_consumer.GatewayStreamConsumer` -> adapter
  :meth:`~encre.gateway.platforms.base.BasePlatformAdapter.send` ->
  ``ilink/bot/sendmessage``, with media through ``ilink/bot/getuploadurl``
  plus an AES-128-ECB encrypted POST to the WeChat CDN.

Sub-modules re-exported here:

* ``weixin`` -- the adapter: iLink long-poll transport, message mapping and
  deduplication, context-token persistence, typing tickets, media
  encryption/transfer, and the QR-code pairing flow.

Configuration: set ``WEIXIN_BOT_TOKEN`` in the environment, or configure
the ``weixin`` platform section of ``config.yaml``. Personal-account
credentials persist under the data directory managed by
:func:`encre.config.get_data_dir`.

Public API:
    :class:`WeixinAdapter`, :class:`ContextTokenStore`,
    :class:`TypingTicketCache`, :func:`check_weixin_requirements`,
    :func:`save_weixin_account`, :func:`load_weixin_account`,
    :func:`atomic_json_write`, :func:`_check_requirements`.

Reference: https://www.wechatbot.dev/zh/protocol
"""

# Adapter surface: the platform class, its supporting stores, the dependency
# checkers, and the account persistence helpers.
from .weixin import (
    ContextTokenStore,
    TypingTicketCache,
    WeixinAdapter,
    _check_requirements,
    atomic_json_write,
    check_weixin_requirements,
    load_weixin_account,
    save_weixin_account,
)

# Explicit public surface for ``dir()`` and star-import consumers.
__all__ = [
    "ContextTokenStore",
    "TypingTicketCache",
    "WeixinAdapter",
    "_check_requirements",
    "atomic_json_write",
    "check_weixin_requirements",
    "load_weixin_account",
    "save_weixin_account",
]
