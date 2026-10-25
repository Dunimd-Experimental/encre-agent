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

"""Tests for gateway authorization (Phase 2a).

Covers:
- :class:`PairingStore` code mint / redeem / expiry / persistence.
- :class:`AuthorizationChecker` 5-layer precedence (platform allow-all >
  allowlist > pairing > global allow-all > deny), with settings + env sources.
- ``/pair`` slash command in :meth:`BaseAdapter.handle_message`.
- Reject notice sent on denial; authorized message dispatched.
"""

import asyncio
import os
import time

import pytest

from encre.gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from encre.gateway.session import SessionSource
from encre.gateway.authz import (
    AuthorizationChecker,
    LAYER_ALLOWLIST,
    LAYER_DEFAULT_ALLOW,
    LAYER_DENY,
    LAYER_GLOBAL_ALLOW_ALL,
    LAYER_PAIRING,
    LAYER_PLATFORM_ALLOW_ALL,
)
from encre.gateway.pairing import PairingStore


# ---- PairingStore ----

@pytest.fixture
def pairing(tmp_path):
    """Create a transient PairingStore backed by a temp-path JSON file, cleaned up after the test."""
    store = PairingStore(path=tmp_path / "pairing.json")
    yield store
    store.close()


def test_verify_pairing_mint_and_redeem(pairing):
    """Validate that a freshly minted pairing code can be redeemed once and marks the user paired.

    The test creates a code with a 600-second TTL, asserts the code is 6 characters long,
    redeems it for platform="telegram", user_id="42", and asserts is_paired returns True,
    confirming the mint-then-redeem flow establishes the pairing relationship.
    """
    code = pairing.create_code(ttl=600)
    assert len(code) == 6, "Pairing code must be exactly 6 characters long."
    assert pairing.redeem(code, "telegram", "42") is True, "Redeem must succeed for a valid code."
    assert pairing.is_paired("telegram", "42") is True, "User must be marked as paired after successful redeem."


def test_verify_pairing_redeem_unknown_code_fails(pairing):
    """Validate that redeeming an unknown code returns False and does not create a pairing.

    The test passes the literal string "NOPE" and asserts both redeem and is_paired return
    falsy values, confirming the store rejects garbage codes without side effects.
    """
    assert pairing.redeem("NOPE", "telegram", "42") is False, "Unknown code must be rejected."
    assert pairing.is_paired("telegram", "42") is False, "Unknown code must not create a pairing."


def test_verify_pairing_redeem_twice_fails(pairing):
    """Validate that a code can only be redeemed once (one-time-use invariant).

    The test redeems a code successfully, then attempts a second redeem with the same code
    but a different user_id and asserts it fails, confirming the one-time-use guarantee that
    prevents code replay attacks.
    """
    code = pairing.create_code()
    assert pairing.redeem(code, "telegram", "42") is True, "First redeem must succeed."
    # Second redeem of the same code is rejected.
    assert pairing.redeem(code, "telegram", "99") is False, "Reusing a code must be rejected."
    assert pairing.is_paired("telegram", "99") is False, "Code reuse must not create a pairing."


def test_verify_pairing_expired_code_is_rejected(pairing):
    """Validate that a code created with a negative TTL is already expired and cannot be redeemed.

    The test creates a code with ttl=-1 (already expired at creation time) and asserts
    redeem returns False and is_paired remains False, confirming expiry is checked at
    redeem time rather than at create time.
    """
    code = pairing.create_code(ttl=-1)  # already expired
    assert pairing.redeem(code, "telegram", "42") is False, "Expired code must be rejected at redeem time."
    assert pairing.is_paired("telegram", "42") is False, "Expired code must not create a pairing."


def test_verify_pairing_unpair_removes_entry(pairing):
    """Validate that unpair removes an existing pairing and is idempotent.

    The test mints and redeems a code, asserts the user is paired, unpairs them, asserts
    is_paired returns False, and then calls unpair again to confirm it returns False
    (idempotent deletion, no error on double-unpair).
    """
    code = pairing.create_code()
    pairing.redeem(code, "telegram", "42")
    assert pairing.is_paired("telegram", "42") is True, "User must be paired after redeem."
    assert pairing.unpair("telegram", "42") is True, "Unpair must succeed for a paired user."
    assert pairing.is_paired("telegram", "42") is False, "User must be unpaired after unpair."
    assert pairing.unpair("telegram", "42") is False, "Unpair must be idempotent (returns False when already gone)."


