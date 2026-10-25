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

"""Model-driven context compaction -- Claude Code style.

Architecture
------------
There is ONE compaction mechanism: the model reads the conversation and
produces a structured summary.  No heuristic truncation, no fixed turn
counts -- pure token arithmetic decides when to compact.

The pipeline runs **before every API call** in the agent loop:

1. **Microcompact** -- cheap cleanup of old tool results (no API call).
   Clears tool outputs whose cache TTL has expired, replacing them with
   ``[Previous tool output cleared]`` stubs.

2. **Traditional compact** -- the model summarises the conversation into a
   9-section structured block.  Runs when microcompact isn't enough to
   stay under the token budget.

3. **Reactive compact** -- emergency summarisation when the API returns a
   400 / context-length-exceeded error.

Compact agent
-------------
The summarisation call uses the **same backend** as the main loop, but:
- Thinking is disabled
- Only ``file_read`` tool is available
- Output is capped at 16 384 tokens

The summary is inserted as a pair of boundary messages:

    [role: compact_boundary]
    [role: user, is_compact_summary: true, content: <summary>]

Anchor protection
-----------------
The FIRST user message (task definition) and the LAST 4 complete turns
are never removed.  They are spliced into the compacted result after
the summary.

Boundary format
---------------
Messages carry an ``is_compact_boundary`` marker so the frontend can
render a visual divider, and ``is_compact_summary`` so the model can
distinguish the summary from real user input.
"""

import time
from abc import ABC, abstractmethod
from typing import Any

from encre.logging_config import get_logger
from encre.prompts.loader import PromptLoader
from encre.utils.tokens import count_message_tokens

logger = get_logger("encre.compact")

# ── Thresholds ────────────────────────────────────────

AUTOCOMPACT_BUFFER_TOKENS = 13_000   # how far below the effective window we trigger
MICROCOMPACT_CACHE_TTL_MINUTES = 30  # clear tool results older than this
MICROCOMPACT_KEEP_RECENT_TURNS = 5   # keep this many recent turns during microcompact
COMPACT_MAX_OUTPUT_TOKENS = 16_384   # max tokens for the summary response
MAX_CONSECUTIVE_COMPACT_FAILURES = 3  # circuit breaker
_PRE_COMPACT_CACHE_MAX_SESSIONS = 8   # LRU cap on archived pre-compact sessions

# Anti-thrash thresholds (Hermes/Claude autocompact port): a compaction that
# frees less than this fraction of the pre-compact token count is "ineffective";
# after two consecutive ineffective passes automatic compaction backs off.  A
# summary-LLM failure puts summarisation into this many seconds of cooldown.
_INEFFECTIVE_COMPACT_MIN_SAVE = 0.10
_COMPACT_FAILURE_COOLDOWN_SECONDS = 60.0

# Three-tier tool classification for microcompact:
#   CLEARABLE: large one-shot outputs, safe to wipe entirely
#   SUMMARIZABLE: outputs where the head/structure matters -- keep first 200 chars
#   PROTECTED: outputs that must NEVER be cleared (diffs, errors, patches)
CLEARABLE_TOOLS = frozenset({
    "web_search", "web_fetch", "pdf", "image",
    "spreadsheet", "document", "media",
})
SUMMARIZABLE_TOOLS = frozenset({
    "bash", "grep", "glob", "file_read", "file_write",
    "file_edit", "git", "lsp", "notebook", "database",
    "docker", "browser", "deploy",
    "test_runner", "lint_format",
})
PROTECTED_TOOLS = frozenset({
    "apply_patch",  # edits are irreplaceable
})

# Backward-compat: legacy single-set union
COMPACTABLE_TOOLS = CLEARABLE_TOOLS | SUMMARIZABLE_TOOLS

# Cap on tokens that active archive injection may add to a summary.
ACTIVE_ARCHIVE_TOKEN_BUDGET = 6_000
# Cap on kept image stubs (recent 2 by default)
ACTIVE_ARCHIVE_KEEP_IMAGES = 2
# How many of the most recent tool results to actively surface after summary
ACTIVE_ARCHIVE_KEEP_RECENT_TOOLS = 6

# P1: milestone summarisation cadence.  When the loop has advanced
# MILESTONE_INTERVAL turns since the last milestone, a fresh
# summary snapshot is written to session.metadata.  Set to 0 to
# disable milestone writes entirely.
MILESTONE_INTERVAL = 12
# Cap on the number of stored milestones.  Older entries are dropped
# so the metadata does not grow unbounded across a long session.
MILESTONE_MAX_ENTRIES = 6

# ── Compact summary prompt ────────────────────────────────────

_COMPACT_PROMPT = """You are summarising an AI agent conversation that has reached the context limit. \
Your summary must be COMPLETE enough for the agent to CONTINUE working without losing any important context.

Output your response in two parts:
<analysis>
Scratchpad -- note what happened, what was completed, what failed, what remains. This is removed before the summary is given to the agent.
</analysis>
<summary>
The actual summary that will be inserted into the conversation.
</summary>

## Required sections in <summary>

### 1. Primary Request and Intent
The user's original goal. What they asked for. Include any constraints or preferences they specified.  # noqa: E501

### 2. Files and Code Sections
List every file that was read, modified, or created. Note key functions, classes, or sections that were worked on. Quote exact paths.

### 3. Key Decisions and Changes Made
What was decided and why. What changes were made. Include architecture decisions, library choices, design patterns.  # noqa: E501

### 4. Errors and Fixes
Every error that occurred and how it was resolved. Include exact error messages when relevant.

### 5. Current State
WHERE the work is right now. What was the last action taken? What is the current value of important variables / configuration / state?

### 6. Pending Tasks
What still needs to be done. List explicitly. Note any blockers.

### 7. User Messages
All messages from the user (not tool results). Preserve the exact questions and instructions.

### 8. Workspace Context
Current working directory, active git branch, relevant environment details.

### 9. Next Step (Optional)
If the next action is clear, state it. Otherwise say what information is needed to decide the next step.  # noqa: E501

## Rules
- Be SPECIFIC. Do not say "various files were edited" -- name them.
- Quote exact file paths, function signatures, and error messages.
- The agent reading this summary cannot see the original messages. It must be able to continue from where you left off.  # noqa: E501
- Keep the summary under 4000 words. Prioritise recent information over old.
"""


