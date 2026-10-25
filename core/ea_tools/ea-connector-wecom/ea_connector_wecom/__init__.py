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
WeCom (Enterprise WeChat) platform package for the Encre gateway layer.

This package is the implementation surface of the ``ea-connector-wecom``
connector plugin. It holds everything Encre needs to talk to WeCom. It
contains two adapters that expose the same vendor through two different
integration modes, plus the shared crypto satellite that one of them
depends on:

* :mod:`ea_connector_wecom.adapter` -- :class:`WeComAdapter`, the
  WebSocket adapter for WeCom AI Bots speaking ``aibot_*`` JSON frames.
  WeCom appears in the adapter registry once the plugin is activated:
  :mod:`ea_connector_wecom.plugin` bridges the adapter spec through the
  gateway-adapter surface at activation time. The adapter module
  additionally carries the QR-code credential wizard
  (:func:`ea_connector_wecom.adapter.qr_scan_for_bot_info`) plus the
  standalone out-of-process sender used by cron-style delivery.
* :mod:`ea_connector_wecom.callback_adapter` --
  :class:`WecomCallbackAdapter`, the callback-mode adapter that serves WeCom's
  encrypted XML callbacks on a local aiohttp HTTP endpoint and delivers agent
  replies through WeCom's proactive ``message/send`` HTTP API.
* :mod:`ea_connector_wecom.wecom_crypto` -- :class:`WXBizMsgCrypt`,
  a BizMsgCrypt-compatible AES-256-CBC envelope codec with SHA-1 signature
  verification. Only the callback adapter imports it; AI Bot mode uses a
  separate JSON-based path and never touches this module.

Data flow: :class:`encre.gateway.run.GatewayRunner` builds one adapter per
configured :class:`encre.gateway.config.PlatformConfig`, the adapter
authorises inbound vendor traffic and normalises it into
:class:`encre.gateway.platforms.base.MessageEvent` objects, the agent executes,
and agent output is pushed back through the adapter's ``send`` family. Those
:class:`~encre.gateway.platforms.base.SendResult` values are consumed by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.

Public API:

* :data:`adapter` -- re-exported module object, kept for
  ``from ea_connector_wecom import adapter`` imports.
"""

from . import adapter
