#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
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

# ---------------------------------------------------------------------------
# Module-level telemetry: per-session and cross-session instrumentation.
#
# ``EncreTelemetry`` records tool calls, permission decisions, turn
# metrics, backend retries, and context-usage breakdowns for a single
# agent session.  Every record is encrypted and persisted as one JSON
# line in ``<data_dir>/telemetry/<session_id>.jsonl``.  A companion
# cumulative-counter file (``telemetry_cumulative.json``) survives
# individual session deletion so cross-session totals (tokens, cost,
# tool-call breakdown) are always available.
#
# Secrets are stripped from every persisted text payload via a two-tier
# redactor: structured credential regexes run unconditionally, while a
# context-gated Shannon-entropy pass catches high-entropy quoted strings
# only on lines that already carry a secret-keyed keyword.
# ---------------------------------------------------------------------------

import contextlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from encre.crypto import decrypt, encrypt
from encre.logging_config import get_logger
from encre.policy_version import policy_versions_stamped

logger = get_logger("encre.telemetry")


# Per-1K-token USD pricing (input, output) for cost tracking.  Unknown
# models default to zero cost; ``_compute_cost_usd`` prefix-matches the
# model name so dated snapshots (``claude-sonnet-4-6-20250929``) and
# provider-qualified names (``anthropic/claude-sonnet-4``) resolve to the
# base entry.  Prices are approximate public list prices as of mid-2025;
# update via this table when a provider changes pricing.
PRICING_TABLE: dict[str, tuple[float, float]] = {
    # Anthropic Claude (USD per 1K tokens: input, output)
    "claude-opus-4":        (0.015, 0.075),
    "claude-sonnet-4":      (0.003, 0.015),
    "claude-haiku-4":       (0.0008, 0.004),
    "claude-3-5-sonnet":    (0.003, 0.015),
    "claude-3-5-haiku":     (0.0008, 0.004),
    "claude-3-opus":        (0.015, 0.075),
    "claude-3-haiku":       (0.00025, 0.00125),
    # OpenAI
    "gpt-5.6":              (0.0050, 0.030),
    "gpt-5.6-terra":        (0.0025, 0.015),
    "gpt-5.6-luna":         (0.0010, 0.006),
    "gpt-4o":               (0.0025, 0.010),
    "gpt-4o-mini":          (0.00015, 0.0006),
    "gpt-4-turbo":          (0.010, 0.030),
    "gpt-4":                (0.030, 0.060),
    "gpt-3.5-turbo":        (0.0005, 0.0015),
    "o1":                   (0.015, 0.060),
    "o1-mini":              (0.003, 0.012),
    # DeepSeek
    "deepseek-chat":        (0.00014, 0.00028),
    "deepseek-reasoner":    (0.00055, 0.00219),
    # Google Gemini
    "gemini-1.5-pro":       (0.00125, 0.005),
    "gemini-1.5-flash":     (0.000075, 0.0003),
    "gemini-2.0-flash":     (0.0001, 0.0004),
    # Groq / Meta open models (cheap)
    "llama-3.1-405b":       (0.00059, 0.00079),
    "llama-3.1-70b":        (0.00059, 0.00079),
    "llama-3.3-70b":        (0.00059, 0.00079),
}


