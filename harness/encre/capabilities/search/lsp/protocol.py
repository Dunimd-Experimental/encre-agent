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

"""Protocol dataclasses shared across the Encre LSP integration.

These lightweight ``dataclass`` types model the subset of the Language Server
Protocol we consume: positions, ranges, locations, diagnostics, hover results
and the overall connection state.  They are plain value objects with no
behaviour, so they can be built directly from decoded JSON payloads.
"""

from dataclasses import dataclass


@dataclass
class Position:
    """A zero-based (line, character) position in a text document."""
    line: int
    character: int


@dataclass
class Range:
    """A span between a start and end :class:`Position`."""
    start: Position
    end: Position


@dataclass
class Location:
    """A link to a symbol: a document URI plus a :class:`Range`."""
    uri: str
    range: Range


@dataclass
class Diagnostic:
    """A single diagnostic (error/warning/hint) reported for a range."""
    range: Range
    severity: int
    message: str
    source: str = ""


@dataclass
class HoverResult:
    """The result of a hover request, plus an optional source range."""
    contents: str
    range: Range | None = None


@dataclass
class LSPState:
    """Tracks whether LSP initialisation succeeded or failed."""
    status: str
    error: str | None = None
