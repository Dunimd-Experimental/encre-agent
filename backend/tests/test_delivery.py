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

"""Tests for gateway outbound delivery (Phase 2b).

Exercises :class:`DeliveryTarget.parse` for both ``platform:chat_id`` and bare
adapter forms, plus every routing path of :class:`DeliveryRouter.deliver`
(explicit target, bare adapter default, and origin fallback). Also covers
output-length truncation behaviour and per-target failure isolation, using
in-memory adapter doubles so no real platform is contacted.
"""

import asyncio

import pytest

from encre.gateway.platforms.base import BasePlatformAdapter, SendResult
from encre.gateway.delivery import (
    MAX_PLATFORM_OUTPUT,
    DeliveryRouter,
    DeliveryTarget,
)


class _Adapter(BasePlatformAdapter):
    """In-memory adapter that records every send for delivery assertions."""

    def __init__(self, *, push_chat_id=None, splits=False, max_len=0):
        """Configure the double without calling the base initializer, which would require credentials."""
        # Skip BasePlatformAdapter.__init__ so no platform credentials are needed.
        self.max_message_length = max_len
        self.splits_long_messages = splits
        self.default_push_chat_id = push_chat_id
        self.sent: list[tuple[str, str]] = []

    async def connect(self, *, is_reconnect=False) -> bool:
        """Always report a successful connection since no real transport is used."""
        return True

    async def disconnect(self) -> None:
        """No transport is opened, so disconnection is a no-op."""

    async def send(self, chat_id, content, *, reply_to=None, metadata=None):
        """Record the (chat_id, content) pair and report success with a fixed message id."""
        self.sent.append((chat_id, content))
        return SendResult(success=True, message_id="m1")


class _FailingAdapter(_Adapter):
    """Adapter that always raises on send, to prove one bad target does not abort the batch."""

    async def send(self, chat_id, content, *, reply_to=None, metadata=None):
        """Always raise RuntimeError to simulate an unreachable or broken platform."""
        raise RuntimeError("boom")


class _Manager:
    """Minimal adapter-manager stub exposing _instances for DeliveryRouter construction."""

    def __init__(self, instances):
        """Store the adapter mapping that DeliveryRouter looks up by platform name."""
        self._instances = instances


# ---- DeliveryTarget.parse ----


def test_parse_explicit_target():
    """DeliveryTarget.parse must split 'telegram:123456' into platform and chat_id.

    Splits on the first colon so the adapter identifier stays separate from the
    destination address, giving platform == 'telegram' and chat_id == '123456'.
    """
    t = DeliveryTarget.parse("telegram:123456")
    assert t.platform == "telegram"
    assert t.chat_id == "123456"


def test_parse_bare_adapter():
    """A bare adapter name without a colon must leave chat_id as None.

    A bare name means no explicit destination was given, so the router should
    fall back to the adapter's configured default push target.
    """
    t = DeliveryTarget.parse("telegram")
    assert t.platform == "telegram"
    assert t.chat_id is None


def test_parse_strips_whitespace_around_separator():
    """Parsing must tolerate user-typed whitespace around the colon separator."""
    t = DeliveryTarget.parse("  telegram : 123  ")
    assert t.platform == "telegram"
    assert t.chat_id == "123"


def test_parse_empty_chat_falls_back_to_none():
    """An empty chat_id suffix ('telegram:') behaves exactly like a bare adapter name."""
    t = DeliveryTarget.parse("telegram:")
    assert t.platform == "telegram"
    assert t.chat_id is None


# ---- deliver ----


@pytest.mark.asyncio
async def test_deliver_explicit_target_sends_to_correct_chat():
    """An explicit target must route straight to the chat id it names, unmodified."""
    a = _Adapter()
    router = DeliveryRouter(_Manager({"telegram": a}), audit_dir="/tmp/encre_test_audit")
    results = await router.deliver("hello", ["telegram:123"])
    assert len(results) == 1
    assert results[0].success
    assert a.sent == [("123", "hello")]


@pytest.mark.asyncio
async def test_deliver_bare_adapter_uses_default_push_chat_id():
    """A bare adapter target resolves to that adapter's default_push_chat_id."""
    a = _Adapter(push_chat_id="auto-chat")
    router = DeliveryRouter(_Manager({"telegram": a}))
    results = await router.deliver("hello", ["telegram"])
    assert results[0].success
    assert a.sent == [("auto-chat", "hello")]