def _compute_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return the USD cost for *model*'s token usage, 0.0 if unpriced.

    Prefix-matches the (lowercased) model name against ``PRICING_TABLE``,
    preferring the longest key match so ``claude-sonnet-4-6-20250929``
    resolves to ``claude-sonnet-4`` rather than a shorter accidental
    prefix.  Falls back to substring matching so provider-qualified names
    like ``anthropic/claude-sonnet-4`` still resolve.
    """
    if not model or not PRICING_TABLE:
        return 0.0
    m = model.lower().strip()
    best_key = ""
    for key in PRICING_TABLE:
        if m.startswith(key) and len(key) > len(best_key):
            best_key = key
    if not best_key:
        for key in PRICING_TABLE:
            if key in m and len(key) > len(best_key):
                best_key = key
    if not best_key:
        return 0.0
    in_price, out_price = PRICING_TABLE[best_key]
    return (input_tokens / 1000.0) * in_price + (output_tokens / 1000.0) * out_price


@dataclass
class ToolCallRecord:
    """Snapshot of a single tool invocation's timing and outcome.

    Appended to ``EncreTelemetry.tool_calls`` on every tool dispatch and
    later rolled up into the session summary (count, latency averages,
    success rate).
    """
    tool_name: str
    latency_ms: float
    success: bool
    timestamp: float = field(default_factory=time.time)
    tokens_used: int = 0
    error_message: str = ""


@dataclass
class PermissionDecisionRecord:
    """One permission decision for a tool call.

    ``effect`` is the final outcome (``allow`` / ``ask`` / ``deny``) and
    ``source`` records which layer decided it: ``rule_exact`` /
    ``rule_wildcard`` (structured permission rules), ``default_effect``
    (the configured default effect), ``classifier``
    (autosafety risk classifier / mode-aware engine flow) or
    ``fallback_ask`` (permission check unavailable, degraded to ask).
    """

    tool_name: str
    effect: str
    source: str
    matched_rule: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class TurnRecord:
    """Summary of one agent turn (a single LLM request/response cycle).

    Captures the turn ordinal, event count, wall-clock latency, whether
    context compaction was triggered, token usage, model, and channel so
    that turn-level analytics can be computed without re-scanning the raw
    event log.
    """
    turn_number: int
    event_count: int
    latency_ms: float
    compact_triggered: bool = False
    token_usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    channel: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class RetryRecord:
    """Record of a backend retry event."""
    attempt: int
    error_type: str  # "http_status", "exception"
    error_detail: str
    delay_s: float
    timestamp: float = field(default_factory=time.time)


# --- Secret redaction -------------------------------------------------------
#
# Structured credential patterns plus a context-gated high-entropy pass
# keep secrets (API keys, tokens, passwords, private keys) out of telemetry
# payloads and persisted session history.  :func:`redact_text` never throws
# and is deliberately conservative: only quoted strings inside secret-keyed
# lines are entropy-checked, so ordinary prose is untouched.

_REDACT_PLACEHOLDER = "<redacted>"
#: Skip the per-line entropy pass above this payload size (perf guard);
#: the cheap structured regexes still run.
_REDACT_ENTROPY_SKIP_BYTES = 256 * 1024
_REDACT_ENTROPY_THRESHOLD = 4.5

# key=value / key: value shapes ("api_key=abc123", "Authorization: Basic ...").
_RE_SECRET_KV = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret(?:[_-]?key)?|access[_-]?key|auth[_-]?token|"
    r"token|password|passwd|pwd|authorization|bearer|private[_-]?key|"
    r"client[_-]?secret|credential[s]?)"
    r"[\"']?\s*([:=])\s*[\"']?([^\s\"'&,;]{6,})"
)
# Concrete credential formats.
_RE_AWS_ACCESS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_RE_SK_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")
_RE_GH_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")
_RE_BEARER = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=\-]{16,})")
_RE_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY[A-Z ]*-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY[A-Z ]*-----"
)
_RE_PEM_HEADER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY[A-Z ]*-----")
# High-entropy quoted strings (only inside secret-keyed context lines).
_RE_QUOTED_SPAN = re.compile(r"[\"']([^\"'\n]{20,})[\"']")
_RE_SECRET_CONTEXT = re.compile(
    r"(?i)\b(key|secret|token|password|passwd|pwd|authorization|auth|credential)"
)


def _shannon_entropy(value: str) -> float:
    """Return the Shannon entropy (bits per char) of *value*."""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    n = float(len(value))
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def redact_text(text: str) -> tuple[str, int]:
    """Redact secret-looking material from *text*.

    Returns ``(redacted_text, replacement_count)`` with matches replaced by
    ``<redacted>``.  Structured patterns (key/value shapes, AWS AKIA keys,
    ``sk-`` API keys, GitHub tokens, Bearer headers, PEM private-key
    blocks) always run; the high-entropy quoted-string pass (Shannon
    entropy > 4.5, length >= 20) only fires on lines carrying a
    secret-keyed keyword and is skipped entirely for payloads over 256 KB
    to bound latency on large tool outputs.
    """
    if not text:
        return text, 0
    count = 0

    def _kv_sub(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{m.group(1)}{m.group(2)}{_REDACT_PLACEHOLDER}"

    def _plain_sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return _REDACT_PLACEHOLDER

    def _bearer_sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"Bearer {_REDACT_PLACEHOLDER}"

    def _entropy_sub(m: re.Match[str]) -> str:
        nonlocal count
        if _shannon_entropy(m.group(1)) > _REDACT_ENTROPY_THRESHOLD:
            count += 1
            return f"{m.group(0)[0]}{_REDACT_PLACEHOLDER}{m.group(0)[-1]}"
        return m.group(0)

    text = _RE_PEM_BLOCK.sub(_plain_sub, text)
    text = _RE_PEM_HEADER.sub(_plain_sub, text)
    text = _RE_SECRET_KV.sub(_kv_sub, text)
    text = _RE_AWS_ACCESS_KEY.sub(_plain_sub, text)
    text = _RE_SK_API_KEY.sub(_plain_sub, text)
    text = _RE_GH_TOKEN.sub(_plain_sub, text)
    text = _RE_BEARER.sub(_bearer_sub, text)

    if len(text) <= _REDACT_ENTROPY_SKIP_BYTES:
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if not _RE_SECRET_CONTEXT.search(line):
                continue
            new_line = _RE_QUOTED_SPAN.sub(_entropy_sub, line)
            if new_line != line:
                lines[i] = new_line
        text = "\n".join(lines)
    return text, count


def _redact_strings(node: Any) -> int:
    """Redact every string payload in *node* in place; return the count."""
    total = 0
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if isinstance(value, str):
                node[key], n = redact_text(value)
                total += n
            else:
                total += _redact_strings(value)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            if isinstance(value, str):
                node[i], n = redact_text(value)
                total += n
            else:
                total += _redact_strings(value)
    return total


class EncreTelemetry:
    """Per-session telemetry collector with encrypted JSONL persistence.

    Each ``EncreTelemetry`` instance represents one agent session and
    accumulates structured records (tool calls, permission decisions,
    turns, retries, context breakdowns) in memory.  On each write the
    record is stamped with the policy-version registry, secret-redacted,
    optionally buffered for OTLP export, encrypted, and appended to the
    session's JSONL file.

    Cumulative counters are persisted to a shared file that survives
    session deletion, enabling cross-session usage reporting without
    re-scanning every JSONL file.  The optional OTLP exporter (see
    :mod:`encre.telemetry_otlp`) can push records to an external collector
    via HTTP when configured.

    All write paths are wrapped in exception guards: telemetry failures
    never propagate to the caller, so a broken log directory or
    misconfigured collector cannot crash the agent loop.
    """

    def __init__(
        self,
        enabled: bool = True,
        session_id: str = "",
        parent_session_id: str = "",
        otlp_config: Any = None,
    ) -> None:
        self.enabled = enabled
        self.session_id = session_id or str(int(time.time() * 1000))
        self.parent_session_id = parent_session_id
        self.tool_calls: list[ToolCallRecord] = []
        self.permission_decisions: list[PermissionDecisionRecord] = []
        self.turns: list[TurnRecord] = []
        self.retries: list[RetryRecord] = []
        self.total_redactions: int = 0  # secret redaction replacements
        self._session_started_at: float = time.time()
        self._output_dir: str = ""
        self._session_cost_usd: float = 0.0  # restored from JSONL on resume
        self._current_channel: str = ""
        self._current_model: str = ""  # set on each turn; inherited by tool calls
        # Policy version stamp (Task 8.1): the registry is computed once per
        # process at module import; this per-instance copy keeps the in-place
        # secret redactor from ever mutating shared state.
        self._policy_versions: dict[str, str] = policy_versions_stamped()
        # Optional OTLP/HTTP logs exporter (config-driven).  ``None`` unless
        # explicitly enabled -- zero overhead (no thread, no buffering).
        self._otlp: Any = None
        if enabled and otlp_config is not None:
            try:
                from encre.telemetry_otlp import build_otlp_exporter
                self._otlp = build_otlp_exporter(
                    otlp_config, session_id=self.session_id,
                )
            except Exception:
                self._otlp = None

    def _ensure_output(self) -> None:
        """Lazily create the telemetry output directory on first write."""
        if self._output_dir:
            return
        from encre.config import get_data_dir
        _dir = get_data_dir() / "telemetry"
        _dir.mkdir(parents=True, exist_ok=True)
        self._output_dir = str(_dir)

    def _write_jsonl(self, record: dict[str, Any]) -> None:
        """Serialize, redact, encrypt, and append one telemetry record.

        The record is stamped with the current policy-version registry,
        passed through the secret-redaction pipeline, optionally buffered
        for OTLP export, encrypted, and appended to the session JSONL
        file.  Any I/O or encryption failure is silently swallowed so
        telemetry write errors never crash the agent loop.
        """
        if not self._output_dir:
            self._ensure_output()
        try:
            _path = os.path.join(self._output_dir, f"{self.session_id}.jsonl")
            # Stamp every persisted record with the policy version registry
            # (Task 8.1) -- the common write path covers all record kinds.
            record.setdefault("policy_versions", self._policy_versions)
            # Secret redaction: strip credential-looking material from every
            # text payload before it reaches the log file, and accumulate
            # the replacement count on the record and the session total.
            redactions = _redact_strings(record)
            if redactions:
                record["redactions"] = redactions
                self.total_redactions += redactions
            # Opt-in OTLP export: the (already redacted) record is buffered
            # for the background logs exporter; failures degrade silently.
            if self._otlp is not None:
                self._otlp.append(record)
            line = json.dumps(record, ensure_ascii=False)
            try:
                encrypted_line = encrypt(line)
            except Exception:
                # Operational data: skip the write rather than persist
                # plaintext or crash the telemetry loop.
                logger.error("telemetry: encryption failed; dropping record", exc_info=True)
                return
            with open(_path, "a", encoding="utf-8") as f:
                f.write(encrypted_line + "\n")
        except Exception:
            pass  # never crash on telemetry write failure

    def add_redaction_count(self, n: int) -> None:
        """Accumulate *n* out-of-band secret-redaction replacements.

        Redactions applied outside ``_write_jsonl`` (e.g. session-history
        redaction in ``EncreSession.add_tool_result``) funnel their
        replacement counts here so they land in the same
        ``total_redactions`` channel reported by ``get_summary``.  A
        non-positive count is a no-op, and a disabled telemetry instance
        records nothing (matching ``record_tool_call`` semantics).
        """
        if self.enabled and n > 0:
            self.total_redactions += n

    def record_tool_call(
        self,
        tool_name: str,
        latency_ms: float,
        success: bool = True,
        tokens_used: int = 0,
        error_message: str = "",
    ) -> None:
        """Record a single tool invocation with its latency and outcome.

        Creates a :class:`ToolCallRecord`, appends it to the in-memory
        list, writes the event to the JSONL log, and updates the
        persistent cumulative counter.  No-ops when telemetry is
        disabled.
        """
        if not self.enabled:
            return
        record = ToolCallRecord(
            tool_name=tool_name,
            latency_ms=latency_ms,
            success=success,
            tokens_used=tokens_used,
            error_message=error_message,
        )
        self.tool_calls.append(record)
        entry = {
            "event": "tool_call",
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id or None,
            "timestamp": record.timestamp,
            "tool_name": record.tool_name,
            "latency_ms": record.latency_ms,
            "success": record.success,
            "tokens_used": record.tokens_used,
            "error": record.error_message or None,
            "model": self._current_model or None,
            "channel": self._current_channel or None,
        }
        logger.debug(json.dumps(entry, ensure_ascii=False))
        self._write_jsonl(entry)
        self._update_cumulative_from_tool_call(tool_name)

    def record_permission_decision(
        self,
        tool_name: str,
        effect: str,
        source: str,
        matched_rule: str = "",
    ) -> None:
        """Record one permission decision for a tool call.

        Args:
            tool_name: Name of the tool the decision was made for.
            effect: Final outcome: ``allow`` / ``ask`` / ``deny``.
            source: Decision layer: ``rule_exact`` / ``rule_wildcard`` /
                ``default_effect`` / ``classifier`` / ``fallback_ask``.
            matched_rule: Human-readable description of the winning
                permission rule (empty when no rule matched).
        """
        if not self.enabled:
            return
        record = PermissionDecisionRecord(
            tool_name=tool_name,
            effect=effect,
            source=source,
            matched_rule=matched_rule,
        )
        self.permission_decisions.append(record)
        entry = {
            "event": "permission_decision",
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id or None,
            "timestamp": record.timestamp,
            "tool_name": record.tool_name,
            "effect": record.effect,
            "source": record.source,
            "matched_rule": record.matched_rule or None,
            "model": self._current_model or None,
            "channel": self._current_channel or None,
        }
        logger.debug(json.dumps(entry, ensure_ascii=False))
        self._write_jsonl(entry)

    def record_context_breakdown(self, sections: dict[str, int]) -> None:
        """Record the per-section token breakdown of a compiled context.

        Emitted by the prompt phase after :func:`encre.context_compiler.
        compile_context` so the compiled context's composition (identity /
        safety rules / steering / history / retrieved memory / tool
        results / background) is traceable per turn.

        Args:
            sections: Mapping of section name -> actual token count.
        """
        if not self.enabled or not sections:
            return
        entry = {
            "event": "context_breakdown",
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id or None,
            "timestamp": time.time(),
            "sections": {str(k): int(v) for k, v in sections.items()},
            "total_tokens": sum(int(v) for v in sections.values()),
            "model": self._current_model or None,
        }
        logger.debug(json.dumps(entry, ensure_ascii=False))
        self._write_jsonl(entry)

    def record_context_usage(self, usage: dict[str, Any]) -> None:
        """Record the per-request context-usage composition.

        Emitted by the prompt phase after every
        :func:`encre.context_compiler.compute_context_usage` call so the
        request's category composition (system prompt / messages / tools
        split by connector kind / skills / free space) is traceable per
        turn and shippable to the frontend context-usage display.

        Args:
            usage: The breakdown dict produced by
                :func:`encre.context_compiler.compute_context_usage`.
        """
        if not self.enabled or not usage:
            return
        entry = {
            "event": "context_usage",
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id or None,
            "timestamp": time.time(),
            "context_window": int(usage.get("context_window") or 0),
            "system_prompt_tokens": int(usage.get("system_prompt_tokens") or 0),
            "messages_tokens": int(usage.get("messages_tokens") or 0),
            "tools_tokens": dict(usage.get("tools_tokens") or {}),
            "skills_tokens": int(usage.get("skills_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "free_tokens": usage.get("free_tokens"),
            "model": self._current_model or None,
        }
        logger.debug(json.dumps(entry, ensure_ascii=False))
        self._write_jsonl(entry)

    def record_turn(
        self,
        turn_number: int,
        event_count: int,
        latency_ms: float,
        compact_triggered: bool = False,
        token_usage: dict[str, int] | None = None,
        model: str = "",
        channel: str = "",
    ) -> None:
        """Record one completed agent turn with latency, tokens, and cost.

        Stores a :class:`TurnRecord` for in-memory aggregation, remembers
        the active model and channel so subsequent tool calls inherit
        them, computes the per-turn USD cost, updates the persistent
        cumulative counter, and writes the event to the JSONL log.
        """
        if not self.enabled:
            return
        token_usage = token_usage or {}
        record = TurnRecord(
            turn_number=turn_number,
            event_count=event_count,
            latency_ms=latency_ms,
            compact_triggered=compact_triggered,
            token_usage=token_usage,
            model=model,
            channel=channel,
        )
        self.turns.append(record)
        # Remember the active model/channel so tool calls that follow this
        # turn (and any tool-only sessions launched from here) inherit them.
        if model:
            self._current_model = model
        if channel:
            self._current_channel = channel
        # Cost tracking: price this turn's tokens against the model.
        inp = token_usage.get("input_tokens", token_usage.get("prompt_tokens", 0)) or 0
        out = token_usage.get("output_tokens", token_usage.get("completion_tokens", 0)) or 0
        cost = _compute_cost_usd(model, inp, out)
        self._session_cost_usd += cost
        entry = {
            "event": "turn",
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id or None,
            "timestamp": record.timestamp,
            "turn_number": record.turn_number,
            "event_count": record.event_count,
            "latency_ms": record.latency_ms,
            "compact_triggered": record.compact_triggered,
            "token_usage": record.token_usage,
            "model": record.model,
            "channel": record.channel,
            "cost_usd": round(cost, 6),
        }
        logger.debug(json.dumps(entry, ensure_ascii=False))
        self._write_jsonl(entry)
        self._update_cumulative_from_turn(token_usage, model, cost)

    def record_retry(
        self,
        attempt: int,
        error_type: str,
        error_detail: str,
        delay_s: float,
    ) -> None:
        """Record a backend retry event."""
        if not self.enabled:
            return
        record = RetryRecord(
            attempt=attempt,
            error_type=error_type,
            error_detail=error_detail,
            delay_s=delay_s,
        )
        self.retries.append(record)
        entry = {
            "event": "retry",
            "timestamp": record.timestamp,
            "attempt": record.attempt,
            "error_type": record.error_type,
            "error_detail": record.error_detail,
            "delay_s": record.delay_s,
        }
        logger.warning(json.dumps(entry, ensure_ascii=False))
        self._write_jsonl(entry)

    def get_summary(self) -> dict[str, Any]:
        """Aggregate all in-memory records into a session summary dict.

        Computes averages (latency), distributions (tool usage by name,
        retry counts by error detail), totals (tokens, events,
        compactions), and the session-wide cache-hit rate.  The result
        is also written as a ``session_summary`` event by :meth:`flush`.
        """
        total_tool_calls = len(self.tool_calls)
        successful_tool_calls = sum(1 for t in self.tool_calls if t.success)
        failed_tool_calls = total_tool_calls - successful_tool_calls
        tool_latencies = [t.latency_ms for t in self.tool_calls]
        avg_tool_latency = sum(tool_latencies) / len(tool_latencies) if tool_latencies else 0.0

        total_turns = len(self.turns)
        turn_latencies = [t.latency_ms for t in self.turns]
        avg_turn_latency = sum(turn_latencies) / len(turn_latencies) if turn_latencies else 0.0
        total_events = sum(t.event_count for t in self.turns)
        compactions = sum(1 for t in self.turns if t.compact_triggered)
        session_duration_s = time.time() - self._session_started_at

        tool_usage: dict[str, int] = {}
        for t in self.tool_calls:
            tool_usage[t.tool_name] = tool_usage.get(t.tool_name, 0) + 1

        total_retries = len(self.retries)
        retry_by_type: dict[str, int] = {}
        for r in self.retries:
            retry_by_type[r.error_detail] = retry_by_type.get(r.error_detail, 0) + 1

        session_input_tokens = sum(
            (t.token_usage.get("input_tokens", t.token_usage.get("prompt_tokens", 0)) or 0)
            for t in self.turns
        )
        session_cache_hit_tokens = sum(
            (t.token_usage.get("cache_read_input_tokens", 0) or 0)
            for t in self.turns
        )

        return {
            "session_duration_s": session_duration_s,
            "total_turns": total_turns,
            "total_tool_calls": total_tool_calls,
            "successful_tool_calls": successful_tool_calls,
            "failed_tool_calls": failed_tool_calls,
            "avg_tool_latency_ms": round(avg_tool_latency, 2),
            "avg_turn_latency_ms": round(avg_turn_latency, 2),
            "total_events": total_events,
            "compactions": compactions,
            "tool_usage": tool_usage,
            "total_retries": total_retries,
            "retry_by_error": retry_by_type,
            "session_cost_usd": round(self._session_cost_usd, 6),
            "session_input_tokens": session_input_tokens,
            "session_cache_hit_tokens": session_cache_hit_tokens,
            "session_cache_hit_rate": round(
                session_cache_hit_tokens / session_input_tokens, 4
            ) if session_input_tokens else 0.0,
            "total_redactions": self.total_redactions,
        }

    def flush(self) -> dict[str, Any]:
        """Write a final ``session_summary`` event and return the summary.

        Calls :meth:`get_summary`, persists it to the JSONL log, triggers
        a best-effort OTLP final flush, and dispatches a fire-and-forget
        OTLP session-summary export (when configured).  Safe to call
        multiple times; each call appends a new summary record.
        """
        summary = self.get_summary()
        entry = {"event": "session_summary", "session_id": self.session_id, "parent_session_id": self.parent_session_id or None, **summary}
        logger.debug(json.dumps(entry, ensure_ascii=False))
        self._write_jsonl(entry)
        # Best-effort final push of the buffered OTLP log records.
        if self._otlp is not None:
            with contextlib.suppress(Exception):
                self._otlp.flush()
        # Opt-in OTLP export (ENCRE_OTEL_ENDPOINT): fire-and-forget, never
        # blocks or crashes the loop.
        try:
            from encre.telemetry_otlp import export_session_summary
            export_session_summary(summary, self.session_id)
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
        return summary

    def reset(self) -> None:
        """Clear all in-memory records and reset session-start time.

        Called when a session is resumed so that post-resume activity is
        tracked from a clean slate while the cumulative counter still
        preserves historical totals.
        """
        self.tool_calls.clear()
        self.turns.clear()
        self.retries.clear()
        self.total_redactions = 0
        self._session_started_at = time.time()
        self._session_cost_usd = 0.0

    def restore_session_cost_from_jsonl(self) -> float:
        """Recompute this session's cumulative cost from its JSONL log.

        Called when a session is resumed so ``get_summary()`` reflects the
        full session cost rather than only post-resume activity.  Reads
        each ``turn`` event's ``cost_usd`` field (falling back to recomputing
        it from the model + token_usage for older entries that predate the
        cost field) and stores the sum in ``self._session_cost_usd``.
        Returns the restored total.
        """
        if not self._output_dir:
            self._ensure_output()
        if not self._output_dir:
            self._session_cost_usd = 0.0
            return 0.0
        path = os.path.join(self._output_dir, f"{self.session_id}.jsonl")
        if not os.path.exists(path):
            self._session_cost_usd = 0.0
            return 0.0
        total = 0.0
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            return 0.0
        for line in lines:
            if not line.strip():
                continue
            data = None
            try:
                try:
                    data = json.loads(decrypt(line.strip()))
                except Exception:
                    data = json.loads(line.strip())
            except Exception:
                continue
            if not isinstance(data, dict) or data.get("event") != "turn":
                continue
            cost = data.get("cost_usd")
            if cost is None:
                tu = data.get("token_usage", {}) or {}
                inp = tu.get("input_tokens", tu.get("prompt_tokens", 0)) or 0
                out = tu.get("output_tokens", tu.get("completion_tokens", 0)) or 0
                cost = _compute_cost_usd(data.get("model", ""), inp, out)
            total += float(cost or 0)
        self._session_cost_usd = total
        return total

    # ── Persistent cumulative counters (survive session deletion) ─────

    @staticmethod
    def _cumulative_path() -> str:
        """Return the absolute path to the cumulative counter JSON file."""
        from encre.config import get_data_dir
        return str(get_data_dir() / "telemetry_cumulative.json")

    @staticmethod
    def _load_cumulative() -> dict[str, Any]:
        """Load the persistent cumulative counter file, applying defaults.

        Reads ``telemetry_cumulative.json`` and fills in any missing
        keys with zero-valued defaults.  Returns a fresh dict when the
        file does not exist or cannot be parsed.
        """
        path = EncreTelemetry._cumulative_path()
        try:
            from encre.secure_io import read_json

            data = read_json(path, default=None) or {}
            defaults = {
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cache_hit_tokens": 0,
                "total_tool_calls": 0,
                "tool_call_breakdown": {},
                "total_cost_usd": 0.0,
                "model_cost_breakdown": {},
            }
            for k, v in defaults.items():
                data.setdefault(k, v)
            return data
        except (FileNotFoundError, json.JSONDecodeError):
            return {
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cache_hit_tokens": 0,
                "total_tool_calls": 0,
                "tool_call_breakdown": {},
                "total_cost_usd": 0.0,
                "model_cost_breakdown": {},
            }

    @staticmethod
    def _save_cumulative(data: dict[str, Any]) -> None:
        """Persist the cumulative counter dict to disk.

        Writes to ``telemetry_cumulative.json`` via :func:`encre.secure_io.
        write_json`.  Failures are silently swallowed so telemetry I/O
        errors never crash the agent loop.
        """
        try:
            from encre.secure_io import write_json

            write_json(EncreTelemetry._cumulative_path(), data)
        except Exception:
            pass  # never crash on telemetry write failure

    def _update_cumulative_from_turn(
        self,
        token_usage: dict[str, int] | None,
        model: str = "",
        cost_usd: float = 0.0,
    ) -> None:
        """Accumulate token and cost counters for one turn in the persistent store.

        Loads the cumulative counter file, adds the turn's input/output/
        cache-hit tokens and cost, updates the per-model breakdown, and
        saves the file.  Fails silently on I/O errors.
        """
        if not token_usage:
            return
        inp = token_usage.get("input_tokens", token_usage.get("prompt_tokens", 0)) or 0
        out = token_usage.get("output_tokens", token_usage.get("completion_tokens", 0)) or 0
        cache_hit = token_usage.get("cache_read_input_tokens", 0) or 0
        cu = self._load_cumulative()
        cu["total_input_tokens"] += inp
        cu["total_output_tokens"] += out
        cu["total_cache_hit_tokens"] = cu.get("total_cache_hit_tokens", 0) + cache_hit
        cu["total_cost_usd"] = round(cu.get("total_cost_usd", 0.0) + cost_usd, 6)
        if model:
            mb = cu.setdefault("model_cost_breakdown", {})
            entry = mb.setdefault(model, {
                "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                "cache_hit_tokens": 0,
            })
            entry.setdefault("cache_hit_tokens", 0)
            entry["input_tokens"] += inp
            entry["output_tokens"] += out
            entry["cost_usd"] = round(entry.get("cost_usd", 0.0) + cost_usd, 6)
            entry["cache_hit_tokens"] += cache_hit
        self._save_cumulative(cu)

    def _update_cumulative_from_tool_call(self, tool_name: str) -> None:
        """Increment the total tool-call counter and per-tool breakdown.

        Loads the cumulative counter file, bumps ``total_tool_calls`` and
        the per-tool-name count, and saves the file.  Fails silently on
        I/O errors.
        """
        cu = self._load_cumulative()
        cu["total_tool_calls"] += 1
        cu["tool_call_breakdown"][tool_name] = cu["tool_call_breakdown"].get(tool_name, 0) + 1
        self._save_cumulative(cu)

    @staticmethod
    def get_all_sessions_usage() -> dict[str, Any]:
        """Aggregate telemetry data across all sessions.

        Core totals (tokens, tool calls) come from persistent cumulative
        counters that survive session deletion. Model breakdown and
        per-session detail come from scanning JSONL files.
        """
        # Load persistent cumulative counters
        cumulative = EncreTelemetry._load_cumulative()

        total_input = cumulative["total_input_tokens"]
        total_output = cumulative["total_output_tokens"]
        total_cache_hit = cumulative.get("total_cache_hit_tokens", 0)
        total_tokens = total_input + total_output
        total_tool_calls = cumulative["total_tool_calls"]
        tool_call_breakdown = dict(cumulative["tool_call_breakdown"])
        cache_hit_rate = round(total_cache_hit / total_input, 4) if total_input else 0.0

        # Scan JSONL files for model breakdown and session list
        from encre.config import get_data_dir
        telemetry_dir = get_data_dir() / "telemetry"
        model_breakdown: dict[str, dict[str, int]] = {}
        session_summaries: dict[str, dict[str, Any]] = {}

        if telemetry_dir.is_dir():
            for fpath in sorted(telemetry_dir.glob("*.jsonl")):
                session_id = fpath.stem
                try:
                    lines = fpath.read_text("utf-8").strip().split("\n")
                except Exception:
                    continue

                session_input = 0
                session_output = 0
                session_tokens = 0
                session_tool_calls = 0
                session_tool_breakdown: dict[str, int] = {}
                session_model = ""
                session_channel = ""
                turn_count = 0
                session_first_active = 0.0
                session_last_active = 0.0
                session_cost = 0.0

                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        try:
                            decrypted = decrypt(line.strip())
                        except Exception:
                            decrypted = line.strip()
                        data = json.loads(decrypted)
                    except Exception:
                        continue

                    evt = data.get("event", "")
                    ts = data.get("timestamp", 0) or 0

                    # Track the first and last event timestamps
                    if ts:
                        if session_first_active == 0:
                            session_first_active = ts
                        if ts > session_last_active:
                            session_last_active = ts

                    if evt == "turn":
                        tu = data.get("token_usage", {}) or {}
                        inp = tu.get("input_tokens", tu.get("prompt_tokens", 0)) or 0
                        out = tu.get("output_tokens", tu.get("completion_tokens", 0)) or 0
                        total = tu.get("total_tokens", inp + out) or 0
                        session_input += inp
                        session_output += out
                        session_tokens += total
                        turn_count += 1
                        model = data.get("model", "") or ""
                        session_model = model
                        channel = data.get("channel", "") or ""
                        if channel:
                            session_channel = channel
                        # Cost: prefer the persisted cost_usd, else recompute
                        # for older entries that predate the cost field.
                        cost = data.get("cost_usd")
                        if cost is None:
                            cost = _compute_cost_usd(model, inp, out)
                        session_cost += float(cost or 0)
                        if model:
                            md = model_breakdown.setdefault(model, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "turns": 0})
                            md["input_tokens"] += inp
                            md["output_tokens"] += out
                            md["total_tokens"] += total
                            md["turns"] += 1

                    elif evt == "tool_call":
                        tn = data.get("tool_name", "unknown")
                        session_tool_calls += 1
                        session_tool_breakdown[tn] = session_tool_breakdown.get(tn, 0) + 1
                        # Inherit model from the tool_call payload if the
                        # session has no turn events (tool-only automation).
                        if not session_model:
                            m = data.get("model", "") or ""
                            if m:
                                session_model = m
                        if not session_channel:
                            ch = data.get("channel", "") or ""
                            if ch:
                                session_channel = ch

                    elif evt == "session_summary":
                        pass

                if session_tokens > 0 or session_tool_calls > 0:
                    # Fallback: use file mtime if no event timestamp found
                    if session_first_active == 0:
                        try:
                            session_first_active = fpath.stat().st_mtime
                        except Exception:
                            session_first_active = 0.0
                    if session_last_active == 0:
                        session_last_active = session_first_active
                    session_summaries[session_id] = {
                        "session_id": session_id,
                        "model": session_model,
                        "channel": session_channel or "normal",
                        "input_tokens": session_input,
                        "output_tokens": session_output,
                        "total_tokens": session_tokens,
                        "turns": turn_count,
                        "tool_calls": session_tool_calls,
                        "tool_call_breakdown": session_tool_breakdown,
                        "first_active": session_first_active,
                        "last_active": session_last_active,
                        "cost_usd": round(session_cost, 6),
                    }

        # Build sorted session list (by first_active timestamp ascending) for time-series charts
        sorted_sessions = sorted(
            session_summaries.values(),
            key=lambda s: s.get("first_active", 0) or 0,
        )

        return {
            "total_sessions": len(session_summaries),
            "total_tokens": total_tokens,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_hit_tokens": total_cache_hit,
            "cache_hit_rate": cache_hit_rate,
            "total_tool_calls": total_tool_calls,
            "tool_call_breakdown": tool_call_breakdown,
            "total_cost_usd": round(cumulative.get("total_cost_usd", 0.0), 6),
            "model_cost_breakdown": cumulative.get("model_cost_breakdown", {}),
            "model_breakdown": {
                m: v for m, v in sorted(
                    model_breakdown.items(),
                    key=lambda x: -x[1]["total_tokens"],
                )
            },
            "sessions": sorted_sessions,
        }