def test_verify_pairing_persists_across_store_reopen(tmp_path):
    """Validate that pairing state survives store close and reopen (JSON serialisation).

    The test creates a store, mints and redeems a code, closes the store, reopens a new
    store pointing at the same JSON file, and asserts the pairing is still present,
    confirming disk persistence works end-to-end.
    """
    p = tmp_path / "pairing.json"
    s1 = PairingStore(path=p)
    code = s1.create_code()
    s1.redeem(code, "telegram", "42")
    s1.close()
    s2 = PairingStore(path=p)
    assert s2.is_paired("telegram", "42") is True, "Pairing must survive store reopen."
    s2.close()


def test_verify_pairing_is_per_platform_user(pairing):
    """Validate that pairing is scoped to (platform, user_id) and not shared across platforms.

    The test redeems a code for platform="telegram", user_id="42" and asserts that the
    same user_id on platform="discord" is not considered paired, confirming the pairing
    key is the tuple (platform, user_id).
    """
    code = pairing.create_code()
    pairing.redeem(code, "telegram", "42")
    # Same user_id on a different platform is NOT paired.
    assert pairing.is_paired("discord", "42") is False, "Pairing must be scoped to the originating platform."


# ---- AuthorizationChecker 5-layer precedence ----


def _checker(settings=None, pairing=None, monkeypatch_env=None):
    """Build an AuthorizationChecker wired to the given settings dict and pairing store.

    The settings mapping is wrapped in a zero-argument function so the checker sees the
    same callable surface as a live EncreConfig object.

    Args:
        settings: Mapping of config keys to string values (simulates EncreConfig).
        pairing: Optional PairingStore to inject into the checker.
        monkeypatch_env: Unused here; kept for API consistency with callers.

    Returns:
        An AuthorizationChecker instance wired to the provided config and pairing.
    """
    cfg = settings or {}

    def config_fn():
        """Return the captured settings mapping as the current config snapshot."""
        return cfg

    return AuthorizationChecker(pairing=pairing, config_fn=config_fn)


def _src(platform="telegram", user_id="42", user_id_alt=None):
    """Build a SessionSource for the given platform and user identity.

    Args:
        platform: Platform identifier string (e.g. "telegram", "discord").
        user_id: Primary user ID string.
        user_id_alt: Optional alternate user ID (e.g. UUID) for platforms that decouple them.

    Returns:
        A SessionSource instance with chat_id="1" and chat_type="dm".
    """
    return SessionSource(platform=platform, chat_id="1", chat_type="dm", user_id=user_id, user_id_alt=user_id_alt)


def test_verify_authz_default_allows_everyone():
    """Validate that with no authorization configured, all messages are allowed (legacy behaviour).

    The test constructs a checker with empty settings and asserts the result is authorized
    at the LAYER_DEFAULT_ALLOW layer, confirming the fallback policy is permissive rather
    than restrictive to preserve backward compatibility.
    """
    c = _checker()
    r = c.is_authorized(_src(), "telegram")
    assert r.authorized, "Default policy must allow all messages."
    assert r.layer == LAYER_DEFAULT_ALLOW, "Default policy must resolve at the DEFAULT_ALLOW layer."


def test_verify_authz_platform_allow_all_from_settings(pairing):
    """Validate that adapter_*_allow_all=true settings enable platform-wide allow-all.

    The test sets adapter_telegram_allow_all="true" and asserts that any user (including
    "anyone") is authorized at the LAYER_PLATFORM_ALLOW_ALL layer, confirming the settings
    parser recognises the platform-allow-all key.
    """
    c = _checker(settings={"adapter_telegram_allow_all": "true"}, pairing=pairing)
    r = c.is_authorized(_src(user_id="anyone"), "telegram")
    assert r.authorized, "Platform allow-all must authorize any user on that platform."
    assert r.layer == LAYER_PLATFORM_ALLOW_ALL, "Authorization must resolve at the PLATFORM_ALLOW_ALL layer."


def test_verify_authz_platform_allow_all_from_env(monkeypatch):
    """Validate that TELEGRAM_ALLOW_ALL_USERS=1 env var enables platform-wide allow-all.

    The test monkeypatches the environment and asserts the checker reads the env var and
    authorizes at LAYER_PLATFORM_ALLOW_ALL, confirming the env-var fallback path works.
    """
    monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "1")
    c = _checker()
    r = c.is_authorized(_src(user_id="anyone"), "telegram")
    assert r.authorized, "TELEGRAM_ALLOW_ALL_USERS=1 must authorize any user."
    assert r.layer == LAYER_PLATFORM_ALLOW_ALL, "Authorization must resolve at the PLATFORM_ALLOW_ALL layer."


