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

"""Signal messenger adapter for the Encre gateway layer.

This module is one concrete adapter inside :mod:`encre.gateway.platforms`, so
it implements :class:`encre.gateway.platforms.base.BasePlatformAdapter` and
is instantiated by :class:`encre.gateway.run.GatewayRunner` through the
:class:`encre.gateway.platform_registry.PlatformEntry` registered at the
bottom of this file.

Transport shape: Encre does not speak the Signal protocol itself. A separate
``signal-cli`` process runs in HTTP mode (``signal-cli daemon --http
127.0.0.1:8080``) and this adapter talks to it over two HTTP surfaces:

* inbound -- a long-lived SSE stream on ``GET /api/v1/events`` owned by
  :meth:`SignalAdapter._sse_listener`, which parses envelopes and turns each
  one into a :class:`~encre.gateway.platforms.base.MessageEvent`;
* outbound -- JSON-RPC 2.0 calls on ``POST /api/v1/rpc`` made through the
  single helper :meth:`SignalAdapter._rpc` for ``send``, ``sendTyping``,
  ``sendReaction``, ``getAttachment``, ``getContact`` and ``listContacts``.

Data flow through the adapter:

  SSE envelope -> :meth:`SignalAdapter._handle_envelope` ->
  :class:`~encre.gateway.platforms.base.MessageEvent` ->
  ``BasePlatformAdapter.handle_message`` -> ``GatewayRunner`` session
  resolution -> agent execution. Responses return the other way: the agent's
  reply is dispatched by ``GatewayStreamConsumer`` through this adapter's
  ``send``/``send_image``/``send_document`` methods, which return
  :class:`~encre.gateway.platforms.base.SendResult` values. Because Signal has
  no edit API for sent messages, every ``SendResult.message_id`` here is
  ``None`` and the stream consumer stays on its non-edit fallback path.

Module-level helpers own the signal-cli-specific parsing that does not belong
on the adapter: magic-byte sniffing for attachment extensions, E.164 / ACI-PNI
identifier detection, mention rendering, and the ffmpeg remux step that turns
Android voice notes into containers STT APIs accept. Rate-limit constants and
the pacing scheduler live in the sibling module
:mod:`encre.gateway.platforms.signal_rate_limit`; markdown-to-Signal body-range
conversion lives in :mod:`encre.gateway.platforms.signal_format`.

Runtime requirements: a reachable ``signal-cli`` HTTP daemon plus the
``SIGNAL_HTTP_URL`` and ``SIGNAL_ACCOUNT`` values, supplied either through
``PlatformConfig.extra`` or the environment.

Public API:
    * :class:`SignalAdapter`
    * :func:`check_signal_requirements`
    * :func:`validate_signal_config`
    * :func:`_remux_aac_to_m4a` -- shared remux helper, also used by other
      adapters that receive ADTS AAC audio
"""

import asyncio
import base64
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

import httpx

from encre.gateway.config import Platform, PlatformConfig
from encre.tools._encoding import decode_bytes
from encre.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    cache_image_from_bytes,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_url,
)
from encre.gateway.platforms.helpers import redact_phone
from .signal_format import markdown_to_signal
from .signal_rate_limit import (
    SIGNAL_BATCH_PACING_NOTICE_THRESHOLD,
    SIGNAL_MAX_ATTACHMENTS_PER_MSG,
    SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
    SignalRateLimitError,
    _extract_retry_after_seconds,
    _format_wait,
    _is_signal_rate_limit_error,
    _signal_send_timeout,
    get_scheduler,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hard ceiling on a single inbound attachment. signal-cli returns the whole
# file as one base64 string over HTTP, so oversized blobs would be decoded
# into memory before the cache helpers ever see them; rejecting early in
# _handle_envelope avoids that allocation entirely.
SIGNAL_MAX_ATTACHMENT_SIZE = 100 * 1024 * 1024  # 100 MB

# signal-cli's own per-message text cap. Agent output is split upstream by the
# stream consumer, so this value is retained as the documented contract for
# callers that build messages outside that path.
MAX_MESSAGE_LENGTH = 8000  # Signal message size limit

# Interval used by the typing-indicator refresh loop inherited from the base
# adapter. Signal treats typing as a decaying indicator rather than a
# persistent flag, so it must be re-asserted on this cadence.
TYPING_INTERVAL = 8.0  # seconds between typing indicator refreshes

# Exponential backoff window for the SSE listener. Each failure doubles the
# delay up to the cap, and 20% jitter is added at sleep time so many adapters
# reconnecting after a shared daemon outage do not arrive simultaneously.
SSE_RETRY_DELAY_INITIAL = 2.0
SSE_RETRY_DELAY_MAX = 60.0

# The health monitor wakes on this cadence and compares it against the last
# SSE activity timestamp. Because signal-cli sends ":" keepalive comments on
# an idle stream, activity freshness does not imply message traffic.
HEALTH_CHECK_INTERVAL = 30.0  # seconds between health checks

# Beyond this much silence the monitor probes /api/v1/check and force-closes
# the SSE stream when the daemon answers non-200, so a half-dead socket does
# not silently drop inbound messages.
HEALTH_CHECK_STALE_THRESHOLD = 120.0  # seconds without SSE activity before concern


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_comma_list(value: str) -> List[str]:
    """Split a comma-separated configuration string into cleaned tokens.

    Allowlist style environment values arrive as ``"+15511234567,+4479..."``
    and may contain padding around the separators. Empty tokens are dropped so
    a trailing comma does not create a bogus allowlist entry.

    Args:
        value: Raw configuration string, possibly empty.

    Returns:
        Ordered list of stripped tokens. Empty input yields an empty list.
    """
    return [v.strip() for v in value.split(",") if v.strip()]


def _guess_extension(data: bytes) -> str:
    """Guess a file extension from magic bytes.

    signal-cli returns attachments as bare base64 with no reliable filename,
    so the extension must be derived from the payload itself. It feeds both
    the cache helper selection in :meth:`SignalAdapter._fetch_attachment` and
    the MIME mapping in :func:`_ext_to_mime`.

    Android Signal delivers voice notes as raw ADTS AAC frames, which share
    the ``0xFF 0xFx`` sync word with MPEG-1/2 Layer 3 (MP3). The byte-1
    layout disambiguates: ADTS packs ``ID layer protection_absent`` into
    bits 3-0, where ``ID`` is 0 for MPEG-2/4 AAC and ``layer`` is always
    0 for ADTS. A real MP3 frame has ``ID=1`` and ``layer`` in {1, 2, 3}.

    Args:
        data: Raw attachment bytes. May be shorter than some magic numbers
            require; each check that needs extra context guards its own length.

    Returns:
        Lower-case extension including the leading dot, or ``".bin"`` when no
        signature matches.
    """
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"%PDF":
        return ".pdf"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return ".mp4"
    if data[:4] == b"OggS":
        return ".ogg"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        # ``0xFF 0xFx`` is shared by MP3 and ADTS AAC. The discriminator is
        # bits 7-4 and 2-1 of byte 1 (mask 0xF6). ADTS pins ID=00 and
        # layer=00, so the byte lands exactly on 0xF0; a real MP3 frame has
        # ID=1 and layer in {01,10,11}, i.e. 0xF2 / 0xF4 / 0xF6. Testing for
        # the ADTS shape first keeps an AAC voice note from being cached as an
        # MP3, which would route it to the wrong player and the wrong STT input.
        if (data[1] & 0xF6) == 0xF0:
            return ".aac"
        return ".mp3"
    if data[:2] == b"PK":
        return ".zip"
    return ".bin"


def _is_image_ext(ext: str) -> bool:
    """Return True for extensions the cache layer stores as images.

    The result selects :func:`cache_image_from_bytes` in
    :meth:`SignalAdapter._fetch_attachment`. Case-insensitive so extensions
    guessed from magic bytes and extensions supplied by the caller agree.

    Args:
        ext: Extension including the leading dot.

    Returns:
        True when *ext* is one of the image formats Signal treats as photos.
    """
    return ext.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _is_audio_ext(ext: str) -> bool:
    """Return True for extensions the cache layer stores as audio.

    The result selects :func:`cache_audio_from_bytes` in
    :meth:`SignalAdapter._fetch_attachment`, which is the path that leads to
    speech-to-text. Note ``.bin`` deliberately falls through to the document
    branch, so an unrecognised attachment is never mis-routed into audio.

    Args:
        ext: Extension including the leading dot.

    Returns:
        True when *ext* is a recognised audio container.
    """
    return ext.lower() in {".mp3", ".wav", ".ogg", ".m4a", ".aac"}


# Extension to MIME mapping for attachments. signal-cli does report a
# contentType per attachment, but that field is absent or empty on many
# message types, so this table is the fallback used by
# :meth:`SignalAdapter._handle_envelope` when deciding the inbound
# MessageType. Values are kept to the small set _guess_extension can produce.
_EXT_TO_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
    ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".mp4": "video/mp4", ".pdf": "application/pdf", ".zip": "application/zip",
}


def _ext_to_mime(ext: str) -> str:
    """Map a file extension to the MIME type Signal expects for it.

    Args:
        ext: Extension including the leading dot.

    Returns:
        The matching MIME type, or ``"application/octet-stream"`` for
        anything unmapped so an unknown file still reaches the agent as a
        document rather than failing the send.
    """
    return _EXT_TO_MIME.get(ext.lower(), "application/octet-stream")


