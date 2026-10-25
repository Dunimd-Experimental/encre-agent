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

"""The model-adapter contract of the backend factory (P1.2).

The former hard-coded ``backend_map`` dispatch in
:mod:`encre.backend` is driven by
:class:`~encre.backends.adapter_registry.AdapterRegistry` instead.  This
module defines the contract every adapter implements:

* :class:`BackendConfig` -- the immutable dispatch snapshot the factory
  hands to adapters: the requested ``backend_type`` plus the raw
  constructor kwargs (``api_key``, ``base_url``, ``model``, ...).
  Convenience properties expose the three kwargs every provider needs so
  adapters can inspect them without reaching into the raw dict.
* :class:`BackendAdapter` -- the structural type the registry stores.
  ``matches(config)`` decides whether the adapter serves a request;
  ``create(config)`` constructs the real :class:`~encre.backends.base.BaseBackend`.
  ``model_info`` is optional: returning ``None`` means "this adapter cannot
  describe the model", and the registry treats that as a decline (used to
  enrich :func:`~encre.backends.registry.resolve_model_info`).

Drop-in usage for third-party packages::

    from encre.backends import BaseBackend, register_backend_adapter

    class MyBackend(BaseBackend): ...

    class MyAdapter:
        name = "my-provider"
        def matches(self, config): return config.backend_type == "my-provider"
        def create(self, config): return MyBackend(**config.kwargs)
        def model_info(self, model_name, config=None): return None

    register_backend_adapter(MyAdapter())

After that single registration ``create_backend("my-provider", ...)`` works
with zero core changes.
"""


from dataclasses import dataclass, field
from typing import Any, Protocol

from encre.backends.base import BaseBackend
from encre.backends.registry import ModelInfo


@dataclass(frozen=True)
class BackendConfig:
    """Immutable snapshot of one ``create_backend`` request.

    Attributes:
        backend_type: The requested provider key (e.g. ``"openai"``,
            ``"anthropic"``, or a third-party-registered name).
        kwargs: The raw constructor kwargs the factory received.  Adapters
            must copy before mutating; the factory and sibling adapters may
            still be reading the same dict.
    """

    backend_type: str
    kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def model(self) -> str:
        """The requested model name (``""`` when not supplied)."""
        return str(self.kwargs.get("model", "") or "")

    @property
    def api_key(self) -> str:
        """The supplied API key (``""`` when not supplied)."""
        return str(self.kwargs.get("api_key", "") or "")

    @property
    def base_url(self) -> str:
        """The supplied API base URL (``""`` when not supplied)."""
        return str(self.kwargs.get("base_url", "") or "")


class BackendAdapter(Protocol):
    """The structural contract every backend adapter implements.

    ``name`` is the unique registry key; re-registering the same name
    replaces the previous adapter in place (the swap mechanism, mirroring
    :class:`~encre.loop.phase_registry.PhaseRegistry`).  ``matches`` /
    ``create`` reproduce the former hard-coded dispatch; ``model_info`` is
    an optional enrichment hook -- adapters that cannot describe a model
    return ``None``.
    """

    name: str

    def matches(self, config: BackendConfig) -> bool:
        """Return ``True`` when this adapter can serve ``config``."""
        ...

    def create(self, config: BackendConfig) -> BaseBackend:
        """Construct the real backend for ``config``."""
        ...

    def model_info(
        self, model_name: str, config: BackendConfig | None = None,
    ) -> ModelInfo | None:
        """Describe ``model_name``, or ``None`` when the adapter cannot.

        Optional enrichment hook consumed by
        :class:`~encre.backends.adapter_registry.AdapterRegistry.model_info`;
        built-in adapters decline (return ``None``) because their model
        metadata already lives in the static model registry.
        """
        ...
