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

"""Process-wide attachment rate-limit model for the Signal adapter.

This module is the pacing half of Encre's Signal surface. It belongs to the
Python gateway layer alongside
:mod:`encre.gateway.platforms.signal` and
:mod:`encre.gateway.platforms.signal_format`.

Signal-Server enforces a per-account attachment quota that signal-cli surfaces as
``RateLimitException`` (HTTP 429) responses. Rather than discovering that limit
the hard way, this module keeps a token-bucket *model* of the server's capacity so
bulk image delivery can self-throttle before sending. The server remains the
source of truth: whenever the model drifts, Signal still returns 429s and the
model recalibrates from the authoritative ``Retry-After`` hint.

Producers:
    * :meth:`encre.gateway.platforms.signal.SignalAdapter.send_multiple_images`
      calls :meth:`SignalAttachmentScheduler.acquire` before each attachment
      batch, :meth:`SignalAttachmentScheduler.report_rpc_duration` after a
      successful send, and :meth:`SignalAttachmentScheduler.feedback` on a 429.

Concurrency model: concurrent :meth:`SignalAttachmentScheduler.acquire` calls
serialise through an :class:`asyncio.Lock`, giving FIFO fairness across the agent
sessions that share one signal-cli daemon inside a single process.

Public API:
    * :class:`SignalAttachmentScheduler` -- the token-bucket model.
    * :func:`get_scheduler` -- process-wide singleton accessor.
    * :class:`SignalRateLimitError` / :class:`SignalSchedulerError` -- typed
      failures that let callers branch on rate-limit vs. misuse.
    * :func:`_signal_send_timeout` / :func:`_format_wait` -- timeout and pacing
      helpers used by the adapter's send path.
"""

