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

import logging
import re
from typing import Any

from encre.errors import WithheldError  # noqa: F401
from encre.prompts.loader import PromptLoader
from encre.utils.types import BackendText

from encre.logging_config import get_logger


async def adjudicate_deliberation(
    backend: Any,
    text_parts: list[str],
    thinking_parts: list[str],
) -> bool:
    """Confirm the deliberation-loop heuristic with a cheap LLM verdict.

    The heuristic casts a wide net; this one-shot call (tiny output budget,
    no tools, no conversation history) asks the model itself to classify
    the flagged monologue, so verbose but legitimate answers are never
    interrupted by a nudge.  Returns True to proceed with the nudge.

    Any adjudication failure falls back to the heuristic verdict (True):
    availability problems must not change loop behaviour.
    """
    if backend is None:
        return True
    text = "".join(text_parts).strip()
    thinking = "".join(thinking_parts).strip()
    monologue = text if len(text) >= len(thinking) else thinking
    if len(monologue) > 4000:
        monologue = monologue[:2000] + "\n[...]\n" + monologue[-2000:]
    try:
        chunks: list[str] = []
        gen = backend.chat(
            messages=[
                {
                    "role": "user",
                    "content": (
                        "You are a strict output classifier reviewing the LAST "
                        "response of an AI coding agent.\n\nRESPONSE:\n"
                        f"{monologue}\n\nClassify the response:\n"
                        "- DELIVERABLE: it concretely answers the user's request, "
                        "or presents a committed result, summary, or question as "
                        "this turn's answer.\n"
                        "- LOOP: it is an uncommitted internal deliberation "
                        "monologue -- repeatedly re-deciding and hedging without "
                        "acting or answering.\n\n"
                        "Reply with exactly one word: DELIVERABLE or LOOP."
                    ),
                }
            ],
            tools=None,
            max_tokens=8,
            stream=False,
        )
        async for event in gen:
            if isinstance(event, BackendText):
                chunks.append(event.text)
        verdict = "".join(chunks).strip().upper()
        return "LOOP" in verdict or "DELIVERABLE" not in verdict
    except Exception:
        return True


logger = get_logger(__name__)

# Prompt copy for runtime-injected messages (grace calls, auto-continue,
# delegation guidance, steer injections, thinking prefills, standing-orders
# reminders) lives in prompts/runtime/ -- never hardcoded in Python.  Runtime
# texts are static per process; the loader already caches them, so callers
# may invoke the loaders freely on every turn.

_RUNTIME_LOADER: PromptLoader | None = None


def _get_runtime_loader() -> PromptLoader:
    global _RUNTIME_LOADER
    if _RUNTIME_LOADER is None:
        _RUNTIME_LOADER = PromptLoader()
    return _RUNTIME_LOADER


def _load_runtime_text(name: str, **ctx: Any) -> str:
    """Load a runtime prompt body (stripping frontmatter) from ``runtime/``.

    Falls back to an empty string when the file is missing so callers can
    degrade gracefully instead of raising mid-loop.
    """
    try:
        if ctx:
            return _get_runtime_loader().load_with_context(name, category="runtime", **ctx)
        return _get_runtime_loader().load(name, category="runtime")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[runtime_prompt] load '%s' failed: %s", name, exc)
        return ""


def _load_runtime_meta(name: str) -> dict[str, Any]:
    """Load a runtime prompt's frontmatter metadata from ``runtime/``.

    Returns an empty dict when the file is missing or has no frontmatter so
    the checkpoint gate can degrade to safe defaults.
    """
    try:
        meta, _body = _get_runtime_loader().load_full(name, category="runtime")
        return meta
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[runtime_prompt] meta '%s' failed: %s", name, exc)
        return {}


# ── Limits ────────────────────────────────────────────────────────────
MAX_EMPTY_RESPONSE_RETRIES = 2
MAX_TRUNCATED_TOOL_CALL_RETRIES = 2
MAX_STUB_RESPONSE_RETRIES = 2
MAX_DELIBERATION_RETRIES = 2
MAX_MESSAGE_REPAIR_DEPTH = 50
PRE_API_COMPACT_THRESHOLD_RATIO = 0.85
POST_TOOL_COMPACT_THRESHOLD_RATIO = 0.80
MAX_STEER_MESSAGES = 5

# ── Surrogate / control character patterns ────────────────────────────
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\n{4,}")


# ── 1. Message Repair ─────────────────────────────────────────────────


