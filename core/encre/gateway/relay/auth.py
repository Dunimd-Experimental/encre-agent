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

"""Relay channel authentication (gateway half).

The connector-gateway channel is itself authenticated, because a gateway may be
customer-managed and directly reachable from the internet.  Without this layer,
any host that can reach the connector's ``/relay`` endpoint could register as a
gateway and receive every tenant's inbound traffic.

This module implements the gateway half of two HMAC schemes whose wire bytes
must match the connector's TypeScript implementation exactly.  A byte-level
mismatch here is not a security bug, it is a compatibility bug: the token
simply fails to verify.

1. **WS upgrade auth** (gateway -> connector): the gateway presents
   ``Authorization: Bearer <token>`` on the ``/relay`` WebSocket upgrade, where
   ``token = make_upgrade_token(gateway_id, secret)``.  Format:
   ``base64url(f"{payload}:{exp}:{sig}")`` with
   ``sig = HMAC_SHA256(f"{payload}:{exp}", secret).hexdigest()`` and
   ``payload == gateway_id``.  The connector peeks at the payload head to index
   its per-gateway secret table, then verifies.
   Used by :class:`encre.gateway.relay.ws_transport.WebSocketRelayTransport`.

2. **Inbound delivery signature** (connector -> gateway): the connector signs
   each inbound delivery with the per-tenant delivery key, carried as
   ``x-relay-timestamp`` plus ``x-relay-signature`` headers; the gateway
   verifies before accepting the event.  Format:
   ``sig = HMAC_SHA256(f"{ts}.{body_json}", key).hexdigest()`` over the exact
   request body, with a replay-window skew check.

Both schemes take a **multi-secret verify list** (primary first, then a
secondary during a rotation window) so a secret rotation does not invalidate
tokens that are already outstanding in flight.

EXPERIMENTAL: may change without a deprecation cycle until Class-1 platforms
validate the relay contract.

Public API:

- :func:`make_upgrade_token` / :func:`verify_token` -- the WS-upgrade token.
- :func:`verify_delivery_signature` -- inbound delivery verification.
- :func:`sign` / :func:`verify_signature` -- the shared HMAC primitives.
- :data:`DELIVERY_TS_HEADER`, :data:`DELIVERY_SIG_HEADER` -- wire header names.
"""

import base64
import hashlib
import hmac
import time
from typing import Optional, Sequence

# Header names the connector uses for inbound delivery signatures.  They are
# lower-case by convention and must not be renamed, since the connector emits
# them literally.
DELIVERY_TS_HEADER = "x-relay-timestamp"
DELIVERY_SIG_HEADER = "x-relay-signature"

# Default replay window for an inbound delivery signature (connector default).
_DEFAULT_MAX_SKEW_SECONDS = 300
# Default TTL for an upgrade token (connector makeUpgradeToken default).
_DEFAULT_UPGRADE_TTL_SECONDS = 300


def _hmac_hex(payload: str, secret: str) -> str:
    """HMAC-SHA256 hex digest of *payload* under *secret* (UTF-8).

    Both inputs are encoded as UTF-8 to match the connector, which signs
    ``Buffer.from(str)`` values.  A differing encoding would make every
    signature verify as false without any visible error.

    Args:
        payload: The message to sign.
        secret: The HMAC key.

    Returns:
        The 64-character lowercase hex digest.
    """
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def sign(payload: str, secret: str) -> str:
    """HMAC-SHA256 hex digest -- the connector's ``sign`` (relayAuthToken.ts).

    Args:
        payload: The message to sign.
        secret: The HMAC key.

    Returns:
        The hex digest.  Exposed as a named function so a caller that signs
        arbitrary relay payloads does not need to reach into the private
        helper.
    """
    return _hmac_hex(payload, secret)


def verify_signature(payload: str, sig_hex: str, secrets: Sequence[str]) -> bool:
    """Check that *sig_hex* is a valid HMAC of *payload* under any *secret*.

    Iterates the rotation verify list and accepts the first match, so a token
    minted under the outgoing key keeps verifying until the rotation window
    closes.  The comparison itself is constant time via
    :func:`hmac.compare_digest`.

    Args:
        payload: The signed material.
        sig_hex: The candidate hex digest.
        secrets: Candidate keys, primary first.  Empty entries are skipped so a
            misconfigured rotation slot cannot accidentally validate anything.

    Returns:
        True when any key produces the candidate digest.  A malformed,
        non-hex, or empty candidate returns False.  Candidates whose length
        differs from the 32-byte HMAC output are skipped before the constant-time
        compare, so a truncated signature is rejected outright.
    """
    try:
        sig_buf = bytes.fromhex(sig_hex)
    except (ValueError, TypeError):
        return False
    if len(sig_buf) == 0:
        return False
    for secret in secrets:
        if not secret:
            continue
        expected = bytes.fromhex(_hmac_hex(payload, secret))
        if len(expected) != len(sig_buf):
            continue
        if hmac.compare_digest(sig_buf, expected):
            return True
    return False


