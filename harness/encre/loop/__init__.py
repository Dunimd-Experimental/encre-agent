#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

"""Encre agent loop package.

The former monolithic ``encre/loop.py`` is split into:

* :mod:`encre.loop.runner` -- the ``EncreLoop`` coordinator and phase sequence;
* :mod:`encre.loop.wiring` -- constructor wiring of every collaborator;
* :mod:`encre.loop.control` -- external control API (plan mode, permissions,
  commands);
* :mod:`encre.loop.support` -- cross-phase helper methods;
* :mod:`encre.loop.turn_ctx` -- the shared :class:`TurnContext` /
  :class:`TurnExit` control contract;
* :mod:`encre.loop.phases` -- the per-turn phase mixins (prompt, compact,
  model, recovery, tools, execute).
"""

from encre.backend import create_backend
from encre.loop.runner import EncreLoop
from encre.loop.turn_ctx import TurnContext, TurnExit

__all__ = [
    "EncreLoop",
    "TurnContext",
    "TurnExit",
    "create_backend",
]
