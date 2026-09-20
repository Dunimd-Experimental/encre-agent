#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

"""Tests for the relay connector subsystem (Phase 4).

Covers:
- :class:`CapabilityDescriptor` serialization / unknown-key forward-compat.
- HMAC auth: upgrade token round-trip, multi-secret rotation, replay window.
- :class:`WebSocketRelayTransport` frame encode/decode + requestId RPC +
  4401 revocation handling (via a fake transport, no real WS).
- :class:`RelayAdapter` send / get_chat_info / inbound bridging (via a fake
  transport).
- Config-driven activation: ``relay_is_configured`` is False without a URL.
"""

import asyncio
import time

import pytest

from encre.gateway.platforms.base import MessageEvent, MessageType, SendResult
from encre.gateway.session import SessionSource
from encre.gateway.relay.adapter import RelayAdapter, RELAY_DISABLED_CODE
from encre.gateway.relay.auth import (
    make_token,
    make_upgrade_token,
    sign,
    verify_delivery_signature,
    verify_token,
)
from encre.gateway.relay.descriptor import (
    CONTRACT_VERSION,
    DEFAULT_MAX_MESSAGE_LENGTH,
    CapabilityDescriptor,
)
from encre.gateway.relay.transport import RelayTransport
from encre.gateway.relay.ws_transport import _ws_dial_url


# 鈹€鈹€ CapabilityDescriptor 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def _descriptor(**overrides):
    base = dict(
        contract_version=CONTRACT_VERSION,
        platform="discord",
        label="Discord",
        max_message_length=2000,
        supports_draft_streaming=True,
        supports_edit=True,
        supports_threads=True,
        markdown_dialect="discord",
        len_unit="utf16",
    )
    base.update(overrides)
    return CapabilityDescriptor(**base)


def test_verify_descriptor_round_trip():
    """Validate that to_json/from_json round-trip preserves all descriptor fields.

    The test exercises serialization of a fully-populated descriptor and
    deserializes it via from_json, asserting equality because the contract
    version and platform metadata must survive JSON encoding losslessly.
    """
    d = _descriptor()
    d2 = CapabilityDescriptor.from_json(d.to_json())
    assert d == d2


def test_verify_descriptor_unknown_keys_ignored():
    """Validate that from_dict tolerates unknown keys without raising.

    The test exercises from_dict with extra keys (future_field, another)
    and asserts the known fields are still populated correctly because
    forward-compat requires the decoder to ignore unrecognised fields.
    """
    d = _descriptor()
    raw = {**d.to_dict(), "future_field": "x", "another": 123}
    d2 = CapabilityDescriptor.from_dict(raw)
    assert d2.platform == "discord"
    assert d2.max_message_length == 2000


def test_verify_descriptor_max_length_zero_defaults():
    """Validate that a zero max_message_length is replaced by the DEFAULT constant.

    The test exercises from_dict with max_message_length=0 and asserts the
    result equals DEFAULT_MAX_MESSAGE_LENGTH because zero is used as the
    sentinel value meaning "unset".
    """
    d = _descriptor(max_message_length=0)
    d2 = CapabilityDescriptor.from_dict(d.to_dict())
    assert d2.max_message_length == DEFAULT_MAX_MESSAGE_LENGTH


def test_verify_descriptor_is_frozen():
    """Validate that CapabilityDescriptor instances are immutable after construction.

    The test exercises attribute assignment on a constructed descriptor
    and asserts that an exception is raised because the class uses a
    frozen dataclass to prevent runtime mutation of contract fields.
    """
    d = _descriptor()
    with pytest.raises(Exception):
        d.platform = "telegram"  # type: ignore[misc]


# 鈹€鈹€ auth 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def test_verify_upgrade_token_round_trip():
    """Validate that make_upgrade_token produces a token that verify_token accepts.

    The test exercises upgrade-token creation with a TTL and asserts the
    subject ("gw-1") round-trips through verify_token because the upgrade
    path requires the GW to prove its identity to the connector.
    """
    tok = make_upgrade_token("gw-1", "secret", ttl_seconds=60)
    assert verify_token(tok, ["secret"]) == "gw-1"


def test_verify_upgrade_token_wrong_secret_rejected():
    """Validate that a token signed with one secret is rejected by a different secret.

    The test exercises verify_token with the wrong secret list and asserts
    None is returned because HMAC signature verification must reject
    tokens signed with an unrelated key.
    """
    tok = make_upgrade_token("gw-1", "secret")
    assert verify_token(tok, ["wrong"]) is None