def repair_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repair message list before sending to the API.

    Fixes:
    - Role alternation: ensure user/assistant/tool roles alternate properly
    - Surrogate characters: remove invalid Unicode surrogates
    - Control characters: strip non-printable control chars
    - Excessive whitespace: collapse 4+ newlines to 2
    - Empty messages: remove or pad with whitespace
    - Tool call / tool result pairing: ensure every tool_call has a result

    Mirrors Hermes agent's message sanitization in conversation_loop.py.
    """
    if not messages:
        return messages

    repaired: list[dict[str, Any]] = []

    for msg in messages:
        msg = dict(msg)  # shallow copy
        role = msg.get("role", "")
        content = msg.get("content", "")

        # Strip surrogates and control characters from string content
        if isinstance(content, str):
            content = _SURROGATE_RE.sub("", content)
            content = _CONTROL_RE.sub("", content)
            content = _WHITESPACE_RE.sub("\n\n", content)
            msg["content"] = content

        # Also fix tool call arguments
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                args = tc.get("function", {}).get("arguments", "")
                if isinstance(args, str):
                    args = _SURROGATE_RE.sub("", args)
                    args = _CONTROL_RE.sub("", args)
                    tc["function"]["arguments"] = args

        # Skip completely empty messages (no content, no tool_calls, no tool_call_id)
        if not content and not msg.get("tool_calls") and not msg.get("tool_call_id"):
            continue

        # Pad empty assistant content when it has tool_calls (API requires content)
        if role == "assistant" and msg.get("tool_calls") and not content:
            msg["content"] = ""

        # Pad empty tool results (API requires non-null content)
        if role == "tool" and not content:
            msg["content"] = "(tool returned no output)"

        repaired.append(msg)

    # Fix role alternation: ensure the first message is system/user,
    # and tool messages are preceded by assistant with tool_calls.
    repaired = _fix_role_alternation(repaired)

    return repaired


def _fix_role_alternation(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure messages alternate properly for the chat completions API.

    Rules:
    - Consecutive same-role messages get merged
    - Tool messages are left untouched; pairing is handled separately
      by _sanitize_tool_groups so we don't insert empty tool_calls blocks
      that DeepSeek/OpenAI reject.
    - Leading assistant messages get a user placeholder
    """
    if not messages:
        return messages

    result: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "")

        # Merge consecutive same-role user/assistant messages.  We deep-copy
        # when mutating so the original session state is not affected.
        if result and result[-1].get("role") == role and role in ("user", "assistant"):
            prev = result[-1]
            prev_content = str(prev.get("content") or "")
            cur_content = str(msg.get("content") or "")
            if prev_content and cur_content:
                prev["content"] = prev_content + "\n" + cur_content
            elif cur_content:
                prev["content"] = cur_content
            # Merge tool_calls for assistant messages, preserving order.
            if role == "assistant" and msg.get("tool_calls"):
                prev_tc = list(prev.get("tool_calls") or [])
                prev_tc.extend(msg["tool_calls"])
                prev["tool_calls"] = prev_tc
            # Preserve ``reasoning_content`` across the merge.  The merged
            # assistant now owns both tool_call sets, and DeepSeek's
            # thinking mode (plus GLM interleaved / Kimi preserved thinking)
            # rejects the follow-up request with HTTP 400 when a tool-call
            # assistant lacks its ``reasoning_content`` -- dropping the
            # merged-away side's reasoning would reintroduce exactly that.
            if role == "assistant":
                prev_r = str(prev.get("reasoning_content") or "")
                cur_r = str(msg.get("reasoning_content") or "")
                if prev_r and cur_r:
                    prev["reasoning_content"] = prev_r + "\n" + cur_r
                elif cur_r:
                    prev["reasoning_content"] = cur_r
            # Preserve compact-layering markers from the merged message so a
            # real user instruction merged into a summary doesn't erase the
            # summary/archive/hint boundary the compact engine set.  Without
            # this, the model can no longer distinguish "summary" from "live
            # user instruction" after repair_messages runs.
            for _marker in (
                "is_compact_summary",
                "is_compact_boundary",
                "is_compact_active_archive",
                "is_compact_archive_hint",
            ):
                if msg.get(_marker) and not prev.get(_marker):
                    prev[_marker] = True
            continue

        result.append(dict(msg))

    # Ensure first non-system message is a user message
    first_idx = 0
    if result and result[0].get("role") == "system":
        first_idx = 1
    if first_idx < len(result) and result[first_idx].get("role") == "assistant":
        result.insert(
            first_idx,
            {
                "role": "user",
                "content": "(continue)",
            },
        )

    return result


# ── 2. Empty Response Detection ───────────────────────────────────────


def is_empty_response(
    text_parts: list[str],
    tool_call_buffers: dict[int, dict[str, Any]],
    thinking_parts: list[str],
) -> bool:
    """Return True if the model returned no usable content."""
    has_text = any(part.strip() for part in text_parts)
    has_tools = bool(tool_call_buffers)
    has_thinking = any(part.strip() for part in thinking_parts)
    return not (has_text or has_tools or has_thinking)


def build_empty_retry_message(retry_count: int) -> str:
    """Build the retry message for empty responses."""
    if retry_count == 1:
        return (
            "Your previous response was empty. Please provide a response. "
            "If you need to use a tool, do so. If you're done, say so explicitly."
        )
    return "Your response was empty again. You must respond with text or a tool call. Do not return an empty response."


# ── 2b. Stub Response Detection ──────────────────────────────────────
# A stub is a response that has *some* text (so is_empty_response is False)
# but is too brief to be a real deliverable — e.g. "I'll help you with that."
# or "Done!" — without any tool calls to back it up.
#
# This catches the #1 task-delivery failure mode: the model acknowledges the
# request but never actually starts working, and the auto-continue mechanism
# doesn't fire because token_budget is 0 or exhausted.

_STUB_PATTERNS: tuple[str, ...] = (
    "i'll help",
    "i will help",
    "let me help",
    "i can help",
    "i'll do",
    "i will do",
    "let me do",
    "i'll start",
    "i will start",
    "let me start",
    "i'll begin",
    "i will begin",
    "let me begin",
    "i'll look",
    "i will look",
    "let me look",
    "i'll check",
    "i will check",
    "let me check",
    "i'll try",
    "i will try",
    "let me try",
    "i'll see",
    "i will see",
    "let me see",
    "i'll work",
    "i will work",
    "let me work",
    "i'll get",
    "i will get",
    "let me get",
    "sure!",
    "of course!",
    "absolutely!",
    "no problem!",
    "i'll take",
    "i will take",
    "let me take",
    "i'll handle",
    "i will handle",
    "let me handle",
)

_STUB_COMPLETED_PATTERNS: tuple[str, ...] = (
    "done!",
    "finished!",
    "complete!",
    "all done",
    "all set",
    "that's it",
    "thats it",
    "here you go",
    "there you go",
)

