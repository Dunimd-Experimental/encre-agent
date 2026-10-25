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

"""Relay subsystem: connector-backed platform fronting (EXPERIMENTAL).

The relay is an indirect platform path.  Instead of speaking a concrete vendor
protocol from inside the gateway process the way
:mod:`encre.gateway.platforms` does, the gateway dials **out** to a connector
host over a WebSocket and lets the connector own the real platform sockets.
That makes hosted gateways viable: a gateway with no public inbound IP can
still serve platform traffic because the vendor never has to call back into
the gateway.

This is deliberately distinct from :mod:`encre.gateway.ws_bridge`, which
bridges a whole *adapter process* -- a remote Encre adapter that owns the real
platform socket and attaches to a local gateway.  The relay inverts that
direction: the local gateway owns the agent and reaches out to a connector that
owns the platform.

Activation is config-driven, not a feature flag.  When ``GATEWAY_RELAY_URL``
(environment) or ``gateway.relay_url`` (settings) is present,
:class:`encre.gateway.run.GatewayRunner` registers the relay platform at async
startup; otherwise this module is inert and the gateway runs as an ordinary
direct-connect single-tenant gateway.  The subsystem is therefore non-breaking
to add: :func:`register_relay_adapter` imports the adapter and transport lazily,
so an unconfigured deployment pays only for the config lookups below.

Message flow once registered::

    platform vendor  <--vendor protocol-->  connector
                                               |  inbound: normalized event
                                               v
                              RelayAdapter.handle_message -> runner
                                               |  agent turn (Encre Agent Core)
                                               v
                              stream consumer / delivery router
                                               |  adapter.send()
                                               v
                              outbound frame -> connector -> platform vendor

The connector is the trust boundary.  It enforces platform authorization (hence
:attr:`RelayAdapter.authorization_is_upstream`), strips provider signatures and
tenant credentials, and holds the vendor tokens -- this package never sees one.
Authentication of the connector channel itself is handled by
:mod:`encre.gateway.relay.auth`.

Public API:

- :func:`relay_is_configured` -- the gate :class:`GatewayRunner` checks.
- :func:`relay_url`, :func:`relay_gateway_id`, :func:`relay_upgrade_secret`,
  :func:`relay_wake_url`, :func:`relay_platform_identities` -- read the relay
  configuration from environment or settings.
- :func:`register_relay_adapter` -- build a :class:`RelayAdapter` from config
  and register it as the ``relay`` platform in a :class:`GatewayRunner`.
- :func:`relay_relevance_policy`, :func:`send_relay_policy` -- declare the
  gateway's message-relevance policy to the connector at boot.
- :func:`self_provision_relay` -- (stub) self-provision with the connector.
"""

import logging
import os
from typing import Any

logger = logging.getLogger("encre.gateway.relay")

# ---------------------------------------------------------------------------
# Configuration accessors
#
# Every accessor reads the environment first and falls back to the settings
# mapping, so a hosted deployment can override a committed config.yaml without
# editing the file.  Each one returns None for "absent", which the callers treat
# as "relay not configured" rather than as an error.
# ---------------------------------------------------------------------------


def _settings() -> dict[str, Any]:
    """Return the loaded settings mapping, or an empty dict on any failure.

    All accessors in this module route through here so a missing, unreadable, or
    malformed ``config.yaml`` degrades the relay to "not configured" instead of
    raising during gateway startup.  The settings are re-read on every call
    rather than cached, so a config edit is picked up at the next gateway start
    without restarting the interpreter.

    Returns:
        The loaded settings dict, or ``{}`` when ``load_settings`` raises.
    """
    try:
        from encre.settings_manager import load_settings
        return load_settings() or {}
    except Exception:
        return {}


def relay_url() -> str | None:
    """Return the connector endpoint the gateway dials out to.

    Environment wins over settings.  The settings key is historically
    ``gateway_relay_url``; the bare ``relay_url`` alias is still honoured for
    older deployments.

    Returns:
        A stripped, non-empty URL string, or None when relay is not configured.
        Whitespace-only and empty values are treated as absent, so a stray empty
        environment variable cannot register a platform that can never connect.
    """
    env = os.environ.get("GATEWAY_RELAY_URL", "").strip()
    if env:
        return env
    val = _settings().get("gateway_relay_url") or _settings().get("relay_url")
    return str(val).strip() if val else None


