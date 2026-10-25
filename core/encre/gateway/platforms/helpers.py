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

"""Shared state and formatting plumbing used by platform adapters.

Module position: every adapter under :mod:`encre.gateway.platforms` inherits
from :class:`encre.gateway.platforms.base.BasePlatformAdapter`, but the
cross-cutting problems -- duplicate delivery, thread participation, vendor
formatting limits, media spooling -- are identical across vendors.  This
module centralises them so an adapter has one import to reach them and one
place to fix a bug.

Data flows through these helpers in two directions:

* Inbound: :class:`encre.gateway.run.GatewayRunner` normalises vendor
  events into :class:`encre.gateway.platforms.base.MessageEvent`; adapters
  consult :class:`MessageDeduplicator` before that event is dispatched to
  :meth:`encre.gateway.platforms.base.BasePlatformAdapter.handle_message`,
  and :class:`ThreadParticipationTracker` decides whether a thread message
  that does not mention the bot is even worth processing.
* Outbound: :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`
  pushes agent output through an adapter's ``send`` method; the text
  formatters here translate markdown for vendors that cannot render it, and
  the media helpers materialise attachments as local files that the adapter
  can upload as native attachments.

Public API:
    * :class:`MessageDeduplicator`
    * :class:`ThreadParticipationTracker`
    * :func:`convert_table_to_bullets`
    * :func:`truncate_text`
    * :func:`utf16_len`
    * :func:`redact_phone`
    * :func:`cache_media_from_bytes`
    * :func:`cache_media_from_url`
    * :func:`strip_markdown`
"""

import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any

# Namespace shared with sibling modules under encre.gateway.platforms.
logger = logging.getLogger("encre.gateway.platforms.helpers")


# -- Message deduplication -----------------------------------------------------
# Some vendors redeliver the same message after a reconnect or a retry
# (at-least-once delivery), which would otherwise double-execute the agent
# turn for a single user utterance.


class MessageDeduplicator:
    """Sliding-window deduplication for inbound platform messages.

    Adapters call :meth:`is_duplicate` as the first step of inbound
    processing, before the event is handed to
    :meth:`encre.gateway.platforms.base.BasePlatformAdapter.handle_message`.
    A ``True`` return means the message was already seen inside the window
    and must be dropped without dispatch.

    Invariants:
        * The cache never grows past ``max_size`` entries.
        * Entries older than ``ttl_seconds`` are evicted lazily, at the
          start of the next :meth:`is_duplicate` call.
        * ``is_duplicate`` returns ``False`` exactly once for a given
          fingerprint inside a window and ``True`` on every subsequent call
          until expiry.  The first sighting is therefore the one that gets
          processed.

    Thread-safety:
        A single :class:`threading.Lock` guards all cache access, so the
        object can be shared between adapter threads.  Adapters that are
        purely asyncio can still reuse one instance per platform.

    Attributes:
        _cache: fingerprint -> insertion timestamp, in insertion order.
        _max_size: hard entry cap; the oldest entry is evicted first.
        _ttl: seconds after which an entry is considered stale.
        _lock: serialises read-modify-write on ``_cache``.
    """

    def __init__(self, max_size: int = 1000, ttl_seconds: float = 300.0) -> None:
        """Create a bounded, TTL-backed deduplication cache.

        Args:
            max_size: Maximum number of remembered fingerprints.  Chosen
                generously: the window is sized to survive reconnect
                storms, not to be cheap.
            ttl_seconds: How long a fingerprint suppresses its twin.  Must
                exceed the vendor's longest retry/backoff interval, or a
                slow redelivery will be processed twice.
        """
        # OrderedDict is load-bearing: both the size cap and the TTL sweep
        # rely on oldest-first ordering.
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    def is_duplicate(self, chat_id: str, message_id: str | None = None, text: str = "") -> bool:
        """Check whether this message was already processed in the window.

        The call is also the point at which the message is recorded, so
        callers must only invoke it on messages they are about to dispatch.

        Args:
            chat_id: Conversation identifier, scoping the fingerprint so
                that identical message ids in different chats do not
                collide.
            message_id: Vendor-unique message id when the platform
                provides one.  Preferred because it is stable and exact.
            text: Message body, used only as a fallback discriminator when
                no ``message_id`` is available.

        Returns:
            ``True`` if the fingerprint was already present (drop the
            message), ``False`` if this is the first sighting (proceed).

        Raises:
            No exceptions propagate; all access is lock-protected.
        """
        with self._lock:
            key = self._make_key(chat_id, message_id, text)
            now = time.time()
            self._evict_expired(now)

            if key in self._cache:
                return True

            self._cache[key] = now
            if len(self._cache) > self._max_size:
                # Bounded by construction: evict the oldest fingerprint
                # rather than let memory grow unbounded over the adapter's
                # lifetime.
                self._cache.popitem(last=False)
            return False

    def _make_key(self, chat_id: str, message_id: str | None, text: str) -> str:
        """Derive a stable fingerprint for a message.

        With a vendor message id the key is the literal pair, which is both
        cheap and unambiguous.  Without one the key is a truncated SHA-256
        over the chat and the first 200 characters of the body; hashing
        keeps ``OrderedDict`` keys short and avoids storing raw message
        text in memory, and the 200-character cap prevents very long
        messages from ever dominating the digest.

        Args:
            chat_id: Conversation scope.
            message_id: Optional vendor message id.
            text: Fallback body for content-based deduplication.

        Returns:
            A string suitable as an ``OrderedDict`` key.
        """
        if message_id:
            return f"{chat_id}:{message_id}"
        content = f"{chat_id}:{text[:200]}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    def _evict_expired(self, now: float) -> None:
        """Drop every entry older than the TTL, from the oldest first.

        Only called with ``_lock`` already held.  The sweep is
        amortised: a quiet adapter performs near-zero eviction work per
        call because the loop exits as soon as the first live entry is
        reached.

        Args:
            now: Current wall clock, supplied by the caller so the value
                is consistent across the eviction and the insertion that
                follows it.
        """
        cutoff = now - self._ttl
        while self._cache:
            key, ts = next(iter(self._cache.items()))
            if ts > cutoff:
                break
            self._cache.popitem(last=False)


