#!/usr/bin/env python3

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

import asyncio
import hashlib
import json
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, ClassVar

from encre.logging_config import get_logger

logger = get_logger(__name__)


class _ErrorCategory(Enum):
    """Categorises a tool-execution error so the recovery engine can choose
    the appropriate remediation strategy.

    Each category maps to a distinct recovery path:
    - ``TRANSIENT`` / ``MODEL_ERROR``: retry with exponential backoff
      (rate limits, timeouts, network blips, upstream API errors).
    - ``INPUT_MALFORMED``: correct the arguments once and retry; further
      retries with the same bad input are pointless.
    - ``TOOL_NOT_FOUND`` / ``PERMISSION_DENIED``: no recovery possible
      short of replanning; abort the turn and feed the error to the model.
    - ``TOOL_EXECUTION`` / ``SANDBOX_ERROR``: may be transient or point
      to a missing dependency; fallback chains are attempted first.
    - ``UNKNOWN``: a catch-all that gets one backoff retry before
      aborting, preventing infinite loops on truly unexpected failures.
    """

    TRANSIENT = auto()  # Rate limit, timeout, network -- retry with backoff
    INPUT_MALFORMED = auto()  # Bad JSON, missing required param -- don't retry blindly
    TOOL_NOT_FOUND = auto()  # Unknown tool -- can't recover
    PERMISSION_DENIED = auto()  # Blocked by safety/hook/user -- don't retry
    TOOL_EXECUTION = auto()  # Tool ran but returned error -- may retry or fallback
    SANDBOX_ERROR = auto()  # Sandbox failure -- may retry
    MODEL_ERROR = auto()  # Backend/model error -- retry with backoff
    UNKNOWN = auto()  # Unexpected -- retry once


class _RecoveryAction(Enum):
    """Decision a recovery engine can take after a tool execution failure.

    The actions are ordered from most aggressive (retry) to most severe
    (abort session).  The engine walks this decision tree per-error:

    1. **Retry paths** -- ``RETRY``, ``RETRY_WITH_BACKOFF``,
       ``RETRY_WITH_FIXED_ARGS`` for recoverable transient failures.
    2. **Fallback path** -- ``FALLBACK_TOOL`` tries a substitute tool
       (e.g. ``file_read`` -> ``bash``) when the primary tool is
       unavailable or consistently fails.
    3. **Degradation** -- ``DEGRADE`` accepts a partial result and
       continues; used when the tool output is best-effort.
    4. **Skip** -- ``SKIP`` proceeds as if the tool succeeded with an
       empty/partial result, for non-critical helpers.
    5. **Reflection** -- ``REFLECT`` feeds the failure pattern back to
       the model so it can replan its strategy.
    6. **Abort** -- ``ABORT_TURN`` ends the current turn (model replans);
       ``ABORT_SESSION`` ends the whole session for unrecoverable errors.
    """

    RETRY = auto()  # Retry the same tool with same args
    RETRY_WITH_BACKOFF = auto()  # Retry with exponential backoff
    RETRY_WITH_FIXED_ARGS = auto()  # Retry with modified args (corrected)
    FALLBACK_TOOL = auto()  # Try a different tool for the same intent
    DEGRADE = auto()  # Accept partial result and continue (L3)
    SKIP = auto()  # Skip this tool entirely
    REFLECT = auto()  # L4: Feed strategy reflection to model for replanning
    ABORT_TURN = auto()  # Abort this turn, feed error to model
    ABORT_SESSION = auto()  # Unrecoverable, end the session


@dataclass
class _RecoveryDecision:
    action: _RecoveryAction
    reason: str = ""
    retry_delay_seconds: float = 0.0
    fallback_tool: str = ""
    fallback_args: dict[str, Any] | None = None
    modified_args: dict[str, Any] | None = None


@dataclass
class RecoveryState:
    """Tracks recovery state for a single tool execution within a turn."""

    tool_name: str
    tool_args: dict[str, Any]
    attempts: int = 0
    max_retries: int = 3
    last_error: str = ""
    last_category: _ErrorCategory = _ErrorCategory.UNKNOWN
    recovery_history: list[_RecoveryDecision] = field(default_factory=list)
    succeeded: bool = False
    final_result: str = ""


