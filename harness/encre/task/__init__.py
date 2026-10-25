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

# Public surface of the task package.
#
# A "task" is a single unit of work (a bash command, an agent subtask, or a
# multi-step workflow) tracked by an in-memory ``EncreTaskManager`` and run by
# ``EncreTaskExecutor``.  The lightweight task layer is used by workflow /
# automation features that do not need the full swarm/DAG machinery.

from encre.task.executor import EncreTaskExecutor
from encre.task.manager import EncreTaskManager
from encre.task.types import EncreTask

__all__ = [
    "EncreTask",
    "EncreTaskExecutor",
    "EncreTaskManager",
]
