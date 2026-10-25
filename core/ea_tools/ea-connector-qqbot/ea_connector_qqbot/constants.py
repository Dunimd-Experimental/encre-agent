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

"""Shared constants for the Encre QQ Bot platform adapter.

Attribution: inspired by the Hermes Agent project
(https://github.com/NousResearch/hermes-agent.git).

Every value in this module is a module-level constant derived either from
the QQ Bot v2 wire protocol or from operational tuning knobs. The module
imports only :mod:`os`, which keeps it free of any dependency on sibling
sub-modules. That matters: :mod:`ea_connector_qqbot.crypto` and
:mod:`ea_connector_qqbot.utils` are reached from this module's
importers, and :mod:`ea_connector_qqbot.onboard` imports this
module at module load time. Adding a cyclic import here would break the
whole package.

Importers:

* :mod:`ea_connector_qqbot.adapter` -- consumes every constant
  except the onboard and QR URL values.
* :mod:`ea_connector_qqbot.chunked_upload` -- consumes
  :const:`FILE_UPLOAD_TIMEOUT`.
* :mod:`ea_connector_qqbot.onboard` -- consumes the portal
  host, onboard paths, poll interval, onboard timeout, and QR URL template.
* :mod:`ea_connector_qqbot.utils` -- consumes
  :const:`QQBOT_VERSION` for the User-Agent string.

Constant groups:

* Adapter version (:const:`QQBOT_VERSION`).
* Endpoints (:const:`PORTAL_HOST`, :const:`API_BASE`, :const:`TOKEN_URL`,
  :const:`GATEWAY_URL_PATH`, :const:`ONBOARD_CREATE_PATH`,
  :const:`ONBOARD_POLL_PATH`, :const:`QR_URL_TEMPLATE`).
* Timeouts and retry tuning.
* Message length and deduplication windows.
* QQ Bot wire codes (:const:`MSG_TYPE_*`, :const:`MEDIA_TYPE_*`).

Reference: https://bot.q.qq.com/wiki/develop/api-v2/
"""

import os

# ---------------------------------------------------------------------------
# Adapter version
# ---------------------------------------------------------------------------

# Advertised adapter version. Increment on functional changes to the
# qqbot package; the value is embedded in the User-Agent built by
# :func:`ea_connector_qqbot.utils.build_user_agent`, so QQ's
# side of the API can correlate incidents to a client release.
QQBOT_VERSION = "1.1.0"

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

# Portal host used by the scan-to-configure flow. Overridable through
# QQ_PORTAL_HOST so corporate proxies and staging environments can point
# the onboard calls at a different edge. Defaults to the production portal.
PORTAL_HOST = os.getenv("QQ_PORTAL_HOST", "q.qq.com")

# REST API v2 base URL. Every authenticated endpoint the adapter calls is
# a path relative to this origin, so it is composed in
# :meth:`ea_connector_qqbot.adapter.QQAdapter._api_request`.
API_BASE = "https://api.sgroup.qq.com"

# OAuth2 client-credentials endpoint. Exchanges appId + clientSecret for a
# short-lived access_token; the response is cached and refreshed by
# :meth:`ea_connector_qqbot.adapter.QQAdapter._ensure_token`.
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

# Gateway discovery path. A GET against ``API_BASE + GATEWAY_URL_PATH``
# returns the WebSocket URL the adapter must connect to next.
GATEWAY_URL_PATH = "/gateway"

# QR-code onboard endpoints, resolved against PORTAL_HOST (not API_BASE).
ONBOARD_CREATE_PATH = "/lite/create_bind_task"
ONBOARD_POLL_PATH = "/lite/poll_bind_result"

# QR code target URL. ``build_connect_url()`` substitutes ``{task_id}``.
# The ``source=encre`` query parameter tags the scan as originating from
# Encre, which lets the portal distinguish Encre traffic from other QQ bot
# clients in its own telemetry.
QR_URL_TEMPLATE = (
    "https://q.qq.com/qqbot/openclaw/connect.html"
    "?task_id={task_id}&_wv=2&source=encre"
)

