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

"""Tests for the gateway session routing layer.

These tests verify that session keys are derived consistently, that
:class:`SessionSource` survives serialization without leaking gateway-local
flags, and that :class:`SessionStore` persists bindings across reopens.

They also cover :class:`SendResult`'s retry metadata plus
:func:`classify_send_error`, the :class:`MessageEvent.source` field with the
:meth:`BaseAdapter.get_chat_info` default, and that :class:`GatewayMessage`
submit and submit_stream frames propagate ``source``.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from encre.gateway.platforms.base import (
    SEND_ERROR_KINDS,
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    classify_send_error,
)
from encre.gateway.ws_bridge.protocol import GatewayMessage, GatewayOp
from encre.gateway.session import (
    SessionSource,
    SessionStore,
    build_session_key,
)


# ---- build_session_key conformance ----


@pytest.mark.parametrize(
    "source, expected",
    [
        # DM with chat_id -- the canonical Telegram private chat case.
        (
            SessionSource(platform="telegram", chat_id="123456789", chat_type="dm", user_id="42"),
            "agent:main:telegram:dm:123456789",
        ),
        # DM with chat_id + thread_id -- threaded DMs are isolated per thread.
        (
            SessionSource(platform="telegram", chat_id="123", chat_type="dm", thread_id="55"),
            "agent:main:telegram:dm:123:55",
        ),
        # DM without chat_id falls back to the sender id (per-user isolation).
        (
            SessionSource(platform="telegram", chat_id="", chat_type="dm", user_id="42"),
            "agent:main:telegram:dm:42",
        ),
        # user_id_alt takes precedence over user_id in the DM fallback.
        (
            SessionSource(platform="signal", chat_id="", chat_type="dm", user_id="9", user_id_alt="uuid-1"),
            "agent:main:signal:dm:uuid-1",
        ),
        # DM with no identifiers -> bare per-platform DM sink.
        (
            SessionSource(platform="telegram", chat_id="", chat_type="dm"),
            "agent:main:telegram:dm",
        ),
        # Group: chat_id + user_id, isolated per user by default.
        (
            SessionSource(platform="discord", chat_id="100", chat_type="group", user_id="7"),
            "agent:main:discord:group:100:7",
        ),
        # Forum/thread: shared across participants, no user_id appended.
        (
            SessionSource(platform="telegram", chat_id="9", chat_type="forum", thread_id="55", user_id="7"),
            "agent:main:telegram:forum:9:55",
        ),
        # Thread with per-user isolation enabled -> user_id appended.
        (
            build_session_key(
                SessionSource(platform="slack", chat_id="C1", chat_type="thread", thread_id="T1", user_id="U1"),
                thread_sessions_per_user=True,
            ),
            "agent:main:slack:thread:C1:T1:U1",
        ),
        # Group without user_id -> one shared session per chat.
        (
            SessionSource(platform="discord", chat_id="100", chat_type="group"),
            "agent:main:discord:group:100",
        ),
    ],
)
def test_verify_build_session_key_conformance(source, expected):
    """Validate that build_session_key produces the expected canonical key.

    Asserts exact equality against a precomputed key for many
    platform/chat_type/thread combinations, because the session key indexes
    SessionStore and any drift causes cross-session leakage or lost messages.
    """
    # The thread case above passes a pre-built key as `source`; handle both.
    if isinstance(source, str):
        assert source == expected
    else:
        assert build_session_key(source) == expected


def test_verify_build_session_key_profile_namespace():
    """Validate that a non-default profile namespaces the session key.

    Asserts the key contains "acme" when profile="acme" is supplied, so
    multi-tenant deployments stay isolated even when platform and chat ids
    collide.
    """
    s = SessionSource(platform="telegram", chat_id="1", chat_type="dm")
    assert build_session_key(s, profile="acme") == "agent:acme:telegram:dm:1"


def test_verify_build_session_key_deterministic():
    """Validate that identical sources always produce the same key.

    Calls build_session_key twice on the same source and asserts equality,
    since determinism is needed for cache hits and for resolving the same
    logical session across runtime invocations.
    """
    s = SessionSource(platform="telegram", chat_id="1", chat_type="dm", user_id="2")
    assert build_session_key(s) == build_session_key(s)


# ---- SessionSource wire round-trip ----


def test_verify_session_source_round_trip():
    """Validate that SessionSource round-trips through to_dict/from_dict.

    Serializes a fully populated source, asserts the discriminator fields are
    present, asserts optional fields appear when set, and asserts is_bot is
    absent from the wire payload because it is a gateway-local flag that must
    not leak into persisted or cross-process data.
    """
    s = SessionSource(
        platform="discord",
        chat_id="100",
        chat_type="group",
        chat_name="general",
        user_id="7",
        user_name="alice",
        thread_id="55",
        chat_topic="dev",
        scope_id="guild-1",
        user_id_alt="alt-7",
        chat_id_alt="alt-100",
    )
    d = s.to_dict()
    # Always-sent discriminators must be present.
    for k in ("platform", "chat_id", "chat_type", "chat_name", "user_id", "user_name", "thread_id", "chat_topic"):
        assert k in d
    # Optional fields are included only when set.
    assert d["scope_id"] == "guild-1"
    assert d["user_id_alt"] == "alt-7"
    # is_bot must never appear on the wire.
    assert "is_bot" not in d

    s2 = SessionSource.from_dict(d)
    assert s2.platform == "discord"
    assert s2.chat_id == "100"
    assert s2.chat_type == "group"
    assert s2.user_id == "7"
    assert s2.thread_id == "55"
    assert s2.scope_id == "guild-1"
    assert s2.is_bot is False  # gateway-side only, default


def test_verify_session_source_from_dict_ignores_unknown_keys():
    """Validate that from_dict tolerates unknown fields gracefully.

    Passes a dict containing a synthetic future_field and asserts the source
    is built without raising, since forward compatibility requires skipping
    unrecognized keys.
    """
    s = SessionSource.from_dict({"platform": "telegram", "chat_id": "1", "future_field": "x"})
    assert s.platform == "telegram"
    assert s.chat_id == "1"


def test_verify_session_source_optional_fields_omitted_when_unset():
    """Validate that unset optional fields are omitted from the dict.

    Builds a minimal source and asserts scope_id, user_id_alt, and chat_id_alt
    are absent, so unset optionals stay out of the payload and None is not
    confused with a missing key.
    """
    s = SessionSource(platform="telegram", chat_id="1", chat_type="dm")
    d = s.to_dict()
    assert "scope_id" not in d
    assert "user_id_alt" not in d
    assert "chat_id_alt" not in d


# ---- SessionStore persistence ----


@pytest.fixture
def store(tmp_path):
    """Provide a SessionStore backed by a per-test SQLite database."""
    db = tmp_path / "routing.db"
    s = SessionStore(db_path=db)
    yield s
    s.close()


def test_verify_session_store_get_miss(store):
    """Validate that get returns None for an unregistered source.

    Asserts None rather than a bogus key or an exception, because callers
    branch on None to decide whether to create a new session.
    """
    s = SessionSource(platform="telegram", chat_id="1", chat_type="dm")
    assert store.get(s) is None


def test_verify_session_store_put_and_get(store):
    """Validate that put followed by get returns the stored session ID.

    Writes a mapping and reads it back, asserting equality, since lossless
    K-V persistence is the store's core contract.
    """
    s = SessionSource(platform="telegram", chat_id="1", chat_type="dm", user_id="42")
    store.put(s, "sess-abc")
    assert store.get(s) == "sess-abc"


def test_verify_session_store_get_or_create_creates_on_miss(store):
    """Validate that get_or_create invokes the factory exactly once on a miss.

    Asserts the returned ID comes from make and that make ran only once, then
    repeats the lookup and asserts no further call, so cached hits bypass the
    factory.
    """
    s = SessionSource(platform="discord", chat_id="100", chat_type="group", user_id="7")
    calls = []

    def make():
        """Count factory invocations and mint a fresh session id."""
        calls.append(1)
        return "new-sess"

    sid = store.get_or_create(s, make)
    assert sid == "new-sess"
    assert len(calls) == 1
    # The second call reuses the cached value; the factory is not called again.
    sid2 = store.get_or_create(s, make)
    assert sid2 == "new-sess"
    assert len(calls) == 1


def test_verify_session_store_reset_clears_entry(store):
    """Validate that reset removes a previously stored mapping.

    Writes a session, confirms it is retrievable, resets, and asserts a
    subsequent get returns None, since reset is the only way to expire a
    binding without closing the store.
    """
    s = SessionSource(platform="telegram", chat_id="1", chat_type="dm")
    store.put(s, "sess-1")
    assert store.get(s) == "sess-1"
    store.reset(s)
    assert store.get(s) is None


def test_verify_session_store_persists_across_reopen(tmp_path):
    """Validate that mappings survive store close and reopen.

    Writes a mapping, closes the store, opens a second instance on the same
    database, and asserts the value is still there, because persistence across
    restarts is required for gateway resilience.
    """
    db = tmp_path / "routing.db"
    s = SessionSource(platform="telegram", chat_id="9", chat_type="dm")
    store1 = SessionStore(db_path=db)
    store1.put(s, "persisted-sess")
    store1.close()
    store2 = SessionStore(db_path=db)
    assert store2.get(s) == "persisted-sess"
    store2.close()


def test_verify_session_store_replaces_on_put(store):
    """Validate that a second put for the same source overwrites the value.

    Asserts the final get returns "new" after two sequential puts, because the
    store is an upsert, not an append-only log.
    """
    s = SessionSource(platform="telegram", chat_id="1", chat_type="dm")
    store.put(s, "old")
    store.put(s, "new")
    assert store.get(s) == "new"


# ---- SendResult + error classification ----


def test_verify_send_result_new_fields_default_none():
    """Validate that SendResult's optional fields default to None.

    Builds a minimal successful result and asserts retry_after and error_kind
    are None, so a clean success never carries stale error metadata.
    """
    r = SendResult(success=True)
    assert r.retry_after is None
    assert r.error_kind is None


def test_verify_send_result_accepts_new_fields():
    """Validate that SendResult stores retry_after and error_kind when given.

    Builds a failed result with rate-limit metadata and asserts both fields
    survive, since adapters need them to schedule retries with the right
    back-off window.
    """
    r = SendResult(success=False, error="flood", retryable=True, retry_after=30.0, error_kind="rate_limited")
    assert r.retry_after == 30.0
    assert r.error_kind == "rate_limited"


def test_verify_send_error_kinds_complete():
    """Validate that SEND_ERROR_KINDS lists the documented error taxonomy.

    Asserts the frozenset matches the seven canonical kinds, because
    classify_send_error branches on these strings and drift would silently push
    unmapped errors onto "unknown".
    """
    assert SEND_ERROR_KINDS == frozenset(
        {"too_long", "bad_format", "forbidden", "not_found", "rate_limited", "transient", "unknown"}
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("message is too long", "too_long"),
        ("message_too_long error", "too_long"),
        ("can't parse entities: unmatched tag", "bad_format"),
        ("Forbidden: bot was blocked by the user", "forbidden"),
        ("chat not found", "not_found"),
        ("message to edit not found", "not_found"),
        ("Too Many Requests: retry after 30", "rate_limited"),
        ("flood control exceeded", "rate_limited"),
        ("Connection timed out", "transient"),
        ("network unreachable", "transient"),
        ("some weird unmapped error", "unknown"),
    ],
)
def test_verify_classify_send_error_maps_text_to_kind(text, expected):
    """Validate that classify_send_error maps error text to the right kind.

    Checks exact kind equality across a spectrum of real-world error messages,
    because this function is the single source of truth for retry-versus-fail
    decisions on the send path.
    """
    assert classify_send_error(error_text=text) == expected


def test_verify_classify_send_error_from_exception():
    """Validate that classify_send_error accepts an Exception instance.

    Passes an exception whose message contains "Forbidden" and asserts the
    result is "forbidden", since adapters often surface wrapped exceptions
    rather than plain strings.
    """
    assert classify_send_error(Exception("Forbidden: blocked")) == "forbidden"


def test_verify_classify_send_error_empty_defaults_to_unknown():
    """Validate that classify_send_error returns "unknown" for empty input.

    Asserts the fallback kind, so a caller handed an empty error still gets a
    deterministic classification instead of a crash or None.
    """
    assert classify_send_error() == "unknown"


# ---- MessageEvent.source + get_chat_info ----


def test_verify_message_event_source_defaults_none():
    """Validate that MessageEvent.source is None when not supplied.

    Builds a minimal event and asserts source is None, because source is an
    optional enrichment field that downstream code must handle when absent.
    """
    e = MessageEvent(text="hi")
    assert e.source is None


def test_verify_message_event_accepts_source():
    """Validate that MessageEvent stores the provided SessionSource.

    Builds an event with an explicit source and asserts the object survives
    with its platform readable, since the gateway routes on source.platform.
    """
    src = SessionSource(platform="telegram", chat_id="1", chat_type="dm")
    e = MessageEvent(text="hi", message_type=MessageType.TEXT, source=src)
    assert e.source is not None
    assert e.source.platform == "telegram"


class _StubAdapter(BasePlatformAdapter):
    """Minimal adapter used to exercise base-class default behavior."""

    name = "stub"

    async def connect(self, *, is_reconnect=False) -> bool:
        """Report a successful connection."""
        return True

    async def disconnect(self) -> None:
        """No-op disconnect for the stub adapter."""
        pass

    async def send(self, chat_id, content, *, reply_to=None, metadata=None):
        """Report a successful send without contacting any platform."""
        return SendResult(success=True)


def test_verify_get_chat_info_default_impl():
    """Validate that the base get_chat_info returns sensible defaults.

    Calls get_chat_info on a stub adapter and asserts the dict contains "name"
    and "type" with reasonable values, so platforms that do not override chat
    info resolution still get a usable fallback.
    """
    from encre.gateway.config import Platform, PlatformConfig
    a = _StubAdapter(config=PlatformConfig(enabled=True), platform=Platform.TELEGRAM)
    info = asyncio.run(a.get_chat_info("123"))
    assert info == {"name": "123", "type": "dm"}


# ---- GatewayMessage submit/submit_stream carry source ----


def test_verify_submit_stream_frame_carries_source():
    """Validate that submit_stream attaches the source dict to the frame.

    Asserts the frame op is SUBMIT_STREAM, the prompt is preserved, and the
    source dict is attached, because the server resolves or creates the target
    session from source before running the agent.
    """
    src = SessionSource(platform="telegram", chat_id="1", chat_type="dm").to_dict()
    msg = GatewayMessage.submit_stream("hello", source=src)
    assert msg.op == GatewayOp.SUBMIT_STREAM
    assert msg.data["prompt"] == "hello"
    assert msg.data["source"] == src


def test_verify_submit_stream_frame_omits_source_when_none():
    """Validate that submit_stream omits source when none is supplied.

    Asserts "source" is absent from data, so legacy clients without a
    SessionSource can still submit streams without a missing-key validation
    error server-side.
    """
    msg = GatewayMessage.submit_stream("hello")
    assert "source" not in msg.data


def test_verify_submit_frame_carries_source():
    """Validate that submit attaches the source dict to the frame.

    Asserts the frame op is SUBMIT and the source platform survives, so the
    non-streaming path carries the same routing metadata as submit_stream for
    consistent session lookup.
    """
    src = SessionSource(platform="discord", chat_id="100", chat_type="group").to_dict()
    msg = GatewayMessage.submit("hi", source=src)
    assert msg.op == GatewayOp.SUBMIT
    assert msg.data["source"]["platform"] == "discord"