def make_token(payload: str, secret: str, ttl_seconds: int = 0) -> str:
    """Build a signed, optionally-expiring token -- the connector's ``makeToken``.

    Format: ``base64url(f"{payload}:{exp}:{sig}")`` where ``exp`` is a
    unix-seconds expiry (0 meaning never) and
    ``sig = HMAC_SHA256(f"{payload}:{exp}", secret)``.

    Args:
        payload: The claim the token carries.  For upgrade tokens this is the
            gateway id.  May itself contain colons; :func:`verify_token` splits
            from the right so that round-trips.
        secret: The HMAC key.
        ttl_seconds: Lifetime in seconds.  Zero means the token never expires,
            which is the right default only for tokens that are re-minted on
            every dial rather than cached.

    Returns:
        The unpadded base64url token.  Padding is stripped to match Node's
        ``Buffer.toString("base64url")`` byte for byte.
    """
    exp = int(time.time()) + ttl_seconds if ttl_seconds > 0 else 0
    signed = f"{payload}:{exp}"
    sig = _hmac_hex(signed, secret)
    raw = f"{signed}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_upgrade_token(
    gateway_id: str, secret: str, ttl_seconds: int = _DEFAULT_UPGRADE_TTL_SECONDS
) -> str:
    """The WS-upgrade bearer token a gateway sends: ``payload = gateway_id``.

    The connector peeks ``gateway_id`` (the payload head) to index its secret
    verify list, then verifies the signature against that gateway's stored
    secret(s).  Mirrors the connector's ``makeUpgradeToken``.

    Args:
        gateway_id: The gateway's identifier.  Traveling in the clear inside the
            token is intentional: it is a routing key, not a secret.
        secret: The per-gateway upgrade secret.
        ttl_seconds: Token lifetime.  The default 300 seconds matches the
            connector; a token that outlives the connector's replay window is
            rejected there regardless of this value.

    Returns:
        The bearer token string, without the ``Bearer `` prefix.
    """
    return make_token(gateway_id, secret, ttl_seconds)


def verify_token(token: str, secrets: Sequence[str]) -> Optional[str]:
    """Verify a token built by :func:`make_token`; return the payload or None.

    Splits from the right so a payload may itself contain colons (mirrors the
    connector's ``verifyToken``).  Rejects an expired token and any signature
    that does not match a secret in the verify list.

    Args:
        token: The base64url token, with or without padding.
        secrets: Candidate keys, primary first.

    Returns:
        The decoded payload on success, or None for any malformed input, a
        non-integer expiry, an expired token, or a bad signature.  Callers treat
        None as "not authorized" and must not retry with the same token.
    """
    try:
        # base64url decode with padding restored.
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, TypeError):
        return None
    parts = decoded.split(":")
    if len(parts) < 3:
        return None
    sig = parts[-1]
    try:
        exp = int(parts[-2])
    except ValueError:
        return None
    payload = ":".join(parts[:-2])
    if exp != 0 and int(time.time()) > exp:
        return None
    signed = f"{payload}:{exp}"
    return payload if verify_signature(signed, sig, secrets) else None


def _delivery_payload(ts: int, body_json: str) -> str:
    """Signed material for an inbound delivery: ``f"{ts}.{body_json}"``.

    The timestamp is included in the signed material rather than only the
    header, so an attacker cannot replay a captured body with a fresh
    timestamp.

    Args:
        ts: The unix-seconds timestamp from the delivery header.
        body_json: The literal request body, UTF-8.

    Returns:
        The exact string the HMAC is computed over.
    """
    return f"{ts}.{body_json}"


def verify_delivery_signature(
    body_json: str,
    timestamp: Optional[str],
    signature: Optional[str],
    verify_keys: Sequence[str],
    max_skew_seconds: int = _DEFAULT_MAX_SKEW_SECONDS,
    *,
    now: Optional[int] = None,
) -> bool:
    """Verify a connector-to-gateway inbound delivery signature.

    ``body_json`` MUST be the exact request body bytes decoded as UTF-8 -- the
    connector signs over the literal serialized body, so the gateway verifies
    over the literal received body.  Re-serializing the parsed JSON would
    reorder keys, re-quote escapes, or drop whitespace, and would silently fail
    every signature.

    The timestamp must also fall within ``max_skew_seconds`` of *now*; the
    window is symmetric, which tolerates clock skew in both directions without
    letting an arbitrarily old delivery replay.

    Args:
        body_json: The literal received request body as UTF-8 text.
        timestamp: The ``x-relay-timestamp`` header value, unix seconds.
        signature: The ``x-relay-signature`` header value, hex digest.
        verify_keys: Delivery keys to try, primary first, for rotation.
        max_skew_seconds: Replay window, applied symmetrically.
        now: Override for the current time, for deterministic tests.  Defaults
            to ``int(time.time())``.

    Returns:
        True only when the timestamp is within the replay window **and** the
        HMAC matches one of the keys.  Missing headers, a non-integer
        timestamp, or a bad signature all return False rather than raising, so a
        caller can reject unauthenticated deliveries inline.
    """
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    current = now if now is not None else int(time.time())
    if abs(current - ts) > max_skew_seconds:
        return False
    return verify_signature(_delivery_payload(ts, body_json), signature, verify_keys)
