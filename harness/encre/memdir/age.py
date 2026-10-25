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

import time

"""Compute human-friendly ages and staleness hints for memory files.

This module converts filesystem modification timestamps (provided in
milliseconds since the epoch) into integer day counts, natural-language
age strings (e.g. "today", "3 weeks ago"), and a short reminder snippet
that warns the model when a memory may be stale. These helpers feed the
memory manifest and prompt construction in :mod:`encre.memdir.system`.
"""


def memory_age_days(mtime_ms: float) -> int:
    """Convert a modification timestamp into a whole-day age.

    Args:
        mtime_ms: Modification time in milliseconds since the epoch.

    Returns:
        The number of complete days elapsed since ``mtime_ms``.
    """
    # Convert the stored millisecond timestamp to seconds relative to now
    delta = time.time() - mtime_ms / 1000.0
    # 86400 seconds == 1 day; integer division drops the partial day
    return int(delta / 86400)


def memory_age_text(mtime_ms: float) -> str:
    """Return a natural-language description of how old a memory is.

    Buckets the age into today/yesterday/days/weeks/months/years and
    returns the most appropriate phrasing for display in the manifest.

    Args:
        mtime_ms: Modification time in milliseconds since the epoch.

    Returns:
        A short human-readable age string (e.g. "3 weeks ago").
    """
    days = memory_age_days(mtime_ms)
    # Bucket the age from finest (today) to coarsest (years)
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    weeks = days // 7
    if weeks == 1:
        return "1 week ago"
    if weeks < 5:
        return f"{weeks} weeks ago"
    months = days // 30
    if months == 1:
        return "1 month ago"
    if months < 12:
        return f"{months} months ago"
    years = days // 365
    if years == 1:
        return "1 year ago"
    return f"{years} years ago"


def memory_freshness_text(mtime_ms: float) -> str:
    """Return a staleness reminder snippet for older memories.

    For memories older than a day, returns a ``<system-reminder>`` block
    urging the caller to verify file:line references before trusting them.
    Recent memories return an empty string so the prompt is not cluttered.

    Args:
        mtime_ms: Modification time in milliseconds since the epoch.

    Returns:
        A reminder string, or ``""`` when the memory is fresh.
    """
    days = memory_age_days(mtime_ms)
    # Only warn once memories are more than a day old to avoid noise
    if days > 1:
        return (
            f"\n<system-reminder>This memory is {days} days old and may be stale. "
            f"Verify file:line references before relying on them.</system-reminder>"
        )
    return ""