# Responses with these markers are NOT stubs — they contain real deliverable
# content (code, file paths, structured output).
_REAL_CONTENT_MARKERS: tuple[str, ...] = (
    "```",  # code block
    "file:///",  # file reference
    ".py",
    ".js",
    ".ts",
    ".rs",
    ".go",
    ".java",  # file extensions
    "def ",
    "class ",
    "function ",
    "import ",  # code keywords
    "http://",
    "https://",  # URLs
)


def is_stub_response(
    text_parts: list[str],
    tool_call_buffers: dict[int, dict[str, Any]],
    thinking_parts: list[str],
    prompt: str = "",
) -> bool:
    """Return True if the model returned a suspiciously brief text-only response.

    A stub has:
    - Some text (not empty — empty is handled by :func:`is_empty_response`)
    - NO tool calls (a response with tools is never a stub)
    - Brief text (< 300 chars) that looks like an acknowledgment without action

    This catches the failure mode where the model says "I'll help you with
    that." or "Done!" without actually doing any work.  The auto-continue
    mechanism only fires when ``token_budget > 0``, so without this check a
    stub slips through to ``finish("stop")`` and the user gets nothing.

    Conservative by design — false negatives (missing a stub) are harmless,
    false positives (flagging a real response) waste one retry turn.
    """
    if tool_call_buffers:
        return False  # Has tool calls — not a stub
    text = "".join(text_parts).strip()
    if not text:
        return False  # Empty — handled by is_empty_response
    if len(text) > 300:
        return False  # Substantial text — probably a real response

    # If the model produced substantial thinking content, it may be
    # reasoning before acting — don't flag as stub.
    thinking = "".join(thinking_parts).strip()
    if len(thinking) > 200:
        return False

    # If the response contains real content markers, it's not a stub
    # even if it's short (e.g. a short code snippet).
    lowered = text.lower()
    for marker in _REAL_CONTENT_MARKERS:
        if marker in lowered:
            return False

    # Check for acknowledgment-only patterns ("I'll help", "Let me check", etc.)
    for pattern in _STUB_PATTERNS:
        if pattern in lowered:
            return True

    # "Done!" / "Finished!" without any tool calls this turn is suspicious.
    # Only flag if the user's prompt was substantial (a real task, not small
    # talk).  A 5-char "Done!" in response to "thanks" is fine.
    if prompt and len(prompt.strip()) > 50:
        for pattern in _STUB_COMPLETED_PATTERNS:
            if pattern in lowered and len(text) < 100:
                return True

    # Very short text (< 60 chars) in response to a substantial prompt
    # is suspicious even without a known stub pattern.
    return len(text) < 60 and bool(prompt) and len(prompt.strip()) > 100


def build_stub_retry_message(retry_count: int) -> str:
    """Build a nudge that tells the model to actually do the work.

    Unlike :func:`build_empty_retry_message` (for truly empty responses),
    this targets stubs where the model wrote a brief acknowledgment but
    never started working.  The message is forceful: *act now, don't narrate*.
    """
    if retry_count == 1:
        return (
            "[Your previous response was too brief to be a real deliverable. "
            "Do not just acknowledge the request — actually DO the work now. "
            "Use the appropriate tools to gather context, execute the task, "
            "and verify the result. A short text acknowledgment is NOT a "
            "deliverable. Act.]"
        )
    return (
        "[Your response is still too brief. You MUST take action NOW: "
        "call the appropriate tools, execute the task, and deliver a "
        "complete result. Do not stop with just text — act.]"
    )


# ── 2c. Deliberation Loop Detection ───────────────────────────────────────────────────────────────
# The mirror image of a stub: instead of a too-brief acknowledgment, the model
# produces a very LONG stream of internal monologue ("let me...", "OK, final
# decision...", "wait, let me reconsider...") that re-decides the same thing
# over and over, never emits a tool call, and never commits to an answer.
# Such a turn burns the entire token budget with zero delivery.

_DELIBERATION_MARKERS: tuple[str, ...] = (
    "let me",
    "i'll",
    "i will",
    "hmm",
    "wait,",
    "ok,",
    "okay,",
    "final decision",
    "reconsider",
    "should i",
    "maybe i",
    "on second thought",
    "alternatively",
    "i think i'll",
    "let me just",
    "let me reconsider",
    "actually,",
    "i'm torn",
    "ugh",
    "circular",
    "let me think",
)

_DELIBERATION_MIN_TOTAL_CHARS = 1500
# Classic loop: thinking-only monologue (visible text near-empty).
_DELIBERATION_MIN_MARKERS = 8
# Text-heavy monologue: verbose prose answers are the main false-positive
# risk, so require a much denser hedge pattern before flagging.
_DELIBERATION_MIN_MARKERS_TEXT_HEAVY = 12


