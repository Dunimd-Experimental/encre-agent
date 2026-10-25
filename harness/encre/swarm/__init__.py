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

# Public surface of the multi-agent "swarm" package.
#
# A swarm decomposes a high-level goal into a DAG of tasks (``planner``),
# assigns each task to a role-specialised agent (``roles``), and executes the
# tree through the single DAG engine (``encre.dag.executor``) with dependency
# awareness, bounded parallelism, a shared ``blackboard``, per-node
# point-to-point ``mailbox`` channels, and an optional reviewer gate.
# ``session`` ties the pieces together into a single ``execute`` call.

from encre.swarm.blackboard import BlackboardEntry, EncreBlackboard
from encre.swarm.mailbox import EncreMailbox, MailboxMessage
from encre.swarm.planner import EncreTaskPlanner, TaskNode, TaskTree
from encre.swarm.roles import (
    ROLE_ARCHITECT,
    ROLE_CODER,
    ROLE_DEBUGGER,
    ROLE_GENERAL,
    ROLE_RESEARCHER,
    ROLE_REVIEWER,
    ROLE_TESTER,
    AgentRole,
    RoleRegistry,
)
from encre.swarm.session import EncreSwarmSession, SwarmEvent, SwarmResult

__all__ = [
    "ROLE_ARCHITECT",
    "ROLE_CODER",
    "ROLE_DEBUGGER",
    "ROLE_GENERAL",
    "ROLE_RESEARCHER",
    "ROLE_REVIEWER",
    "ROLE_TESTER",
    "AgentRole",
    "BlackboardEntry",
    "EncreBlackboard",
    "EncreMailbox",
    "EncreSwarmSession",
    "EncreTaskPlanner",
    "MailboxMessage",
    "RoleRegistry",
    "SwarmEvent",
    "SwarmResult",
    "TaskNode",
    "TaskTree",
]
