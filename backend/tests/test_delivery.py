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

"""Tests for gateway outbound delivery (Phase 2b).

Covers:
- :class:`DeliveryTarget.parse` for explicit and bare-adapter forms.
- :class:`DeliveryRouter.deliver` explicit-target / adapter-id / origin paths.
- Truncation: under-cap passes through; over-cap saves audit + appends note
  for non-chunking adapters; chunking adapters receive the full payload.
- Per-target failure isolation (one bad target does not abort the rest).
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
    """In-memory adapter recording every send for delivery test assertions."""

    def __init__(self, *, push_chat_id=None, splits=False, max_len=0):
        # Bypass BasePlatformAdapter.__init__ for testing.
        self.max_message_length = max_len
        self.splits_long_messages = splits
        self.default_push_chat_id = push_chat_id
        self.sent: list[tuple[str, str]] = []

    async def connect(self, *, is_reconnect=False) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def send(self, chat_id, content, *, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))
        return SendResult(success=True, message_id="m1")


class _FailingAdapter(_Adapter):
    """Adapter that always raises RuntimeError on send to test failure isolation."""

    async def send(self, chat_id, content, *, reply_to=None, metadata=None):
        raise RuntimeError("boom")


class _Manager:
    """Minimal adapter manager stub holding _instances for DeliveryRouter construction."""

    def __init__(self, instances):
        self._instances = instances


# ---- DeliveryTarget.parse -------------------------------------------------


def test_parse_explicit_target():
    """Validate that DeliveryTarget.parse('telegram:123456') splits platform and chat_id correctly.

    The test parses an explicit target string and asserts platform == 'telegram'
    and chat_id == '123456' because the parser must split on the first colon
    to separate the adapter identifier from the destination address.
    """
    t = DeliveryTarget.parse("telegram:123456")
    assert t.platform == "telegram"
    assert t.chat_id == "123456"


def test_parse_bare_adapter():
    """Validate that DeliveryTarget.parse('telegram') sets chat_id to None for bare adapter names.

    The test parses a bare adapter name without a colon and asserts platform
    is 'telegram' and chat_id is None because bare names indicate the adapter's
    default push target should be used instead of an explicit chat ID.
    """
    t = DeliveryTarget.parse("telegram")
    assert t.platform == "telegram"
    assert t.chat_id is None


def test_parse_strips_whitespace_around_separator():
    """Validate that DeliveryTarget.parse trims whitespace around the colon separator.

    The test passes '  telegram : 123  ' and asserts platform == 'telegram'
    and chat_id == '123' because the parser must be tolerant of user-typed
    whitespace in target strings.
    """
    t = DeliveryTarget.parse("  telegram : 123  ")
    assert t.platform == "telegram"
    assert t.chat_id == "123"


def test_parse_empty_chat_falls_back_to_none():
    """Validate that DeliveryTarget.parse('telegram:') sets chat_id to None when the suffix is empty.

    The test asserts platform == 'telegram' and chat_id is None because an
    empty chat_id suffix must be treated the same as a bare adapter name,
    falling back to the adapter's default push target.
    """
    t = DeliveryTarget.parse("telegram:")
    assert t.platform == "telegram"
    assert t.chat_id is None


# ---- deliver ------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_explicit_target_sends_to_correct_chat():
    """Validate that deliver('hello', ['telegram:123']) routes to chat_id '123' via the telegram adapter.

    The test constructs a router with an in-memory adapter, delivers a short
    message to an explicit target, and asserts the result is successful and
    the adapter received exactly [('123', 'hello')] because explicit targets
    must route to the specified chat ID without modification.
    """
    a = _Adapter()
    router = DeliveryRouter(_Manager({"telegram": a}), audit_dir="/tmp/encre_test_audit")
    results = await router.deliver("hello", ["telegram:123"])
    assert len(results) == 1
    assert results[0].success
    assert a.sent == [("123", "hello")]


@pytest.mark.asyncio
async def test_deliver_bare_adapter_uses_default_push_chat_id():
    """Validate that deliver with a bare adapter name uses the adapter's default_push_chat_id.

    The test constructs an adapter with push_chat_id='auto-chat', delivers to
    'telegram' (bare), and asserts the sent tuple is [('auto-chat', 'hello')]
    because bare targets must resolve to the adapter's configured default.
    """
    a = _Adapter(push_chat_id="auto-chat")
    router = DeliveryRouter(_Manager({"telegram": a}))
    results = await router.deliver("hello", ["telegram"])
    assert results[0].success
    assert a.sent == [("auto-chat", "hello")]


@pytest.mark.asyncio
async def test_deliver_origin_fallback_routes_to_origin_chat():
    """Validate that deliver uses origin=(platform, chat_id) when no explicit targets are given.

    The test delivers with origin=('telegram', '999') and targets=['telegram:1', 'discord:2'],
    and asserts telegram succeeded with chat_id '1' and discord failed because
    origin is only used when targets is None, not when explicit targets are provided.
    """
    a = _Adapter()
    router = DeliveryRouter(_Manager({"telegram": a}))
    # First deliver with targets=None and origin set: routes to origin chat '999'.
    results = await router.deliver("hello", None, origin=("telegram", "999"))
    assert len(results) == 1
    assert results[0].success is True
    assert a.sent == [("999", "hello")]
    # Explicit targets override origin: telegram ok, discord missing.
    results = await router.deliver("hello", ["telegram:1", "discord:2"])
    assert len(results) == 2
    assert results[0].success is True   # telegram ok
    assert results[1].success is False  # discord failed
    assert a.sent == [("999", "hello"), ("1", "hello")]


@pytest.mark.asyncio
async def test_deliver_no_targets_no_origin_returns_empty_list():
    """Validate that deliver with None targets and None origin returns an empty result list.

    The test asserts results == [] and the adapter received no sends because
    without targets or origin there is no destination to route to.
    """
    a = _Adapter()
    router = DeliveryRouter(_Manager({"telegram": a}))
    results = await router.deliver("hello", None, origin=None)
    assert results == []
    assert a.sent == []


@pytest.mark.asyncio
async def test_deliver_missing_adapter_reports_error_not_crash():
    """Validate that deliver with an unknown adapter reports a failure result instead of raising.

    The test constructs a router with an empty adapter manager, delivers to
    'telegram:123', and asserts results[0].success is False and the error
    message contains 'not running' because missing adapters must produce
    a recoverable error result, not an unhandled exception.
    """
    router = DeliveryRouter(_Manager({}))
    results = await router.deliver("hello", ["telegram:123"])
    assert results[0].success is False
    assert "not running" in results[0].error


@pytest.mark.asyncio
async def test_deliver_bare_adapter_no_push_target_reports_error():
    """Validate that deliver with a bare adapter name but no default_push_chat_id reports a chat_id error.

    The test constructs an adapter with push_chat_id=None, delivers to 'telegram',
    and asserts success is False and the error contains 'chat_id' because a
    bare target requires the adapter to have a default destination configured.
    """
    a = _Adapter(push_chat_id=None)
    router = DeliveryRouter(_Manager({"telegram": a}))
    results = await router.deliver("hello", ["telegram"])
    assert results[0].success is False
    assert "chat_id" in results[0].error


# ---- truncation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_under_cap_passes_through_unchanged():
    """Validate that deliver with content under max_output sends the original content without truncation.

    The test sets max_output=100 and delivers 50 'x' characters, asserting
    truncated is False, saved_path is None, and the sent content equals the
    original because content within the platform cap must pass through
    without modification or audit logging.
    """
    a = _Adapter()
    router = DeliveryRouter(_Manager({"telegram": a}), max_output=100)
    content = "x" * 50
    results = await router.deliver(content, ["telegram:1"])
    assert results[0].truncated is False
    assert results[0].saved_path is None
    assert a.sent[0][1] == content


@pytest.mark.asyncio
async def test_deliver_over_cap_truncates_saves_audit_and_appends_note(tmp_path):
    """Validate that deliver truncates over-cap content, saves full audit, and appends a truncation note.

    The test sets max_output=100 and delivers 500 'y' characters to a
    non-chunking adapter (splits=False). It asserts truncated is True,
    saved_path is not None, the sent content contains '[truncated', the
    sent content is shorter than the original, and the full 500-char
    content was saved to the audit file because over-cap delivery must
    preserve the full output for audit while sending a shortened version.
    """
    a = _Adapter()  # splits_long_messages = False
    router = DeliveryRouter(_Manager({"telegram": a}), max_output=100, audit_dir=tmp_path)
    content = "y" * 500
    results = await router.deliver(content, ["telegram:1"])
    assert results[0].truncated is True
    assert results[0].saved_path is not None
    sent_content = a.sent[0][1]
    assert "[truncated" in sent_content
    # The payload was shortened (full 500 chars wouldn't fit); the note carries
    # the absolute audit path, so the exact length depends on the path -- we
    # only assert it's meaningfully shorter than the original.
    assert len(sent_content) < len(content)
    # Full output was saved to disk (encrypted at rest; read through the
    # transparent secure_io helper).
    from encre.secure_io import read_text
    saved = read_text(results[0].saved_path)
    assert saved == content


@pytest.mark.asyncio
async def test_deliver_chunking_adapter_gets_full_payload(tmp_path):
    """Validate that a chunking adapter (splits=True) receives the full content even when over max_output.

    The test sets max_output=100 and delivers 500 'z' characters to an adapter
    advertising splits_long_messages=True. It asserts truncated is False,
    saved_path is None, and the sent content equals the full 500-char
    original because chunking adapters are capable of handling large
    payloads natively and should not be truncated by the router.
    """
    a = _Adapter(splits=True)
    router = DeliveryRouter(_Manager({"telegram": a}), max_output=100, audit_dir=tmp_path)
    content = "z" * 500
    results = await router.deliver(content, ["telegram:1"])
    assert results[0].truncated is False
    assert results[0].saved_path is None
    assert a.sent[0][1] == content  # full payload, no truncation


# ---- failure isolation --------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_one_bad_target_does_not_abort_others():
    """Validate that a failing target does not prevent delivery to other healthy targets.

    The test constructs a router with one healthy adapter (telegram) and one
    failing adapter (discord). It delivers to both targets and asserts
    telegram succeeded while discord failed, and that the good adapter
    received its message because per-target failure isolation must ensure
    one broken destination does not block the entire delivery batch.
    """
    good = _Adapter()
    bad = _FailingAdapter()
    router = DeliveryRouter(_Manager({"telegram": good, "discord": bad}))
    results = await router.deliver("hello", ["telegram:1", "discord:2"])
    assert len(results) == 2
    assert results[0].success is True   # telegram ok
    assert results[1].success is False   # discord failed
    assert good.sent == [("1", "hello")]
