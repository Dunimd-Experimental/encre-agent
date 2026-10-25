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

"""
LINE Messaging API platform adapter for Encre.

A bundled platform plugin that runs an aiohttp webhook server, accepts LINE
webhook events (signature-verified), and relays messages to/from the agent
via the standard ``BasePlatformAdapter`` interface.

Design highlights
-----------------

**Reply token preferred, Push fallback.** LINE's reply token is single-use
and expires roughly 60 seconds after the inbound event. We try Reply first
(it's free) and fall back to the metered Push API when the token is absent,
expired, or rejected by the API.

**Slow-LLM postback button (optional).** When the LLM is still running past
``slow_response_threshold`` seconds (default 45, leaving 15s margin on the
60s reply-token TTL), we burn the original reply token to send a Template
Buttons bubble -- the user taps it later to receive the cached answer via a
*fresh* reply token (also free). State machine: PENDING -> READY -> DELIVERED,
with ERROR for cancelled runs. Set the threshold to 0 to disable the
button and always Push-fallback instead.

**Three-allowlist gating.** Separate allowlists for users (U-prefixed),
groups (C-prefixed), and rooms (R-prefixed). ``LINE_ALLOW_ALL_USERS=true``
is a dev-only escape hatch.

**Media via public HTTPS.** LINE's Messaging API does *not* accept
binary uploads -- images, audio, and video must be reachable HTTPS URLs.
We register registered tempfiles under ``/line/media/<token>/<filename>``
served by the same aiohttp app, with an allowed-roots traversal guard.
``LINE_PUBLIC_URL`` (e.g. ``https://my-tunnel.example.com``) overrides
the host:port construction so URLs are reachable when bind is 0.0.0.0
or behind a reverse proxy.

**5-message batching.** LINE accepts at most 5 message objects per
Reply/Push call; longer responses are smart-chunked at 4500 chars
(LINE per-bubble limit is 5000) and batched.

Synthesis credits
-----------------

This file is a synthesis of seven open community PRs adding LINE support
to Encre. It deliberately ports the *strongest* idea from each into
a single plugin-form module that requires zero core edits:

* PR #18153 (leepoweii)   -- Template Buttons postback cache state machine,
  Markdown URL preservation, system-message bypass.
* PR #8398  (yuga-hashimoto) -- media URL serving with traversal guard,
  send_voice / send_video, ``LINE_PUBLIC_URL`` env, macOS ``/tmp`` root.
* PR #16832 (jethac)      -- config wiring style, voice/image tests.
* PR #21023 (perng)       -- plugin-form skeleton (the only one already
  modeled on ``ADDING_A_PLATFORM.md``), reply -> push fallback at 50s TTL,
  loading-animation indicator, source dispatcher.
* PR #14942 (soichiyo)    -- Cloudflare-tunnel operating model (docs only).
* PR #14988 (David-0x221Eight) -- text-first scope discipline.
* PR #6676  (liyoungc)    -- Push-only mode (used as the ``threshold=0``
  fallback path here).
"""

import asyncio
import base64
import enum
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote as _urlquote

from aiohttp import web

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy / function-level imports for gateway internals are NOT used here --
# the plugin discovery flow imports adapter.py late enough that gateway is
# already loaded.
# ---------------------------------------------------------------------------

from encre.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
)
from encre.gateway.config import Platform
from encre.gateway.status import acquire_scoped_lock
from encre.config import get_env_value, save_env_value


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_LOADING_URL = "https://api.line.me/v2/bot/chat/loading/start"
LINE_CONTENT_URL_FMT = "https://api-data.line.me/v2/bot/message/{message_id}/content"
LINE_BOT_INFO_URL = "https://api.line.me/v2/bot/info"

# LINE Messaging API hard limits
LINE_PER_BUBBLE_CHARS = 5000  # Hard limit per text message object
LINE_SAFE_BUBBLE_CHARS = 4500  # Conservative limit for chunking
LINE_MAX_MESSAGES_PER_CALL = 5  # API rejects >5 messages per Reply/Push
LINE_REPLY_TOKEN_TTL_SECONDS = 50  # Conservative cap below LINE's ~60s

# Webhook hardening
WEBHOOK_BODY_MAX_BYTES = 1_048_576  # 1 MiB -- webhooks are tiny JSON
DEFAULT_WEBHOOK_PORT = 8646
DEFAULT_WEBHOOK_PATH = "/line/webhook"
DEFAULT_MEDIA_PATH_PREFIX = "/line/media"

# Slow-LLM postback button defaults
DEFAULT_SLOW_RESPONSE_THRESHOLD = 45.0  # seconds; 0 disables
DEFAULT_PENDING_REPLY_TEXT = (
    "馃 Still thinking. Tap below to fetch the answer when it's ready."
)
DEFAULT_BUTTON_LABEL = "Get answer"
DEFAULT_DELIVERED_TEXT = "Already replied ✓"
DEFAULT_INTERRUPTED_TEXT = "Run was interrupted before completion."

# Media defaults
MEDIA_TOKEN_TTL_SECONDS = 1800  # 30 minutes; LINE caches the URL aggressively
LINE_IMAGE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per LINE docs
LINE_AV_MAX_BYTES = 200 * 1024 * 1024  # 200 MB for voice/video

# Map LINE webhook message types to the normalized MessageType the gateway
# routes on. LINE has no separate "voice" type -- audio messages are recorded
# voice clips, so they map to VOICE (which the gateway sends through STT),
# mirroring how Telegram/WhatsApp classify voice notes. Anything unknown
# falls back to TEXT.
_LINE_MESSAGE_TYPES = {
    "text": MessageType.TEXT,
    "image": MessageType.PHOTO,
    "video": MessageType.VIDEO,
    "audio": MessageType.VOICE,
    "file": MessageType.DOCUMENT,
    "location": MessageType.LOCATION,
    "sticker": MessageType.STICKER,
}

# A 1x1 transparent PNG used as fallback video preview thumbnail when no
# explicit preview is supplied -- LINE requires ``previewImageUrl`` for
# video messages. Sourced from the Python stdlib (no Pillow dependency).
_FALLBACK_PNG_PREVIEW = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63000100000005000100377a7ff20000000049454e"
    "44ae426082"
)


# ---------------------------------------------------------------------------
# Markdown stripping (URL-preserving)
# ---------------------------------------------------------------------------

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITAL_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_MD_CODE_INLINE_RE = re.compile(r"`([^`]+)`")
_MD_CODE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)


def strip_markdown_preserving_urls(text: str) -> str:
    """Strip Markdown that LINE can't render, but keep URLs usable.

    LINE's text bubble has zero Markdown support -- bold, italics, code
    fences, headings, and bullet markers all render as literal characters.
    URLs *are* auto-linked by the client, but only when they appear bare
    (not inside ``[label](url)`` syntax). This converts ``[label](url)``
    to ``label (url)`` so the URL remains tappable, then strips the rest.

    Source: PR #18153 (leepoweii) -- adapted to keep code-block content
    visible (LINE users frequently want command snippets to land as
    plain text, not be eaten by the fence).
    """
    if not text:
        return text

    # Code blocks first -- keep the inner content, drop the fences.
    def _unfence(m: re.Match) -> str:
        """Strip a fenced-code delimiter pair and trim its trailing newline.

        Kept as a closure so ``re.sub`` can hand it back the full match;
        only group 1 (the interior) survives, which is what a LINE bubble
        can actually display.

        Args:
            m: Match object from ``_MD_CODE_BLOCK_RE``; group 1 is the
                body of the fenced block.

        Returns:
            The block body with the closing newline removed, or an empty
            string when the fence wrapped nothing.
        """
        return m.group(1).rstrip("\n")
    text = _MD_CODE_BLOCK_RE.sub(_unfence, text)

    # Inline code: keep content, drop backticks.
    text = _MD_CODE_INLINE_RE.sub(r"\1", text)

    # Markdown links -> "label (url)"
    text = _MD_LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)

    # Bold/italic markers -- strip.
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITAL_RE.sub(r"\1", text)

    # Headings (#, ##) and bullet markers -- strip the prefix only.
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_BULLET_RE.sub("鈥?", text)

    return text


