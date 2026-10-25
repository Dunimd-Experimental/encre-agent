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

"""Thinking-disabled backend cloning for side-channel model calls.

Model-assisted side channels (memory significance scoring, task
decomposition, ...) must never think and must never flip the main
conversation's thinking setting while doing so.  They run on a shallow
clone of the loop's backend: the clone shares the HTTP client and
configuration objects but owns its own ``thinking_enabled`` flag, which
the provider request builders read per request.
"""

from __future__ import annotations

import contextlib
import copy
from typing import Any

__all__ = ["thinking_off_clone"]


def thinking_off_clone(backend: Any, _depth: int = 0) -> Any:
    """Return a shallow copy of ``backend`` with thinking force-disabled.

    Wrapper backends (failover / router) get their inner backends cloned
    too, because those are the objects that actually build the request.
    Any failure returns the original backend unchanged -- a degradation to
    "thinking stays as configured", never an error.

    Args:
        backend: The backend (or wrapper) to clone.
        _depth: Recursion guard for nested wrappers.

    Returns:
        A thinking-disabled clone, or the original backend on failure.
    """
    if backend is None or _depth > 3:
        return backend
    try:
        clone = copy.copy(backend)
    except Exception:
        return backend
    if hasattr(clone, "thinking_enabled"):
        with contextlib.suppress(Exception):
            clone.thinking_enabled = False
    # Failover wrapper: ``_backends`` is a list of (name, backend) pairs.
    inners = getattr(clone, "_backends", None)
    if isinstance(inners, list) and inners:
        clone._backends = [
            (item[0], thinking_off_clone(item[1], _depth + 1)) if isinstance(item, tuple) and len(item) == 2 else item
            for item in inners
        ]
    # Router wrapper: ``_routes`` maps category -> Route(backend=...).
    routes = getattr(clone, "_routes", None)
    if isinstance(routes, dict) and routes:
        new_routes: dict[Any, Any] = {}
        for key, route in routes.items():
            inner = getattr(route, "backend", None)
            if inner is not None:
                route = copy.copy(route)
                route.backend = thinking_off_clone(inner, _depth + 1)
            new_routes[key] = route
        clone._routes = new_routes
    default = getattr(clone, "_default", None)
    if default is not None and hasattr(default, "chat"):
        clone._default = thinking_off_clone(default, _depth + 1)
    return clone
