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

"""Stable contracts shared by the kernel, features, and embedders.

Import from this package, not from the implementation modules, when you need a
cross-cutting type.  Everything here is dependency-free (except
:mod:`encre.contracts.ports`, which only references the other contracts), so
importing it never pulls in the agent, the loop, or a backend.
"""

from __future__ import annotations

from encre.contracts.decisions import DecisionKind, PolicyDecision
from encre.contracts.events import EventEnvelope, new_envelope
from encre.contracts.ports import (
    ClockPort,
    ContextPort,
    EventPort,
    MemoryPort,
    ModelPort,
    PolicyPort,
    StorePort,
    ToolPort,
)
from encre.contracts.tools import ToolSpec
from encre.contracts.version import (
    API_VERSION,
    EVENT_SCHEMA_VERSION,
    HARNESS_VERSION,
    PLUGIN_API_VERSION,
    TOOL_SPEC_VERSION,
)

__all__ = [
    "API_VERSION",
    "EVENT_SCHEMA_VERSION",
    "HARNESS_VERSION",
    "PLUGIN_API_VERSION",
    "TOOL_SPEC_VERSION",
    "ClockPort",
    "ContextPort",
    "DecisionKind",
    "EventEnvelope",
    "EventPort",
    "MemoryPort",
    "ModelPort",
    "PolicyDecision",
    "PolicyPort",
    "StorePort",
    "ToolPort",
    "ToolSpec",
    "new_envelope",
]
