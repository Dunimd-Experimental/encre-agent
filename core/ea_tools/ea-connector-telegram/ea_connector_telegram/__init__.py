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
Telegram platform adapter package for Encre's gateway layer.

This package supplies the complete Telegram Bot API adapter used by
:class:`encre.gateway.run.GatewayRunner` to bridge Encre's agent core
to Telegram DMs, groups, supergroups, channels, and forum topics.
The adapter implementation satisfies the
:class:`encre.gateway.platforms.base.BasePlatformAdapter` interface,
translating between Telegram's wire protocol and Encre's shared
:class:`~encre.gateway.platforms.base.MessageEvent` and
:class:`~encre.gateway.platforms.base.SendResult` model.

Public API (re-exported for the platform registry):
    - :func:`check_telegram_requirements` -- probes whether the
      ``python-telegram-bot`` SDK is importable and flips the module-level
      ``TELEGRAM_AVAILABLE`` flag so the adapter's class-level type aliases
      get rebound before instantiation.
    - ``_build_adapter`` -- factory wrapper that constructs
      :class:`TelegramAdapter` and applies post-construction notification
      mode resolution from ``config.yaml`` display settings and env vars.

Implementation modules:
    - ``adapter.py`` -- the :class:`TelegramAdapter` class and all Bot API
      send/receive logic, long-poll lifecycle, inline-keyboard handlers,
      MarkdownV2 formatting, Bot API 10.1 rich-message rendering, media
      upload/download, group/DM topic routing, and reaction lifecycle.
    - ``telegram_ids.py`` -- chat-id parsing helpers that distinguish numeric
      bot IDs (private DMs, channels, supergroups) from ``@username`` forms
      and provide stable string keys for persisted state dictionaries.
    - ``telegram_network.py`` -- proxy and DNS-over-HTTPS fallback transport
      for restricted-network environments where ``api.telegram.org`` DNS may
      be blocked, poisoned, or routed to an unreachable IP.

Data flow: incoming Telegram updates arrive via long-polling (or webhook in
cloud deployments), are mapped to :class:`MessageEvent` instances by the
adapter's ``_handle_*`` callbacks, dispatched through
:func:`encre.gateway.platforms.base.BasePlatformAdapter.handle_message` into
the gateway runner, and agent output is relayed back through
:func:`TelegramAdapter.send` or :func:`TelegramAdapter.edit_message` to the
originating chat. Outbound :class:`~encre.gateway.platforms.base.SendResult`
values are consumed by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.
"""

from .adapter import check_telegram_requirements, _build_adapter

__all__ = ["check_telegram_requirements", "_build_adapter"]