def is_deliberation_loop(
    text_parts: list[str],
    tool_call_buffers: dict[int, dict[str, Any]],
    thinking_parts: list[str],
) -> bool:
    """Return True if the response is a long uncommitted deliberation monologue.

    A deliberation loop has:
    - NO tool calls (a response with tools made progress by definition)
    - A large combined volume of text and/or thinking (>= 1500 chars)
    - Heavy density of hedging/re-planning markers (tiered threshold)
    - No real deliverable content (no code blocks, file references, URLs)
    - No trailing question to the user (asking IS a legitimate exit)

    Thinking-only responses (empty visible text, long reasoning) are the
    classic case: the model re-decides the same question dozens of times,
    then the turn ends without acting or answering.  Conservative by design:
    false negatives are harmless, false positives waste one retry turn.
    """
    if tool_call_buffers:
        return False  # Has tool calls -- not stuck
    text = "".join(text_parts).strip()
    thinking = "".join(thinking_parts).strip()
    combined = f"{text}\n{thinking}".strip()
    if len(combined) < _DELIBERATION_MIN_TOTAL_CHARS:
        return False
    # Real deliverable content (code, files, links) is never a deliberation
    # loop, even when it is verbose.
    lowered = combined.lower()
    for marker in _REAL_CONTENT_MARKERS:
        if marker in lowered:
            return False
    # A response that ends by asking the user a question already took the
    # legitimate exit -- asking IS a deliverable, never nudge it.
    if text.rstrip().endswith("?"):
        return False
    marker_hits = sum(lowered.count(marker) for marker in _DELIBERATION_MARKERS)
    threshold = _DELIBERATION_MIN_MARKERS if len(text) < 300 else _DELIBERATION_MIN_MARKERS_TEXT_HEAVY
    return marker_hits >= threshold


def build_deliberation_retry_message(retry_count: int) -> str:
    """Build a nudge that forces the model out of a deliberation loop.

    The failure being corrected is endless internal re-planning: the model
    keeps re-deciding instead of acting OR asking.  The nudge explicitly
    authorizes the two -- and only two -- acceptable exits.
    """
    if retry_count == 1:
        return (
            "[Your previous response was hundreds of words of internal "
            "deliberation with no action and no answer. STOP deliberating. "
            "You must do exactly ONE of the following now: (a) act -- call "
            "the appropriate tools and execute the task, or (b) if a stored "
            "rule genuinely conflicts with the request, reply to the user "
            "with ONE short sentence stating the conflict and asking for "
            "confirmation. Long internal monologue is NOT a deliverable.]"
        )
    return (
        "[You are still deliberating instead of delivering. Decide NOW: "
        "either call the tools to do the work, or reply with ONE concise "
        "clarifying question to the user. Do not output any further "
        "re-planning or internal monologue.]"
    )


# ── 3. Truncated Tool Call Detection ──────────────────────────────────


def is_truncated_tool_call(tool_call_buffers: dict[int, dict[str, Any]]) -> bool:
    """Return True if any tool call has truncated/invalid JSON arguments."""
    for tc in tool_call_buffers.values():
        args = tc.get("arguments", "")
        if not args:
            return True
        if isinstance(args, str):
            stripped = args.strip()
            if not stripped:
                return True
            # Check if it looks like truncated JSON
            if stripped.startswith("{") and not stripped.endswith("}"):
                return True
            if stripped.startswith("[") and not stripped.endswith("]"):
                return True
            # Try to parse
            import json

            try:
                json.loads(stripped)
            except json.JSONDecodeError:
                # Could be truncated or just bad JSON
                if len(stripped) > 10:
                    return True
    return False


def build_truncated_retry_message(tool_name: str, args_preview: str) -> str:
    """Build the retry message for truncated tool calls."""
    return (
        f"The tool call for '{tool_name}' was truncated or had invalid arguments. "
        f"Arguments received: {args_preview[:200]}... "
        f"Please re-issue the tool call with complete, valid JSON arguments."
    )


# ── 4. Pre-API Token Pressure Check ──────────────────────────────────


def check_token_pressure(
    messages: list[dict[str, Any]],
    context_window: int,
    max_tokens: int,
    extra_tokens: int = 0,
) -> float:
    """Return the ratio of estimated input tokens to available context.

    Values > 1.0 mean the request will likely overflow.
    Values > PRE_API_COMPACT_THRESHOLD_RATIO mean we should compact first.

    ``extra_tokens`` accounts for the fixed per-request overhead (system
    prompt + tool schemas) that is sent alongside *messages* but is not
    part of the message list itself.  Ignoring it made the pressure check
    under-estimate by tens of thousands of tokens for memory-heavy agents,
    delaying compaction until the model degraded on an overfull context.
    """
    try:
        from encre.utils.tokens import count_message_tokens

        input_tokens = count_message_tokens(messages)
    except Exception:
        # Fallback: rough estimate (4 chars per token)
        total_chars = sum(len(str(m.get("content", ""))) + len(str(m.get("tool_calls", ""))) for m in messages)
        input_tokens = total_chars // 4
    input_tokens += max(0, extra_tokens)

    available = context_window - max_tokens
    if available <= 0:
        return 1.0
    return input_tokens / available


# ── 5. Post-Tool Compression Check ────────────────────────────────────


def should_post_tool_compact(
    messages: list[dict[str, Any]],
    context_window: int,
    max_tokens: int,
    extra_tokens: int = 0,
) -> bool:
    """Return True if context should be compressed after tool execution."""
    ratio = check_token_pressure(
        messages,
        context_window,
        max_tokens,
        extra_tokens=extra_tokens,
    )
    return ratio > POST_TOOL_COMPACT_THRESHOLD_RATIO


# ── 6. Interrupt / Abort Check ────────────────────────────────────────


class InterruptSignal:
    """Lightweight interrupt signal that can be checked cheaply."""

    def __init__(self) -> None:
        self._aborted = False

    def abort(self) -> None:
        self._aborted = True

    @property
    def is_aborted(self) -> bool:
        return self._aborted

    def reset(self) -> None:
        self._aborted = False


def check_interrupt(loop: Any) -> bool:
    """Return True if the loop should abort before the next API call.

    Checks the loop's cancel flag and any pending interrupt signals.
    """
    # Check the loop's existing _cancelled() method
    cancelled = getattr(loop, "_cancelled", lambda: False)
    if callable(cancelled):
        try:
            return bool(cancelled())
        except Exception:
            logger.debug("exception suppressed", exc_info=True)

    # Check for abort event
    cancel_event = getattr(loop, "_cancel_event", None)
    if cancel_event is not None:
        try:
            return cancel_event.is_set()
        except Exception:
            logger.debug("exception suppressed", exc_info=True)

    return False


