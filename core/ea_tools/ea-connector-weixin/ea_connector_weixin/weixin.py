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
Weixin (WeChat) platform adapter for Encre's gateway layer.

Layer
-----
This module is one of the vendor adapters living in
:mod:`encre.gateway.platforms`. It implements the contract of
:class:`encre.gateway.platforms.base.BasePlatformAdapter` for personal WeChat
accounts authenticated through Tencent's iLink Bot API. :class:`encre.gateway.run.GatewayRunner`
constructs it from :class:`encre.gateway.config.PlatformConfig`, owns its
lifecycle, and hands it inbound traffic; the :class:`~encre.gateway.platforms.base.SendResult`
values produced by :meth:`WeixinAdapter.send` are consumed by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer` when relaying agent
output back to the platform.

Data flow
---------
Inbound: :meth:`WeixinAdapter._poll_loop` long-polls
``ilink/bot/getupdates`` -> ``_process_message`` authenticates and maps the wire
envelope onto :class:`~encre.gateway.platforms.base.MessageEvent` -> the base
class forwards the event into the agent session.

Outbound: ``GatewayStreamConsumer`` -> :meth:`WeixinAdapter.send` ->
``_split_text`` chunking -> ``_send_text_chunk`` -> ``ilink/bot/sendmessage``.
Media travels through ``ilink/bot/getuploadurl`` plus an AES-128-ECB encrypted
POST to the WeChat CDN.

iLink protocol constraints encoded here
---------------------------------------
- Every outbound message must echo the peer's latest ``context_token``. Those
  tokens are persisted per account by :class:`ContextTokenStore` so a restart
  does not lose conversational continuity.
- Media is never transported in plaintext: payloads are AES-128-ECB encrypted
  with PKCS#7 padding and referenced by an ``encrypted_query_param``.
- iLink returns ``ret``/``errcode`` of ``-2`` both for a genuine frequency
  limit and for a stale session carrying ``errmsg == "unknown error"``;
  :func:`_is_stale_session_ret` separates the two.

Public API
----------
:func:`check_weixin_requirements`, :func:`qr_login`,
:func:`save_weixin_account`, :func:`load_weixin_account`,
:func:`send_weixin_direct`, :class:`WeixinAdapter`,
:class:`ContextTokenStore`, :class:`TypingTicketCache`.
"""

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import struct
import tempfile
import textwrap
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

# Column width used when re-wrapping long assistant output before it reaches a
# WeChat client. WeChat message bubbles do not hard-wrap long tokens, so an
# unwrapped stack trace or URL renders as an unusably long single line.
WEIXIN_COPY_LINE_WIDTH = 120

import aiohttp
import qrcode
import qrcode.image.svg
# Optional dependency flag: ``aiohttp`` carries every network call in this
# module. It is probed at import time so that
# :func:`check_weixin_requirements` can fail fast and the platform registry can
# disable the adapter instead of raising ImportError mid-lifecycle.
AIOHTTP_AVAILABLE = True

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
# Same fail-fast pattern as ``aiohttp``; ``cryptography`` is only needed for the
# CDN media cipher, so its absence should not break unrelated platform loading.
CRYPTO_AVAILABLE = True

from encre.gateway.config import Platform, PlatformConfig
from encre.gateway.platforms.helpers import MessageDeduplicator
from encre.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from encre.config import get_data_dir
from encre.secure_io import read_json as _read_secure_json, write_json as _write_secure_json

def atomic_json_write(path, data):
    """Persist *data* as encrypted JSON at *path* via :func:`encre.secure_io.write_json`.

    iLink credentials and ``context_token`` values are routed through this
    single channel so no caller can accidentally write plaintext secrets to the
    account directory. ``write_json`` handles the temp-file-plus-rename dance,
    which keeps a crash mid-write from leaving a truncated credential file.
    """
    _write_secure_json(path, data)


# Thin environment lookup shim used by config fallbacks below.
get_secret = lambda key, default=None: os.environ.get(key, default)

# --- iLink service endpoints -------------------------------------------------

# Base hosts. ``ILINK_BASE_URL`` is the API gateway; ``WEIXIN_CDN_BASE_URL`` is
# the separate media CDN and is overridable because some iLink tenants serve
# media from a different edge.
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
# Packed into a single 32-bit integer as (major<<16)|(minor<<8)|patch and sent
# as the ``iLink-App-ClientVersion`` header. iLink gates features on this value,
# so bumping it here can change server-side behaviour.
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

# Poll hold is deliberately below the client-side timeout so the adapter always
# wakes up in time to honour ``_running`` on disconnect instead of blocking for
# the full server hold.
LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_TIMEOUT_MS = 35_000

# Poll-loop resilience tuning: short retries for transient faults, a long pause
# once the failure count saturates, and 300s of dedup tolerance so a slow poll
# loop does not reprocess a burst it has already seen.
MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2  # iLink frequency limit -- backoff and retry
MESSAGE_DEDUP_TTL_SECONDS = 300


def _is_stale_session_ret(
    ret: "Optional[int]", errcode: "Optional[int]", errmsg: "Optional[str]",
) -> bool:
    """Classify an iLink error pair as a stale session rather than a rate limit.

    iLink uses ``-2`` for two unrelated conditions. A genuine frequency limit
    carries a descriptive ``errmsg``, while a session that the server no longer
    recognises comes back as ``-2`` with ``errmsg == "unknown error"``. Treating
    both identically would open the rate-limit circuit breaker on every
    credential refresh, stalling outbound delivery for 30 seconds at a time.

    Args:
        ret: Top-level ``ret`` field of the response envelope.
        errcode: ``errcode`` field, which iLink mirrors alongside ``ret``.
        errmsg: Human-readable error text used as the discriminator.

    Returns:
        ``True`` only when the code is ``-2`` **and** the message is the exact
        stale-session sentinel, case-insensitively.
    """
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    return (errmsg or "").lower() == "unknown error"


# iLink media_type discriminants accepted by ``ilink/bot/getuploadurl``.
MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

# Token -> live adapter index. ``send_weixin_direct`` looks an adapter up here so
# a cron-triggered push can reuse an already-open connection and its
# ``context_token`` cache instead of opening a second iLink session, which the
# server rejects as a duplicate login.
_LIVE_ADAPTERS: Dict[str, Any] = {}


def _make_ssl_connector() -> Optional["aiohttp.TCPConnector"]:
    """Build a ``TCPConnector`` pinned to the certifi CA bundle, or ``None``.

    Tencent's ``ilinkai.weixin.qq.com`` certificate chain is not verifiable
    against some system stores, most notably Homebrew's OpenSSL on macOS Apple
    Silicon, which then fails the handshake for reasons entirely unrelated to
    the token. Using certifi's Mozilla bundle makes verification deterministic;
    when certifi is absent, ``None`` falls back to aiohttp's default, which
    honours ``SSL_CERT_FILE`` via ``trust_env=True``.

    Returns:
        A connector with an explicit CA file, or ``None`` to let aiohttp pick
        its own verification material.
    """
    import ssl
    import certifi
    if not AIOHTTP_AVAILABLE:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx)


# ``item_list`` element type discriminants used by both inbound extraction and
# outbound item construction.
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

# ``msg.message_type`` discriminants; ``MSG_TYPE_USER`` is what iLink tags
# inbound messages with and ``MSG_TYPE_BOT`` is required on outbound sends.
MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

# ``sendtyping`` status discriminants.
TYPING_START = 1
TYPING_STOP = 2

# Markdown structural recognisers. Fences are tracked explicitly so that line
# splitting and wrapping never break a code block across two messages, which
# WeChat would render as two syntactically invalid fragments.
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TABLE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_FENCE_RE = re.compile(r"^```([^\n`]*)\s*$")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def check_weixin_requirements() -> bool:
    """Report whether the Weixin adapter can start.

    Wired into the platform registry as the adapter's ``check_fn`` so the
    gateway skips Weixin entirely when either network or crypto runtime
    dependency is missing, instead of failing at first ``connect()``.

    Returns:
        ``True`` when both ``aiohttp`` and ``cryptography`` imported cleanly.
    """
    return AIOHTTP_AVAILABLE and CRYPTO_AVAILABLE


def _safe_id(value: Optional[str], keep: int = 8) -> str:
    """Truncate an identifier for safe log interpolation.

    iLink UINs and room ids are long, and logging them in full would make
    every line of this adapter wide and would print real user ids into shared
    logs. Truncation keeps the value identifiable for correlation while
    bounding line length.

    Args:
        value: The identifier, possibly ``None``.
        keep: Maximum number of characters retained.

    Returns:
        A display-safe prefix, or ``"?"`` when nothing usable was supplied.
    """
    raw = str(value or "").strip()
    if not raw:
        return "?"
    if len(raw) <= keep:
        return raw
    return raw[:keep]


def _json_dumps(payload: Dict[str, Any]) -> str:
    """Serialise a request body in the compact form iLink expects.

    ``ensure_ascii=False`` is required rather than cosmetic: iLink payloads can
    carry Chinese display text, and escaping it to ``\\uXXXX`` changes the
    server-side length calculations and can trip its size limits. Compact
    separators keep the body as small as possible for the same reason.

    Args:
        payload: The JSON-serialisable request body.

    Returns:
        The serialised request body without redundant whitespace.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    """Apply PKCS#7 padding to a multiple of *block_size*.

    AES operates on 16-byte blocks and iLink's CDN expects PKCS#7 padding even
    when the input already lands on a block boundary, in which case a full
    extra block of 0x10 is appended.

    Args:
        data: Plaintext to pad.
        block_size: Cipher block size, 16 for AES.

    Returns:
        Padded plaintext, always a positive multiple of ``block_size``.
    """
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt *plaintext* with AES-128 in ECB mode.

    ECB is mandated by the WeChat CDN protocol rather than chosen here: every
    block is encrypted independently with no chaining, which means identical
    plaintext blocks produce identical ciphertext blocks. That is a deliberate
    trade-off made by the protocol, so callers must not assume semantic
    security beyond what the server's own ``encrypted_query_param`` provides.

    Args:
        plaintext: Bytes to encrypt; padded internally.
        key: 16-byte AES key.

    Returns:
        Padded then encrypted ciphertext.
    """
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt AES-128 ECB ciphertext and strip PKCS#7 padding.

    Padding is only stripped when it is well-formed (length in range and every
    padding byte equal to that length). Anything else is returned untouched, so
    a malformed or non-padded payload degrades to raw bytes instead of raising
    and losing the whole download.

    Args:
        ciphertext: Ciphertext from the CDN.
        key: 16-byte AES key.

    Returns:
        Decrypted plaintext, with padding removed when it validates.
    """
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


def _aes_padded_size(size: int) -> int:
    """Return the ciphertext length for a plaintext of *size* bytes.

    ``getuploadurl`` requires the caller to declare ``filesize`` up front, and
    the server rejects the upload when it disagrees with the actual ciphertext
    length. Predicting the post-padding length locally avoids an upload round
    trip that would already have been rejected.

    Args:
        size: Plaintext byte count.

    Returns:
        The padded ciphertext length, always a multiple of 16.
    """
    return ((size + 1 + 15) // 16) * 16


def _random_wechat_uin() -> str:
    """Generate the random per-request ``X-WECHAT-UIN`` header value.

    iLink correlates requests by this header. Reusing one value across calls
    looks like a single device replaying traffic and invites throttling, so a
    fresh random 32-bit identifier is minted for every request and encoded as a
    base64 decimal string, matching what native WeChat clients send.

    Returns:
        A base64-encoded random UIN suitable for the header.
    """
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _base_info() -> Dict[str, Any]:
    """Return the ``base_info`` block merged into every iLink request.

    iLink reads ``channel_version`` to decide which server-side features a
    client may use. It is injected here rather than at each call site so the
    value cannot drift between endpoints.

    Returns:
        The envelope fragment merged into outgoing payloads.
    """
    return {"channel_version": CHANNEL_VERSION}


def _headers(token: Optional[str], body: str) -> Dict[str, str]:
    """Build the header set for one iLink request.

    Two properties matter here. First, ``Content-Length`` is computed from the
    already-serialised *body* rather than letting aiohttp infer it, so the body
    and headers can never disagree. Second, ``X-WECHAT-UIN`` is randomised per
    request, see :func:`_random_wechat_uin`.

    Args:
        token: Bearer token, or ``None`` for unauthenticated QR endpoints.
        body: The serialised request body, used for length calculation.

    Returns:
        The header mapping for :func:`_api_post`.
    """
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _account_dir(encre_home: str) -> Path:
    """Return the per-user Weixin credential directory, creating it as needed.

    Centralising directory creation here means every persistence helper in this
    module agrees on one location and no caller can write credentials to an
    adjacent path by accident.

    Args:
        encre_home: Root data directory, typically from :func:`encre.config.get_data_dir`.

    Returns:
        The ``weixin/accounts`` directory path.
    """
    path = Path(encre_home) / "weixin" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _account_file(encre_home: str, account_id: str) -> Path:
    """Return the credential file path for one account.

    Args:
        encre_home: Root data directory.
        account_id: iLink bot id that scopes the credential file.

    Returns:
        The JSON credential path for that account.
    """
    return _account_dir(encre_home) / f"{account_id}.json"


def save_weixin_account(
    encre_home: str,
    *,
    account_id: str,
    token: str,
    base_url: str,
    user_id: str = "",
) -> None:
    """Persist account credentials for reuse across restarts.

    Called by :func:`qr_login` and :meth:`WeixinAdapter.__init__` so a QR
    scan survives process restarts without rescan. The payload is written
    through :func:`atomic_json_write` and then best-effort restricted to the
    owner; ``chmod`` is wrapped because Windows does not support POSIX modes
    and the adapter must still work there.

    Args:
        encre_home: Root data directory.
        account_id: iLink bot id used as the file name.
        token: Bearer token minted at QR confirmation.
        base_url: iLink host the account was issued against.
        user_id: The human WeChat id behind the bot, when reported.
    """
    payload = {
        "token": token,
        "base_url": base_url,
        "user_id": user_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = _account_file(encre_home, account_id)
    atomic_json_write(path, payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_weixin_account(encre_home: str, account_id: str) -> Optional[Dict[str, Any]]:
    """Load persisted account credentials, or ``None`` when unavailable.

    Failures are swallowed deliberately: a missing or corrupted credential file
    should degrade to "not logged in" and let the operator run
    :func:`qr_login`, rather than raise during gateway startup and take every
    other platform down with it.

    Args:
        encre_home: Root data directory.
        account_id: iLink bot id to look up.

    Returns:
        The credential mapping, or ``None`` when absent or unreadable.
    """
    path = _account_file(encre_home, account_id)
    if not path.exists():
        return None
    try:
        return _read_secure_json(path)
    except Exception:
        return None


class ContextTokenStore:
    """Disk-backed ``context_token`` cache keyed by account plus peer.

    iLink requires every outbound message to echo the latest
    ``context_token`` observed for that peer. Tokens arrive with inbound
    messages and are single-use in the sense that a stale one triggers a
    session-expired error on send. Caching them per peer lets
    :meth:`WeixinAdapter.send` reply to a peer long after the original
    message was received, including from cron-initiated pushes with no
    preceding inbound traffic.

    Invariants:
        Every entry in the in-memory cache belongs to exactly one account
        prefix, so :meth:`_persist` can safely rewrite one account's file
        without disturbing another account's tokens.

    Lifecycle: construct -> :meth:`restore` on connect -> :meth:`set` on
    every inbound message -> :meth:`get` on every send. ``set`` writes
    through to disk on each call; there is no background flush.

    Attributes:
        _root: Account credential directory, created by :func:`_account_dir`.
        _cache: ``"<account>:<user>" -> token`` mapping held in memory.
    """

    def __init__(self, encre_home: str):
        """Initialize the store against the account credential directory.

        No tokens are loaded here. Call :meth:`restore` explicitly so the
        caller controls when disk I/O happens, which matters because
        :meth:`WeixinAdapter.connect` wants the load to occur after the
        sessions are ready.

        Args:
            encre_home: Root data directory from
                :func:`encre.config.get_data_dir`.
        """
        self._root = _account_dir(encre_home)
        self._cache: Dict[str, str] = {}

    def _path(self, account_id: str) -> Path:
        """Return the context-token file for one account.

        Kept separate from :func:`_account_file` so token state and credential
        state can be rotated independently.

        Args:
            account_id: iLink bot id scoping the file.

        Returns:
            The JSON token cache path.
        """
        return self._root / f"{account_id}.context-tokens.json"

    def _key(self, account_id: str, user_id: str) -> str:
        """Compose the composite cache key.

        Args:
            account_id: iLink bot id.
            user_id: Peer id the token applies to.

        Returns:
            The namespaced cache key.
        """
        return f"{account_id}:{user_id}"

    def restore(self, account_id: str) -> None:
        """Reload a persisted token cache after a restart.

        Corrupt files log and continue rather than raise, since a lost token
        only costs one degraded send, while a startup exception would prevent
        the whole platform from coming up. Entries with empty or non-string
        values are skipped so a partially written file self-heals.

        Args:
            account_id: iLink bot id whose tokens are restored.
        """
        path = self._path(account_id)
        if not path.exists():
            return
        try:
            data = _read_secure_json(path)
        except Exception as exc:
            logger.warning("weixin: failed to restore context tokens for %s: %s", _safe_id(account_id), exc)
            return
        restored = 0
        for user_id, token in data.items():
            if isinstance(token, str) and token:
                self._cache[self._key(account_id, user_id)] = token
                restored += 1
        if restored:
            logger.info("weixin: restored %d context token(s) for %s", restored, _safe_id(account_id))

    def get(self, account_id: str, user_id: str) -> Optional[str]:
        """Fetch the cached token for one peer.

        Args:
            account_id: iLink bot id.
            user_id: Peer id to look up.

        Returns:
            The stored token, or ``None`` when the peer was never seen or the
            entry was evicted after a session-expired error.
        """
        return self._cache.get(self._key(account_id, user_id))

    def set(self, account_id: str, user_id: str, token: str) -> None:
        """Record a fresh token and write it through to disk immediately.

        Write-through is intentional: a token is only useful on the next send,
        which may happen after a crash or restart, so buffering it in memory
        would defeat the purpose of the cache.

        Args:
            account_id: iLink bot id.
            user_id: Peer the token was received from.
            token: The fresh ``context_token`` value.
        """
        self._cache[self._key(account_id, user_id)] = token
        self._persist(account_id)

    def _persist(self, account_id: str) -> None:
        """Rewrite one account's token file, keyed by peer id only.

        Only entries with this account's prefix are emitted, so accounts can
        share one process without cross-contaminating each other's files.
        Persistence failure is logged and ignored: the in-memory cache still
        serves the current process, so the write is best-effort durability.

        Args:
            account_id: iLink bot id whose slice is persisted.
        """
        prefix = f"{account_id}:"
        payload = {
            key[len(prefix) :]: value
            for key, value in self._cache.items()
            if key.startswith(prefix)
        }
        try:
            atomic_json_write(self._path(account_id), payload)
        except Exception as exc:
            logger.warning("weixin: failed to persist context tokens for %s: %s", _safe_id(account_id), exc)


class TypingTicketCache:
    """Short-lived typing ticket cache populated from ``getconfig``.

    iLink gates the ``sendtyping`` endpoint behind a per-peer ticket minted by
    ``ilink/bot/getconfig``. Fetching it per call would add a round trip to
    every typing signal, so tickets are cached with a TTL close to the server's
    own expiry.

    Invariant:
        Entries are evicted lazily on :meth:`get`, so the cache never returns a
        ticket the server would reject as expired.

    Attributes:
        _ttl_seconds: Entry lifetime; defaults to the server-side window.
        _cache: ``user_id -> (ticket, created_monotonic)`` mapping.
    """

    def __init__(self, ttl_seconds: float = 600.0):
        """Create an empty typing ticket cache.

        Args:
            ttl_seconds: Lifetime of a cached ticket. Defaults to the
                server-side window iLink advertises for typing tickets.
        """
        self._ttl_seconds = ttl_seconds
        self._cache: Dict[str, Tuple[str, float]] = {}

    def get(self, user_id: str) -> Optional[str]:
        """Return a still-valid ticket for a peer.

        Expired entries are removed on read rather than by a background timer,
        keeping the cache allocation-free while idle.

        Args:
            user_id: Peer the ticket was issued for.

        Returns:
            The ticket, or ``None`` when absent or expired.
        """
        entry = self._cache.get(user_id)
        if not entry:
            return None
        if time.time() - entry[1] >= self._ttl_seconds:
            self._cache.pop(user_id, None)
            return None
        return entry[0]

    def set(self, user_id: str, ticket: str) -> None:
        """Cache a freshly minted ticket.

        Args:
            user_id: Peer the ticket applies to.
            ticket: Ticket value returned by ``getconfig``.
        """
        self._cache[user_id] = (ticket, time.time())


def _cdn_download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    """Build a CDN download URL from an ``encrypted_query_param``.

    The parameter must be fully percent-encoded because it is itself a signed
    base64-ish token that may contain characters invalid in a query string.

    Args:
        cdn_base_url: CDN root, trailing slash tolerated.
        encrypted_query_param: Signed download token from the iLink item.

    Returns:
        The absolute download URL.
    """
    return f"{cdn_base_url.rstrip('/')}/download?encrypted_query_param={quote(encrypted_query_param, safe='')}"


def _cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    """Build a CDN upload URL when the API returns only ``upload_param``.

    ``getuploadurl`` prefers to hand back a ready ``upload_full_url``; when it
    does not, this reconstructs the equivalent endpoint so the caller has one
    code path regardless of which form the server chose.

    Args:
        cdn_base_url: CDN root.
        upload_param: Signed upload credential.
        filekey: Client-chosen opaque file key.

    Returns:
        The absolute upload URL.
    """
    return (
        f"{cdn_base_url.rstrip('/')}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


def _parse_aes_key(aes_key_b64: str) -> bytes:
    """Decode an iLink ``aes_key`` field into 16 raw key bytes.

    The protocol is inconsistent about encoding: image items embed the key as a
    hex string inside ``image_item.aeskey``, while ``media.aes_key`` carries a
    base64 blob that is either the raw 16 bytes or a 32-character hex string.
    Both shapes are accepted so a change in server encoding does not break media
    download.

    Args:
        aes_key_b64: Base64-encoded key material from the message item.

    Returns:
        The 16-byte AES key.

    Raises:
        ValueError: When the decoded length is neither 16 nor a 32-char hex
            string, meaning the key is malformed rather than merely re-encoded.
    """
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        text = decoded.decode("ascii", errors="ignore")
        if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)
    raise ValueError(f"unexpected aes_key format ({len(decoded)} decoded bytes)")


def _guess_chat_type(message: Dict[str, Any], account_id: str) -> Tuple[str, str]:
    """Decide whether an inbound message is a group or a direct message.

    iLink marks group traffic with a room id, but some tenants omit it. As a
    fallback, a message whose ``to_user_id`` differs from our own bot id while
    carrying plain text is treated as group traffic, since a direct message to
    the bot would echo the bot's own id.

    Args:
        message: Raw inbound iLink message envelope.
        account_id: Our own iLink bot id, used for the fallback comparison.

    Returns:
        A ``(chat_type, chat_id)`` pair, where ``chat_type`` is ``"group"`` or
        ``"dm"`` and ``chat_id`` is the room id for groups or the sender id for
        direct messages.
    """
    room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
    to_user_id = str(message.get("to_user_id") or "").strip()
    is_group = bool(room_id) or (to_user_id and account_id and to_user_id != account_id and message.get("msg_type") == 1)
    if is_group:
        return "group", room_id or to_user_id or str(message.get("from_user_id") or "")
    return "dm", str(message.get("from_user_id") or "")


async def _api_post(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    token: Optional[str],
    timeout_ms: int,
) -> Dict[str, Any]:
    """POST a JSON body to one iLink endpoint and return the parsed envelope.

    Timeout is enforced with :func:`asyncio.wait_for` rather than aiohttp's
    ``ClientTimeout`` because these calls are sometimes awaited from another
    thread via ``asyncio.run_coroutine_threadsafe()`` (cron pushes); aiohttp's
    client timeout context then raises
    "Timeout context manager should be used inside a task". Bounding the call
    with ``wait_for`` works identically from either entry point.

    Args:
        session: The caller-owned ``ClientSession``.
        base_url: iLink host for this account.
        endpoint: Relative endpoint from the ``EP_*`` constants.
        payload: Request body, merged with ``base_info``.
        token: Bearer token, or ``None`` for unauthenticated calls.
        timeout_ms: Timeout in milliseconds.

    Returns:
        The parsed JSON response envelope, including error codes for the caller
        to inspect.

    Raises:
        RuntimeError: On a non-2xx HTTP status, with a truncated body excerpt.
        asyncio.TimeoutError: When the call exceeds *timeout_ms*.
    """
    body = _json_dumps({**payload, "base_info": _base_info()})
    url = f"{base_url.rstrip('/')}/{endpoint}"
    # Use asyncio.wait_for() instead of aiohttp ClientTimeout to avoid
    # "Timeout context manager should be used inside a task" errors when
    # invoked via asyncio.run_coroutine_threadsafe() from cron jobs.
    async def _do() -> Dict[str, Any]:
        """Issue the POST and return the decoded envelope.

        Isolated into a closure so :func:`asyncio.wait_for` bounds the whole
        request-plus-decode exchange with one deadline, instead of racing the
        body read against the connect timeout.
        """
        async with session.post(url, data=body, headers=_headers(token, body)) as response:
            raw = await response.text()
            if not response.ok:
                raise RuntimeError(f"iLink POST {endpoint} HTTP {response.status}: {raw[:200]}")
            return json.loads(raw)
    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


async def _api_get(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    endpoint: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    """GET one iLink endpoint and return the parsed envelope.

    Used only by the unauthenticated QR flow, which needs no bearer token and
    therefore omits the auth headers entirely. Timeout handling mirrors
    :func:`_api_post` for the same threading reason.

    Args:
        session: The caller-owned ``ClientSession``.
        base_url: iLink host.
        endpoint: Relative endpoint, optionally with a query string already
            appended by the caller.
        timeout_ms: Timeout in milliseconds.

    Returns:
        The parsed JSON response envelope.

    Raises:
        RuntimeError: On a non-2xx HTTP status.
        asyncio.TimeoutError: When the call exceeds *timeout_ms*.
    """
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    # Use asyncio.wait_for() instead of aiohttp ClientTimeout to avoid
    # "Timeout context manager should be used inside a task" errors when
    # invoked via asyncio.run_coroutine_threadsafe() from cron jobs.
    async def _do() -> Dict[str, Any]:
        """Issue the GET and return the decoded envelope.

        Same closure-and-deadline rationale as the POST variant.
        """
        async with session.get(url, headers=headers) as response:
            raw = await response.text()
            if not response.ok:
                raise RuntimeError(f"iLink GET {endpoint} HTTP {response.status}: {raw[:200]}")
            return json.loads(raw)
    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


async def _get_updates(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    sync_buf: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    """Long-poll for new messages, resuming from *sync_buf*.

    ``get_updates_buf`` is the server's cursor. Passing it back unchanged means
    the server only returns messages received after the previous poll, which is
    what makes this a push-style loop on top of a pull API.

    A timeout is not an error here: it means "no new messages within the hold",
    so the cursor is echoed back unchanged and the caller continues immediately
    rather than sleeping.

    Args:
        session: The poll ``ClientSession``.
        base_url: iLink host.
        token: Bearer token.
        sync_buf: Cursor from the previous successful poll.
        timeout_ms: Hold time in milliseconds.

    Returns:
        The response envelope, or a synthetic empty envelope on timeout.
    """
    try:
        return await _api_post(
            session,
            base_url=base_url,
            endpoint=EP_GET_UPDATES,
            payload={"get_updates_buf": sync_buf},
            token=token,
            timeout_ms=timeout_ms,
        )
    except asyncio.TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def _send_message(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    to: str,
    text: str,
    context_token: Optional[str],
    client_id: str,
) -> Dict[str, Any]:
    """Send a text message via the iLink ``sendmessage`` endpoint.

    ``message_type`` must be ``MSG_TYPE_BOT`` and ``message_state`` must be
    ``MSG_STATE_FINISH``; partial states are reserved for server-side streaming
    that this adapter does not implement. ``from_user_id`` is deliberately left
    empty because the server derives the sender from the bearer token.

    Args:
        session: The send ``ClientSession``.
        base_url: iLink host.
        token: Bearer token.
        to: Destination peer id.
        text: Message body, guaranteed non-empty by the caller.
        context_token: Peer token to echo, or ``None`` to send tokenless.
        client_id: Client-generated idempotency id.

    Returns:
        The raw response envelope, which callers inspect for ``ret`` and
        ``errcode`` because iLink reports failures in-band rather than over HTTP.

    Raises:
        ValueError: When *text* is empty or whitespace-only.
    """
    if not text or not text.strip():
        raise ValueError("_send_message: text must not be empty")
    message: Dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        message["context_token"] = context_token
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MESSAGE,
        payload={"msg": message},
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


async def _send_typing(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    typing_ticket: str,
    status: int,
) -> None:
    """Emit a typing indicator for a peer.

    Requires a ticket from :func:`_get_config`; iLink ignores typing requests
    without one. The call is fire-and-forget from the caller's perspective and
    therefore uses the shorter config timeout rather than the message timeout.

    Args:
        session: The send ``ClientSession``.
        base_url: iLink host.
        token: Bearer token.
        to_user_id: Peer to signal.
        typing_ticket: Ticket minted by ``getconfig``.
        status: ``TYPING_START`` or ``TYPING_STOP``.
    """
    await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_TYPING,
        payload={
            "ilink_user_id": to_user_id,
            "typing_ticket": typing_ticket,
            "status": status,
        },
        token=token,
        timeout_ms=CONFIG_TIMEOUT_MS,
    )


async def _get_config(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    user_id: str,
    context_token: Optional[str],
) -> Dict[str, Any]:
    """Fetch per-peer config, which is where typing tickets are minted.

    Args:
        session: The caller-owned ``ClientSession``.
        base_url: iLink host.
        token: Bearer token.
        user_id: Peer to fetch config for.
        context_token: Peer token when one is known; echoing it improves the
            chance the server issues a valid ticket.

    Returns:
        The response envelope, from which the caller reads ``typing_ticket``.
    """
    payload: Dict[str, Any] = {"ilink_user_id": user_id}
    if context_token:
        payload["context_token"] = context_token
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_GET_CONFIG,
        payload=payload,
        token=token,
        timeout_ms=CONFIG_TIMEOUT_MS,
    )


async def _get_upload_url(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    media_type: int,
    filekey: str,
    rawsize: int,
    rawfilemd5: str,
    filesize: int,
    aeskey_hex: str,
) -> Dict[str, Any]:
    """Request a pre-signed CDN upload slot for one media object.

    The server needs the plaintext digest and size up front so it can verify
    the ciphertext it receives is the expected payload. ``filesize`` must be
    the *post-padding* length, computed by :func:`_aes_padded_size`, not the
    plaintext size, or the subsequent upload is rejected as a length mismatch.

    Args:
        session: The send ``ClientSession``.
        base_url: iLink host.
        token: Bearer token.
        to_user_id: Destination peer.
        media_type: One of the ``MEDIA_*`` discriminants.
        filekey: Opaque client-chosen key.
        rawsize: Plaintext byte count.
        rawfilemd5: MD5 of the plaintext.
        filesize: Padded ciphertext byte count.
        aeskey_hex: Hex representation of the freshly generated AES key.

    Returns:
        The response envelope containing ``upload_param`` and/or
        ``upload_full_url``.
    """
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_GET_UPLOAD_URL,
        payload={
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aeskey_hex,
        },
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


async def _upload_ciphertext(
    session: "aiohttp.ClientSession",
    *,
    ciphertext: bytes,
    upload_url: str,
) -> str:
    """POST encrypted media to the CDN and return the download reference.

    Both possible URL shapes (a constructed CDN endpoint or a direct
    ``upload_full_url``) are consumed the same way: a POST with the raw
    ciphertext as the body. The CDN answers with the
    ``x-encrypted-param`` response header, which is the token that later gets
    embedded in the message item, so a 200 without that header is a protocol
    failure rather than a success.

    Args:
        session: The send ``ClientSession``.
        ciphertext: AES-128-ECB ciphertext to upload.
        upload_url: Either a constructed CDN URL or the server-provided direct
            URL.

    Returns:
        The ``x-encrypted-param`` value from the CDN response.

    Raises:
        RuntimeError: On a non-200 status or when the reference header is absent.
        asyncio.TimeoutError: After 120 seconds, generous because media uploads
            can be large.
    """
    # Use asyncio.wait_for() instead of aiohttp ClientTimeout to avoid
    # "Timeout context manager should be used inside a task" errors when
    # invoked via asyncio.run_coroutine_threadsafe() from cron jobs.
    async def _do_upload() -> str:
        """POST the ciphertext and return the CDN download reference.

        The ``x-encrypted-param`` header is extracted before the body is
        drained so a header-only success is not mistaken for a body error, and
        the body is still fully read to keep the connection reusable.
        """
        async with session.post(upload_url, data=ciphertext, headers={"Content-Type": "application/octet-stream"}) as response:
            if response.status == 200:
                encrypted_param = response.headers.get("x-encrypted-param")
                if encrypted_param:
                    await response.read()
                    return encrypted_param
                raw = await response.text()
                raise RuntimeError(f"CDN upload missing x-encrypted-param header: {raw[:200]}")
            raw = await response.text()
            raise RuntimeError(f"CDN upload HTTP {response.status}: {raw[:200]}")
    return await asyncio.wait_for(_do_upload(), timeout=120)


async def _download_bytes(
    session: "aiohttp.ClientSession",
    *,
    url: str,
    timeout_seconds: float = 60.0,
) -> bytes:
    """Fetch raw bytes from a URL without content-type negotiation.

    Timeout is enforced with :func:`asyncio.wait_for` for the same threading
    reason as the iLink calls.

    Args:
        session: The caller-owned ``ClientSession``.
        url: Fully qualified media URL.
        timeout_seconds: Per-call deadline in seconds.

    Returns:
        The downloaded bytes.

    Raises:
        aiohttp.ClientResponseError: On a non-2xx HTTP status.
        asyncio.TimeoutError: When the download exceeds the deadline.
    """
    # Use asyncio.wait_for() instead of aiohttp ClientTimeout to avoid
    # "Timeout context manager should be used inside a task" errors.
    async def _do_download() -> bytes:
        """Fetch the URL and return the full body.

        ``raise_for_status`` is used here rather than a manual status check so
        the resulting :class:`aiohttp.ClientResponseError` carries the URL and
        status that the media-download callers log.
        """
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.read()
    return await asyncio.wait_for(_do_download(), timeout=timeout_seconds)


# Hostnames WeChat media may legitimately be served from. Anything outside this
# set is refused, which is what makes the adapter safe to point at user-supplied
# media URLs: the CDN token is server-controlled, but ``full_url`` fields come
# from message payloads and are therefore attacker-influenceable.
_WEIXIN_CDN_ALLOWLIST: frozenset[str] = frozenset(
    {
        "novac2c.cdn.weixin.qq.com",
        "ilinkai.weixin.qq.com",
        "wx.qlogo.cn",
        "thirdwx.qlogo.cn",
        "res.wx.qq.com",
        "mmbiz.qpic.cn",
        "mmbiz.qlogo.cn",
    }
)


def _assert_weixin_cdn_url(url: str) -> None:
    """Refuse to fetch from a host outside the WeChat CDN allowlist.

    Only ``full_url`` fetches need this guard, since the constructed
    ``encrypted_query_param`` path is derived from the configured CDN base.
    The check is applied before any network I/O so an SSRF attempt never opens
    a socket.

    Args:
        url: Candidate media URL to validate.

    Raises:
        ValueError: When the URL is unparseable, uses a non-http(s) scheme, or
            names a host outside :data:`_WEIXIN_CDN_ALLOWLIST`.
    """
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Unparseable media URL: {url!r}") from exc

    if scheme not in {"http", "https"}:
        raise ValueError(
            f"Media URL has disallowed scheme {scheme!r}; only http/https are permitted."
        )
    if host not in _WEIXIN_CDN_ALLOWLIST:
        raise ValueError(
            f"Media URL host {host!r} is not in the WeChat CDN allowlist. "
            "Refusing to fetch to prevent SSRF."
        )


def _media_reference(item: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Safely reach the nested ``media`` sub-object of a message item.

    Items are shaped inconsistently across media types and some fields are
    absent on older payloads, so the chain is flattened with fallbacks instead
    of raising on a missing key.

    Args:
        item: Raw message item.
        key: Item sub-key such as ``"image_item"``.

    Returns:
        The ``media`` mapping, or an empty dict when absent.
    """
    return (item.get(key) or {}).get("media") or {}


async def _download_and_decrypt_media(
    session: "aiohttp.ClientSession",
    *,
    cdn_base_url: str,
    encrypted_query_param: Optional[str],
    aes_key_b64: Optional[str],
    full_url: Optional[str],
    timeout_seconds: float,
) -> bytes:
    """Fetch and decrypt one inbound media object.

    Two delivery forms exist and both are supported: the preferred CDN path
    built from ``encrypted_query_param``, and the legacy ``full_url`` form that
    requires the SSRF allowlist check first. Decryption only runs when a key is
    present, so unencrypted legacy payloads pass through untouched.

    Args:
        session: The poll ``ClientSession``, reused because this runs on the
            inbound path.
        cdn_base_url: CDN root used to build the preferred URL.
        encrypted_query_param: Signed CDN download token.
        aes_key_b64: Base64 key material, or ``None`` for plaintext payloads.
        full_url: Legacy direct URL, validated before fetch.
        timeout_seconds: Download deadline.

    Returns:
        Decrypted media bytes.

    Raises:
        RuntimeError: When neither download form is available.
        ValueError: When the key is malformed or the URL is not allowlisted.
    """
    if encrypted_query_param:
        raw = await _download_bytes(
            session,
            url=_cdn_download_url(cdn_base_url, encrypted_query_param),
            timeout_seconds=timeout_seconds,
        )
    elif full_url:
        _assert_weixin_cdn_url(full_url)
        raw = await _download_bytes(session, url=full_url, timeout_seconds=timeout_seconds)
    else:
        raise RuntimeError("media item had neither encrypt_query_param nor full_url")
    if aes_key_b64:
        raw = _aes128_ecb_decrypt(raw, _parse_aes_key(aes_key_b64))
    return raw


def _mime_from_filename(filename: str) -> str:
    """Guess a MIME type from a filename, defaulting to opaque binary.

    Args:
        filename: Uploaded or received file name.

    Returns:
        The guessed MIME type, or ``application/octet-stream`` when unknown.
    """
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _split_table_row(line: str) -> List[str]:
    """Split a pipe-delimited table row into stripped cells.

    Leading and trailing pipes are optional in Markdown tables, so both are
    removed when present before splitting.

    Args:
        line: One table row.

    Returns:
        The list of cell contents.
    """
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def _normalize_markdown_blocks(content: str) -> str:
    """Collapse blank-line runs so WeChat bubbles render compactly.

    WeChat renders a blank line as a full bubble gap, so a document with
    several consecutive blanks arrives looking ragged. Runs are reduced to at
    most one blank line while fenced code blocks are passed through untouched,
    since blank lines inside a fence are meaningful to the syntax highlighter.

    Args:
        content: Assistant output to normalise.

    Returns:
        The normalised text, trailing whitespace removed.
    """
    lines = content.splitlines()
    result: List[str] = []
    in_code_block = False
    blank_run = 0

    for raw_line in lines:
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            in_code_block = not in_code_block
            result.append(line)
            blank_run = 0
            continue

        if in_code_block:
            result.append(line)
            continue

        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                result.append("")
            continue

        blank_run = 0
        result.append(line)

    return "\n".join(result).strip()


def _wrap_copy_friendly_lines_for_weixin(content: str) -> str:
    """Wrap long display lines that are hard to copy in WeChat clients.

    WeChat does not hard-wrap long tokens, so a stack trace or a long URL
    becomes a single unbreakable line that the recipient must scroll sideways
    to copy. Lines are wrapped at :data:`WEIXIN_COPY_LINE_WIDTH` while fenced
    code, table rows, table alignment rules, and already-short lines are left
    alone, since wrapping those would corrupt their structure.

    Args:
        content: Assistant output to wrap.

    Returns:
        Wrapped text preserving structural lines verbatim.
    """
    if not content:
        return content

    wrapped: List[str] = []
    in_code_block = False

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if _FENCE_RE.match(stripped):
            in_code_block = not in_code_block
            wrapped.append(line)
            continue

        if (
            in_code_block
            or len(line) <= WEIXIN_COPY_LINE_WIDTH
            or not stripped
            or stripped.startswith("|")
            or _TABLE_RULE_RE.match(stripped)
        ):
            wrapped.append(line)
            continue

        wrapped_lines = textwrap.wrap(
            line,
            width=WEIXIN_COPY_LINE_WIDTH,
            break_long_words=False,
            break_on_hyphens=False,
            replace_whitespace=False,
            drop_whitespace=True,
        )
        wrapped.extend(wrapped_lines or [line])

    return "\n".join(wrapped).strip()


def _split_markdown_blocks(content: str) -> List[str]:
    """Split formatted content into blocks separated by blank lines.

    Fence boundaries force a block boundary even without a blank line, so a
    code block can never be merged with the prose around it. This is the
    lowest-level splitter and is reused by both the delivery splitter and the
    length packer.

    Args:
        content: Formatted assistant output.

    Returns:
        The list of non-empty blocks.
    """
    if not content:
        return []

    blocks: List[str] = []
    lines = content.splitlines()
    current: List[str] = []
    in_code_block = False

    for raw_line in lines:
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            if not in_code_block and current:
                blocks.append("\n".join(current).strip())
                current = []
            current.append(line)
            in_code_block = not in_code_block
            if not in_code_block:
                blocks.append("\n".join(current).strip())
                current = []
            continue

        if in_code_block:
            current.append(line)
            continue

        if not line.strip():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)

    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def _split_delivery_units_for_weixin(content: str) -> List[str]:
    """Split formatted content into chat-friendly delivery units.

    Weixin can render Markdown, but chat readability is better when top-level
    line breaks become separate messages. Keep fenced code blocks intact and
    attach indented continuation lines to the previous top-level line so nested
    list items do not get torn apart.

    Args:
        content: Formatted assistant output.

    Returns:
        The list of delivery units, each a self-contained bubble.
    """
    units: List[str] = []

    for block in _split_markdown_blocks(content):
        if _FENCE_RE.match(block.splitlines()[0].strip()):
            units.append(block)
            continue

        current: List[str] = []
        for raw_line in block.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                if current:
                    units.append("\n".join(current).strip())
                    current = []
                continue

            is_continuation = bool(current) and raw_line.startswith((" ", "\t"))
            if is_continuation:
                current.append(line)
                continue

            if current:
                units.append("\n".join(current).strip())
            current = [line]

        if current:
            units.append("\n".join(current).strip())

    return [unit for unit in units if unit]


def _looks_like_chatty_line_for_weixin(line: str) -> bool:
    """Return True when a line looks like a standalone chat utterance.

    This heuristic decides whether splitting a short block into separate
    bubbles improves readability or destroys it. Structural markers, numbered
    and bulleted list entries, bold-only lines, tables, and anything over 48
    characters are all treated as document content rather than chat.

    Args:
        line: One candidate line.

    Returns:
        ``True`` when the line reads as a plain conversational sentence.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if len(stripped) > 48:
        return False
    if line.startswith((" ", "\t")):
        return False
    if stripped.startswith((">", "-", "*", "【", "#", "|")):
        return False
    if _TABLE_RULE_RE.match(stripped):
        return False
    if re.match(r"^\*\*[^*]+\*\*$", stripped):
        return False
    if re.match(r"^\d+\.\s", stripped):
        return False
    return True


def _looks_like_heading_line_for_weixin(line: str) -> bool:
    """Return True when a short line behaves like a heading.

    A heading followed by its body should stay in one bubble, otherwise the
    recipient sees a title bubble with no content beneath it.

    Args:
        line: One candidate line.

    Returns:
        ``True`` for ATX headings and short colon-terminated labels.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if _HEADER_RE.match(stripped):
        return True
    return len(stripped) <= 24 and stripped.endswith((":", "："))


def _should_split_short_chat_block_for_weixin(block: str) -> bool:
    """Split only chat-like multiline blocks into separate bubbles.

    Restricts bubble splitting to short blocks of 2-6 lines where every line
    looks conversational and the first line is not a heading. This prevents
    prose from being shredded into a stream of single-line bubbles.

    Args:
        block: One Markdown block to evaluate.

    Returns:
        ``True`` when splitting improves chat readability.
    """
    lines = [line for line in block.splitlines() if line.strip()]
    if not 2 <= len(lines) <= 6:
        return False
    if _looks_like_heading_line_for_weixin(lines[0]):
        return False
    return all(_looks_like_chatty_line_for_weixin(line) for line in lines)


def _pack_markdown_blocks_for_weixin(content: str, max_length: int) -> List[str]:
    """Pack blocks greedily into messages that fit the platform limit.

    Blocks are joined with a blank line, which keeps the Markdown boundary
    meaningful inside a single bubble. A single block longer than the limit is
    truncated via the base adapter rather than dropped, so the recipient still
    gets the beginning of it.

    Args:
        content: Full assistant output.
        max_length: Per-message character budget.

    Returns:
        The packed messages, each within the budget.
    """
    if len(content) <= max_length:
        return [content]

    packed: List[str] = []
    current = ""
    for block in _split_markdown_blocks(content):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            packed.append(current)
            current = ""
        if len(block) <= max_length:
            current = block
            continue
        packed.extend(BasePlatformAdapter.truncate_message(block, max_length))
    if current:
        packed.append(current)
    return packed


def _split_text_for_weixin_delivery(
    content: str, max_length: int, split_per_line: bool = False,
) -> List[str]:
    """Split content into sequential Weixin messages.

    *compact* (default): Keep everything in a single message whenever it fits
    within the platform limit, even when the author used explicit line breaks.
    Only fall back to block-aware packing when the payload exceeds
    ``max_length``.

    *per_line* (``split_per_line=True``): top-level line breaks become
    separate chat messages; oversized units still use block-aware packing.

    The active mode is controlled via ``config.yaml`` ->
    ``platforms.weixin.extra.split_multiline_messages`` (``true`` / ``false``)
    or the env var ``WEIXIN_SPLIT_MULTILINE_MESSAGES``.

    Args:
        content: Assistant output to deliver.
        max_length: Per-message character budget.
        split_per_line: Select the line-granular splitting mode.

    Returns:
        The ordered messages to send, or an empty list for empty content.
    """
    if not content:
        return []
    if split_per_line:
        # One message per top-level delivery unit.
        if len(content) <= max_length and "\n" not in content:
            return [content]
        chunks: List[str] = []
        for unit in _split_delivery_units_for_weixin(content):
            if len(unit) <= max_length:
                chunks.append(unit)
                continue
            chunks.extend(_pack_markdown_blocks_for_weixin(unit, max_length))
        return [c for c in chunks if c] or [content]

    # Compact (default): single message when under the limit, unless the
    # content looks like a short chatty exchange, in which case split into
    # separate bubbles for a more natural chat feel.
    if len(content) <= max_length:
        return (
            [u for u in _split_delivery_units_for_weixin(content) if u]
            if _should_split_short_chat_block_for_weixin(content)
            else [content]
        )
    return _pack_markdown_blocks_for_weixin(content, max_length) or [content]


def _coerce_bool(value: Any, default: bool = True) -> bool:
    """Coerce a config value to bool, tolerating strings like ``"true"``.

    ``config.yaml`` and environment variables both arrive as strings, so a
    plain ``bool()`` would treat ``"false"`` as truthy. Unrecognised values
    fall back to *default* rather than raising, keeping a typo in config from
    preventing the platform from starting.

    Args:
        value: Raw config value of any type.
        default: Value used when the input is missing or unrecognised.

    Returns:
        The coerced boolean.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _extract_text(item_list: List[Dict[str, Any]]) -> str:
    """Flatten an ``item_list`` into a single plain-text representation.

    Handles three shapes: a bare text item, a text item quoting media (which is
    annotated so the agent knows it is replying to an attachment it cannot see
    as bytes here), and a text item quoting another message (annotated with the
    quoted content, resolved recursively so nested quotes flatten out). When no
    text item exists, a voice item's transcription is used instead.

    Args:
        item_list: Raw ``item_list`` from an inbound iLink message.

    Returns:
        The extracted text, or an empty string when nothing readable exists.
    """
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            ref_type = ref_item.get("type")
            if ref_type in {ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE}:
                title = ref.get("title") or ""
                prefix = f"[Referenced media: {title}]\n" if title else "[Referenced media]\n"
                return f"{prefix}{text}".strip()
            if ref_item:
                parts: List[str] = []
                if ref.get("title"):
                    parts.append(str(ref["title"]))
                ref_text = _extract_text([ref_item])
                if ref_text:
                    parts.append(ref_text)
                if parts:
                    return f"[Reply: {' | '.join(parts)}]\n{text}".strip()
            return text
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text:
                return voice_text
    return ""


def _message_type_from_media(media_types: List[str], text: str) -> MessageType:
    """Map extracted media MIME prefixes onto the shared event model.

    Images and video outrank other media because a screenshot with a caption
    should reach the agent as a photo, not a document. A leading ``/`` marks a
    command, which the session layer treats differently from free text.

    Args:
        media_types: MIME prefixes collected during media download.
        text: Extracted text, used for the command and text fallbacks.

    Returns:
        The :class:`encre.gateway.platforms.base.MessageType` for the event.
    """
    if any(m.startswith("image/") for m in media_types):
        return MessageType.PHOTO
    if any(m.startswith("video/") for m in media_types):
        return MessageType.VIDEO
    if any(m.startswith("audio/") for m in media_types):
        return MessageType.VOICE
    if media_types:
        return MessageType.DOCUMENT
    if text.startswith("/"):
        return MessageType.COMMAND
    return MessageType.TEXT


def _sync_buf_path(encre_home: str, account_id: str) -> Path:
    """Return the long-poll cursor file for one account.

    The cursor is persisted so a restart does not re-deliver the whole history
    since the last successful poll.

    Args:
        encre_home: Root data directory.
        account_id: iLink bot id scoping the cursor.

    Returns:
        The cursor JSON path.
    """
    return _account_dir(encre_home) / f"{account_id}.sync.json"


def _load_sync_buf(encre_home: str, account_id: str) -> str:
    """Load the persisted long-poll cursor, defaulting to empty.

    An empty cursor makes the server return current messages rather than the
    entire backlog, which is the desired behaviour on a fresh install.
    Corrupt files degrade to empty instead of raising.

    Args:
        encre_home: Root data directory.
        account_id: iLink bot id.

    Returns:
        The cursor string, or ``""`` when unavailable.
    """
    path = _sync_buf_path(encre_home, account_id)
    if not path.exists():
        return ""
    try:
        return (_read_secure_json(path, default={}) or {}).get("get_updates_buf", "")
    except Exception:
        return ""


def _save_sync_buf(encre_home: str, account_id: str, sync_buf: str) -> None:
    """Persist the long-poll cursor after each successful poll.

    Written through immediately because the cursor is the only thing preventing
    redelivery of an entire history on the next start.

    Args:
        encre_home: Root data directory.
        account_id: iLink bot id.
        sync_buf: Fresh cursor from the last successful response.
    """
    path = _sync_buf_path(encre_home, account_id)
    atomic_json_write(path, {"get_updates_buf": sync_buf})


async def qr_login(
    encre_home: str,
    *,
    bot_type: str = "3",
    timeout_seconds: int = 480,
) -> Optional[Dict[str, str]]:
    """
    Run the interactive iLink QR login flow.

    Exposed as a helper for the gateway setup wizard. The flow renders the QR
    both as a URL and, when the optional ``qrcode`` package is installed, as an
    ASCII matrix, then polls for confirmation.

    Notable server behaviours handled here:

    - ``scaned_but_redirect`` tells the client to switch iLink hosts and
      continue polling against the new base URL.
    - ``expired`` triggers a bounded number of QR refreshes, since a stale QR
      can never be confirmed.
    - ``confirmed`` yields the credential set, which is persisted immediately
      via :func:`save_weixin_account`.

    Args:
        encre_home: Root data directory for credential persistence.
        bot_type: iLink bot type discriminator sent as a query parameter.
        timeout_seconds: Total wall-clock budget for the interactive flow.

    Returns:
        A credential dict with ``account_id``, ``token``, ``base_url`` and
        ``user_id``, or ``None`` when login fails or times out.

    Raises:
        RuntimeError: When ``aiohttp`` is unavailable.
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp is required for Weixin QR login")

    async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
        try:
            qr_resp = await _api_get(
                session,
                base_url=ILINK_BASE_URL,
                endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                timeout_ms=QR_TIMEOUT_MS,
            )
        except Exception as exc:
            logger.error("weixin: failed to fetch QR code: %s", exc)
            return None

        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
        if not qrcode_value:
            logger.error("weixin: QR response missing qrcode")
            return None

        # qrcode_url is the full scannable liteapp URL; qrcode_value is just
        # the hex token. WeChat needs to scan the full URL, not the raw hex
        # string, so the URL wins whenever it is present.
        qr_scan_data = qrcode_url if qrcode_url else qrcode_value

        print("\nPlease scan the following QR code with WeChat:")
        if qrcode_url:
            print(qrcode_url)
        try:
            import qrcode

            qr = qrcode.QRCode()
            qr.add_data(qr_scan_data)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except Exception as _qr_exc:
            print(f"(QR rendering failed in terminal: {_qr_exc}, please open the URL above directly)")

        deadline = time.monotonic() + timeout_seconds
        current_base_url = ILINK_BASE_URL
        refresh_count = 0

        while time.monotonic() < deadline:
            try:
                status_resp = await _api_get(
                    session,
                    base_url=current_base_url,
                    endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                    timeout_ms=QR_TIMEOUT_MS,
                )
            except asyncio.TimeoutError:
                await asyncio.sleep(1)
                continue
            except Exception as exc:
                logger.warning("weixin: QR poll error: %s", exc)
                await asyncio.sleep(1)
                continue

            status = str(status_resp.get("status") or "wait")
            if status == "wait":
                print(".", end="", flush=True)
            elif status == "scaned":
                print("\nQR code scanned, please confirm in WeChat...")
            elif status == "scaned_but_redirect":
                redirect_host = str(status_resp.get("redirect_host") or "")
                if redirect_host:
                    current_base_url = f"https://{redirect_host}"
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    print("\nQR code expired multiple times, please re-run login.")
                    return None
                print(f"\nQR code expired, refreshing... ({refresh_count}/3)")
                try:
                    qr_resp = await _api_get(
                        session,
                        base_url=ILINK_BASE_URL,
                        endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                        timeout_ms=QR_TIMEOUT_MS,
                    )
                    qrcode_value = str(qr_resp.get("qrcode") or "")
                    qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
                    qr_scan_data = qrcode_url if qrcode_url else qrcode_value
                    if qrcode_url:
                        print(qrcode_url)
                    try:
                        import qrcode as _qrcode
                        qr = _qrcode.QRCode()
                        qr.add_data(qr_scan_data)
                        qr.make(fit=True)
                        qr.print_ascii(invert=True)
                    except Exception:
                        pass
                except Exception as exc:
                    logger.error("weixin: QR refresh failed: %s", exc)
                    return None
            elif status == "confirmed":
                account_id = str(status_resp.get("ilink_bot_id") or "")
                token = str(status_resp.get("bot_token") or "")
                base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                user_id = str(status_resp.get("ilink_user_id") or "")
                if not account_id or not token:
                    logger.error("weixin: QR confirmed but credential payload was incomplete")
                    return None
                save_weixin_account(
                    encre_home,
                    account_id=account_id,
                    token=token,
                    base_url=base_url,
                    user_id=user_id,
                )
                print(f"\nWeChat connection successful, account_id={account_id}")
                return {
                    "account_id": account_id,
                    "token": token,
                    "base_url": base_url,
                    "user_id": user_id,
                }
            await asyncio.sleep(1)

        print("\nWeChat login timed out.")
        return None

class WeixinAdapter(BasePlatformAdapter):
    """Native Encre adapter for Weixin personal accounts over the iLink API.

    This class is the primary deliverable of the module. It satisfies the
    :class:`encre.gateway.platforms.base.BasePlatformAdapter` contract and is
    instantiated by :class:`encre.gateway.run.GatewayRunner` from the ``weixin``
    platform config; outbound :class:`~encre.gateway.platforms.base.SendResult`
    values are consumed by
    :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.

    Lifecycle
    ---------
    construct -> :meth:`connect` (opens two ``ClientSession``s, restores tokens,
    spawns the poll task) -> long-running -> :meth:`disconnect` (cancels the
    poll task, flushes text batches, closes both sessions).

    Threading model
    ---------------
    Two separate ``ClientSession`` objects are used on purpose. The poll session
    owns the long-lived ``getupdates`` hold and must never be shared with send
    traffic, while the send session has aiohttp's internal timeout disabled
    because ``send()`` may be awaited from another thread via
    ``asyncio.run_coroutine_threadsafe()``; aiohttp's client-level timeout
    context then raises "Timeout context manager should be used inside a task".
    All timeout enforcement therefore happens through :func:`asyncio.wait_for`.

    Invariants
    ---------
    - ``_poll_session`` is non-None for the whole life of the poll loop and of
      every media-download path, which run on the inbound side.
    - ``_send_session`` and ``_token`` are both non-None before any outbound
      call returns a successful result; otherwise callers get
      ``SendResult(success=False, error="Not connected")`` instead of an
      exception.
    - Outbound text is serialised through ``_send_text_gate`` so concurrent
      sends from one adapter cannot interleave and cannot trip the server's
      per-session rate limit simultaneously.

    Attributes:
        supports_code_blocks: WeChat renders fenced code blocks, so the agent
            does not need to flatten them before sending.
        splits_long_messages: ``send()`` handles chunking itself.
        MAX_MESSAGE_LENGTH: Character budget per message, below the platform
            hard limit to leave room for the chunking heuristics.
        SUPPORTS_MESSAGE_EDITING: False, because WeChat cannot edit sent
            messages; streaming therefore falls back to send-final-only.
        _SPLIT_THRESHOLD: Text length above which the flush delay widens, since
            a long final chunk signals the user has finished a large paste.
    """

    supports_code_blocks = True  # Weixin renders fenced code blocks
    splits_long_messages = True  # send() chunks via _split_text()

    MAX_MESSAGE_LENGTH = 2000

    # WeChat does not support editing sent messages. Streaming must use the
    # fallback "send-final-only" path so a trailing cursor glyph is never left
    # visible to the recipient.
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config: PlatformConfig):
        """Build the adapter from platform config.

        Resolution order for credentials is config first, then environment,
        then a previously persisted account file. That ordering lets a QR login
        from :func:`qr_login` be picked up automatically on the next start
        without any additional configuration.

        Every tunable is read through a coercing helper so a malformed
        ``config.yaml`` value degrades to the default rather than raising, which
        would prevent the whole gateway from starting.

        Args:
            config: The ``weixin`` platform entry from ``config.yaml``.
        """
        super().__init__(config, Platform.WEIXIN)
        extra = config.extra or {}
        encre_home = str(get_data_dir())
        self._encre_home = encre_home
        self._token_store = ContextTokenStore(encre_home)
        self._typing_cache = TypingTicketCache()
        self._poll_session: Optional[aiohttp.ClientSession] = None
        self._send_session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._dedup = MessageDeduplicator(ttl_seconds=MESSAGE_DEDUP_TTL_SECONDS)

        self._account_id = str(extra.get("account_id") or get_secret("WEIXIN_ACCOUNT_ID", "")).strip()
        self._token = str(config.token or extra.get("token") or get_secret("WEIXIN_TOKEN", "")).strip()
        self._base_url = str(extra.get("base_url") or get_secret("WEIXIN_BASE_URL", ILINK_BASE_URL)).strip().rstrip("/")
        self._cdn_base_url = str(
            extra.get("cdn_base_url") or get_secret("WEIXIN_CDN_BASE_URL", WEIXIN_CDN_BASE_URL)
        ).strip().rstrip("/")
        # Delay inserted between chunks. Too aggressive a cadence trips iLink's
        # frequency limiter, so the default is generous.
        self._send_chunk_delay_seconds = float(
            extra.get("send_chunk_delay_seconds") or os.getenv("WEIXIN_SEND_CHUNK_DELAY_SECONDS", "1.5")
        )
        self._send_chunk_retries = int(
            extra.get("send_chunk_retries") or os.getenv("WEIXIN_SEND_CHUNK_RETRIES", "4")
        )
        self._send_chunk_retry_delay_seconds = float(
            extra.get("send_chunk_retry_delay_seconds")
            or os.getenv("WEIXIN_SEND_CHUNK_RETRY_DELAY_SECONDS", "1.0")
        )
        # Serialises outbound text across all callers of this adapter, so two
        # concurrent sends cannot both hit the server rate limit at once.
        self._send_text_gate = asyncio.Lock()
        self._rate_limit_circuit_threshold = max(
            1,
            int(
                extra.get("rate_limit_circuit_threshold")
                or os.getenv("WEIXIN_RATE_LIMIT_CIRCUIT_THRESHOLD", "1")
            ),
        )
        self._rate_limit_circuit_window_seconds = float(
            extra.get("rate_limit_circuit_window_seconds")
            or os.getenv("WEIXIN_RATE_LIMIT_CIRCUIT_WINDOW_SECONDS", "30.0")
        )
        self._rate_limit_circuit_open_seconds = float(
            extra.get("rate_limit_circuit_open_seconds")
            or os.getenv("WEIXIN_RATE_LIMIT_CIRCUIT_OPEN_SECONDS", "30.0")
        )
        # Monotonic deadline at which the breaker closes. Zero means closed.
        self._rate_limit_circuit_until = 0.0
        self._rate_limit_events: List[float] = []
        self._dm_policy = str(extra.get("dm_policy") or os.getenv("WEIXIN_DM_POLICY", "pairing")).strip().lower()
        self._group_policy = str(extra.get("group_policy") or os.getenv("WEIXIN_GROUP_POLICY", "disabled")).strip().lower()
        allow_from = extra.get("allow_from")
        if allow_from is None:
            allow_from = os.getenv("WEIXIN_ALLOWED_USERS", "")
        group_allow_from = extra.get("group_allow_from")
        if group_allow_from is None:
            group_allow_from = os.getenv("WEIXIN_GROUP_ALLOWED_USERS", "")
        self._allow_from = self._coerce_list(allow_from)
        self._group_allow_from = self._coerce_list(group_allow_from)
        self._split_multiline_messages = _coerce_bool(
            extra.get("split_multiline_messages")
            or os.getenv("WEIXIN_SPLIT_MULTILINE_MESSAGES"),
            default=False,
        )

        # Text debounce batching. iLink delivers messages individually, so a
        # rapid burst (forwarded batches, paste-splits) would otherwise fire a
        # separate agent invocation per message. Batching concatenates them and
        # dispatches once the sender goes quiet. Defaults are tuned to iLink's
        # typical delivery cadence and are tunable via config.yaml under
        # ``platforms.weixin.extra.text_batch_delay_seconds`` and
        # ``text_batch_split_delay_seconds``.
        self._text_batch_delay_seconds = self._coerce_float_extra(
            "text_batch_delay_seconds", 3.0
        )
        self._text_batch_split_delay_seconds = self._coerce_float_extra(
            "text_batch_split_delay_seconds", 5.0
        )
        self._pending_text_batches: Dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}

        if self._account_id and not self._token:
            persisted = load_weixin_account(encre_home, self._account_id)
            if persisted:
                self._token = str(persisted.get("token") or "").strip()
                self._base_url = str(persisted.get("base_url") or self._base_url).strip().rstrip("/")

    def _coerce_float_extra(self, key: str, default: float) -> float:
        """Read a float from ``config.extra``, guarding against bad values.

        The result is fed directly to :func:`asyncio.sleep`, so NaN, infinity
        and unparseable values are rejected rather than propagated, since they
        would hang the batch flush or raise deep inside the event loop.

        Args:
            key: Config key under ``platforms.weixin.extra``.
            default: Value used when the input is missing or invalid.

        Returns:
            A finite, non-negative float.
        """
        import math

        value = self.config.extra.get(key) if getattr(self.config, "extra", None) else None
        if value is None:
            return float(default)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not math.isfinite(parsed) or parsed < 0:
            return float(default)
        return parsed

    @staticmethod
    def _coerce_list(value: Any) -> List[str]:
        """Normalise a config allow-list to a list of trimmed strings.

        Accepts comma-separated strings (the ``config.yaml`` and env var form)
        as well as real lists, tuples and sets. Whitespace-only entries are
        dropped so a trailing comma does not create a phantom allow-list entry
        that would never match.

        Args:
            value: Raw allow-list value of any supported type.

        Returns:
            The normalised list, empty when nothing usable was supplied.
        """
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    async def get_qrcode_url(self) -> tuple[str, str]:
        """Fetch a fresh login QR code for a non-interactive frontend.

        The raw ``qrcode_img_content`` is a scannable liteapp URL, not an image,
        so it is rendered into an SVG and embedded as a base64 data URI. That
        lets a web frontend display it in a plain ``<img>`` tag with no extra
        backend endpoint. When the optional ``qrcode`` package is absent the
        URL is returned as-is and only a warning is logged, degrading the UX
        rather than failing the call.

        Returns:
            A ``(display_url, qrcode_token)`` pair, where the second element is
            the opaque token used later by :meth:`poll_qrcode_status`.

        Raises:
            RuntimeError: When ``aiohttp`` is unavailable or the response
                carries no qrcode value.
        """
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError("aiohttp is required for Weixin QR login")
        async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
            qr_resp = await _api_get(
                session,
                base_url=ILINK_BASE_URL,
                endpoint=f"{EP_GET_BOT_QR}?bot_type=3",
                timeout_ms=QR_TIMEOUT_MS,
            )
            qrcode_value = str(qr_resp.get("qrcode") or "")
            qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
            if not qrcode_value:
                raise RuntimeError("QR response missing qrcode")
            url = qrcode_url if qrcode_url else qrcode_value
            # Render the liteapp URL to an image so the frontend can display it
            # in an <img> tag (the raw URL is not an image).
            qr = qrcode.QRCode(
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10,
                border=2,
            )
            qr.add_data(url)
            img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
            svg_data = img.to_string()
            if isinstance(svg_data, str):
                svg_data = svg_data.encode("utf-8")
            b64 = base64.b64encode(svg_data).decode("ascii")
            url = f"data:image/svg+xml;base64,{b64}"
            return url, qrcode_value

    async def poll_qrcode_status(self, qrcode_token: str) -> dict | None:
        """Poll the confirmation state of one pending QR code.

        Complemented :meth:`get_qrcode_url` for frontends that poll on their own
        timer instead of running :func:`qr_login` in-process.

        Args:
            qrcode_token: Opaque token returned as the second tuple element by
                :meth:`get_qrcode_url`.

        Returns:
            A credential dict on ``confirmed`` status, ``{"scanned": True}``
            on the intermediate ``scanned`` status (QR scanned but the
            confirmation is still pending on the phone), otherwise ``None`` --
            including for an incomplete credential payload, which the caller
            must treat as not-yet-confirmed rather than as success.
        """
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError("aiohttp is required for Weixin QR login")
        async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
            status_resp = await _api_get(
                session,
                base_url=ILINK_BASE_URL,
                endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_token}",
                timeout_ms=QR_TIMEOUT_MS,
            )
            status = str(status_resp.get("status") or "wait")
            if status == "confirmed":
                account_id = str(status_resp.get("ilink_bot_id") or "")
                token = str(status_resp.get("bot_token") or "")
                base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                if not account_id or not token:
                    return None
                return {
                    "ilink_bot_id": account_id,
                    "bot_token": token,
                    "baseurl": base_url,
                    "ilink_user_id": str(status_resp.get("ilink_user_id") or ""),
                }
            if status == "scanned":
                return {"scanned": True}
            return None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Start the adapter: open sessions, restore tokens, spawn the poller.

        Preconditions are checked and reported through
        :meth:`BasePlatformAdapter._set_fatal_error` with ``retryable=False``
        so the runner does not spin on a permanently misconfigured platform.

        Args:
            is_reconnect: Accepted for interface compatibility with the base
                class; iLink has no distinct reconnect handshake, so the flag
                does not change behaviour here.

        Returns:
            ``True`` when the adapter is running, ``False`` when a precondition
            failed.
        """
        if not check_weixin_requirements():
            message = "Weixin startup failed: aiohttp and cryptography are required"
            self._set_fatal_error("weixin_missing_dependency", message, retryable=False)
            logger.warning("[%s] %s", self.name, message)
            return False
        if not self._token:
            message = "Weixin startup failed: WEIXIN_TOKEN is required"
            self._set_fatal_error("weixin_missing_token", message, retryable=False)
            logger.warning("[%s] %s", self.name, message)
            return False
        if not self._account_id:
            message = "Weixin startup failed: WEIXIN_ACCOUNT_ID is required"
            self._set_fatal_error("weixin_missing_account", message, retryable=False)
            logger.warning("[%s] %s", self.name, message)
            return False

        self._poll_session = aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector())
        # Disable aiohttp's built-in ClientTimeout (total=None) to prevent
        # "Timeout context manager should be used inside a task" errors when
        # send() is invoked via asyncio.run_coroutine_threadsafe() from cron.
        # Timeout is managed externally via asyncio.wait_for() in _api_post/_api_get.
        _no_aiohttp_timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=None, sock_read=None)
        self._send_session = aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector(), timeout=_no_aiohttp_timeout)
        self._token_store.restore(self._account_id)
        self._poll_task = asyncio.create_task(self._poll_loop(), name="weixin-poll")
        self._mark_connected()
        _LIVE_ADAPTERS[self._token] = self
        logger.info("[%s] Connected account=%s base=%s", self.name, _safe_id(self._account_id), self._base_url)
        if self._group_policy != "disabled":
            logger.warning(
                "[%s] WEIXIN_GROUP_POLICY=%s is set, but QR-login connects an iLink bot "
                "identity (e.g. ...@im.bot) which typically cannot be invited into ordinary "
                "WeChat groups. iLink usually does not deliver ordinary-group events for "
                "these accounts, so group messages may never reach Encre regardless of this "
                "policy. If group delivery doesn't work, the limitation is on the iLink side, "
                "not in Encre.",
                self.name,
                self._group_policy,
            )
        return True

    async def disconnect(self) -> None:
        """Shut the adapter down cleanly.

        Ordering matters: the poll task is cancelled and awaited before the
        sessions it holds are closed, and pending text batches are cancelled
        before their dictionaries are cleared, so no task can observe a closed
        session or a torn-down batch entry.

        Safe to call repeatedly and after a failed :meth:`connect`.
        """
        _LIVE_ADAPTERS.pop(self._token, None)
        self._running = False
        for task in self._pending_text_batch_tasks.values():
            if not task.done():
                task.cancel()
        self._pending_text_batches.clear()
        self._pending_text_batch_tasks.clear()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        if self._poll_session and not self._poll_session.closed:
            await self._poll_session.close()
        self._poll_session = None
        if self._send_session and not self._send_session.closed:
            await self._send_session.close()
        self._send_session = None
        self._mark_disconnected()
        logger.info("[%s] Disconnected", self.name)

    async def _poll_loop(self) -> None:
        """Drive the ``getupdates`` long-poll for the adapter's lifetime.

        Loop properties:

        - The server may suggest a new hold duration via
          ``longpolling_timeout_ms``; that value is adopted so the client never
          polls more aggressively than the server wants.
        - A stale or expired session pauses for ten minutes. Retrying sooner
          would just burn credentials and rate-limit budget.
        - Other failures use a short retry delay, escalating to a long backoff
          once the consecutive-failure count saturates, and then reset the
          counter so a single recovered cycle cannot wedge the loop.
        - The cursor is persisted on every successful poll, which is what
          prevents re-delivering history across a restart.
        - Each message is processed in its own task so one slow media download
          cannot stall the poll and drop subsequent messages.

        Exits only on cancellation, which is how :meth:`disconnect` stops it.
        """
        assert self._poll_session is not None
        sync_buf = _load_sync_buf(self._encre_home, self._account_id)
        timeout_ms = LONG_POLL_TIMEOUT_MS
        consecutive_failures = 0

        while self._running:
            try:
                response = await _get_updates(
                    self._poll_session,
                    base_url=self._base_url,
                    token=self._token,
                    sync_buf=sync_buf,
                    timeout_ms=timeout_ms,
                )
                suggested_timeout = response.get("longpolling_timeout_ms")
                if isinstance(suggested_timeout, int) and suggested_timeout > 0:
                    timeout_ms = suggested_timeout

                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if ret not in {0, None} or errcode not in {0, None}:
                    if (ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE
                            or _is_stale_session_ret(ret, errcode, response.get("errmsg"))):
                        logger.error("[%s] Session expired; pausing for 10 minutes", self.name)
                        await asyncio.sleep(600)
                        consecutive_failures = 0
                        continue
                    consecutive_failures += 1
                    logger.warning(
                        "[%s] getUpdates failed ret=%s errcode=%s errmsg=%s (%d/%d)",
                        self.name,
                        ret,
                        errcode,
                        response.get("errmsg", ""),
                        consecutive_failures,
                        MAX_CONSECUTIVE_FAILURES,
                    )
                    await asyncio.sleep(BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS)
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                    continue

                consecutive_failures = 0
                new_sync_buf = str(response.get("get_updates_buf") or "")
                if new_sync_buf:
                    sync_buf = new_sync_buf
                    _save_sync_buf(self._encre_home, self._account_id, sync_buf)

                for message in response.get("msgs") or []:
                    asyncio.create_task(self._process_message_safe(message))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                logger.error("[%s] poll error (%d/%d): %s", self.name, consecutive_failures, MAX_CONSECUTIVE_FAILURES, exc)
                await asyncio.sleep(BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0

    async def _process_message_safe(self, message: Dict[str, Any]) -> None:
        """Isolate one inbound message from poll-loop failure.

        This wrapper exists so that an unexpected exception in
        :meth:`_process_message` cannot be attributed to the poll loop's
        failure counter and trigger an unnecessary backoff cycle.

        Args:
            message: One raw iLink message envelope.
        """
        try:
            await self._process_message(message)
        except Exception as exc:
            logger.error("[%s] unhandled inbound error from=%s: %s", self.name, _safe_id(message.get("from_user_id")), exc, exc_info=True)

    async def _process_message(self, message: Dict[str, Any]) -> None:
        """Authenticate, map and dispatch one inbound iLink message.

        Steps, in order: drop self-sent and duplicate traffic; apply the
        DM/group intake policy; refresh the peer's ``context_token`` and
        request a typing ticket; download every media item, including media
        quoted inside a reply; build a
        :class:`~encre.gateway.platforms.base.MessageEvent` and hand it to
        :meth:`BasePlatformAdapter.handle_message`.

        Plain text is routed through :meth:`_enqueue_text_event` for batching,
        while media events dispatch immediately, because a user attaching a
        file is not going to keep typing and delaying them would feel broken.

        Args:
            message: One raw iLink message envelope.
        """
        assert self._poll_session is not None
        sender_id = str(message.get("from_user_id") or "").strip()
        if not sender_id:
            return
        if sender_id == self._account_id:
            return

        message_id = str(message.get("message_id") or "").strip()
        if message_id and self._dedup.is_duplicate(sender_id, message_id):
            return

        # Secondary content-fingerprint dedup for text messages. iLink can
        # re-deliver a message with a new id, so matching on id alone is not
        # enough.
        item_list = message.get("item_list") or []
        text = _extract_text(item_list)
        if text:
            if self._dedup.is_duplicate(sender_id, None, text):
                logger.debug("[%s] Content-dedup: skipping duplicate message from %s", self.name, sender_id)
                return

        chat_type, effective_chat_id = _guess_chat_type(message, self._account_id)
        if chat_type == "group":
            if self._group_policy == "disabled":
                return
            if self._group_policy == "allowlist" and effective_chat_id not in self._group_allow_from:
                return
            if self._group_policy == "pairing":
                return
        elif not self._is_dm_intake_allowed(sender_id):
            return

        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._token_store.set(self._account_id, sender_id, context_token)
        # Fetching the ticket is fire-and-forget: typing feedback is cosmetic
        # and must not block the actual message pipeline.
        asyncio.create_task(self._maybe_fetch_typing_ticket(sender_id, context_token or None))

        media_paths: List[str] = []
        media_types: List[str] = []

        for item in item_list:
            await self._collect_media(item, media_paths, media_types)
            ref_message = item.get("ref_msg") or {}
            ref_item = ref_message.get("message_item")
            if isinstance(ref_item, dict):
                await self._collect_media(ref_item, media_paths, media_types)

        if not text and not media_paths:
            return

        source = self.build_source(
            chat_id=effective_chat_id,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_id,
        )
        event = MessageEvent(
            text=text,
            message_type=_message_type_from_media(media_types, text),
            source=source,
            raw_message=message,
            message_id=message_id or None,
            media_urls=media_paths,
            media_types=media_types,
            timestamp=datetime.now(),
        )
        logger.info("[%s] inbound from=%s type=%s media=%d", self.name, _safe_id(sender_id), source.chat_type, len(media_paths))
        if event.message_type == MessageType.TEXT:
            self._enqueue_text_event(event)
        else:
            await self.handle_message(event)

    def _open_dm_opted_in(self) -> bool:
        """Report whether open DM policy is explicitly opted in via the env.

        Open policy is deliberately double-gated: the config value selects it,
        but an environment variable must also affirm it, so a stray
        ``dm_policy: open`` in a committed config cannot silently expose the
        bot to anyone on the internet.

        Returns:
            ``True`` when an open DM opt-in environment variable is set.
        """
        if os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"}:
            return True
        return os.getenv("WEIXIN_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"}

    def _is_dm_allowed(self, sender_id: str) -> bool:
        """Decide whether a peer may converse with the bot at all.

        Used for the strictest interpretation of the DM policy, where even
        intake is refused outright.

        Args:
            sender_id: Peer id under consideration.

        Returns:
            ``True`` when the peer passes the configured DM policy.
        """
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return sender_id in self._allow_from
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    def _is_dm_intake_allowed(self, sender_id: str) -> bool:
        """Decide whether a peer's message should be accepted for processing.

        Intake is intentionally more permissive than :meth:`_is_dm_allowed`:
        under ``pairing`` policy messages are accepted so the pairing handshake
        can complete, while the base adapter still decides whether to respond.

        Args:
            sender_id: Peer id under consideration.

        Returns:
            ``True`` when the peer's message should be processed.
        """
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return sender_id in self._allow_from
        if self._dm_policy == "pairing":
            return True
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    @property
    def enforces_own_access_policy(self) -> bool:
        """Declare that this adapter gates access at intake itself.

        Telling the base adapter that intake is already enforced prevents the
        runner from applying a second, redundant access check on top of
        ``dm_policy``/``group_policy``.

        Returns:
            Always ``True`` for Weixin.
        """
        return True

    # ------------------------------------------------------------------
    # Text debounce batching
    # ------------------------------------------------------------------

    # Above this length the last chunk is assumed to terminate a large paste,
    # so the flush delay widens to give the sender time to keep going.
    _SPLIT_THRESHOLD = 1800  # iLink chunks at ~2048 chars

    def _text_batch_key(self, event: MessageEvent) -> str:
        """Derive the batching key for one text event.

        The key is the session key, so two users in different sessions never
        merge into one batch, while rapid messages from the same user inside
        the same session do.

        Args:
            event: The event being batched.

        Returns:
            The session-scoped batch key.
        """
        from encre.gateway.session import build_session_key
        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

    def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Buffer a text event and reset the flush timer.

        When users forward multiple messages or send rapid-fire texts
        via WeChat, each arrives as a separate iLink message. This
        concatenates them and waits for a short quiet period before
        dispatching the combined message.

        Each arriving chunk cancels the previous flush task, which is what
        makes the batch a *quiet-period* rather than a fixed-delay timer: the
        effective delay only starts once the sender stops. Media is merged into
        the existing event rather than dispatched separately, so a
        text-plus-image burst reaches the agent as one conversation.

        Args:
            event: The text event to batch.
        """
        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
        else:
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)

        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(
            self._flush_text_batch(key)
        )

    async def _flush_text_batch(self, key: str) -> None:
        """Wait for the quiet period, then dispatch the aggregated text.

        A longer-than-last-chunk message widens the quiet window, because a
        long final chunk signals the sender is mid-burst. After sleeping, the
        task re-checks that it is still the registered task for this key; a
        newer enqueue has already cancelled and replaced it, so only the most
        recent task may flush.

        Args:
            key: Batch key as produced by :meth:`_text_batch_key`.
        """
        current_task = asyncio.current_task()
        try:
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            if last_len >= self._SPLIT_THRESHOLD:
                delay = self._text_batch_split_delay_seconds
            else:
                delay = self._text_batch_delay_seconds
            await asyncio.sleep(delay)
            if self._pending_text_batch_tasks.get(key) is not current_task:
                return
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            await self.handle_message(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    async def _collect_media(self, item: Dict[str, Any], media_paths: List[str], media_types: List[str]) -> None:
        """Download one media item and append it to the collected lists.

        Dispatches on the item type discriminant. Each download is independent
        and non-fatal: one failed attachment must not discard the rest of the
        message, since the accompanying text may still be meaningful.

        Args:
            item: One message item, possibly a quoted reference item.
            media_paths: Accumulator for cached local file paths.
            media_types: Accumulator for the matching MIME prefixes.
        """
        item_type = item.get("type")
        if item_type == ITEM_IMAGE:
            path = await self._download_image(item)
            if path:
                media_paths.append(path)
                media_types.append("image/jpeg")
        elif item_type == ITEM_VIDEO:
            path = await self._download_video(item)
            if path:
                media_paths.append(path)
                media_types.append("video/mp4")
        elif item_type == ITEM_FILE:
            path, mime = await self._download_file(item)
            if path:
                media_paths.append(path)
                media_types.append(mime)
        elif item_type == ITEM_VOICE:
            voice_path = await self._download_voice(item)
            if voice_path:
                media_paths.append(voice_path)
                media_types.append("audio/silk")

    async def _download_image(self, item: Dict[str, Any]) -> Optional[str]:
        """Download and decrypt one inbound image item.

        Image items place the key in a different shape than every other media
        type: ``image_item.aeskey`` is a hex string rather than a base64 blob,
        so it is hex-decoded and re-base64-encoded to satisfy
        :func:`_parse_aes_key`. ``media.aes_key`` remains the fallback.

        Args:
            item: The ``image`` message item.

        Returns:
            A cached local image path, or ``None`` when the download failed.
        """
        media = _media_reference(item, "image_item")
        try:
            data = await _download_and_decrypt_media(
                self._poll_session,
                cdn_base_url=self._cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=(item.get("image_item") or {}).get("aeskey")
                and base64.b64encode(bytes.fromhex(str((item.get("image_item") or {}).get("aeskey")))).decode("ascii")
                or media.get("aes_key"),
                full_url=media.get("full_url"),
                timeout_seconds=30.0,
            )
            return cache_image_from_bytes(data, ".jpg")
        except Exception as exc:
            logger.warning("[%s] image download failed: %s", self.name, exc)
            return None

    async def _download_video(self, item: Dict[str, Any]) -> Optional[str]:
        """Download and decrypt one inbound video item.

        Uses the longest download timeout of any inbound media type because
        videos are the largest payloads and CDN latency dominates.

        Args:
            item: The ``video`` message item.

        Returns:
            A cached local video path, or ``None`` when the download failed.
        """
        media = _media_reference(item, "video_item")
        try:
            data = await _download_and_decrypt_media(
                self._poll_session,
                cdn_base_url=self._cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=media.get("aes_key"),
                full_url=media.get("full_url"),
                timeout_seconds=120.0,
            )
            return cache_document_from_bytes(data, "video.mp4")
        except Exception as exc:
            logger.warning("[%s] video download failed: %s", self.name, exc)
            return None

    async def _download_file(self, item: Dict[str, Any]) -> Tuple[Optional[str], str]:
        """Download and decrypt one inbound document item.

        Returns both the path and the MIME type, because unlike images and
        video the document type is not fixed and the caller needs it to set
        ``media_types`` on the event. The filename drives the guessed MIME
        type, so an unknown extension degrades to octet-stream.

        Args:
            item: The ``file`` message item.

        Returns:
            A ``(path, mime_type)`` pair. ``path`` is ``None`` on failure, but
            ``mime_type`` is still returned so the caller can react
            consistently.
        """
        file_item = item.get("file_item") or {}
        media = file_item.get("media") or {}
        filename = str(file_item.get("file_name") or "document.bin")
        mime = _mime_from_filename(filename)
        try:
            data = await _download_and_decrypt_media(
                self._poll_session,
                cdn_base_url=self._cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=media.get("aes_key"),
                full_url=media.get("full_url"),
                timeout_seconds=60.0,
            )
            return cache_document_from_bytes(data, filename), mime
        except Exception as exc:
            logger.warning("[%s] file download failed: %s", self.name, exc)
            return None, mime

    async def _download_voice(self, item: Dict[str, Any]) -> Optional[str]:
        """Download and decrypt one inbound voice item.

        When the item already carries a ``text`` transcription there is nothing
        to download: the text is the useful content and the audio bytes would
        only be noise. Those are returned as ``None`` and the caller falls
        back to the transcription extracted by :func:`_extract_text`.

        Args:
            item: The ``voice`` message item.

        Returns:
            A cached local audio path, ``None`` when transcribed or failed.
        """
        voice_item = item.get("voice_item") or {}
        media = voice_item.get("media") or {}
        if voice_item.get("text"):
            return None
        try:
            data = await _download_and_decrypt_media(
                self._poll_session,
                cdn_base_url=self._cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=media.get("aes_key"),
                full_url=media.get("full_url"),
                timeout_seconds=60.0,
            )
            return cache_audio_from_bytes(data, ".silk")
        except Exception as exc:
            logger.warning("[%s] voice download failed: %s", self.name, exc)
            return None

    async def _maybe_fetch_typing_ticket(self, user_id: str, context_token: Optional[str]) -> None:
        """Pre-fetch a typing ticket for a peer that just messaged.

        Fetching ahead of need means :meth:`send_typing` can fire without an
        extra round trip while the agent is still thinking. The call is best
        effort and failures are debug-logged only.

        Args:
            user_id: Peer that sent the inbound message.
            context_token: Token carried by that message, when present.
        """
        if not self._poll_session or not self._token:
            return
        if self._typing_cache.get(user_id):
            return
        try:
            response = await _get_config(
                self._poll_session,
                base_url=self._base_url,
                token=self._token,
                user_id=user_id,
                context_token=context_token,
            )
            typing_ticket = str(response.get("typing_ticket") or "")
            if typing_ticket:
                self._typing_cache.set(user_id, typing_ticket)
        except Exception as exc:
            logger.debug("[%s] getConfig failed for %s: %s", self.name, _safe_id(user_id), exc)

    def _split_text(self, content: str) -> List[str]:
        """Split content using this adapter's configured delivery mode.

        Thin binding from the instance configuration onto
        :func:`_split_text_for_weixin_delivery`, which owns all the splitting
        policy.

        Args:
            content: Assistant output to split.

        Returns:
            The ordered chunks, each within :attr:`MAX_MESSAGE_LENGTH`.
        """
        return _split_text_for_weixin_delivery(
            content, self.MAX_MESSAGE_LENGTH, self._split_multiline_messages,
        )

    def _rate_limit_cooldown_remaining(self) -> float:
        """Seconds left on the outbound rate-limit circuit breaker.

        Args:
            No arguments; reads monotonic state.

        Returns:
            Non-negative seconds until the breaker closes.
        """
        return max(0.0, self._rate_limit_circuit_until - time.monotonic())

    def _rate_limit_error(self) -> RuntimeError:
        """Build a descriptive error for a send blocked by the breaker.

        Args:
            No arguments; reads the current cooldown.

        Returns:
            A :class:`RuntimeError` naming how long the caller must wait.
        """
        return RuntimeError(
            f"iLink sendmessage rate limited; cooldown active for {self._rate_limit_cooldown_remaining():.1f}s"
        )

    def _open_rate_limit_circuit(self) -> None:
        """Open the outbound breaker for the configured cooldown window.

        Uses ``max`` rather than assignment so that overlapping rate-limit
        signals never shorten an already-running cooldown.

        Disabled when the configured open duration is non-positive.
        """
        if self._rate_limit_circuit_open_seconds <= 0:
            return
        self._rate_limit_circuit_until = max(
            self._rate_limit_circuit_until,
            time.monotonic() + self._rate_limit_circuit_open_seconds,
        )

    def _record_rate_limit_event(self) -> bool:
        """Record a genuine iLink rate limit and report whether the breaker opened.

        Only events inside a sliding window are considered, so a trickle of
        isolated limits does not accumulate. The threshold comparison is
        inclusive, so hitting the threshold exactly opens the circuit.

        Returns:
            ``True`` when this call opened the breaker.
        """
        now = time.monotonic()
        window_start = now - self._rate_limit_circuit_window_seconds
        self._rate_limit_events = [ts for ts in self._rate_limit_events if ts >= window_start]
        self._rate_limit_events.append(now)
        if len(self._rate_limit_events) >= self._rate_limit_circuit_threshold:
            self._open_rate_limit_circuit()
            return self._rate_limit_cooldown_remaining() > 0
        return False

    def _reset_rate_limit_circuit(self) -> None:
        """Close the breaker after a successful send.

        Clearing both the event window and the deadline on success is what
        keeps a brief burst from permanently throttling the adapter.
        """
        self._rate_limit_events.clear()
        self._rate_limit_circuit_until = 0.0

    async def _send_text_chunk(
        self,
        *,
        chat_id: str,
        chunk: str,
        context_token: Optional[str],
        client_id: str,
    ) -> None:
        """Send one text chunk under the adapter-wide outbound gate.

        Serialising all outbound text through a single lock keeps the two
        concurrent senders common in Encre -- the stream consumer and a
        cron-triggered push -- from racing iLink's per-session limits.

        On session-expired errors, automatically retries
        *without* ``context_token``: iLink accepts tokenless sends as a
        degraded fallback, which keeps cron-initiated push messages working
        even when no user message has refreshed the session recently.

        Args:
            chat_id: Destination peer.
            chunk: One already-split text chunk.
            context_token: Token to echo, or ``None``.
            client_id: Client idempotency id.
        """
        async with self._send_text_gate:
            await self._send_text_chunk_locked(
                chat_id=chat_id,
                chunk=chunk,
                context_token=context_token,
                client_id=client_id,
            )

    async def _send_text_chunk_locked(
        self,
        *,
        chat_id: str,
        chunk: str,
        context_token: Optional[str],
        client_id: str,
    ) -> None:
        """Send a chunk while the adapter-wide outbound text gate is held.

        Retry policy per attempt:

        - Check the breaker first; a send attempted during cooldown is
          pointless and only extends it.
        - Session-expired (``-14`` or the stale sentinel) triggers exactly one
          retry without the ``context_token``, and the poisoned token is
          evicted from the store so it is not reused.
        - Genuine rate limit (``-2``) backs off by a fixed 3x multiplier and
          records the event against the sliding-window breaker. The error is
          recorded before breaking so an exhausted loop raises a descriptive
          :class:`RuntimeError` instead of falling through to the assertion.
        - Any other exception uses linear backoff scaled by attempt number.

        On success the breaker is reset, so a clean send immediately restores
        full throughput.

        Raises:
            RuntimeError: When retries are exhausted or the breaker is open.
        """
        last_error: Optional[Exception] = None
        retried_without_token = False
        for attempt in range(self._send_chunk_retries + 1):
            if self._rate_limit_cooldown_remaining() > 0:
                raise self._rate_limit_error()
            try:
                resp = await _send_message(
                    self._send_session,
                    base_url=self._base_url,
                    token=self._token,
                    to=chat_id,
                    text=chunk,
                    context_token=context_token,
                    client_id=client_id,
                )
                # Check iLink response for session-expired error
                if resp and isinstance(resp, dict):
                    ret = resp.get("ret")
                    errcode = resp.get("errcode")
                    if (ret is not None and ret not in {0,}) or (errcode is not None and errcode not in {0,}):
                        is_session_expired = (
                            ret == SESSION_EXPIRED_ERRCODE
                            or errcode == SESSION_EXPIRED_ERRCODE
                            or _is_stale_session_ret(ret, errcode, resp.get("errmsg"))
                        )
                        # Session expired: strip token and retry once.
                        if is_session_expired and not retried_without_token and context_token:
                            retried_without_token = True
                            context_token = None
                            # The token is known-bad, so drop it from the
                            # store rather than retrying with it later.
                            self._token_store._cache.pop(
                                self._token_store._key(self._account_id, chat_id), None
                            )
                            logger.warning(
                                "[%s] session expired for %s; retrying without context_token",
                                self.name, _safe_id(chat_id),
                            )
                            continue
                        # Rate limit (-2): backoff and retry.
                        is_rate_limited = (
                            ret == RATE_LIMIT_ERRCODE
                            or errcode == RATE_LIMIT_ERRCODE
                        )
                        if is_rate_limited:
                            errmsg = resp.get("errmsg") or resp.get("msg") or "rate limited"
                            # Record the error so we raise a descriptive
                            # RuntimeError (instead of AssertionError) if the
                            # loop exhausts with the server still rate-limiting.
                            last_error = RuntimeError(
                                f"iLink sendmessage rate limited: ret={ret} errcode={errcode} errmsg={errmsg}"
                            )
                            if self._record_rate_limit_event():
                                last_error = self._rate_limit_error()
                                break
                            if attempt >= self._send_chunk_retries:
                                break
                            wait = self._send_chunk_retry_delay_seconds * 3  # 3x backoff for rate limit
                            logger.warning(
                                "[%s] rate limited for %s; backing off %.1fs before retry",
                                self.name, _safe_id(chat_id), wait,
                            )
                            await asyncio.sleep(wait)
                            continue
                        errmsg = resp.get("errmsg") or resp.get("msg") or "unknown error"
                        raise RuntimeError(
                            f"iLink sendmessage error: ret={ret} errcode={errcode} errmsg={errmsg}"
                        )
                self._reset_rate_limit_circuit()
                return
            except Exception as exc:
                last_error = exc
                if attempt >= self._send_chunk_retries:
                    break
                wait = self._send_chunk_retry_delay_seconds * (attempt + 1)
                logger.warning(
                    "[%s] send chunk failed to=%s attempt=%d/%d, retrying in %.2fs: %s",
                    self.name,
                    _safe_id(chat_id),
                    attempt + 1,
                    self._send_chunk_retries + 1,
                    wait,
                    exc,
                )
                if wait > 0:
                    await asyncio.sleep(wait)
        assert last_error is not None
        raise last_error

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message to a peer, delivering media before text.

        This is the outbound entry point whose
        :class:`~encre.gateway.platforms.base.SendResult` is consumed by
        :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.

        Order of work: resolve the peer token, extract inline ``MEDIA:`` tags
        and bare local paths, deliver attachments first, then the text
        split into chunks. Delivering media first matches WeChat's rendering
        order for human-authored messages, where an attachment precedes its
        caption.

        Attachment failures are logged and skipped rather than aborting the
        whole send: losing one image should not prevent the accompanying text
        from arriving. A text failure, by contrast, propagates as a failed
        :class:`~encre.gateway.platforms.base.SendResult` so the consumer can
        report it.

        Args:
            chat_id: Destination peer id.
            content: Raw assistant output, which may embed media references.
            reply_to: Quoted message id; unused, since iLink reply threading
                is not available on this path.
            metadata: Caller metadata, passed through to media sends.

        Returns:
            A successful result carrying the last client id, or a failed result
            naming the error.
        """
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")
        context_token = self._token_store.get(self._account_id, chat_id)
        last_message_id: Optional[str] = None

        # Extract MEDIA: tags and bare local file paths before text delivery.
        media_files, cleaned_content = self.extract_media(content)
        media_files = self.filter_media_delivery_paths(media_files)
        _, image_cleaned = self.extract_images(cleaned_content)
        local_files, final_content = self.extract_local_files(image_cleaned)
        local_files = self.filter_local_delivery_paths(local_files)

        # Extension buckets decide which send helper handles each attachment,
        # since iLink needs a distinct media_type per shape.
        _AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
        _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

        async def _deliver_media(path: str, is_voice: bool = False) -> None:
            """Route one local attachment to the matching send helper.

            Args:
                path: Local file path to deliver.
                is_voice: Marks a voice attachment, which overrides the
                    extension-based guess.
            """
            ext = Path(path).suffix.lower()
            if is_voice or ext in _AUDIO_EXTS:
                await self.send_voice(chat_id=chat_id, audio_path=path, metadata=metadata)
            elif ext in _VIDEO_EXTS:
                await self.send_video(chat_id=chat_id, video_path=path, metadata=metadata)
            elif ext in _IMAGE_EXTS:
                await self.send_image_file(chat_id=chat_id, image_path=path, metadata=metadata)
            else:
                await self.send_document(chat_id=chat_id, file_path=path, metadata=metadata)

        try:
            # Deliver extracted MEDIA: attachments first.
            for media_path, is_voice in media_files:
                try:
                    await _deliver_media(media_path, is_voice)
                except Exception as exc:
                    logger.warning("[%s] media delivery failed for %s: %s", self.name, media_path, exc)

            # Deliver bare local file paths.
            for file_path in local_files:
                try:
                    await _deliver_media(file_path, is_voice=False)
                except Exception as exc:
                    logger.warning("[%s] local file delivery failed for %s: %s", self.name, file_path, exc)

            # Deliver text content.
            chunks = [c for c in self._split_text(self.format_message(final_content)) if c and c.strip()]
            for idx, chunk in enumerate(chunks):
                client_id = f"encre-weixin-{uuid.uuid4().hex}"
                await self._send_text_chunk(
                    chat_id=chat_id,
                    chunk=chunk,
                    context_token=context_token,
                    client_id=client_id,
                )
                last_message_id = client_id
                # Inter-chunk pacing avoids tripping the per-session frequency
                # limiter on long replies.
                if idx < len(chunks) - 1 and self._send_chunk_delay_seconds > 0:
                    await asyncio.sleep(self._send_chunk_delay_seconds)
            return SendResult(success=True, message_id=last_message_id)
        except Exception as exc:
            logger.error("[%s] send failed to=%s: %s", self.name, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def _ensure_typing_ticket(self, chat_id: str) -> Optional[str]:
        """Return a valid typing ticket, refreshing from getConfig if expired.

        The iLink typing ticket has a 600-second TTL.  When a long-running
        session exceeds that window the cached ticket evicts, and both
        ``send_typing`` and ``stop_typing`` silently no-op: leaving the
        WeChat client stuck showing the typing indicator forever.  This
        method transparently refreshes the ticket so the stop signal can
        always be delivered.

        Args:
            chat_id: Peer the ticket is needed for.

        Returns:
            A usable ticket, or ``None`` when it could not be obtained.
        """
        ticket = self._typing_cache.get(chat_id)
        if ticket:
            return ticket
        if not self._send_session or not self._token:
            return None
        # Ticket expired or never fetched: refresh via getConfig.
        # Use the most recent context_token for this peer if available.
        context_token = self._token_store.get(self._account_id, chat_id)
        try:
            response = await _get_config(
                self._send_session,
                base_url=self._base_url,
                token=self._token,
                user_id=chat_id,
                context_token=context_token,
            )
            typing_ticket = str(response.get("typing_ticket") or "")
            if typing_ticket:
                self._typing_cache.set(chat_id, typing_ticket)
                return typing_ticket
        except Exception as exc:
            logger.debug(
                "[%s] typing ticket refresh failed for %s: %s",
                self.name, _safe_id(chat_id), exc,
            )
        return None

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Signal to a peer that the bot is composing.

        No-ops silently when disconnected or when no ticket can be obtained,
        because typing feedback is purely cosmetic and must never raise on the
        agent's critical path.

        Args:
            chat_id: Peer to signal.
            metadata: Unused, accepted for interface compatibility.
        """
        if not self._send_session or not self._token:
            return
        typing_ticket = await self._ensure_typing_ticket(chat_id)
        if not typing_ticket:
            return
        try:
            await _send_typing(
                self._send_session,
                base_url=self._base_url,
                token=self._token,
                to_user_id=chat_id,
                typing_ticket=typing_ticket,
                status=TYPING_START,
            )
        except Exception as exc:
            logger.debug("[%s] typing start failed for %s: %s", self.name, _safe_id(chat_id), exc)

    async def stop_typing(self, chat_id: str) -> None:
        """Clear the typing indicator for a peer.

        Mirrors :meth:`send_typing` in its failure posture. Stopping matters
        more than starting, since a stranded typing indicator reads as a hung
        bot to the recipient.

        Args:
            chat_id: Peer whose indicator is being cleared.
        """
        if not self._send_session or not self._token:
            return
        typing_ticket = await self._ensure_typing_ticket(chat_id)
        if not typing_ticket:
            return
        try:
            await _send_typing(
                self._send_session,
                base_url=self._base_url,
                token=self._token,
                to_user_id=chat_id,
                typing_ticket=typing_ticket,
                status=TYPING_STOP,
            )
        except Exception as exc:
            logger.debug("[%s] typing stop failed for %s: %s", self.name, _safe_id(chat_id), exc)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image from either a URL or a local path.

        Remote URLs are fetched to a temporary file first, because the upload
        pipeline reads from disk. That temp file is unlinked in a ``finally``
        block so a failed upload cannot leak scratch files. Local paths are
        used directly and never deleted.

        Args:
            chat_id: Destination peer.
            image_url: http(s) URL or local path, with an optional
                ``file://`` prefix.
            caption: Caption to send with the image.
            reply_to: Unused, accepted for interface compatibility.
            metadata: Forwarded to the underlying document send.

        Returns:
            The :class:`~encre.gateway.platforms.base.SendResult` from the
            underlying send.
        """
        if image_url.startswith(("http://", "https://")):
            file_path = await self._download_remote_media(image_url)
            cleanup = True
        else:
            file_path = image_url.replace("file://", "")
            if not os.path.isabs(file_path):
                file_path = os.path.abspath(file_path)
            cleanup = False
        try:
            return await self.send_document(chat_id, file_path, caption=caption, metadata=metadata)
        finally:
            if cleanup and file_path and os.path.exists(file_path):
                try:
                    os.unlink(file_path)
                except OSError:
                    pass

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file.

        WeChat treats an image and a document with the same wire path here, so
        this delegates straight to :meth:`send_document` and discards the
        parameters iLink does not accept.

        Args:
            chat_id: Destination peer.
            image_path: Local image path.
            caption: Optional caption.
            reply_to: Accepted and discarded.
            metadata: Forwarded to the document send.
            **kwargs: Accepted and discarded.

        Returns:
            The :class:`~encre.gateway.platforms.base.SendResult`.
        """
        del reply_to, kwargs
        return await self.send_document(
            chat_id=chat_id,
            file_path=image_path,
            caption=caption,
            metadata=metadata,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document attachment via the CDN upload flow.

        Args:
            chat_id: Destination peer.
            file_path: Local file to upload.
            caption: Caption text sent ahead of the attachment.
            file_name: Unused; iLink derives the name from the path.
            reply_to: Unused.
            metadata: Unused.
            **kwargs: Accepted for interface compatibility.

        Returns:
            A successful result with the message id, or a failed result naming
            the error.
        """
        del file_name, reply_to, metadata, kwargs
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")
        try:
            message_id = await self._send_file(chat_id, file_path, caption or "")
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.error("[%s] send_document failed to=%s: %s", self.name, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a video attachment via the CDN upload flow.

        Args:
            chat_id: Destination peer.
            video_path: Local video path.
            caption: Optional caption.
            reply_to: Unused.
            metadata: Unused.

        Returns:
            A successful result with the message id, or a failed result.
        """
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")
        try:
            message_id = await self._send_file(chat_id, video_path, caption or "")
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.error("[%s] send_video failed to=%s: %s", self.name, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send audio, always as a file attachment.

        Native outbound Weixin voice bubbles are not proven-working in the
        upstream reference implementation. Prefer a reliable file attachment
        fallback so users at least receive playable audio, even for .silk.

        Args:
            chat_id: Destination peer.
            audio_path: Local audio path.
            caption: Caption used as the fallback attachment label.
            reply_to: Unused.
            metadata: Unused.

        Returns:
            A successful result with the message id, or a failed result.
        """
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")

        fallback_caption = caption or "[voice message as attachment]"
        try:
            message_id = await self._send_file(
                chat_id,
                audio_path,
                fallback_caption,
                force_file_attachment=True,
            )
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.error("[%s] send_voice failed to=%s: %s", self.name, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def _download_remote_media(self, url: str) -> str:
        """Fetch a remote media URL to a temp file for upload.

        The URL is validated through the project's SSRF guard before any
        network access, since ``send_image`` may be called with content the
        agent produced rather than content the platform server handed us.

        Args:
            url: http(s) URL to fetch.

        Returns:
            The local temp file path, which the caller owns and must unlink.

        Raises:
            ValueError: When the URL fails SSRF validation.
        """
        from encre.ssrf import EncreSSRFGuard

        if not EncreSSRFGuard().validate_url(url):
            raise ValueError(f"Blocked unsafe URL (SSRF protection): {url}")

        assert self._send_session is not None
        # Use asyncio.wait_for() instead of aiohttp ClientTimeout to avoid
        # "Timeout context manager should be used inside a task" errors.
        async def _do_fetch():
            """Fetch the remote media body, raising on a non-2xx status."""
            async with self._send_session.get(url) as response:
                response.raise_for_status()
                return await response.read()
        data = await asyncio.wait_for(_do_fetch(), timeout=30)
        # The suffix is derived from the path component only, so a query string
        # cannot inject a misleading extension into the temp file.
        suffix = Path(url.split("?", 1)[0]).suffix or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(data)
            return handle.name

    async def _send_file(
        self,
        chat_id: str,
        path: str,
        caption: str,
        force_file_attachment: bool = False,
    ) -> str:
        """Upload one local file to WeChat through the CDN pipeline.

        The pipeline: read plaintext -> derive the media shape and item
        builder -> mint a random ``filekey`` and a fresh AES key -> request an
        upload slot declaring the post-padding size -> encrypt and POST the
        ciphertext -> send the caption as a separate text message -> send the
        media item as its own message.

        The caption and the attachment are two distinct messages because iLink
        does not support a combined caption-plus-media item on this path.

        Args:
            chat_id: Destination peer.
            path: Local file path.
            caption: Caption text, sent before the attachment.
            force_file_attachment: Downgrade a ``.silk`` file to a plain
                attachment instead of a native voice bubble.

        Returns:
            The client id of the media message.
        """
        assert self._send_session is not None and self._token is not None
        plaintext = Path(path).read_bytes()
        media_type, item_builder = self._outbound_media_builder(path, force_file_attachment=force_file_attachment)
        filekey = secrets.token_hex(16)
        aes_key = secrets.token_bytes(16)
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        upload_response = await _get_upload_url(
            self._send_session,
            base_url=self._base_url,
            token=self._token,
            to_user_id=chat_id,
            media_type=media_type,
            filekey=filekey,
            rawsize=rawsize,
            rawfilemd5=rawfilemd5,
            filesize=_aes_padded_size(rawsize),
            aeskey_hex=aes_key.hex(),
        )
        upload_param = str(upload_response.get("upload_param") or "")
        upload_full_url = str(upload_response.get("upload_full_url") or "")
        ciphertext = _aes128_ecb_encrypt(plaintext, aes_key)

        # Prefer upload_full_url (direct CDN), fall back to constructed CDN URL
        # from upload_param.  Both paths use POST; the earlier PUT form for
        # upload_full_url caused 404s on the WeChat CDN.
        if upload_full_url:
            upload_url = upload_full_url
        elif upload_param:
            upload_url = _cdn_upload_url(self._cdn_base_url, upload_param, filekey)
        else:
            raise RuntimeError(f"getUploadUrl returned neither upload_param nor upload_full_url: {upload_response}")

        encrypted_query_param = await _upload_ciphertext(
            self._send_session,
            ciphertext=ciphertext,
            upload_url=upload_url,
        )
        context_token = self._token_store.get(self._account_id, chat_id)
        # The iLink API expects aes_key as base64(hex_string), not
        # base64(raw_bytes). Sending base64(raw_bytes) causes images to render
        # as grey boxes on the receiver side because the decryption key does
        # not match what the client derives.
        aes_key_for_api = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")
        item_kwargs = {
            "encrypt_query_param": encrypted_query_param,
            "aes_key_for_api": aes_key_for_api,
            "ciphertext_size": len(ciphertext),
            "plaintext_size": rawsize,
            "filename": Path(path).name,
            "rawfilemd5": rawfilemd5,
        }
        # Silk codec parameters are only meaningful on the native voice shape;
        # they are omitted when the file was downgraded to a plain attachment.
        if media_type == MEDIA_VOICE and path.endswith(".silk"):
            item_kwargs["encode_type"] = 6
            item_kwargs["sample_rate"] = 24000
            item_kwargs["bits_per_sample"] = 16
        media_item = item_builder(**item_kwargs)

        last_message_id = None
        if caption:
            last_message_id = f"encre-weixin-{uuid.uuid4().hex}"
            await _send_message(
                self._send_session,
                base_url=self._base_url,
                token=self._token,
                to=chat_id,
                text=self.format_message(caption),
                context_token=context_token,
                client_id=last_message_id,
            )

        last_message_id = f"encre-weixin-{uuid.uuid4().hex}"
        await _api_post(
            self._send_session,
            base_url=self._base_url,
            endpoint=EP_SEND_MESSAGE,
            payload={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": chat_id,
                    "client_id": last_message_id,
                    "message_type": MSG_TYPE_BOT,
                    "message_state": MSG_STATE_FINISH,
                    "item_list": [media_item],
                    **({"context_token": context_token} if context_token else {}),
                }
            },
            token=self._token,
            timeout_ms=API_TIMEOUT_MS,
        )
        return last_message_id

    def _outbound_media_builder(self, path: str, force_file_attachment: bool = False):
        """Choose the iLink media shape and build its message item.

        Returns a pair of the ``media_type`` discriminant for
        ``getuploadurl`` and a callable that assembles the ``item_list``
        element. Returning a builder rather than a dict keeps the shared
        ``encrypt_query_param``/``aes_key`` wiring in one place, so the four
        shapes cannot drift apart.

        Shape selection: MIME type wins for image and video, ``.silk`` maps to
        a native voice bubble unless downgraded, and everything else becomes a
        generic file.

        Args:
            path: Local file path.
            force_file_attachment: Downgrade ``.silk`` to a file attachment.

        Returns:
            A ``(media_type, item_builder)`` pair.
        """
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if mime.startswith("image/"):
            return MEDIA_IMAGE, lambda **kw: {
                "type": ITEM_IMAGE,
                "image_item": {
                    "media": {
                        "encrypt_query_param": kw["encrypt_query_param"],
                        "aes_key": kw["aes_key_for_api"],
                        "encrypt_type": 1,
                    },
                    "mid_size": kw["ciphertext_size"],
                },
            }
        if mime.startswith("video/"):
            return MEDIA_VIDEO, lambda **kw: {
                "type": ITEM_VIDEO,
                "video_item": {
                    "media": {
                        "encrypt_query_param": kw["encrypt_query_param"],
                        "aes_key": kw["aes_key_for_api"],
                        "encrypt_type": 1,
                    },
                    "video_size": kw["ciphertext_size"],
                    "play_length": kw.get("play_length", 0),
                    "video_md5": kw.get("rawfilemd5", ""),
                },
            }
        if path.endswith(".silk") and not force_file_attachment:
            return MEDIA_VOICE, lambda **kw: {
                "type": ITEM_VOICE,
                "voice_item": {
                    "media": {
                        "encrypt_query_param": kw["encrypt_query_param"],
                        "aes_key": kw["aes_key_for_api"],
                        "encrypt_type": 1,
                    },
                    "encode_type": kw.get("encode_type"),
                    "bits_per_sample": kw.get("bits_per_sample"),
                    "sample_rate": kw.get("sample_rate"),
                    "playtime": kw.get("playtime", 0),
                },
            }
        if mime.startswith("audio/"):
            return MEDIA_FILE, lambda **kw: {
                "type": ITEM_FILE,
                "file_item": {
                    "media": {
                        "encrypt_query_param": kw["encrypt_query_param"],
                        "aes_key": kw["aes_key_for_api"],
                        "encrypt_type": 1,
                    },
                    "file_name": kw["filename"],
                    "len": str(kw["plaintext_size"]),
                },
            }
        return MEDIA_FILE, lambda **kw: {
            "type": ITEM_FILE,
            "file_item": {
                "media": {
                    "encrypt_query_param": kw["encrypt_query_param"],
                    "aes_key": kw["aes_key_for_api"],
                    "encrypt_type": 1,
                },
                "file_name": kw["filename"],
                "len": str(kw["plaintext_size"]),
            },
        }

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Report minimal chat metadata for a peer.

        iLink exposes no chat metadata endpoint, so the type is inferred from
        the well-known room suffix and the name falls back to the id itself.

        Args:
            chat_id: Peer id to describe.

        Returns:
            A mapping with ``name``, ``type`` and ``chat_id``.
        """
        chat_type = "group" if chat_id.endswith("@chatroom") else "dm"
        return {"name": chat_id, "type": chat_type, "chat_id": chat_id}

    def format_message(self, content: Optional[str]) -> str:
        """Apply the WeChat-specific display normalisation to outbound text.

        Composes block normalisation with line wrapping so the text arrives
        already shaped for a mobile chat surface. ``None`` collapses to an
        empty string rather than the literal word ``None``.

        Args:
            content: Raw assistant output.

        Returns:
            The display-ready text.
        """
        if content is None:
            return ""
        return _wrap_copy_friendly_lines_for_weixin(_normalize_markdown_blocks(content))


async def send_weixin_direct(
    *,
    extra: Dict[str, Any],
    token: Optional[str],
    chat_id: str,
    message: str,
    media_files: Optional[List[Tuple[str, bool]]] = None,
) -> Dict[str, Any]:
    """
    One-shot send helper for ``send_message`` and cron delivery.

    This bypasses the long-poll adapter lifecycle and uses the raw API directly.

    Two execution strategies, chosen by availability:

    - When a live adapter for this token is registered in
      :data:`_LIVE_ADAPTERS` and its send session is bound to the currently
      running loop, delivery reuses that adapter. This preserves its
      ``context_token`` cache and avoids opening a second iLink session, which
      the server treats as a duplicate login.
    - Otherwise a throwaway adapter is constructed with a fresh
      ``ClientSession`` and the given credentials injected directly.

    Text is delivered before media in the live-adapter branch and in the
    same order in the fallback branch, so both paths behave identically for
    consumers that only inspect the returned dict.

    Args:
        extra: Platform extra config, supplying account and host overrides.
        token: Bearer token, taking precedence over the extra and env forms.
        chat_id: Destination peer.
        message: Text to send.
        media_files: Optional ``(path, is_voice)`` attachment pairs.

    Returns:
        A result dict with ``success``/``platform``/``chat_id``/``message_id``
        and ``context_token_used``, or ``{"error": ...}`` on failure.
    """
    account_id = str(extra.get("account_id") or get_secret("WEIXIN_ACCOUNT_ID", "")).strip()
    base_url = str(extra.get("base_url") or get_secret("WEIXIN_BASE_URL", ILINK_BASE_URL)).strip().rstrip("/")
    cdn_base_url = str(extra.get("cdn_base_url") or get_secret("WEIXIN_CDN_BASE_URL", WEIXIN_CDN_BASE_URL)).strip().rstrip("/")
    resolved_token = str(token or extra.get("token") or get_secret("WEIXIN_TOKEN", "")).strip()
    if not resolved_token:
        return {"error": "Weixin token missing. Configure WEIXIN_TOKEN or platforms.weixin.token."}
    if not account_id:
        return {"error": "Weixin account ID missing. Configure WEIXIN_ACCOUNT_ID or platforms.weixin.extra.account_id."}

    token_store = ContextTokenStore(str(get_data_dir()))
    token_store.restore(account_id)
    context_token = token_store.get(account_id, chat_id)

    # Reuse a running adapter only when its session belongs to this event loop;
    # a session bound to a different loop would raise on first await.
    live_adapter = _LIVE_ADAPTERS.get(resolved_token)
    send_session = getattr(live_adapter, '_send_session', None)
    if (live_adapter is not None and send_session is not None
            and not send_session.closed
            and send_session._loop is asyncio.get_running_loop()):
        last_result: Optional[SendResult] = None
        cleaned = live_adapter.format_message(message)
        if cleaned:
            last_result = await live_adapter.send(chat_id, cleaned)
            if not last_result.success:
                return {"error": f"Weixin send failed: {last_result.error}"}

        for media_path, _is_voice in media_files or []:
            ext = Path(media_path).suffix.lower()
            if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
                last_result = await live_adapter.send_image_file(chat_id, media_path)
            else:
                last_result = await live_adapter.send_document(chat_id, media_path)
            if not last_result.success:
                return {"error": f"Weixin media send failed: {last_result.error}"}

        return {
            "success": True,
            "platform": "weixin",
            "chat_id": chat_id,
            "message_id": last_result.message_id if last_result else None,
            "context_token_used": bool(context_token),
        }

    # Fallback: build a disposable adapter with the session and credentials
    # injected, so this works without a connected gateway process.
    async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
        adapter = WeixinAdapter(
            PlatformConfig(
                enabled=True,
                token=resolved_token,
                extra={
                    **dict(extra or {}),
                    "account_id": account_id,
                    "base_url": base_url,
                    "cdn_base_url": cdn_base_url,
                },
            )
        )
        adapter._send_session = session
        adapter._session = session
        adapter._token = resolved_token
        adapter._account_id = account_id
        adapter._base_url = base_url
        adapter._cdn_base_url = cdn_base_url
        adapter._token_store = token_store

        last_result: Optional[SendResult] = None
        cleaned = adapter.format_message(message)
        if cleaned:
            last_result = await adapter.send(chat_id, cleaned)
            if not last_result.success:
                return {"error": f"Weixin send failed: {last_result.error}"}

        for media_path, _is_voice in media_files or []:
            ext = Path(media_path).suffix.lower()
            if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
                last_result = await adapter.send_image_file(chat_id, media_path)
            else:
                last_result = await adapter.send_document(chat_id, media_path)
            if not last_result.success:
                return {"error": f"Weixin media send failed: {last_result.error}"}

        return {
            "success": True,
            "platform": "weixin",
            "chat_id": chat_id,
            "message_id": last_result.message_id if last_result else None,
            "context_token_used": bool(context_token),
        }


# ------------------------------------------------------------------
# Platform registration
# ------------------------------------------------------------------
#
# The legacy bottom-of-module PlatformEntry registration moved to
# ``plugin.py``; this module only keeps the availability probe the spec
# reuses.


def _check_requirements() -> bool:
    """Registry-level dependency check for the Weixin adapter.

    Deliberately distinct from :func:`check_weixin_requirements` because the
    gateway-adapter surface imports this module before some of the rest of
    the gateway is initialised, and the check must stay dependency-free to
    avoid circular import surprises.

    Returns:
        ``True`` when both runtime dependencies are importable.
    """
    return AIOHTTP_AVAILABLE and CRYPTO_AVAILABLE