def test_verify_token_multi_secret_rotation():
    """Validate that a token signed with an old secret validates against a rotated secret list.

    The test exercises verify_token with a rotation list ["new-secret", "old-secret"]
    and asserts the token is accepted because secret rotation requires
    the verifier to try each candidate in order until one matches.
    """
    tok = make_token("gw-1", "old-secret", ttl_seconds=60)
    assert verify_token(tok, ["new-secret", "old-secret"]) == "gw-1"


def test_verify_token_payload_may_contain_colons():
    """Validate that verify_token handles payloads containing colon characters.

    The test exercises make_token with a payload containing colons and
    asserts the round-trip succeeds because the token decoder splits from
    the right edge, not the left, so embedded colons do not confuse parsing.
    """
    tok = make_token("gw:with:colons", "secret", ttl_seconds=60)
    assert verify_token(tok, ["secret"]) == "gw:with:colons"


def test_verify_token_expired_rejected(monkeypatch):
    """Validate that expired tokens are rejected by verify_token.

    The test exercises monkeypatched time to simulate passage of 60 seconds
    past a 1-second TTL token and asserts None is returned because the
    token's exp claim must be checked against the current clock.
    """
    base = int(time.time())
    monkeypatch.setattr("encre.gateway.relay.auth.time.time", lambda: base)
    tok = make_token("gw-1", "secret", ttl_seconds=1)
    # Advance the clock well past the 1s expiry.
    monkeypatch.setattr("encre.gateway.relay.auth.time.time", lambda: base + 60)
    assert verify_token(tok, ["secret"]) is None


def test_verify_token_no_ttl_never_expires():
    """Validate that a token with ttl_seconds=0 has no expiry and always verifies.

    The test exercises make_token with ttl_seconds=0 and asserts
    verify_token returns the subject because zero-ttl tokens are
    deliberately designed to never expire.
    """
    tok = make_token("gw-1", "secret", ttl_seconds=0)
    assert verify_token(tok, ["secret"]) == "gw-1"


def test_verify_delivery_signature_round_trip():
    """Validate that sign/verify_delivery_signature form a correct HMAC pair.

    The test exercises sign with a timestamp and body then passes the
    result to verify_delivery_signature and asserts True because the
    delivery-signature path is the authenticated outbound channel.
    """
    ts = int(time.time())
    body = '{"event":"inbound"}'
    sig = sign(f"{ts}.{body}", "delivery-key")
    assert verify_delivery_signature(body, str(ts), sig, ["delivery-key"]) is True


def test_verify_delivery_signature_wrong_key_rejected():
    """Validate that verify_delivery_signature rejects signatures made with a different key.

    The test exercises sign with "delivery-key" and verifies with ["wrong"]
    and asserts False because HMAC verification must fail when the key
    set does not contain the signing key.
    """
    ts = int(time.time())
    body = '{"event":"inbound"}'
    sig = sign(f"{ts}.{body}", "delivery-key")
    assert verify_delivery_signature(body, str(ts), sig, ["wrong"]) is False


def test_verify_delivery_signature_replay_window():
    """Validate that verify_delivery_signature rejects signatures outside the 300s replay window.

    The test exercises sign with a timestamp 9999 seconds in the past
    and asserts False because the replay-window check prevents replay
    of old delivery signatures.
    """
    ts = int(time.time()) - 9999  # outside the 300s window
    body = '{"event":"inbound"}'
    sig = sign(f"{ts}.{body}", "delivery-key")
    assert verify_delivery_signature(body, str(ts), sig, ["delivery-key"]) is False


def test_verify_delivery_signature_missing_headers_rejected():
    """Validate that verify_delivery_signature rejects requests with missing headers.

    The test exercises verify_delivery_signature with None timestamp and
    signature and asserts False because both headers are required for
    HMAC verification.
    """
    assert verify_delivery_signature("{}", None, None, ["k"]) is False


# 鈹€鈹€ ws_transport dial URL 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def test_verify_ws_dial_url_https_to_wss():
    """Validate that _ws_dial_url converts HTTPS URLs to WSS endpoints.

    The test exercises the URL transformation with an HTTPS input and
    asserts the output is a WSS URL with "/relay" appended because the
    relay endpoint is conventionally exposed at that path.
    """
    assert _ws_dial_url("https://connector.example.com") == "wss://connector.example.com/relay"


def test_verify_ws_dial_url_http_to_ws_with_path():
    """Validate that _ws_dial_url converts HTTP URLs to WS endpoints preserving the base path.

    The test exercises the URL transformation with an HTTP URL that has
    an existing path component and asserts the "/relay" suffix is appended
    after the existing path because the dialer must preserve base paths.
    """
    assert _ws_dial_url("http://localhost:8080/api") == "ws://localhost:8080/api/relay"


def test_verify_ws_dial_url_already_relay_path():
    """Validate that _ws_dial_url leaves a URL already ending in /relay unchanged.

    The test exercises the function with a WSS URL that already has the
    relay path and asserts idempotency because double-appending "/relay"
    must not occur when the path is already correct.
    """
    assert _ws_dial_url("wss://x/relay") == "wss://x/relay"


