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

"""QQ Bot platform package: flat re-export surface for the QQ Bot v2 adapter.

Attribution: inspired by the Hermes Agent project
(https://github.com/NousResearch/hermes-agent.git).

This module is the package surface of the ea-connector-qqbot connector
plugin. It exposes every public symbol of the QQ Bot package at a single
import level so callers can write
``from ea_connector_qqbot import QQAdapter`` without naming a sub-module.

Ownership and data flow:

* :class:`QQAdapter` implements
  :class:`encre.gateway.platforms.base.BasePlatformAdapter` and is
  instantiated, started, and torn down by
  :class:`encre.gateway.run.GatewayRunner`.
* Inbound path: QQ Bot WebSocket Gateway dispatch -> adapter event
  handler -> :class:`encre.gateway.platforms.base.MessageEvent` ->
  :class:`encre.gateway.run.GatewayRunner` (authorization, session
  resolution) -> Encre Agent Core agent.
* Outbound path: agent reply ->
  :class:`encre.gateway.stream_consumer.GatewayStreamConsumer` -> adapter
  :meth:`~encre.gateway.platforms.base.BasePlatformAdapter.send` -> QQ
  Bot REST API at ``api.sgroup.qq.com``.

Sub-modules re-exported here:

* ``adapter`` -- the adapter: gateway connect/resume/heartbeat, event
  dispatch, message deduplication, DM and group ACL gating, REST send,
  native media upload, inline-keyboard send, and voice transcription.
* ``constants`` -- endpoint URLs, timeouts, reconnect backoff table,
  dedup windows, and the ``msg_type`` / ``file_type`` code tables
  published by the QQ Bot v2 API.
* ``utils`` -- User-Agent composition, HTTP header factory, config
  coercion.
* ``crypto`` -- AES-256-GCM bind-key generation and ciphertext
  decryption for the credential exchange.
* ``onboard`` -- synchronous scan-to-configure provisioning (create bind
  task, render QR, poll, decrypt).
* ``chunked_upload`` -- three-step chunked upload (prepare, PUT parts to
  COS, complete) for media above the ~10 MB inline-base64 ceiling.
* ``keyboards`` -- inline-keyboard payload builders, button-data parsers,
  approval request rendering, INTERACTION_CREATE decoding.

Configuration: set ``QQ_APP_ID`` and ``QQ_CLIENT_SECRET`` in the
environment, or place ``app_id`` / ``client_secret`` under the ``qqbot``
platform section of ``config.yaml``.

Public API:
    :class:`QQAdapter`, :exc:`QQCloseError`, :func:`check_qq_requirements`,
    :func:`qr_register`, :func:`build_connect_url`, :class:`BindStatus`,
    :func:`generate_bind_key`, :func:`decrypt_secret`,
    :func:`build_user_agent`, :func:`get_api_headers`, :func:`coerce_list`,
    :class:`ChunkedUploader`, :exc:`UploadDailyLimitExceededError`,
    :exc:`UploadFileTooLargeError`, :class:`InlineKeyboard`,
    :class:`ApprovalRequest`, :class:`ApprovalSender`,
    :class:`InteractionEvent`, :func:`build_approval_keyboard`,
    :func:`build_approval_text`, :func:`build_update_prompt_keyboard`,
    :func:`parse_approval_button_data`, :func:`parse_update_prompt_button_data`,
    :func:`parse_interaction_event`.

Reference: https://bot.q.qq.com/wiki/develop/api-v2/
"""

# Adapter surface: the platform class, its transport-level close exception,
# the dependency checker consumed by the platform registry, and the config
# coercion helper that sibling modules import from this package level.
from .adapter import (  # noqa: F401
    QQAdapter,
    QQCloseError,
    check_qq_requirements,
    _coerce_list,
)

# Onboard surface: task creation and polling are collapsed into one blocking
# call that resolves to decrypted bot credentials plus the scanner's openid.
from .onboard import (  # noqa: F401
    BindStatus,
    build_connect_url,
    qr_register,
)
# AES-256-GCM primitives that back the credential exchange above.
from .crypto import decrypt_secret, generate_bind_key  # noqa: F401

# Transport surface: User-Agent, header factory, and config coercion, shared
# by both the adapter and the onboard HTTP helpers.
from .utils import build_user_agent, get_api_headers, coerce_list  # noqa: F401

# Upload surface: the chunked upload driver and the two user-facing failure
# types the adapter turns into caller-visible error strings.
from .chunked_upload import (  # noqa: F401
    ChunkedUploader,
    UploadDailyLimitExceededError,
    UploadFileTooLargeError,
)

# Keyboard surface: the inline-keyboard data model, approval and update-prompt
# builders, button-data parsers, interaction decoder, and the sender helper.
from .keyboards import (  # noqa: F401
    ApprovalRequest,
    ApprovalSender,
    InlineKeyboard,
    InteractionEvent,
    build_approval_keyboard,
    build_approval_text,
    build_update_prompt_keyboard,
    parse_approval_button_data,
    parse_interaction_event,
    parse_update_prompt_button_data,
)

# Explicit public surface for ``dir()`` and star-import consumers.
__all__ = [
    # adapter
    "QQAdapter",
    "QQCloseError",
    "check_qq_requirements",
    "_coerce_list",
    # onboard
    "BindStatus",
    "build_connect_url",
    "qr_register",
    # crypto
    "decrypt_secret",
    "generate_bind_key",
    # utils
    "build_user_agent",
    "get_api_headers",
    "coerce_list",
    # chunked upload
    "ChunkedUploader",
    "UploadDailyLimitExceededError",
    "UploadFileTooLargeError",
    # keyboards
    "ApprovalRequest",
    "ApprovalSender",
    "InlineKeyboard",
    "InteractionEvent",
    "build_approval_keyboard",
    "build_approval_text",
    "build_update_prompt_keyboard",
    "parse_approval_button_data",
    "parse_interaction_event",
    "parse_update_prompt_button_data",
]