class CompactEngine:
    """Model-driven context compaction engine.

    Provides three levels:

    1. :meth:`microcompact` -- cheap, no API call.  Clears old tool results.
    2. :meth:`compact` -- the model reads the conversation and produces a
       structured summary.  This is the primary mechanism.
    3. :meth:`should_compact` -- pure token arithmetic: should we compact?

    The engine is designed to be called **before every API call** in the
    agent loop.  It does NOT modify session state -- it returns new
    message lists that the caller can choose to adopt.

    **Archive fallback**: Before compaction, original messages are saved
    to ``session.compact_archives``.  If the model realises the summary
    is missing critical details, it can request the archive content from
    previous compact events (displayed as compact boundary cards in the
    UI).  This prevents permanent information loss from a single lossy
    summary.
    """

    def __init__(self) -> None:
        """Initialise the compaction engine.

        Sets up the consecutive-failure counter used by the circuit
        breaker and the pre-compact message cache that backs the archive
        fallback (so the model can recall original context if a summary
        drops critical details).
        """
        self._failure_count = 0
        self._last_compact_turn = -1
        # Pre-compact message cache: maps session_id → list of original
        # messages before the most recent compaction.  Saved BEFORE compact
        # runs so the model can request a recall if the summary missed
        # critical info.  Capped (LRU, oldest sessions evicted) so long-lived
        # processes do not accumulate full message lists forever.
        self._pre_compact_cache: dict[str, list[dict[str, Any]]] = {}
        # Anti-thrash guards (port of Hermes/Claude autocompact): consecutive
        # compactions that saved little (< _INEFFECTIVE_COMPACT_MIN_SAVE%) make
        # future automatic compactions skip until real progress, and summary-LLM
        # failures put summarisation into a cooldown window instead of retrying
        # immediately in a hot loop.
        self._ineffective_compaction_count = 0
        self._summary_failure_cooldown_until = 0.0

    def sanitize(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Fix broken tool_call groups (backward compat with EncreCompactEngine).

        Strips orphaned tool results and incomplete assistant tool_call
        blocks that would cause 400 errors from backends.
        """
        return _sanitize_tool_groups(messages)

    # ── Decision ───────────────────────────────────────

    def should_compact(
        self,
        messages: list[dict[str, Any]],
        context_window: int,
        max_output_tokens: int = 32_768,
        *,
        min_turns: int = 4,
        extra_tokens: int = 0,
    ) -> bool:
        """Return True if the token count exceeds the compact threshold.

        The threshold is::

            effective_window = context_window - min(max_output_tokens, 32_768)
            trigger = effective_window - AUTOCOMPACT_BUFFER_TOKENS

        This fires BEFORE context is dangerously full so the model has
        headroom to consume the summary.  ``extra_tokens`` accounts for the
        fixed per-request overhead (system prompt + tool schemas) that is
        not part of *messages*; without it the trigger under-estimates the
        real request size and compaction starts too late.
        """
        if len(messages) < min_turns * 2:
            return False
        effective = context_window - min(max_output_tokens, 32_768)
        threshold = effective - AUTOCOMPACT_BUFFER_TOKENS
        est = count_message_tokens(messages) + max(0, extra_tokens)
        return est > threshold

    def should_microcompact(
        self,
        messages: list[dict[str, Any]],
        context_window: int,
    ) -> bool:
        """Return True if microcompact could free meaningful space."""
        est = count_message_tokens(messages)
        # Trigger only once we cross 40% of the window -- microcompact is
        # cheap but pointless when there is still plenty of headroom.
        return est > context_window * 0.40

    # ── Microcompact (no API call) ─────────────────────────────────

    async def microcompact(
        self,
        messages: list[dict[str, Any]],
        _context_window: int,
        keep_recent_turns: int = MICROCOMPACT_KEEP_RECENT_TURNS,
        protected_texts: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Clear old tool results to free cache-able space.

        Three-tier strategy (per-tool classification):
        - CLEARABLE:  full wipe → ``[Previous tool output cleared]``
        - SUMMARIZABLE: keep first 200 chars + length hint
        - PROTECTED:  never touch (apply_patch, etc.)

        Preserves:
        - System messages
        - All user messages
        - The last *keep_recent_turns* assistant messages and their tools
        - Tool results within the recent window
        - All PROTECTED tool outputs everywhere
        - Tool results whose content contains one of *protected_texts*
          (core-contributed evidence preservation; see
          ``VerificationLedger.preservation_notes``)

        Returns a NEW message list.  Does not mutate the input.
        """
        if len(messages) < keep_recent_turns * 2:
            return list(messages)

        # Evidence markers: any tool result carrying one of these snippets
        # is preserved verbatim.  Markers are truncated to 120 chars to keep
        # the substring scan cheap on long envelopes.
        markers = [
            t.strip()[:120] for t in (protected_texts or []) if str(t).strip()
        ]

        # Find the cutoff: the keep_recent_turns-th assistant from the end
        assistant_count = 0
        cutoff = 0
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                assistant_count += 1
                if assistant_count >= keep_recent_turns:
                    cutoff = i
                    break

        if cutoff == 0:
            return list(messages)

        result: list[dict[str, Any]] = []
        cleared = 0
        summarised = 0
        for i, msg in enumerate(messages):
            if i < cutoff and msg.get("role") == "tool":
                tool_name = msg.get("name", "")
                # PROTECTED: never touch
                if tool_name in PROTECTED_TOOLS:
                    result.append(msg)
                    continue
                content = msg.get("content", "")
                if not isinstance(content, str) or len(content) <= 200:
                    result.append(msg)
                    continue
                # Evidence preservation: a tool result holding a protected
                # snippet (verification evidence, plan state) is never
                # cleared or summarised -- the post-compaction verify gates
                # still need it.
                if markers and any(m in content for m in markers):
                    result.append(msg)
                    continue
                new_msg = dict(msg)
                if tool_name in CLEARABLE_TOOLS:
                    new_msg["content"] = "[Previous tool output cleared]"
                    cleared += 1
                elif tool_name in SUMMARIZABLE_TOOLS:
                    # Keep first 200 chars + total length hint so the
                    # model can still see the head (often contains
                    # the matching line, error message, or file head)
                    head = content[:200]
                    tail = f"\n... [truncated; {len(content) - 200} chars cleared]"
                    new_msg["content"] = head + tail
                    summarised += 1
                else:
                    # Unknown tool: behave like SUMMARIZABLE (safe default)
                    head = content[:200]
                    tail = f"\n... [truncated; {len(content) - 200} chars cleared]"
                    new_msg["content"] = head + tail
                    summarised += 1
                result.append(new_msg)
                continue
            result.append(msg)

        if cleared or summarised:
            logger.info(
                "[microcompact] cleared=%d summarised=%d kept %d recent turns",
                cleared, summarised, keep_recent_turns,
            )
        return result

    # ── Traditional compact (model-driven) ─────────────────────────────

    async def compact(
        self,
        messages: list[dict[str, Any]],
        backend: Any,  # BaseBackend instance
        turn_count: int = 0,
        system_prompt: str = "",
        workspace_context: str = "",
        session_id: str = "",
        *,
        enable_caching: bool = False,
        force: bool = False,
    ) -> list[dict[str, Any]] | None:
        """Run model-driven summarisation.

        Uses *backend* to generate a structured summary of the
        conversation.  Returns a new message list with:
        - System message preserved
        - Summary inserted as user message with ``is_compact_summary: True``
        - Last 2 turns preserved after the summary

        **Archive fallback**: Before compaction, the original messages are
        cached in ``_pre_compact_cache`` keyed by *session_id*.  The
        compacted result includes an archive marker so the model can
        reference it if the summary missed details.

        Returns ``None`` if summarisation fails (circuit breaker open,
        API error, etc.).
        """
        # ── Anti-thrash guards (port of Hermes/Claude autocompact) ─────
        # Skip automatic compaction when recent compactions saved little (the
        # context is dominated by irreducible recent turns) or when the summary
        # LLM recently failed -- otherwise we burn LLM calls re-compacting the
        # same near-limit context in a hot loop.  ``force=True`` (reactive /
        # overflow recovery) bypasses these so a genuine overflow is always
        # handled.
        if not force:
            if self._ineffective_compaction_count >= 2:
                logger.warning(
                    "[compact] skipping -- %d consecutive ineffective compactions",
                    self._ineffective_compaction_count,
                )
                return _budget_fallback(
                    messages, backend.context_window_size(), keep_recent=8,
                )
            if time.time() < self._summary_failure_cooldown_until:
                logger.warning(
                    "[compact] skipping -- summary LLM in cooldown until %.1f",
                    self._summary_failure_cooldown_until,
                )
                return _budget_fallback(
                    messages, backend.context_window_size(), keep_recent=8,
                )

        if self._failure_count >= MAX_CONSECUTIVE_COMPACT_FAILURES:
            logger.warning("[compact] circuit breaker open -- %d consecutive failures, attempting segmented rescue",
                           self._failure_count)
            # P1: circuit-breaker rescue -- before surrendering to budget
            # truncation, try to summarise in segments.  This is more
            # likely to succeed because each segment is smaller and less
            # likely to hit a context-overflow error.
            rescued = await _segmented_rescue(messages, backend, self._failure_count)
            if rescued is not None:
                self._failure_count = 0
                return rescued
            # Final fallback: keep more recent turns than the legacy
            # 4-turn budget so we lose less middle context.
            logger.warning("[compact] segmented rescue failed -- falling back to extended budget truncation")
            return _budget_fallback(messages, backend.context_window_size(), keep_recent=8)

        # Save pre-compact messages to archive cache BEFORE compaction
        if session_id:
            # LRU refresh: re-insert so recency order is maintained, then
            # evict the oldest sessions beyond the cap.
            self._pre_compact_cache.pop(session_id, None)
            self._pre_compact_cache[session_id] = list(messages)
            while len(self._pre_compact_cache) > _PRE_COMPACT_CACHE_MAX_SESSIONS:
                oldest = next(iter(self._pre_compact_cache))
                self._pre_compact_cache.pop(oldest, None)

        # Build the compact prompt -- strip images/documents
        compact_msgs = _prepare_compact_input(messages, system_prompt)

        est = count_message_tokens(compact_msgs)
        logger.info(
            "[compact] starting turn=%d est=%dk failures=%d",
            turn_count, est // 1000, self._failure_count,
        )

        try:
            summary = await _generate_summary(backend, compact_msgs, enable_caching=enable_caching)
        except Exception as exc:
            logger.warning("[compact] API call failed: %s", exc, exc_info=True)
            self._failure_count += 1
            # P1: try segmented rescue before plain budget fallback
            rescued = await _segmented_rescue(messages, backend, self._failure_count)
            if rescued is not None:
                self._failure_count = 0
                return rescued
            return _budget_fallback(messages, backend.context_window_size(), keep_recent=8)

        if not summary or len(summary) < 100:
            logger.warning("[compact] empty or too-short summary (%d chars)",
                           len(summary) if summary else 0)
            self._failure_count += 1
            rescued = await _segmented_rescue(messages, backend, self._failure_count)
            if rescued is not None:
                self._failure_count = 0
                return rescued
            return _budget_fallback(messages, backend.context_window_size(), keep_recent=8)

        # P2: validate that the summary actually covers all 9 required
        # sections.  A hallucinated or truncated summary is dangerous
        # because it silently drops context.  We check the heading
        # strings; if more than one is missing, retry once with an
        # explicit reminder.  If the retry also fails, degrade to
        # segmented rescue rather than accept a lossy summary.
        validation = _validate_summary_sections(summary)
        if not validation.ok:
            logger.warning(
                "[compact] summary missing sections %s -- retrying once",
                validation.missing,
            )
            try:
                retry_summary = await _generate_summary(
                    backend, compact_msgs, extra_instruction=_SUMMARY_SECTION_REMINDER,
                    enable_caching=enable_caching,
                )
                if retry_summary and len(retry_summary) >= 100:
                    retry_validation = _validate_summary_sections(retry_summary)
                    if retry_validation.ok or len(retry_validation.missing) < len(validation.missing):
                        summary = retry_summary
                        validation = retry_validation
            except Exception as retry_exc:
                logger.warning("[compact] section-reminder retry failed: %s", retry_exc)
        if not validation.ok:
            logger.warning(
                "[compact] summary still missing sections %s after retry -- rescuing",
                validation.missing,
            )
            self._failure_count += 1
            rescued = await _segmented_rescue(messages, backend, self._failure_count)
            if rescued is not None:
                self._failure_count = 0
                return rescued


        # P3: cheap text-match check that key constraints from the REAL user
        # messages survive the summary (synthetic compaction blocks are
        # skipped by _extract_key_constraints).  When terms are missing,
        # retry the summary ONCE with an explicit reminder.  A heavy retry
        # loop here burns LLM calls in an already near-limit context, which
        # stalls compaction and triggers repeated turns.
        _key_terms = _extract_key_constraints(messages)
        _missing_text = _verify_key_constraints(summary, _key_terms) if _key_terms else []
        if _missing_text:
            logger.warning(
                "[compact] P3: summary missing %d/%d key constraint terms: %s",
                len(_missing_text), len(_key_terms), _missing_text[:6],
            )
            try:
                _extra_list = "\n".join(f"- {m}" for m in _missing_text[:8])
                _extra = (
                    "Your previous summary was missing some critical user requirements. "
                    "You MUST include the following constraints in your summary:\n"
                    + _extra_list
                    + "\n\nRe-emit the full summary with ALL 9 sections and include "
                    "these constraints in the 'Primary Request and Intent' section."
                )
                _retry_summary = await _generate_summary(
                    backend, compact_msgs, extra_instruction=_extra,
                    enable_caching=enable_caching,
                )
                if _retry_summary and len(_retry_summary) >= 100:
                    summary = _retry_summary
                    _missing_text = _verify_key_constraints(summary, _key_terms)
            except Exception as _rexc:
                logger.warning("[compact] P3 retry failed: %s", _rexc)

        if _missing_text:
            logger.warning(
                "[compact] summary still missing key constraints after retry -- rescuing",
            )
            self._failure_count += 1
            rescued = await _segmented_rescue(messages, backend, self._failure_count)
            if rescued is not None:
                self._failure_count = 0
                return rescued
            return _budget_fallback(messages, backend.context_window_size(), keep_recent=8)

        # P4: optional LLM spot-check of the most critical constraints.
        # Log-only -- a negative result never triggers a heavy summary
        # regeneration.  Each check is an extra LLM call; in a near-limit
        # context these calls tend to fail and stall compaction, which is
        # exactly the failure mode that causes repeated/repetitive turns.
        if _key_terms:
            try:
                _p4_ok, _p4_missing = await _verify_summary_coverage(
                    backend, summary, _key_terms,
                )
                if not _p4_ok:
                    logger.warning(
                        "[compact] P4: summary may miss %d constraints (log-only): %s",
                        len(_p4_missing), _p4_missing[:6],
                    )
            except Exception:
                logger.debug("[compact] P4 verification skipped", exc_info=True)

        self._failure_count = 0
        self._last_compact_turn = turn_count

        # P5: scrub stale architecture-contract blocks out of the summary.
        # The system prompt carries the binding "Architecture Contract" block;
        # compaction may snapshot it into the summary, and once a newer
        # contract supersedes it the stale copy must not survive -- otherwise
        # the model anchors on outdated architecture after compacting.
        try:
            from encre.contract import scrub_contract_mentions
            scrubbed = scrub_contract_mentions(summary)
            if scrubbed and scrubbed != summary:
                logger.info("[compact] scrubbed stale contract block from summary (chars %d->%d)",
                            len(summary), len(scrubbed))
                summary = scrubbed
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
        # Build the compacted message list
        compacted = _build_compacted(messages, summary, workspace_context, system_prompt, session_id)
        new_est = count_message_tokens(compacted)
        logger.info(
            "[compact] done turn=%d msgs %d->%d tokens %dk->%dk "
            "(summary %d chars, sections %d/%d)",
            turn_count, len(messages), len(compacted),
            est // 1000, new_est // 1000, len(summary),
            len(validation.found), 9,
        )

        # ── Post-compact fit check + ineffective tracking (Hermes port) ──
        # If the compacted result is still over the context window (or barely
        # shrank), the compaction did not do its job.  Track consecutive
        # ineffective passes so automatic compaction backs off instead of
        # re-firing every turn; the summary LLM failure goes into cooldown.
        try:
            _window = backend.context_window_size()
        except Exception:
            _window = 0
        _save_ratio = (est - new_est) / est if est else 0.0
        if new_est >= _window > 0:
            logger.warning(
                "[compact] result still over context %dk >= %dk -- treating as ineffective",
                new_est // 1000, _window // 1000,
            )
            self._ineffective_compaction_count += 1
            self._summary_failure_cooldown_until = time.time() + _COMPACT_FAILURE_COOLDOWN_SECONDS
        elif _save_ratio < _INEFFECTIVE_COMPACT_MIN_SAVE:
            logger.warning(
                "[compact] saved only %.1f%% (ineffective) -- streak=%d",
                _save_ratio * 100, self._ineffective_compaction_count + 1,
            )
            self._ineffective_compaction_count += 1
        else:
            self._ineffective_compaction_count = 0

        return compacted

    def get_archive(self, session_id: str) -> list[dict[str, Any]] | None:
        """Retrieve pre-compact messages for *session_id* from the archive cache.

        Returns ``None`` if no archive exists for this session.
        """
        return self._pre_compact_cache.get(session_id)


# ── Internal helpers ──────────────────────────────────────


def _prepare_compact_input(
    messages: list[dict[str, Any]],
    _system_prompt: str = "",
) -> list[dict[str, Any]]:
    """Strip images/documents from messages, add compact prompt.

    Image handling (P3): the most recent ``ACTIVE_ARCHIVE_KEEP_IMAGES``
    image blocks are kept with a brief description stub instead of the
    generic ``[image]`` marker, so the summary can reference them by
    position.  Older images are still collapsed to ``[image]`` to save
    tokens.  PDF/document content: similarly preserved for the most
    recent entries (text-extracted if available) and stubbed for the
    rest.

    Content is also length-capped so a single huge tool result does not
    blow the summary input budget.
    """
    # Pre-scan: identify which image/document blocks are recent enough
    # to keep with descriptive stubs.
    image_count = 0
    document_count = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "image":
                        image_count += 1
                    elif block.get("type") == "document":
                        document_count += 1

    keep_image_threshold = max(0, image_count - ACTIVE_ARCHIVE_KEEP_IMAGES)
    keep_document_threshold = max(0, document_count - 2)
    image_seen = 0
    document_seen = 0

    result: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content", "")

        # Strip image/document blocks from array content
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "image":
                        if image_seen >= keep_image_threshold:
                            # Recent image -- keep with a descriptive stub
                            # so the summary knows it exists.  We do not
                            # embed the bytes; we just note position.
                            alt = block.get("alt", "") or block.get("text", "")
                            stub = f"[recent image: {alt[:80]}]" if alt else "[recent image]"
                            text_parts.append(stub)
                        else:
                            text_parts.append("[image]")
                        image_seen += 1
                    elif btype == "document":
                        if document_seen >= keep_document_threshold:
                            # Recent document -- keep a short text stub.
                            text = block.get("text", "") or block.get("alt", "")
                            if isinstance(text, str) and len(text) > 400:
                                text = text[:400] + "..."
                            text_parts.append(f"[recent document excerpt]\n{text}" if text else "[recent document]")
                        else:
                            text_parts.append("[document]")
                        document_seen += 1
                    else:
                        text_parts.append(str(block)[:200])
            content = " ".join(text_parts)

        if isinstance(content, str) and len(content) > 80_000:
            content = content[:80_000] + "\n... [truncated for compact input]"

        new_msg = dict(msg)
        new_msg["content"] = content
        result.append(new_msg)

    # Append the compact instruction as a user message
    result.append({
        "role": "user",
        "content": _COMPACT_PROMPT,
    })
    return result


async def _generate_summary(
    backend: Any,
    messages: list[dict[str, Any]],
    *,
    extra_instruction: str = "",
    enable_caching: bool = False,
) -> str | None:
    """Call the backend to produce a conversation summary.

    Uses a streaming request with:
    - thinking explicitly disabled
    - no tools
    - a compact output token budget
    - optional *extra_instruction* appended to the compact prompt (used
      to re-emphasise required sections when the first attempt was
      lossy)
    """
    text_parts: list[str] = []

    request_messages = messages
    if extra_instruction:
        # Inject the extra instruction as a fresh user message so the
        # model sees it in the same context as the original prompt.
        request_messages = [
            *messages,
            {"role": "user", "content": extra_instruction},
        ]

    # The compact input is a deepcopy of get_context_messages() -- the
    # compaction view spliced with the persisted tail.  That splice can cut
    # through the middle of a tool_call group (orphan tool results or an
    # incomplete assistant group), which OpenAI-compatible backends such as
    # DeepSeek reject with HTTP 400 ("role 'tool' must follow tool_calls").
    # Pair-repair here so every summary/verification call is safe, not just
    # the main-loop requests.
    request_messages = _sanitize_tool_groups(request_messages)

    try:
        async for event in backend.chat(
            messages=request_messages,
            tools=None,        # NO tools during compact
            tool_choice="none",
            temperature=0.0,
            max_tokens=COMPACT_MAX_OUTPUT_TOKENS,
            stream=True,
            enable_caching=enable_caching,
        ):
            from encre.utils.types import BackendText
            if isinstance(event, BackendText) and event.text:
                text_parts.append(event.text)
    except Exception:
        raise

    full_text = "".join(text_parts).strip()
    if not full_text:
        return None

    # Strip <analysis> block
    import re
    full_text = re.sub(
        r"<analysis>[\s\S]*?</analysis>", "", full_text, flags=re.IGNORECASE,
    )

    # Extract <summary> block
    m = re.search(r"<summary>([\s\S]*?)</summary>", full_text, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # If no tags found, return the entire output
    return full_text



def extract_user_requirements(summary: str) -> str:
    import re as _re
    m = _re.search(
        r"(?:#+\s*)?\d*\.?\s*Primary\s+Request\s+(?:and\s+Intent)?[:\]]*\s*\n(.*?)(?:\n(?:#+\s*)?\d*\.?\s*(?:Files|Key|Errors|Current|Pending|User|Workspace|Next)|$)",
        summary,
        _re.DOTALL | _re.IGNORECASE,
    )
    if m:
        req = m.group(1).strip()
        if len(req) > 800:
            req = req[:800] + "..."
        return (
            "=== User Requirements (extracted from compact summary) ===\n"
            f"{req}\n"
            "=== End User Requirements ==="
        )
    return ""


def _is_synthetic_user_message(msg: dict[str, Any]) -> bool:
    """Return ``True`` for compaction-generated user messages.

    Synthetic entries (summary blocks, active archives, archive hints and
    workspace-context blocks) are ``role: user`` but are NOT user input.
    Anchor/key-constraint logic must skip them, otherwise a re-compaction of
    an already-compacted session anchors on an OLD summary instead of the
    real original task -- losing the original instruction and leaving two
    contradictory summaries in context (the model then repeats old work).
    """
    return bool(
        msg.get("is_synthetic")
        or msg.get("is_compact_summary")
        or msg.get("is_compact_active_archive")
        or msg.get("is_compact_archive_hint")
        or msg.get("is_compact_context")
        or msg.get("is_compact_boundary")
    )


def _build_compacted(
    messages: list[dict[str, Any]],
    summary: str,
    workspace_context: str = "",
    _system_prompt: str = "",
    session_id: str = "",
) -> list[dict[str, Any]]:
    """Build the compacted message list.

    Structure:
        [system message if present]
        [compact boundary marker]
        [summary user message]
        [ACTIVE ARCHIVE -- P0: the N most recent tool outputs that the
         microcompact stage would otherwise have wiped, kept verbatim
         inside a token budget so the model doesn't have to ask]
        [archive reference hint -- legacy fallback]
        [workspace context if present]
        [first user message -- task anchor]
        [last 4 complete turns]

    The first user message is ALWAYS included so the model never forgets
    the original task.  An archive reference is appended so the model
    knows it can request older context if the summary is insufficient.
    """
    result: list[dict[str, Any]] = []

    # Keep system message
    for msg in messages:
        if msg.get("role") == "system":
            result.append(dict(msg))
            break

    # Compact boundary marker
    result.append({
        "role": "user",
        "content": summary,
        "name": "compact_summary",
        "is_compact_boundary": True,
        "is_compact_summary": True,
    })

    # P0 active archive injection -- surface the N most recent tool
    # results verbatim so the model can see them without requesting
    # them.  This is critical: the legacy design relied on the model
    # knowing to ASK for the archive, which it almost never does.
    active_archive = _build_active_archive(messages)
    if active_archive:
        result.append({
            "role": "user",
            "content": active_archive,
            "is_compact_active_archive": True,
        })

    # Archive reference: hint that older context is still available
    if session_id:
        result.append({
            "role": "user",
            "content": (
                "[The full pre-compact conversation history is archived and "
                "available on request. If the summary above missed any critical "
                "details (error messages, file contents, user instructions), "
                "you may request the archive.]"
            ),
            "is_compact_archive_hint": True,
        })

    if workspace_context:
        result.append({
            "role": "user",
            "content": workspace_context,
            "is_compact_context": True,
        })

    # Anchor: first REAL user message (task definition).  Synthetic
    # compaction blocks (previous summaries, archives, hints, workspace
    # context) are skipped so the anchor is the genuine original task even
    # when this session was already compacted before.  Using the old summary
    # as the anchor previously dropped the original instruction and left
    # multiple contradictory summary blocks in context.
    first_user = None
    first_user_idx = -1
    for i, msg in enumerate(messages):
        if msg.get("role") == "user" and not _is_synthetic_user_message(msg):
            first_user = dict(msg)
            first_user_idx = i
            break
    if first_user:
        result.append(first_user)

    # Protected messages: spec definitions and plan messages that must
    # survive compaction so the model never forgets the user's structured
    # requirements.  These are appended right after the first user anchor.
    for msg in messages:
        if msg.get("kind") in ("spec", "plan") or msg.get("is_plan"):
            mid = msg.get("id", "")
            if mid and mid in {m.get("id", "") for m in result}:
                continue
            result.append(dict(msg))

    # Last 4 turns -- increased from 2 to provide more recent context
    # Exclude synthetic (compaction-generated) user messages so that
    # accumulated compression passes don't push keep_from past real turns.
    user_idxs = [
        i for i, m in enumerate(messages)
        if m.get("role") == "user" and not _is_synthetic_user_message(m)
    ]
    keep_from = user_idxs[-4] if len(user_idxs) >= 4 else user_idxs[-2] if len(user_idxs) >= 2 else 0
    keep_from = max(keep_from, first_user_idx if first_user_idx >= 0 else 0)

    seen_ids = {m.get("id", "") for m in result}
    for msg in messages[keep_from:]:
        mid = msg.get("id", "")
        if mid and mid in seen_ids:
            continue
        if mid:
            seen_ids.add(mid)
        result.append(dict(msg))

    return _sanitize_tool_groups(result)


def _build_active_archive(messages: list[dict[str, Any]]) -> str:
    """Build the P0 active-archive block.

    Returns a markdown block that surfaces the N most recent
    tool results (and a few critical early tool results) inside a
    token budget.  The model sees this immediately, no ask required.

    Strategy:
    - Walk messages in reverse
    - Keep tool messages with substantive content (>= 50 chars)
    - Stop once we hit the token budget
    - Include only the most recent ``ACTIVE_ARCHIVE_KEEP_RECENT_TOOLS``
      tool outputs to avoid bloat
    """
    budget = ACTIVE_ARCHIVE_TOKEN_BUDGET
    used = 0
    kept: list[str] = []
    seen_tool_call_ids: set[str] = set()

    # Walk in reverse so most recent first
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue
        if len(kept) >= ACTIVE_ARCHIVE_KEEP_RECENT_TOOLS:
            break
        tool_name = msg.get("name", "tool")
        tool_call_id = msg.get("tool_call_id", "")
        if tool_call_id and tool_call_id in seen_tool_call_ids:
            continue
        if tool_call_id:
            seen_tool_call_ids.add(tool_call_id)
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        # Skip trivial outputs (they were already preserved by
        # microcompact's 200-char threshold).
        if len(content) < 50:
            continue
        # Cap each entry at 800 chars to keep the archive dense.
        if len(content) > 800:
            content = content[:800] + f"\n... [{len(content) - 800} chars trimmed]"
        entry = f"### tool: {tool_name}\n```\n{content}\n```"
        entry_tokens = len(entry) // 4  # rough chars/4 ≈ tokens
        if used + entry_tokens > budget:
            continue
        kept.append(entry)
        used += entry_tokens

    if not kept:
        return ""

    kept.reverse()  # restore chronological order
    return (
        "## Recent Critical Tool Outputs (active archive -- P0 injection)\n"
        "These are the most recent tool outputs from BEFORE the summary. "
        "You can use them directly without asking for the archive.  "
        "If you need older context, ask for the archive.\n\n"
        + "\n\n".join(kept)
    )


def _sanitize_tool_groups(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove / repair tool_call groups so the backend never rejects a turn.

    Handles two orphan shapes that both produce 400 errors -- the classic
    pause/resume failure where a cancelled turn leaves a half-finished
    assistant+tools block behind:

    1. **Orphan tool result** -- a ``role:tool`` message whose
       ``tool_call_id`` no preceding kept assistant declared. The backend
       rejects this as "tool_use_id not found". Fixed by dropping it.

    2. **Incomplete assistant group** -- an assistant ``tool_calls`` block
       missing one or more matching tool_results (a tool was cancelled mid
       turn before its result was written). The backend rejects this as
       "messages must end with a tool_result" / "insufficient tool messages".
       Fixed by synthesizing an error tombstone for each missing id, so the
       already-executed sibling results are preserved instead of dropping the
       whole group.

    An assistant block whose ``tool_calls`` carry no usable id at all is
    dropped together with its trailing tool messages, since it can never be
    matched.
    """
    if not messages:
        return messages

    # tombstone body reused for every synthesized missing result
    tombstone = (
        "[Error: This tool call's result was not persisted. "
        "The tool did not complete.]"
    )

    result: list[dict[str, Any]] = []
    # ids declared by the IMMEDIATELY preceding kept assistant group.  A
    # tool result is only valid while it stays inside the consecutive tool
    # run that directly follows that assistant.  DeepSeek/OpenAI validate
    # ADJACENCY ("Messages with role 'tool' must be a response to a
    # preceding message with 'tool_calls'"), so a result whose id was
    # declared earlier but separated from its host assistant by a
    # user/system message (duplicate result, late-arriving result after
    # pause/resume, interleaved steer injection) must be dropped, not kept.
    pending_declared: set[str] = set()
    # DeepSeek additionally validates GLOBAL uniqueness of every
    # tool_call_id across the whole request ("Duplicate value for
    # 'tool_call_id' ... HTTP 400"), so an id may be declared / answered at
    # most once no matter which group it belongs to.
    global_seen: set[str] = set()
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        role = msg.get("role", "")

        if role == "tool":
            tid = msg.get("tool_call_id", "")
            # Keep only while still inside the tool run of the preceding
            # kept assistant and only if that assistant declared this id.
            if tid and tid in pending_declared:
                result.append(dict(msg))
            # else: orphan / duplicate / late result -> drop
            i += 1
            continue

        if role == "assistant" and msg.get("tool_calls"):
            tool_calls = msg["tool_calls"]
            # An aborted stream can commit the same tool_call twice inside
            # ONE assistant message.  Keep the first occurrence per id and
            # never re-declare an id an earlier kept group already used:
            # the API rejects duplicated ids within tool_calls just like
            # duplicated tool results.
            deduped_calls: list[dict[str, Any]] = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    deduped_calls.append(tc)
                    continue
                cid = tc.get("id", "")
                if cid and (cid in global_seen or
                            any(c.get("id") == cid for c in deduped_calls)):
                    continue
                deduped_calls.append(tc)
            expected = {
                tc.get("id", "") for tc in deduped_calls if tc.get("id")
            }
            if not expected:
                # Assistant claims tool_calls but none have an id -- the
                # block can never be matched.  Drop it and any immediately
                # following tool messages so the API never sees an
                # unmatched tool_call block.
                i += 1
                while i < n and messages[i].get("role") == "tool":
                    i += 1
                continue

            # Collect following tool results (consecutive tool messages).
            j = i + 1
            found: set[str] = set()
            while j < n and messages[j].get("role") == "tool":
                tid = messages[j].get("tool_call_id", "")
                if tid:
                    found.add(tid)
                j += 1

            missing = expected - found
            # Keep the assistant + the FIRST result per declared id.  A
            # double-appended result (aborted turn retried) must not
            # survive twice -- the API rejects the duplicate id.  Any
            # other tool messages in the consecutive run are orphans
            # (their id belongs to no preceding kept assistant) -- drop.
            declared_results: list[dict[str, Any]] = []
            got: set[str] = set()
            for k in range(i + 1, j):
                tid = messages[k].get("tool_call_id")
                if tid in expected and tid not in got:
                    got.add(tid)
                    declared_results.append(dict(messages[k]))
            assistant_msg = dict(msg)
            assistant_msg["tool_calls"] = deduped_calls
            result.append(assistant_msg)
            pending_declared = set(expected)
            result.extend(declared_results)
            if missing:
                # Incomplete group -- synthesize tombstones for the missing
                # ids so the sibling results are preserved instead of
                # discarding the whole turn's work.
                for mid in missing:
                    result.append({
                        "role": "tool",
                        "tool_call_id": mid,
                        "content": tombstone,
                    })
            global_seen |= expected
            i = j
            continue

        # Any non-tool message (user / system / plain assistant) closes the
        # current tool run: results appearing after it can no longer be
        # paired adjacently with the earlier assistant.
        result.append(dict(msg))
        pending_declared = set()
        i += 1

    return result


def _keep_recent_for_window(context_window: int) -> int:
    """Scale the kept-recent-turns count by the model context window.

    Small-window models must retain fewer turns or the last-resort
    truncation itself still overflows the window.
    """
    if context_window <= 0:
        return 4
    if context_window >= 128_000:
        return 8
    if context_window >= 32_000:
        return 6
    if context_window >= 16_000:
        return 4
    return 2


def _budget_fallback(
    messages: list[dict[str, Any]],
    _context_window: int,
    keep_recent: int = 4,
) -> list[dict[str, Any]] | None:
    """Last-resort budget-based truncation when the model call fails.

    Keeps: system + first user + spec/plan anchors + last *keep_recent* turns.

    The effective turn count is clamped by the model's context window
    (see :func:`_keep_recent_for_window`) so small-window models do not
    overflow even with the extended fallback.
    """
    # Scale down for small windows; explicit call sites may ask for more
    # turns on large-window models, but the window always wins.
    keep_recent = min(keep_recent, _keep_recent_for_window(_context_window))
    if len(messages) <= keep_recent * 2 + 2:
        return list(messages)

    system = [m for m in messages if m.get("role") == "system"]
    first_user = None
    for m in messages:
        if m.get("role") == "user" and not _is_synthetic_user_message(m):
            first_user = dict(m)
            break

    # Protected messages: specs and plans that must survive truncation.
    protected = []
    for m in messages:
        if m.get("kind") in ("spec", "plan") or m.get("is_plan"):
            mid = m.get("id", "")
            if mid and mid == (first_user or {}).get("id", ""):
                continue
            protected.append(dict(m))

    non_system = [m for m in messages if m.get("role") != "system"]
    recent = non_system[-(keep_recent * 2):]

    result = system + ([first_user] if first_user else []) + protected + recent
    return _sanitize_tool_groups(result)


# ── P1 segmented rescue ─────────────────────────────────────


async def _segmented_rescue(
    messages: list[dict[str, Any]],
    backend: Any,
    _failure_count: int = 0,
    max_segments: int = 3,
) -> list[dict[str, Any]] | None:
    """P1 circuit-breaker rescue: summarise in segments.

    When the full conversation cannot be summarised in one pass
    (context overflow, API error, etc.), we slice the messages into
    ``max_segments`` roughly-equal chunks and summarise each
    independently.  Each segment is small enough to succeed, and the
    concatenation gives us a richer summary than a single budget
    truncation.

    Returns a compacted message list, or ``None`` if every segment
    fails (caller should fall back to ``_budget_fallback``).
    """
    if len(messages) < 6:
        return None

    # Slice into segments at user-message boundaries so each segment
    # is a coherent chunk of the conversation.
    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_indices) < 2:
        return None
    chunk_size = max(1, len(user_indices) // max_segments)
    boundaries = [user_indices[i] for i in range(0, len(user_indices), chunk_size)]
    if boundaries[-1] != len(messages):
        boundaries.append(len(messages))

    segment_summaries: list[str] = []
    for seg_idx in range(len(boundaries) - 1):
        start = boundaries[seg_idx]
        end = boundaries[seg_idx + 1]
        segment = messages[start:end]
        if len(segment) < 2:
            continue
        try:
            seg_input = _prepare_compact_input(segment, "")
            seg_summary = await _generate_summary(backend, seg_input)
            if seg_summary and len(seg_summary) >= 50:
                segment_summaries.append(
                    f"### Segment {seg_idx + 1}\n{seg_summary.strip()}"
                )
        except Exception as exc:
            logger.warning(
                "[segmented_rescue] segment %d failed: %s", seg_idx + 1, exc,
            )
            continue

    if not segment_summaries:
        return None

    # Build the result: system + combined summary + first user + last turns
    combined_summary = (
        "## Segmented Rescue Summary\n"
        "The full conversation was too large for a single summary, so it "
        "was split and summarised in segments.\n\n"
        + "\n\n".join(segment_summaries)
    )
    return _build_compacted(messages, combined_summary, "", "", "")


# ── P2 summary validation ────────────────────────────────────


_REQUIRED_SECTION_HEADINGS: tuple[str, ...] = (
    "Primary Request and Intent",
    "Files and Code Sections",
    "Key Decisions and Changes",
    "Errors and Fixes",
    "Current State",
    "Pending Tasks",
    "User Messages",
    "Workspace Context",
    "Next Step",
)


class _SectionValidation:
    """Result of :func:`_validate_summary_sections`."""

    __slots__ = ("found", "missing", "ok")

    def __init__(self, found: list[str], missing: list[str]) -> None:
        self.found = found
        self.missing = missing
        self.ok = not missing


def _validate_summary_sections(summary: str) -> _SectionValidation:
    """P2: confirm the model-generated summary actually covers the
    9 required sections.  The compact prompt REQUIRES these headings;
    a missing heading usually means the model ran out of output tokens
    or hallucinated structure.
    """
    if not summary:
        return _SectionValidation(found=[], missing=list(_REQUIRED_SECTION_HEADINGS))

    found: list[str] = []
    missing: list[str] = []
    lowered = summary.lower()
    for heading in _REQUIRED_SECTION_HEADINGS:
        # Match either the literal heading or its lowercased prefix
        # (e.g. "1. Primary Request" or "### Primary Request and Intent")
        needle = heading.lower()
        if needle in lowered:
            found.append(heading)
        else:
            # Try matching just the first 25 chars
            short = needle[:25]
            if short in lowered:
                found.append(heading)
            else:
                missing.append(heading)
    return _SectionValidation(found=found, missing=missing)


_SUMMARY_SECTION_REMINDER = (
    "Your previous summary was missing some required sections.  "
    "Please RE-EMIT the full summary with ALL 9 sections present and clearly headed:\n"
    "1. Primary Request and Intent\n"
    "2. Files and Code Sections\n"
    "3. Key Decisions and Changes Made\n"
    "4. Errors and Fixes\n"
    "5. Current State\n"
    "6. Pending Tasks\n"
    "7. User Messages\n"
    "8. Workspace Context\n"
    "9. Next Step (Optional)\n"
    "Wrap the response in <summary>...</summary> tags.  Do NOT skip any section."
)





# P3: Compact content verification
def _extract_key_constraints(messages, max_terms=15):
    """Extract key constraint terms from user messages before compaction."""
    import re as _re
    patterns = [
        r"do\s*n[o']t\s+use\s+(\w[\w.]*)",
        r"don't\s+use\s+(\w[\w.]*)",
        r"must\s+not\s+(\w[\w.]*)",
        r"should\s+not\s+(\w[\w.]*)",
        r"without\s+(\w[\w.]*)",
        r"avoid\s+(\w[\w.]*)",
        r"only\s+use\s+(\w[\w.]*)",
        r"prefer\s+(\w[\w.]*)",
        r"use\s+(\w+[\d.]+\w*)\s+instead",
        r"not\s+(\w[\w.]*)\s+but\s+(\w[\w.]*)",
        r"no\s+(\w[\w.]*)",
    ]
    terms = set()
    for msg in messages:
        if msg.get("role") != "user":
            continue
        # Skip compaction-generated synthetic blocks (previous summaries,
        # archives, hints): they are not user input and would pollute the
        # constraint set with stale content on re-compaction.
        if _is_synthetic_user_message(msg):
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        lowered = content.lower()
        for pattern in patterns:
            for m in _re.finditer(pattern, lowered):
                for g in m.groups():
                    if g and len(g) > 2:
                        terms.add(g.rstrip(".,;:!?"))
        for m in _re.finditer(r"\b(v?\d+\.\d+)\b", lowered):
            terms.add(m.group(1).rstrip(".,;:!?"))
        _tech_pattern = (
            r"\b(python|rust|go|typescript|javascript|react|vue|django|flask"
            r"|fastapi|postgres|mysql|redis|docker|kubernetes|aws|gcp|azure)\b"
        )
        for m in _re.finditer(_tech_pattern, lowered):
            terms.add(m.group(1))
        if len(terms) >= max_terms:
            break
    return terms


def _verify_key_constraints(summary, key_terms):
    """Check which key constraint terms appear in the compact summary."""
    if not key_terms or not summary:
        return list(key_terms) if key_terms else []
    lowered = summary.lower()
    missing = []
    for term in key_terms:
        if term not in lowered:
            missing.append(term)
    return missing


# P4: summarisation verification loop
_VERIFICATION_PROMPT = (
    "You are checking whether a conversation summary is complete.\n\n"
    "SUMMARY:\n{summary}\n\n"
    "KEY CONSTRAINT: Does the summary above mention or address the following "
    'user requirement: "{constraint}"?\n\n'
    "Answer EXACTLY one word: YES if the summary mentions this requirement, "
    "NO if it does not. If unsure, answer NO."
)

_MAX_VERIFICATION_RETRIES = 2


async def _verify_summary_coverage(backend, summary, key_terms):
    """Verify that summary covers the key constraint terms."""
    if not key_terms or not summary:
        return True, []
    critical = sorted(key_terms, key=len, reverse=True)[:5]
    missing = []
    for term in critical:
        try:
            prompt = _VERIFICATION_PROMPT.format(summary=summary[:2000], constraint=term)
            result = ""
            async for event in backend.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=None, tool_choice="none",
                temperature=0.0, max_tokens=3, stream=True,
            ):
                from encre.utils.types import BackendText
                if isinstance(event, BackendText) and event.text:
                    result += event.text
            result = result.strip().upper()
            if "NO" in result and "YES" not in result:
                missing.append(term)
        except Exception:
            continue
    return len(missing) == 0, missing


# Backward-compatible alias
EncreCompactEngine = CompactEngine


# ---------------------------------------------------------------------------
# Legacy rule-based compaction strategies (moved from compact/strategies.py)
#
# These :class:`EncreCompactStrategy` implementations predate the model-driven
# :class:`CompactEngine` above and are kept for backward compatibility.  Each
# strategy exposes ``compact`` (produce a reduced message list) and
# ``should_compact`` (decide whether to run).
#
# :class:`EncreMultiStagePipeline` chains them from cheapest to most
# expensive -- budget reduction -> snip -> micro compact -> semantic compact
# -> context collapse -> auto (LLM) compact -- escalating only as needed and
# protecting the task anchor and recent turns.
# ---------------------------------------------------------------------------

def _estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate full message tokens including tool_calls and block content."""
    return count_message_tokens([message])


class EncreCompactStrategy(ABC):
    """Abstract base class for all compaction strategies.

    Subclasses implement :meth:`compact` (return a reduced copy of the
    message list) and :meth:`should_compact` (decide whether compaction is
    warranted for the current token budget).
    """
    @abstractmethod
    async def compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    async def should_compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> bool:
        ...


class EncreAlwaysCompactStrategy(EncreCompactStrategy):
    """Aggressive strategy that always keeps only the first and last turns.

    Useful as a guaranteed fallback: it never relies on token estimates,
    merely retaining the first two and last two non-system messages.
    """
    async def compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> list[dict[str, Any]]:
        """Keep system messages plus the first and last two turns."""
        if len(messages) <= 2:
            return messages
        system_messages = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        if len(non_system) <= 4:
            return messages
        kept = non_system[:2] + non_system[-2:]
        return system_messages + kept

    async def should_compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> bool:
        """Compact once the conversation exceeds ten messages."""
        return len(messages) > 10


class EncreTokenBudgetStrategy(EncreCompactStrategy):
    """Keep messages until a fraction of the token budget is consumed."""
    def __init__(self, budget_ratio: float = 0.5) -> None:
        """Configure the fraction of the token budget to retain."""
        self._budget_ratio = budget_ratio

    async def compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> list[dict[str, Any]]:
        """Greedily keep messages (system, then first and last turns) under budget."""
        budget = int(_max_tokens * self._budget_ratio)
        system_messages = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        if not non_system:
            return messages

        kept: list[dict[str, Any]] = []
        token_count = 0
        for m in system_messages:
            kept.append(m)
            token_count += self._estimate_tokens(m)

        first = non_system[:2]
        for m in first:
            kept.append(m)
            token_count += self._estimate_tokens(m)

        last = non_system[-2:]
        for m in last:
            t = self._estimate_tokens(m)
            if token_count + t <= budget or m == last[-1]:
                kept.append(m)
                token_count += t

        return kept

    async def should_compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> bool:
        """Compact when total tokens exceed the configured budget ratio."""
        total = sum(self._estimate_tokens(m) for m in messages)
        return total > _max_tokens * self._budget_ratio

    @staticmethod
    def _estimate_tokens(message: dict[str, Any]) -> int:
        """Estimate a single message's token count."""
        return _estimate_message_tokens(message)


class EncreBudgetReductionStrategy(EncreCompactStrategy):
    """Stage 1: Cap per-message size. Always active, cheapest.
    Truncates individual messages that exceed per-message limits."""

    def __init__(self, max_chars_per_message: int = 40000) -> None:
        """Configure the maximum characters retained per message."""
        self._max_chars = max_chars_per_message

    async def compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> list[dict[str, Any]]:
        """Truncate every message that exceeds the per-message character cap."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > self._max_chars:
                truncated = dict(msg)
                truncated["content"] = content[:self._max_chars] + "\n... [message truncated]"
                result.append(truncated)
            elif isinstance(content, list):
                # Handle array content (Anthropic format)
                truncated = dict(msg)
                new_content: list[dict[str, Any]] = []
                total_len = 0
                for block in content:
                    block_text = str(block.get("text", block.get("input", "")))
                    total_len += len(block_text)
                    if total_len <= self._max_chars:
                        new_content.append(block)
                    else:
                        break
                truncated["content"] = new_content
                result.append(truncated)
            else:
                result.append(msg)
        return result

    async def should_compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> bool:
        """Trigger when any single message exceeds the per-message cap."""
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > self._max_chars:
                return True
        return False


class EncreSnipStrategy(EncreCompactStrategy):
    """Stage 2: Trim older history, keeping system + N most recent turns.

    Turn count adapts to token budget so that even a conversation with very
    large tool outputs (e.g. web_search, bash) is trimmed aggressively
    rather than blowing past the context window.
    """

    def __init__(self, keep_recent_turns: int = 6) -> None:
        """Configure how many recent turns to retain when snipping."""
        self._keep_turns = keep_recent_turns
        self._min_turns = 3

    async def compact(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 128000,
    ) -> list[dict[str, Any]]:
        """Keep system + the most recent turns that fit within 60% of budget."""
        system = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        if len(non_system) <= self._min_turns * 2:
            return messages

        # Adaptive: start at keep_turns, shrink if the result still exceeds budget
        for keep_turns in range(self._keep_turns, self._min_turns - 1, -1):
            kept = non_system[-(keep_turns * 2):]
            total = sum(_estimate_message_tokens(m) for m in system + kept)
            if total <= max_tokens * 0.6:
                return system + kept
        # Last resort: minimal context
        return system + non_system[-(self._min_turns * 2):]

    async def should_compact(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 128000,
    ) -> bool:
        """Snip only once the token budget is actually threatened (>=50%)."""
        non_system = [m for m in messages if m.get("role") != "system"]
        if len(non_system) <= self._min_turns * 2 + 2:
            return False
        # Only snip when the token budget is actually threatened
        total = sum(_estimate_message_tokens(m) for m in messages)
        return total > max_tokens * 0.5


class EncreMicroCompactStrategy(EncreCompactStrategy):
    """Stage 3: Merge consecutive similar-role messages, trim verbose tool outputs.
    Non-destructive: only compresses redundant content."""

    async def compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> list[dict[str, Any]]:
        """Merge consecutive user messages and trim verbose tool outputs."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content", "")
            role = msg.get("role", "")

            # Trim large tool results -- shorter cap to handle tools like web_search/web_fetch
            if role in ("tool",) or (isinstance(content, str) and len(content) > 2000):
                if isinstance(content, str):
                    trimmed = dict(msg)
                    trimmed["content"] = content[:2000] + "\n... [output truncated for space]"
                    result.append(trimmed)
                else:
                    result.append(msg)
            # Merge consecutive user messages (e.g., guidance injection)
            elif role == "user" and result and result[-1].get("role") == "user":
                prev_content = result[-1].get("content", "")
                if isinstance(prev_content, str) and isinstance(content, str):
                    result[-1]["content"] = prev_content + "\n\n" + content
                else:
                    result.append(msg)
            else:
                result.append(msg)
        return result

    async def should_compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> bool:
        """Trigger when a message exceeds 2000 chars or history is very long."""
        # Check if any message has large content
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 2000:
                return True
        return len(messages) > 15


class EncreContextCollapseStrategy(EncreCompactStrategy):
    """Stage 4: Replace old tool outputs with one-line summaries.
    Preserves the fact that a tool was called but not the full output."""

    def __init__(self, collapse_before_turn: int = 5) -> None:
        """Configure how many recent turns are protected from collapsing."""
        self._collapse_before = collapse_before_turn

    async def compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> list[dict[str, Any]]:
        """Collapse tool outputs older than the protected turn window."""
        if len(messages) <= self._collapse_before * 2:
            return messages

        result: list[dict[str, Any]] = []
        turn_count = 0
        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content", "")

            # Count turns from the end
            if role == "assistant":
                turn_count += 1

            turns_from_end = 0
            # Count remaining assistant messages
            for j in range(i, len(messages)):
                if messages[j].get("role") == "assistant":
                    turns_from_end += 1

            # Collapse old tool results
            if role in ("tool",) and turns_from_end > self._collapse_before:
                if isinstance(content, str) and len(content) > 200:
                    collapsed = dict(msg)
                    collapsed["content"] = content[:200] + " [collapsed]"
                    result.append(collapsed)
                else:
                    result.append(msg)
            else:
                result.append(msg)

        return result

    async def should_compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> bool:
        """Collapse when total tokens exceed 60% of the budget."""
        if len(messages) <= self._collapse_before * 3:
            return False
        total = sum(_estimate_message_tokens(m) for m in messages)
        return total > _max_tokens * 0.6


class EncreSemanticCompactStrategy(EncreCompactStrategy):
    """Stage 5: Semantic tool output summarization and context partitioning.

    Instead of blunt truncation, intelligently summarizes tool outputs:
    - grep -> groups matches by file, shows counts + first 3 matches
    - glob -> groups files by directory
    - bash -> extracts errors and key output lines
    - file_read -> preserves function signatures only

    Also splits context into hot/warm/cold tiers for better information
    density management.
    """

    def __init__(self, max_tool_output_chars: int = 8000) -> None:
        """Wire up the shared :class:`SemanticToolOutputCompactor`."""
        from encre.compact.semantic import SemanticToolOutputCompactor
        self._compactor = SemanticToolOutputCompactor()
        self._compactor.MAX_TOOL_OUTPUT_CHARS = max_tool_output_chars

    async def compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> list[dict[str, Any]]:
        """Replace oversized tool outputs with semantic summaries."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            tool_name = msg.get("name", "")

            if role in ("tool",) and isinstance(content, str):
                compacted = self._compactor.compact_tool_output(tool_name, content)
                if compacted != content:
                    new_msg = dict(msg)
                    new_msg["content"] = compacted
                    result.append(new_msg)
                    continue
            result.append(msg)
        return result

    async def should_compact(
        self,
        messages: list[dict[str, Any]],
        _max_tokens: int = 128000,
    ) -> bool:
        """Trigger when any tool output exceeds 8000 characters."""
        for msg in messages:
            if msg.get("role") in ("tool",):
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 8000:
                    return True
        return False


_strategies_prompt_loader = PromptLoader()


class EncreAutoCompactStrategy(EncreCompactStrategy):
    """Auto compact strategy with optional LLM summarization.

    Falls back to budget-based compaction when no backend is available.
    When a backend is provided, uses LLM summarization as the last stage.
    """

    SUMMARIZE_PROMPT = _strategies_prompt_loader.load("summarize", category="compact")

    def __init__(
        self,
        threshold_ratio: float = 0.75,
        backend: Any = None,
        summarizer_model: str = "",
    ) -> None:
        """Configure threshold ratio, optional summarisation backend/model."""
        self._threshold_ratio = threshold_ratio
        self._backend = backend
        self._summarizer_model = summarizer_model

    def set_backend(self, backend: Any) -> None:
        """Attach a backend used for LLM-based summarisation."""
        self._backend = backend

    async def compact(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 128000,
    ) -> list[dict[str, Any]]:
        """Run budget truncation, then LLM summary if a backend is available."""
        should = await self.should_compact(messages, max_tokens)
        if not should:
            return messages

        # Stage 1: budget-based truncation (cheapest)
        budget_strategy = EncreTokenBudgetStrategy(budget_ratio=0.6)
        truncated = await budget_strategy.compact(messages, max_tokens)

        # Stage 2: LLM summarization (when backend available)
        if self._backend is not None and len(truncated) > 4:
            try:
                summary = await self._summarize(truncated)
                if summary:
                    # Keep system message + summary + last 2 messages
                    system_msgs = [m for m in truncated if m.get("role") == "system"]
                    non_system = [m for m in truncated if m.get("role") != "system"]
                    keep_last = non_system[-2:] if len(non_system) >= 2 else non_system
                    summary_msg = {"role": "user", "content": f"[Conversation summary]\n{summary}"}
                    truncated = [*system_msgs, summary_msg, *keep_last]
            except Exception:
                pass  # Summarization failed, keep truncated result

        return truncated

    async def _summarize(self, messages: list[dict[str, Any]]) -> str:
        """Call the backend to generate a summary of the conversation."""
        if self._backend is None:
            return ""

        conversation_text: list[str] = []
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if isinstance(content, str):
                conversation_text.append(f"[{role}]: {content[:500]}")
            elif isinstance(content, list):
                # Multimodal content -- extract text parts only
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", "")[:500])
                conversation_text.append(f"[{role}]: {' '.join(parts)}")

        full_text = "\n".join(conversation_text)
        if len(full_text) < 500:
            return ""

        summary_msgs = [
            {"role": "user", "content": f"{self.SUMMARIZE_PROMPT}\n\nConversation:\n{full_text[:8000]}"},
        ]

        try:
            result_parts: list[str] = []
            async for event in self._backend.chat(
                messages=summary_msgs,
                max_tokens=512,
                temperature=0.0,
                stream=True,
                enable_caching=False,
            ):
                from encre.utils.types import BackendText
                if isinstance(event, BackendText):
                    result_parts.append(event.text)
            return "".join(result_parts).strip()
        except Exception:
            return ""

    async def should_compact(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 128000,
    ) -> bool:
        """Compact when total tokens exceed the configured threshold ratio."""
        total = sum(self._estimate_tokens(m) for m in messages)
        return total > max_tokens * self._threshold_ratio

    @staticmethod
    def _estimate_tokens(message: dict[str, Any]) -> int:
        """Estimate a single message's token count."""
        return _estimate_message_tokens(message)


class EncreMultiStagePipeline(EncreCompactStrategy):
    """6-stage compaction pipeline -- cheapest first, only escalates when needed.

    Stages (in order):
      1. Budget Reduction -- cap per-message size (always)
      2. Snip -- trim oldest history beyond N turns
      3. MicroCompact -- merge similar messages, trim verbose tool outputs
      4. Semantic Compact -- intelligent tool output summarization
      5. Context Collapse -- summarize old tool outputs to one-liners
      6. Auto Compact -- full model-generated summary (last resort)

    Architecture: budget -> snip -> micro -> semantic -> collapse -> auto-compact
    """

    def __init__(
        self,
        context_threshold: float = 0.20,
        keep_recent_turns: int = 6,
        collapse_before_turn: int = 4,
        max_messages: int = 15,
    ) -> None:
        """Build the ordered list of compaction stages."""
        self._context_threshold = context_threshold
        self._max_messages = max_messages
        self._stages: list[EncreCompactStrategy] = [
            EncreBudgetReductionStrategy(),
            EncreSnipStrategy(keep_recent_turns=keep_recent_turns),
            EncreMicroCompactStrategy(),
            EncreSemanticCompactStrategy(),
            EncreContextCollapseStrategy(collapse_before_turn=collapse_before_turn),
            EncreAutoCompactStrategy(threshold_ratio=0.75),
        ]

    @staticmethod
    def _sanitize_sequence(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fix message sequences broken by compaction.

        Compaction strategies snip by message count, which can split tool_call
        groups -- an assistant message with N tool_calls must be followed by
        exactly N tool results. If any are missing, most backends (OpenAI,
        DeepSeek, Anthropic) reject the request with a 400 error.

        Also handles leading/trailing orphaned messages.
        """
        if not messages:
            return messages

        # Phase 1: strip leading/trailing orphans -- must loop because popping
        # an assistant(tool_calls) can expose new leading tool messages.
        while True:
            while messages and messages[0].get("role") == "tool":
                messages.pop(0)
            if not messages:
                break
            if messages[0].get("role") == "assistant" and messages[0].get("tool_calls"):
                messages.pop(0)
                continue  # Recheck for newly exposed leading tools
            break
        while messages and messages[-1].get("role") == "tool":
            messages.pop()

        # Phase 2: ensure every assistant with tool_calls has ALL its tool results
        result: list[dict[str, Any]] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "")
            tool_calls = msg.get("tool_calls")

            if role == "assistant" and tool_calls:
                # Collect expected tool_call_ids
                expected_ids: set[str] = set()
                for tc in tool_calls:
                    tid = tc.get("id", "") if isinstance(tc, dict) else ""
                    if tid:
                        expected_ids.add(tid)

                # Look ahead for following tool results
                j = i + 1
                found_ids: set[str] = set()
                tool_results: list[dict[str, Any]] = []
                while j < len(messages) and messages[j].get("role") == "tool":
                    tid = messages[j].get("tool_call_id", "")
                    if tid:
                        found_ids.add(tid)
                        tool_results.append(messages[j])
                    j += 1

                # Keep only if ALL expected tool results are present
                if expected_ids and expected_ids.issubset(found_ids):
                    result.append(msg)
                    result.extend(tool_results)
                    i = j
                    continue
                # Missing tool results -> drop this assistant + partial tool results
                i = j
                continue

            result.append(msg)
            i += 1

        return result

    async def compact(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 128000,
    ) -> list[dict[str, Any]]:
        """Run all stages (multi-pass) then sanitise and anchor-protect."""
        current = messages
        # Multi-pass: run the full pipeline up to 3 times so that
        # information-dense outputs (web_search results, large diffs)
        # get multiple rounds of reduction until they stabilise.
        for _round in range(3):
            changed = False
            for stage in self._stages:
                if await stage.should_compact(current, max_tokens):
                    prev = current
                    current = await stage.compact(current, max_tokens)
                    if current != prev:
                        changed = True
                if not await self._is_over_threshold(current, max_tokens):
                    break
            if not changed or not await self._is_over_threshold(current, max_tokens):
                break

        # -- Anchor-point protection ---------------------------------------------
        # The first user message is the TASK -- it must never be lost.
        # The last assistant + tool results are the CURRENT STATE --
        # the model needs them to continue.  We splice both into the
        # compacted result if any stage removed them.
        current = self._sanitize_sequence(current)
        current = self._anchor_protect(current, messages)
        return current

    async def should_compact(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 128000,
    ) -> bool:
        """Compact when message count or token total crosses the threshold."""
        if len(messages) > self._max_messages:
            return True
        return await self._is_over_threshold(messages, max_tokens)

    async def _is_over_threshold(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> bool:
        """Return True while total tokens exceed the context threshold."""
        total = sum(self._estimate_tokens(m) for m in messages)
        return total > max_tokens * self._context_threshold

    def _anchor_protect(
        self,
        compacted: list[dict[str, Any]],
        original: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Ensure the first user message and last 2 turns survive.

        The first user message IS the task definition -- lose it and the
        model forgets why it's working.  The last two turns carry the
        immediate context the model needs to reason forward.  Any
        compaction stage that removed these is overridden.
        """
        # Find anchor messages by scanning original
        first_user = None
        for m in original:
            if m.get("role") == "user":
                first_user = m
                break

        # Find last 2 user messages and their follow-ups
        user_idxs = [i for i, m in enumerate(original) if m.get("role") == "user"]
        penultimate_user_idx = user_idxs[-2] if len(user_idxs) >= 2 else -1
        recent_original = original[penultimate_user_idx:] if penultimate_user_idx >= 0 else []

        # Check what survived compaction
        compacted_ids = {m.get("id", "") for m in compacted if m.get("id")}

        # Ensure first user is present
        if first_user is not None and first_user.get("id", "") not in compacted_ids:
            compacted = [first_user, *compacted]
            compacted_ids.add(first_user.get("id", ""))

        # Ensure recent messages (last 2 turns) are present
        for m in recent_original:
            if m.get("id", "") and m.get("id") not in compacted_ids:
                compacted.append(m)
                compacted_ids.add(m.get("id", ""))

        # Deduplicate by id while preserving order
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for m in compacted:
            mid = m.get("id", "")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            deduped.append(m)

        # Sanitize: run _sanitize_sequence to fix any broken tool_call groups
        return self._sanitize_sequence(deduped)

    @staticmethod
    def _estimate_tokens(message: dict[str, Any]) -> int:
        """Estimate a single message's token count."""
        return _estimate_message_tokens(message)
