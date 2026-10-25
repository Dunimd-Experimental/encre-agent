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

from typing import Any

from encre.context.source import ContextSource, ReconcileStatus, ReconciledBlock


class ContextRenderer:
    """Manages context sources and produces a reconciliation annotation
    block that tells the model what changed since the last turn.

    This is NOT a replacement for the enrichment phase's string
    concatenation -- it's an annotation layer ON TOP.  The enrichment
    phase still calls ``_build_*`` methods as before.  After each
    ``_build_*`` call, the enrichment phase calls
    ``renderer.record(key, content)`` which stores the content and
    returns its ``ReconcileStatus``.  At the end,
    ``renderer.build_annotation()`` produces a compact change log.

    Usage in ``_run_impl``::

        status = self._ctx_renderer.record("Workspace Structure", content)
        if status == ReconcileStatus.REPLACED:
            prompt += f"\\n\\n{content}"
        elif status == ReconcileStatus.UPDATED:
            prompt += f"\\n\\n[Updated]\\n{content}"

        # At end:
        annotation = self._ctx_renderer.build_annotation()
        if annotation:
            prompt += f"\\n\\n{annotation}"
    """

    def __init__(self) -> None:
        self._records: dict[str, str] = {}
        self._prev_records: dict[str, str] = {}

    def record(self, key: str, content: str) -> ReconcileStatus:
        """Store content for *key* and return its reconciliation status
        compared to the previous turn's content.
        """
        self._records[key] = content
        prev = self._prev_records.get(key, "")
        if not prev:
            return ReconcileStatus.REPLACED
        if self._hash(content) == self._hash(prev):
            return ReconcileStatus.UNCHANGED
        return ReconcileStatus.UPDATED

    def build_annotation(self) -> str:
        """Produce a compact block listing what changed this turn."""
        if not self._records:
            return ""
        lines: list[str] = []
        for key, content in self._records.items():
            prev = self._prev_records.get(key, "")
            if not prev:
                lines.append(f"- **{key}**: New")
            elif self._hash(content) == self._hash(prev):
                lines.append(f"- **{key}**: Unchanged")
            else:
                lines.append(f"- **{key}**: Updated")
        if not lines:
            return ""
        return "## Context Changes This Turn\n" + "\n".join(lines)

    def finalize_turn(self) -> None:
        """Called at the end of each turn to persist the current records
        as the baseline for the next turn's reconciliation.
        """
        self._prev_records = dict(self._records)
        self._records.clear()

    def reset(self) -> None:
        self._records.clear()
        self._prev_records.clear()

    def _hash(self, content: str) -> str:
        import hashlib
        return hashlib.sha256(content.encode()).hexdigest()[:16]