def test_verify_authz_allowlist_from_settings(pairing):
    """Validate that adapter_*_allowed_users settings enforce an explicit user allowlist.

    The test configures allowed_users="42,99" and asserts that both listed users are
    authorized while user "77" is denied at LAYER_DENY, confirming the allowlist parser
    splits on commas and the deny layer is reached for unlisted users.
    """
    c = _checker(settings={"adapter_telegram_allowed_users": "42,99"}, pairing=pairing)
    assert c.is_authorized(_src(user_id="42"), "telegram").authorized is True, "Listed user 42 must be allowed."
    assert c.is_authorized(_src(user_id="99"), "telegram").authorized is True, "Listed user 99 must be allowed."
    r = c.is_authorized(_src(user_id="77"), "telegram")
    assert r.authorized is False, "Unlisted user 77 must be denied."
    assert r.layer == LAYER_DENY, "Denial must resolve at the DENY layer."


def test_verify_authz_allowlist_checks_user_id_alt(pairing):
    """Validate that the allowlist check also considers the alternate user ID field.

    The test sets adapter_signal_allowed_users="uuid-1" and sends a message with
    user_id="9" but user_id_alt="uuid-1", asserting the alt-ID match grants authorization
    at the LAYER_ALLOWLIST layer.
    """
    c = _checker(settings={"adapter_signal_allowed_users": "uuid-1"}, pairing=pairing)
    r = c.is_authorized(_src(platform="signal", user_id="9", user_id_alt="uuid-1"), "signal")
    assert r.authorized, "Alternate user ID must be checked against the allowlist."
    assert r.layer == LAYER_ALLOWLIST, "Authorization must resolve at the ALLOWLIST layer."


def test_verify_authz_allowlist_from_env(monkeypatch):
    """Validate that TELEGRAM_ALLOWED_USERS env var enforces an explicit allowlist.

    The test monkeypatches TELEGRAM_ALLOWED_USERS="42" and asserts user "42" is allowed
    while user "77" is denied, confirming the env-var path for allowlists mirrors the
    settings path.
    """
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "42")
    c = _checker()
    assert c.is_authorized(_src(user_id="42"), "telegram").authorized is True, "Env-allowed user must be authorized."
    assert c.is_authorized(_src(user_id="77"), "telegram").authorized is False, "Env-unlisted user must be denied."


def test_verify_authz_pairing_layer(pairing):
    """Validate that a paired user is authorized at the LAYER_PAIRING layer.

    The test redeems a code for user "42" on the injected pairing store, constructs a
    checker with that store, and asserts the user resolves at LAYER_PAIRING, confirming
    the pairing layer is consulted when the allowlist and platform-allow-all layers do not match.
    """
    pairing.redeem(pairing.create_code(), "telegram", "42")
    c = _checker(pairing=pairing)
    r = c.is_authorized(_src(user_id="42"), "telegram")
    assert r.authorized, "Paired user must be authorized."
    assert r.layer == LAYER_PAIRING, "Authorization must resolve at the PAIRING layer."


def test_verify_authz_global_allow_all(pairing):
    """Validate that gateway_allow_all_users=true settings enable global allow-all.

    The test sets the global allow-all flag and asserts any user is authorized at
    LAYER_GLOBAL_ALLOW_ALL, confirming the gateway-level override bypasses all
    platform-scoped restrictions.
    """
    c = _checker(settings={"gateway_allow_all_users": "true"}, pairing=pairing)
    r = c.is_authorized(_src(user_id="anyone"), "telegram")
    assert r.authorized, "Global allow-all must authorize any user on any platform."
    assert r.layer == LAYER_GLOBAL_ALLOW_ALL, "Authorization must resolve at the GLOBAL_ALLOW_ALL layer."


def test_verify_authz_global_allow_all_from_env(monkeypatch):
    """Validate that GATEWAY_ALLOW_ALL_USERS env var enables global allow-all.

    The test monkeypatches the env var to "yes" and asserts the checker authorizes
    any user at LAYER_GLOBAL_ALLOW_ALL, confirming the env-var fallback path.
    """
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "yes")
    c = _checker()
    r = c.is_authorized(_src(user_id="anyone"), "telegram")
    assert r.authorized, "GATEWAY_ALLOW_ALL_USERS=yes must authorize any user."
    assert r.layer == LAYER_GLOBAL_ALLOW_ALL, "Authorization must resolve at the GLOBAL_ALLOW_ALL layer."