# -- Thread participation tracking ---------------------------------------------
# Group-chat etiquette varies sharply by platform: on some a plain mention
# wakes the bot, on others only a direct reply does.  Rather than make every
# adapter re-derive the answer, participation is tracked centrally and
# consulted by adapters that receive thread or topic traffic.


class ThreadParticipationTracker:
    """Remember which threads the bot has taken part in.

    The tracker exists to answer one question: is a message in this thread
    addressed to the agent, or is it ambient conversation the agent should
    stay out of?  The bot records a thread the first time it posts into it
    (:meth:`mark_participated`), and adapters check :meth:`has_participated`
    before deciding to respond to an unmentioned thread message.  This
    prevents an agent from barging into conversations it was never called
    into while still following every thread it has entered.

    Invariants:
        * At most ``max_threads`` threads are remembered at any time.
        * Order reflects recency: most recently touched threads sit at the
          end, so eviction drops the stalest entries first.

    Thread-safety:
        No lock is held here; the tracker is written and read only from
        within a single adapter's event loop, which is how adapters in this
        package are driven.

    Attributes:
        _threads: thread key -> timestamp of the most recent
            :meth:`mark_participated` call.
        _max_threads: capacity cap for the tracking window.
    """

    def __init__(self, max_threads: int = 500) -> None:
        """Create a bounded recency table of participated threads.

        Args:
            max_threads: Maximum number of distinct threads to remember.
                Older threads fall out automatically, which is the desired
                behaviour: an ancient thread should not count as current
                context when the bot reappears there.
        """
        self._threads: OrderedDict[str, float] = OrderedDict()
        self._max_threads = max_threads

    def mark_participated(self, thread_key: str) -> None:
        """Record that the bot has spoken in a thread.

        Touching an already-known thread refreshes its position instead of
        adding a duplicate, so a hot thread never inflates the table.

        Args:
            thread_key: Stable identifier for the thread or topic, as
                constructed by the adapter (typically a composite of the
                chat id and the vendor thread id).
        """
        self._threads[thread_key] = time.time()
        self._threads.move_to_end(thread_key)
        while len(self._threads) > self._max_threads:
            self._threads.popitem(last=False)

    def has_participated(self, thread_key: str) -> bool:
        """Report whether the bot has previously spoken in a thread.

        Args:
            thread_key: Same key namespace passed to
                :meth:`mark_participated`.

        Returns:
            ``True`` if the thread is within the remembered window, which
            callers treat as permission to answer without an explicit
            mention.
        """
        return thread_key in self._threads


# -- Text formatting utilities -------------------------------------------------
# Many vendors render only a small markdown subset.  These helpers degrade
# rich output produced by the agent into a form the target platform will
# actually display, rather than shipping markup as literal text.