def split_for_line(text: str, max_chars: int = LINE_SAFE_BUBBLE_CHARS) -> List[str]:
    """Split ``text`` into LINE-sized bubbles, preferring paragraph/line breaks.

    Returns at most ``LINE_MAX_MESSAGES_PER_CALL`` chunks; longer text is
    truncated with an ellipsis on the final chunk to keep the response
    deliverable in a single Reply/Push call.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    remaining = text
    while remaining and len(chunks) < LINE_MAX_MESSAGES_PER_CALL:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            remaining = ""
            break
        # Try to break on the latest paragraph or newline within budget.
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut < int(max_chars * 0.5):
            cut = remaining.rfind("\n", 0, max_chars)
        if cut < int(max_chars * 0.5):
            cut = remaining.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        # Truncate gracefully -- caller already burned its 5-bubble budget.
        if chunks:
            tail = chunks[-1]
            if len(tail) > max_chars - 1:
                tail = tail[: max_chars - 1]
            chunks[-1] = tail.rstrip() + "…"
        else:
            chunks.append(remaining[: max_chars - 1] + "…")
    return chunks


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def verify_line_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    """Verify a LINE webhook's ``X-Line-Signature`` header.

    LINE signs the *raw* request body with HMAC-SHA256 keyed by the
    channel secret, then base64-encodes the digest. Constant-time
    comparison defends against timing oracles.
    """
    if not signature or not channel_secret or body is None:
        return False
    try:
        digest = hmac.new(
            channel_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(digest).decode("utf-8")
    except Exception:
        return False
    # Compare as bytes: compare_digest raises TypeError on a str with
    # non-ASCII characters, and the signature is a raw request header.
    return hmac.compare_digest(expected.encode(), signature.encode())


# ---------------------------------------------------------------------------
# Cache state machine -- slow-LLM postback flow
# ---------------------------------------------------------------------------

class State(enum.Enum):
    """Lifecycle of one outstanding postback button request.

    These are the only states an entry in :class:`RequestCache` may hold.
    They gate both the outbound delivery path in :meth:`LineAdapter.send`
    and the inbound tap path in :meth:`LineAdapter._handle_postback_event`,
    so they are intentionally ordered as a one-way progression: a request
    can only move forward (or sideways into ``ERROR``), never back to
    ``PENDING``.

    Attributes:
        PENDING: Button was sent, the agent run is still executing.
        READY: Agent run completed; its full text is cached and waiting
            for the user to tap.
        DELIVERED: The cached payload already reached the user; a repeat
            tap must answer with the "already replied" acknowledgement
            instead of re-sending the response.
        ERROR: The run raised or was interrupted; the cached text is the
            interruption message rather than a real answer.
    """

    PENDING = "pending"  # button sent, LLM still running
    READY = "ready"      # LLM done, response cached, waiting for postback tap
    DELIVERED = "delivered"
    ERROR = "error"      # LLM raised / interrupted; cached error text waiting


@dataclass
class _CacheEntry:
    """One cached response row, keyed by an opaque request id.

    The ``request_id`` handed to the postback button's ``data`` field is
    this entry's dictionary key, so the tap can be routed back to the
    exact run that produced it.

    Attributes:
        state: Current lifecycle position; see :class:`State`.
        payload: Cached final text once ``READY``/``ERROR``, or the plain
            ``str`` error message in the ``ERROR`` case. ``None`` while
            ``PENDING``.
        chat_id: Line chat the button was posted to, used by
            :meth:`RequestCache.find_pending_for_chat` to match an
            incoming reply against an outstanding button.
        created_at: Epoch seconds when the entry was registered; the
            ``PENDING`` TTL is measured from here.
        updated_at: Epoch seconds of the last state transition; the
            ``READY``/``ERROR``/``DELIVERED`` TTL is measured from here.
    """

    state: State
    payload: Any = None
    chat_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class RequestCache:
    """In-memory cache for slow-LLM postback retrieval.

    PRs #18153 originally combined two TTLs -- one for PENDING (24h) and
    a shorter one for READY/DELIVERED/ERROR (1h). We keep the same model
    here.

    Invariant: a PENDING entry is keyed by ``created_at`` and a
    non-PENDING entry by ``updated_at``; :meth:`prune` relies on that
    split, so any new state must land on one side or the other.
    """

    def __init__(
        self,
        ttl_seconds: int = 3600,
        pending_ttl_seconds: int = 86400,
    ) -> None:
        """Create an empty cache with two separate expiry horizons.

        Args:
            ttl_seconds: Lifetime of a resolved entry (``READY``,
                ``ERROR``, ``DELIVERED``), counted from ``updated_at``.
            pending_ttl_seconds: Lifetime of a still-running entry,
                counted from ``created_at``. Defaults to a full day so a
                slow run is not silently discarded before the user taps.
        """
        self._entries: Dict[str, _CacheEntry] = {}
        self._ttl = ttl_seconds
        self._pending_ttl = pending_ttl_seconds

    def register_pending(self, chat_id: str) -> str:
        """Open a new ``PENDING`` slot and return its request id.

        Called from the slow-response timer in
        :meth:`LineAdapter._keep_typing`; the returned id is embedded in
        the postback button payload so the later tap can find this entry.

        Args:
            chat_id: Line chat the button was posted to.

        Returns:
            A fresh UUID string used as the cache key and button payload.
        """
        rid = str(uuid.uuid4())
        self._entries[rid] = _CacheEntry(state=State.PENDING, chat_id=chat_id)
        return rid

    def get(self, request_id: str) -> Optional[_CacheEntry]:
        """Look up an entry by request id, or ``None`` if expired/unknown.

        Args:
            request_id: The id carried by the tapped postback button.

        Returns:
            The live entry, or ``None`` when the id is unknown or has
            already been pruned. Never mutates state.
        """
        return self._entries.get(request_id)

    def set_ready(self, request_id: str, payload: Any) -> None:
        """Promote a ``PENDING`` entry to ``READY`` with its final text.

        Only a ``PENDING`` entry can be promoted: once the run ends there
        is exactly one terminal answer to cache, so a second call is a
        no-op rather than an overwrite.

        Args:
            request_id: Entry opened by :meth:`register_pending`.
            payload: Full agent response text to deliver on the tap.
        """
        entry = self._entries.get(request_id)
        if entry is None or entry.state is not State.PENDING:
            return
        entry.state = State.READY
        entry.payload = payload
        entry.updated_at = time.time()

    def set_error(self, request_id: str, message: str) -> None:
        """Promote a ``PENDING`` entry to ``ERROR`` with a failure message.

        Used by interrupt paths (see
        :meth:`LineAdapter.interrupt_session_activity`) so an orphaned
        button resolves to a human-readable notice instead of hanging.

        Args:
            request_id: Entry opened by :meth:`register_pending`.
            message: Text shown to the user if they tap the button.
        """
        entry = self._entries.get(request_id)
        if entry is None or entry.state is not State.PENDING:
            return
        entry.state = State.ERROR
        entry.payload = message
        entry.updated_at = time.time()

    def mark_delivered(self, request_id: str) -> None:
        """Mark a resolved entry ``DELIVERED`` after the tap succeeds.

        Restricted to ``READY``/``ERROR`` so that marking cannot swallow
        a still-running entry, and so a ``DELIVERED`` entry stays
        ``DELIVERED`` under repeated taps.

        Args:
            request_id: Entry whose payload was just sent.
        """
        entry = self._entries.get(request_id)
        if entry is None or entry.state not in {State.READY, State.ERROR}:
            return
        entry.state = State.DELIVERED
        entry.updated_at = time.time()

    def find_pending_for_chat(self, chat_id: str) -> Optional[str]:
        """Find an outstanding ``PENDING`` request id for a chat.

        Used by :meth:`LineAdapter.send` to decide whether an incoming
        agent response should be cached for a button tap rather than
        pushed immediately. Only ``PENDING`` entries match: a resolved
        entry means the button has already been answered, so its chat may
        send normal bubbles again.

        Args:
            chat_id: Line chat id to search for.

        Returns:
            The first matching request id, or ``None`` if the chat has
            no unresolved button.
        """
        for rid, entry in self._entries.items():
            if entry.state is State.PENDING and entry.chat_id == chat_id:
                return rid
        return None

    def prune(self) -> int:
        """Drop expired entries and return how many were removed.

        Iterates over a snapshot of the keys because entries are deleted
        during iteration. Applies the ``PENDING`` horizon to in-flight
        entries and the short horizon to resolved ones.

        Returns:
            Number of entries evicted from the cache.
        """
        now = time.time()
        removed = 0
        for rid in list(self._entries.keys()):
            entry = self._entries[rid]
            if entry.state is State.PENDING:
                if now - entry.created_at > self._pending_ttl:
                    del self._entries[rid]
                    removed += 1
            else:
                if now - entry.updated_at > self._ttl:
                    del self._entries[rid]
                    removed += 1
        return removed


# ---------------------------------------------------------------------------
# Inbound dedup
# ---------------------------------------------------------------------------

class _MessageDeduplicator:
    """Bounded LRU of LINE webhook event IDs to ignore at-least-once retries."""

    def __init__(self, max_size: int = 1000) -> None:
        """Create a dedup window holding at most ``max_size`` event ids.

        Args:
            max_size: High-water mark for the ``_seen`` map. Once crossed,
                the oldest tenth is evicted instead of refusing new ids,
                which keeps a burst of retries from starving later events.
        """
        self._seen: Dict[str, float] = {}
        self._max = max_size
        self._lock = threading.Lock()

    def is_duplicate(self, event_id: str) -> bool:
        """Record ``event_id`` and report whether it was already seen.

        LINE delivers webhooks at-least-once and re-sends on timeout, so
        every event id must be claimed exactly once. The check and the
        insertion happen under one lock because the webhook handler runs
        concurrently with the dedup eviction.

        Args:
            event_id: The ``webhookEventId`` from a LINE webhook event.
                Empty ids are never treated as duplicates, since they
                cannot be correlated at all.

        Returns:
            ``True`` if this id was seen before and the event must be
            dropped, ``False`` if it was just claimed for the first time.
        """
        if not event_id:
            return False
        with self._lock:
            if event_id in self._seen:
                return True
            if len(self._seen) >= self._max:
                cutoff = sorted(self._seen.values())[len(self._seen) // 10 or 1]
                self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
            self._seen[event_id] = time.time()
            return False


# ---------------------------------------------------------------------------
# Source / chat-id resolution
# ---------------------------------------------------------------------------

def _resolve_chat(source: Dict[str, Any]) -> Tuple[str, str]:
    """Return ``(chat_id, chat_type)`` from a LINE event ``source`` block.

    LINE sources are one of:
      * ``{"type": "user",  "userId":  "U..."}``  -> 1:1 DM
      * ``{"type": "group", "groupId": "C...", "userId": "U..."}``  -> group chat
      * ``{"type": "room",  "roomId":  "R...", "userId": "U..."}``  -> multi-user room

    The returned chat id is what the allowlists and the reply-token store
    key on, so it must be stable across every event from the same
    conversation.

    Args:
        source: The ``source`` block of a LINE webhook event.

    Returns:
        Tuple of ``(chat_id, chat_type)`` where ``chat_type`` is one of
        ``"dm"``, ``"group"``, or ``"room"``. Unknown sources degrade to
        an empty id with type ``"dm"``.

    Source: PR #21023 (perng), unchanged.
    """
    src_type = (source or {}).get("type", "")
    if src_type == "group":
        return source.get("groupId", ""), "group"
    if src_type == "room":
        return source.get("roomId", ""), "room"
    if src_type == "user":
        return source.get("userId", ""), "dm"
    return "", "dm"


def _allowed_for_source(
    source: Dict[str, Any],
    *,
    allow_all: bool,
    user_ids: Set[str],
    group_ids: Set[str],
    room_ids: Set[str],
) -> bool:
    """Three-list access gate -- credit PR #18153.

    LINE identifies a conversation by one of three mutually exclusive id
    prefixes (``U`` user, ``C`` group, ``R`` room), so a single flat
    allowlist cannot express the policy. Each list is consulted only for
    its own source type, which means an id in the wrong list never grants
    access even if it is a valid LINE id elsewhere.

    Args:
        source: LINE event ``source`` block.
        allow_all: Dev escape hatch (``LINE_ALLOW_ALL_USERS``) that bypasses
            every list.
        user_ids: Direct-message ids permitted to talk to the bot.
        group_ids: Group ids permitted to talk to the bot.
        room_ids: Room ids permitted to talk to the bot.

    Returns:
        ``True`` when the event should be processed. ``allow_all`` short-
        circuits every check and returns ``True`` unconditionally; without
        it, an unknown source type always returns ``False``.
    """
    if allow_all:
        return True
    src_type = (source or {}).get("type", "")
    if src_type == "user":
        uid = source.get("userId", "")
        return bool(uid) and uid in user_ids
    if src_type == "group":
        gid = source.get("groupId", "")
        return bool(gid) and gid in group_ids
    if src_type == "room":
        rid = source.get("roomId", "")
        return bool(rid) and rid in room_ids
    return False


# ---------------------------------------------------------------------------
# LINE Reply / Push HTTP client
# ---------------------------------------------------------------------------

class _LineClient:
    """Thin async wrapper around the LINE Messaging API.

    We use ``aiohttp`` directly to avoid a ``line-bot-sdk`` dependency
    (the SDK pulls in its own httpx pin and the ergonomic gain is small
    for the four endpoints we actually call).
    """

    def __init__(self, channel_access_token: str, *, timeout: float = 15.0) -> None:
        """Bind the Bearer token and default per-request timeout.

        Args:
            channel_access_token: LINE channel access token from
                ``LINE_CHANNEL_ACCESS_TOKEN`` or ``config.extra``. It is
                stored verbatim and placed in the ``Authorization`` header
                on every call, so it must never be logged.
            timeout: Total wall-clock budget in seconds for a single
                Reply/Push/Loading request. Content downloads and the bot
                info lookup use their own tighter budgets.
        """
        self._token = channel_access_token
        self._timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        }

    async def reply(self, reply_token: str, messages: List[Dict[str, Any]]) -> None:
        """Send ``messages`` using a single-use reply token.

        This is the free path: LINE does not meter Reply, so callers try
        it before :meth:`push`. A token is bound to one chat and one
        webhook event and expires after roughly 60 seconds; the adapter
        caps it earlier at ``LINE_REPLY_TOKEN_TTL_SECONDS``.

        Args:
            reply_token: Token echoed back by LINE with the inbound event.
            messages: Already-built message objects, at most
                ``LINE_MAX_MESSAGES_PER_CALL`` of them.

        Raises:
            RuntimeError: When LINE answers 4xx/5xx. The body is truncated
                to 200 characters to keep log noise small. Callers are
                expected to catch this and fall back to :meth:`push`.
        """
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                LINE_REPLY_URL,
                headers=self._headers,
                json={"replyToken": reply_token, "messages": messages},
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(f"LINE reply {resp.status}: {body[:200]}")

    async def push(self, chat_id: str, messages: List[Dict[str, Any]]) -> None:
        """Send ``messages`` to ``chat_id`` directly, bypassing reply tokens.

        Push is metered and is the fallback whenever no reply token is
        available, expired, or rejected. It needs only a chat id, which is
        why it is also the only option for the standalone cron delivery
        path, which never sees a webhook event.

        Args:
            chat_id: Target ``U``/``C``/``R`` id.
            messages: Already-built message objects.

        Raises:
            RuntimeError: On a 4xx/5xx response from LINE, with the body
                truncated to 200 characters.
        """
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                LINE_PUSH_URL,
                headers=self._headers,
                json={"to": chat_id, "messages": messages},
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(f"LINE push {resp.status}: {body[:200]}")

    async def loading(self, chat_id: str, seconds: int = 60) -> None:
        """Loading indicator (DM only). LINE rejects this for groups/rooms."""
        if not chat_id or not chat_id.startswith("U"):
            return
        import aiohttp
        # LINE caps loadingSeconds in 5-step increments, max 60.
        clamped = max(5, min(60, (seconds // 5) * 5 or 5))
        try:
            timeout = aiohttp.ClientTimeout(total=5.0)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                await session.post(
                    LINE_LOADING_URL,
                    headers=self._headers,
                    json={"chatId": chat_id, "loadingSeconds": clamped},
                )
        except Exception as exc:  # best-effort; never raise
            logger.debug("LINE loading indicator failed: %s", exc)

    async def fetch_content(self, message_id: str) -> bytes:
        """Download an inbound media message's binary content."""
        import aiohttp
        url = LINE_CONTENT_URL_FMT.format(message_id=message_id)
        timeout = aiohttp.ClientTimeout(total=30.0)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, headers={"Authorization": f"Bearer {self._token}"}) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"LINE content {resp.status}")
                return await resp.read()

    async def get_bot_user_id(self) -> Optional[str]:
        """Fetch this channel's own userId so we can filter self-messages."""
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10.0)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.get(LINE_BOT_INFO_URL, headers=self._headers) as resp:
                    if resp.status >= 400:
                        return None
                    data = await resp.json()
                    return data.get("userId")
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _text_message(text: str) -> Dict[str, Any]:
    """Build a LINE text message object, capped to per-bubble max."""
    if len(text) > LINE_PER_BUBBLE_CHARS:
        text = text[: LINE_PER_BUBBLE_CHARS - 1] + "…"
    return {"type": "text", "text": text}


