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

# Data model for the simple task layer.
#
# ``EncreTask`` is a serialisable record describing one unit of work: its
# identity, prompt, type, lifecycle ``status``, and the result/error produced
# once executed.  It is deliberately minimal so tasks can be created, updated,
# listed, and deleted through ``EncreTaskManager`` without external storage.

from dataclasses import dataclass, field
from typing import Any

from encre.utils.types import TaskStatus, TaskType


@dataclass
class EncreTask:
    """A single tracked unit of work in the task layer.

    Attributes:
        id: Stable unique identifier for the task.
        name: Human-readable title.
        description: Free-form detail shown to operators.
        task_type: Discriminator used by the executor (``bash`` / ``agent`` /
            ``workflow``).
        prompt: The instruction/command executed for this task.
        status: Lifecycle state (``pending`` / ``running`` / ``completed`` /
            ``failed``).
        result: Output captured on success.
        error: Error message captured on failure.
        parent_id: Optional parent task (for workflow sub-steps).
        created_at / updated_at: Unix timestamps.
        metadata: Arbitrary caller-supplied key/value bag (e.g. model config).
    """
    id: str
    name: str
    description: str
    task_type: TaskType
    prompt: str
    status: TaskStatus = "pending"
    result: str = ""
    error: str = ""
    parent_id: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