import asyncio
import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hard cap on attachments per single send RPC. Sourced from the
# Signal-Android and Signal-Desktop clients; exceeding it is rejected server-side.
SIGNAL_MAX_ATTACHMENTS_PER_MSG = 32
# Server-side token-bucket capacity for attachment rate limiting. The bucket is
# credited at most up to this many tokens, which bounds how much a quiet period
# can be banked toward a later burst.
SIGNAL_RATE_LIMIT_BUCKET_CAPACITY = 50
# Fallback per-token refill window in seconds. Used until signal-cli reports a
# real Retry-After; signal-cli before v0.14.3 never surfaced that field, so this
# documented default keeps the model usable on old daemons.
SIGNAL_RATE_LIMIT_DEFAULT_RETRY_AFTER = 4
# Attempts per batch: the initial send plus a single retry. Retries cover
# transient network and delivery failures; rate limits are handled by pacing.
SIGNAL_RATE_LIMIT_MAX_ATTEMPTS = 2
# Once the projected inter-batch wait exceeds this many seconds, the adapter
# posts a user-visible pacing notice instead of stalling silently.
SIGNAL_BATCH_PACING_NOTICE_THRESHOLD = 10.0
# signal-cli (v0.14.3+) JSON-RPC error code for a server RateLimitException.
# It is a negative integer, distinct from transport-level HTTP status codes.
SIGNAL_RPC_ERROR_RATELIMIT = -5


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SignalRateLimitError(Exception):
    """Raised when signal-cli reports that a send hit the attachment quota.

    :meth:`encre.gateway.platforms.signal.SignalAdapter._rpc` raises this for
    rate-limit responses, but only for callers that opted in with
    ``raise_on_rate_limit=True``; other callers get a swallowed ``None`` so that
    best-effort paths such as typing indicators are not interrupted.

    The error carries the server-supplied per-token Retry-After window when
    signal-cli reports it (v0.14.3+). Callers feed that value back into
    :meth:`SignalAttachmentScheduler.feedback` so the model re-derives its refill
    rate from the authoritative source rather than guessing.

    Attributes:
        retry_after: Per-token Retry-After seconds, or ``None`` when the running
            signal-cli version does not expose the field.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        """Initialise with a failure message and optional server retry window.

        Args:
            message: Human-readable description of the rate-limit failure, usually
                taken verbatim from the signal-cli error payload.
            retry_after: Per-token Retry-After seconds reported by the server, or
                ``None`` when unavailable.
        """
        super().__init__(message)
        self.retry_after = retry_after


class SignalSchedulerError(Exception):
    """Raised for invalid use of the attachment scheduler.

    Currently only :meth:`SignalAttachmentScheduler.acquire` raises it, when a
    caller asks for more tokens than the bucket capacity can ever accumulate. Such
    a request would block forever, so it fails fast instead of hanging the event
    loop.
    """


# ---------------------------------------------------------------------------
# Rate-limit detection
# ---------------------------------------------------------------------------

# Matches libsignal-net's RetryLaterException wording ("Retry after 4 seconds" /
# "Retry after 1 second"). signal-cli wraps these as AttachmentInvalidException
# rather than RateLimitException during attachment upload, so the typed error path
# never fires and the window has to be parsed out of the message text.
_RETRY_AFTER_RE = re.compile(r"Retry after (\d+(?:\.\d+)?)\s*second", re.IGNORECASE)


def _extract_retry_after_seconds(err: Any) -> Optional[float]:
    """Pull the per-token Retry-After window from a signal-cli rate-limit error.

    Two sources are consulted, in order of authority:
        1. ``error.data.response.results[*].retryAfterSeconds`` -- the structured
           field signal-cli v0.14.3+ surfaces for a plain RateLimitException.
        2. ``"Retry after N seconds"`` parsed from the message text -- covers
           libsignal-net's RetryLaterException, which is wrapped as
           AttachmentInvalidException during attachment upload and leaves the
           structured field ``null``.

    Args:
        err: The signal-cli error. Either a ``dict`` in JSON-RPC error shape, or
            an arbitrary exception object whose string form is inspected.

    Returns:
        The Retry-After window in seconds, or ``None`` when neither source yields
        a value.
    """
    msg = ""
    if isinstance(err, dict):
        data = err.get("data") or {}
        response = data.get("response") or {}
        results = response.get("results") or []
        candidates = [
            r.get("retryAfterSeconds") for r in results
            if isinstance(r, dict) and r.get("retryAfterSeconds")
        ]
        if candidates:
            # The server reports a window per result, not per batch. Taking the
            # largest is the conservative bound for recovering the whole batch.
            return float(max(candidates))
        msg = str(err.get("message", ""))
    else:
        msg = str(err)
    match = _RETRY_AFTER_RE.search(msg)
    return float(match.group(1)) if match else None


def _is_signal_rate_limit_error(err: Any) -> bool:
    """Return ``True`` when a signal-cli RPC error reflects a rate-limit failure.

    Three layers are matched, because signal-cli's error taxonomy is inconsistent
    across versions and code paths:
        * the typed ``RATELIMIT_ERROR`` code (signal-cli v0.14.3+, plain
          RateLimitException);
        * legacy ``[429]`` / ``RateLimitException`` substrings;
        * libsignal-net's ``RetryLaterException`` / ``Retry after N seconds``
          wording, which leaks through ``AttachmentInvalidException`` when the
          limit is hit mid-upload. signal-cli never re-tags these as
          RateLimitException, so the substring is the only available signal.

    Args:
        err: The signal-cli error, either a ``dict`` or an arbitrary exception.

    Returns:
        ``True`` when the error is recognised as a rate-limit condition.
    """
    if isinstance(err, dict) and err.get("code") == SIGNAL_RPC_ERROR_RATELIMIT:
        return True

    message = (
        str(err.get("message", ""))
        if isinstance(err, dict)
        else str(err)
    )
    msg_lower = message.lower()
    return (
        "[429]" in message
        or "ratelimit" in msg_lower
        or "retrylaterexception" in msg_lower
        or "retry after" in msg_lower
    )


# ---------------------------------------------------------------------------
# Formatting and timeout helpers
# ---------------------------------------------------------------------------

def _format_wait(seconds: float) -> str:
    """Format a wait duration for a user-facing pacing notice.

    Sub-90-second waits read better as seconds; anything longer rounds to whole
    minutes so the notice stays short enough to fit in a Signal message.

    Args:
        seconds: The wait duration in seconds. Values below zero clamp to zero so
            a negative projection never renders as a nonsense label.

    Returns:
        A compact label such as ``"42s"`` or ``"3 min"``.
    """
    s = max(0.0, seconds)
    if s < 90:
        return f"{int(round(s))}s"
    return f"{max(1, int(round(s / 60)))} min"


def _signal_send_timeout(num_attachments: int) -> float:
    """Compute the HTTP timeout for a Signal ``send`` RPC.

    signal-cli uploads attachments serially inside the call, so server-side time
    scales with batch size. A flat 30s timeout is fine for text but truncates
    large attachment batches mid-upload, which would log a phantom failure even
    though signal-cli completes the send seconds later and the recipient does get
    the images. The timeout therefore scales at 5s per attachment with a 60s
    floor, while text-only sends keep the 30s floor.

    Args:
        num_attachments: The number of attachments in the outgoing message. Zero
            or negative means a text-only send.

    Returns:
        The timeout in seconds to pass to the send RPC.
    """
    if num_attachments <= 0:
        return 30.0
    return max(60.0, 5.0 * num_attachments)


# ---------------------------------------------------------------------------
# Token-bucket scheduler
# ---------------------------------------------------------------------------

class SignalAttachmentScheduler:
    """Token-bucket model of Signal-Server's attachment rate limit.

    The bucket holds up to ``capacity`` tokens (default 50, matching the
    server-side bucket). Each attachment consumes one token and tokens refill at
    ``refill_rate`` tokens per second, calibrated from the per-token Retry-After
    hint the server sends with a 429. Before any 429 has been observed, the
    documented default of one token per four seconds applies.

    This is deliberately a *model* and not an enforcement point: :meth:`acquire`
    does not deduct tokens, and the bucket only moves when the caller reports a
    real outcome via :meth:`report_rpc_duration` or :meth:`feedback`. That keeps
    the simulation aligned with the server's timeline instead of drifting from it
    on optimistic accounting.

    Thread-safety: all state is mutated only while holding ``self._lock``. The
    two read-only projections (:meth:`estimate_wait` and :method:`state`) are
    lock-free on purpose, because they feed informational notices and a momentary
    race against a concurrent acquire is harmless.

    Lifecycle: constructed via :func:`get_scheduler` (lazily, on first use) and
    held for the process lifetime. :func:`_reset_scheduler` drops it for tests.

    Attributes:
        capacity: Maximum tokens the bucket can hold.
        tokens: Tokens currently held; a float because refill accrues fractionally.
        refill_rate: Tokens per second, i.e. the reciprocal of the per-token
            refill window. Recalibrated by :meth:`feedback`.
        last_refill: :func:`time.monotonic` anchor for the last refill or report.
        _lock: Serialises ``acquire`` and the mutating reports.
    """

    def __init__(
        self,
        capacity: float = float(SIGNAL_RATE_LIMIT_BUCKET_CAPACITY),
        default_retry_after: float = float(SIGNAL_RATE_LIMIT_DEFAULT_RETRY_AFTER),
    ) -> None:
        """Initialise the bucket full and set the default refill rate.

        Starting full mirrors the server: a fresh account has never been throttled,
        so the first burst should not be needlessly delayed.

        Args:
            capacity: Maximum tokens the bucket can hold. Defaults to
                :data:`SIGNAL_RATE_LIMIT_BUCKET_CAPACITY` (50).
            default_retry_after: Per-token refill window in seconds used until the
                server supplies an authoritative value. Defaults to
                :data:`SIGNAL_RATE_LIMIT_DEFAULT_RETRY_AFTER` (4).
        """
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.refill_rate = 1.0 / float(default_retry_after)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Accrue tokens earned since ``last_refill`` without exceeding capacity.

        Uses a monotonic clock so wall-clock adjustments (NTP steps, DST changes,
        VM pauses) cannot rewind the bucket or grant a free burst. Called only
        while the caller holds ``self._lock``.
        """
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed > 0 and self.tokens < self.capacity:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def estimate_wait(self, n: int) -> float:
        """Estimate seconds until ``n`` tokens would be available.

        Used to decide whether to post a user-facing pacing notice *before*
        committing to an :meth:`acquire` that might block for a long time. The
        estimate is computed against a projected bucket state rather than the
        stored value, so time elapsed since the last refill is accounted for.

        This method takes no lock. A brief race with a concurrent acquire yields a
        slightly optimistic estimate, which is acceptable because the result only
        gates an informational message.

        Args:
            n: The number of tokens the caller intends to acquire.

        Returns:
            The estimated wait in seconds, or ``0.0`` when enough tokens already
            appear to be available.
        """
        now = time.monotonic()
        elapsed = now - self.last_refill
        projected = self.tokens
        if elapsed > 0 and projected < self.capacity:
            projected = min(self.capacity, projected + elapsed * self.refill_rate)
        deficit = n - projected
        if deficit <= 0:
            return 0.0
        return deficit / self.refill_rate

    async def acquire(self, n: int) -> float:
        """Block until ``n`` tokens are available, returning total seconds slept.

        No tokens are deducted here. The bucket is a read-only projection of
        server capacity, so the caller must report the real outcome afterwards:
        :meth:`report_rpc_duration` on success, :meth:`feedback` on a 429.

        The simulation is imperfect when many coroutines request large uploads at
        once, because ``report_rpc_duration`` lags the actual server-side
        accounting. That is acceptable because Signal-Server remains ground truth
        and will raise rate-limit exceptions that trigger recalibration.

        The lock is released around ``asyncio.sleep`` so other callers can
        interleave, and the loop re-checks after each sleep in case the deadline
        computed from the model was pessimistic.

        Args:
            n: The number of tokens required before the send may proceed.

        Returns:
            The total seconds spent sleeping while waiting.

        Raises:
            SignalSchedulerError: If ``n`` exceeds :attr:`capacity`, which the
                bucket can never satisfy and would therefore block forever.
        """
        if n <= 0:
            return 0.0
        if n > self.capacity:
            raise SignalSchedulerError(
                f"Signal scheduler was called requesting {n} tokens "
                f"(max is {self.capacity})",
            )

        total_slept = 0.0
        first_pass = True
        while True:
            async with self._lock:
                self._refill()
                if self.tokens >= n:
                    # Log only when we actually waited, so unblocked fast paths do
                    # not flood DEBUG output.
                    if not first_pass or total_slept > 0:
                        logger.debug(
                            "Signal scheduler: tokens sufficient for %d "
                            "(remaining=%.1f, total_slept=%.1fs)",
                            n, self.tokens, total_slept,
                        )
                    return total_slept
                deficit = n - self.tokens
            # wait = how long until the shortfall accrues, in seconds.
            wait = deficit / self.refill_rate
            if first_pass:
                logger.info(
                    "Signal scheduler: pausing %.1fs for %d tokens "
                    "(available=%.1f, deficit=%.1f, refill=%.4f/s 鈮?%.1fs/token)",
                    wait, n, self.tokens, deficit,
                    self.refill_rate, 1.0 / self.refill_rate,
                )
                first_pass = False
            await asyncio.sleep(wait)
            total_slept += wait

    async def report_rpc_duration(self, rpc_duration: float, n_attachments: int) -> None:
        """Record an attachment-send RPC that just completed successfully.

        Deducts ``n_attachments`` tokens *without* crediting any refill that
        occurred during the upload window. Signal-Server checks the bucket at RPC
        start and does not refill while the request is being processed; refill
        resumes only after the response. Crediting upload-time refill would
        accumulate drift that eventually triggers 429s on batches the model
        believed were affordable.

        ``last_refill`` advances to now so the next :meth:`acquire` or
        :meth:`_refill` begins counting from this point rather than re-crediting
        the upload interval.

        Args:
            rpc_duration: Wall-clock seconds the completed RPC took. Used only to
                decide log level, not to adjust the bucket.
            n_attachments: Attachments the RPC sent, i.e. tokens to deduct.
        """
        if n_attachments <= 0:
            return

        async with self._lock:
            now = time.monotonic()
            token_before = self.tokens
            self.tokens = max(0.0, token_before - float(n_attachments))
            self.last_refill = now
        # Large slow uploads are worth surfacing at INFO; small ones stay DEBUG.
        logger.log(
            logging.INFO if rpc_duration > 10 and n_attachments > 5 else logging.DEBUG,
            "Signal scheduler: RPC for %d att took %.1fs 鈥?"
            "tokens %.1f 鈫?%.1f (deducted=%d, no upload refill credited, refill=%.4fs鈦宦?",
            n_attachments, rpc_duration,
            token_before, self.tokens,
            n_attachments, self.refill_rate,
        )

    def feedback(self, retry_after: Optional[float], n_attempted: int) -> None:
        """Apply server feedback received after a 429 response.

        ``retry_after`` is the per-token refill window the server reports, and the
        server is authoritative, so ``refill_rate`` is re-derived from it. The
        bucket is then drained to zero and the clock re-anchored, which forces the
        next :meth:`acquire` to wait out the drain the server implied rather than
        optimistically assuming capacity.

        Args:
            retry_after: Per-token Retry-After seconds from the server, or
                ``None`` when the running signal-cli did not surface it. When
                ``None`` the existing rate is kept but the bucket is still drained.
            n_attempted: Attachments the failed attempt tried to send. Retained for
                symmetry with the other scheduler methods; not used in the math.
        """
        if retry_after and retry_after > 0:
            new_rate = 1.0 / float(retry_after)
            if new_rate != self.refill_rate:
                logger.info(
                    "Signal scheduler: calibrating refill_rate to %.4f tokens/sec "
                    "(server retry_after=%.1fs per token)",
                    new_rate, retry_after,
                )
                self.refill_rate = new_rate
        self.tokens = 0.0
        self.last_refill = time.monotonic()

    def state(self) -> dict:
        """Return current scheduler state for diagnostic logging.

        Read-only and side-effect free: it does not advance ``last_refill``, so
        logging paths can call it freely without perturbing the bucket. The token
        count is projected forward to now so the reported figure reflects accrued
        refill rather than a stale stored value.

        Returns:
            A dict with ``tokens``, ``capacity``, ``refill_rate`` and
            ``refill_seconds_per_token`` keys.
        """
        now = time.monotonic()
        elapsed = now - self.last_refill
        projected = self.tokens
        if elapsed > 0 and projected < self.capacity:
            projected = min(self.capacity, projected + elapsed * self.refill_rate)
        return {
            "tokens": round(projected, 1),
            "capacity": int(self.capacity),
            "refill_rate": round(self.refill_rate, 4),
            "refill_seconds_per_token": round(1.0 / self.refill_rate, 1) if self.refill_rate > 0 else float("inf"),
        }


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

