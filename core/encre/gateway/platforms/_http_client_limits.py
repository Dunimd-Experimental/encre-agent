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

"""Shared connection-pool tuning for long-lived platform adapters.

Module position: :mod:`encre.gateway.platforms` is the IM-bridge layer.
Every adapter in this package (:class:`encre.gateway.platforms.base.BasePlatformAdapter`
subclasses for QQ Bot, Feishu, WeCom, DingTalk, Signal, BlueBubbles and
others) holds one ``httpx.AsyncClient`` for the whole adapter lifetime so
that TLS handshakes and TCP setup are paid once instead of once per API
call.  :class:`encre.gateway.run.GatewayRunner` may instantiate several of
those clients at the same time, so the file-descriptor footprint of the
whole process is the sum over adapters, not the worst single adapter.

The pool is the thing that needs tuning.  A persistent client that keeps
idle keep-alive connections open is what turns one WebSocket-plus-HTTP
client into a steady drain of sockets.  httpx's built-in
``keepalive_expiry`` is 5 seconds; behind a transparent proxy (for
example Cloudflare Warp on macOS) a peer-initiated FIN can sit in
``CLOSE_WAIT`` well past that point, because the local socket only
releases after both sides close.  A handful of such lingering sockets per
adapter, multiplied across every concurrently running platform client in
the process, reaches the default 256 fd ceiling.

:func:`platform_httpx_limits` hands adapter factories the
``httpx.Limits`` to pass to ``httpx.AsyncClient(limits=...)``.  The two
knobs set:

* ``max_keepalive_connections`` = 10 -- a single platform API is polled
  and written to sequentially, so ten pooled idle sockets already
  exceeds any real concurrency demand.
* ``keepalive_expiry`` = 2.0 -- recycle idle sockets quickly enough that
  no proxy can hold a socket past the point where it stops being useful.

``max_connections`` is deliberately left at the httpx default so the
client keeps full burst headroom; only the idle-socket lifetime is
constrained.

Both values are overridable for load testing without a rebuild:

* ``ENCRE_GATEWAY_HTTPX_KEEPALIVE_EXPIRY`` -- float seconds
* ``ENCRE_GATEWAY_HTTPX_MAX_KEEPALIVE`` -- integer connection count

Public API:
    * :func:`platform_httpx_limits`
"""

import os

import httpx


# Idle keep-alive sockets are recycled after this many seconds of silence.
# Chosen below httpx's 5-second default so a proxy's lingering CLOSE_WAIT
# window cannot accumulate sockets across concurrently running adapters.
_DEFAULT_KEEPALIVE_EXPIRY_S = 2.0
# Idle sockets pooled by the client.  Ten is far above the sequential
# request patterns platform APIs actually generate.
_DEFAULT_MAX_KEEPALIVE = 10


def platform_httpx_limits() -> "httpx.Limits | None":
    """Build the connection-pool limits used by every long-lived platform client.

    Callers pass the returned object straight into
    ``httpx.AsyncClient(limits=platform_httpx_limits())``.  The function
    is safe to call on every adapter construction; it performs no I/O and
    reads at most two environment variables.

    Returns:
        An ``httpx.Limits`` instance tuned for a long-lived gateway
        client, or ``None`` when ``httpx`` is unavailable to this module.
        ``None`` is a deliberate sentinel: ``httpx.AsyncClient`` treats a
        ``None`` limit as "use the built-in default", so an adapter can
        write ``limits=platform_httpx_limits()`` unconditionally and still
        get httpx's own behaviour when this helper cannot resolve httpx.

    Note:
        Values are re-read from the environment on each call, so tuning
        does not require restarting the adapter factory's module import
        machinery.
    """
    if httpx is None:
        # Defensive guard: keep returning a usable sentinel rather than
        # raising if httpx can ever be absent from this module's namespace.
        return None

    def _env_float(name: str, default: float) -> float:
        """Read a strictly-positive float from the environment.

        Args:
            name: Environment variable name.
            default: Value used when the variable is unset, empty,
                non-numeric, or not greater than zero.

        Returns:
            The parsed value, or *default*.
        """
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return default
        # Non-positive values would disable keep-alive or the pool limit,
        # which is never an intended configuration.
        return val if val > 0 else default

    def _env_int(name: str, default: int) -> int:
        """Read a strictly-positive integer from the environment.

        Args:
            name: Environment variable name.
            default: Value used when the variable is unset, empty,
                non-numeric, or not greater than zero.

        Returns:
            The parsed value, or *default*.
        """
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return default
        # Non-positive values would disable keep-alive or the pool limit,
        # which is never an intended configuration.
        return val if val > 0 else default

    keepalive_expiry = _env_float(
        "ENCRE_GATEWAY_HTTPX_KEEPALIVE_EXPIRY", _DEFAULT_KEEPALIVE_EXPIRY_S
    )
    max_keepalive = _env_int(
        "ENCRE_GATEWAY_HTTPX_MAX_KEEPALIVE", _DEFAULT_MAX_KEEPALIVE
    )

    return httpx.Limits(
        max_keepalive_connections=max_keepalive,
        # max_connections stays at the httpx default (100) on purpose: only
        # idle-socket lifetime is the fd-pressure risk, not burst capacity.
        keepalive_expiry=keepalive_expiry,
    )