def test_verify_authz_precedence_platform_over_allowlist(pairing):
    """Validate that platform allow-all takes precedence over a restrictive allowlist.

    The test configures both adapter_telegram_allow_all=true and adapter_telegram_allowed_users="99",
    then sends a message from user "77" (not in the allowlist) and asserts authorization
    at LAYER_PLATFORM_ALLOW_ALL, confirming the 5-layer precedence order: platform-allow-all
    beats allowlist.
    """
    c = _checker(settings={
        "adapter_telegram_allow_all": "true",
        "adapter_telegram_allowed_users": "99",
    }, pairing=pairing)
    r = c.is_authorized(_src(user_id="77"), "telegram")
    assert r.authorized, "Platform allow-all must override a restrictive allowlist."
    assert r.layer == LAYER_PLATFORM_ALLOW_ALL, "Authorization must resolve at PLATFORM_ALLOW_ALL, not ALLOWLIST."


def test_verify_authz_precedence_allowlist_over_pairing(pairing):
    """Validate that the allowlist layer takes precedence over the pairing layer.

    The test pairs user "42" and sets the allowlist to only "99". It then asserts that
    user "42" resolves at LAYER_PAIRING (authorized but not via allowlist) and user "99"
    resolves at LAYER_ALLOWLIST, confirming the precedence ordering is allowlist > pairing.
    """
    pairing.redeem(pairing.create_code(), "telegram", "42")
    c = _checker(settings={"adapter_telegram_allowed_users": "99"}, pairing=pairing)
    # 42 is paired but NOT in allowlist -> pairing layer applies (authorized).
    r = c.is_authorized(_src(user_id="42"), "telegram")
    assert r.authorized, "Paired user not in allowlist must still be authorized via pairing layer."
    assert r.layer == LAYER_PAIRING, "Paired-but-unlisted user must resolve at PAIRING."
    # 99 is in allowlist -> allowlist wins.
    r2 = c.is_authorized(_src(user_id="99"), "telegram")
    assert r2.authorized, "Listed user must be authorized."
    assert r2.layer == LAYER_ALLOWLIST, "Listed user must resolve at ALLOWLIST."


def test_verify_authz_precedence_pairing_over_global(pairing):
    """Validate that the pairing layer takes precedence over global allow-all.

    The test pairs user "42" and enables gateway_allow_all_users=true, then asserts
    the resolved layer is PAIRING (not GLOBAL_ALLOW_ALL), confirming the precedence
    order pairing > global-allow-all.
    """
    pairing.redeem(pairing.create_code(), "telegram", "42")
    c = _checker(settings={"gateway_allow_all_users": "true"}, pairing=pairing)
    r = c.is_authorized(_src(user_id="42"), "telegram")
    assert r.authorized, "Paired user must be authorized even when global allow-all is on."
    assert r.layer == LAYER_PAIRING, "Paired user must resolve at PAIRING, not GLOBAL_ALLOW_ALL."


def test_verify_authz_no_user_id_denies_without_allow_all(pairing):
    """Validate that a message without a user_id is denied when no allow-all layer is active.

    The test configures an allowlist requiring user_id="42" and sends a message with no
    user_id, asserting denial at LAYER_DENY, confirming that anonymous messages cannot
    bypass authorization simply because the user_id field is absent.
    """
    c = _checker(settings={"adapter_telegram_allowed_users": "42"}, pairing=pairing)
    r = c.is_authorized(SessionSource(platform="telegram", chat_id="1", chat_type="dm"), "telegram")
    assert not r.authorized, "Message without user_id must be denied without an allow-all layer."
    assert r.layer == LAYER_DENY, "Denial must resolve at the DENY layer."


# ---- handle_message integration: /pair + reject ----


class _AuthzAdapter(BasePlatformAdapter):
    """Minimal test adapter that records sent messages for assertion."""

    name = "telegram"

    def __init__(self):
        """Initialise the base adapter and the list that captures outbound messages."""
        super().__init__()
        self.sent: list[tuple[str, str]] = []

    async def connect(self, *, is_reconnect=False) -> bool:
        """Report a successful connection without touching any platform."""
        return True

    async def disconnect(self) -> None:
        """Tear down nothing; there is no real connection to close."""
        pass

    async def send(self, chat_id, content, *, reply_to=None, metadata=None):
        """Record the chat target and body, then report success."""
        self.sent.append((chat_id, content))
        return SendResult(success=True, message_id="m1")


def _event(text, chat_id="1", user_id="42"):
    """Build a MessageEvent for the given text, chat_id and user_id."""
    return MessageEvent(
        text=text,
        source=SessionSource(platform="telegram", chat_id=chat_id, chat_type="dm", user_id=user_id),
    )


