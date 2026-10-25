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

"""Automatic safety classification for tool-permission decisions.

This module backs the auto-permission ("YOLO") mode where the agent decides
on the user's behalf whether to approve a tool call.  The classifier layers
three tiers of judgment, cheapest first:

1. A static regex pre-filter (``_pattern_classify``) that uses the existing
   bash-command analyzer and a list of dangerous paths to short-circuit the
   clearly-safe and clearly-dangerous cases without spending an LLM call.
2. An LLM call to a fast, cheap model that returns a JSON verdict with a
   risk level, confidence score, and reasoning string.  The prompt is chosen
   based on whether conversation context is available:
   ``classify`` uses the isolated prompt, ``classify_with_context`` judges the
   call against the recent conversation so an expected step (the user asked
   to fix a bug, the agent edits source files) is not flagged as suspicious.
3. A cache keyed on the tool name plus normalised argument features so
   repeat calls (the same ``grep`` on the same file, the same ``bash`` command)
   do not re-invoke the LLM.  The key deliberately hashes the *full* normalised
   command rather than its first word so a cached "rm" verdict cannot be
   replayed for ``rm -rf ~/x`` (cache-poisoning defense).

The classifier also records the user's own approval decisions and feeds a
rolling approval-rate summary back into the LLM prompt so a pattern of
"this user always approves ``bash ls``" can nudge the verdict toward
auto-approve.  When the LLM backend is unreachable the classifier fails
closed: only provably-safe static verdicts are returned, everything else
keeps the ``ASK_USER`` sentinel with confidence 0 so the native safety
engine takes over and prefers denial.
"""

import json
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from encre.backend import create_backend
from encre.backends.base import BaseBackend
from encre.prompts.loader import PromptLoader
from encre.utils.types import BackendText


class AutoDecision(Enum):
    """The five-tier verdict emitted by the classifier.

    ``SAFE`` and ``LOW_RISK`` are auto-approved (LOW_RISK is also logged so
    the user can review after the fact); ``ASK_USER`` defers to a human;
    ``HIGH_RISK`` is auto-denied; ``BLOCK`` is auto-blocked as clearly
    malicious.  The tiering mirrors the static engine's ``DangerLevel`` so
    the two paths can share the same downstream handler.
    """

    SAFE = auto()         # Auto-approve -- clearly safe
    LOW_RISK = auto()     # Auto-approve but log -- probably safe
    ASK_USER = auto()     # Needs human judgment -- ambiguous
    HIGH_RISK = auto()    # Auto-deny -- likely dangerous
    BLOCK = auto()        # Auto-block -- clearly malicious


@dataclass
class ClassificationResult:
    """Outcome of one classification pass.

    ``decision`` is the tiered verdict, ``confidence`` is a 0.0-1.0 score
    the caller can threshold against, ``reasoning`` is a human-readable
    explanation surfaced in the UI, and ``latency_ms`` records how long the
    LLM call took (0.0 for static / cached verdicts).  ``tool_name`` and
    ``tool_args`` are echoed back so downstream code does not need to
    re-pass them.
    """

    decision: AutoDecision
    confidence: float  # 0.0 - 1.0
    reasoning: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0


@dataclass
class UserDecisionRecord:
    """Records user permission decisions for learning.

    Only the *hashed/key features* of the tool args are stored (never the
    full args, which may contain secrets), so the record is safe to keep in
    memory indefinitely.  The record feeds :meth:`get_user_pattern`, which
    computes the rolling approval rate for a tool.
    """

    tool_name: str
    tool_args_summary: str  # hashed/key features, not full args
    user_approved: bool
    timestamp: float = field(default_factory=time.time)


_loader = PromptLoader()