def relay_gateway_id() -> str | None:
    """Return this gateway's identifier for the relay auth payload.

    The identifier is the payload half of the upgrade token minted by
    :func:`encre.gateway.relay.auth.make_upgrade_token`; the connector peeks at
    it to index its per-gateway secret table before verifying the signature, so
    it is transmitted in clear text inside a bearer token.

    Returns:
        A stripped gateway id, or None when unset (an unauthenticated dial).
    """
    env = os.environ.get("GATEWAY_RELAY_GATEWAY_ID", "").strip()
    if env:
        return env
    val = _settings().get("gateway_relay_gateway_id")
    return str(val).strip() if val else None


def relay_upgrade_secret() -> str | None:
    """Return the per-gateway HMAC secret used to authenticate the WS upgrade.

    Returns:
        A stripped secret, or None when unset.  A gateway without both an id and
        a secret still dials the connector; the connector may accept or reject
        that depending on its own policy, and a rejection before the handshake
        is retryable rather than terminal.
    """
    env = os.environ.get("GATEWAY_RELAY_SECRET", "").strip()
    if env:
        return env
    val = _settings().get("gateway_relay_secret")
    return str(val).strip() if val else None


def relay_wake_url() -> str | None:
    """Return the out-of-band wake URL for scale-to-zero gateways.

    A gateway that suspends itself via
    :meth:`RelayAdapter.go_dormant` may be unreachable afterwards, so the
    connector can GET this URL to pull it back up before replaying buffered
    platform traffic.

    Returns:
        A stripped wake URL, or None when scale-to-zero is not used.
    """
    env = os.environ.get("GATEWAY_RELAY_WAKE_URL", "").strip()
    if env:
        return env
    val = _settings().get("gateway_relay_wake_url")
    return str(val).strip() if val else None


def relay_platform_identities() -> list[dict[str, Any]]:
    """Return the platform identities this gateway fronts through the connector.

    Each entry names one vendored identity, e.g.
    ``{"platform": "discord", "bot_id": "..."}``.  A single WebSocket can front
    N of these, but the ``hello`` frame must advertise at least one, so callers
    always fall back to a synthetic ``relay`` identity for an empty list.

    Returns:
        The parsed identity list from ``GATEWAY_RELAY_IDENTITIES`` (a JSON
        string) or the ``gateway_relay_identities`` setting (a list, or a JSON
        string).  An empty list on any malformed input, so bad JSON degrades to
        "no identities advertised" instead of aborting startup.
    """
    import json
    env = os.environ.get("GATEWAY_RELAY_IDENTITIES", "").strip()
    if env:
        try:
            return list(json.loads(env))
        except Exception:
            return []
    val = _settings().get("gateway_relay_identities")
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return list(json.loads(val))
        except Exception:
            return []
    return []


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def relay_is_configured() -> bool:
    """Return True when the relay platform should be registered.

    :class:`encre.gateway.run.GatewayRunner` checks this at async startup and
    calls :func:`register_relay_adapter` only when it is True.  The check is a
    pure URL presence test, deliberately not a connectivity probe, so a
    temporarily unreachable connector never blocks gateway startup.
    """
    return bool(relay_url())


