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

"""Async delegation counter.

Tracks the number of in-flight delegated sub-agent tasks so readiness
and status endpoints can report delegation depth without having to
walk the harness's runtime state.

Both mutators accept an arbitrary ``count`` so callers can reserve a
batch of delegations in one lock acquisition, and both clamp at zero
so a mismatched decrement (e.g. from a task that was never really
incremented, or was double-cancelled) cannot drive the counter
negative.
"""

from __future__ import annotations

import threading

# Lock + counter pair. The counter is an int so ``active_count()`` is
# an O(1) read that can be called from any thread at any time.
_lock = threading.Lock()
_active: int = 0


def increment(count: int = 1) -> int:
    """Increase the active delegation count.

    Args:
        count: Number of delegations to add. Negative values are
            silently treated as zero so a buggy caller cannot drive
            the counter down.

    Returns:
        The new value of the counter after the increment.
    """
    global _active
    with _lock:
        _active += max(0, int(count))
        return _active


def decrement(count: int = 1) -> int:
    """Decrease the active delegation count.

    Clamps at zero so an unmatched decrement cannot drive the counter
    negative — that would produce confusing readiness states and would
    make future increments silently "waste" ticks.

    Args:
        count: Number of delegations to release.

    Returns:
        The new value of the counter after the decrement.
    """
    global _active
    with _lock:
        _active = max(0, _active - max(0, int(count)))
        return _active


def active_count() -> int:
    """Return the number of currently active delegated tasks."""
    with _lock:
        return _active