# Matches a markdown table divider row (``|---|---|``, including alignment
# colons and padding).  Compiled once at import time so it is reusable by
# callers that need to detect table structure outside
# :func:`convert_table_to_bullets`.
TABLE_SEPARATOR_RE = re.compile(r"^\|[-| :]+\|$", re.MULTILINE)


def convert_table_to_bullets(text: str) -> str:
    """Flatten markdown tables into bullet lists of ``Header: value`` pairs.

    Several gateway targets (Telegram, WhatsApp and similar) have no table
    layout, so a table sent as-is appears as a wall of pipe characters.
    The first row is treated as a header row and each data row is rendered
    as one bullet whose cells are labelled with the corresponding header
    cell, preserving the information the table conveyed.

    Args:
        text: Agent output possibly containing one or more markdown tables
            delimited by ``|`` and ending with ``|`` on every row.

    Returns:
        The input with every detected table replaced by its bullet
        rendering.  Text outside table regions is returned untouched, so
        the function is safe to run over a whole message.

    Note:
        Rows whose cells consist only of ``-``, ``:`` and spaces are
        treated as alignment dividers and dropped.  A row shorter than the
        header renders its bare cells; a row longer than the header appends
        the extra cells unlabelled, so no data is silently discarded.
    """
    lines = text.split("\n")
    result: list[str] = []
    headers: list[str] = []
    in_table = False

    for line in lines:
        stripped = line.strip()
        if "|" in stripped and stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            # Alignment divider rows carry formatting metadata only, never
            # content, so they are consumed without producing output.
            if all(set(c) <= {"-", ":", " "} for c in cells):
                continue
            if not in_table:
                headers = cells
                in_table = True
            else:
                # Data row: pair each cell with its header so the flattened
                # row stays readable on its own.
                parts = []
                for i, cell in enumerate(cells):
                    if i < len(headers) and headers[i]:
                        parts.append(f"{headers[i]}: {cell}")
                    else:
                        parts.append(cell)
                # Bullets and " | " separators are the visual cues that
                # replace the table grid on vendors without layout support.
                result.append(f"鈥?{' | '.join(parts)}")
        else:
            if in_table:
                in_table = False
                headers = []
            result.append(line)

    return "\n".join(result)


def truncate_text(text: str, max_length: int, suffix: str = "...") -> str:
    """Clip text to a character budget, marking the omission.

    Vendors enforce hard message-length caps, and hitting them mid-word is
    far more confusing to a user than a visible ellipsis is.  The suffix is
    reserved out of the budget rather than appended on top, so the returned
    string never exceeds ``max_length`` characters.

    Args:
        text: Input text.
        max_length: Maximum length of the returned string, suffix included.
        suffix: Marker appended when truncation occurs.

    Returns:
        ``text`` unchanged when it already fits, otherwise a prefix of it
        followed by ``suffix``.

    Note:
        Length is measured in Python characters, i.e. Unicode code points.
        Adapters that need a UTF-16 code-unit budget should use
        :func:`utf16_len` instead.
    """
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix


def utf16_len(text: str) -> int:
    """Count the UTF-16 code units a string occupies.

    Telegram and a few other vendors size limits in UTF-16 code units rather
    than Unicode code points, so an emoji or any character above the
    basic multilingual plane occupies two units.  Counting in characters
    would understate the true width of a message, so an adapter would believe
    it had more budget than the vendor actually allows and get payloads
    rejected.

    Args:
        text: String to measure.

    Returns:
        Number of UTF-16 code units, obtained by encoding little-endian and
        dividing the byte length by two.
    """
    return len(text.encode("utf-16-le")) // 2


def redact_phone(text: str) -> str:
    """Mask internationally-formatted phone numbers found inside free text.

    Used before logging or echoing user-supplied content that may contain
    contact details, so that full numbers never reach logs or transcripts.

    Args:
        text: Arbitrary content, possibly containing numbers in ``+``
            international format with ten to fifteen digits.

    Returns:
        The input with each matched number reduced to its first four
        characters, four asterisks, and its final four characters.  Values
        outside the matched range are returned verbatim.

    Note:
        The argument is coerced with :func:`str`, so non-string callers are
        tolerated rather than raising.
    """
    import re
    return re.sub(r'\+\d{10,15}', lambda m: m.group(0)[:4] + '****' + m.group(0)[-4:], str(text))


