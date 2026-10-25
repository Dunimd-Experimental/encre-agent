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

"""Environment-variable passthrough registration.

Some adapters (notably Raft) need specific environment variables to
survive into spawned subprocesses.  :func:`register_env_passthrough`
records those names so the process launcher can forward them explicitly
rather than relying on a blanket ``env=None`` inheritance that would
leak unrelated credentials to unrelated children.

The registry is intentionally in-memory and process-scoped: each
process that hosts a gateway or tool executor re-registers on startup
from its adapters, so state does not need to be persisted across
restarts.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Names of environment variables that must be forwarded to subprocesses.
_passthrough: set[str] = set()


def register_env_passthrough(names: list[str] | tuple[str, ...] | str) -> None:
    """Register environment variable name(s) for subprocess passthrough.

    Callers may pass either a single name or an iterable. Empty strings
    are dropped silently because callers sometimes iterate over a
    configuration dict whose values may be missing.

    Args:
        names: A single name or an iterable of names.
    """
    if isinstance(names, str):
        names = [names]
    for name in names:
        if name:
            _passthrough.add(str(name))


def get_env_passthrough() -> frozenset[str]:
    """Return the set of environment variables registered for passthrough.

    Returned as an immutable :class:`frozenset` so that downstream code
    cannot mutate the shared registry through the reference. If a caller
    needs to add names, use :func:`register_env_passthrough` directly.
    """
    return frozenset(_passthrough)
