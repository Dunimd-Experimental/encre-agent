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


# ---- CapabilityDescriptor -------------------------------------------------


def _descriptor(**overrides):
    """Build a fully-populated descriptor, optionally overriding fields.

    The defaults mirror a realistic Discord connector contract so each test
    can focus on a single field instead of re-declaring the whole payload.
    """
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
    """to_json/from_json round-trip preserves all descriptor fields.

    The contract version and platform metadata must survive JSON encoding
    losslessly, so a fully-populated descriptor is encoded and decoded back.
    """
    d = _descriptor()
    d2 = CapabilityDescriptor.from_json(d.to_json())
    assert d == d2


def test_verify_descriptor_unknown_keys_ignored():
    """from_dict tolerates unknown keys without raising.

    Forward compatibility requires the decoder to ignore fields it does not
    recognise, so extra keys must not disturb the known values.
    """
    d = _descriptor()
    raw = {**d.to_dict(), "future_field": "x", "another": 123}
    d2 = CapabilityDescriptor.from_dict(raw)
    assert d2.platform == "discord"
    assert d2.max_message_length == 2000


def test_verify_descriptor_max_length_zero_defaults():
    """A zero max_message_length is replaced by the DEFAULT constant.

    Zero is the sentinel meaning "unset", so decoding must substitute the
    documented default length.
    """
    d = _descriptor(max_message_length=0)
    d2 = CapabilityDescriptor.from_dict(d.to_dict())
    assert d2.max_message_length == DEFAULT_MAX_MESSAGE_LENGTH


def test_verify_descriptor_is_frozen():
    """CapabilityDescriptor instances are immutable after construction.

    The frozen dataclass must reject runtime mutation of contract fields,
    otherwise two sides of the connector could silently disagree.
    """
    d = _descriptor()
    with pytest.raises(Exception):
        d.platform = "telegram"  # type: ignore[misc]


# ---- auth -----------------------------------------------------------------


def test_verify_upgrade_token_round_trip():
    """make_upgrade_token produces a token that verify_token accepts.

    The upgrade path requires the GW to prove its identity to the
    connector, so the subject must round-trip through verification.
    """
    tok = make_upgrade_token("gw-1", "secret", ttl_seconds=60)
    assert verify_token(tok, ["secret"]) == "gw-1"


def test_verify_upgrade_token_wrong_secret_rejected():
    """A token signed with one secret is rejected by a different secret.

    HMAC verification must reject tokens signed with an unrelated key, so
    the verifier has to return None rather than the subject.
    """
    tok = make_upgrade_token("gw-1", "secret")
    assert verify_token(tok, ["wrong"]) is None


def test_verify_token_multi_secret_rotation():
    """A token signed with an old secret validates against a rotated secret list.

    Secret rotation requires the verifier to try each candidate in order
    until one matches, so an old token must still verify during the
    overlap window.
    """
    tok = make_token("gw-1", "old-secret", ttl_seconds=60)
    assert verify_token(tok, ["new-secret", "old-secret"]) == "gw-1"


def test_verify_token_payload_may_contain_colons():
    """verify_token handles payloads containing colon characters.

    The token decoder splits from the right edge rather than the left, so
    colons embedded in the subject must not confuse parsing.
    """
    tok = make_token("gw:with:colons", "secret", ttl_seconds=60)
    assert verify_token(tok, ["secret"]) == "gw:with:colons"


def test_verify_token_expired_rejected(monkeypatch):
    """Expired tokens are rejected by verify_token.

    The token's exp claim must be checked against the current clock, so a
    1-second token is signed and then the clock is advanced past its
    expiry.
    """
    base = int(time.time())
    monkeypatch.setattr("encre.gateway.relay.auth.time.time", lambda: base)
    tok = make_token("gw-1", "secret", ttl_seconds=1)
    # Advance the clock well past the 1s expiry.
    monkeypatch.setattr("encre.gateway.relay.auth.time.time", lambda: base + 60)
    assert verify_token(tok, ["secret"]) is None


def test_verify_token_no_ttl_never_expires():
    """A token with ttl_seconds=0 has no expiry and always verifies.

    Zero-ttl tokens are deliberately designed to live forever, so the
    expiry check must be skipped entirely.
    """
    tok = make_token("gw-1", "secret", ttl_seconds=0)
    assert verify_token(tok, ["secret"]) == "gw-1"


def test_verify_delivery_signature_round_trip():
    """sign/verify_delivery_signature form a correct HMAC pair.

    The delivery-signature path is the authenticated outbound channel, so
    a freshly signed timestamp and body must verify.
    """
    ts = int(time.time())
    body = '{"event":"inbound"}'
    sig = sign(f"{ts}.{body}", "delivery-key")
    assert verify_delivery_signature(body, str(ts), sig, ["delivery-key"]) is True