@pytest.mark.asyncio
async def test_deliver_origin_fallback_routes_to_origin_chat():
    """Origin is used only when targets is None; explicit targets override it."""
    a = _Adapter()
    router = DeliveryRouter(_Manager({"telegram": a}))
    # No targets given, so delivery falls back to the origin chat '999'.
    results = await router.deliver("hello", None, origin=("telegram", "999"))
    assert len(results) == 1
    assert results[0].success is True
    assert a.sent == [("999", "hello")]
    # Explicit targets take precedence over origin: telegram is known, discord is not.
    results = await router.deliver("hello", ["telegram:1", "discord:2"])
    assert len(results) == 2
    assert results[0].success is True   # telegram ok
    assert results[1].success is False  # discord failed
    assert a.sent == [("999", "hello"), ("1", "hello")]


@pytest.mark.asyncio
async def test_deliver_no_targets_no_origin_returns_empty_list():
    """With neither targets nor origin there is no destination, so nothing is sent."""
    a = _Adapter()
    router = DeliveryRouter(_Manager({"telegram": a}))
    results = await router.deliver("hello", None, origin=None)
    assert results == []
    assert a.sent == []


@pytest.mark.asyncio
async def test_deliver_missing_adapter_reports_error_not_crash():
    """An unknown adapter yields a recoverable failure result rather than an exception."""
    router = DeliveryRouter(_Manager({}))
    results = await router.deliver("hello", ["telegram:123"])
    assert results[0].success is False
    assert "not running" in results[0].error


@pytest.mark.asyncio
async def test_deliver_bare_adapter_no_push_target_reports_error():
    """A bare target with no default push destination fails with a chat_id error."""
    a = _Adapter(push_chat_id=None)
    router = DeliveryRouter(_Manager({"telegram": a}))
    results = await router.deliver("hello", ["telegram"])
    assert results[0].success is False
    assert "chat_id" in results[0].error


# ---- truncation ----


@pytest.mark.asyncio
async def test_deliver_under_cap_passes_through_unchanged():
    """Content within the platform cap must be sent verbatim with no audit save."""
    a = _Adapter()
    router = DeliveryRouter(_Manager({"telegram": a}), max_output=100)
    content = "x" * 50
    results = await router.deliver(content, ["telegram:1"])
    assert results[0].truncated is False
    assert results[0].saved_path is None
    assert a.sent[0][1] == content


@pytest.mark.asyncio
async def test_deliver_over_cap_truncates_saves_audit_and_appends_note(tmp_path):
    """Over-cap content is truncated for a non-chunking adapter, audited, and annotated."""
    a = _Adapter()  # splits_long_messages = False
    router = DeliveryRouter(_Manager({"telegram": a}), max_output=100, audit_dir=tmp_path)
    content = "y" * 500
    results = await router.deliver(content, ["telegram:1"])
    assert results[0].truncated is True
    assert results[0].saved_path is not None
    sent_content = a.sent[0][1]
    assert "[truncated" in sent_content
    # The payload was shortened; the appended note embeds the absolute audit
    # path, so the exact length depends on that path. Only assert it is
    # meaningfully shorter than the original rather than checking a precise size.
    assert len(sent_content) < len(content)
    # The complete output was persisted for audit (encrypted at rest and read
    # back through the transparent secure_io helper).
    from encre.secure_io import read_text
    saved = read_text(results[0].saved_path)
    assert saved == content


@pytest.mark.asyncio
async def test_deliver_chunking_adapter_gets_full_payload(tmp_path):
    """A chunking adapter gets the full payload even beyond max_output, with no truncation."""
    a = _Adapter(splits=True)
    router = DeliveryRouter(_Manager({"telegram": a}), max_output=100, audit_dir=tmp_path)
    content = "z" * 500
    results = await router.deliver(content, ["telegram:1"])
    assert results[0].truncated is False
    assert results[0].saved_path is None
    assert a.sent[0][1] == content  # full payload, no truncation


# ---- failure isolation ----


@pytest.mark.asyncio
async def test_deliver_one_bad_target_does_not_abort_others():
    """A failing target must not prevent delivery to the remaining healthy targets."""
    good = _Adapter()
    bad = _FailingAdapter()
    router = DeliveryRouter(_Manager({"telegram": good, "discord": bad}))
    results = await router.deliver("hello", ["telegram:1", "discord:2"])
    assert len(results) == 2
    assert results[0].success is True   # telegram ok
    assert results[1].success is False   # discord failed
    assert good.sent == [("1", "hello")]