@pytest.mark.asyncio
async def test_verify_handle_message_rejects_unauthorized(tmp_path):
    """Validate that handle_message rejects unauthorized users and sends a notice.

    The test configures an allowlist containing only "legit-user" and sends a message
    from "intruder". It asserts the message handler is never invoked (dispatched is empty)
    and that a reject notice was sent containing either "not authorized" or the ❌ emoji.
    """
    a = _AuthzAdapter()
    store = PairingStore(path=tmp_path / "p.json")
    a.set_pairing(store)
    a.set_authz(AuthorizationChecker(pairing=store, config_fn=lambda: {"adapter_telegram_allowed_users": "legit-user"}))
    dispatched = []

    async def handler(adapter, event):
        """Record the event so the test can prove it was or was not called."""
        dispatched.append(event)

    a.set_message_handler(handler)
    await a.handle_message(_event("hi", user_id="intruder"))
    assert dispatched == [], "Unauthorized message must not reach the handler."
    assert a.sent, "A reject notice must be sent to the unauthorized user."
    assert "not authorized" in a.sent[0][1].lower() or "❌" in a.sent[0][1], \
        "Reject notice must mention authorization failure."
    store.close()


@pytest.mark.asyncio
async def test_verify_handle_message_pair_redeem_flow(tmp_path):
    """Validate the full /pair mint-and-redeem lifecycle through handle_message.

    The test exercises four steps:
    1. An intruder tries /pair with an unknown code -> rejected, not paired.
    2. An admin (with global allow-all) mints a code -> notice contains a 6-char code.
    3. A new user redeems the code -> paired and authorized notice sent.
    4. The now-paired user sends a normal message -> dispatched to the handler.
    """
    a = _AuthzAdapter()
    store = PairingStore(path=tmp_path / "p.json")
    a.set_pairing(store)
    a.set_authz(AuthorizationChecker(pairing=store, config_fn=lambda: {}))

    # 1. Intruder tries /pair with an unknown code -> rejected, not paired.
    await a.handle_message(_event("/pair WRONG", user_id="intruder"))
    assert store.is_paired("telegram", "intruder") is False, "Failed redeem must not create a pairing."
    assert any("Invalid" in c or "❌" in c for _, c in a.sent), "Intruder must receive an invalid-code notice."

    a.sent.clear()
    # 2. Mint a code via an admin with global allow-all.
    a2 = _AuthzAdapter()
    a2.set_pairing(store)
    a2.set_authz(AuthorizationChecker(pairing=store, config_fn=lambda: {"gateway_allow_all_users": "true"}))
    await a2.handle_message(_event("/pair", user_id="admin"))
    # The mint notice contains the code.
    mint_msg = [c for _, c in a2.sent if "Pairing code:" in c]
    assert mint_msg, f"Expected mint notice; got {a2.sent}"
    code = mint_msg[0].split("Pairing code:")[1].split("\n")[0].strip()
    assert len(code) == 6, "Minted code must be 6 characters long."

    a2.sent.clear()
    # 3. New user redeems the code -> paired + authorized.
    await a2.handle_message(_event(f"/pair {code}", user_id="newuser"))
    assert store.is_paired("telegram", "newuser") is True, "Redeem must establish the pairing."
    assert any("Paired" in c or "✅" in c for _, c in a2.sent), "New user must receive a pairing-success notice."

    a2.sent.clear()
    # 4. Now the paired newuser can send a normal message -> dispatched.
    dispatched = []

    async def handler(adapter, event):
        """Record the event so the test can count how many were dispatched."""
        dispatched.append(event)

    a2.set_message_handler(handler)
    await a2.handle_message(_event("hello", user_id="newuser"))
    assert len(dispatched) == 1, "Paired user message must reach the handler."
    store.close()


@pytest.mark.asyncio
async def test_verify_handle_message_no_authz_allows_everything():
    """Validate that without an authz checker, handle_message dispatches every message (legacy mode).

    The test registers a handler, sends a message from an arbitrary user, and asserts
    the handler received exactly one event and no reject notice was sent, confirming
    the legacy no-authz path is fully permissive.
    """
    a = _AuthzAdapter()
    dispatched = []

    async def handler(adapter, event):
        """Record the event so the test can prove the legacy path dispatched it."""
        dispatched.append(event)

    a.set_message_handler(handler)
    await a.handle_message(_event("hi", user_id="anyone"))
    assert len(dispatched) == 1, "Message must reach the handler when no authz checker is set."
    assert a.sent == [], "No reject notice must be sent in legacy permissive mode."