# ── Requirement change detection ──────────────────────────────────────

_REQUIREMENT_CHANGE_KEYWORDS: tuple[str, ...] = (
    "actually",
    "instead",
    "change",
    "forget",
    "ignore",
    "different",
    "new plan",
    "instead of",
    "rethink",
    "reconsider",
    "new approach",
    "scrap",
    "abandon",
    "never mind",
    "on second thought",
    "let me rephrase",
    "let me clarify",
    "change of plan",
    "actually, don't",
    "actually, do",
    "forget what i said",
    "disregard",
    "start over",
    "from scratch",
    "different direction",
    "new requirement",
    "changed my mind",
)


def _detect_requirement_change(prompt: str) -> str:
    """Detect whether *prompt* signals a mid-conversation requirement change.

    Returns the matched keyword phrase (for logging) or an empty string
    when no change is detected.  Used by the loop to invalidate the cached
    user requirements summary so the next compact produces a fresh anchor.

    This is a heuristic -- it catches the most common "I changed my mind"
    patterns without requiring an LLM call.  False positives (detecting a
    change when none happened) are safe: they just cause a fresh compact
    summary, which is harmless.
    """
    if not prompt:
        return ""
    lowered = prompt.lower()
    for kw in _REQUIREMENT_CHANGE_KEYWORDS:
        if kw in lowered:
            return kw
    return ""


# ── 7. Tombstone Generation ───────────────────────────────────────────


def build_tombstone_messages(
    tool_call_buffers: dict[int, dict[str, Any]],
    reason: str,
    reasoning_content: str = "",
) -> list[dict[str, Any]]:
    """Build tombstone messages for orphaned tool calls.

    When the streaming is interrupted or the model fails mid-response,
    any tool_use blocks that were emitted need matching tool_result
    blocks or the API will reject the next request.

    Unlike the simple orphan cleanup in recovery_loop.py, tombstones
    also include a placeholder assistant message so the conversation
    history clearly shows where the interruption happened.  That
    placeholder carries the orphaned ``tool_calls`` (each tool result
    must have an owning assistant message or the OpenAI-protocol
    endpoints reject the request) and the reasoning captured before the
    interruption: DeepSeek's thinking mode requires the
    ``reasoning_content`` of every tool-call assistant to be passed back
    in all subsequent requests (HTTP 400 "must be passed back"
    otherwise).

    Mirrors Claude Code's tombstone pattern in query.ts.
    """
    tombstones: list[dict[str, Any]] = []

    # Rebuild the orphaned tool_calls the interrupted attempt emitted so
    # the tombstone assistant owns the tool results that follow.
    orphaned_calls: list[dict[str, Any]] = []
    for tc in tool_call_buffers.values():
        tc_id = tc.get("id", "") or tc.get("_client_id", "")
        if not tc_id:
            continue
        orphaned_calls.append(
            {
                "id": tc_id,
                "type": "function",
                "function": {
                    "name": str(tc.get("name", "")),
                    "arguments": str(tc.get("arguments", "") or "{}"),
                },
            }
        )

    # Add a placeholder assistant message marking the interruption
    if tool_call_buffers:
        placeholder: dict[str, Any] = {
            "role": "assistant",
            "content": f"[Response interrupted: {reason}]",
        }
        if orphaned_calls:
            placeholder["tool_calls"] = orphaned_calls
            if reasoning_content:
                placeholder["reasoning_content"] = reasoning_content
        tombstones.append(placeholder)

    # Add tool_result blocks for each orphaned tool call
    for tc in tool_call_buffers.values():
        tc_id = tc.get("id", "") or tc.get("_client_id", "")
        if not tc_id:
            continue
        tombstones.append(
            {
                "role": "tool",
                "tool_call_id": tc_id,
                "name": tc.get("name", ""),
                "content": f"[Tool execution was interrupted: {reason}]",
                "is_error": True,
            }
        )

    return tombstones


def classify_error(exc: Exception) -> str:
    """Classify an error to determine which recovery strategy to try.

    Returns one of: 'context_overflow', 'rate_limit', 'empty_response',
    'truncated_tool_call', 'network', 'auth', 'unknown'.
    """
    msg = str(exc).lower()

    if is_rate_limit_or_overload_msg(msg):
        return "rate_limit"
    if is_context_overflow_msg(msg):
        return "context_overflow"
    if "timeout" in msg or "timed out" in msg or "connection" in msg:
        return "network"
    if "auth" in msg or "unauthorized" in msg or "api key" in msg:
        return "auth"
    return "unknown"


def is_rate_limit_or_overload_msg(msg: str) -> bool:
    patterns = [
        "rate limit",
        "too many requests",
        "overloaded",
        "capacity",
        "throttl",
        "429",
        "503",
        "service unavailable",
        "high demand",
    ]
    return any(p in msg for p in patterns)


def is_context_overflow_msg(msg: str) -> bool:
    patterns = [
        "prompt is too long",
        "context length exceeded",
        "input length exceeds",
        "input is too long",
        "maximum context length",
        "reduce the length",
        "too many tokens",
        "413",
        "request too large",
    ]
    return any(p in msg for p in patterns)


# ── 9. Thinking Prefill ───────────────────────────────────────────────