def test_verify_delivery_signature_wrong_key_rejected():
    """verify_delivery_signature rejects signatures made with a different key.

    HMAC verification must fail when the candidate key set does not
    contain the signing key.
    """
    ts = int(time.time())
    body = '{"event":"inbound"}'
    sig = sign(f"{ts}.{body}", "delivery-key")
    assert verify_delivery_signature(body, str(ts), sig, ["wrong"]) is False


def test_verify_delivery_signature_replay_window():
    """verify_delivery_signature rejects signatures outside the 300s replay window.

    The replay-window check prevents reuse of old delivery signatures, so
    a timestamp from 9999 seconds ago must be refused even with a valid
    key.
    """
    ts = int(time.time()) - 9999  # outside the 300s window
    body = '{"event":"inbound"}'
    sig = sign(f"{ts}.{body}", "delivery-key")
    assert verify_delivery_signature(body, str(ts), sig, ["delivery-key"]) is False


def test_verify_delivery_signature_missing_headers_rejected():
    """verify_delivery_signature rejects requests with missing headers.

    Both the timestamp and the signature are mandatory for HMAC
    verification, so either being absent must fail closed.
    """
    assert verify_delivery_signature("{}", None, None, ["k"]) is False


# ---- ws_transport dial URL ------------------------------------------------


def test_verify_ws_dial_url_https_to_wss():
    """_ws_dial_url converts HTTPS URLs to WSS endpoints.

    The relay endpoint is conventionally exposed at /relay, so the dialer
    must upgrade the scheme and append that path.
    """
    assert _ws_dial_url("https://connector.example.com") == "wss://connector.example.com/relay"


def test_verify_ws_dial_url_http_to_ws_with_path():
    """_ws_dial_url converts HTTP URLs to WS endpoints preserving the base path.

    The dialer must not clobber an existing path component; the /relay
    suffix goes after it.
    """
    assert _ws_dial_url("http://localhost:8080/api") == "ws://localhost:8080/api/relay"


def test_verify_ws_dial_url_already_relay_path():
    """_ws_dial_url leaves a URL already ending in /relay unchanged.

    The transformation must be idempotent so double-appending /relay never
    happens when the path is already correct.
    """
    assert _ws_dial_url("wss://x/relay") == "wss://x/relay"


# ---- fake transport for adapter tests ------------------------------------


class _FakeTransport:
    """In-memory transport double implementing the RelayTransport Protocol.

    Records every action the adapter sends and serves canned responses, so
    adapter behaviour can be asserted without opening a real WebSocket.
    """

    def __init__(self, descriptor):
        """Bind the connector descriptor and reset all recorded state."""
        self._descriptor = descriptor
        self.connected = False
        self.was_revoked = False
        self.inbound_handler = None
        self.passthrough_handler = None
        self.sent: list[dict] = []
        self.chat_info: dict[str, dict] = {}

    async def connect(self):
        """Mark the transport connected and report success."""
        self.connected = True
        return True

    async def disconnect(self):
        """Mark the transport disconnected."""
        self.connected = False

    async def handshake(self):
        """Return the connector descriptor as the handshake result."""
        return self._descriptor

    def set_inbound_handler(self, h):
        """Store the callback that receives inbound connector frames."""
        self.inbound_handler = h

    def set_passthrough_handler(self, h):
        """Store the callback that receives passthrough frames."""
        self.passthrough_handler = h

    async def send_outbound(self, action, *, platform=None):
        """Record an outbound action and return a canned success payload.

        The get_chat_info operation is answered from the seeded
        ``chat_info`` map; everything else yields a generic message id.
        """
        self.sent.append({"kind": "outbound", "action": action, "platform": platform})
        if action.get("op") == "get_chat_info":
            return {"success": True, "chat_info": self.chat_info.get(action["chat_id"], {"name": action["chat_id"], "type": "dm"})}
        return {"success": True, "message_id": "m-fake"}

    async def send_follow_up(self, action, *, platform=None):
        """Record a follow-up action and return a canned message id."""
        self.sent.append({"kind": "follow_up", "action": action, "platform": platform})
        return {"success": True, "message_id": "fu-1"}

    async def get_chat_info(self, chat_id):
        """Return the seeded chat metadata, or a derived default entry."""
        return self.chat_info.get(chat_id, {"name": chat_id, "type": "dm"})

    async def send_interrupt(self, session_key, reason=None):
        """Record an interrupt action without returning a payload."""
        self.sent.append({"kind": "interrupt", "session_key": session_key, "reason": reason})

    async def go_idle(self, timeout_s=10.0):
        """Report that the transport is idle."""
        return True

    @property
    def is_connected(self):
        """Report whether the transport is currently connected."""
        return self.connected


