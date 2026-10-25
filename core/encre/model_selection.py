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

"""Shared target-model-selection logic, transport-agnostic.

"Target models" is the per-consumer restriction that lets an operator pick,
in Settings -> Gateway -> Target Models, *which* of the globally configured
providers may actually serve a given consumer's runs. A consumer is anything
that eventually materialises an :class:`encre.config.EncreConfig` for a run:
an IM platform adapter started by :class:`encre.gateway.run.GatewayRunner`,
a desktop-originated run routed through
:class:`encre.protocol.session_router.SessionRouter`, an automation, a relay.

Those consumers disagree on *where* the selection is stored -- the gateway
keeps it in its ``self._stored_configs`` dict, the desktop writes it through
the workspace store, the frontend persists a flat ``adapter_<id>_models``
config key. They agree on *what* the selection means. This module owns the
shared half so the storage layout stays a private detail of each consumer and
no consumer has to learn the others'.

Flow through the three public functions is intentionally one-directional: a
raw stored value is parsed into ids, ids are then applied to a config. The
parsing functions never touch an :class:`encre.config.EncreConfig`, and
:func:`apply_target_models` never reads storage.

The second concern in this module is *which model a scope is pinned to*:
general-mode conversations remember their own choice, workspace-mode
conversations share one choice per workspace. Both are persisted as an
identity (``model_id`` + endpoint) rather than a flat-list index, because
the list is rebuilt from providers on every launch and an index would point
at a different model afterwards.

Public API:
    :func:`parse_target_model_ids` -- normalise a stored selection to ids.
    :func:`target_models_from_config_dict` -- ids from a config dict.
    :func:`apply_target_models` -- constrain a config and pin its first model.
    :func:`model_identity` -- durable identity of a model config.
    :func:`resolve_model_index` -- locate that identity in a live model list.
"""

import json
from typing import Any

from encre.config import EncreConfig, ModelConfig