def _image_message(original_url: str, preview_url: Optional[str] = None) -> Dict[str, Any]:
    """Build a LINE ``image`` message object from reachable HTTPS URLs.

    LINE never accepts binary uploads, so both fields must be URLs it can
    fetch itself; the adapter supplies them via :meth:`LineAdapter._media_url`.

    Args:
        original_url: Full-resolution image URL.
        preview_url: Thumbnail URL; falls back to ``original_url`` when
            omitted, since LINE requires the field to be present.

    Returns:
        Message object ready to embed in a Reply or Push call.
    """
    return {
        "type": "image",
        "originalContentUrl": original_url,
        "previewImageUrl": preview_url or original_url,
    }


def _audio_message(url: str, duration_ms: int = 1000) -> Dict[str, Any]:
    """Build a LINE ``audio`` message object.

    LINE has no separate voice-note type, so voice replies travel as plain
    audio. The duration is cosmetic -- LINE reads the real length from the
    downloaded file -- but a plausible default keeps the client UI from
    showing a 00:00 placeholder.

    Args:
        url: HTTPS URL of the audio file.
        duration_ms: Declared duration in milliseconds.

    Returns:
        Message object ready to embed in a Reply or Push call.
    """
    return {
        "type": "audio",
        "originalContentUrl": url,
        "duration": int(duration_ms),
    }


def _video_message(url: str, preview_url: str) -> Dict[str, Any]:
    """Build a LINE ``video`` message object.

    Unlike images, LINE hard-requires ``previewImageUrl`` for video, so
    callers must always pass one. See
    :meth:`LineAdapter.send_video`, which synthesizes a 1x1 PNG thumbnail
    when the caller has none.

    Args:
        url: HTTPS URL of the video file.
        preview_url: HTTPS URL of the thumbnail image.

    Returns:
        Message object ready to embed in a Reply or Push call.
    """
    return {
        "type": "video",
        "originalContentUrl": url,
        "previewImageUrl": preview_url,
    }