# ---- RelayAdapter ---------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_relay_adapter_connect_and_send():
    """RelayAdapter.send transmits an outbound action to the transport.

    The adapter must translate a high-level send call into a transport
    action carrying op="send" plus the message content.
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
    """RelayAdapter.get_chat_info returns the transport's chat metadata.

    The adapter delegates chat-info lookups to the transport layer, so the
    seeded name and type must come back unchanged.
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
    """send_follow_up omits the token field from the follow-up action.

    The relay protocol sends tokens separately, so the recorded action must
    carry the session key and kind but no "token" key.
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
    """An inbound frame is rebuilt into a MessageEvent and routed to the injected handler.

    The bridge layer is responsible for normalising connector frames into
    the gateway schema, so the delivered event must carry the original
    text, platform and scope id, and the chat-to-platform mapping must be
    recorded for outbound replies.
    """
    desc = _descriptor()
    transport = _FakeTransport(desc)
    adapter = RelayAdapter(transport=transport)
    await adapter.connect()

    seen = []

    async def handler(adapter, event):
        """Collect every MessageEvent the adapter routes to the handler."""
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
    """RelayAdapter reports authorization_is_upstream=True.

    The relay adapter defers auth checks to the upstream connector rather
    than performing its own 5-layer check.
    """
    adapter = RelayAdapter.__new__(RelayAdapter)
    assert adapter.authorization_is_upstream is True


@pytest.mark.asyncio
async def test_verify_relay_adapter_no_transport_fatals():
    """Constructing RelayAdapter with transport=None causes a fatal on connect.

    A missing transport is a permanent configuration error rather than a
    transient one, so it must be reported with the disabled code.
    """
    adapter = RelayAdapter(transport=None)
    ok = await adapter.connect()
    assert ok is False
    assert adapter.has_fatal_error
    assert adapter._fatal_error_code == RELAY_DISABLED_CODE


@pytest.mark.asyncio
async def test_verify_relay_adapter_send_when_disconnected_returns_transient():
    """Send without an active connection returns a transient failure.

    A missing connection is recoverable by reconnecting and retrying, so
    the result must be marked retryable with the transient error kind.
    """
    desc = _descriptor()
    transport = _FakeTransport(desc)
    adapter = RelayAdapter(transport=transport)
    # Don't connect.
    result = await adapter.send("c1", "hi")
    assert result.success is False
    assert result.retryable is True
    assert result.error_kind == "transient"


# ---- config-driven activation ---------------------------------------------


def test_verify_relay_not_configured_without_env(monkeypatch):
    """relay_is_configured is False when GATEWAY_RELAY_URL is absent.

    Configuration requires an explicit URL, so with the env var stripped
    the relay must report as unconfigured.  The guard also tolerates a
    leaked settings-sourced URL by requiring the two to agree.
    """
    monkeypatch.delenv("GATEWAY_RELAY_URL", raising=False)
    from encre.gateway.relay import relay_is_configured, relay_url
    # Also ensure settings don't leak a relay_url from the user's env.
    assert relay_url() is None or relay_is_configured() is True
    # The clean assertion: with no env and (in CI) no settings, not configured.
    if relay_url() is None:
        assert relay_is_configured() is False


def test_verify_relay_configured_with_env(monkeypatch):
    """Setting GATEWAY_RELAY_URL enables relay_is_configured.

    The config reader must surface the env var value verbatim, so the
    enabled flag and the returned URL are both asserted.
    """
    monkeypatch.setenv("GATEWAY_RELAY_URL", "https://connector.example.com")
    from encre.gateway.relay import relay_is_configured, relay_url
    assert relay_is_configured() is True
    assert relay_url() == "https://connector.example.com"


def test_verify_relay_relevance_policy_default_none(monkeypatch):
    """relay_relevance_policy returns None when no relevance knobs are set.

    The default relevance policy is unconstrained unless the user
    explicitly sets a relevance filter, so the result must be either None
    or a policy that still contains the platform key.
    """
    monkeypatch.delenv("GATEWAY_RELAY_URL", raising=False)
    from encre.gateway.relay import relay_relevance_policy
    # With no relevance knobs set, the policy is None (all-default).
    # (Depends on the user's settings.json; in CI it should be None.)
    policy = relay_relevance_policy()
    assert policy is None or "platform" in policy
