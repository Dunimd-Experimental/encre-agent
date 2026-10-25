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

"""Runtime readiness probe for the gateway process.

The gateway exposes a detailed health endpoint, ``GET /health/detailed`` in
:mod:`encre.transport.http.openai_api`, so a dashboard in another container
can tell whether an agent is actually able to take work rather than merely
whether the process answers TCP. This module owns the readiness verdict that
endpoint embeds.

The probe is intentionally pure and side-effect free. It takes the facts it
needs as arguments -- resolved model, runtime status dict, in-flight API run
counts, completion-queue depth, active delegations -- and returns a plain
dict. Nothing here opens a socket, reads the session directory, or talks to
the Encre Agent Core, because the caller has already collected those facts while
building the response body; re-deriving them here would duplicate work and
could disagree with the numbers the endpoint reports one line below.

The ``*args, **kwargs`` signature is a compatibility envelope. Callers
extend the set of facts they pass as the verdict grows, and the free-form
signature means a caller upgrade does not raise ``TypeError`` in a process
that has not been rebuilt in lock step.

Public API:
    :func:`collect_runtime_readiness` -- compute the readiness verdict dict.
"""


def collect_runtime_readiness(*args, **kwargs):
    """Return the runtime readiness verdict for the gateway process.

    The current implementation is an unconditional permissive verdict: the
    gateway is always reported ready and no individual checks are recorded.
    Every argument is accepted and discarded, so callers may pass
    ``configured_model``, ``runtime_status``, ``active_api_runs``,
    ``process_completion_queue_depth``, and ``active_delegations`` -- as
    :meth:`encre.transport.http.openai_api.APIServerAdapter._handle_health_detailed`
    does -- without any of them affecting the result.

    The returned dict has two keys:

    - ``ready`` -- boolean gate. ``True`` means an orchestrator is entitled
      to send work to this gateway.
    - ``checks`` -- a mapping of named check to result. Empty here, so a
      consumer iterating ``checks.items()`` to render per-check status sees
      nothing rather than a stale or misleading entry.

    Args:
        *args: Positional facts supplied by a caller. Ignored.
        **kwargs: Named facts supplied by a caller. Ignored.

    Returns:
        ``{'ready': True, 'checks': {}}`` -- a fresh dict on every call, so
        callers may store or mutate it without affecting a later probe.

    Note:
        That same caller nests the result under ``"readiness"`` and also
        reads ``readiness["status"]`` to populate its top-level ``"status"``
        field. This stub returns no ``status`` key, so that read raises
        ``KeyError`` on ``GET /health/detailed``. Any real implementation of
        this function has to provide ``status`` as well as ``ready`` and
        ``checks``.

    Raises:
        Nothing. A readiness probe that raised would take the health
        endpoint down with it.
    """
    return {'ready': True, 'checks': {}}