def build_postback_button_message(
    text: str, button_label: str, request_id: str
) -> Dict[str, Any]:
    """Template Buttons message -- the slow-LLM postback bubble.

    From PR #18153 (leepoweii). Template Buttons stay tappable from chat
    history, unlike Quick Reply chips which are dismissed the moment any
    new message arrives in the chat.

    LINE limits: ``text`` <= 160 chars, ``altText`` <= 400 chars.
    ``altText`` is the fallback rendering for notifications and screen
    readers, so it gets the longer budget.

    Args:
        text: Bubble body, truncated to the 160-char LINE limit.
        button_label: Button face text; capped at 20 chars and falling
            back to ``"Get answer"`` when blank.
        request_id: Cached request id embedded as JSON in the postback
            ``data`` field, so the tap can be routed back to the entry in
            :class:`RequestCache`.

    Returns:
        A ``template`` message object whose single action is a
        ``postback`` with ``action`` set to ``show_response``.
    """
    truncated = text if len(text) <= 160 else text[:157] + "..."
    alt = text if len(text) <= 400 else text[:397] + "..."
    return {
        "type": "template",
        "altText": alt,
        "template": {
            "type": "buttons",
            "text": truncated,
            "actions": [
                {
                    "type": "postback",
                    "label": button_label[:20] or "Get answer",
                    "data": json.dumps(
                        {"action": "show_response", "request_id": request_id}
                    ),
                    "displayText": button_label[:300] or "Get answer",
                }
            ],
        },
    }


# Prefixes the gateway uses for system busy-acks (interrupting / queued /
# steered). When the postback cache has a PENDING entry we *bypass* the
# cache for these so they reach the user as visible bubbles instead of
# being silently swallowed. From PR #18153.
_SYSTEM_BYPASS_PREFIXES: Tuple[str, ...] = (
    "鈿?Interrupting",
    "鈴?Queued",
    "鈴?Steered",
    "馃捑",  # background-review summary
)


def _is_system_bypass(content: str) -> bool:
    """Detect gateway system busy-acks that must not be cached.

    When a chat already has a ``PENDING`` postback button, a normal agent
    response is routed into the cache for the user to tap. System
    notifications (interrupting / queued / steered) are the exception:
    they are status signals the user needs immediately, so
    :meth:`LineAdapter.send` pushes them straight to LINE instead of
    swallowing them.

    Prefix matching is deliberately coarse -- the gateway owns these
    strings, so a prefix hit is unambiguous.

    Args:
        content: Candidate response text.

    Returns:
        ``True`` when ``content`` starts with one of
        ``_SYSTEM_BYPASS_PREFIXES``. Empty input always returns
        ``False``.
    """
    if not content:
        return False
    return any(content.startswith(p) for p in _SYSTEM_BYPASS_PREFIXES)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _csv_set(value: str) -> Set[str]:
    """Split a comma-separated string into a stripped id set.

    LINE id lists arrive from two sources with slightly different habits:
    environment variables are one long string, while ``config.yaml``
    extra values are real lists. This collapses the env side so both feed
    the same ``| set(...)`` union in the adapter constructor.

    Args:
        value: Comma-separated ids; blanks and whitespace-only items are
            dropped so a trailing comma is harmless.

    Returns:
        Set of trimmed ids, or an empty set for a falsy input.
    """
    if not value:
        return set()
    return {x.strip() for x in value.split(",") if x.strip()}


