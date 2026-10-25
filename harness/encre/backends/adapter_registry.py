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

"""Ordered registry of the backend adapters (P1.2).

The registry is the swap point of the model layer: the factory no longer
hard-codes the ``backend_type`` dispatch, it iterates
:meth:`AdapterRegistry.resolve`.  Registration order is dispatch
precedence (first ``matches()`` wins); re-registering an existing name
**replaces** the previous adapter while keeping its position (dict
insertion-order semantics) -- that is the documented customisation
mechanism, mirroring :class:`~encre.loop.phase_registry.PhaseRegistry`.
``unregister`` removes an adapter entirely and returns whether anything
was removed, so removal is reversible and side-effect clean.

The module-level :data:`default_registry` is pre-populated with every
built-in adapter (:data:`~encre.backends.adapters.BUILTIN_ADAPTERS`) and
backs the public drop-in API :func:`register_backend_adapter` /
:func:`unregister_backend_adapter`.  A third-party package registers one
adapter and its ``backend_type`` works with zero core changes.
"""


from encre.backends.adapter_api import BackendConfig
from encre.backends.adapters import BUILTIN_ADAPTERS
from encre.backends.base import BaseBackend
from encre.backends.registry import ModelInfo
from encre.logging_config import get_logger

logger = get_logger(__name__)


class AdapterRegistry:
    """Holds the backend adapters keyed by their unique ``name``.

    The backing ``dict`` preserves insertion order (dispatch precedence)
    and keeps the original position when a name is re-registered, so
    replacing a built-in adapter never disturbs the rest of the dispatch.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}

    def register(self, adapter: Any) -> None:
        """Register ``adapter`` under its ``name``.

        Registering a name that already exists replaces the previous
        adapter **in place** (same dispatch position).  This is the
        documented swap mechanism: embedders override e.g. ``"openai"``
        with their own adapter by simply registering one with that name.

        Args:
            adapter: The adapter to register (anything satisfying
                :class:`~encre.backends.adapter_api.BackendAdapter`).

        Raises:
            ValueError: If the adapter has no non-empty ``name``.
        """
        name = str(getattr(adapter, "name", "") or "")
        if not name:
            raise ValueError("adapter.name must be a non-empty string")
        replaced = name in self._adapters
        self._adapters[name] = adapter
        if replaced:
            logger.info("[adapter_registry] replaced adapter %r in place", name)

    def unregister(self, name: str) -> bool:
        """Remove the adapter registered under ``name``.

        Args:
            name: The registry key to remove.

        Returns:
            ``True`` if an adapter was removed, ``False`` if the name was
            not registered (removing twice is a safe no-op).
        """
        removed = self._adapters.pop(name, None) is not None
        if removed:
            logger.info("[adapter_registry] unregistered adapter %r", name)
        return removed

    def get(self, name: str) -> Any | None:
        """Return the adapter registered under ``name`` (``None`` if absent)."""
        return self._adapters.get(name)

    def all(self) -> list[Any]:
        """Return all registered adapters in registration order."""
        return list(self._adapters.values())

    def names(self) -> list[str]:
        """Return all registered adapter names in registration order."""
        return list(self._adapters.keys())

    def resolve(self, config: BackendConfig) -> BaseBackend:
        """Dispatch ``config`` to the first adapter whose ``matches`` wins.

        Args:
            config: The dispatch snapshot (``backend_type`` + raw kwargs).

        Returns:
            The backend constructed by the winning adapter.

        Raises:
            ValueError: When no registered adapter matches -- the same
                error type and message shape the former hard-coded factory
                raised for unknown ``backend_type``.
        """
        for adapter in self._adapters.values():
            if adapter.matches(config):
                return adapter.create(config)
        raise ValueError(
            f"Unknown backend type: {config.backend_type}. "
            f"Available: {sorted(self._adapters.keys())}"
        )

    def model_info(
        self, model_name: str, config: BackendConfig | None = None,
    ) -> ModelInfo | None:
        """First ``model_info`` enrichment offered by any adapter.

        Adapters that cannot describe the model decline with ``None``;
        the first non-``None`` result (registration order) wins.
        """
        for adapter in self._adapters.values():
            info = adapter.model_info(model_name, config)
            if info is not None:
                return info
        return None


# Global default registry, pre-populated with the built-in adapters in the
# historical dispatch order.
default_registry = AdapterRegistry()
for _adapter in BUILTIN_ADAPTERS:
    default_registry.register(_adapter)


def register_backend_adapter(adapter: Any) -> None:
    """Public drop-in API: register ``adapter`` on the default registry.

    Third-party packages call this once at import time; afterwards
    ``create_backend(adapter.name, ...)`` constructs their backend with
    zero core changes.  Registering an existing name replaces the previous
    adapter in place (the swap mechanism).
    """
    default_registry.register(adapter)


def unregister_backend_adapter(name: str) -> bool:
    """Remove ``name`` from the default registry (reversible counterpart).

    Returns:
        ``True`` if an adapter was removed, ``False`` if absent.
    """
    return default_registry.unregister(name)
