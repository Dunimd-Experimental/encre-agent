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
- Output is capped at 4 096 tokens

The summary is inserted as a pair of boundary messages:

    [role: compact_boundary]
    [role: user, is_compact_summary: true, content: <summary>]

Anchor protection
-----------------
The FIRST user message (task definition) and the LAST 2 complete turns
are never removed.  They are spliced into the compacted result after
the summary.

Boundary format
---------------
Messages carry an ``is_compact_boundary`` marker so the frontend can
render a visual divider, and ``is_compact_summary`` so the model can
distinguish the summary from real user input.
"""

import time
from typing import Any

from encre.logging_config import get_logger
from encre.utils.tokens import count_message_tokens

logger = get_logger("encre.compact")

# 鈹€鈹€ Thresholds 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

AUTOCOMPACT_BUFFER_TOKENS = 13_000   # how far below the effective window we trigger
MICROCOMPACT_CACHE_TTL_MINUTES = 30  # clear tool results older than this
MICROCOMPACT_KEEP_RECENT_TURNS = 5   # keep this many recent turns during microcompact
COMPACT_MAX_OUTPUT_TOKENS = 16_384   # max tokens for the summary response
MAX_CONSECUTIVE_COMPACT_FAILURES = 3  # circuit breaker

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
    "docker", "browser", "find_tool", "deploy",
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

# 鈹€鈹€ Compact summary prompt 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

_COMPACT_PROMPT = """You are summarising an AI agent conversation that has reached the context limit. Your summary must be COMPLETE enough for the agent to CONTINUE working without losing any important context.  # noqa: E501

Output your response in two parts:
<analysis>
Scratchpad -- note what happened, what was completed, what failed, what remains. This will be removed before the summary is given to the agent.  # noqa: E501
</analysis>
<summary>
The actual summary that will be inserted into the conversation.
</summary>

## Required sections in <summary>

### 1. Primary Request and Intent
The user's original goal. What they asked for. Include any constraints or preferences they specified.  # noqa: E501

### 2. Files and Code Sections
List every file that was read, modified, or created. Note key functions, classes, or sections that were worked on. Quote exact paths.  # noqa: E501

### 3. Key Decisions and Changes Made
What was decided and why. What changes were made. Include architecture decisions, library choices, design patterns.  # noqa: E501

### 4. Errors and Fixes
Every error that occurred and how it was resolved. Include exact error messages when relevant.

### 5. Current State
WHERE the work is right now. What was the last action taken? What is the current value of important variables / configuration / state?  # noqa: E501

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
        # Pre-compact message cache: maps session_id 鈫?list of original messages
        # before the most recent compaction.  Saved BEFORE compact runs so
        # the model can request a recall if the summary missed critical info.
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

    # 鈹€鈹€ Decision 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

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

    # 鈹€鈹€ Microcompact (no API call) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    async def microcompact(
        self,
        messages: list[dict[str, Any]],
        _context_window: int,
        keep_recent_turns: int = MICROCOMPACT_KEEP_RECENT_TURNS,
    ) -> list[dict[str, Any]]:
        """Clear old tool results to free cache-able space.

        Three-tier strategy (per-tool classification):
        - CLEARABLE:  full wipe 鈫?``[Previous tool output cleared]``
        - SUMMARIZABLE: keep first 200 chars + length hint
        - PROTECTED:  never touch (apply_patch, etc.)

        Preserves:
        - System messages
        - All user messages
        - The last *keep_recent_turns* assistant messages and their tools
        - Tool results within the recent window
        - All PROTECTED tool outputs everywhere

        Returns a NEW message list.  Does not mutate the input.
        """
        if len(messages) < keep_recent_turns * 2:
            return list(messages)

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

    # 鈹€鈹€ Traditional compact (model-driven) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

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
        # 鈹€鈹€ Anti-thrash guards (port of Hermes/Claude autocompact) 鈹€鈹€鈹€鈹€鈹€
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
            self._pre_compact_cache[session_id] = list(messages)

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
            pass

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

        # 鈹€鈹€ Post-compact fit check + ineffective tracking (Hermes port) 鈹€鈹€
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


# 鈹€鈹€ Internal helpers 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


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

    Uses a non-streaming request with:
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
        entry_tokens = len(entry) // 4  # rough chars/4 鈮?tokens
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
    # ids declared by an assistant we have already KEPT (and that therefore
    # may legitimately be referenced by a later tool_result).
    seen_declared: set[str] = set()
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        role = msg.get("role", "")

        if role == "tool":
            tid = msg.get("tool_call_id", "")
            # Keep only if a preceding kept assistant declared this id.
            if tid and tid in seen_declared:
                result.append(dict(msg))
            # else: orphan result (no matching tool_call) -> drop
            i += 1
            continue

        if role == "assistant" and msg.get("tool_calls"):
            tool_calls = msg["tool_calls"]
            expected = {tc.get("id", "") for tc in tool_calls if tc.get("id")}
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
            # Keep the assistant + only the tool results it declared. Any
            # other tool messages in the consecutive run are orphans (their
            # id belongs to no preceding kept assistant) -- drop them.
            declared_results = [
                dict(messages[k]) for k in range(i + 1, j)
                if messages[k].get("tool_call_id") in expected
            ]
            result.append(dict(msg))
            seen_declared |= expected
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
            i = j
            continue

        result.append(dict(msg))
        i += 1

    return result


def _budget_fallback(
    messages: list[dict[str, Any]],
    _context_window: int,
    keep_recent: int = 4,
) -> list[dict[str, Any]] | None:
    """Last-resort budget-based truncation when the model call fails.

    Keeps: system + first user + spec/plan anchors + last *keep_recent* turns.
    """
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


# 鈹€鈹€ P1 segmented rescue 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


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


# 鈹€鈹€ P2 summary validation 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


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
        for m in _re.finditer(r"\b(python|rust|go|typescript|javascript|react|vue|django|flask|fastapi|postgres|mysql|redis|docker|kubernetes|aws|gcp|azure)\b", lowered):
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


# Backward-compatible alias# 鈹€鈹€ Backward-compatible alias 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

EncreCompactEngine = CompactEngine