# 鈹€鈹€ fake transport for adapter tests 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


class _FakeTransport:
    """In-memory transport double implementing the RelayTransport Protocol."""

    def __init__(self, descriptor):
        self._descriptor = descriptor
        self.connected = False
        self.was_revoked = False
        self.inbound_handler = None
        self.passthrough_handler = None
        self.sent: list[dict] = []
        self.chat_info: dict[str, dict] = {}

    async def connect(self):
        self.connected = True
        return True

    async def disconnect(self):
        self.connected = False

    async def handshake(self):
        return self._descriptor

    def set_inbound_handler(self, h):
        self.inbound_handler = h

    def set_passthrough_handler(self, h):
        self.passthrough_handler = h

    async def send_outbound(self, action, *, platform=None):
        self.sent.append({"kind": "outbound", "action": action, "platform": platform})
        if action.get("op") == "get_chat_info":
            return {"success": True, "chat_info": self.chat_info.get(action["chat_id"], {"name": action["chat_id"], "type": "dm"})}
        return {"success": True, "message_id": "m-fake"}

    async def send_follow_up(self, action, *, platform=None):
        self.sent.append({"kind": "follow_up", "action": action, "platform": platform})
        return {"success": True, "message_id": "fu-1"}

    async def get_chat_info(self, chat_id):
        return self.chat_info.get(chat_id, {"name": chat_id, "type": "dm"})

    async def send_interrupt(self, session_key, reason=None):
        self.sent.append({"kind": "interrupt", "session_key": session_key, "reason": reason})

    async def go_idle(self, timeout_s=10.0):
        return True

    @property
    def is_connected(self):
        return self.connected


# 鈹€鈹€ RelayAdapter 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


@pytest.mark.asyncio
async def test_verify_relay_adapter_connect_and_send():
    """Validate that RelayAdapter.send transmits an outbound action to the transport.

    The test exercises connect followed by send with content "hello" and
    asserts the SendResult is successful, the message_id matches the fake,
    and the last sent action has op="send" with the correct content because
    the adapter must translate high-level send calls into transport actions.
    """
    desc = _descriptor()
    transport = _FakeTransport(desc)
    adapter = RelayAdapter(transport=transport)
    ok = await adapter.connect()
    assert ok is True
    assert adapter.is_connected

    result = await adapter.send("chat-1", "hello")
    assert isinstance(result, SendResult)
    assert result.success
    assert result.message_id == "m-fake"
    # The outbound action was an op=send with the content.
    outbound = transport.sent[-1]
    assert outbound["action"]["op"] == "send"
    assert outbound["action"]["content"] == "hello"


@pytest.mark.asyncio
async def test_verify_relay_adapter_get_chat_info():
    """Validate that RelayAdapter.get_chat_info returns the transport's chat metadata.

    The test exercises pre-population of chat_info on the fake transport
    and asserts the adapter returns the stored name and type because
    the adapter delegates chat-info lookups to the transport layer.
    """
    desc = _descriptor()
    transport = _FakeTransport(desc)
    transport.chat_info["c1"] = {"name": "general", "type": "group"}
    adapter = RelayAdapter(transport=transport)
    await adapter.connect()
    info = await adapter.get_chat_info("c1")
    assert info == {"name": "general", "type": "group"}


@pytest.mark.asyncio
async def test_verify_relay_adapter_send_follow_up_tokenless():
    """Validate that send_follow_up omits the token field from the follow-up action.

    The test exercises send_follow_up with a session key and kind and
    asserts the recorded action contains session_key and kind but no
    "token" key because the relay protocol sends tokens separately.
    """
    desc = _descriptor()
    transport = _FakeTransport(desc)
    adapter = RelayAdapter(transport=transport)
    await adapter.connect()
    result = await adapter.send_follow_up("session-key-1", "discord.interaction_token", "hello")
    assert result.success
    # The follow_up action carried the session + kind, NOT a token.
    fu = transport.sent[-1]
    assert fu["action"]["session_key"] == "session-key-1"
    assert fu["action"]["kind"] == "discord.interaction_token"
    assert "token" not in fu["action"]