class RecoveryConfig:
    """Global recovery behavior configuration."""

    MAX_RETRIES = 3
    MAX_FALLBACK_DEPTH = 2
    BASE_BACKOFF_SECONDS = 1.0
    MAX_BACKOFF_SECONDS = 60.0
    BACKOFF_MULTIPLIER = 2.0

    # Fallback chains: if tool X fails, try tool Y
    FALLBACK_CHAINS: ClassVar[dict[str, list[str]]] = {
        "file_read": ["bash"],  # Use cat/head if read fails
        "grep": ["bash"],  # Use grep command directly
        "glob": ["bash"],  # Use ls/find
        "web_fetch": ["bash"],  # Use curl
        "web_search": ["web_fetch"],  # Try direct fetch
        "file_edit": ["bash"],  # Use sed
        "file_write": ["bash"],  # Use cat/echo
    }

    # Error classification patterns
    ERROR_PATTERNS: ClassVar[dict[str, _ErrorCategory]] = {
        "timed out": _ErrorCategory.TRANSIENT,
        "timeout": _ErrorCategory.TRANSIENT,
        "connection refused": _ErrorCategory.TRANSIENT,
        "connection error": _ErrorCategory.TRANSIENT,
        "rate limit": _ErrorCategory.TRANSIENT,
        "too many requests": _ErrorCategory.TRANSIENT,
        "service unavailable": _ErrorCategory.TRANSIENT,
        "internal server error": _ErrorCategory.TRANSIENT,
        "bad gateway": _ErrorCategory.TRANSIENT,
        "gateway timeout": _ErrorCategory.TRANSIENT,
        "invalid json": _ErrorCategory.INPUT_MALFORMED,
        "json decode error": _ErrorCategory.INPUT_MALFORMED,
        "unknown tool": _ErrorCategory.TOOL_NOT_FOUND,
        "permission denied": _ErrorCategory.PERMISSION_DENIED,
        "blocked by hook": _ErrorCategory.PERMISSION_DENIED,
        "denied by user": _ErrorCategory.PERMISSION_DENIED,
        "sandbox": _ErrorCategory.SANDBOX_ERROR,
        "api error": _ErrorCategory.MODEL_ERROR,
    }


def classify_error(error_message: str) -> _ErrorCategory:
    """Classify an error message into a recovery category."""
    lower = error_message.lower()
    for pattern, category in RecoveryConfig.ERROR_PATTERNS.items():
        if pattern in lower:
            return category
    return _ErrorCategory.UNKNOWN


def compute_backoff(attempt: int, base: float = 1.0, max_wait: float = 60.0) -> float:
    """Exponential backoff with jitter."""
    wait = min(base * (2**attempt), max_wait)
    # Add jitter: +/- 25%
    import random

    jitter = wait * 0.25 * (random.random() * 2 - 1)
    return max(0.0, wait + jitter)


#: Retry-family recovery actions -- every one of them consumes budget before
#: it is granted.
_RETRY_FAMILY_ACTIONS = frozenset(
    {
        _RecoveryAction.RETRY,
        _RecoveryAction.RETRY_WITH_BACKOFF,
        _RecoveryAction.RETRY_WITH_FIXED_ARGS,
        _RecoveryAction.FALLBACK_TOOL,
    }
)

#: Error categories for which a retry of a guarded (idempotency-tracked)
#: call is deterministic-safe: the failure happened strictly *before* the
#: tool could apply any effect.  Everything else (timeouts, model errors,
#: sandbox failures, unknown) leaves the previous attempt's outcome
#: uncertain, so an automatic retry risks double application.
_IDEM_SAFE_RETRY_CATEGORIES = frozenset(
    {
        _ErrorCategory.INPUT_MALFORMED,
        _ErrorCategory.TOOL_NOT_FOUND,
        _ErrorCategory.PERMISSION_DENIED,
    }
)


def compute_args_hash(args: dict[str, Any]) -> str:
    """Stable short hash of a tool-args dict (content-addressed, order-free).

    Args are never stored verbatim -- only this digest is persisted, so the
    ledger carries no payloads and no secrets.
    """
    try:
        payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        payload = repr(args)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class IdempotencyRecord:
    """One journal entry for a guarded tool call.

    ``key`` is content-addressed (``"<tool>|<args_hash>"``) and deliberately
    excludes the turn: after a crash the turn counter moves, so the key must
    stay stable across restarts.  ``turn_key`` is recorded for audit only.
    """

    key: str
    turn_key: str
    tool_name: str
    args_hash: str
    status: str = "unknown"  # "unknown" | "succeeded" | "failed"
    result_summary: str = ""
    ts: float = field(default_factory=time.time)