def _truthy_env(name: str, default: bool = False) -> bool:
    """Read a boolean env var, accepting the usual truthy spellings.

    Env vars are always strings, so ``"false"`` would evaluate truthy with
    a plain ``bool()``. This accepts only the common affirmative forms and
    treats everything else as falsy.

    Args:
        name: Environment variable name.
        default: Value returned when the variable is unset, which lets the
            ``config.yaml`` extra value act as the fallback.

    Returns:
        ``True`` for ``1``/``true``/``yes``/``on`` (case-insensitive),
        ``False`` for any other set value, and ``default`` when unset.
    """
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class LineAdapter(BasePlatformAdapter):
    """LINE Messaging API gateway adapter.

    Runs an embedded aiohttp webhook server instead of polling, so this
    adapter owns an HTTP port for its lifetime: inbound events arrive at
    ``webhook_path`` and outbound replies leave through
    :class:`_LineClient`. See :class:`encre.gateway.platforms.base.BasePlatformAdapter`.
    """

    # LINE has its own message-edit story (none) -- we always send fresh
    # bubbles, never edit, so REQUIRES_EDIT_FINALIZE stays False.

    def __init__(self, config, **kwargs):
        """Build the adapter from platform config and environment.

        Every setting is layered the same way: environment variable first
        (so a profile can be driven entirely from ``.env``), then the
        ``config.extra`` mapping from ``config.yaml`` as the fallback.
        ``config.yaml`` is never committed, which is why the token reads
        go through ``os.getenv``.

        Args:
            config: PlatformConfig for LINE carrying an optional ``extra``
                dict.
            **kwargs: Forwarded to the base adapter constructor.
        """
        platform = Platform("line")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        # Credentials
        self.channel_access_token = (
            os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
            or extra.get("channel_access_token", "")
        )
        self.channel_secret = (
            os.getenv("LINE_CHANNEL_SECRET")
            or extra.get("channel_secret", "")
        )

        # Webhook server
        self.webhook_host = os.getenv("LINE_HOST") or extra.get("host", "0.0.0.0")
        try:
            self.webhook_port = int(
                os.getenv("LINE_PORT") or extra.get("port", DEFAULT_WEBHOOK_PORT)
            )
        except (TypeError, ValueError):
            self.webhook_port = DEFAULT_WEBHOOK_PORT
        self.webhook_path = extra.get("webhook_path", DEFAULT_WEBHOOK_PATH)

        # Public base URL -- required for media sending when bind isn't
        # publicly reachable.
        self.public_base_url = (
            os.getenv("LINE_PUBLIC_URL")
            or extra.get("public_url", "")
            or ""
        ).rstrip("/")

        # Three-allowlist gating
        self.allow_all = _truthy_env(
            "LINE_ALLOW_ALL_USERS", bool(extra.get("allow_all_users", False))
        )
        self.allowed_users = _csv_set(
            os.getenv("LINE_ALLOWED_USERS", "")
        ) | set(extra.get("allowed_users", []))
        self.allowed_groups = _csv_set(
            os.getenv("LINE_ALLOWED_GROUPS", "")
        ) | set(extra.get("allowed_groups", []))
        self.allowed_rooms = _csv_set(
            os.getenv("LINE_ALLOWED_ROOMS", "")
        ) | set(extra.get("allowed_rooms", []))

        # Slow-LLM postback button threshold
        try:
            self.slow_response_threshold = float(
                os.getenv("LINE_SLOW_RESPONSE_THRESHOLD")
                or extra.get("slow_response_threshold", DEFAULT_SLOW_RESPONSE_THRESHOLD)
            )
        except (TypeError, ValueError):
            self.slow_response_threshold = DEFAULT_SLOW_RESPONSE_THRESHOLD

        # User-overridable copy
        self.pending_text = (
            os.getenv("LINE_PENDING_TEXT")
            or extra.get("pending_text", DEFAULT_PENDING_REPLY_TEXT)
        )
        self.button_label = (
            os.getenv("LINE_BUTTON_LABEL")
            or extra.get("button_label", DEFAULT_BUTTON_LABEL)
        )
        self.delivered_text = (
            os.getenv("LINE_DELIVERED_TEXT")
            or extra.get("delivered_text", DEFAULT_DELIVERED_TEXT)
        )
        self.interrupted_text = (
            os.getenv("LINE_INTERRUPTED_TEXT")
            or extra.get("interrupted_text", DEFAULT_INTERRUPTED_TEXT)
        )

        # Runtime state
        self._client: Optional[_LineClient] = None
        self._app = None  # aiohttp.web.Application
        self._runner = None  # aiohttp.web.AppRunner
        self._site = None  # aiohttp.web.TCPSite
        self._reply_tokens: Dict[str, Tuple[str, float]] = {}  # chat_id -> (token, expiry)
        self._cache = RequestCache()
        self._dedup = _MessageDeduplicator()
        self._bot_user_id: Optional[str] = None
        self._lock_key: Optional[str] = None

        # Media state
        self._media_tokens: Dict[str, Tuple[str, float]] = {}  # token -> (path, expiry)
        self._media_temp_paths: Set[str] = set()
        self._media_ttl = MEDIA_TOKEN_TTL_SECONDS

        # Pending-button slot per chat -- ensures one outstanding postback
        # button per chat at a time. Postback cache request_id keyed by chat_id.
        self._pending_buttons: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Start the webhook server and validate the channel.

        LINE has no outbound polling API, so this is the only way the
        adapter becomes live: it must bind a public port that the LINE
        console is pointed at. Failures are reported through
        ``_set_fatal_error`` with an explicit ``retryable`` flag so
        :class:`encre.gateway.run.GatewayRunner` can decide whether to
        keep retrying the profile.

        Args:
            is_reconnect: Unused by LINE; accepted for signature parity
                with other adapters.

        Returns:
            ``True`` when the webhook port is listening, ``False`` after
            recording a fatal error.

        Raises:
            Nothing -- all failures are converted into fatal errors by
            design, since the caller owns the reconnect loop.
        """
        if not self.channel_access_token or not self.channel_secret:
            self._set_fatal_error(
                "config_missing",
                "LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET must be set",
                retryable=False,
            )
            return False

        # Prevent two profiles from running on the same channel access token.
        # Use a hash of the token so we don't write the secret to disk.
        tok_hash = hashlib.sha256(self.channel_access_token.encode()).hexdigest()[:16]
        if not acquire_scoped_lock("line", tok_hash):
            self._set_fatal_error(
                "lock_conflict",
                "LINE channel already in use by another profile",
                retryable=False,
            )
            return False
        self._lock_key = tok_hash

        self._client = _LineClient(self.channel_access_token)

        # Best-effort: fetch our own bot userId for self-message filtering.
        # If the call fails (offline tests, transient 5xx) we fall back to
        # not filtering self-events; the cost is minor (LINE doesn't
        # actually echo our own messages back).
        try:
            self._bot_user_id = await self._client.get_bot_user_id()
        except Exception as exc:
            logger.debug("LINE: get_bot_user_id failed: %s", exc)
            self._bot_user_id = None

        # Spin up the aiohttp webhook server.
        self._app = web.Application(client_max_size=WEBHOOK_BODY_MAX_BYTES)
        self._app.router.add_post(self.webhook_path, self._handle_webhook)
        # Public health probe -- useful for tunnel/proxy verification.
        self._app.router.add_get(f"{self.webhook_path}/health", self._handle_health)
        # Media serving endpoint.
        self._app.router.add_get(
            f"{DEFAULT_MEDIA_PATH_PREFIX}/{{token}}/{{filename}}",
            self._handle_media,
        )

        self._runner = web.AppRunner(self._app)
        try:
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.webhook_host, self.webhook_port)
            await self._site.start()
        except OSError as exc:
            self._set_fatal_error(
                "bind_failed",
                f"Could not bind LINE webhook on {self.webhook_host}:{self.webhook_port}: {exc}",
                retryable=True,
            )
            return False

        self._mark_connected()
        logger.info(
            "LINE: webhook listening on %s:%s%s%s",
            self.webhook_host,
            self.webhook_port,
            self.webhook_path,
            f" (public: {self.public_base_url})" if self.public_base_url else "",
        )
        return True

    async def disconnect(self) -> None:
        """Tear down the webhook server, media files, and the channel lock.

        Cleanup is ordered from most specific to least: the listening site
        and runner are stopped first so no request can race the temp-file
        deletion, then tracked tempfiles are unlinked, and finally the
        scoped lock is released so another profile may take the same
        channel. Every step is individually guarded -- a broken cleanup
        must not prevent the remaining steps from running.

        Raises:
            Nothing; all sub-failures are swallowed so ``GatewayRunner``
            can always complete shutdown.
        """
        self._mark_disconnected()

        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
        self._app = None

        # Cleanup any tracked tempfiles.
        for path in list(self._media_temp_paths):
            try:
                os.unlink(path)
            except OSError:
                pass
        self._media_temp_paths.clear()
        self._media_tokens.clear()

        if self._lock_key:
            try:
                from encre.gateway.status import release_scoped_lock
                release_scoped_lock("line", self._lock_key)
            except Exception:
                pass
            self._lock_key = None

    # ------------------------------------------------------------------
    # Webhook handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request) -> Any:
        """Answer the unauthenticated health probe.

        Mounted at ``<webhook_path>/health`` and deliberately outside the
        signature check, so a tunnel or reverse proxy can verify the port
        is up without needing a LINE request.

        Args:
            request: aiohttp request object; ignored.

        Returns:
            JSON response with ``status`` and the platform name.
        """
        from aiohttp import web
        return web.json_response({"status": "ok", "platform": "line"})

    async def _handle_webhook(self, request) -> Any:
        """Validate and fan out one LINE webhook delivery.

        LINE requires a fast 2xx so it stops retrying, so every outcome
        returns promptly and event processing failures are logged rather
        than propagated. A single delivery may contain several events,
        each dispatched independently so one bad event cannot discard the
        rest of the batch.

        Args:
            request: aiohttp request with the raw JSON body and the
                ``X-Line-Signature`` header.

        Returns:
            ``200`` on success, ``401`` for a bad signature, ``400`` for
            an unreadable or unparsable body, ``413`` over the body cap.

        Raises:
            Nothing; all failures map to an HTTP error status.
        """
        from aiohttp import web

        # Body cap defends against memory-exhaustion via crafted Content-Length
        # (aiohttp's client_max_size only applies to certain body modes).
        try:
            body = await request.read()
        except Exception as exc:
            logger.debug("LINE: read failed: %s", exc)
            return web.Response(status=400, text="bad request")
        if len(body) > WEBHOOK_BODY_MAX_BYTES:
            return web.Response(status=413, text="payload too large")

        signature = request.headers.get("X-Line-Signature", "")
        if not verify_line_signature(body, signature, self.channel_secret):
            return web.Response(status=401, text="invalid signature")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.Response(status=400, text="bad json")

        events = payload.get("events", []) or []
        for event in events:
            try:
                await self._dispatch_event(event)
            except Exception:
                logger.exception("LINE: dispatch_event failed")

        return web.Response(status=200, text="ok")

    async def _dispatch_event(self, event: Dict[str, Any]) -> None:
        """Apply the four intake gates, then route by event type.

        Ordering matters: dedup is first because a retried delivery should
        not be re-admitted even if it is otherwise authorized; the
        allowlist comes after self-echo filtering so our own messages are
        dropped cheaply without logging an authorization rejection.

        Args:
            event: One element of a webhook ``events`` array.

        Raises:
            Exceptions from ``_handle_message_event`` propagate to the
            caller, which logs and continues with the next event.
        """
        event_type = event.get("type")
        source = event.get("source") or {}
        webhook_event_id = event.get("webhookEventId", "") or ""

        # Dedup retries (LINE webhooks may be re-delivered).
        if webhook_event_id and self._dedup.is_duplicate(webhook_event_id):
            logger.debug("LINE: ignoring duplicate webhook event %s", webhook_event_id)
            return

        # Filter our own messages (self-echo).
        sender_user_id = source.get("userId", "")
        if self._bot_user_id and sender_user_id == self._bot_user_id:
            return

        # Allowlist gate.
        if not _allowed_for_source(
            source,
            allow_all=self.allow_all,
            user_ids=self.allowed_users,
            group_ids=self.allowed_groups,
            room_ids=self.allowed_rooms,
        ):
            logger.info("LINE: rejecting unauthorized source %s", source)
            return

        if event_type == "message":
            await self._handle_message_event(event)
        elif event_type == "postback":
            await self._handle_postback_event(event)
        elif event_type in {"follow", "unfollow", "join", "leave"}:
            logger.info("LINE: lifecycle event %s from %s", event_type, source)
        else:
            logger.debug("LINE: ignoring event type %r", event_type)

    async def _handle_message_event(self, event: Dict[str, Any]) -> None:
        """Turn a LINE message event into a normalized :class:`MessageEvent`.

        This is the intake half of the Encre pipeline: the event is
        normalized here and handed to :meth:`handle_message`, which hands
        it to :class:`encre.gateway.run.GatewayRunner` for authorization
        and session resolution. The agent's reply comes back later through
        :class:`encre.gateway.stream_consumer.GatewayStreamConsumer` and
        this adapter's :meth:`send`.

        The reply token is stashed by chat id before anything else so a
        later send in the same turn can find it, even if the run itself
        fails.

        Args:
            event: LINE webhook event of type ``"message"``.

        Raises:
            Exceptions from ``handle_message`` propagate; the webhook
            handler logs and drops the batch remainder.
        """
        msg = event.get("message") or {}
        msg_type = msg.get("type", "")
        message_id = msg.get("id", "")
        reply_token = event.get("replyToken", "")
        source = event.get("source") or {}
        chat_id, chat_type = _resolve_chat(source)
        user_id = source.get("userId", "") or chat_id

        # Stash the reply token for outbound use.
        if chat_id and reply_token:
            self._reply_tokens[chat_id] = (
                reply_token,
                time.time() + LINE_REPLY_TOKEN_TTL_SECONDS,
            )

        # Handle media inbound -- fetch the binary, cache it, and surface a
        # vision-tool-friendly local path on the MessageEvent.
        media_urls: List[str] = []
        media_types: List[str] = []
        text = ""

        if msg_type == "text":
            text = msg.get("text", "") or ""
        elif msg_type in {"image", "audio", "video", "file"}:
            local_path = await self._download_media(message_id, msg_type)
            if local_path:
                media_urls.append(local_path)
                media_types.append(msg_type)
            text = f"[{msg_type}]"
        elif msg_type == "sticker":
            keywords = msg.get("keywords") or []
            text = f"[sticker: {', '.join(keywords)}]" if keywords else "[sticker]"
        elif msg_type == "location":
            title = msg.get("title", "")
            address = msg.get("address", "")
            text = f"[location: {title} {address}]".strip()
        else:
            text = f"[unsupported message type: {msg_type}]"

        # Best-effort typing indicator (DM only).
        if chat_type == "dm" and self._client:
            asyncio.create_task(self._client.loading(chat_id))

        source_obj = self.build_source(
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_id,
            chat_name=chat_id,
        )

        event_obj = MessageEvent(
            text=text,
            message_type=_LINE_MESSAGE_TYPES.get(msg_type, MessageType.TEXT),
            source=source_obj,
            raw_message=event,
            message_id=message_id,
            media_urls=media_urls,
            media_types=media_types,
        )

        await self.handle_message(event_obj)

    async def _handle_postback_event(self, event: Dict[str, Any]) -> None:
        """User tapped the slow-LLM postback button -- deliver cached payload.

        The tap is a fresh webhook event, so ``replyToken`` here is valid
        for about 60 seconds: unlike the original burn, this reply is
        free and does not need the Push fallback's urgency, though
        ``READY`` still tries reply-then-push for robustness.

        Every branch marks the entry ``DELIVERED`` on success so a repeat
        tap degrades to an acknowledgement instead of resending the
        answer, and clears ``_pending_buttons`` so the chat may send
        normal bubbles again.

        Args:
            event: LINE webhook event of type ``"postback"``.

        Raises:
            Nothing; all send failures are logged at warning or error
            level.
        """
        postback = event.get("postback") or {}
        data = postback.get("data", "") or ""
        reply_token = event.get("replyToken", "")
        source = event.get("source") or {}
        chat_id, _ = _resolve_chat(source)

        try:
            parsed = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            return

        if parsed.get("action") != "show_response":
            return
        request_id = parsed.get("request_id", "")
        if not request_id:
            return

        entry = self._cache.get(request_id)
        if not self._client or not reply_token or not entry:
            return

        if entry.state is State.READY:
            payload = entry.payload or ""
            chunks = split_for_line(strip_markdown_preserving_urls(str(payload)))
            messages = [_text_message(c) for c in chunks][:LINE_MAX_MESSAGES_PER_CALL]
            try:
                await self._client.reply(reply_token, messages)
                self._cache.mark_delivered(request_id)
                self._pending_buttons.pop(chat_id, None)
            except Exception as exc:
                logger.warning("LINE: postback reply failed (%s); falling back to push", exc)
                try:
                    await self._client.push(chat_id, messages)
                    self._cache.mark_delivered(request_id)
                    self._pending_buttons.pop(chat_id, None)
                except Exception as exc2:
                    logger.error("LINE: postback push fallback failed: %s", exc2)
        elif entry.state is State.ERROR:
            text = str(entry.payload or self.interrupted_text)
            try:
                await self._client.reply(reply_token, [_text_message(text)])
                self._cache.mark_delivered(request_id)
                self._pending_buttons.pop(chat_id, None)
            except Exception as exc:
                logger.warning("LINE: postback ERROR reply failed: %s", exc)
        elif entry.state is State.DELIVERED:
            try:
                await self._client.reply(reply_token, [_text_message(self.delivered_text)])
            except Exception:
                pass
        elif entry.state is State.PENDING:
            # Still working -- re-issue the wait notice.
            try:
                await self._client.reply(reply_token, [_text_message(self.pending_text)])
            except Exception:
                pass

    async def _download_media(self, message_id: str, msg_type: str) -> Optional[str]:
        """Fetch an inbound media message and return a local cache path.

        LINE stores message payloads on its own CDN, so the binary has to
        be pulled explicitly before the agent can inspect it. The bytes
        are handed to :func:`encre.gateway.platforms.base.cache_image_from_bytes`,
        which writes into Encre's shared cache directory, so downstream
        vision tools see a stable local path rather than a transient URL.

        The extension comes from the LINE message type, not from a MIME
        sniff, because the CDN URL carries no reliable content type.

        Args:
            message_id: LINE message id of the inbound media object.
            msg_type: LINE type string, one of ``image``, ``audio``,
                ``video``, or ``file``.

        Returns:
            Local filesystem path of the cached payload, or ``None`` when
            the client is absent, the fetch fails, or caching fails.
        """
        if not self._client or not message_id:
            return None
        try:
            data = await self._client.fetch_content(message_id)
        except Exception as exc:
            logger.warning("LINE: failed to fetch %s content for %s: %s", msg_type, message_id, exc)
            return None
        ext = {
            "image": ".jpg",
            "audio": ".m4a",
            "video": ".mp4",
            "file": ".bin",
        }.get(msg_type, ".bin")
        try:
            return cache_image_from_bytes(data, ext=ext)
        except Exception as exc:
            logger.warning("LINE: failed to cache %s payload: %s", msg_type, exc)
            return None

    # ------------------------------------------------------------------
    # Outbound send (text)
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Deliver one agent response to ``chat_id``.

        Entry point for
        :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`. The
        text-first path is split into three cases:

        1. System busy-acks bypass the cache and go straight to LINE, so
           an interruption notice is never hidden behind a pending button.
        2. If the chat has a ``PENDING`` postback button, the response is
           cached and the method returns success immediately -- the user
           will tap the button to receive it, so no wire call happens now.
        3. Otherwise the text is chunked and sent normally.

        ``reply_to`` is accepted for interface parity but ignored: LINE
        has no reply-to-thread primitive, and this adapter never edits
        messages.

        Args:
            chat_id: Target ``U``/``C``/``R`` id.
            content: Plain text response from the agent.
            reply_to: Ignored; kept for base-class parity.
            metadata: Ignored; kept for base-class parity.

        Returns:
            :class:`SendResult`. On the cached path ``message_id`` is the
            request id rather than a LINE message id.
        """
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")

        # System busy-acks (interrupting / queued / steered) bypass the
        # postback cache and route directly to LINE so they reach the user
        # as visible bubbles. Source: PR #18153.
        if _is_system_bypass(content):
            return await self._send_text_chunks(chat_id, content, force_push=False)

        # If the chat has a PENDING postback button outstanding, route the
        # response into the cache for the user to fetch via tap.
        pending_rid = self._pending_buttons.get(chat_id)
        if pending_rid:
            self._cache.set_ready(pending_rid, content)
            return SendResult(success=True, message_id=pending_rid)

        return await self._send_text_chunks(chat_id, content, force_push=False)

    async def _send_text_chunks(
        self,
        chat_id: str,
        content: str,
        *,
        force_push: bool,
    ) -> SendResult:
        """Chunk ``content`` and send it reply-first with push fallback.

        Markdown is stripped first because LINE renders it literally, and
        chunking happens at the 4500-char safety margin rather than the
        5000-char hard limit to leave room for LINE's own escaping.

        Reply is attempted before push because it is unmetered, but the
        reply token is single-use, so ``_consume_reply_token`` pops it out
        of the store before the network call: if the call fails and we
        fall through to push, the token is not retried.

        Args:
            chat_id: Target chat id.
            content: Plain text response.
            force_push: Skip the reply attempt entirely. Reserved for the
                media path, which always pushes.

        Returns:
            :class:`SendResult`; ``message_id`` is the reply token on the
            reply path and ``None`` on push, which returns no id.
        """
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")

        chunks = split_for_line(strip_markdown_preserving_urls(content))
        if not chunks:
            return SendResult(success=True, message_id=None)
        messages = [_text_message(c) for c in chunks][:LINE_MAX_MESSAGES_PER_CALL]

        token, used_reply = self._consume_reply_token(chat_id)
        if used_reply and not force_push:
            try:
                await self._client.reply(token, messages)
                return SendResult(success=True, message_id=token)
            except Exception as exc:
                logger.info("LINE: reply token rejected (%s); falling back to push", exc)
                # fall through to push

        try:
            await self._client.push(chat_id, messages)
            return SendResult(success=True, message_id=None)
        except Exception as exc:
            logger.error("LINE: push send failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    def _consume_reply_token(self, chat_id: str) -> Tuple[str, bool]:
        """Consume a stashed reply token if present and unexpired.

        Returns ``(token, used_reply)``.
        """
        entry = self._reply_tokens.pop(chat_id, None)
        if not entry:
            return "", False
        token, expires_at = entry
        if not token or time.time() >= expires_at:
            return "", False
        return token, True

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Trigger LINE's loading-animation indicator (DM only)."""
        if self._client and chat_id:
            await self._client.loading(chat_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Best-effort chat info derived from the chat_id prefix.

        LINE's chat-info APIs are limited and per-source-type -- instead of
        chasing them we infer from the well-known ID prefixes:
        ``U`` = user (1:1), ``C`` = group, ``R`` = room. The agent only
        needs ``name`` + ``type`` from this method.
        """
        prefix = (chat_id or "")[:1]
        chat_type = {"U": "dm", "C": "group", "R": "channel"}.get(prefix, "dm")
        return {"name": chat_id or "", "type": chat_type}

    def format_message(self, content: str) -> str:
        """Strip Markdown that LINE can't render. URLs are preserved."""
        return strip_markdown_preserving_urls(content)

    # ------------------------------------------------------------------
    # Slow-LLM postback button -- driven by _keep_typing
    # ------------------------------------------------------------------

    async def _keep_typing(self, chat_id: str, *args, **kwargs) -> None:
        """Override the base loop to fire the postback button at threshold.

        We intentionally keep the base implementation behind us: it's
        responsible for the typing-indicator heartbeat, while *this*
        wrapper layers in the slow-LLM postback bubble at threshold.
        """
        if (
            self.slow_response_threshold <= 0
            or not self._client
            or not chat_id
        ):
            await super()._keep_typing(chat_id, *args, **kwargs)
            return

        async def _fire_postback() -> None:
            """Burn the reply token into a postback button after the threshold.

            Runs on its own task so the typing heartbeat is not blocked by
            the sleep. Guards are checked *after* waking rather than at
            schedule time, because the agent may have finished during the
            sleep.

            The ordering of the two guards is load-bearing: the reply
            token is consumed last, so a chat that already answered simply
            never reaches that point and keeps its token for a later
            reply. A chat that already has an outstanding button bails
            without touching the cache at all.
            """
            try:
                await asyncio.sleep(self.slow_response_threshold)
            except asyncio.CancelledError:
                raise
            # Only fire if we still have a usable reply token. If the agent
            # already responded, _consume_reply_token has cleared it.
            if chat_id not in self._reply_tokens:
                return
            if chat_id in self._pending_buttons:
                return
            rid = self._cache.register_pending(chat_id)
            self._pending_buttons[chat_id] = rid
            token, used = self._consume_reply_token(chat_id)
            if not used:
                self._pending_buttons.pop(chat_id, None)
                return
            msg = build_postback_button_message(
                self.pending_text, self.button_label, rid
            )
            try:
                await self._client.reply(token, [msg])
                logger.info("LINE: sent slow-LLM postback button for chat %s (rid=%s)", chat_id, rid)
            except Exception as exc:
                logger.warning("LINE: postback button send failed: %s", exc)
                self._pending_buttons.pop(chat_id, None)

        post_task = asyncio.create_task(_fire_postback())
        try:
            await super()._keep_typing(chat_id, *args, **kwargs)
        finally:
            if not post_task.done():
                post_task.cancel()
                try:
                    await post_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def interrupt_session_activity(self, session_key: str, chat_id: str) -> None:
        """Resolve any orphan PENDING postback so the button doesn't loop."""
        await super().interrupt_session_activity(session_key, chat_id)
        rid = self._pending_buttons.pop(chat_id, None)
        if rid:
            self._cache.set_error(rid, self.interrupted_text)

    # ------------------------------------------------------------------
    # Outbound media (image / voice / video)
    # ------------------------------------------------------------------

    def _register_media(self, file_path: str, *, cleanup: bool = False) -> str:
        """Register a local file for HTTPS serving; return the URL token."""
        # Evict expired tokens first.
        now = time.time()
        for token in list(self._media_tokens.keys()):
            path, exp = self._media_tokens[token]
            if now > exp:
                self._media_tokens.pop(token, None)
                if path in self._media_temp_paths:
                    self._media_temp_paths.discard(path)
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

        resolved = str(Path(file_path).resolve())
        token = secrets.token_urlsafe(32)
        self._media_tokens[token] = (resolved, now + self._media_ttl)
        if cleanup:
            self._media_temp_paths.add(resolved)
        return token

    def _media_url(self, token: str, filename: str) -> str:
        """Build the public HTTPS URL for a media token. PR #8398 style."""
        if self.public_base_url:
            base = self.public_base_url
        else:
            host = self.webhook_host
            port = self.webhook_port
            if port == 443:
                base = f"https://{host}"
            else:
                base = f"https://{host}:{port}"
        safe_name = _urlquote(filename, safe="")
        return f"{base}{DEFAULT_MEDIA_PATH_PREFIX}/{token}/{safe_name}"

    async def _handle_media(self, request) -> Any:
        """Serve a registered local file over HTTPS for LINE's media URLs.

        Defence-in-depth: even though ``_register_media`` is only called
        from trusted internal code, we recheck the resolved path against
        an allowed-roots set before serving. Sources allowed:
        ``tempfile.gettempdir()``, ``/tmp`` (which resolves to
        ``/private/tmp`` on macOS), and ``data dir``. PR #8398.
        """
        from aiohttp import web

        token = request.match_info["token"]
        entry = self._media_tokens.get(token)
        if not entry:
            return web.Response(status=404, text="not found")

        file_path, expires_at = entry
        if time.time() > expires_at:
            self._media_tokens.pop(token, None)
            return web.Response(status=410, text="gone")

        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return web.Response(status=404, text="not found")

        try:
            from encre.config import get_data_dir
            encre_home = Path(get_data_dir()).resolve()
        except Exception:
            encre_home = Path.home().joinpath("data dir").resolve()

        allowed_roots = {
            Path(tempfile.gettempdir()).resolve(),
            Path("/tmp").resolve(),  # -> /private/tmp on macOS
            encre_home,
        }
        resolved = path.resolve()
        if not any(_is_relative_to(resolved, r) for r in allowed_roots):
            logger.warning("LINE: refusing to serve outside allowed roots: %s", resolved)
            return web.Response(status=403, text="forbidden")

        content_type, _ = mimetypes.guess_type(str(path))
        return web.FileResponse(
            path,
            headers={"Content-Type": content_type or "application/octet-stream"},
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a local image, attached to the webhook's own HTTPS server.

        LINE has no binary upload endpoint, so the image is registered
        locally and referenced by URL. That URL must be publicly
        reachable, which is why a bind on ``0.0.0.0`` without
        ``LINE_PUBLIC_URL`` is rejected up front rather than failing
        later with an opaque LINE error.

        Args:
            chat_id: Target chat id.
            image_path: Local image file, at most
                ``LINE_IMAGE_MAX_BYTES``.
            caption: Optional text bubble appended after the image.
            metadata: Ignored; kept for base-class parity.

        Returns:
            :class:`SendResult`; failures are returned as ``success=False``
            with a human-readable error rather than raised.
        """
        path = Path(image_path)
        if not path.exists() or not path.is_file():
            return SendResult(success=False, error=f"image file not found: {image_path}")
        if path.stat().st_size > LINE_IMAGE_MAX_BYTES:
            return SendResult(success=False, error="image exceeds 10 MB LINE limit")
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")
        if not self.public_base_url and self.webhook_host == "0.0.0.0":
            return SendResult(
                success=False,
                error="LINE_PUBLIC_URL must be set to send images "
                "(LINE only accepts publicly reachable HTTPS URLs)",
            )

        token = self._register_media(str(path.resolve()))
        url = self._media_url(token, path.name)
        if not url.lower().startswith("https://"):
            return SendResult(success=False, error=f"LINE image URL must be HTTPS: {url}")
        msgs: List[Dict[str, Any]] = [_image_message(url)]
        if caption:
            msgs.append(_text_message(caption))
        return await self._send_messages(chat_id, msgs)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        duration_ms: int = 1000,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a local audio file as a LINE voice message.

        LINE has no distinct voice-note type, so audio travels through
        the same URL registration path as images, but with the larger
        ``LINE_AV_MAX_BYTES`` ceiling. ``duration_ms`` is only advisory
        metadata; LINE reads the real length from the file.

        Args:
            chat_id: Target chat id.
            audio_path: Local audio file, at most ``LINE_AV_MAX_BYTES``.
            duration_ms: Declared duration in milliseconds.
            metadata: Ignored; kept for base-class parity.

        Returns:
            :class:`SendResult`; failures return ``success=False``.
        """
        path = Path(audio_path)
        if not path.exists() or not path.is_file():
            return SendResult(success=False, error=f"audio file not found: {audio_path}")
        if path.stat().st_size > LINE_AV_MAX_BYTES:
            return SendResult(success=False, error="audio exceeds 200 MB LINE limit")
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")
        if not self.public_base_url and self.webhook_host == "0.0.0.0":
            return SendResult(
                success=False,
                error="LINE_PUBLIC_URL must be set to send audio",
            )

        token = self._register_media(str(path.resolve()))
        url = self._media_url(token, path.name)
        return await self._send_messages(chat_id, [_audio_message(url, duration_ms)])

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        preview_path: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a local video with a synthesized or supplied thumbnail.

        LINE mandates ``previewImageUrl`` for video, so this method must
        always produce a thumbnail. When the caller supplies none, a
        one-pixel PNG from the stdlib constants is written to a temp file
        and registered with ``cleanup=True`` so the webhook teardown
        unlinks it. Both the video and the preview are registered
        separately because each needs its own URL token.

        Args:
            chat_id: Target chat id.
            video_path: Local video file, at most ``LINE_AV_MAX_BYTES``.
            preview_path: Optional thumbnail; a fallback PNG is used
                when unset or nonexistent.
            metadata: Ignored; kept for base-class parity.

        Returns:
            :class:`SendResult`; validation failures return
            ``success=False``, while a temp-file write failure is raised
            so the caller can see the underlying error.
        """
        path = Path(video_path)
        if not path.exists() or not path.is_file():
            return SendResult(success=False, error=f"video file not found: {video_path}")
        if path.stat().st_size > LINE_AV_MAX_BYTES:
            return SendResult(success=False, error="video exceeds 200 MB LINE limit")
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")
        if not self.public_base_url and self.webhook_host == "0.0.0.0":
            return SendResult(
                success=False,
                error="LINE_PUBLIC_URL must be set to send video",
            )

        # LINE requires a previewImageUrl. Use one if supplied, otherwise
        # write a stdlib 1x1 PNG to /tmp and serve it. PR #8398.
        if preview_path and Path(preview_path).is_file():
            preview_token = self._register_media(str(Path(preview_path).resolve()))
            preview_filename = Path(preview_path).name
        else:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            try:
                tmp.write(_FALLBACK_PNG_PREVIEW)
                tmp.flush()
                tmp.close()
                preview_token = self._register_media(tmp.name, cleanup=True)
                preview_filename = "preview.png"
            except Exception:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise

        video_token = self._register_media(str(path.resolve()))
        video_url = self._media_url(video_token, path.name)
        preview_url = self._media_url(preview_token, preview_filename)
        return await self._send_messages(chat_id, [_video_message(video_url, preview_url)])

    async def _send_messages(
        self,
        chat_id: str,
        messages: List[Dict[str, Any]],
    ) -> SendResult:
        """Send already-built message objects, batched at 5/call."""
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")
        if not messages:
            return SendResult(success=True, message_id=None)

        first_batch = messages[:LINE_MAX_MESSAGES_PER_CALL]
        rest = messages[LINE_MAX_MESSAGES_PER_CALL:]

        # First batch: try reply token, fall back to push.
        token, used_reply = self._consume_reply_token(chat_id)
        if used_reply:
            try:
                await self._client.reply(token, first_batch)
            except Exception as exc:
                logger.info("LINE: reply token rejected (%s); falling back to push", exc)
                try:
                    await self._client.push(chat_id, first_batch)
                except Exception as exc2:
                    return SendResult(success=False, error=str(exc2))
        else:
            try:
                await self._client.push(chat_id, first_batch)
            except Exception as exc:
                return SendResult(success=False, error=str(exc))

        # Subsequent batches: always push (reply token is single-use).
        while rest:
            batch = rest[:LINE_MAX_MESSAGES_PER_CALL]
            rest = rest[LINE_MAX_MESSAGES_PER_CALL:]
            try:
                await self._client.push(chat_id, batch)
            except Exception as exc:
                logger.warning("LINE: push for follow-up batch failed: %s", exc)
                return SendResult(success=False, error=str(exc))

        return SendResult(success=True, message_id=None)


def _is_relative_to(child: Path, parent: Path) -> bool:
    """Backport for Path.is_relative_to (Python 3.9+) -- defensive against
    cwd-resolution differences across CI runners.

    Both sides are resolved before comparison, because a symlinked temp
    directory (notably ``/tmp`` on macOS) would otherwise compare equal
    as a string and unequal as a real path, or vice versa. The fallback
    branch exists only for interpreters without ``is_relative_to``.

    Args:
        child: Candidate path being tested.
        parent: Root it must live under.

    Returns:
        ``True`` when the resolved child is inside the resolved parent.
    """
    try:
        return child.resolve().is_relative_to(parent.resolve())
    except (AttributeError, ValueError):
        try:
            child.resolve().relative_to(parent.resolve())
            return True
        except ValueError:
            return False


# ---------------------------------------------------------------------------
# Plugin entry-point hooks
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Plugin gate: require credentials AND aiohttp at runtime."""
    if not os.getenv("LINE_CHANNEL_ACCESS_TOKEN"):
        return False
    if not os.getenv("LINE_CHANNEL_SECRET"):
        return False
    return True


def validate_config(config) -> bool:
    """Report whether credentials exist for LINE before instantiation.

    Mirrors the env-over-extra precedence used by the adapter
    constructor, so a config that would start successfully is reported as
    valid and one that would die at :meth:`LineAdapter.connect` is not.

    Args:
        config: PlatformConfig, or ``None``. ``extra`` is read via
            ``getattr`` so a missing attribute does not raise.

    Returns:
        ``True`` when both the channel access token and the channel
        secret are available.
    """
    extra = getattr(config, "extra", {}) or {}
    has_token = bool(
        os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or extra.get("channel_access_token")
    )
    has_secret = bool(
        os.getenv("LINE_CHANNEL_SECRET") or extra.get("channel_secret")
    )
    return has_token and has_secret


def is_connected(config) -> bool:
    """Surface in ``encre status`` even before the adapter is instantiated."""
    return validate_config(config)


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Auto-seed PlatformConfig.extra from env-only setups.

    Lets ``encre status`` reflect a LINE configuration that lives entirely
    in ``.env`` without a ``platforms.line`` block in ``config.yaml``.
    Mirrors the IRC plugin's pattern.
    """
    if not (os.getenv("LINE_CHANNEL_ACCESS_TOKEN") and os.getenv("LINE_CHANNEL_SECRET")):
        return None
    seeded: Dict[str, Any] = {}
    if os.getenv("LINE_PORT"):
        try:
            seeded["port"] = int(os.environ["LINE_PORT"])
        except ValueError:
            pass
    if os.getenv("LINE_HOST"):
        seeded["host"] = os.environ["LINE_HOST"]
    if os.getenv("LINE_PUBLIC_URL"):
        seeded["public_url"] = os.environ["LINE_PUBLIC_URL"]
    if os.getenv("LINE_HOME_CHANNEL"):
        seeded["home_channel"] = os.environ["LINE_HOME_CHANNEL"]
    return seeded or {}


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process push delivery for cron jobs running detached from the encre.gateway.

    Without this hook ``deliver=line`` cron jobs fail with ``no live adapter``
    when cron runs as its own process. We always Push (reply tokens require
    an inbound webhook event we don't have in this path).

    ``thread_id`` is accepted for signature parity but ignored -- LINE has
    no native thread primitive on the channel-side API. ``media_files``
    likewise: cron-side media delivery requires a publicly-reachable URL,
    which the standalone path can't construct without binding the webhook
    server, so we send a text reference instead.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    token = (
        os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        or extra.get("channel_access_token", "")
    )
    if not token or not chat_id:
        return {"error": "LINE standalone send: missing token or chat_id"}

    plain = strip_markdown_preserving_urls(message or "")
    chunks = split_for_line(plain) or [""]
    messages = [_text_message(c) for c in chunks][:LINE_MAX_MESSAGES_PER_CALL]
    if media_files:
        # Tack on a hint so the recipient knows media was generated but not delivered.
        messages.append(_text_message(f"[{len(media_files)} attachment(s) generated; not deliverable from cron]"))
        messages = messages[:LINE_MAX_MESSAGES_PER_CALL]

    client = _LineClient(token)
    try:
        await client.push(chat_id, messages)
        return {"success": True, "message_id": None}
    except Exception as exc:
        return {"error": str(exc)}


def interactive_setup() -> None:
    """Minimal stdin wizard for ``encre setup line``.

    Mirrors the irc/teams style: prompts for the two required vars, plus
    one optional public URL. Writes to ``data dir`` via ``encre.config``.
    """
    print()
    print("LINE Messaging API setup")
    print("------------------------")
    print("Create a Messaging API channel at https://developers.line.biz/console/")
    print("then copy the values below.")
    print()

    def _prompt(var: str, prompt: str, *, secret: bool = False) -> None:
        """Ask for one variable and persist it if the user typed something.

        Existing values are never overwritten by an empty entry, so the
        wizard is safe to re-run to fill in one missing field. Secrets go
        through a masked prompt so the token does not echo on the terminal.

        Args:
            var: Environment variable name to write.
            prompt: Label shown to the user.
            secret: Use masked input instead of ``input``.

        Returns:
            None. ``save_env_value`` is called only when a non-empty value
            was entered.

        Raises:
            Nothing; ``EOFError`` and ``KeyboardInterrupt`` abort the
            prompt silently rather than tearing down the wizard.
        """
        existing = get_env_value(var)
        suffix = " [keep current]" if existing else ""
        try:
            if secret:
                from encre.secret_prompt import masked_secret_prompt
                value = masked_secret_prompt(f"{prompt}{suffix}: ")
            else:
                value = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if value:
            save_env_value(var, value)

    _prompt("LINE_CHANNEL_ACCESS_TOKEN", "Channel access token", secret=True)
    _prompt("LINE_CHANNEL_SECRET", "Channel secret", secret=True)
    _prompt("LINE_PUBLIC_URL", "Public HTTPS base URL (optional, e.g. https://my-tunnel.example.com)")
    _prompt("LINE_ALLOWED_USERS", "Allowed user IDs (comma-separated; blank=skip)")
    print("Done. Set the webhook URL in the LINE console to "
          "<your-public-url>/line/webhook and enable 'Use webhook'.")

def _build_adapter(config):
    """Adapter factory handed to the platform registry.

    :class:`encre.gateway.run.GatewayRunner` calls this with the resolved
    LINE PlatformConfig when it starts the platform, so keeping the
    construction in one function is what makes the plugin discoverable
    without any core edits.

    Args:
        config: Resolved LINE PlatformConfig.

    Returns:
        A freshly constructed :class:`LineAdapter`, not yet connected.
    """
    return LineAdapter(config)