def build_thinking_prefill(
    prompt: str,
    *,
    enabled: bool = False,
    hint: str = "",
) -> str | None:
    """Build a thinking block prefill to guide the model.

    When enabled, this returns a partial thinking block that the model
    will continue from, helping it start reasoning immediately.

    Mirrors Hermes agent's thinking prefill in conversation_loop.py.
    """
    if not enabled:
        return None

    # Build a brief thinking hint based on the prompt.  The hint copy lives
    # in prompts/runtime/ (category "runtime"), never hardcoded in Python.
    if not hint:
        if "?" in prompt:
            hint = _load_runtime_text("thinking_prefill_question") or "Let me analyze this question step by step."
        elif "error" in prompt.lower() or "bug" in prompt.lower():
            hint = _load_runtime_text("thinking_prefill_bug") or "Let me investigate this issue systematically."
        else:
            hint = _load_runtime_text("thinking_prefill_default") or "Let me think about the best approach."

    return hint


# ── 10. Budget Grace Call ─────────────────────────────────────────────


class BudgetState:
    """Tracks token budget and whether a grace call has been used.

    The state persists across compaction (the loop instance is not rebuilt
    on compact) AND across session restarts -- ``used_tokens`` /
    ``max_tokens`` / ``grace_used`` are checkpointed to session metadata by
    ``checkpoint()`` so a restarted session resumes the same budget instead
    of resetting to zero.  Mirrors Claude Code's task_budget beta which
    accrues across compact boundaries.
    """

    META_KEY = "budget_state"

    def __init__(self, *, max_tokens: int = 0, grace_enabled: bool = True) -> None:
        self.max_tokens = max_tokens
        self.used_tokens = 0
        self.grace_enabled = grace_enabled
        self._grace_used = False

    @property
    def is_exhausted(self) -> bool:
        return self.max_tokens > 0 and self.used_tokens >= self.max_tokens

    @property
    def can_grace(self) -> bool:
        """Return True if a grace call is available."""
        return self.grace_enabled and self.is_exhausted and not self._grace_used

    def use_grace(self) -> None:
        self._grace_used = True

    def add_usage(self, tokens: int) -> None:
        self.used_tokens += tokens

    def checkpoint(self) -> dict:
        """Return a serialisable snapshot for session-metadata persistence."""
        return {
            "max_tokens": self.max_tokens,
            "used_tokens": self.used_tokens,
            "grace_enabled": self.grace_enabled,
            "grace_used": self._grace_used,
        }

    @classmethod
    def restore(cls, snapshot: dict | None, *, fallback_max: int = 0) -> BudgetState:
        """Rebuild a BudgetState from a checkpointed snapshot.

        When *snapshot* is missing or malformed, returns a fresh state with
        ``fallback_max`` (the current config's token_budget).  This keeps a
        restarted session resuming its accrued budget.
        """
        if not isinstance(snapshot, dict):
            return cls(max_tokens=fallback_max)
        try:
            state = cls(
                max_tokens=int(snapshot.get("max_tokens", fallback_max) or fallback_max),
                grace_enabled=bool(snapshot.get("grace_enabled", True)),
            )
            state.used_tokens = int(snapshot.get("used_tokens", 0) or 0)
            state._grace_used = bool(snapshot.get("grace_used", False))
            return state
        except (TypeError, ValueError):
            return cls(max_tokens=fallback_max)


def build_grace_message(remaining_work: str = "") -> str:
    """Build the grace call message when budget is exhausted."""
    msg = _load_runtime_text("grace_message") or (
        "Token budget exhausted. This is a grace call - please wrap up your "
        "current task concisely. Provide a summary of what was done and "
        "what remains."
    )
    msg = msg.replace("{{remaining_work_suffix}}", "")
    if remaining_work:
        msg += f"\n\nRemaining work: {remaining_work}"
    return msg.strip()


def build_auto_continue_message(progress_context: str = "") -> str:
    """Build a message that nudges the model to continue where it left off.

    Mirrors Claude Code's token budget auto-continue
    (query/tokenBudget.ts:checkTokenBudget): when the model stops early but
    still has budget, inject a concise nudge so it keeps going rather than
    forcing the user to say "continue".

    Args:
        progress_context: Optional real progress state (Plan-Do-Review step
            graph, or verification-ledger / error-tool run state).  Appended
            verbatim after the nudge so the continuation decision is grounded
            in facts; empty means the plain nudge, unchanged.

    Returns:
        The synthetic continuation nudge message.
    """
    msg = _load_runtime_text("auto_continue_message") or (
        "[Continue from where you left off. Do NOT repeat work already done. Keep going with the next task or step.]"
    )
    if progress_context:
        msg = f"{msg}\n\n{progress_context}"
    return msg


def build_delegation_guidance() -> str:
    """Build the coordinator-style delegation guidance for the system prompt.

    Mirrors Claude Code's coordinator mode (coordinatorMode.ts): the main
    agent should *understand* a complex task itself, then delegate
    self-contained subtasks to sub-agents (via the ``agent`` / ``swarm``
    tools) with explicit, actionable instructions -- rather than blindly
    forwarding the user's words and saying "based on your findings".

    This is guidance, not enforcement: the model retains full tool access
    but is steered toward good delegation hygiene on large multi-step work.
    """
    return _load_runtime_text("delegation_guidance") or (
        "=== Delegation Guidance (coordinator mode) ===\n"
        "For complex, multi-step, or parallelisable work, DELEGATE to sub-agents "
        "via the `agent` or `swarm` tools instead of doing everything yourself.\n"
        "When delegating:\n"
        "1. UNDERSTAND the task yourself first -- read the relevant code, grasp "
        "the goal -- so you can write a precise brief.\n"
        "2. Give each sub-agent a SELF-CONTAINED instruction: full context, exact "
        "file paths, and the expected deliverable. Do NOT just forward the user's "
        "words or say 'based on your findings'.\n"
        "3. Run independent sub-tasks in PARALLEL (pass a `tasks` array to the "
        "agent tool).\n"
        "4. After results return, SYNTHESISE them yourself: verify, resolve "
        "conflicts, decide the next step. You own the outcome.\n"
        "5. Cite file:line in your final answer. Do not restate sub-agent output "
        "verbatim -- extract what matters.\n"
        "=== End Delegation Guidance ==="
    )