async def register_relay_adapter(manager: Any) -> bool:
    """Build a :class:`RelayAdapter` from config and register it with *manager*.

    Called by :class:`encre.gateway.run.GatewayRunner` at async startup when
    :func:`relay_is_configured` is True.  The relay platform is not present in
    :mod:`encre.gateway.platform_registry`, so the runner cannot construct it
    through its normal ``start_adapter`` path; instead the adapter is inserted
    directly into the runner's ``_instances`` map, where the rest of the runner
    -- delivery routing, the adapter panel, ``stop_all`` -- treats it exactly
    like any in-process adapter.

    The adapter and its transport are constructed here, at registration time,
    rather than at module import so an unconfigured gateway incurs no relay
    import cost.

    Args:
        manager: The :class:`GatewayRunner` instance.  Only ``_instances`` and
            ``_handle_message`` are used, so a dict-like stand-in works in tests.

    Returns:
        True when the platform is registered and connected, False otherwise.
        A failure logs a warning and never raises, so a misconfigured relay can
        never abort gateway startup.

    Raises:
        Nothing -- every failure path is logged and swallowed.
    """
    url = relay_url()
    if not url:
        return False
    try:
        from encre.gateway.relay.adapter import RelayAdapter
        from encre.gateway.relay.ws_transport import WebSocketRelayTransport

        identities = relay_platform_identities()
        # One WebSocket fronts N platforms, but the hello frame must advertise
        # at least one identity; use the first for the transport's primary
        # platform/bot_id and let the connector fan out the rest.
        platform = identities[0].get("platform", "relay") if identities else "relay"
        bot_id = identities[0].get("bot_id") if identities else None

        transport = WebSocketRelayTransport(
            url=url,
            platform=platform,
            bot_id=bot_id,
            identities=identities,
            gateway_id=relay_gateway_id(),
            upgrade_secret=relay_upgrade_secret(),
        )
        adapter = RelayAdapter(transport=transport)

        # Insert straight into the instance map: the registry has no "relay"
        # entry, so the normal start_adapter lookup would not find it.
        manager._instances["relay"] = adapter
        # Wire the inbound path.  BasePlatformAdapter.handle_message invokes the
        # handler as handler(adapter, event), which matches GatewayRunner's
        # _handle_message signature.
        adapter.set_message_handler(getattr(manager, "_handle_message", None))
        ok = await adapter.connect()
        if ok:
            logger.info("[relay] registered relay platform (fronting %d identity/ies)", len(identities) or 1)
            # Declare the relevance policy to the connector (fail-soft).
            await send_relay_policy()
        else:
            manager._instances.pop("relay", None)
            logger.warning("[relay] adapter connect failed -- relay platform not registered")
        return ok
    except Exception as e:
        logger.warning("[relay] registration failed: %s %s", type(e).__name__, e)
        # Remove a half-registered instance so the runner never advertises a
        # platform that is neither connected nor stopped cleanly.
        if isinstance(manager, dict):
            pass
        try:
            manager._instances.pop("relay", None)
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Message-relevance policy
#
# Relevance is the connector-side decision of which inbound messages are worth
# handing to the gateway at all (mentions vs. free channels vs. other bots).
# It is layered on top of the connector's own authorization gate and is purely
# an optimization, so pushing it never blocks boot.
# ---------------------------------------------------------------------------


def relay_relevance_policy() -> dict[str, Any] | None:
    """Return the gateway's relevance settings as a platform-agnostic policy.

    The policy mirrors three gateway settings in a vendor-neutral shape:
    ``gateway_require_mention`` -> ``requireAddress``,
    ``gateway_free_response_channels`` (a list or a comma-separated string) ->
    ``freeResponseScopes``, and ``gateway_allow_bots`` (``all`` or ``mentions``)
    -> ``allowOtherBots``.

    Returns:
        The policy dict, or None when every setting is at its default.  The
        connector's absent-row default already matches the gateway default, so
        sending nothing in the default case keeps the connector's table empty
        and avoids a needless write.
    """
    require_address = bool(_settings().get("gateway_require_mention"))
    free_scopes = _settings().get("gateway_free_response_channels")
    if isinstance(free_scopes, str):
        free_scopes = [s.strip() for s in free_scopes.split(",") if s.strip()]
    allow_bots = str(_settings().get("gateway_allow_bots", "")).lower() in ("all", "mentions")
    if not require_address and not free_scopes and not allow_bots:
        return None
    policy: dict[str, Any] = {"platform": "relay"}
    if require_address:
        policy["requireAddress"] = True
    if free_scopes:
        policy["freeResponseScopes"] = list(free_scopes)
    if allow_bots:
        policy["allowOtherBots"] = True
    return policy


async def send_relay_policy() -> bool:
    """Send the relevance policy to the connector at boot (fail-soft).

    Relevance is an optimization layered on the connector's authorization gate,
    never a boot dependency, so a failure logs and boot continues.

    Returns:
        True when nothing had to be sent (all-default settings) or when the
        policy was handed off; False only if a future implementation fails to
        deliver it.  Never raises.
    """
    policy = relay_relevance_policy()
    if policy is None:
        # All-default: the connector's absent-row default already matches.
        return True
    # The actual POST lives on the connector's /relay/policy route; this stub
    # only records intent, and a full implementation would reuse the upgrade
    # token plus the relay host to authenticate the call.
    logger.info("[relay] would POST relevance policy: %s", policy)
    return True


# ---------------------------------------------------------------------------
# Self-provisioning (stub)
# ---------------------------------------------------------------------------


def self_provision_relay() -> bool:
    """Attempt self-provisioning with the connector via ``/relay/provision``.

    A full implementation would POST the wake URL and gateway id to the
    connector's provision route, then persist the returned per-gateway secret
    that :func:`relay_upgrade_secret` later reads.

    Returns:
        False today (unimplemented, or no relay URL configured) so callers fall
        back to an externally-provisioned secret.  A False return is not an
        error -- it is the expected signal that manual provisioning is required.
    """
    if not relay_url():
        return False
    logger.debug("[relay] self_provision_relay not yet implemented (use an externally-provisioned secret)")
    return False