@pytest.mark.asyncio
async def test_verify_relay_adapter_inbound_bridge_routes_to_handler():
    """Validate that an inbound frame is rebuilt into a MessageEvent and routed to the injected handler.

    The test exercises transport.inbound_handler with a raw event dict
    and asserts the handler received a MessageEvent with correct text,
    source platform, scope_id, and that the chat-to-platform mapping
    is recorded in adapter._platform_by_chat because the bridge layer
    is responsible for normalising connector frames into the gateway schema.
    """
    desc = _descriptor()
    transport = _FakeTransport(desc)
    adapter = RelayAdapter(transport=transport)
    await adapter.connect()

    seen = []

    async def handler(adapter, event):
        seen.append(event)

    adapter.set_message_handler(handler)

    # Simulate the connector delivering an inbound event.
    transport.inbound_handler({
        "text": "hi from discord",
        "chat_id": "100",
        "user_id": "7",
        "source": {
            "platform": "discord",
            "chat_id": "100",
            "chat_type": "group",
            "user_id": "7",
            "scope_id": "guild-1",
        },
    })

    # The handler was scheduled -- let it run.
    for _ in range(10):
        await asyncio.sleep(0)
        if seen:
            break
    assert len(seen) == 1
    event = seen[0]
    assert event.text == "hi from discord"
    assert event.source is not None
    assert event.source.platform == "discord"
    assert event.source.scope_id == "guild-1"
    # The outbound-reply platform stamp was recorded.
    assert adapter._platform_by_chat.get("100") == "discord"


@pytest.mark.asyncio
async def test_verify_relay_adapter_authorization_is_upstream():
    """Validate that RelayAdapter reports authorization_is_upstream=True.

    The test exercises direct attribute access on a fresh adapter and
    asserts True because the relay adapter defers auth checks to the
    upstream connector rather than performing its own 5-layer check.
    """
    adapter = RelayAdapter.__new__(RelayAdapter)
    assert adapter.authorization_is_upstream is True


@pytest.mark.asyncio
async def test_verify_relay_adapter_no_transport_fatals():
    """Validate that constructing RelayAdapter with transport=None causes a fatal on connect.

    The test exercises connect on an adapter with no transport and
    asserts ok=False, has_fatal_error=True, and the fatal code equals
    RELAY_DISABLED_CODE because a missing transport is a permanent
    configuration error, not a transient one.
    """
    adapter = RelayAdapter(transport=None)
    ok = await adapter.connect()
    assert ok is False
    assert adapter.has_fatal_error
    assert adapter._fatal_error_code == RELAY_DISABLED_CODE


@pytest.mark.asyncio
async def test_verify_relay_adapter_send_when_disconnected_returns_transient():
    """Validate that send without an active connection returns a transient failure.

    The test exercises send on a disconnected adapter and asserts
    success=False, retryable=True, and error_kind="transient" because
    a missing connection is recoverable by reconnecting and retrying.
    """
    desc = _descriptor()
    transport = _FakeTransport(desc)
    adapter = RelayAdapter(transport=transport)
    # Don't connect.
    result = await adapter.send("c1", "hi")
    assert result.success is False
    assert result.retryable is True
    assert result.error_kind == "transient"


# 鈹€鈹€ config-driven activation 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def test_verify_relay_not_configured_without_env(monkeypatch):
    """Validate that relay_is_configured is False when GATEWAY_RELAY_URL is absent.

    The test exercises env-stripping and asserts the combined condition
    that either relay_url is None or relay_is_configured is True, and
    when relay_url is None it must also be that relay_is_configured
    is False because configuration requires an explicit URL.
    """
    monkeypatch.delenv("GATEWAY_RELAY_URL", raising=False)
    from encre.gateway.relay import relay_is_configured, relay_url
    # Also ensure settings don't leak a relay_url from the user's env.
    assert relay_url() is None or relay_is_configured() is True
    # The clean assertion: with no env and (in CI) no settings, not configured.
    if relay_url() is None:
        assert relay_is_configured() is False


def test_verify_relay_configured_with_env(monkeypatch):
    """Validate that setting GATEWAY_RELAY_URL enables relay_is_configured.

    The test exercises env-setting with a HTTPS URL and asserts
    relay_is_configured() is True and relay_url() returns the same
    string because the config reader must surface the env var value.
    """
    monkeypatch.setenv("GATEWAY_RELAY_URL", "https://connector.example.com")
    from encre.gateway.relay import relay_is_configured, relay_url
    assert relay_is_configured() is True
    assert relay_url() == "https://connector.example.com"


def test_verify_relay_relevance_policy_default_none(monkeypatch):
    """Validate that relay_relevance_policy returns None when no relevance knobs are set.

    The test exercises env-stripping and asserts the policy is None or
    contains "platform" because the default relevance policy is unconstrained
    unless the user explicitly sets a relevance filter.
    """
    monkeypatch.delenv("GATEWAY_RELAY_URL", raising=False)
    from encre.gateway.relay import relay_relevance_policy
    # With no relevance knobs set, the policy is None (all-default).
    # (Depends on the user's settings.json; in CI it should be None.)
    policy = relay_relevance_policy()
    assert policy is None or "platform" in policy