# ---------------------------------------------------------------------------
# Timeouts and retry tuning
# ---------------------------------------------------------------------------

# Default per-request timeout for REST API calls, in seconds.
DEFAULT_API_TIMEOUT = 30.0

# Timeout for media upload requests. Uploads carry large payloads or span
# multiple round trips, so they get a wider budget than ordinary REST calls.
FILE_UPLOAD_TIMEOUT = 120.0

# WebSocket connection establishment timeout, in seconds.
CONNECT_TIMEOUT_SECONDS = 20.0

# Exponential backoff delays (seconds) applied to reconnect attempts as
# ``RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]``. The
# clamp keeps the delay bounded at the last entry once retries exceed the
# table length, so a flapping gateway cannot grow the wait without limit.
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]

# Hard cap on reconnect attempts before the listener gives up and marks the
# platform disconnected.
MAX_RECONNECT_ATTEMPTS = 100

# Fixed delay before retrying after a gateway close code 4008 (rate limit).
# QQ sends 4008 when the client hammers the gateway; a long flat pause is
# cheaper here than the generic exponential ladder.
RATE_LIMIT_DELAY = 60  # seconds

# A disconnect occurring within this many seconds of connection is counted
# as "quick". Quick disconnects usually mean misconfigured credentials or
# missing intents rather than a transient network blip.
QUICK_DISCONNECT_THRESHOLD = 5.0  # seconds

# Consecutive quick disconnects tolerated before the adapter reports a fatal
# configuration error instead of looping on reconnect.
MAX_QUICK_DISCONNECT_COUNT = 3

# Interval between poll_bind_result calls during onboard, in seconds.
ONBOARD_POLL_INTERVAL = 2.0  # seconds between poll_bind_result calls

# HTTP timeout for create_bind_task and poll_bind_result calls.
ONBOARD_API_TIMEOUT = 10.0

# ---------------------------------------------------------------------------
# Message limits
# ---------------------------------------------------------------------------

# Maximum characters per outbound QQ message. :meth:`send` splits longer
# formatted content into chunks of at most this size before posting.
MAX_MESSAGE_LENGTH = 4000

# Dedup window, in seconds. Entries older than this are pruned from the
# msg_id dedup map, so a long-running adapter does not accumulate state.
DEDUP_WINDOW_SECONDS = 300

# Upper bound on msg_id dedup entries before the map is pruned wholesale.
DEDUP_MAX_SIZE = 1000

# Content-based dedup window, in seconds. QQ may dispatch one user message
# under several event types (C2C, DIRECT, GUILD) with distinct msg_ids, so
# a short content fingerprint window collapses those into one delivery.
DEDUP_CONTENT_WINDOW_SECONDS = 5

# ---------------------------------------------------------------------------
# QQ Bot message types (msg_type field of outbound message bodies)
# ---------------------------------------------------------------------------

# Outbound ``msg_type`` discriminators from the QQ Bot v2 message schema.
# The adapter always sends MSG_TYPE_TEXT for chat content and uses the
# other codes only for media and typing indicators.
MSG_TYPE_TEXT = 0         # Plain text message
MSG_TYPE_MARKDOWN = 2     # QQ markdown message (rich formatting)
MSG_TYPE_MEDIA = 7        # RichMedia message (image/video/voice/file with file_info)
MSG_TYPE_INPUT_NOTIFY = 6 # Typing indicator (input_notify)

# ---------------------------------------------------------------------------
# QQ Bot file media types (file_type field of upload requests)
# ---------------------------------------------------------------------------

# Outbound ``file_type`` discriminators accepted by the upload endpoints.
# Values are threaded through _send_media from the caller-facing helpers.
MEDIA_TYPE_IMAGE = 1      # Image file (jpg, png, gif, etc.)
MEDIA_TYPE_VIDEO = 2      # Video file (mp4, mov, etc.)
MEDIA_TYPE_VOICE = 3      # Voice file (silk, amr, wav, etc.)
MEDIA_TYPE_FILE = 4       # Generic file (doc, zip, pdf, etc.)