class EncreAutoSafetyClassifier:
    """Lightweight ML-based safety classifier for auto permission mode.

    Uses a fast/cheap model to evaluate whether a tool action is safe.
    Maintains a learning cache of user decisions to improve over time.

    The classifier is intentionally not stateful across process restarts:
    the cache, user decisions, and stats are all in-memory so a crashed
    classifier cannot leak stale approvals.  The LLM backend is created
    lazily on first use so importing this module never constructs a client
    for a model the user has not configured.

    Usage:
        classifier = EncreAutoSafetyClassifier(config)
        result = await classifier.classify("bash", {"command": "ls -la"})
        if result.decision in (AutoDecision.SAFE, AutoDecision.LOW_RISK):
            # auto-approve
    """

    SYSTEM_PROMPT = _loader.load("safety_classifier", category="autosafety")
    # Intent-aware prompt used by ``classify_with_context``: it judges whether
    # a tool call is consistent with what the user actually asked for.
    CONTEXT_SYSTEM_PROMPT = _loader.load("safety_context_eval", category="autosafety")

    def __init__(
        self,
        backend_type: str = "openai",
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        cache_size: int = 1000,
        confidence_threshold: float = 0.7,
    ) -> None:
        self._backend_type = backend_type
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._confidence_threshold = confidence_threshold
        self._cache: dict[str, ClassificationResult] = {}
        self._cache_size = cache_size
        self._user_decisions: list[UserDecisionRecord] = []
        self._backend: BaseBackend | None = None
        self._total_classifications = 0
        self._cache_hits = 0

    def _get_backend(self) -> BaseBackend:
        """Return the lazily-constructed classification backend.

        The model is picked per backend type so the classifier always uses
        the cheapest tier available: Sonnet on Anthropic, a Luna model on
        OpenAI.  The backend is shared for the lifetime of the classifier
        (it holds an httpx client that must not be reconstructed per call).
        """
        if self._backend is None:
            # Use cheapest model by default
            if not self._model:
                if self._backend_type == "anthropic":
                    self._model = "claude-sonnet-5"
                elif self._backend_type == "openai":
                    self._model = "gpt-5.6-luna"
                else:
                    self._model = "default"
            self._backend = create_backend(
                self._backend_type,
                api_key=self._api_key,
                base_url=self._base_url,
                model=self._model,
            )
        return self._backend

    def _make_cache_key(self, tool_name: str, tool_args: dict[str, Any]) -> str:
        """Create a stable cache key from tool name + key arg patterns.

        The key encodes only *features* that survive between calls of the
        same operation: for ``bash`` the base command plus a SHA-256 of the
        full normalised command (whitespace-collapsed), for ``url`` the
        parsed domain, for ``path`` the file extension.  Verbatim string
        values are truncated to 40 characters so a key that would never be
        reused does not bloat the cache.  Non-string args contribute only
        their type name — the actual value is irrelevant for safety because
        e.g. the ``count`` argument of ``file_edit`` does not change the
        danger of editing a file.

        The SHA-256 of the full normalised command is deliberate: a
        first-word-only key would let a cached "rm" verdict (from
        ``rm unused.txt``) be replayed for ``rm -rf ~/x`` — a cache-poisoning
        vulnerability.  Collapsing whitespace before hashing means
        ``ls -la`` and ``ls   -la`` hash identically, so cosmetic whitespace
        variations still hit the cache.
        """
        # Extract key features, not full args (which vary per call)
        features: list[str] = [tool_name]
        for key in sorted(tool_args.keys()):
            val = tool_args[key]
            if isinstance(val, str):
                if key in ("command",):
                    import hashlib
                    normalized = " ".join(val.split())
                    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                    base_cmd = normalized.split()[0] if normalized else val[:50]
                    features.append(f"{key}={base_cmd}:sha256:{digest}")
                elif key in ("url",):
                    from urllib.parse import urlparse
                    try:
                        parsed = urlparse(val)
                        features.append(f"url_domain={parsed.netloc}")
                    except Exception:
                        features.append(f"url={val[:60]}")
                elif key in ("path", "file_path"):
                    import os
                    features.append(f"ext={os.path.splitext(val)[1]}")
                else:
                    features.append(f"{key}={str(val)[:40]}")
            else:
                features.append(f"{key}={type(val).__name__}")
        return "|".join(features)

    def learn_from_user(self, tool_name: str, tool_args: dict[str, Any], approved: bool) -> None:
        """Record a user permission decision for future learning.

        The record is appended to a rolling window capped at ``_cache_size``
        (the most recent ``N`` decisions are kept).  Only the hashed feature
        summary of the args is stored, never the raw args — so this is safe
        to call for every approval without leaking secrets into memory.
        """
        cache_key = self._make_cache_key(tool_name, tool_args)
        self._user_decisions.append(UserDecisionRecord(
            tool_name=tool_name,
            tool_args_summary=cache_key,
            user_approved=approved,
        ))
        # Keep only recent decisions
        if len(self._user_decisions) > self._cache_size:
            self._user_decisions = self._user_decisions[-self._cache_size:]

    def get_user_pattern(self, tool_name: str) -> dict[str, Any] | None:
        """Get user's historical approval pattern for a tool.

        Returns ``None`` when the user has never decided on this tool, so
        the caller knows to omit the user-history line from the LLM prompt.
        Otherwise returns a dict with the total count, the approved/denied
        split, and the approval rate (0.0-1.0) which is interpolated into
        the eval prompt as "approved X% of N similar calls".
        """
        relevant = [d for d in self._user_decisions if d.tool_name == tool_name]
        if not relevant:
            return None
        approved = sum(1 for d in relevant if d.user_approved)
        return {
            "total": len(relevant),
            "approved": approved,
            "denied": len(relevant) - approved,
            "approval_rate": approved / len(relevant) if relevant else 0,
        }

    async def classify(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        tool: Any = None,
    ) -> ClassificationResult:
        """Classify a tool action's safety level.

        Judged in isolation (no conversation context).  Used by the
        interactive permission path where the user is about to be asked;
        the classifier can either pre-decide the answer or hand back
        ``ASK_USER`` for the human to rule.  The full three-tier pipeline
        runs in order: static pre-filter, cache, LLM.  Each tier short-
        circuits the rest on success so a clear-cut verdict never costs an
        LLM call.
        """
        self._total_classifications += 1

        # Fast path: pattern-based pre-filtering.  ``>= 0.95`` so a static
        # HIGH_RISK verdict (confidence exactly 0.95) bypasses the LLM and
        # the cache instead of being re-classified and cached.
        pattern_result = self._pattern_classify(tool_name, tool_args)
        if pattern_result.confidence >= 0.95:
            self._cache_hits += 1
            return pattern_result

        # Cache lookup
        cache_key = self._make_cache_key(tool_name, tool_args)
        if cache_key in self._cache:
            self._cache_hits += 1
            return self._cache[cache_key]

        # Check user history
        user_pattern = self.get_user_pattern(tool_name)

        # For clearly safe tools, skip LLM call
        if (tool is not None and tool.is_readonly(tool_args)) or tool_name in (
            "file_read", "grep", "glob", "web_search", "device",
        ):
            result = ClassificationResult(
                decision=AutoDecision.SAFE,
                confidence=0.99,
                reasoning="Read-only tool, always safe",
                tool_name=tool_name,
                tool_args=tool_args,
            )
            self._cache_result(cache_key, result)
            return result

        # For clearly dangerous patterns, skip LLM call
        if pattern_result.decision == AutoDecision.BLOCK:
            return pattern_result

        # LLM-based classification
        start = time.time()
        try:
            backend = self._get_backend()
            eval_prompt = self._build_eval_prompt(tool_name, tool_args, user_pattern)
            messages = [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": eval_prompt},
            ]

            full_response = ""
            async for event in backend.chat(
                messages=messages, max_tokens=512, temperature=0.0, enable_caching=False,
            ):
                if isinstance(event, BackendText):
                    full_response += event.text

            llm_decision = self._parse_response(full_response)
            llm_decision.tool_name = tool_name
            llm_decision.tool_args = tool_args
            llm_decision.latency_ms = (time.time() - start) * 1000

            self._cache_result(cache_key, llm_decision)
            return llm_decision

        except Exception:
            # Fail closed: with the classifier unreachable, fall back to the
            # static engine verdict.  Only provably-safe static verdicts are
            # returned; anything else keeps the ASK_USER sentinel
            # (confidence 0.0) so auto mode can run the native engine check
            # and prefer denial instead of silently allowing.
            try:
                static = self._pattern_classify(tool_name, tool_args)
                if static.confidence >= 0.95:
                    return static
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
            return ClassificationResult(
                decision=AutoDecision.ASK_USER,
                confidence=0.0,
                reasoning="Classifier unavailable -- defaulting to ask user",
                tool_name=tool_name,
                tool_args=tool_args,
            )

    async def classify_with_context(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        conversation_context: str,
        tool: Any = None,
    ) -> ClassificationResult:
        """Classify a tool action against the user's intent.

        Unlike :meth:`classify`, which judges a call in isolation, this variant
        also reviews the recent conversation so the model can tell an expected
        step ("user asked to fix the bug" -> editing source files) from an
        unrelated or destructive one.  It backs the auto approval ("YOLO")
        path, where the AI decides on the agent's behalf instead of the user.

        The conversation context is truncated to the last 4000 characters
        (enough to capture the current task without blowing up the prompt)
        and the tool args are truncated to 2000 characters.  Truncation is
        applied *after* the cache lookup so a repeat call still hits the
        cache even if its args are huge.
        """
        self._total_classifications += 1

        # Fast path: the static pre-filter always wins for clear-cut cases.
        # ``>= 0.95`` mirrors :meth:`classify` so static HIGH_RISK verdicts
        # bypass the LLM and the cache.
        pattern_result = self._pattern_classify(tool_name, tool_args)
        if pattern_result.confidence >= 0.95:
            self._cache_hits += 1
            return pattern_result

        # Cache lookup: the verdict only depends on the tool call, not on the
        # surrounding conversation, so cached decisions stay valid.
        cache_key = self._make_cache_key(tool_name, tool_args)
        if cache_key in self._cache:
            self._cache_hits += 1
            return self._cache[cache_key]

        # Read-only tools never need the model.
        if (tool is not None and tool.is_readonly(tool_args)) or tool_name in (
            "file_read", "grep", "glob", "web_search", "device",
        ):
            result = ClassificationResult(
                decision=AutoDecision.SAFE,
                confidence=0.99,
                reasoning="Read-only tool, always safe",
                tool_name=tool_name,
                tool_args=tool_args,
            )
            self._cache_result(cache_key, result)
            return result

        # Clearly malicious patterns never reach the model.
        if pattern_result.decision == AutoDecision.BLOCK:
            return pattern_result

        args_str = json.dumps(tool_args or {}, indent=2, ensure_ascii=False)
        if len(args_str) > 2000:
            args_str = args_str[:2000] + "\n... truncated"
        context_str = (conversation_context or "").strip()
        if len(context_str) > 4000:
            context_str = context_str[-4000:]

        user_prompt = (
            "## Recent conversation\n"
            f"{context_str}\n\n"
            "## Tool call awaiting authorization\n"
            f"Tool: {tool_name}\n"
            f"Arguments: {args_str}\n\n"
            "Is this action consistent with the user's intent? Reply with JSON only."
        )

        start = time.time()
        try:
            backend = self._get_backend()
            messages = [
                {"role": "system", "content": self.CONTEXT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]

            full_response = ""
            async for event in backend.chat(
                messages=messages, max_tokens=512, temperature=0.0, enable_caching=False,
            ):
                if isinstance(event, BackendText):
                    full_response += event.text

            verdict = self._parse_response(full_response)
            verdict.tool_name = tool_name
            verdict.tool_args = tool_args
            verdict.latency_ms = (time.time() - start) * 1000

            self._cache_result(cache_key, verdict)
            return verdict

        except Exception:
            # Fail closed: an unreachable classifier must never silently
            # grant access.  Fall back to the static engine verdict; only
            # provably-safe static results are returned, anything else keeps
            # the ASK_USER sentinel (confidence 0.0) so the auto-approval
            # path can deny instead of allow.
            try:
                static = self._pattern_classify(tool_name, tool_args)
                if static.confidence >= 0.95:
                    return static
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
            return ClassificationResult(
                decision=AutoDecision.ASK_USER,
                confidence=0.0,
                reasoning="Context classifier unavailable -- defaulting to ask user",
                tool_name=tool_name,
                tool_args=tool_args,
            )

    def _pattern_classify(
        self, tool_name: str, tool_args: dict[str, Any]
    ) -> ClassificationResult:
        """Fast regex-based pre-classification for clear cases.

        Bash commands are delegated to :func:`analyze_bash_command` (the
        shared static analyzer used by the safety engine) so the two paths
        cannot drift apart.  File operations are checked against a list of
        system paths (``/etc/`, ``/boot/``, ``~/.ssh/``, ``C:\\Windows``)
        and a list of sensitive filenames (``.env``, ``.pem``, ``id_rsa``,
        ``api_key``) — the former block outright, the latter escalate to
        ASK_USER so the user can confirm they really meant to touch a
        credential file.  Everything else returns ASK_USER at 0.5
        confidence, which the caller will rou

from encre.logging_config import get_logger


logger = get_logger(__name__)
te to the LLM.
        """
        from encre.safety import DangerLevel, analyze_bash_command

        if tool_name == "bash":
            command = tool_args.get("command", "")
            if not command:
                return ClassificationResult(
                    decision=AutoDecision.SAFE, confidence=1.0,
                    reasoning="Empty command", tool_name=tool_name, tool_args=tool_args,
                )

            analysis = analyze_bash_command(command)

            if analysis.danger_level == DangerLevel.CRITICAL:
                return ClassificationResult(
                    decision=AutoDecision.BLOCK, confidence=1.0,
                    reasoning=f"Critical danger: {'; '.join(analysis.injection_details)}",
                    tool_name=tool_name, tool_args=tool_args,
                )

            if analysis.danger_level == DangerLevel.HIGH:
                return ClassificationResult(
                    decision=AutoDecision.HIGH_RISK, confidence=0.95,
                    reasoning=f"High risk: {'; '.join(analysis.injection_details)}",
                    tool_name=tool_name, tool_args=tool_args,
                )

            if analysis.danger_level == DangerLevel.SAFE:
                return ClassificationResult(
                    decision=AutoDecision.SAFE, confidence=0.98,
                    reasoning="Static analysis: safe command",
                    tool_name=tool_name, tool_args=tool_args,
                )

            # MEDIUM/LOW -> need LLM evaluation
            return ClassificationResult(
                decision=AutoDecision.ASK_USER, confidence=0.5,
                reasoning="Medium risk -- needs LLM evaluation",
                tool_name=tool_name, tool_args=tool_args,
            )

        # File operations
        if tool_name in ("file_write", "file_edit"):
            path = tool_args.get("path", tool_args.get("file_path", ""))
            dangerous_paths = ["/etc/", "/boot/", "/sys/", "/proc/", "~/.ssh/",
                              "/root/", "C:\\Windows\\", "C:\\Windows\\System32\\"]
            for dp in dangerous_paths:
                if dp.lower() in path.lower():
                    return ClassificationResult(
                        decision=AutoDecision.BLOCK, confidence=1.0,
                        reasoning=f"Dangerous path: {path}", tool_name=tool_name,
                        tool_args=tool_args,
                    )

            # Check for .env / credential files
            sensitive_files = [".env", "credentials", "secret", ".pem", "id_rsa",
                              "password", "token", "api_key"]
            path_lower = path.lower()
            for sf in sensitive_files:
                if sf in path_lower:
                    return ClassificationResult(
                        decision=AutoDecision.ASK_USER, confidence=0.8,
                        reasoning=f"Potentially sensitive file: {path}",
                        tool_name=tool_name, tool_args=tool_args,
                    )

            return ClassificationResult(
                decision=AutoDecision.LOW_RISK, confidence=0.85,
                reasoning=f"File write to project path: {path}",
                tool_name=tool_name, tool_args=tool_args,
            )

        return ClassificationResult(
            decision=AutoDecision.ASK_USER, confidence=0.5,
            reasoning="Unknown tool -- needs evaluation",
            tool_name=tool_name, tool_args=tool_args,
        )

    def _build_eval_prompt(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        user_pattern: dict[str, Any] | None,
    ) -> str:
        """Render the eval prompt for the isolated (no-context) classifier.

        The tool args are JSON-serialised and truncated to 2000 chars so a
        huge ``file_edit`` payload does not blow up the prompt.  The user
        pattern (rolling approval rate) is interpolated as a single line so
        the model can weigh the user's history against the risk signal.
        """
        args_str = json.dumps(tool_args, indent=2, ensure_ascii=False)
        if len(args_str) > 2000:
            args_str = args_str[:2000] + "\n... truncated"
        user_pattern_str = ""
        if user_pattern:
            total = user_pattern["total"]
            rate = user_pattern["approval_rate"]
            user_pattern_str = f"User history: approved {rate:.0%} of {total} similar {tool_name} calls."
        return _loader.load_with_context(
            "safety_eval", category="autosafety",
            tool_name=tool_name,
            args_str=args_str,
            user_pattern=user_pattern_str,
        )

    def _parse_response(self, response: str) -> ClassificationResult:
        """Parse the model's JSON verdict into a :class:`ClassificationResult`.

        The model is prompted to reply with JSON only, but models sometimes
        wrap the JSON in prose or markdown fences.  The parser therefore
        looks for the first ``{`` and last ``}`` rather than parsing the
        whole response, and falls back to ASK_USER on any parse failure
        (fail closed).  The ``risk_level`` field maps to the verdict tiers:
        ``critical`` -> BLOCK, ``high`` -> HIGH_RISK, ``medium`` -> ASK_USER
        unless confidence clears the threshold (then LOW_RISK), ``low`` ->
        LOW_RISK, anything else -> SAFE.
        """
        try:
            json_start = response.find("{")
            json_end = response.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                data = json.loads(response[json_start:json_end])
            else:
                return ClassificationResult(
                    decision=AutoDecision.ASK_USER, confidence=0.0,
                    reasoning="Could not parse classifier response"
                )

            data.get("safe", False)
            risk = data.get("risk_level", "medium")
            confidence = float(data.get("confidence", 0.5))
            reasoning = data.get("reasoning", "")

            if risk == "critical":
                decision = AutoDecision.BLOCK
            elif risk == "high":
                decision = AutoDecision.HIGH_RISK
            elif risk == "medium":
                decision = AutoDecision.ASK_USER if confidence < self._confidence_threshold else AutoDecision.LOW_RISK
            elif risk == "low":
                decision = AutoDecision.LOW_RISK
            else:
                decision = AutoDecision.SAFE

            return ClassificationResult(
                decision=decision,
                confidence=confidence,
                reasoning=reasoning,
            )
        except Exception:
            return ClassificationResult(
                decision=AutoDecision.ASK_USER, confidence=0.0,
                reasoning="Parse error -- defaulting to ask",
            )

    def _cache_result(self, key: str, result: ClassificationResult) -> None:
        """Insert *result* under *key*, evicting the oldest entry when full.

        The cache is a plain ``dict`` used as an LRU by insertion order:
        ``next(iter(...))`` is the oldest key.  The cap is enforced lazily
        on insert rather than with a scheduled cleanup so the cache never
        grows unbounded even if no cleanup runs.
        """
        if len(self._cache) >= self._cache_size:
            # Remove oldest entry
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        self._cache[key] = result

    @property
    def stats(self) -> dict[str, Any]:
        """Return the classifier's runtime statistics.

        Used by the telemetry and settings UI to surface how much work the
        classifier is doing.  ``cache_hit_rate`` is the fraction of calls
        answered without an LLM round trip; a high rate means the user's
        workload is repetitive enough that the cache pays for itself.
        """
        return {
            "total_classifications": self._total_classifications,
            "cache_hits": self._cache_hits,
            "cache_size": len(self._cache),
            "cache_hit_rate": self._cache_hits / max(self._total_classifications, 1),
            "user_decisions_recorded": len(self._user_decisions),
        }