# Held at module scope so every SignalAdapter in the process paces against the
# same bucket. Multiple adapters driving the same signal-cli daemon must share one
# model, or each would believe it has full capacity.
_scheduler: Optional[SignalAttachmentScheduler] = None


def get_scheduler() -> SignalAttachmentScheduler:
    """Return the process-wide scheduler, creating it on first access.

    Construction is lazy because the defaults are only known at call time and
    tests want to be able to reset the instance between cases. Creation is not
    guarded by a lock; concurrent first access can construct twice, but both
    instances start identically full so the outcome is equivalent.

    Returns:
        The shared :class:`SignalAttachmentScheduler` singleton.
    """
    global _scheduler
    if _scheduler is None:
        _scheduler = SignalAttachmentScheduler()
        logger.info(
            "Signal scheduler: created (capacity=%d tokens, refill=%.4f/s 鈮?%.1fs/token)",
            int(_scheduler.capacity),
            _scheduler.refill_rate,
            1.0 / _scheduler.refill_rate,
        )
    return _scheduler


def _reset_scheduler() -> None:
    """Drop the cached scheduler so the next :func:`get_scheduler` builds a fresh one.

    Test-only helper. Never call from production paths, since it discards any
    calibrated refill rate and token state accumulated so far.
    """
    global _scheduler
    _scheduler = None
