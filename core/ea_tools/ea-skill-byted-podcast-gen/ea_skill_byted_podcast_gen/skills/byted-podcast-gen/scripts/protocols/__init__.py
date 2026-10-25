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

"""Public surface of the SAPI-style WebSocket protocol helpers.

Re-exports the binary message codec, the message/event/flag enums and the
async handshake helpers from ``protocols.protocols`` so callers can write
``from protocols import start_session`` instead of reaching into the
implementation module. Keeping the full symbol list in ``__all__`` makes
the intended public API explicit for linters and IDE completion.
"""

from .protocols import (
    CompressionBits,
    EventType,
    HeaderSizeBits,
    Message,
    MsgType,
    MsgTypeFlagBits,
    SerializationBits,
    VersionBits,
    audio_only_client,
    cancel_session,
    finish_connection,
    finish_session,
    full_client_request,
    receive_message,
    start_connection,
    start_session,
    task_request,
    wait_for_event,
)

__all__ = [
    "CompressionBits",
    "EventType",
    "HeaderSizeBits",
    "Message",
    "MsgType",
    "MsgTypeFlagBits",
    "SerializationBits",
    "VersionBits",
    "audio_only_client",
    "cancel_session",
    "finish_connection",
    "finish_session",
    "full_client_request",
    "receive_message",
    "start_connection",
    "start_session",
    "task_request",
    "wait_for_event",
]