def _remux_aac_to_m4a(aac_data: bytes) -> Optional[Tuple[bytes, str]]:
    """Losslessly remux raw ADTS AAC bytes into an MP4 (".m4a") container.

    Android Signal delivers voice notes as bare ADTS AAC frames, which most
    speech-to-text providers refuse: they need the AAC payload muxed into an
    MP4 container. ``ffmpeg -c:a copy`` only demuxes and remuxes, so no codec
    re-encode happens and audio quality stays bit-identical; typical
    voice-note sizes complete in well under a second.

    This helper is invoked off the event loop by
    :meth:`SignalAdapter._fetch_attachment` through ``asyncio.to_thread``,
    because it blocks on a subprocess plus two file writes.

    Both temporary files are created with ``tempfile.NamedTemporaryFile`` and
    removed in a ``finally`` block, so the on-disk artifacts disappear on
    success, on remux failure, and on timeout alike.

    Returns:
        A ``(m4a_bytes, ".m4a")`` pair on success, or ``None`` when ``ffmpeg``
        cannot be found, the input is not valid ADTS, or the remux fails for
        any other reason. Callers must treat ``None`` as "cache the raw ADTS
        bytes unchanged" and must never raise for it.

    Note:
        ``ffmpeg`` is located with :func:`shutil.which` first, then by probing
        the two common macOS Homebrew prefixes.

    Raises:
        Nothing. Every failure mode is logged and folded into ``None``.
    """
    # shutil.which only consults PATH. Minimal CI images and fresh macOS
    # dev hosts frequently have ffmpeg in the Homebrew prefix without a link
    # into PATH, so probe the two canonical locations before giving up.
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        for prefix in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
            if os.path.isfile(prefix) and os.access(prefix, os.X_OK):
                ffmpeg = prefix
                break
    if not ffmpeg:
        logger.debug("Signal: ffmpeg not found, skipping AAC鈫扢4A remux")
        return None
    # delete=False keeps the source readable after the with-block closes,
    # because ffmpeg reopens it by path in its own address space.
    try:
        with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as src:
            src.write(aac_data)
            src_path = src.name
        # Output lands next to the input with only the suffix changed; both
        # paths are cleaned up below regardless of which branch ran.
        dst_path = src_path[:-4] + ".m4a"
        try:
            # -c:a copy is the whole point: stream copy only, never re-encode.
            # +faststart moves the moov atom to the front so players can begin
            # playback before the container is fully written.
            proc = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", src_path,
                 "-c:a", "copy", "-movflags", "+faststart", dst_path],
                capture_output=True, timeout=10,
            )
            if proc.returncode != 0:
                logger.warning(
                    "Signal: AAC鈫扢4A remux failed (ffmpeg exit %d): %s",
                    proc.returncode, decode_bytes(proc.stderr)[:300],
                )
                return None
            with open(dst_path, "rb") as f:
                return f.read(), ".m4a"
        finally:
            # unlink is best-effort and guarded: a failure to remove a temp
            # file here must not mask the remux result or the error already
            # being reported by the caller.
            for p in (src_path, dst_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
    except subprocess.TimeoutExpired:
        logger.warning("Signal: AAC鈫扢4A remux timed out (>10s)")
        return None
    except Exception:
        logger.exception("Signal: AAC鈫扢4A remux error")
        return None


def _render_mentions(text: str, mentions: list) -> str:
    """Replace Signal mention placeholders with readable @identifiers.

    Signal encodes @mentions as the Unicode object replacement character
    (U+FFFC) with the mention metadata delivered out-of-band in a parallel
    ``mentions`` array carrying ``start`` / ``length`` / ``number`` or
    ``uuid``. Left untouched the body would show a tofu box where a name
    belongs, so the agent-visible text gets a stable ``@<identifier>`` form.

    Args:
        text: Raw message body containing U+FFFC placeholders.
        mentions: List of mention dicts from the signal-cli envelope.

    Returns:
        The body with every placeholder substituted, unchanged when there are
        no mentions or no placeholder present.
    """
    if not mentions or "\uFFFC" not in text:
        return text
    # Sort mentions by start position (reverse) to replace from end to start
    # so indices don't shift as we replace
    sorted_mentions = sorted(mentions, key=lambda m: m.get("start", 0), reverse=True)
    for mention in sorted_mentions:
        start = mention.get("start", 0)
        length = mention.get("length", 1)
        # Use the mention's number or UUID as the replacement
        identifier = mention.get("number") or mention.get("uuid") or "user"
        replacement = f"@{identifier}"
        text = text[:start] + replacement + text[start + length:]
    return text


def _is_signal_service_id(value: str) -> bool:
    """Return True if *value* already looks like a Signal service identifier.

    signal-cli addresses recipients either by E.164 phone number or by a
    server-issued service ID. Service IDs come in three shapes and all of them
    are unambiguous: the ``PNI:`` prefix (public number ID), the ``u:``
    prefix (UUID shorthand used by the daemon), or a bare RFC-4122 UUID.
    Recognising these early prevents a phone-number upgrade attempt from being
    run against a value that is already in canonical form.

    Args:
        value: Candidate identifier string.

    Returns:
        True when *value* matches any known service-ID shape. Empty and
        malformed input returns False rather than raising.
    """
    if not value:
        return False
    if value.startswith("PNI:") or value.startswith("u:"):
        return True
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _looks_like_e164_number(value: str) -> bool:
    """Return True for a plausible E.164 phone number.

    Used as a cheap pre-filter by :meth:`SignalAdapter._resolve_recipient` so
    the ``listContacts`` RPC is only attempted when the chat id is a bare
    number worth upgrading. The length window (7 to 15 digits after the plus)
    matches the ITU-T E.164 maximum while still accepting short national
    formats some clients emit.

    Args:
        value: Candidate chat id.

    Returns:
        True only for a leading ``+`` followed by an in-range digit string.
    """
    if not value or not value.startswith("+"):
        return False
    digits = value[1:]
    return digits.isdigit() and 7 <= len(digits) <= 15


def check_signal_requirements() -> bool:
    """Report whether the runtime can even import the Signal transport deps.

    Wired into the :class:`encre.gateway.platform_registry.PlatformEntry` as
    ``check_fn`` so the gateway can advertise the platform as unavailable
    instead of failing at adapter construction time. The heavy dependency
    (``httpx``) is verified here; the external ``signal-cli`` daemon itself is
    probed later by :meth:`SignalAdapter.connect` through the health endpoint.

    Returns:
        True always for now: ``httpx`` is a hard import of this module, so a
        missing wheel would already have raised at import time. Kept as an
        explicit gate so the registry contract stays uniform across adapters.
    """
    return True


def validate_signal_config(config: PlatformConfig) -> bool:
    """Check if Signal has enough config to connect.

    Both the daemon URL and the account identity are mandatory; either can be
    supplied through ``PlatformConfig.extra`` or, failing that, through the
    ``SIGNAL_HTTP_URL`` / ``SIGNAL_ACCOUNT`` environment variables. Extra
    takes precedence so a config file can override a stale environment value.

    Args:
        config: Platform configuration for the Signal section.

    Returns:
        True when both the URL and the account resolve to a non-empty value.
    """
    extra = getattr(config, "extra", {}) or {}
    http_url = (extra.get("http_url", "") or os.getenv("SIGNAL_HTTP_URL", "")).strip()
    account = (extra.get("account", "") or os.getenv("SIGNAL_ACCOUNT", "")).strip()
    return bool(http_url and account)


# ---------------------------------------------------------------------------
# Signal Adapter
# ---------------------------------------------------------------------------

class SignalAdapter(BasePlatformAdapter):
    """Signal messenger adapter over a signal-cli HTTP daemon.

    One instance wraps one ``signal-cli`` daemon for one account. It owns a
    single ``httpx.AsyncClient`` for the adapter's whole lifetime, an SSE task
    that turns inbound envelopes into
    :class:`~encre.gateway.platforms.base.MessageEvent` values, and a health
    monitor that force-reconnects a half-dead stream.

    Lifecycle: constructed by the registry entry at the bottom of this file,
    ``connect()`` starts both background tasks, ``disconnect()`` cancels them
    and closes the client. All state is process-local and file-free.

    Thread/async safety: every method is async and all mutable state is touched
    only from the gateway event loop. The one cross-coroutine shared structure
    is the recipient identifier cache, which is guarded by
    :attr:`_recipient_cache_lock` so concurrent outbound sends do not issue
    duplicate ``listContacts`` calls.

    Chat addressing convention used everywhere in this class: a group chat id
    is ``"group:<signal group id>"`` and a direct chat id is an E.164 number
    or a Signal service ID. The ``group:`` prefix is what routes a send to the
    ``groupId`` parameter instead of ``recipient``.

    Invariants maintained by the echo-suppression caches:

    * :attr:`_recent_sent_timestamps` holds the envelope timestamps of messages
      this adapter just sent, so the self-sync echo of our own outbound reply
      is discarded instead of being processed as user input.
    * :attr:`_sent_message_timestamps` is a longer-lived, quote-oriented copy
      of the same data used to mark ``reply_to_is_own_message`` on inbound
      quotes.

    Collaborators: :mod:`encre.gateway.platforms.signal_rate_limit` supplies
    the pacing scheduler and backoff constants, and
    :mod:`encre.gateway.platforms.signal_format` owns markdown conversion.

    Attributes:
        platform: The :class:`encre.gateway.config.Platform` discriminator.
        SUPPORTS_MESSAGE_EDITING: Always False -- see the note above the flag.
        http_url: Base URL of the signal-cli HTTP daemon, trailing slash removed.
        account: Account identity used for every RPC ``params["account"]``.
        ignore_stories: When True, story envelopes are dropped at the top of
            :meth:`_handle_envelope`.
        group_allow_from: Group ids permitted to trigger processing; empty
            means group messages are disabled entirely.
        require_mention: When True, group messages are ignored unless they
            mention this bot account.
        dm_allow_from: Direct-chat allowlist used to gate reaction hooks.
        client: Shared ``httpx.AsyncClient``; None until ``connect()``.
        _typing_tasks: Per-chat typing refresh tasks.
        _typing_failures: Consecutive sendTyping failures per chat.
        _typing_skip_until: Monotonic cooldown deadline per chat.
        _running: Whether the background tasks should keep looping.
        _last_sse_activity: Wall-clock freshness of the SSE stream.
        _sse_response: Current open SSE response, used to force a reconnect.
        _account_normalized: Trimmed account string for self-message filtering.
        _recent_sent_timestamps: Recent outbound timestamps mapped to their
            insertion time for echo suppression.
        _sent_message_timestamps: Bounded set-like cache of outbound timestamps
            used for quote ownership matching.
        _recipient_uuid_by_number: E.164 number to service ID upgrade map.
        _recipient_number_by_uuid: Reverse of the above.
        _recipient_cache_lock: Guards lazy contact resolution.
    """

    platform = Platform.SIGNAL
    # Signal has no real edit API for already-sent messages. Mark it explicitly
    # so streaming suppresses the visible cursor instead of leaving a stale tofu
    # square behind in chat clients when edit attempts fail.
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config: PlatformConfig):
        """Initialise adapter state from platform config and environment.

        No I/O happens here: the daemon is not contacted until ``connect()``.
        Construction is pure bookkeeping so the registry factory can build an
        adapter cheaply and defer all network work.

        Configuration precedence is config ``extra`` first, then the
        environment, matching the pattern used by the sibling adapters. Group
        and DM allowlists, and the mention requirement, are read from the
        environment because they are operator-facing runtime policy rather
        than per-chat data.

        Args:
            config: Platform configuration. ``config.extra`` may carry
                ``http_url``, ``account``, ``ignore_stories`` and
                ``require_mention``.

        Raises:
            Nothing directly; a misconfigured daemon surfaces later as a
            ``False`` return from :meth:`connect`.
        """
        super().__init__(config, Platform.SIGNAL)

        extra = config.extra or {}
        self.http_url = extra.get("http_url", "http://127.0.0.1:8080").rstrip("/")
        self.account = extra.get("account", "")
        self.ignore_stories = extra.get("ignore_stories", True)

        # Parse allowlists -- group policy is derived from presence of group allowlist
        group_allowed_str = os.getenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        self.group_allow_from = set(_parse_comma_list(group_allowed_str))

        # Mention filter -- only respond in groups when the bot account is @mentioned.
        # Read from config extra first, then SIGNAL_REQUIRE_MENTION env var.
        _rm_cfg = extra.get("require_mention")
        if _rm_cfg is not None:
            self.require_mention = bool(_rm_cfg)
        else:
            self.require_mention = os.getenv("SIGNAL_REQUIRE_MENTION", "false").lower() in ("true", "1", "yes", "on")

        # DM allowlist -- mirrors SIGNAL_ALLOWED_USERS checked by run.py.
        # Stored here so the reaction hooks can skip unauthorized senders
        # (reactions fire before run.py's auth gate, so without this check
        # every inbound DM from any contact gets an in-progress reaction).
        # "*" means all users allowed (open mode); empty means no restriction
        # recorded at adapter level (run.py still enforces auth separately).
        dm_allowed_str = os.getenv("SIGNAL_ALLOWED_USERS", "*")
        self.dm_allow_from = set(_parse_comma_list(dm_allowed_str))

        # HTTP client. Lazily created in connect() and shared across every RPC
        # and the SSE stream so the TLS/TCP setup cost is paid exactly once.
        self.client: Optional[httpx.AsyncClient] = None

        # Background tasks. Each is created in connect() and cancelled in
        # disconnect(); the typing dict is keyed by chat id because the base
        # adapter keeps a refresh loop alive for one chat at a time.
        self._sse_task: Optional[asyncio.Task] = None
        self._health_monitor_task: Optional[asyncio.Task] = None
        self._typing_tasks: Dict[str, asyncio.Task] = {}
        # Per-chat typing-indicator backoff. When signal-cli reports
        # NETWORK_FAILURE (recipient offline / unroutable), base.py's
        # _keep_typing refresh loop would otherwise hammer sendTyping every
        # ~2s indefinitely, producing WARNING-level log spam and pointless
        # RPC traffic. We track consecutive failures per chat and skip the
        # RPC during a cooldown window instead.
        self._typing_failures: Dict[str, int] = {}
        self._typing_skip_until: Dict[str, float] = {}
        self._running = False
        self._last_sse_activity = 0.0
        self._sse_response: Optional[httpx.Response] = None

        # Normalize account for self-message filtering. Comparison against the
        # envelope's sourceNumber must be exact, so strip once up front rather
        # than on every inbound envelope.
        self._account_normalized = self.account.strip()

        # Track recently sent message timestamps to prevent echo-back loops
        # in Note to Self / self-chat mode and linked-device group sync-sents.
        # OrderedDict[timestamp_ms -> insertion_monotonic_seconds] gives us
        # LRU eviction (popitem(last=False) drops oldest) plus a TTL so that
        # under chatty groups a still-pending echo cannot be evicted just
        # because >50 outbounds happened. With a 5-minute TTL the cap only
        # matters for runaway producers, not normal traffic bursts.
        self._recent_sent_timestamps: "OrderedDict[int, float]" = OrderedDict()
        self._max_recent_timestamps = 512
        self._recent_sent_ttl_seconds = 300.0
        # Keep a separate bounded cache of outbound Signal message timestamps.
        # Signal quote.id is the timestamp of the quoted message, so this lets
        # inbound replies identify that the user replied to a message sent by
        # this bot even after the self-sync echo was filtered above.
        # OrderedDict (not set) so the cap evicts the OLDEST timestamp in FIFO
        # order -- a plain set.pop() removes an arbitrary element, which could
        # drop a still-recent timestamp and miss a genuine reply-to-own-message.
        self._sent_message_timestamps: "OrderedDict[str, None]" = OrderedDict()
        self._max_sent_message_timestamps = 500
        # Signal increasingly exposes ACI/PNI UUIDs as stable recipient IDs.
        # Keep a best-effort mapping so outbound sends can upgrade from a
        # phone number to the corresponding UUID when signal-cli prefers it.
        self._recipient_uuid_by_number: Dict[str, str] = {}
        self._recipient_number_by_uuid: Dict[str, str] = {}
        self._recipient_cache_lock = asyncio.Lock()

        logger.info("Signal adapter initialized: url=%s account=%s groups=%s",
                     self.http_url, redact_phone(self.account),
                     "enabled" if self.group_allow_from else "disabled")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to the signal-cli daemon and start the SSE listener.

        Establishes the shared ``httpx.AsyncClient``, verifies the daemon with
        a ``GET /api/v1/check`` round trip, then spawns the two background
        tasks that drive the adapter for its remaining lifetime: the SSE
        inbound stream and the health monitor.

        Args:
            is_reconnect: Indicates this call follows a previous disconnect.
                Kept for signature compatibility with the base adapter; the
                current implementation treats both paths identically because
                all per-connection state is re-created here.

        Returns:
            True when the daemon answered the health check and both tasks
            started. False when credentials are missing or the daemon is
            unreachable, in which case the client is closed again before
            returning.
        """
        if not self.http_url or not self.account:
            logger.error("Signal: SIGNAL_HTTP_URL and SIGNAL_ACCOUNT are required")
            return False

        # Tighter keepalive so idle CLOSE_WAIT drains promptly (#18451).
        from encre.gateway.platforms._http_client_limits import platform_httpx_limits
        self.client = httpx.AsyncClient(timeout=30.0, limits=platform_httpx_limits())
        try:
            # Health check -- verify signal-cli daemon is reachable
            try:
                resp = await self.client.get(f"{self.http_url}/api/v1/check", timeout=10.0)
                if resp.status_code != 200:
                    logger.error("Signal: health check failed (status %d)", resp.status_code)
                    return False
            except Exception as e:
                logger.error("Signal: cannot reach signal-cli at %s: %s", self.http_url, e)
                return False

            # _running is set before the tasks are created so their while
            # conditions see True on the very first iteration and neither one
            # exits immediately after being scheduled.
            self._running = True
            self._last_sse_activity = time.time()
            self._sse_task = asyncio.create_task(self._sse_listener())
            self._health_monitor_task = asyncio.create_task(self._health_monitor())

            logger.info("Signal: connected to %s", self.http_url)
            return True
        finally:
            # Only clean up the client when we did not reach the success path;
            # on success it must stay open for the whole adapter lifetime.
            if not self._running:
                if self.client:
                    await self.client.aclose()
                    self.client = None

    async def disconnect(self) -> None:
        """Stop the SSE listener and release every adapter resource.

        Cancellation is ordered so that no background task can issue an RPC
        through a closed client: both long-lived tasks are cancelled and
        awaited first, then the typing refresh tasks, and only then the HTTP
        client is closed.

        Returns:
            None.

        Note:
            Awaiting a cancelled task and swallowing its
            ``asyncio.CancelledError`` is deliberate -- otherwise the
            cancellation would propagate out of ``disconnect()`` and abort the
            runner's shutdown sequence.
        """
        self._running = False

        if self._sse_task:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass

        if self._health_monitor_task:
            self._health_monitor_task.cancel()
            try:
                await self._health_monitor_task
            except asyncio.CancelledError:
                pass

        # Cancel all typing tasks. Each one is an infinite refresh loop that
        # would otherwise keep firing sendTyping against a closed client.
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()

        if self.client:
            await self.client.aclose()
            self.client = None

        logger.info("Signal: disconnected")

    # ------------------------------------------------------------------
    # SSE Streaming (inbound messages)
    # ------------------------------------------------------------------

    async def _sse_listener(self) -> None:
        """Listen for SSE events from the signal-cli daemon.

        Runs forever while :attr:`_running` is true. Each iteration opens a
        streaming ``GET /api/v1/events`` connection, feeds every ``data:``
        line through ``json.loads`` and into :meth:`_handle_envelope`, and
        reconnects with exponential backoff on any transport failure.

        The account is URL-quoted with ``safe=''`` because E.164 numbers
        contain ``+``, which would otherwise be parsed as a form separator.

        Returns:
            None. Only returns once ``_running`` flips false or the task is
            cancelled.

        Note:
            The stream is consumed line by line from a rolling buffer because
            httpx chunks do not respect SSE event boundaries. Signal keepalive
            comments (lines starting with ``:``) carry no payload but do prove
            the socket is alive, so they refresh
            :attr:`_last_sse_activity` as well.
        """
        url = f"{self.http_url}/api/v1/events?account={quote(self.account, safe='')}"
        backoff = SSE_RETRY_DELAY_INITIAL

        while self._running:
            try:
                logger.debug("Signal SSE: connecting to %s", url)
                # timeout=None is required: this stream is open for hours and
                # any read timeout would tear it down mid-idle.
                async with self.client.stream(
                    "GET", url,
                    headers={"Accept": "text/event-stream"},
                    timeout=None,
                ) as response:
                    self._sse_response = response
                    backoff = SSE_RETRY_DELAY_INITIAL  # Reset on successful connection
                    self._last_sse_activity = time.time()
                    logger.info("Signal SSE: connected")

                    buffer = ""
                    async for chunk in response.aiter_text():
                        if not self._running:
                            break
                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line:
                                # Blank lines delimit SSE events; they carry
                                # no data of their own.
                                continue
                            # SSE keepalive comments (":") prove the connection
                            # is alive -- update activity so the health monitor
                            # doesn't report false idle warnings.
                            if line.startswith(":"):
                                self._last_sse_activity = time.time()
                                continue
                            # Parse SSE data lines
                            if line.startswith("data:"):
                                data_str = line[5:].strip()
                                if not data_str:
                                    continue
                                self._last_sse_activity = time.time()
                                try:
                                    data = json.loads(data_str)
                                    await self._handle_envelope(data)
                                except json.JSONDecodeError:
                                    # Never fatal: one malformed frame must not
                                    # kill the listener for the rest of the run.
                                    logger.debug("Signal SSE: invalid JSON: %s", data_str[:100])
                                except Exception:
                                    logger.exception("Signal SSE: error handling event")

            except asyncio.CancelledError:
                # Cancellation is the normal exit path from disconnect().
                break
            except httpx.HTTPError as e:
                if self._running:
                    logger.warning("Signal SSE: HTTP error: %s (reconnecting in %.0fs)", e, backoff)
            except Exception as e:
                if self._running:
                    logger.warning("Signal SSE: error: %s (reconnecting in %.0fs)", e, backoff)

            if self._running:
                # Add 20% jitter to prevent thundering herd on reconnection
                jitter = backoff * 0.2 * random.random()
                await asyncio.sleep(backoff + jitter)
                # Double the delay for the next failure, capped so a daemon
                # outage never parks the adapter for longer than a minute.
                backoff = min(backoff * 2, SSE_RETRY_DELAY_MAX)

        self._sse_response = None

    # ------------------------------------------------------------------
    # Health Monitor
    # ------------------------------------------------------------------

    async def _health_monitor(self) -> None:
        """Monitor SSE connection health and force reconnect if stale.

        Complements the listener's own retry loop. The listener only notices
        trouble when the socket actually errors, which a silently dropped
        connection never does. This task instead watches
        :attr:`_last_sse_activity` and distinguishes "quiet but healthy" from
        "half-dead":

        * daemon answers 200 -> the stream is merely idle, so activity is
          reset and no warning repeats;
        * daemon answers non-200 or the probe raises -> the connection is
          dead, so :meth:`_force_reconnect` is triggered.

        Returns:
            None. Exits when :attr:`_running` is cleared.
        """
        while self._running:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            if not self._running:
                break

            elapsed = time.time() - self._last_sse_activity
            if elapsed > HEALTH_CHECK_STALE_THRESHOLD:
                logger.warning("Signal: SSE idle for %.0fs, checking daemon health", elapsed)
                try:
                    resp = await self.client.get(
                        f"{self.http_url}/api/v1/check", timeout=10.0
                    )
                    if resp.status_code == 200:
                        # Daemon is alive but SSE is idle -- update activity to
                        # avoid repeated warnings (connection may just be quiet)
                        self._last_sse_activity = time.time()
                        logger.debug("Signal: daemon healthy, SSE idle")
                    else:
                        logger.warning("Signal: health check failed (%d), forcing reconnect", resp.status_code)
                        self._force_reconnect()
                except Exception as e:
                    logger.warning("Signal: health check error: %s, forcing reconnect", e)
                    self._force_reconnect()

    def _force_reconnect(self) -> None:
        """Force SSE reconnection by closing the current response.

        Closing the open stream from outside is the only reliable way to make
        the listener's ``async with`` block exit and fall through to its
        backoff branch. Awaiting the close synchronously would deadlock, so
        the close runs as a fire-and-forget task tracked in the base class's
        ``_background_tasks`` set so it is not garbage collected mid-flight.

        Returns:
            None.
        """
        if self._sse_response and not self._sse_response.is_stream_consumed:
            try:
                task = asyncio.create_task(self._sse_response.aclose())
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except Exception:
                pass
            self._sse_response = None

    # ------------------------------------------------------------------
    # Message Handling
    # ------------------------------------------------------------------

    async def _handle_envelope(self, envelope: dict) -> None:
        """Process an incoming signal-cli envelope.

        This is the single inbound translation point: one SSE JSON envelope in,
        at most one :class:`~encre.gateway.platforms.base.MessageEvent` out,
        dispatched through :meth:`BasePlatformAdapter.handle_message` which
        hands it to :class:`encre.gateway.run.GatewayRunner`.

        The method is deliberately filter-heavy and returns early at each gate.
        Gates in order:

        1. unwrap ``syncMessage`` envelopes and drop self-sync echoes;
        2. require a sender, drop self-messages (except Note to Self);
        3. drop stories when :attr:`ignore_stories`;
        4. require a ``dataMessage`` (or an edited message's);
        5. enforce the group allowlist, then the optional mention filter;
        6. drop contentless envelopes such as profile key updates;
        7. build the source, infer the message type, cache attachments, and
           dispatch.

        Args:
            envelope: Decoded SSE ``data`` object from the daemon.

        Returns:
            None. Attachments are downloaded and cached to disk; failures are
            logged and skipped rather than propagated so one bad attachment
            cannot lose the rest of the message.
        """
        # Unwrap nested envelope if present. Some signal-cli versions wrap the
        # payload one level deeper than others, so the fallback keeps both.
        envelope_data = envelope.get("envelope", envelope)

        # Handle syncMessage: extract "Note to Self" messages (sent to own account)
        # while still filtering other sync events (read receipts, typing, etc.)
        is_note_to_self = False
        if "syncMessage" in envelope_data:
            sync_msg = envelope_data.get("syncMessage")
            if sync_msg and isinstance(sync_msg, dict):
                sent_msg = sync_msg.get("sentMessage")
                if sent_msg and isinstance(sent_msg, dict):
                    dest = sent_msg.get("destinationNumber") or sent_msg.get("destination")
                    sent_ts = sent_msg.get("timestamp")
                    sent_msg_group_info = sent_msg.get("groupInfo") or {}
                    sent_msg_group_id = sent_msg_group_info.get("groupId") if sent_msg_group_info else None
                    if dest == self._account_normalized or sent_msg_group_id:
                        # Check if this is an echo of our own outbound reply
                        if self._consume_sent_timestamp(sent_ts):
                            return
                        # Genuine user Note to Self -- promote to dataMessage
                        is_note_to_self = True
                        envelope_data = {**envelope_data, "dataMessage": sent_msg}
            if not is_note_to_self:
                return

        # Extract sender info. sourceNumber is legacy, sourceUuid is current;
        # source is the daemon's generic fallback field.
        sender = (
            envelope_data.get("sourceNumber")
            or envelope_data.get("sourceUuid")
            or envelope_data.get("source")
        )
        sender_name = envelope_data.get("sourceName", "")
        sender_uuid = envelope_data.get("sourceUuid", "")
        self._remember_recipient_identifiers(sender, sender_uuid)

        if not sender:
            logger.debug("Signal: ignoring envelope with no sender")
            return

        # Self-message filtering -- prevent reply loops (but allow Note to Self)
        if self._account_normalized and sender == self._account_normalized and not is_note_to_self:
            return

        # Filter stories
        if self.ignore_stories and envelope_data.get("storyMessage"):
            return

        # Get data message -- also check editMessage (edited messages contain
        # their updated dataMessage inside editMessage.dataMessage)
        data_message = (
            envelope_data.get("dataMessage")
            or (envelope_data.get("editMessage") or {}).get("dataMessage")
        )
        if not data_message:
            return

        # Check for group message. The groupId is what decides chat routing for
        # the rest of the method, including which RPC parameter a send uses.
        group_info = data_message.get("groupInfo")
        group_id = group_info.get("groupId") if group_info else None
        is_group = bool(group_id)

        # Group message filtering -- derived from SIGNAL_GROUP_ALLOWED_USERS:
        # - No env var set -> groups disabled (default safe behavior)
        # - Env var set with group IDs -> only those groups allowed
        # - Env var set with "*" -> all groups allowed
        # DM auth is fully handled by run.py (_is_user_authorized)
        if is_group:
            if not self.group_allow_from:
                logger.debug("Signal: ignoring group message (no SIGNAL_GROUP_ALLOWED_USERS)")
                return
            if "*" not in self.group_allow_from and group_id not in self.group_allow_from:
                logger.debug("Signal: group %s not in allowlist", group_id[:8] if group_id else "?")
                return

        # Build chat info. The "group:" prefix is load-bearing: every send
        # method in this class splits on it to choose groupId vs recipient.
        chat_id = sender if not is_group else f"group:{group_id}"
        chat_type = "group" if is_group else "dm"

        # Extract text and render mentions so the agent sees @identifiers
        # instead of the U+FFFC object replacement character.
        text = data_message.get("message", "")
        mentions = data_message.get("mentions", [])
        if text and mentions:
            text = _render_mentions(text, mentions)

        # Mention filter: in groups, only process messages that @mention the bot account
        if is_group and self.require_mention:
            account_norm = self._account_normalized
            # Check rendered mention tags OR raw mention metadata
            mentioned_in_text = account_norm and (
                f"@{account_norm}" in (text or "")
            )
            mentioned_in_metadata = any(
                m.get("number") == account_norm or m.get("uuid") == account_norm
                for m in (data_message.get("mentions") or [])
            )
            if not mentioned_in_text and not mentioned_in_metadata:
                logger.debug(
                    "Signal: ignoring group message (require_mention=true, bot not mentioned)"
                )
                return

        # Strip the bot's own @mention from any group message so the agent
        # doesn't misinterpret "@+155****4567 say hello" as a directive to
        # contact that phone number. _render_mentions replaces the Signal
        # U+FFFC placeholder with @<number-or-uuid>, which looks like an
        # addressee to the LLM rather than a self-reference. Applies to every
        # group (not just require_mention groups) so the self-mention is
        # cleaned wherever it appears.
        if is_group and text:
            account_norm = self._account_normalized
            if account_norm:
                text = text.replace(f"@{account_norm}", "")
                # Also strip if the mention was rendered using the bot's UUID
                bot_uuid = self._recipient_uuid_by_number.get(account_norm)
                if bot_uuid:
                    text = text.replace(f"@{bot_uuid}", "")
                # Tidy the spacing the removed mention left behind: collapse the
                # double-space at a mid-sentence removal and trim the ends.
                # Only touches the doubled space the removal introduced, so
                # intentional newlines in a multi-line message are preserved.
                text = text.replace("  ", " ").strip()

        # Extract quote (reply-to) context from Signal dataMessage. Signal's
        # quote.id is the timestamp of the quoted message; quote.author points
        # at the quoted sender when available. Preserve both so the gateway can
        # tell the agent when the user replied to a specific assistant message.
        quote_data = data_message.get("quote") or {}
        reply_to_id = str(quote_data.get("id")) if quote_data.get("id") else None
        reply_to_text = quote_data.get("text")
        reply_to_author = self._extract_quote_author(quote_data)
        reply_to_author_name = quote_data.get("authorName") or quote_data.get("authorProfileName")
        reply_to_is_own = self._quote_references_own_message(reply_to_id, reply_to_author)

        # Process attachments. Each one is fetched over RPC, cached to disk, and
        # appended as a local path plus a MIME type; the ordering of the two
        # lists is kept in sync because callers zip them positionally.
        attachments_data = data_message.get("attachments", [])
        media_urls = []
        media_types = []

        if attachments_data and not getattr(self, "ignore_attachments", False):
            for att in attachments_data:
                att_id = att.get("id")
                att_size = att.get("size", 0)
                if not att_id:
                    continue
                if att_size > SIGNAL_MAX_ATTACHMENT_SIZE:
                    logger.warning("Signal: attachment too large (%d bytes), skipping", att_size)
                    continue
                try:
                    cached_path, ext = await self._fetch_attachment(att_id)
                    if cached_path:
                        # Use contentType from Signal if available, else map from extension
                        content_type = att.get("contentType") or _ext_to_mime(ext)
                        media_urls.append(cached_path)
                        media_types.append(content_type)
                except Exception:
                    logger.exception("Signal: failed to fetch attachment %s", att_id)

        # Skip envelopes with no meaningful content (no text, no attachments).
        # Catches profile key updates, empty messages, and other metadata-only
        # envelopes that still carry a dataMessage wrapper but have nothing
        # worth processing. See issue: signal-cli logs "Profile key update" +
        # Encre receives msg='' triggering a full agent turn for nothing.
        if (not text or not text.strip()) and not media_urls:
            logger.debug(
                "Signal: skipping contentless envelope from %s (%d attachments)",
                redact_phone(sender), len(media_urls) if media_urls else 0,
            )
            return

        # Build session source. chat_id_alt / user_id_alt carry the secondary
        # identifiers so the session layer can correlate across number and UUID.
        source = self.build_source(
            chat_id=chat_id,
            chat_name=group_info.get("groupName") if group_info else sender_name,
            chat_type=chat_type,
            user_id=sender,
            user_name=sender_name or sender,
            user_id_alt=sender_uuid if sender_uuid else None,
            chat_id_alt=group_id if is_group else None,
        )

        # Determine message type from media. Voice outranks photo so a message
        # containing both an image and a voice note is surfaced as voice input,
        # which is the path that leads to speech-to-text.
        msg_type = MessageType.TEXT
        if media_types:
            if any(mt.startswith("audio/") for mt in media_types):
                msg_type = MessageType.VOICE
            elif any(mt.startswith("image/") for mt in media_types):
                msg_type = MessageType.PHOTO
            elif any(mt.startswith("video/") for mt in media_types):
                msg_type = MessageType.VIDEO
            else:
                # Catch-all: application/*, text/*, and unknown MIME types are
                # treated as documents so run.py's document-context injection
                # surfaces the cached file path to the agent (same pattern as
                # WhatsApp/Slack/BlueBubbles/Mattermost).
                msg_type = MessageType.DOCUMENT

        # Parse timestamp from envelope data (milliseconds since epoch). The
        # daemon omits it on some event types, so the fallback is "now".
        ts_ms = envelope_data.get("timestamp", 0)
        if ts_ms:
            try:
                timestamp = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            except (ValueError, OSError):
                timestamp = datetime.now(tz=timezone.utc)
        else:
            timestamp = datetime.now(tz=timezone.utc)

        # Build and dispatch event.
        # Store raw envelope data in raw_message so on_processing_start/complete
        # can extract targetAuthor + targetTimestamp for sendReaction.
        event = MessageEvent(
            source=source,
            text=text or "",
            message_type=msg_type,
            media_urls=media_urls,
            media_types=media_types,
            timestamp=timestamp,
            raw_message={
                "sender": sender,
                "timestamp_ms": ts_ms,
                "quote": quote_data if quote_data else None,
            },
            reply_to_message_id=reply_to_id,
            reply_to_text=reply_to_text,
            reply_to_author_id=reply_to_author,
            reply_to_author_name=reply_to_author_name,
            reply_to_is_own_message=reply_to_is_own,
        )

        logger.debug("Signal: message from %s in %s: %s",
                      redact_phone(sender), chat_id[:20], (text or "")[:50])

        await self.handle_message(event)

    def _remember_recipient_identifiers(self, number: Optional[str], service_id: Optional[str]) -> None:
        """Cache any number-to-UUID mapping observed from Signal envelopes.

        Every inbound envelope is an opportunity to learn how the daemon
        addresses a peer. Storing both directions lets outbound sends upgrade
        a phone number to the service ID signal-cli prefers, and lets
        :meth:`_quote_references_own_message` recognise this adapter's own
        messages under either identifier.

        Args:
            number: E.164 number or similar addressable form seen in the
                envelope.
            service_id: ACI/PNI service ID seen alongside it. Must already
                look like a service ID; anything else is ignored so a stray
                profile name never poisons the map.

        Returns:
            None.
        """
        if not number or not service_id or not _is_signal_service_id(service_id):
            return
        self._recipient_uuid_by_number[number] = service_id
        self._recipient_number_by_uuid[service_id] = number

    @staticmethod
    def _extract_quote_author(quote_data: Any) -> Optional[str]:
        """Return the best available Signal sender identifier from quote metadata.

        The daemon renames quote author fields across versions, so the lookup
        is a prioritised sweep over every spelling it has used rather than a
        single key. The first non-empty hit wins.

        Args:
            quote_data: The ``quote`` dict from a dataMessage, or anything
                else, in which case the method bails out cleanly.

        Returns:
            The author identifier as a string, or ``None`` when no known key
            carries a value.
        """
        if not isinstance(quote_data, dict):
            return None
        for key in (
            "author",
            "authorNumber",
            "authorUuid",
            "authorAci",
            "authorServiceId",
            "authorServiceIdString",
        ):
            value = quote_data.get(key)
            if value:
                return str(value)
        return None

    def _quote_references_own_message(
        self,
        reply_to_id: Optional[str],
        reply_to_author: Optional[str],
    ) -> bool:
        """True when a Signal quote points at this adapter's outbound message.

        Used to set ``reply_to_is_own_message`` so the session layer knows the
        user is replying to the bot instead of to a third party. Matching
        tries the strongest signal first -- the quoted timestamp against the
        outbound timestamp cache -- then falls back to identity comparison
        under the account number, its cached UUID, and the reverse UUID map.

        Args:
            reply_to_id: The quoted message's timestamp as a string.
            reply_to_author: The quoted message's author identifier.

        Returns:
            True when any available evidence identifies the quote as pointing
            at a message this adapter sent.
        """
        if reply_to_id and str(reply_to_id) in self._sent_message_timestamps:
            return True
        if not reply_to_author:
            return False
        author = str(reply_to_author).strip()
        if self._account_normalized and author == self._account_normalized:
            return True
        cached_uuid = self._recipient_uuid_by_number.get(self._account_normalized)
        if cached_uuid and author == cached_uuid:
            return True
        cached_number = self._recipient_number_by_uuid.get(author)
        return bool(cached_number and cached_number == self._account_normalized)

    def _remember_sent_message_timestamp(self, timestamp: Any) -> None:
        """Keep a bounded cache of outbound Signal timestamps for quote matching.

        Signal identifies quoted messages by timestamp rather than by a stable
        message id, so the outbound timestamps this adapter records are the
        only way to later recognise a reply-to-own-message.

        Args:
            timestamp: The ``timestamp`` field of a successful send RPC
                result, or ``None`` to skip.

        Returns:
            None.
        """
        if timestamp is None:
            return
        key = str(timestamp)
        # Re-insert to mark most-recently-used so eviction drops genuinely old
        # timestamps, not a recently re-seen one.
        self._sent_message_timestamps.pop(key, None)
        self._sent_message_timestamps[key] = None
        # FIFO-evict the oldest entry once over the cap.
        while len(self._sent_message_timestamps) > self._max_sent_message_timestamps:
            self._sent_message_timestamps.popitem(last=False)

    def _extract_contact_uuid(self, contact: Any, phone_number: str) -> Optional[str]:
        """Best-effort extraction of a Signal service ID from listContacts output.

        The daemon's contact shape varies between versions and between
        Android and desktop backends: the service ID may sit at the top level,
        under ``profile``, and may be spelled ``uuid`` or ``serviceId``. The
        number must also actually match the requested contact, otherwise a
        neighbouring entry would be attributed to the wrong chat.

        Args:
            contact: One element of the ``listContacts`` result.
            phone_number: The E.164 number the caller is resolving.

        Returns:
            The service ID when both a valid ID and a matching number are
            present, otherwise ``None``.
        """
        if not isinstance(contact, dict):
            return None

        number = contact.get("number")
        recipient = contact.get("recipient")
        service_id = contact.get("uuid") or contact.get("serviceId")
        if not service_id:
            profile = contact.get("profile")
            if isinstance(profile, dict):
                service_id = profile.get("serviceId") or profile.get("uuid")

        if service_id and _is_signal_service_id(service_id):
            matches_number = number == phone_number or recipient == phone_number
            if matches_number:
                return service_id
        return None

    async def _resolve_recipient(self, chat_id: str) -> str:
        """Return the preferred Signal recipient identifier for a direct chat.

        signal-cli accepts either an E.164 number or a service ID as
        ``recipient``, but delivery is more reliable when the service ID is
        used. This method upgrades only bare numbers, and only once, caching
        the result so repeat sends to the same chat never pay for
        ``listContacts`` again.

        Args:
            chat_id: The adapter-level chat id. Group ids (``group:``
                prefix), already-canonical service IDs, and anything that is
                not a plausible E.164 number pass through untouched.

        Returns:
            The service ID when one is known, else the original chat id.

        Note:
            The double-checked lock around the RPC means only one concurrent
            caller per event-loop pass pays for the contact fetch.
        """
        if (
            not chat_id
            or chat_id.startswith("group:")
            or _is_signal_service_id(chat_id)
            or not _looks_like_e164_number(chat_id)
        ):
            return chat_id

        cached = self._recipient_uuid_by_number.get(chat_id)
        if cached:
            return cached

        async with self._recipient_cache_lock:
            cached = self._recipient_uuid_by_number.get(chat_id)
            if cached:
                return cached

            contacts = await self._rpc("listContacts", {
                "account": self.account,
                "allRecipients": True,
            })
            if isinstance(contacts, list):
                for contact in contacts:
                    number = contact.get("number") if isinstance(contact, dict) else None
                    service_id = self._extract_contact_uuid(contact, chat_id)
                    if number and service_id:
                        self._remember_recipient_identifiers(number, service_id)

            return self._recipient_uuid_by_number.get(chat_id, chat_id)

    # ------------------------------------------------------------------
    # Attachment Handling
    # ------------------------------------------------------------------

    async def _fetch_attachment(self, attachment_id: str) -> tuple:
        """Fetch an attachment via JSON-RPC and cache it. Returns (path, ext).

        The daemon returns the whole file as a single base64 string, so this
        method owns the full decode-and-store path: base64 decode, magic-byte
        extension guess, optional AAC remux, and routing into the right cache
        helper. The returned path is what ends up in
        ``MessageEvent.media_urls`` and is later handed to the document- or
        audio-context injection in the gateway.

        Args:
            attachment_id: The attachment ``id`` from the dataMessage.

        Returns:
            ``(cached_path, extension)`` on success, or ``(None, "")`` when
            the daemon returned nothing usable. An empty tuple element in the
            first position signals to the caller that this attachment should
            simply be skipped.

        Note:
            Any transport or decode failure raises; the caller in
            :meth:`_handle_envelope` catches it so one bad attachment does not
            discard the rest of the message.
        """
        result = await self._rpc("getAttachment", {
            "account": self.account,
            "id": attachment_id,
        })

        if not result:
            return None, ""

        # Handle dict response (signal-cli returns {"data": "base64..."})
        if isinstance(result, dict):
            result = result.get("data")
            if not result:
                logger.warning("Signal: attachment response missing 'data' key")
                return None, ""

        # Result is base64-encoded file content
        raw_data = base64.b64decode(result)
        ext = _guess_extension(raw_data)

        # Android Signal voice notes are raw ADTS AAC streams. Most STT
        # providers (Groq Whisper, OpenAI Whisper) reject raw ADTS -- they
        # require AAC to be muxed into an MP4 container. Remux losslessly
        # with ``ffmpeg -c:a copy`` so the cached file is a normal .m4a.
        # No re-encode, sub-100ms on a Pi 5. Graceful no-op if ffmpeg is
        # absent: the raw ADTS file is cached as-is and STT may reject it
        # (there is no downstream sniff-and-remux fallback).
        if ext == ".aac":
            remuxed: Optional[Tuple[bytes, str]] = await asyncio.to_thread(_remux_aac_to_m4a, raw_data)
            if remuxed is not None:
                raw_data, ext = remuxed

        if _is_image_ext(ext):
            path = cache_image_from_bytes(raw_data, ext)
        elif _is_audio_ext(ext):
            path = cache_audio_from_bytes(raw_data, ext)
        else:
            path = cache_document_from_bytes(raw_data, ext)

        return path, ext

    # ------------------------------------------------------------------
    # JSON-RPC Communication
    # ------------------------------------------------------------------

    async def _rpc(
        self,
        method: str,
        params: dict,
        rpc_id: str = None,
        *,
        log_failures: bool = True,
        raise_on_rate_limit: bool = False,
        timeout: float = 30.0,
    ) -> Any:
        """Send a JSON-RPC 2.0 request to signal-cli daemon.

        Every outbound capability of this adapter funnels through here, so the
        JSON-RPC envelope shape, error decoding, and rate-limit handling are
        implemented exactly once. Callers only express intent by passing a
        method name and a params dict.

        The response contract is deliberately lossy: any transport failure,
        HTTP error, malformed JSON, or JSON-RPC error yields ``None`` rather
        than an exception, so callers can branch on truthiness. The one
        exception is :class:`SignalRateLimitError`, which is re-raised when
        the caller opts in.

        Args:
            method: signal-cli RPC method name such as ``send`` or
                ``sendTyping``.
            params: Method-specific parameters, always including
                ``account`` at the call site.
            rpc_id: Stable request id. Defaults to a timestamped id derived
                from the method name; pass an explicit value for methods that
                are intentionally repeated, such as typing refreshes.
            log_failures: When False, error and exception paths log at DEBUG
                instead of WARNING -- used by the typing-indicator path to
                silence repeated NETWORK_FAILURE spam for unreachable
                recipients while still preserving visibility for the first
                occurrence and for unrelated RPCs.
            raise_on_rate_limit: When True, a Signal ``[429]`` /
                ``RateLimitException`` response raises
                ``SignalRateLimitError`` instead of being swallowed -- lets
                callers (multi-attachment send) opt into backoff-retry without
                changing default behaviour.
            timeout: Per-request timeout in seconds. Larger attachment
                batches pass a computed value so slow uploads are not cut
                short.

        Returns:
            The decoded ``result`` field, or ``None`` on any failure.

        Raises:
            SignalRateLimitError: Only when *raise_on_rate_limit* is True and
                the daemon reports a rate limit, either as a JSON-RPC error
                or as a ``RATE_LIMIT_FAILURE`` entry in the result's
                per-recipient list.
        """
        if not self.client:
            logger.warning("Signal: RPC called but client not connected")
            return None

        if rpc_id is None:
            rpc_id = f"{method}_{int(time.time() * 1000)}"

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": rpc_id,
        }

        try:
            # One shared client for every RPC; the timeout is per-call so a
            # slow attachment upload does not hold the connection budget.
            resp = await self.client.post(
                f"{self.http_url}/api/v1/rpc",
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                # JSON-RPC level failure. Rate limits arrive here as a daemon
                # error object rather than inside the result, so this branch
                # must run before the plain log-and-return path.
                err = data["error"]
                if raise_on_rate_limit:
                    if _is_signal_rate_limit_error(err):
                        err_msg = str(err.get("message", "")) if isinstance(err, dict) else str(err)
                        retry_after = _extract_retry_after_seconds(err)
                        raise SignalRateLimitError(err_msg, retry_after=retry_after)
                if log_failures:
                    logger.warning("Signal RPC error (%s): %s", method, err)
                else:
                    logger.debug("Signal RPC error (%s): %s", method, err)
                return None

            result = data.get("result")
            # Signal also reports per-recipient rate limits INSIDE the result
            # list, which looks like a success to a naive caller. Only scan
            # when the caller opted in, so ordinary sends keep their cheap
            # path and never raise.
            if isinstance(result, dict) and raise_on_rate_limit:
                results = result.get("results")
                if isinstance(results, list):
                    for r in results:
                        if isinstance(r, dict) and r.get("type") == "RATE_LIMIT_FAILURE":
                            retry_after = r.get("retryAfterSeconds")
                            raise SignalRateLimitError("Rate limit exceeded for recipient", retry_after=retry_after)

            return result

        except SignalRateLimitError:
            raise
        except Exception as e:
            if log_failures:
                logger.warning("Signal RPC %s failed: %s", method, e)
            else:
                logger.debug("Signal RPC %s failed: %s", method, e)
            return None

    # ------------------------------------------------------------------
    # Formatting: markdown to Signal body ranges
    # ------------------------------------------------------------------

    @staticmethod
    def _markdown_to_signal(text: str) -> tuple[str, list[str]]:
        """Backward-compatible wrapper around the shared Signal formatter.

        The real implementation lives in
        :func:`encre.gateway.platforms.signal_format.markdown_to_signal` so
        that every consumer of Signal formatting (this adapter and any test
        or tooling) shares one code path.

        Args:
            text: Markdown-flavoured agent output.

        Returns:
            A ``(plain_text, text_styles)`` pair. ``text_styles`` holds one
            element per contiguous styled range and maps onto signal-cli's
            ``textStyle`` / ``textStyles`` parameters.
        """
        return markdown_to_signal(text)

    def format_message(self, content: str) -> str:
        """Strip markdown for plain-text fallback (used by the base class).

        The actual rich formatting happens in :meth:`send` via
        :meth:`_markdown_to_signal`, which is where the signal-cli style
        ranges get attached. This hook exists only so the inherited send path
        has something deterministic to call; it is a pass-through on purpose
        because removing markdown here would not add signal-cli formatting.

        Args:
            content: Raw agent output.

        Returns:
            The content unchanged.
        """
        # This is only called if someone uses the base-class send path.
        # Our send() override bypasses this entirely.
        return content

    def _validate_send_result(self, result: Any) -> tuple[bool, Optional[str]]:
        """Validate signal-cli send response results.

        signal-cli returns a list of per-recipient outcomes rather than a
        single boolean, and each element can fail for a different reason.
        This method collapses that shape into the success/error pair the rest
        of the adapter works with.

        A missing or malformed result is treated as success because callers
        already branch on ``result is None`` for transport failure; reaching
        this method means the RPC itself completed.

        Args:
            result: The decoded ``result`` object from a ``send`` RPC.

        Returns:
            ``(True, None)`` when every recipient outcome is a success, else
            ``(False, error_message)`` carrying the first failure type or
            failure reason found.
        """
        if not result or not isinstance(result, dict):
            return True, None

        results = result.get("results")
        if isinstance(results, list):
            for r in results:
                if not isinstance(r, dict):
                    continue
                rtype = r.get("type")
                if rtype and rtype != "SUCCESS":
                    return False, str(rtype)
                if "success" in r and not r.get("success"):
                    fail = r.get("failure")
                    if fail:
                        return False, str(fail)
                    return False, "Recipient delivery failed"
        return True, None

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message with native Signal formatting.

        Converts agent markdown into signal-cli body ranges, addresses the
        chat, posts the ``send`` RPC, and records the resulting timestamp so
        the self-sync echo of this message is suppressed on the way back in.

        Args:
            chat_id: ``group:<id>`` for group chats, otherwise a direct chat
                id which is passed through :meth:`_resolve_recipient` first.
            content: Markdown text produced by the agent.
            reply_to: Ignored. Signal reply threading is not available through
                the daemon's send RPC.
            metadata: Reserved for parity with other adapters; unused here.

        Returns:
            :class:`~encre.gateway.platforms.base.SendResult`.
            ``message_id`` is always ``None`` because Signal has no editable
            message identifier; the stream consumer therefore stays on its
            non-edit fallback path. ``success=False`` when the RPC failed or
            every recipient outcome was a failure.
        """
        await self._stop_typing_indicator(chat_id)

        plain_text, text_styles = self._markdown_to_signal(content)

        params: Dict[str, Any] = {
            "account": self.account,
            "message": plain_text,
        }

        # signal-cli takes a single textStyle for one range, or textStyles
        # for several; using the wrong key silently drops formatting.
        if text_styles:
            if len(text_styles) == 1:
                params["textStyle"] = text_styles[0]
            else:
                params["textStyles"] = text_styles

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [await self._resolve_recipient(chat_id)]

        logger.info("[Signal] Sending response (%d chars) to %s", len(plain_text), chat_id)
        result = await self._rpc("send", params)

        if result is not None:
            success, err_msg = self._validate_send_result(result)
            if not success:
                return SendResult(success=False, error=err_msg, raw_response=result)
            self._track_sent_timestamp(result)
            # Signal has no editable message identifier. Returning None keeps the
            # stream consumer on the non-edit fallback path instead of pretending
            # future edits can remove an in-progress cursor from the chat thread.
            return SendResult(success=True, message_id=None)
        return SendResult(success=False, error="RPC send failed")

    def _track_sent_timestamp(self, rpc_result) -> None:
        """Record outbound message timestamp for echo-back filtering.

        Populates both caches: the short TTL-bound recency map used to
        discard self-sync echoes, and the longer quote-ownership map. Both
        are insertion-ordered so eviction drops the oldest entry.

        The two-stage pruning matters: TTL pruning first removes entries
        that are simply too old to still be echoing, and only then does the
        hard cap trim the remainder. That keeps a burst of outbound messages
        from evicting a still-pending echo.

        Args:
            rpc_result: Decoded result of a successful ``send`` RPC.

        Returns:
            None.
        """
        ts = rpc_result.get("timestamp") if isinstance(rpc_result, dict) else None
        if ts:
            self._remember_sent_message_timestamp(ts)
            now = time.monotonic()
            # Re-insert to mark as most-recently-used.
            self._recent_sent_timestamps.pop(ts, None)
            self._recent_sent_timestamps[ts] = now
            # Drop entries older than TTL first (cheap O(k) where k=expired).
            cutoff = now - self._recent_sent_ttl_seconds
            while self._recent_sent_timestamps:
                oldest_ts, oldest_at = next(iter(self._recent_sent_timestamps.items()))
                if oldest_at < cutoff:
                    self._recent_sent_timestamps.popitem(last=False)
                else:
                    break
            # Hard cap as a last-resort guard against runaway producers.
            while len(self._recent_sent_timestamps) > self._max_recent_timestamps:
                self._recent_sent_timestamps.popitem(last=False)

    def _consume_sent_timestamp(self, ts) -> bool:
        """Pop a timestamp if it matches one we sent. Returns True on echo.

        The pop is the whole mechanism: an inbound envelope whose timestamp
        is still in the recency map is by definition this adapter's own
        outbound message echoing back, so consuming it both identifies the
        echo and prevents the same envelope from being double-counted.

        Args:
            ts: The envelope ``timestamp`` in milliseconds.

        Returns:
            True when *ts* was present and has now been removed.
        """
        if ts and ts in self._recent_sent_timestamps:
            self._recent_sent_timestamps.pop(ts, None)
            return True
        return False

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send a typing indicator.

        base.py's ``_keep_typing`` refresh loop calls this every ~2s while
        the agent is processing. If signal-cli returns NETWORK_FAILURE for
        this recipient (offline, unroutable, group membership lost, etc.)
        the unmitigated behaviour is: a WARNING log every 2 seconds for as
        long as the agent keeps running. Instead we:

        - silence the WARNING after the first consecutive failure (subsequent
          attempts log at DEBUG) so transport issues are still visible once
          but don't flood the log,
        - skip the RPC entirely during an exponential cooldown window once
          three consecutive failures have happened, so we stop hammering
          signal-cli with requests it can't deliver.

        A successful sendTyping clears the counters.

        Args:
            chat_id: Target chat, ``group:`` prefixed or a direct chat id.
            metadata: Unused; present for base-class signature parity.

        Returns:
            None. This method never raises; every failure mode is folded into
            the per-chat backoff state so the caller's refresh loop is never
            disturbed.
        """
        now = time.monotonic()
        skip_until = self._typing_skip_until.get(chat_id, 0.0)
        if now < skip_until:
            # Inside the cooldown window: do not even attempt the RPC. The
            # monotonic clock is used throughout so wall-clock adjustments
            # cannot reset or extend the window.
            return

        params: Dict[str, Any] = {
            "account": self.account,
        }

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [await self._resolve_recipient(chat_id)]

        fails = self._typing_failures.get(chat_id, 0)
        # log_failures mirrors the failure count so exactly one WARNING is
        # emitted per outage, and every retry after that is DEBUG.
        result = await self._rpc(
            "sendTyping",
            params,
            rpc_id="typing",
            log_failures=(fails == 0),
        )

        if result is None:
            fails += 1
            self._typing_failures[chat_id] = fails
            # After 3 consecutive failures, back off exponentially (16s,
            # 32s, 60s cap) to stop spamming signal-cli for a recipient
            # that clearly isn't reachable right now.
            if fails >= 3:
                backoff = min(60.0, 16.0 * (2 ** (fails - 3)))
                self._typing_skip_until[chat_id] = now + backoff
        else:
            # Any success resets both counters so a later outage backs off
            # afresh instead of inheriting a stale cooldown.
            self._typing_failures.pop(chat_id, None)
            self._typing_skip_until.pop(chat_id, None)

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """Send a batch of images via chunked Signal RPC calls.

        Signal caps attachments per message, so the batch is split into
        chunks of ``SIGNAL_MAX_ATTACHMENTS_PER_MSG`` and each chunk becomes
        its own ``send`` RPC, paced by the shared rate-limit scheduler.

        Per-image alt texts are dropped -- Signal's send RPC only carries
        one shared message body. Bad images (download failure, missing
        file, oversize) are skipped with a warning so one bad URL
        doesn't lose the rest of the batch. ``human_delay`` is ignored:
        the rate-limit scheduler handles inter-batch pacing.

        Args:
            chat_id: Target chat, ``group:`` prefixed or a direct chat id.
            images: ``(url, alt_text)`` pairs. URLs may be ``file://`` paths
                or remote http(s) URLs.
            metadata: Unused; present for base-class signature parity.
            human_delay: Unused. Pacing is owned by the scheduler.

        Returns:
            None. This is a fire-and-forget batch path; per-chunk failures
            are logged rather than returned.
        """
        if not images:
            return

        scheduler = get_scheduler()
        logger.info(
            "Signal send_multiple_images: received %d image(s) for %s 鈥?"
            "scheduler state: %s",
            len(images), chat_id[:30], scheduler.state(),
        )

        await self._stop_typing_indicator(chat_id)

        # Pre-flight validation: resolve every URL to a local file and drop
        # the unusable ones before any RPC is attempted, so a single bad URL
        # cannot cost a chunk of the batch. The three counters are reported
        # together in the error log below.
        attachments: List[str] = []
        skipped_download = 0
        skipped_missing = 0
        skipped_oversize = 0
        for image_url, _alt_text in images:
            if image_url.startswith("file://"):
                file_path = unquote(image_url[7:])
            else:
                try:
                    file_path = await cache_image_from_url(image_url)
                except Exception as e:
                    logger.warning("Signal: failed to download image %s: %s", image_url, e)
                    skipped_download += 1
                    continue

            if not file_path or not Path(file_path).exists():
                logger.warning("Signal: image file not found for %s", image_url)
                skipped_missing += 1
                continue

            file_size = Path(file_path).stat().st_size
            if file_size > SIGNAL_MAX_ATTACHMENT_SIZE:
                logger.warning(
                    "Signal: image too large (%d bytes), skipping %s", file_size, image_url
                )
                skipped_oversize += 1
                continue

            attachments.append(file_path)

        if not attachments:
            logger.error(
                "Signal: no valid images in batch of %d "
                "(download=%d missing=%d oversize=%d)",
                len(images), skipped_download, skipped_missing, skipped_oversize,
            )
            return

        logger.info(
            "Signal send_multiple_images: %d/%d images valid, sending in chunks",
            len(attachments), len(images),
        )

        # Base params are shared by every chunk; only the attachment list
        # differs. Addressing is resolved once rather than per chunk.
        base_params: Dict[str, Any] = {
            "account": self.account,
            "message": "",
        }
        if chat_id.startswith("group:"):
            base_params["groupId"] = chat_id[6:]
        else:
            base_params["recipient"] = [await self._resolve_recipient(chat_id)]

        att_batches = [
            attachments[i:i + SIGNAL_MAX_ATTACHMENTS_PER_MSG]
            for i in range(0, len(attachments), SIGNAL_MAX_ATTACHMENTS_PER_MSG)
        ]

        for idx, att_batch in enumerate(att_batches):
            n = len(att_batch)
            # Ask the scheduler how long this chunk should wait before it is
            # admitted, and tell the user about long waits so a silent pause
            # does not look like a hang.
            estimated = scheduler.estimate_wait(n)
            logger.debug(
                "Signal batch %d/%d: %d attachments, estimated wait=%.1fs",
                idx + 1, len(att_batches), n, estimated,
            )
            if estimated >= SIGNAL_BATCH_PACING_NOTICE_THRESHOLD:
                await self._notify_batch_pacing(
                    chat_id, idx + 1, len(att_batches), estimated
                )

            params = dict(base_params, attachments=att_batch)
            # Timeout scales with chunk size because upload time is roughly
            # proportional to total bytes.
            send_timeout = _signal_send_timeout(n)

            for attempt in range(1, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS + 1):
                # acquire() is what actually enforces pacing; it may sleep for
                # a long time when a previous chunk was rate limited.
                await scheduler.acquire(n)
                try:
                    _rpc_t0 = time.monotonic()
                    result = await self._rpc(
                        "send", params, raise_on_rate_limit=True, timeout=send_timeout,
                    )
                    _rpc_duration = time.monotonic() - _rpc_t0
                    if result is not None:
                        success, err_msg = self._validate_send_result(result)
                        if success:
                            self._track_sent_timestamp(result)
                            # Feeding the observed duration back lets the
                            # scheduler retune its pace estimate for the next
                            # chunk instead of assuming the worst case.
                            await scheduler.report_rpc_duration(_rpc_duration, n)
                            logger.info(
                                "Signal batch %d/%d: %d attachments sent in %.1fs "
                                "(attempt %d/%d)",
                                idx + 1, len(att_batches), n, _rpc_duration,
                                attempt, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
                            )
                        else:
                            logger.error(
                                "Signal: RPC send failed for batch %d/%d (%d attachments, "
                                "attempt %d/%d, rpc_duration=%.1fs): %s",
                                idx + 1, len(att_batches), n,
                                attempt, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
                                _rpc_duration, err_msg,
                            )
                            # Retry transient (non-rate-limit) failures once
                            if attempt < SIGNAL_RATE_LIMIT_MAX_ATTEMPTS:
                                backoff = 2.0 ** attempt
                                logger.info(
                                    "Signal: retrying batch %d/%d after %.1fs backoff",
                                    idx + 1, len(att_batches), backoff,
                                )
                                await asyncio.sleep(backoff)
                                continue
                    else:
                        # Assume the server didn't accept the batch, don't deduce tokens
                        logger.error(
                            "Signal: RPC send failed for batch %d/%d (%d attachments, "
                            "attempt %d/%d, rpc_duration=%.1fs)",
                            idx + 1, len(att_batches), n,
                            attempt, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
                            _rpc_duration,
                        )
                        # Retry transient (non-rate-limit) failures once
                        if attempt < SIGNAL_RATE_LIMIT_MAX_ATTEMPTS:
                            backoff = 2.0 ** attempt
                            logger.info(
                                "Signal: retrying batch %d/%d after %.1fs backoff",
                                idx + 1, len(att_batches), backoff,
                            )
                            await asyncio.sleep(backoff)
                            continue
                    # Either the chunk succeeded or all attempts are spent;
                    # break so the loop moves on to the next chunk.
                    break
                except SignalRateLimitError as e:
                    # Feed the server's retry_after into the scheduler so the
                    # next acquire() honours it instead of guessing.
                    scheduler.feedback(e.retry_after, n)
                    if attempt >= SIGNAL_RATE_LIMIT_MAX_ATTEMPTS:
                        logger.error(
                            "Signal: rate-limit retries exhausted on batch %d/%d "
                            "(%d attachments lost, server retry_after=%s)",
                            idx + 1, len(att_batches), n,
                            f"{e.retry_after:.0f}s" if e.retry_after else "unknown",
                        )
                        break
                    logger.warning(
                        "Signal: rate-limited on batch %d/%d "
                        "(attempt %d/%d, server retry_after=%s); "
                        "scheduler will pace the retry",
                        idx + 1, len(att_batches),
                        attempt, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
                        f"{e.retry_after:.0f}s" if e.retry_after else "unknown",
                    )

    async def _notify_batch_pacing(
        self,
        chat_id: str,
        next_batch_idx: int,
        total_batches: int,
        wait_s: float,
    ) -> None:
        """Inform the user when an inter-batch pacing wait crosses the
        notice threshold. Best-effort; logs and continues on failure.

        Without this message a multi-hundred-image batch would appear to hang
        for the whole paced wait. The notice itself goes through
        :meth:`send`, so it does not interfere with the attachment chunks.

        Args:
            chat_id: Target chat to message.
            next_batch_idx: 1-based index of the chunk about to be sent.
            total_batches: Total chunk count for this batch.
            wait_s: Estimated seconds the caller is about to wait.

        Returns:
            None. All exceptions are swallowed after logging.
        """
        try:
            await self.send(
                chat_id,
                f"(More images coming 鈥?pausing ~{_format_wait(wait_s)} "
                f"for Signal rate limit, batch {next_batch_idx}/{total_batches}.)",
            )
        except Exception as e:
            logger.warning("Signal: failed to send pacing notice: %s", e)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send an image. Supports http(s):// and file:// URLs.

        A single image goes through the same validation and RPC path as the
        chunked batch path, minus the scheduler, because a one-off image does
        not need pacing.

        Args:
            chat_id: ``group:<id>`` for group chats, otherwise a direct chat
                id resolved through :meth:`_resolve_recipient`.
            image_url: ``file://`` path to a local image, or a remote
                http(s) URL that is downloaded into the cache first.
            caption: Optional message body sent alongside the attachment.
            **kwargs: Absorbed for base-class signature parity.

        Returns:
            :class:`~encre.gateway.platforms.base.SendResult`. Failures carry
            a human-readable reason in ``error``.
        """
        await self._stop_typing_indicator(chat_id)

        # Resolve image to local path
        if image_url.startswith("file://"):
            file_path = unquote(image_url[7:])
        else:
            # Download remote image to cache
            try:
                file_path = await cache_image_from_url(image_url)
            except Exception as e:
                logger.warning("Signal: failed to download image: %s", e)
                return SendResult(success=False, error=str(e))

        if not file_path or not Path(file_path).exists():
            return SendResult(success=False, error="Image file not found")

        # Validate size
        file_size = Path(file_path).stat().st_size
        if file_size > SIGNAL_MAX_ATTACHMENT_SIZE:
            return SendResult(success=False, error=f"Image too large ({file_size} bytes)")

        params: Dict[str, Any] = {
            "account": self.account,
            "message": caption or "",
            "attachments": [file_path],
        }

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [await self._resolve_recipient(chat_id)]

        result = await self._rpc("send", params)
        if result is not None:
            success, err_msg = self._validate_send_result(result)
            if not success:
                return SendResult(success=False, error=err_msg, raw_response=result)
            self._track_sent_timestamp(result)
            return SendResult(success=True)
        return SendResult(success=False, error="RPC send with attachment failed")

    async def _send_attachment(
        self,
        chat_id: str,
        file_path: str,
        media_label: str,
        caption: Optional[str] = None,
    ) -> SendResult:
        """Send any file as a Signal attachment via RPC.

        Shared implementation for send_document, send_image_file, send_voice,
        and send_video -- avoids duplicating the validation/routing/RPC logic.

        Signal treats all attachments uniformly at the RPC level: a caption
        plus a list of local paths. The ``media_label`` parameter only
        shapes the error text so callers can tell which kind of media failed.

        Args:
            chat_id: ``group:<id>`` for group chats, otherwise a direct chat
                id resolved through :meth:`_resolve_recipient`.
            file_path: Local path to the file to send.
            media_label: Human-readable label such as ``"File"`` or
                ``"Audio"``, used only in error messages.
            caption: Optional message body sent alongside the attachment.

        Returns:
            :class:`~encre.gateway.platforms.base.SendResult`.
        """
        await self._stop_typing_indicator(chat_id)

        try:
            file_size = Path(file_path).stat().st_size
        except FileNotFoundError:
            return SendResult(success=False, error=f"{media_label} file not found: {file_path}")

        if file_size > SIGNAL_MAX_ATTACHMENT_SIZE:
            return SendResult(success=False, error=f"{media_label} too large ({file_size} bytes)")

        params: Dict[str, Any] = {
            "account": self.account,
            "message": caption or "",
            "attachments": [file_path],
        }

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [await self._resolve_recipient(chat_id)]

        result = await self._rpc("send", params)
        if result is not None:
            success, err_msg = self._validate_send_result(result)
            if not success:
                return SendResult(success=False, error=err_msg, raw_response=result)
            self._track_sent_timestamp(result)
            return SendResult(success=True)
        return SendResult(success=False, error=f"RPC send {media_label.lower()} failed")

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document/file attachment.

        Args:
            chat_id: Target chat id.
            file_path: Local path to the document.
            caption: Optional caption sent with the file.
            filename: Ignored; signal-cli derives the name from the path.
            **kwargs: Absorbed for base-class signature parity.

        Returns:
            :class:`~encre.gateway.platforms.base.SendResult`.
        """
        return await self._send_attachment(chat_id, file_path, "File", caption)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file as a native Signal attachment.

        Called by the gateway media delivery flow when MEDIA: tags containing
        image paths are extracted from agent responses.

        Args:
            chat_id: Target chat id.
            image_path: Local path to the image.
            caption: Optional caption.
            reply_to: Ignored; Signal reply threading is unavailable.
            **kwargs: Absorbed for base-class signature parity.

        Returns:
            :class:`~encre.gateway.platforms.base.SendResult`.
        """
        return await self._send_attachment(chat_id, image_path, "Image", caption)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send an audio file as a Signal attachment.

        Signal does not distinguish voice messages from file attachments at
        the API level, so this routes through the same RPC send path.

        Args:
            chat_id: Target chat id.
            audio_path: Local path to the audio file.
            caption: Optional caption.
            reply_to: Ignored; Signal reply threading is unavailable.
            **kwargs: Absorbed for base-class signature parity.

        Returns:
            :class:`~encre.gateway.platforms.base.SendResult`.
        """
        return await self._send_attachment(chat_id, audio_path, "Audio", caption)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video file as a Signal attachment.

        Args:
            chat_id: Target chat id.
            video_path: Local path to the video file.
            caption: Optional caption.
            reply_to: Ignored; Signal reply threading is unavailable.
            **kwargs: Absorbed for base-class signature parity.

        Returns:
            :class:`~encre.gateway.platforms.base.SendResult`.
        """
        return await self._send_attachment(chat_id, video_path, "Video", caption)

    # ------------------------------------------------------------------
    # Typing Indicators
    # ------------------------------------------------------------------

    async def _stop_typing_indicator(self, chat_id: str) -> None:
        """Stop a typing indicator loop for a chat.

        Called at the start of every outbound send so a stale "typing" state
        cannot outlive the reply it was announcing. Three things happen in
        order:

        1. the per-chat refresh task is cancelled and awaited,
        2. an explicit ``stop=true`` sendTyping RPC is posted so the
           recipient's device drops the indicator immediately instead of
           waiting for Signal's built-in ~5 second timeout,
        3. the per-chat backoff state is cleared so the next agent turn
           starts with a clean slate.

        Step 2 is best-effort: the backoff cleanup in step 3 must run no
        matter what step 2 does, otherwise one failed stop would leave the
        chat permanently inside a typing cooldown.

        Args:
            chat_id: The chat whose indicator should stop.

        Returns:
            None. Never raises.
        """
        task = self._typing_tasks.pop(chat_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Send an explicit stop-typing RPC so the recipient's device drops the
        # indicator immediately instead of waiting for Signal's ~5s built-in
        # timeout.  Failures are best-effort -- the backoff state must still be
        # cleared so the next agent turn starts clean.
        try:
            params: Dict[str, Any] = {"account": self.account}
            if chat_id.startswith("group:"):
                params["groupId"] = chat_id[6:]
            else:
                params["recipient"] = [await self._resolve_recipient(chat_id)]
            params["stop"] = True
            await self._rpc(
                "sendTyping",
                params,
                rpc_id="typing-stop",
                log_failures=False,
            )
        except Exception:
            # Best-effort: any RPC failure (or recipient-resolution failure)
            # must not prevent backoff cleanup.
            pass

        self._typing_failures.pop(chat_id, None)
        self._typing_skip_until.pop(chat_id, None)

    async def stop_typing(self, chat_id: str) -> None:
        """Public interface for stopping typing -- called by base adapter's
        _keep_typing finally block to clean up platform-level typing tasks.

        Thin delegation to :meth:`_stop_typing_indicator`; it exists so the
        base class's refresh loop can release platform resources without
        reaching into private members.

        Args:
            chat_id: The chat whose indicator should stop.

        Returns:
            None.
        """
        await self._stop_typing_indicator(chat_id)

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    async def send_reaction(
        self,
        chat_id: str,
        emoji: str,
        target_author: str,
        target_timestamp: int,
    ) -> bool:
        """Send a reaction emoji to a specific message via signal-cli RPC.

        Signal identifies the reaction target by author plus timestamp rather
        than by a message id, which is why
        :meth:`SignalAdapter._handle_envelope` stashes the envelope's sender
        and timestamp into ``MessageEvent.raw_message`` in the first place.

        Args:
            chat_id: The chat (phone number or ``group:<id>``).
            emoji: Reaction emoji string, e.g. the eyes or ballot-box emoji.
            target_author: Phone number / UUID of the message author.
            target_timestamp: Signal timestamp (ms) of the message to react to.

        Returns:
            True when the RPC returned a result, False otherwise.
        """
        params: Dict[str, Any] = {
            "account": self.account,
            "emoji": emoji,
            "targetAuthor": target_author,
            "targetTimestamp": target_timestamp,
        }

        # Reactions deliberately do NOT go through _resolve_recipient: the
        # daemon matches the target author by the identifier the envelope
        # originally carried, and rewriting it could point at the wrong user.
        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [chat_id]

        result = await self._rpc("sendReaction", params)
        if result is not None:
            return True
        logger.debug("Signal: sendReaction failed (chat=%s, emoji=%s)", chat_id[:20], emoji)
        return False

    async def remove_reaction(
        self,
        chat_id: str,
        target_author: str,
        target_timestamp: int,
    ) -> bool:
        """Remove a reaction by sending an empty-string emoji.

        Signal models reaction removal as a separate flag rather than as the
        absence of an emoji, so both ``emoji: ""`` and ``remove: true`` are
        sent together for maximum compatibility with daemon versions.

        Args:
            chat_id: The chat (phone number or ``group:<id>``).
            target_author: Phone number / UUID of the message author.
            target_timestamp: Signal timestamp (ms) of the target message.

        Returns:
            True when the RPC returned a result, False otherwise.
        """
        params: Dict[str, Any] = {
            "account": self.account,
            "emoji": "",
            "targetAuthor": target_author,
            "targetTimestamp": target_timestamp,
            "remove": True,
        }

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [chat_id]

        result = await self._rpc("sendReaction", params)
        return result is not None

    # ------------------------------------------------------------------
    # Processing Lifecycle Hooks (reactions as progress indicators)
    # ------------------------------------------------------------------

    def _extract_reaction_target(self, event: MessageEvent) -> Optional[tuple]:
        """Extract (target_author, target_timestamp) from a MessageEvent.

        Signal reactions are addressed by author plus timestamp, so both must
        come back out of the envelope metadata stored during inbound
        processing. If the event does not carry that raw data, reactions are
        simply skipped for it rather than raising.

        Args:
            event: The inbound message event about to be, or just having
                been, processed.

        Returns:
            A ``(author, timestamp_ms)`` tuple, or ``None`` when the event
            does not carry the raw Signal envelope data needed for
            sendReaction.
        """
        raw = event.raw_message
        if not isinstance(raw, dict):
            return None
        author = raw.get("sender")
        ts = raw.get("timestamp_ms")
        if not author or not ts:
            return None
        return (author, ts)

    def _reactions_enabled(self, event: "MessageEvent" = None) -> bool:
        """Check if message reactions are enabled for this event.

        Two gates:
        1. SIGNAL_REACTIONS env var -- set to false/0/no to disable globally.
        2. DM allowlist -- if SIGNAL_ALLOWED_USERS is set, only react to
           messages from senders in that list.  This prevents unauthorized
           contacts from seeing the in-progress reaction (which fires before
           run.py's auth gate and would otherwise reveal that a bot is
           listening).

        The DM allowlist gate exists because reactions are a side effect that
        is visible to the sender even when the message itself is later
        rejected by authorization.

        Args:
            event: Optional message event whose source is checked against the
                DM allowlist. When ``None``, only the global switch applies.

        Returns:
            True when a reaction should be posted for this event.
        """
        if os.getenv("SIGNAL_REACTIONS", "true").lower() in {"false", "0", "no"}:
            return False
        if event is not None:
            sender = getattr(getattr(event, "source", None), "user_id", None)
            if sender and "*" not in self.dm_allow_from and sender not in self.dm_allow_from:
                return False
        return True

    async def on_processing_start(self, event: MessageEvent) -> None:
        """React with the in-progress emoji when processing begins.

        Fires as soon as the gateway accepts the message, before the agent
        has produced anything, so the sender gets immediate visible feedback
        instead of a silent wait.

        Args:
            event: The accepted inbound message event.

        Returns:
            None. Silent when reactions are disabled or the event carries no
            reaction target.
        """
        if not self._reactions_enabled(event):
            return
        target = self._extract_reaction_target(event)
        if target:
            await self.send_reaction(event.source.chat_id, "馃憖", *target)

    async def on_processing_complete(self, event: MessageEvent, outcome: "ProcessingOutcome") -> None:
        """Swap the in-progress reaction for a terminal success or failure mark.

        On CANCELLED we leave the in-progress reaction in place -- no
        terminal outcome means the reaction should keep reflecting "in
        progress" (matches Telegram).

        The swap is two RPCs: remove first, then add. Signal keeps only one
        reaction per (reactor, target) pair, so removing first avoids the new
        emoji being overwritten by the old one.

        Args:
            event: The inbound message event that finished processing.
            outcome: The :class:`~encre.gateway.platforms.base.ProcessingOutcome`
                for the turn.

        Returns:
            None.
        """
        if not self._reactions_enabled(event):
            return
        if outcome == ProcessingOutcome.CANCELLED:
            return
        target = self._extract_reaction_target(event)
        if not target:
            return
        chat_id = event.source.chat_id
        # Remove the in-progress reaction, then add the final one
        await self.remove_reaction(chat_id, *target)
        if outcome == ProcessingOutcome.SUCCESS:
            await self.send_reaction(chat_id, "✅", *target)
        elif outcome == ProcessingOutcome.FAILURE:
            await self.send_reaction(chat_id, "❌", *target)

    # ------------------------------------------------------------------
    # Chat Info
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get information about a chat/contact.

        Groups are answered locally because the group id is all we reliably
        know about them at this layer. Direct chats attempt a ``getContact``
        lookup so a stored name can replace the raw number; any failure
        falls back to the identifier itself rather than erroring.

        Args:
            chat_id: ``group:<id>`` for groups, otherwise a direct chat id.

        Returns:
            A dict with ``name``, ``type`` (``"dm"`` or ``"group"``) and
            ``chat_id`` keys.
        """
        if chat_id.startswith("group:"):
            return {
                "name": chat_id,
                "type": "group",
                "chat_id": chat_id,
            }

        # Try to resolve contact name. The RPC returns None on failure, in
        # which case the raw identifier is used as the display name.
        result = await self._rpc("getContact", {
            "account": self.account,
            "contactAddress": chat_id,
        })

        name = chat_id
        if result and isinstance(result, dict):
            name = result.get("name") or result.get("profileName") or chat_id

        return {
            "name": name,
            "type": "dm",
            "chat_id": chat_id,
        }