# -- Media cache helpers -------------------------------------------------------
# Agent output and inbound attachments are exchanged as bytes or URLs, but
# native delivery requires a real file on disk that the adapter can upload.
# These helpers perform that materialisation in the system temp directory
# and are the only place that code path allocates attachment files.


def cache_media_from_bytes(
    data: bytes,
    extension: str,
    prefix: str = "media",
) -> str:
    """Write media bytes to a temporary file and return its path.

    This is the inbound half of attachment handling: an adapter downloads
    bytes from the vendor API, hands them here, and then passes the
    resulting path to the agent.  The reverse direction (agent output to a
    platform attachment) is handled by the ``MEDIA:`` tag processing in
    :mod:`encre.gateway.platforms.base`.

    Args:
        data: Raw media payload.
        extension: File extension for the new file.  A leading dot is
            optional and is added when missing, so callers may pass either
            ``".png"`` or ``"png"``.
        prefix: Label embedded in the temp file name, used only for
            post-mortem identification in crash dumps; it has no effect on
            behaviour.

    Returns:
        Absolute path to the created file, ready for upload or hand-off.

    Raises:
        OSError: If the write or the descriptor close fails; callers are
            expected to translate this into a failed :class:`SendResult` or
            to skip the attachment.

    Note:
        The descriptor is closed in a ``finally`` so a failing write cannot
        leak an open file handle, which matters because adapters run for the
        whole lifetime of the gateway process.
    """
    import tempfile
    import os

    if not extension.startswith("."):
        extension = f".{extension}"
    fd, path = tempfile.mkstemp(prefix=f"encre_{prefix}_", suffix=extension)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return path


def cache_media_from_url(url: str, extension: str = ".bin") -> str | None:
    """Fetch media from a URL into a temporary file.

    Used when a vendor hands back a signed content URL instead of inline
    bytes, which is the common case for large uploads.

    Args:
        url: Location to fetch from, normally vendor-signed and therefore
            time-limited.
        extension: File extension for the cache file; a leading dot is
            optional.

    Returns:
        Absolute path to the downloaded file, or ``None`` if the download
        could not be completed.  ``None`` is the error signal here rather
        than an exception so that adapters can degrade gracefully to a text
        link instead of failing the whole turn.

    Note:
        The temp file is created and closed before the download begins, so
        ``urlretrieve`` writes into an already-existing empty path rather
        than racing with its own creation.  On failure the empty file may
        be left behind in the temp directory; it is never reported to the
        caller.
    """
    import urllib.request
    import tempfile
    import os

    if not extension.startswith("."):
        extension = f".{extension}"
    try:
        fd, path = tempfile.mkstemp(prefix="encre_media_", suffix=extension)
        os.close(fd)
        urllib.request.urlretrieve(url, path)
        return path
    except Exception as e:
        logger.warning("Failed to cache media from %s: %s", url, e)
        return None


def strip_markdown(text: str) -> str:
    """Strip markdown markup, returning plain text with spacing normalised.

    A number of vendors accept no markup at all and would otherwise display
    the syntax characters verbatim.  The rules intentionally do not parse
    markdown as a document; each pattern only removes its own delimiter so
    the body text it wrapped survives.

    Args:
        text: Markdown-flavoured output.

    Returns:
        Plain text with emphasis, inline code, links, headings, ordered
        list markers and unordered list bullets removed, runs of three or
        more newlines collapsed to two, and surrounding whitespace trimmed.

    Note:
        Patterns are single-delimiter only, so a bold span closed with a
        different number of asterisks than it opened with is not matched.
        Nested or deeply formatted blocks may therefore retain stray
        markers; the trade-off is keeping the helper safe and predictable
        over a perfect guarantee on complex input.
    """
    text = re.sub(r"\*+([^*]+)\*+", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^###?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def redact_phone(phone: str) -> str:
    """Reduce a phone number to a display-safe fragment.

    Intended for rendering a number to a user who is not the owner, where
    enough context remains to recognise the number but not enough to use
    it.  Non-digit characters apart from the leading plus are discarded
    first, so formatting such as dashes and parentheses does not confuse
    the masking.

    Args:
        phone: Phone number in any common formatting.

    Returns:
        An ellipsis followed by the final four digits when the cleaned
        value is longer than four characters, otherwise the cleaned value
        unchanged.

    Note:
        This module-level definition shadows the free-text variant earlier
        in the file, so code importing :func:`redact_phone` receives this
        single-number form.
    """
    cleaned = re.sub(r"[^\d+]", "", phone)
    if len(cleaned) > 4:
        return f"...{cleaned[-4:]}"
    return cleaned
