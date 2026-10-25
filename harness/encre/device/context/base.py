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

"""Device context provider framework.

A :class:`DeviceProvider` abstracts a single device-information source
(platform, hardware, GPU, display, network, location, sensor, battery).
Providers are pure read-only collectors that return a small JSON-safe
``dict`` describing one facet of the machine.  They run at agent startup
(or from cache) and their results feed the L1 catalog injected into the
system prompt, plus the L2 ``device_*`` tools that fetch details on demand.

Every provider must be defensive: catch its own exceptions and return
``None`` (or ``{}``) on failure so a missing platform API never breaks
agent startup.
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Any


class DeviceProvider(ABC):
    """Base class for a single device-information collector.

    Subclasses set ``name`` (unique across the registry), ``sensitive``
    (whether the data requires explicit user consent / ``ask`` permission),
    and implement :meth:`collect` to return a JSON-safe ``dict`` describing
    the facet.  ``ready()`` reports whether the platform backend is usable.
    """

    name: str = ""
    sensitive: bool = False

    @abstractmethod
    def collect(self) -> dict[str, Any] | None:
        """Collect this facet of device information.

        Returns:
            A JSON-safe dict, or ``None`` if the source is unavailable.
        """

    async def collect_async(self) -> dict[str, Any] | None:
        """Async variant of :meth:`collect`.

        The default implementation offloads the synchronous :meth:`collect`
        to a thread-pool executor so it never blocks the event loop.  Providers
        that have a native async API (e.g. WinRT) override this to ``await``
        it directly.
        """
        return await asyncio.to_thread(self.collect)

    def ready(self) -> bool:
        """Return True if the provider's backend is available on this host."""
        return True