def parse_target_model_ids(raw: Any) -> list[str] | None:
    """Normalise a stored target-model selection into a list of model ids.

    The same logical value can arrive from three different producers with
    three different shapes: the Electron renderer persists a JSON *string*,
    the gateway reads back an already-decoded Python list, and
    :class:`encre.gateway.run.GatewayRunner` may hand over an untouched
    ``config.yaml`` fragment. This function collapses all three to one
    representation so callers only ever compare ids as plain strings.

    Failure is deliberately silent. Every degraded shape -- ``None``,
    ``""``, a string that fails to decode, a scalar, an empty list -- maps
    to ``None``, meaning "unrestricted". A malformed selection must never
    wedge a consumer: the correct degraded behaviour is to fall back to
    normal model resolution (indicator model, then the random enabled pool),
    never to raise into the adapter start path. The gateway logs a warning
    for the unparseable-but-present case so an operator can still find it.

    Args:
        raw: The stored selection in any accepted shape, or ``None``.

    Returns:
        A new list of ids with every element coerced to ``str``, or ``None``
        when the value is empty, malformed, or not a list. Coercion matters
        because a JSON-decoded list can mix ints and strings while model ids
        elsewhere in the codebase are uniformly strings.

    Note:
        Returns a fresh list, never the input object. Callers may mutate the
        result (as :func:`apply_target_models` does via ``list(...)``) without
        corrupting the stored config dict.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, list) or not raw:
        return None
    return [str(i) for i in raw]


def target_models_from_config_dict(stored: dict[str, Any]) -> list[str] | None:
    """Read the target-model selection out of a per-consumer config dict.

    This is the storage-side half of the module. It exists so that a
    consumer that already has a decoded config dict -- the gateway's
    ``self._stored_configs[adapter_id]``, or the dict a workspace-store
    handler just deserialised -- does not have to know the field-name
    history of the selection.

    Two field names are accepted because the schema moved once. ``models``
    is the current name and is the same key the frontend writes as the flat
    ``adapter_<id>_models`` config entry; ``model_ids`` is a legacy alias
    retained for configs written before the rename. ``models`` wins when
    both are present.

    Args:
        stored: A decoded per-consumer config dict. ``None``-ish absence of
            both keys is the expected normal case for an unrestricted consumer.

    Returns:
        The parsed id list, or ``None`` for unrestricted. See
        :func:`parse_target_model_ids` for the full acceptance rules.
    """
    raw = stored.get("models") if stored.get("models") is not None else stored.get("model_ids")
    return parse_target_model_ids(raw)


def apply_target_models(
    cfg: EncreConfig,
    target_model_ids: list[str] | None,
) -> ModelConfig | None:
    """Constrain *cfg* to *target_model_ids* and pin it onto a candidate.

    This is the only function in the module that mutates a config, and it is
    called at run-creation time by
    :class:`encre.protocol.session_router.SessionRouter` before the run is
    handed to the Encre Agent Core. It does two separate things that both matter:

    1. It sets ``cfg.target_model_ids``, which narrows the *fallback* domain.
       When the run later fails on its pinned model, the agent's runtime
       retry/fallback path consults this list, so the restriction survives a
       model failure instead of silently widening back to every enabled
       model.
    2. It pins ``cfg`` onto ``candidates[0]`` so the very first attempt goes
       to a model the operator actually selected. Without this the run would
       start on the indicator model and only reach a target model after a
       failure, which reads as "the selection did not work".

    Candidate order comes entirely from
    :func:`encre.config.EncreConfig.resolve_model_candidates`: target models
    first, then the indicator model, then a random enabled model. This
    function deliberately does not re-sort -- the ordering policy lives in one
    place.

    Args:
        cfg: The config about to back a run. Mutated in place.
        target_model_ids: The constrained model set, or ``None``/empty for
            unrestricted.

    Returns:
        The pinned :class:`encre.config.ModelConfig`, or ``None`` when no
        restriction was requested or nothing eligible remained after
        filtering. ``None`` does not mean failure: it means "leave the config
        alone and let normal resolution run", which is why callers treat both
        outcomes as proceedable.

    Raises:
        Nothing. All input shapes are handled; a config with no enabled
        models yields ``None`` rather than an exception.
    """
    if not target_model_ids:
        return None
    # Copy rather than alias so later caller mutation cannot leak back into
    # the caller's list.
    cfg.target_model_ids = list(target_model_ids)
    candidates = cfg.resolve_model_candidates()
    if not candidates:
        return None
    first = candidates[0]
    # Field-by-field pin instead of reassignment: cfg is passed by reference
    # and the caller's object must carry the resolved values.
    cfg.model = first.model_id
    cfg.backend_type = first.backend_type
    cfg.api_key = first.api_key
    cfg.base_url = first.base_url
    cfg.max_tokens = first.max_tokens
    return first


def model_identity(model: Any) -> dict[str, str] | None:
    """Build the durable identity of a model, independent of list position.

    Flat model lists are re-derived from providers on every launch, so a
    remembered index can silently point at a different model after an edit
    or reorder. Persisting ``model_id`` plus the endpoint coordinates lets a
    scope re-find the same model later; two providers may legally expose the
    same model id, which is why the backend and base URL travel along.

    Args:
        model: A :class:`encre.config.ModelConfig` (or any object exposing
            the same attributes). ``None`` yields ``None``.

    Returns:
        ``{"model_id", "backend_type", "base_url"}`` with empty strings for
        missing fields, or ``None`` when there is no model or no model id.
    """
    if model is None:
        return None
    model_id = str(getattr(model, "model_id", "") or "").strip()
    if not model_id:
        return None
    return {
        "model_id": model_id,
        "backend_type": str(getattr(model, "backend_type", "") or ""),
        "base_url": str(getattr(model, "base_url", "") or ""),
    }


def resolve_model_index(cfg: Any, identity: Any) -> int | None:
    """Find the model an identity refers to in *cfg*'s current model list.

    Matching is strict on all three fields; when the strict pass finds
    nothing but exactly one entry shares the ``model_id``, that entry wins.
    The fallback keeps a selection alive across an endpoint edit (same model,
    user changed the base URL) without ever guessing between two providers
    that expose the same id.

    Args:
        cfg: Config whose ``models`` list is searched.
        identity: A :func:`model_identity` dict, possibly loaded from older
            storage with missing fields.

    Returns:
        The index in ``cfg.models``, or ``None`` when the model is gone or
        the identity is ambiguous.
    """
    if not isinstance(identity, dict):
        return None
    model_id = str(identity.get("model_id") or "").strip()
    if not model_id:
        return None
    backend_type = str(identity.get("backend_type") or "").strip()
    base_url = str(identity.get("base_url") or "").strip()
    candidates: list[int] = []
    for idx, model in enumerate(getattr(cfg, "models", None) or []):
        if str(getattr(model, "model_id", "") or "").strip() != model_id:
            continue
        candidates.append(idx)
        if backend_type and str(getattr(model, "backend_type", "") or "") != backend_type:
            continue
        if base_url and str(getattr(model, "base_url", "") or "") != base_url:
            continue
        return idx
    return candidates[0] if len(candidates) == 1 else None
