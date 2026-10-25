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

import asyncio
import enum
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ReconcileStatus(str, enum.Enum):
    """Result of comparing a context source against its previous version."""

    UNCHANGED = "Unchanged"
    UPDATED = "Updated"
    REPLACED = "New"


@dataclass
class ReconciledBlock:
    """A single context block after reconciliation."""

    key: str
    content: str
    status: ReconcileStatus
    priority: int = 50

    def render(self) -> str:
        prefix = {
            ReconcileStatus.UNCHANGED: "[unchanged]",
            ReconcileStatus.UPDATED: "[updated]",
            ReconcileStatus.REPLACED: "[new]",
        }.get(self.status, "")
        header = f"## {self.key} {prefix}" if prefix else f"## {self.key}"
        return f"{header}\n{self.content}"


class ContextSource(ABC):
    """A typed source of context for the system prompt.

    Each source has a unique ``key``, a ``priority`` for ordering, and
    tracks its previous content so the renderer can detect changes.
    """

    def __init__(self, key: str, priority: int = 50) -> None:
        self.key = key
        self.priority = priority
        self._last_content: str = ""

    @abstractmethod
    async def load(self, loop: Any) -> str:
        """Load the current content for this context source.
        ``loop`` is the active ``EncreLoop`` instance that owns the
        subsystems (memory, profile, etc.) needed to build the content.
        """
        ...

    def reconcile(self, new_content: str) -> ReconcileStatus:
        if not self._last_content:
            return ReconcileStatus.REPLACED
        if self._content_hash(new_content) == self._content_hash(self._last_content):
            return ReconcileStatus.UNCHANGED
        return ReconcileStatus.UPDATED

    def store(self, content: str) -> None:
        self._last_content = content

    def _content_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]


class LambdaSource(ContextSource):
    """Convenience wrapper that turns a callable into a ``ContextSource``.

    Useful for wrapping existing ``_build_*`` methods from ``EncreLoop``
    without creating a new class per source.
    """

    def __init__(self, key: str, loader: Any, priority: int = 50) -> None:
        super().__init__(key, priority=priority)
        self._loader = loader

    async def load(self, loop: Any) -> str:
        if asyncio.iscoroutinefunction(self._loader):
            return await self._loader(loop)
        return self._loader(loop)