class IdempotencyLedger:
    """Append-only idempotency journal for guarded tool calls.

    Records live in ``<session_dir>/idempotency.jsonl`` (one JSON object per
    line, newest last) plus a bounded in-memory index of the newest
    :data:`MAX_ENTRIES` keys.  The file is never rewritten: a crash between
    ``mark_started`` and ``mark_result`` leaves an ``unknown`` record on disk,
    which is exactly the signal the executor needs to refuse a blind re-run.
    Args values are never persisted -- only :func:`compute_args_hash` digests
    and a truncated result summary.
    """

    MAX_ENTRIES = 512
    _SUMMARY_LIMIT = 500

    def __init__(self, session_dir: str | Path) -> None:
        self._path = Path(session_dir) / "idempotency.jsonl"
        self._index: dict[str, IdempotencyRecord] = {}
        self._order: deque[str] = deque()
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        """Rebuild the in-memory index from the journal (newest wins)."""
        if not self._path.exists():
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            logger.warning("IdempotencyLedger read failed: %s", self._path, exc_info=True)
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = str(data.get("key", ""))
            if not key:
                continue
            if key not in self._index:
                self._order.append(key)
            self._index[key] = IdempotencyRecord(
                key=key,
                turn_key=str(data.get("turn_key", "")),
                tool_name=str(data.get("tool_name", "")),
                args_hash=str(data.get("args_hash", "")),
                status=str(data.get("status", "unknown")),
                result_summary=str(data.get("result_summary", ""))[: self._SUMMARY_LIMIT],
                ts=float(data.get("ts", 0.0)),
            )
        self._evict_locked()

    def _evict_locked(self) -> None:
        """Bound the in-memory index (caller holds ``_lock``)."""
        while len(self._order) > self.MAX_ENTRIES:
            oldest = self._order.popleft()
            self._index.pop(oldest, None)

    def _append_locked(self, record: IdempotencyRecord) -> None:
        """Append one record to the journal (caller holds ``_lock``)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "key": record.key,
                            "turn_key": record.turn_key,
                            "tool_name": record.tool_name,
                            "args_hash": record.args_hash,
                            "status": record.status,
                            "result_summary": record.result_summary[: self._SUMMARY_LIMIT],
                            "ts": record.ts,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            logger.warning("IdempotencyLedger append failed key=%s", record.key, exc_info=True)

    def lookup(self, key: str) -> IdempotencyRecord | None:
        """Return the latest record for ``key``, or ``None``."""
        with self._lock:
            return self._index.get(key)

    def mark_started(self, key: str, tool_name: str, args: dict[str, Any], turn_key: str = "") -> None:
        """Journal an in-flight attempt (status ``unknown``)."""
        record = IdempotencyRecord(
            key=key,
            turn_key=turn_key,
            tool_name=tool_name,
            args_hash=compute_args_hash(args),
        )
        with self._lock:
            self._index[key] = record
            if key not in self._order:
                self._order.append(key)
            self._evict_locked()
            self._append_locked(record)

    def mark_result(self, key: str, status: str, result_summary: str = "", turn_key: str = "") -> None:
        """Journal a terminal status for ``key`` (append-only overwrite)."""
        with self._lock:
            existing = self._index.get(key)
            record = IdempotencyRecord(
                key=key,
                turn_key=turn_key or (existing.turn_key if existing else ""),
                tool_name=existing.tool_name if existing else "",
                args_hash=existing.args_hash if existing else "",
                status=status,
                result_summary=result_summary[: self._SUMMARY_LIMIT],
            )
            self._index[key] = record
            if key not in self._order:
                self._order.append(key)
            self._evict_locked()
            self._append_locked(record)


class RetryBudget:
    """Purely numeric retry budget: per-scope turn limits + a session cap.

    No vocabulary, no heuristics -- every granted retry (model round, tool
    retry, reactive compaction) spends a counted unit.  When a scope's turn
    limit or the session total is exhausted, ``try_spend`` returns ``False``
    and the caller must degrade (skip / release) instead of looping.
    """

    #: Per-turn caps by scope.
    TURN_SCOPE_LIMITS: ClassVar[dict[str, int]] = {
        "model": 6,
        "tool": 8,
        "compact": 3,
    }
    #: Total units any single session may spend across all scopes.
    SESSION_TOTAL_LIMIT: ClassVar[int] = 40

    def __init__(self) -> None:
        self._turn_spent: dict[str, int] = {}
        self._session_spent = 0

    def reset_turn(self) -> None:
        """Clear per-turn counters (session counters are never reset)."""
        self._turn_spent.clear()

    def try_spend(self, scope: str, amount: int = 1) -> bool:
        """Spend ``amount`` units of ``scope``; ``False`` means denied.

        A denial changes no counters, so callers can retry the same spend
        after a reset without drift.
        """
        amount = max(1, int(amount))
        turn_limit = self.TURN_SCOPE_LIMITS.get(scope)
        if turn_limit is None:
            return False
        turn_used = self._turn_spent.get(scope, 0)
        if turn_used + amount > turn_limit:
            return False
        if self._session_spent + amount > self.SESSION_TOTAL_LIMIT:
            return False
        self._turn_spent[scope] = turn_used + amount
        self._session_spent += amount
        return True

    def exhausted(self, scope: str) -> bool:
        """Whether ``scope`` has no turn budget left."""
        limit = self.TURN_SCOPE_LIMITS.get(scope)
        if limit is None:
            return True
        return self._turn_spent.get(scope, 0) >= limit

    @property
    def turn_spent(self) -> dict[str, int]:
        """Snapshot of per-turn spend by scope (for audit views)."""
        return dict(self._turn_spent)

    @property
    def session_spent(self) -> int:
        """Total units spent this session."""
        return self._session_spent


class ErrorRecoveryEngine:
    """Multi-path error recovery engine for tool execution failures.

    Recovery paths (7 total):
      1. RETRY_SAME  -- transient error -> immediate retry
      2. RETRY_BACKOFF -- rate limit/timeout -> exponential backoff retry
      3. FIX_ARGS    -- malformed input -> try to infer correct args and retry
      4. FALLBACK    -- tool unavailable -> try fallback chain
      5. DEGRADE     -- partial success acceptable -> continue with partial result
      6. SKIP        -- non-critical tool -> skip and continue
      7. ABORT       -- unrecoverable -> feed error to model for replanning

    Termination conditions (10 total):
      1. Max retries exceeded (per-tool)
      2. Max fallback depth exceeded
      3. Max session errors exceeded
      4. Permission denied (no retry)
      5. Tool not found (no retry)
      6. Critical system error (abort)
      7. Same error repeats 3+ times (stuck loop)
      8. Backoff total time exceeds turn timeout
      9. All fallback tools exhausted
      10. Manual abort requested
    """

    def __init__(self) -> None:
        self._session_errors: int = 0
        self._max_session_errors: int = 10
        self._last_errors: list[str] = []  # sliding window for stuck detection
        # Numeric retry budget shared with the turn orchestrator: every
        # retry-family decision spends one "tool" unit before it is granted.
        self.budget = RetryBudget()
        # Optional tier probe: when it reports a light-tier (weak) model the
        # ladder refuses blind same-call retries and trips stuck detection
        # one failure earlier -- weak models repeat failing calls verbatim
        # far more often than they self-correct.
        self._tier_probe: Callable[[], bool] | None = None

    def set_tier_probe(self, probe: Callable[[], bool] | None) -> None:
        """Attach a callable reporting whether the active model is light-tier."""
        self._tier_probe = probe

    def _light_tier(self) -> bool:
        if self._tier_probe is None:
            return False
        try:
            return bool(self._tier_probe())
        except Exception:
            return False

    def decide(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        error_message: str,
        attempt: int,
        fallback_depth: int = 0,
    ) -> _RecoveryDecision:
        """Determine the recovery action for a tool execution failure.

        Retry-family actions (retry / backoff / fixed-args / fallback) are
        granted only while the numeric retry budget has room; an exhausted
        budget degrades to ``SKIP`` so the turn makes progress instead of
        looping.
        """
        decision = self._decide_uncapped(tool_name, tool_args, error_message, attempt, fallback_depth)
        if decision.action in _RETRY_FAMILY_ACTIONS and not self.budget.try_spend("tool"):
            return _RecoveryDecision(
                action=_RecoveryAction.SKIP,
                reason="tool retry budget exhausted",
            )
        return decision

    def _decide_uncapped(
        self,
        tool_name: str,
        tool_args: dict[str, Any],  # noqa: ARG002 -- kept for signature parity
        error_message: str,
        attempt: int,
        fallback_depth: int = 0,
    ) -> _RecoveryDecision:
        """Category-driven decision tree (no budget accounting)."""

        category = classify_error(error_message)
        # Only count non-transient errors -- transient errors (timeouts, rate limits)
        # are self-resolving and should not silently kill the session.
        if category not in (_ErrorCategory.TRANSIENT, _ErrorCategory.MODEL_ERROR):
            self._session_errors += 1
        self._last_errors.append(error_message[:200])
        if len(self._last_errors) > 10:
            self._last_errors.pop(0)

        # Termination condition: session-level max errors
        if self._session_errors >= self._max_session_errors:
            return _RecoveryDecision(
                action=_RecoveryAction.ABORT_SESSION,
                reason=f"Max session errors ({self._max_session_errors}) reached",
            )

        # Termination condition: stuck loop (same error 3+ times; light-tier
        # models repeat failing calls verbatim, so 2 in a row is enough)
        if self._is_stuck(error_message):
            return _RecoveryDecision(
                action=_RecoveryAction.ABORT_TURN,
                reason="Same error repeated -- likely stuck in a loop",
            )

        # Termination condition: max retries per tool
        if attempt >= RecoveryConfig.MAX_RETRIES:
            if fallback_depth < RecoveryConfig.MAX_FALLBACK_DEPTH:
                fallback = self._get_fallback(tool_name, fallback_depth)
                if fallback:
                    return _RecoveryDecision(
                        action=_RecoveryAction.FALLBACK_TOOL,
                        reason=f"Max retries ({attempt}) -- falling back to {fallback}",
                        fallback_tool=fallback,
                    )
            return _RecoveryDecision(
                action=_RecoveryAction.SKIP,
                reason=f"Max retries ({attempt}) and no fallback available",
            )

        # Route by category
        if category == _ErrorCategory.TRANSIENT:
            delay = compute_backoff(attempt, RecoveryConfig.BASE_BACKOFF_SECONDS)
            return _RecoveryDecision(
                action=_RecoveryAction.RETRY_WITH_BACKOFF,
                reason=f"Transient error -- retry {attempt + 1}/{RecoveryConfig.MAX_RETRIES}",
                retry_delay_seconds=delay,
            )

        if category == _ErrorCategory.MODEL_ERROR:
            delay = compute_backoff(attempt, 2.0, 120.0)
            return _RecoveryDecision(
                action=_RecoveryAction.RETRY_WITH_BACKOFF,
                reason="Model/API error -- retry with longer backoff",
                retry_delay_seconds=delay,
            )

        if category == _ErrorCategory.INPUT_MALFORMED:
            if attempt == 0:
                return _RecoveryDecision(
                    action=_RecoveryAction.RETRY_WITH_FIXED_ARGS,
                    reason="Malformed input -- attempting arg correction",
                )
            return _RecoveryDecision(
                action=_RecoveryAction.ABORT_TURN,
                reason="Cannot fix malformed input after retry",
            )

        if category == _ErrorCategory.PERMISSION_DENIED:
            return _RecoveryDecision(
                action=_RecoveryAction.SKIP,
                reason="Permission denied -- skipping tool",
            )

        if category == _ErrorCategory.TOOL_NOT_FOUND:
            return _RecoveryDecision(
                action=_RecoveryAction.ABORT_TURN,
                reason="Tool not found -- asking model to replan",
            )

        if category == _ErrorCategory.TOOL_EXECUTION:
            if fallback_depth < RecoveryConfig.MAX_FALLBACK_DEPTH:
                fallback = self._get_fallback(tool_name, fallback_depth)
                if fallback:
                    return _RecoveryDecision(
                        action=_RecoveryAction.FALLBACK_TOOL,
                        reason=f"Tool execution failed -- falling back to {fallback}",
                        fallback_tool=fallback,
                    )
            if self._light_tier():
                # No fallback chain entry: a same-call retry is a coin flip a
                # weak model usually loses.  Hand the turn back so the model
                # re-plans with the error visible instead of burning a retry.
                return _RecoveryDecision(
                    action=_RecoveryAction.ABORT_TURN,
                    reason="Tool execution failed (light-tier) -- no fallback, asking model to replan",
                )
            return _RecoveryDecision(
                action=_RecoveryAction.RETRY,
                reason=f"Tool execution failed -- retry {attempt + 1}",
            )

        # UNKNOWN
        if attempt == 0 and not self._light_tier():
            delay = compute_backoff(0, 1.0)
            return _RecoveryDecision(
                action=_RecoveryAction.RETRY_WITH_BACKOFF,
                reason="Unknown error -- retry once with backoff",
                retry_delay_seconds=delay,
            )
        return _RecoveryDecision(
            action=_RecoveryAction.ABORT_TURN,
            reason="Unknown error persists -- asking model to replan",
        )

    def reset_session(self) -> None:
        self._session_errors = 0
        self._last_errors.clear()

    def _is_stuck(self, _error: str) -> bool:
        window = 2 if self._light_tier() else 3
        if len(self._last_errors) < window:
            return False
        # Check if the last `window` errors are essentially the same
        recent = self._last_errors[-window:]
        normalized = [e[:100].lower().strip() for e in recent]
        return len(set(normalized)) == 1

    @staticmethod
    def infer_correction(state: RecoveryState) -> str:
        """Derive a human-readable correction from recovery history."""
        if not state.recovery_history:
            return ""
        return ErrorRecoveryEngine._infer_from_decisions(state.recovery_history, state.tool_name)

    @staticmethod
    def infer_correction_from_history(recovery_history: list[_RecoveryDecision], tool_name: str = "") -> str:
        """Derive a human-readable correction from a list of recovery decisions."""
        if not recovery_history:
            return ""
        return ErrorRecoveryEngine._infer_from_decisions(recovery_history, tool_name)

    @staticmethod
    def _infer_from_decisions(decisions: list[_RecoveryDecision], tool_name: str) -> str:
        for decision in reversed(decisions):
            if decision.action == _RecoveryAction.FALLBACK_TOOL:
                return f"use fallback tool '{decision.fallback_tool}' instead of '{tool_name}'"
            if decision.action == _RecoveryAction.RETRY_WITH_FIXED_ARGS:
                return f"retry with modified args: {decision.modified_args}"
            if decision.action == _RecoveryAction.RETRY_WITH_BACKOFF:
                return f"retry with backoff ({decision.retry_delay_seconds:.0f}s delay)"
        last = decisions[-1]
        return f"{last.action.name}: {last.reason}" if last.reason else last.action.name

    @staticmethod
    def _get_fallback(tool_name: str, depth: int) -> str | None:
        chain = RecoveryConfig.FALLBACK_CHAINS.get(tool_name, [])
        if depth < len(chain):
            return chain[depth]
        return None


class RetryableExecutor:
    """Wraps a tool execution function with automatic retry and recovery.

    Usage:
        executor = RetryableExecutor(recovery_engine)
        result = await executor.execute(
            tool_name="file_read",
            tool_args={"path": "/tmp/data.json"},
            execute_fn=lambda args: tool.execute(**args),
        )
    """

    def __init__(self, recovery: ErrorRecoveryEngine | None = None) -> None:
        self.recovery = recovery or ErrorRecoveryEngine()

    async def execute(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        execute_fn: Callable[[dict[str, Any]], Any],
        on_retry: Callable[[_RecoveryDecision, int], None] | None = None,
        *,
        idempotency: IdempotencyLedger | None = None,
        idem_key: str = "",
        turn_key: str = "",
    ) -> RecoveryState:
        """Execute a tool with automatic retry/recovery.

        When ``idem_key`` is given together with an ``idempotency`` ledger,
        the call runs under the idempotency guard:

        * attempt 0 consults the ledger first -- a ``succeeded`` record on
          the same turn replays the prior result instead of re-applying;
          an ``unknown`` record (crash residue) refuses the call outright;
        * retries after attempt 0 are only allowed when the previous failure
          was deterministic-safe (input / tool / permission); uncertain
          outcomes (timeout, sandbox, unknown) are never auto-retried;
        * every terminal outcome is journaled, so a crash mid-call stays
          distinguishable from a completed one.
        """
        state = RecoveryState(tool_name=tool_name, tool_args=dict(tool_args))
        fallback_depth = 0
        current_args = dict(tool_args)
        current_tool = tool_name
        guarded = idempotency is not None and bool(idem_key)

        while state.attempts < RecoveryConfig.MAX_RETRIES + 2:  # +2 for fallback attempts
            if guarded and idempotency is not None:
                if state.attempts == 0:
                    prior = idempotency.lookup(idem_key)
                    if prior is not None and prior.status == "succeeded":
                        if prior.turn_key == turn_key:
                            state.succeeded = True
                            state.final_result = f"[idempotent-replay] duplicate call suppressed; previous result: {prior.result_summary}"
                            return state
                    elif prior is not None and prior.status == "unknown":
                        # Crash residue from an earlier run: the tool may or
                        # may not have applied -- refuse instead of guessing.
                        state.succeeded = False
                        state.final_result = (
                            "[idempotency-guard] previous attempt never reached "
                            "a terminal state (crash residue); verify the effect "
                            "externally before re-running"
                        )
                        return state
                    idempotency.mark_started(idem_key, tool_name, current_args, turn_key)
                elif state.last_category not in _IDEM_SAFE_RETRY_CATEGORIES:
                    state.succeeded = False
                    state.final_result = (
                        "[idempotency-guard] previous attempt outcome uncertain "
                        f"({state.last_category.name}); automatic retry refused "
                        "to avoid double application"
                    )
                    return state
            try:
                result = await execute_fn(current_args)
            except Exception as e:
                error_msg = str(e)
                state.last_error = error_msg
                state.last_category = classify_error(error_msg)

                decision = self.recovery.decide(
                    current_tool,
                    current_args,
                    error_msg,
                    state.attempts,
                    fallback_depth,
                )
                state.recovery_history.append(decision)

                # A decision that stops retrying is terminal for this call:
                # journal the failure so the ledger never stays "unknown"
                # behind a completed turn.
                if guarded and idempotency is not None and decision.action not in _RETRY_FAMILY_ACTIONS:
                    idempotency.mark_result(idem_key, "failed", error_msg, turn_key)

                if on_retry:
                    on_retry(decision, state.attempts)

                if decision.action == _RecoveryAction.RETRY:
                    state.attempts += 1
                    continue

                elif decision.action == _RecoveryAction.RETRY_WITH_BACKOFF:
                    await asyncio.sleep(decision.retry_delay_seconds)
                    state.attempts += 1
                    continue

                elif decision.action == _RecoveryAction.RETRY_WITH_FIXED_ARGS:
                    if decision.modified_args:
                        current_args = decision.modified_args
                    state.attempts += 1
                    continue

                elif decision.action == _RecoveryAction.FALLBACK_TOOL:
                    current_tool = decision.fallback_tool
                    current_args = decision.fallback_args or current_args
                    fallback_depth += 1
                    state.attempts += 1
                    continue

                elif decision.action == _RecoveryAction.DEGRADE:
                    state.succeeded = True
                    state.final_result = f"[Degraded] {error_msg}"
                    return state

                elif decision.action == _RecoveryAction.SKIP:
                    state.succeeded = True
                    state.final_result = f"[Skipped] {error_msg}"
                    return state

                elif decision.action in (_RecoveryAction.ABORT_TURN, _RecoveryAction.ABORT_SESSION):
                    state.succeeded = False
                    state.final_result = f"[Aborted] {error_msg}"
                    return state

            else:
                # A tool-reported ``"Error: ..."`` string is a *deterministic
                # business failure* (missing file, permission denied, bad
                # old_str, path guard, ...).  Surface it verbatim as a
                # terminal failure: no retry, no fallback, no
                # [Degraded]/[Skipped]/[Aborted] rewriting.  Re-running the
                # same call cannot change the outcome, and routing these
                # through the recovery tree previously caused retry storms,
                # false successes, and file contents that merely *start*
                # with "Error" being swallowed by the recovery machinery.
                if isinstance(result, str) and result.startswith("Error"):
                    state.succeeded = False
                    state.final_result = result
                    if guarded and idempotency is not None:
                        idempotency.mark_result(idem_key, "failed", result, turn_key)
                    return state
                state.succeeded = True
                state.final_result = str(result) if result else ""
                if guarded and idempotency is not None:
                    idempotency.mark_result(idem_key, "succeeded", state.final_result, turn_key)
                return state

        state.succeeded = False
        state.final_result = f"[Exhausted] Max retries exceeded: {state.last_error}"
        return state
