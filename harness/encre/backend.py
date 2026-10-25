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

"""Public backend factory facade.

The former hard-coded ``backend_map`` dispatch lived here; it is now
driven by :class:`~encre.backends.adapter_registry.AdapterRegistry` --
the built-in adapters live in :mod:`encre.backends.adapters` and
third-party backends drop in via
:func:`~encre.backends.adapter_registry.register_backend_adapter`.
This module keeps only the thin factory pipeline every backend shares:
the empty-type guard, the generic per-model context-window override, and
the delegation to the default adapter registry.

The context-window and sampling overrides are applied *after* construction
rather than passed through the constructor because only a handful of
backends accept them as kwargs (openai_compatible, openrouter), while
every backend — including :class:`FailoverBackend`, whose
``context_window_size()`` delegates to its primary child — must honour
them.  Post-construction stamping keeps the contract uniform.
"""

from typing import Any

from encre.backends.adapter_api import BackendConfig
from encre.backends.adapter_registry import default_registry
from encre.backends.base import BaseBackend


def _override_context_window(backend: BaseBackend, window: int) -> None:
    """Force ``context_window_size()`` to return an explicit per-model value.

    Only a handful of backends accept a ``context_window`` constructor kwarg
    (openai_compatible, openrouter).  This applies the override generically to
    ANY backend instance -- including ``FailoverBackend``, whose own
    ``context_window_size()`` delegates to the primary child -- so switching to
    a model with a different window takes effect immediately for compaction.

    The override is installed as a bound-method-replacement: a zero-arg
    closure that ignores ``self`` and returns the explicit window.  This
    is safe because :class:`BaseBackend` documents that subclasses must
    not assume ``context_window_size`` is bound to their own state.
    """
    if not window or window <= 0:
        return
    def _size(_b: BaseBackend = backend, _w: int = window) -> int:
        return _w
    backend.context_window_size = _size  # type: ignore[method-assign]


def _apply_sampling_overrides(
    backend: BaseBackend,
    temperature: float | None,
    top_p: float | None,
    top_k: int | None,
) -> None:
    """Stamp user-configured sampling overrides onto ANY backend instance.

    Like :func:`_override_context_window`, this works generically after
    construction so no backend constructor needs sampling parameters.  The
    request builders of each protocol family consult
    ``backend.sampling_overrides`` and merge the values into the outgoing
    payload (key names differ per provider, e.g. ``topP`` for Gemini).

    ``None`` values are omitted so the backend's own defaults apply; only
    explicitly-set overrides are forwarded.  This matters for providers
    that reject unknown keys: a stray ``"temperature": None`` in the payload
    would be a 400, not a silent default.
    """
    overrides: dict[str, float | int] = {}
    if temperature is not None:
        overrides["temperature"] = float(temperature)
    if top_p is not None:
        overrides["top_p"] = float(top_p)
    if top_k is not None:
        overrides["top_k"] = int(top_k)
    if overrides:
        backend.sampling_overrides = overrides


def create_backend(type: str, **kwargs: Any) -> BaseBackend | None:
    """Construct the backend for ``type`` through the adapter registry.

    The explicit per-model context window is applied generically AFTER
    construction (see _override_context_window) so every backend honours it
    without each constructor needing a context_window parameter.  All other
    kwargs preprocessing (thinking kwargs, the failover-only ``models``
    kwarg) is owned by the winning adapter.

    Args:
        type: The backend type key (``""`` returns ``None`` -- no vendor
            configured).  Unknown types raise ``ValueError``.
        **kwargs: Raw constructor kwargs forwarded to the adapter.

    Returns:
        The constructed :class:`~encre.backends.base.BaseBackend`, or
        ``None`` for an empty ``type``.
    """
    if not type:
        return None

    explicit_window: int = kwargs.pop("context_window", 0) or 0
    sampling = (
        kwargs.pop("temperature", None),
        kwargs.pop("top_p", None),
        kwargs.pop("top_k", None),
    )
    config = BackendConfig(backend_type=type, kwargs=kwargs)
    backend = default_registry.resolve(config)
    _override_context_window(backend, explicit_window)
    _apply_sampling_overrides(backend, *sampling)
    return backend
