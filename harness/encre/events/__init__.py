#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

"""Typed event infrastructure for the Encre agent.

Exports:

* :class:`EventSourcedBus` -- the single append-only event backbone; it also
  carries named-phase lifecycle hooks with the historical
  ``pre_model_request`` semantics.
* :class:`LedgerEvent` -- a single entry in the append-only ledger.
* :class:`EventStream` -- typed publish/subscribe fan-out stream with async
  iteration, projected over the backbone (the single contract between the
  loop and every event consumer: frontend transport, plugin taps, telemetry).
* ``PHASE_*`` constants -- well-known phase names.
* Session-lifecycle events (:mod:`~encre.events.lifecycle`) -- typed
  ``SessionEvent`` dataclasses published through the backbone.
"""

from encre.events.ledger import (
    PHASE_PRE_MODEL_REQUEST,
    PHASE_SESSION_END,
    PHASE_SESSION_START,
    PHASE_TURN_END,
    PHASE_TURN_START,
    EventSourcedBus,
    LedgerEvent,
)
from encre.events.lifecycle import (
    CheckpointCreated,
    SessionEnded,
    SessionEvent,
    SessionStarted,
    TurnEnded,
    TurnStarted,
    UserMessagePersisted,
)
from encre.events.stream import EventStream

__all__ = [
    "CheckpointCreated",
    "EventSourcedBus",
    "EventStream",
    "LedgerEvent",
    "PHASE_PRE_MODEL_REQUEST",
    "PHASE_SESSION_END",
    "PHASE_SESSION_START",
    "PHASE_TURN_END",
    "PHASE_TURN_START",
    "SessionEnded",
    "SessionEvent",
    "SessionStarted",
    "TurnEnded",
    "TurnStarted",
    "UserMessagePersisted",
]
