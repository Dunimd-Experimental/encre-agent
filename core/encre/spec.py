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

"""Top-level compatibility facade for the specification engine.

This module owns no implementation. It is a thin alias layer that re-exports
the specification model published by :mod:`encre.modes.spec`, whose concrete
implementation lives in :class:`encre.modes.spec.engine.EncreSpecEngine`.
Callers anywhere in the repository -- the harness loop, the server protocol
handlers, and the desktop renderer's spec UI -- may import from either path;
keeping both stable means a call site can be moved without an import churn.

The specification engine is the piece of Encre that turns an informal request
into a structured, reviewable document. A :class:`SpecDocument` is a
sequence of :class:`SpecSection` objects, each carrying its own
:class:`SpecStatus` so that generation, review, and approval can be tracked
section by section. State is serialised into the file-based session store at
``data/sessions/<uuid>/meta.json``, the same JSON/JSONL layout the rest of
Encre uses; there is no database behind this module.

Why a separate module rather than a single import line? Importing
``encre.modes.spec`` pulls the whole modes package, including loop-plan and
profile machinery that a caller importing only ``SpecStatus`` does not need.
Importing ``encre.spec`` keeps that path shallow and lets the alias be swapped
independently of the modes package layout.

Public API:
    :class:`EncreSpecEngine` -- generate, review, and update spec documents.
    :class:`SpecDocument` -- an ordered collection of spec sections.
    :class:`SpecSection` -- one titled block of a document with its status.
    :class:`SpecStatus` -- lifecycle marker for a section or document.
"""

from encre.modes.spec import EncreSpecEngine, SpecDocument, SpecSection, SpecStatus

__all__ = ["EncreSpecEngine", "SpecDocument", "SpecSection", "SpecStatus"]