# ── 11. /steer Injection ──────────────────────────────────────────────


class SteerQueue:
    """Queue for user mid-conversation instruction injections.

    Users can inject instructions via /steer commands while the agent
    is running. These are queued and drained before each API call.

    Mirrors Hermes agent's /steer mechanism.

    Also carries a ``system_messages`` queue: mid-conversation *system*
    instructions (stage transitions, stuck-recovery nudges, dynamic
    policy changes) that should reach the model as ``role: system`` rather
    than ``role: user``.  Unlike the system prompt prefix -- which is
    rewritten each turn -- these are appended as discrete system messages
    so the model treats them as authoritative directives layered on top of
    its standing instructions, and so the base system prompt stays
    cache-stable.
    """

    def __init__(self) -> None:
        self._queue: list[str] = []
        self._system_messages: list[str] = []
        self._max = MAX_STEER_MESSAGES

    def push(self, message: str) -> None:
        """Add a steer message to the queue."""
        if len(self._queue) < self._max:
            self._queue.append(message)
            logger.info("[steer] queued instruction: %s", message[:80])

    def push_system(self, message: str) -> None:
        """Queue a mid-conversation *system* instruction.

        System messages are injected as ``role: system`` entries (after the
        base system prompt, before the user/assistant history) rather than
        rewritten into the prefix.  Used for stage transitions, stuck
        recovery, and dynamic directives that should not perturb the cached
        base prompt.
        """
        if not message or not message.strip():
            return
        if len(self._system_messages) < self._max:
            self._system_messages.append(message)
            logger.info("[steer] queued system instruction: %s", message[:80])

    def drain(self) -> list[str]:
        """Drain and return all queued steer messages."""
        if not self._queue:
            return []
        items = list(self._queue)
        self._queue.clear()
        return items

    def drain_system(self) -> list[str]:
        """Drain and return all queued system messages."""
        if not self._system_messages:
            return []
        items = list(self._system_messages)
        self._system_messages.clear()
        return items

    @property
    def has_pending(self) -> bool:
        return bool(self._queue)

    @property
    def has_pending_system(self) -> bool:
        return bool(self._system_messages)


def build_steer_injection(steer_messages: list[str]) -> str:
    """Build a user message from queued steer instructions."""
    if not steer_messages:
        return ""
    if len(steer_messages) == 1:
        return _load_runtime_text("steer_single", message=steer_messages[0]) or f"[User instruction mid-conversation]\n{steer_messages[0]}"
    parts = "\n".join(f"- {m}" for m in steer_messages)
    return _load_runtime_text("steer_multi", items=parts) or f"[User instructions mid-conversation]\n{parts}"


# ── 12. Standing Orders Reminder ──────────────────────────────────────
# A per-turn reminder of the binding pre-action governance constraints
# (recall memory first, clarify ambiguity, checkpoint long chains, disclose
# assumptions, honour authority precedence).  Merged into the LAST user
# message on fresh user turns so the model re-arms its constraints right
# before it starts responding -- mirroring the existing guidance-merge
# pattern used for evolution feedback and advisor notes.


def build_live_register_text(register: str, prefix: str = "") -> str:
    """Render the per-turn ``Live Register`` declaration.

    The system prompt deliberately carries ALL register sections so its prefix
    never changes shape mid-session (provider prompt cache survives).  This
    short block is what makes that safe to read: it names the register
    governing the current turn and lists the sections that are inert right
    now.  The context compiler content-hashes it, so a stable register costs
    one message for the whole session and only a transition adds another.

    ``prefix`` is the built system prompt.  When supplied, the inert list is
    filtered to sections actually present in it: the activation table is
    directory-wide, while the prefix carries exactly one specialty, one
    tool-execution variant (full or slim), and no skill catalogue at all -- and
    naming an absent section as inert would be its own small lie.

    Returns an empty string when the prompt file cannot be loaded so the loop
    degrades to the pre-register behaviour instead of failing.
    """
    try:
        from encre.prompts.system import REGISTER_TITLES, inactive_section_titles

        resolved = register if register in REGISTER_TITLES else "work"
        inert = inactive_section_titles(resolved)
        if prefix:
            inert = [title for title in inert if f"## {title}" in prefix]
        lines = [f"- {title}" for title in inert] or ["- (none)"]
        return _get_runtime_loader().load_with_context(
            "live_register",
            category="runtime",
            register=resolved,
            title=REGISTER_TITLES[resolved],
            inactive="\n".join(lines),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[register] live declaration load failed: %s", exc)
        return ""


def build_standing_orders_reminder() -> str:
    """Build the standing-orders reminder injected on fresh user turns.

    The reminder text lives in ``prompts/reminders/standing_orders_reminder.prompt``
    (category ``reminders``) so prompt copy is never hardcoded in Python.
    Returns an empty string when the file cannot be loaded so the loop
    degrades gracefully.
    """
    try:
        return _get_runtime_loader().load("standing_orders_reminder", category="reminders")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[standing_orders] reminder load failed: %s", exc)
        return ""


def append_to_last_user_message(messages: list[dict[str, Any]], suffix: str) -> None:
    """Append *suffix* to the last ``user`` message in *messages* in place.

    Uses a shallow copy of the target dict so the original session messages
    are not mutated (mirrors ``_merge_into_last_user`` in loop.py).  No-op
    when no user message exists or *suffix* is empty.
    """
    if not suffix or not messages:
        return
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            msg = dict(messages[i])
            existing = str(msg.get("content") or "")
            msg["content"] = existing + "\n\n" + suffix
            messages[i] = msg
            return


# ── 13. Checkpoint Hard-Gate ─────────────────────────────────────────
# THE single authority on when a long autonomous chain pauses.  The prompt
# copies of this rule (mandatory_constraints Gate 4, task_completion's
# "Boundary of Keep Going", standing-orders rule 3) are gone: a pause
# instruction stated in the system prompt competes with -- and historically
# beat -- the instruction to finish the job.  The model now hears about
# pausing only when this gate actually fires, via
# prompts/runtime/checkpoint_message.prompt.
#
# The gate is register-scoped.  ``chat`` and ``answer`` turns are not
# autonomous work chains at all, so no threshold applies; ``work`` gets a
# budget high enough to finish a real job (reading a project, wiring a change,
# verifying it) in one pass instead of being truncated at step five.

#: Consecutive tool-call steps allowed in the ``work`` register before the loop
#: forces a checkpoint.  Kept as a module-level constant because tests and
#: diagnostics probe it; ``checkpoint_threshold_for`` is what the loop uses.
CHECKPOINT_TOOL_STEP_THRESHOLD = 12

#: Per-register pause budget.  ``0`` means the gate is disabled for that
#: register -- no pause message is ever injected on chat or answer turns.
_CHECKPOINT_THRESHOLDS: dict[str, int] = {
    "chat": 0,
    "answer": 0,
    "work": CHECKPOINT_TOOL_STEP_THRESHOLD,
}


def checkpoint_threshold_for(register: str) -> int:
    """Return the pause budget for *register*; ``0`` disables the gate.

    An unknown register falls back to the ``work`` budget: pausing too rarely
    is recoverable, but treating an unclassified turn as "never pause" would
    let an unattended chain run without a checkpoint.
    """
    return _CHECKPOINT_THRESHOLDS.get(register or "work", CHECKPOINT_TOOL_STEP_THRESHOLD)


def build_checkpoint_message(steps: int) -> str:
    """Build the checkpoint user message handed to the model.

    Text lives in ``prompts/runtime/checkpoint_message.prompt`` (category
    ``runtime``) so prompt copy is never hardcoded in Python.  ``{{steps}}``
    is substituted with the actual step count.  Returns an empty string when
    the file cannot be loaded so the loop degrades gracefully.
    """
    try:
        return _get_runtime_loader().load_with_context(
            "checkpoint_message",
            category="runtime",
            steps=str(steps),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[checkpoint] message load failed: %s", exc)
        return ""


def _checkpoint_authorization_patterns() -> list[str]:
    """Return the hands-off authorization phrases from the runtime prompt file."""
    meta = _load_runtime_meta("autonomous_authorization_patterns")
    patterns = meta.get("patterns")
    if isinstance(patterns, list):
        return [str(p).lower() for p in patterns if str(p).strip()]
    return []


def recent_user_texts(messages: list[dict[str, Any]], limit: int = 4) -> list[str]:
    """Return the most recent genuine user texts in *messages* (newest last).

    Used by :func:`checkpoint_gate_relaxed` so an authorisation granted a turn
    ago still counts.  Synthetic injections are filtered out -- the
    ``[CONTEXT UPDATE]`` delta entries, the checkpoint message itself, and the
    steering blocks -- because those are runtime chatter, not user speech.
    """
    texts: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = str(msg.get("content") or "").strip()
        if not content or content.startswith(
            ("[CONTEXT UPDATE]", "[User instruction", "[User instructions", "## Checkpoint", "=== Standing Orders")
        ):
            continue
        if "Checkpoint — Pause and Hand Back" in content or "Checkpoint \u2014 Pause" in content:
            continue
        texts.append(content)
        if len(texts) >= limit:
            break
    texts.reverse()
    return texts


def checkpoint_gate_relaxed(
    latest_user_prompt: str,
    recent_user_prompts: list[str] | None = None,
) -> bool:
    """Return True when the user has explicitly authorised a hands-off run.

    Matches the user's instruction(s) (lowercased) against the phrases in
    ``prompts/runtime/autonomous_authorization_patterns.prompt``.  A match means
    the user asked the agent to work unattended, so pausing mid-chain would
    fight their explicit intent.

    ``recent_user_prompts`` is scanned as well as the current message: an
    authorisation given one turn earlier ("just run it all yourself, I'm going
    out") is still standing for the task it was given for, and requiring the
    user to re-authorise every turn made the gate effectively permanent.
    """
    texts = [latest_user_prompt or ""]
    texts.extend(recent_user_prompts or [])
    combined = "\n".join(texts).lower()
    if not combined.strip():
        return False
    return any(pattern in combined for pattern in _checkpoint_authorization_patterns())


def count_consecutive_tool_steps(messages: list[dict[str, Any]]) -> int:
    """Count consecutive assistant tool-calling turns since the last user message.

    Walks *messages* backwards from the end, counting assistant messages that
    carried ``tool_calls``, stopping at the first ``user`` message (the most
    recent user turn resets the run).  Tool-result entries and text-only
    assistant messages are skipped -- only turns that actually invoked tools
    count as steps.  This is the code-side mirror of the prompt-level
    checkpoint gate.
    """
    steps = 0
    for msg in reversed(messages):
        role = msg.get("role")
        if role == "user":
            break
        if role == "assistant" and msg.get("tool_calls"):
            steps += 1
    return steps
