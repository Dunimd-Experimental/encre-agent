#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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

"""Significance judgment: the single entry point into memory.

Core principle (project-wide, non-negotiable): **context != memory**.

* Context is the temporary working set inside the reasoning window; it is
  managed by compaction and lives exactly as long as the current inference
  process.
* Memory is persistent cognition that the agent keeps **only after it has
  judged, on its own, that something is meaningful to itself**.  It survives
  across sessions.
* Compaction is therefore *never* allowed to write memory.  Memory is written
  only through significance judgment.

This module implements that judgment.  :class:`SignificanceJudge` evaluates
each :class:`Experience` produced during execution and, when the aggregate
significance clears a configurable threshold, emits a
:class:`MemoryCandidate`.  Anything below the threshold is dropped: no
candidate, no event, nothing is written.

The scoring core is a pure, offline, rule-based function: no network and no
LLM are required for it to work.  Any injected dependency (the memory system)
is defensive -- a missing or failing dependency degrades the relevant signal
to inactive rather than raising.

Signals (each weighted, all overridable via ``weights``):

``user_correction`` / ``user_emphasis``
    Explicit user correction or emphasis, recognised from the feedback
    learner or from correction patterns in a user message.
``repeated_pattern``
    A pattern seen repeatedly within the session (token-overlap matched).
``error_or_surprise``
    Tool failures, exceptions, or results that contradict expectations.
``key_decision``
    A decision that advances the goal (commitments, choices, resolutions).
``strong_association``
    Strong association with existing memory, measured through the injected
    memory system's local retrieval.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from encre.logging_config import get_logger

logger = get_logger(__name__)

__all__ = [
    "Experience",
    "MemoryCandidate",
    "SignificanceJudge",
]


# ----------------------------------------------------------------------
# Text helpers (local, dependency-free)
# ----------------------------------------------------------------------


def _fingerprint(text: str, size: int = 16) -> str:
    """Stable content fingerprint used for repetition detection."""
    normalised = " ".join((text or "").lower().split())
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:size]


def _canonical_content(text: str) -> str:
    """Collapse whitespace so candidates compare and store cleanly."""
    return " ".join((text or "").split())


# ----------------------------------------------------------------------
# Correction / decision / emphasis patterns
# ----------------------------------------------------------------------

#: Phrases that signal the user is correcting or constraining the agent.
_CORRECTION_PATTERNS: tuple[str, ...] = (
    r"\bdon'?t\b",
    r"\bdo not\b",
    r"\bnever\b",
    r"\binstead\b",
    r"\bactually\b",
    r"\bwrong\b",
    r"\bincorrect\b",
    r"\bmistake\b",
    r"\bnot what i\b",
    r"\bthat'?s not\b",
    r"^\s*no\b",
    r"\bplease (?:use|avoid|stop|remember)\b",
    r"\bmust (?:be|use|always|never)\b",
    r"\bshould (?:be|use|always|never)\b",
    r"\balways\b",
    r"\bremember\b",
    r"\bkeep in mind\b",
    r"\bnote that\b",
    r"不要", r"不对", r"不能用", r"必须", r"应该", r"记住", r"注意", r"改成",
    r"纠正", r"固定", r"统一", r"规定", r"默认(?:用|是|为)", r"以后都",
)

#: Phrases that signal an explicit emphasis (without a correction).
_EMPHASIS_PATTERNS: tuple[str, ...] = (
    r"\bimportant\b",
    r"\bcritical\b",
    r"\bessential\b",
    r"\bmandatory\b",
    r"\bfrom now on\b",
    r"\bplease note\b",
    r"重要", r"务必", r"一定要",
)

#: Phrases that signal a deliberate, goal-advancing decision.
_DECISION_PATTERNS: tuple[str, ...] = (
    r"\bdecided\b",
    r"\bdecision\b",
    r"\bcommit(?:ted|ting)?\b",
    r"\bresolved\b",
    r"\bconclusion\b",
    r"\bsolution\b",
    r"\bwe agreed\b",
    r"决定", r"方案", r"结论", r"采用", r"约定",
)

#: Weaker, higher-recall decision hints.  Alone they stay below threshold;
#: combined with another signal they raise the aggregate.
_DECISION_WEAK_PATTERNS: tuple[str, ...] = (
    r"\bwe(?:'ll| will)\b",
    r"\bi(?:'ll| will)\b",
    r"\blet'?s\b",
    r"\bgoing to\b",
    r"\bapproach\b",
    r"\bstrategy\b",
    r"\bplan\b",
    r"计划",
)

#: Phrases that signal a surprising / unexpected outcome.
_SURPRISE_PATTERNS: tuple[str, ...] = (
    r"\bunexpected\b",
    r"\bsurpris(?:e|ing)\b",
    r"\bcontradict\b",
    r"\bhowever\b",
    r"\bbut actually\b",
    r"\bturns out\b",
    r"\bdiscovered\b",
    r"意外", r"竟然", r"出乎意料", r"反而",
)

#: Metadata flags that mark an experience as an error/failure.
_ERROR_FLAGS: tuple[str, ...] = (
    "is_error", "error", "failed", "failure", "exception", "traceback",
)

#: Metadata flags that mark an experience as a user correction.
_CORRECTION_FLAGS: tuple[str, ...] = (
    "correction", "user_correction", "is_correction",
)

#: Metadata flags that mark an experience as a goal-advancing decision.
_DECISION_FLAGS: tuple[str, ...] = (
    "decision", "key_decision", "is_decision", "goal_met", "milestone",
)

#: Metadata flags that mark an experience as an explicit emphasis.
_EMPHASIS_FLAGS: tuple[str, ...] = (
    "emphasis", "user_emphasis", "is_emphasis",
)

#: Experience kinds whose content is intrinsically about a decision.
_DECISION_KINDS: frozenset[str] = frozenset(
    {"decision", "key_decision", "commitment", "plan", "goal"}
)

#: Experience kinds treated as conversational user input.
_USER_KINDS: frozenset[str] = frozenset({"user_message", "user", "correction"})

#: The five significance signals, in stable order.
SIGNALS: tuple[str, ...] = (
    "user_correction",
    "user_emphasis",
    "repeated_pattern",
    "error_or_surprise",
    "key_decision",
    "strong_association",
)

#: Default per-signal weights.  The aggregate is ``1 - prod(1 - w_i * s_i)``,
#: so a single strong signal is already sufficient to clear the default
#: threshold while weak, unrelated content never does.
DEFAULT_WEIGHTS: dict[str, float] = {
    "user_correction": 0.85,
    "user_emphasis": 0.65,
    "repeated_pattern": 0.75,
    "error_or_surprise": 0.65,
    "key_decision": 0.65,
    "strong_association": 0.60,
}

#: Content shorter than this is never a memory candidate.
_MIN_CONTENT_CHARS = 8

#: Content longer than this is truncated when forming a candidate.
_MAX_CANDIDATE_CHARS = 2000


@dataclass
class Experience:
    """One judged unit of experience produced during execution.

    Args:
        kind: Experience type (``"user_message"`` / ``"tool_result"`` /
            ``"error"`` / ``"turn_end"`` / ``"correction"`` ...).
        content: The raw experience text.
        session_id: Owning session.
        refs: Source references (event id / tool call id / message id).
        metadata: Optional context such as tool name, success flag, or
            repetition hints.
        ts: Wall-clock timestamp (defaults to ``time.time()``).
    """

    kind: str
    content: str
    session_id: str = ""
    refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        return {
            "kind": self.kind,
            "content": self.content,
            "session_id": self.session_id,
            "refs": list(self.refs),
            "metadata": dict(self.metadata),
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Experience":
        """Build an :class:`Experience` from a dict (see :meth:`to_dict`)."""
        return cls(
            kind=str(data.get("kind", "")),
            content=str(data.get("content", "")),
            session_id=str(data.get("session_id", "")),
            refs=[str(r) for r in (data.get("refs") or [])],
            metadata=dict(data.get("metadata", {}) or {}),
            ts=float(data.get("ts", 0.0)),
        )


@dataclass
class MemoryCandidate:
    """A judged experience that cleared the significance threshold.

    This is the *only* shape in which an experience may leave the judgment
    stage.  It is a proposal, not a stored memory: persistence is owned by
    the downstream consolidation stage.

    Args:
        content: Refined statement (not raw stream output).
        significance: Aggregate significance in ``[0.0, 1.0]``.
        reasons: The signals that fired, each with its contribution, e.g.
            ``"user_correction (w=0.85, s=1.00)"``.
        source_refs: Source references (event id / tool call id / message id).
        suggested_type: One of ``"episodic"`` / ``"semantic"`` /
            ``"procedural"`` / ``"autobiographical"``.
        session_id: Owning session.
        created_at: Wall-clock timestamp.
        metadata: Extra provenance (experience kind, judge version, ...).
    """

    content: str
    significance: float
    reasons: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    suggested_type: str = "episodic"
    session_id: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        return {
            "content": self.content,
            "significance": self.significance,
            "reasons": list(self.reasons),
            "source_refs": list(self.source_refs),
            "suggested_type": self.suggested_type,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryCandidate":
        """Build a :class:`MemoryCandidate` from a dict (see :meth:`to_dict`)."""
        return cls(
            content=str(data.get("content", "")),
            significance=float(data.get("significance", 0.0)),
            reasons=[str(r) for r in (data.get("reasons") or [])],
            source_refs=[str(r) for r in (data.get("source_refs") or [])],
            suggested_type=str(data.get("suggested_type", "episodic")),
            session_id=str(data.get("session_id", "")),
            created_at=float(data.get("created_at", 0.0)),
            metadata=dict(data.get("metadata", {}) or {}),
        )


class SignificanceJudge:
    """Decides which experiences are meaningful enough to become memory.

    The judge is the single gate into memory.  It is deliberately pure and
    offline by default: :meth:`judge` runs no network or LLM call, so it is
    usable from any context.  Dependencies (the memory system, an optional
    LLM assist) are injected, never reached for globally.

    Args:
        threshold: Minimum aggregate significance for a candidate.  Content
            below it yields ``None`` -- "not worth remembering".
        recall_engine: Optional object exposing ``recall(query, top_k)``
            (e.g. :class:`~encre.memdir.recall.RecallEngine`); used for the
            ``strong_association`` signal.  When absent the signal is simply
            inactive.
        memory_system: Optional file facade kept for callers that still hold
            one; it is not used for retrieval (the facade has none).
        weights: Optional per-signal weight overrides.  Unknown keys are
            ignored; known keys not supplied keep their default.
    """

    #: Signals recognised by :meth:`judge`.
    SIGNALS = SIGNALS

    #: Default weights (class-level so callers can read them).
    DEFAULT_WEIGHTS = DEFAULT_WEIGHTS

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        recall_engine: Any = None,
        memory_system: Any = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        self.threshold = float(threshold)
        self._recall_engine = recall_engine
        # Retained only so existing constructors keep working; retrieval no
        # longer flows through the file facade.
        self._memory_system = memory_system
        self._weights: dict[str, float] = dict(DEFAULT_WEIGHTS)
        if weights:
            for key, value in weights.items():
                if key in self._weights:
                    self._weights[key] = float(value)

        # Content fingerprint -> prior occurrences, per session.  Populated
        # by :meth:`record` (and automatically by :meth:`judge_batch` /
        # :meth:`attach`); :meth:`judge` only reads it, so a single call is
        # deterministic and side-effect free.
        self._session_fingerprints: dict[str, dict[str, int]] = {}
        # Subscribed buses, so attach() is idempotent per bus.
        self._attached: set[int] = set()
        # The bus candidates are published on when judging observed events.
        self._attached_bus: Any = None
        # Fire-and-forget judgment tasks, retained for cancellation/await in
        # tests and shutdown.
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def weights(self) -> dict[str, float]:
        """A copy of the effective per-signal weights."""
        return dict(self._weights)

    def set_threshold(self, threshold: float) -> None:
        """Update the significance threshold at runtime."""
        self.threshold = float(threshold)

    # ------------------------------------------------------------------
    # Core judgment
    # ------------------------------------------------------------------

    def judge(self, experience: Experience) -> MemoryCandidate | None:
        """Evaluate one experience and return a candidate, or ``None``.

        Returns ``None`` when the experience is empty, too short, or its
        aggregate significance falls below :attr:`threshold`.  A ``None``
        result means "do not remember": no candidate, no event, no write.

        The call is side-effect free: repetition is measured against the
        history accumulated by :meth:`record` (fed automatically by
        :meth:`judge_batch` and :meth:`attach`), so judging the same
        experience twice yields the same result.

        Args:
            experience: The experience to evaluate.

        Returns:
            A :class:`MemoryCandidate` when significant, else ``None``.
        """
        content = _canonical_content(experience.content)
        if len(content) < _MIN_CONTENT_CHARS:
            return None

        signals, detail = self._score(experience)
        significance = self._aggregate(signals)

        if significance < self.threshold:
            return None

        reasons = [
            f"{name} (w={self._weights.get(name, 0.0):.2f}, s={signals[name]:.2f})"
            for name in SIGNALS
            if signals.get(name, 0.0) > 0.0
        ]

        stored = content[:_MAX_CANDIDATE_CHARS]
        candidate = MemoryCandidate(
            content=stored,
            significance=round(significance, 6),
            reasons=reasons,
            source_refs=list(experience.refs),
            suggested_type=self._suggest_type(experience, signals),
            session_id=experience.session_id,
            metadata={
                "experience_kind": experience.kind,
                "signals": dict(signals),
                "detail": detail,
                "judge_version": "1.0",
            },
        )
        return candidate

    def judge_batch(self, experiences: Any) -> list[MemoryCandidate]:
        """Judge an iterable of experiences, dropping the ``None`` results.

        Each experience is recorded after it is judged, so a pattern that
        repeats within the batch (or across batches) raises its own
        ``repeated_pattern`` significance on the later occurrences.

        Args:
            experiences: Iterable of :class:`Experience`.

        Returns:
            The significant candidates, in input order.
        """
        out: list[MemoryCandidate] = []
        for experience in experiences:
            candidate = self.judge(experience)
            self.record(experience)
            if candidate is not None:
                out.append(candidate)
        return out

    def record(self, experience: Experience) -> None:
        """Record an experience's content for repetition detection.

        Repetition is inherently a property of history, not of a single
        experience, so the streaming entry points (:meth:`judge_batch` and
        :meth:`attach`) call this automatically.  Callers judging one
        experience at a time may call it explicitly.

        Args:
            experience: The experience to remember for overlap counting.
        """
        text = _canonical_content(experience.content)
        if len(text) < _MIN_CONTENT_CHARS:
            return
        per_session = self._session_fingerprints.setdefault(experience.session_id, {})
        fingerprint = _fingerprint(text)
        per_session[fingerprint] = per_session.get(fingerprint, 0) + 1

    # ------------------------------------------------------------------
    # Signal scoring
    # ------------------------------------------------------------------

    def _score(self, experience: Experience) -> tuple[dict[str, float], dict[str, str]]:
        """Return ``({signal: score}, {signal: explanation})`` for one experience."""
        text = experience.content or ""
        lowered = text.lower()
        meta = experience.metadata or {}

        scores: dict[str, float] = {}
        detail: dict[str, str] = {}

        correction = self._correction_score(experience, lowered, meta)
        if correction > 0.0:
            scores["user_correction"] = correction
            detail["user_correction"] = self._correction_detail(experience, lowered, meta)

        emphasis = self._emphasis_score(experience, lowered, meta)
        if emphasis > 0.0:
            scores["user_emphasis"] = emphasis
            detail["user_emphasis"] = "explicit emphasis marker"

        repetition = self._repetition_score(experience, text)
        if repetition > 0.0:
            scores["repeated_pattern"] = repetition
            detail["repeated_pattern"] = (
                f"content repeated {self._occurrences(experience, text)}x in session"
            )

        error = self._error_score(experience, lowered, meta)
        if error > 0.0:
            scores["error_or_surprise"] = error
            detail["error_or_surprise"] = self._error_detail(experience, lowered, meta)

        decision = self._decision_score(experience, lowered, meta)
        if decision > 0.0:
            scores["key_decision"] = decision
            detail["key_decision"] = "goal-advancing decision marker"

        association = self._association_score(experience, text)
        if association > 0.0:
            scores["strong_association"] = association
            detail["strong_association"] = "high overlap with existing memory"

        return scores, detail

    def _aggregate(self, scores: dict[str, float]) -> float:
        """Noisy-OR the weighted signal scores into ``[0, 1]``.

        ``1 - prod(1 - w_i * s_i)``: each signal independently raises the
        aggregate and saturates at 1.0, so one strong signal is enough while
        several weak ones still need to add up.
        """
        remaining = 1.0
        for name, score in scores.items():
            weight = self._weights.get(name, 0.0)
            if weight <= 0.0 or score <= 0.0:
                continue
            remaining *= 1.0 - min(1.0, weight * score)
        return 1.0 - remaining

    # ---- user correction -------------------------------------------------

    def _correction_score(
        self, experience: Experience, lowered: str, meta: dict[str, Any]
    ) -> float:
        """Score explicit user correction/constraint, 0.0 when absent."""
        score = 0.0
        if any(bool(meta.get(flag)) for flag in _CORRECTION_FLAGS):
            score = max(score, 1.0)
        if experience.kind in ("correction", "user_correction"):
            score = max(score, 1.0)
        if self._from_feedback_learner(meta):
            score = max(score, 0.95)
        if experience.kind in _USER_KINDS and _matches(lowered, _CORRECTION_PATTERNS):
            score = max(score, 0.85)
        return score

    @staticmethod
    def _correction_detail(
        experience: Experience, lowered: str, meta: dict[str, Any]
    ) -> str:
        if any(bool(meta.get(flag)) for flag in _CORRECTION_FLAGS):
            return "metadata correction flag"
        if experience.kind in ("correction", "user_correction"):
            return "correction experience kind"
        if SignificanceJudge._from_feedback_learner(meta):
            return "sourced from feedback learner"
        if _matches(lowered, _CORRECTION_PATTERNS):
            return "user correction pattern"
        return "correction signal"

    @staticmethod
    def _from_feedback_learner(meta: dict[str, Any]) -> bool:
        """Whether metadata marks this experience as a learned correction."""
        source = str(meta.get("source", "")).lower()
        if source in ("feedback", "feedback_learner", "learner"):
            return True
        return bool(meta.get("feedback_learner"))

    # ---- user emphasis ---------------------------------------------------

    @staticmethod
    def _emphasis_score(
        experience: Experience, lowered: str, meta: dict[str, Any]
    ) -> float:
        """Score explicit emphasis markers, 0.0 when absent."""
        if any(bool(meta.get(flag)) for flag in _EMPHASIS_FLAGS):
            return 1.0
        if experience.kind in _USER_KINDS and _matches(lowered, _EMPHASIS_PATTERNS):
            return 0.8
        return 0.0

    # ---- repeated pattern ------------------------------------------------

    def _repetition_score(self, experience: Experience, text: str) -> float:
        """Score in-session repetition of an experience's content.

        The count includes the current occurrence: a single occurrence scores
        0.0, a second occurrence 0.7, and a third (or later) 1.0.
        """
        occurrences = self._occurrences(experience, text)
        if occurrences <= 1:
            return 0.0
        # 2 occurrences -> 0.7 (clears the default threshold on its own),
        # 3 -> 1.0, capped at 1.0.
        return min(1.0, (occurrences - 1) * 0.7)

    def _occurrences(self, experience: Experience, text: str) -> int:
        """Return the total occurrence count of ``text`` including this one."""
        fingerprint = _fingerprint(text)
        prior = self._session_fingerprints.get(experience.session_id, {}).get(
            fingerprint, 0
        )
        return prior + 1

    # ---- error / surprise ------------------------------------------------

    def _error_score(
        self, experience: Experience, lowered: str, meta: dict[str, Any]
    ) -> float:
        """Score tool failures, exceptions, and surprising outcomes."""
        score = 0.0
        if experience.kind in ("error", "exception", "tool_error"):
            score = max(score, 1.0)
        elif experience.kind == "tool_result" and self._meta_error(meta):
            score = max(score, 0.9)
        elif self._meta_error(meta):
            score = max(score, 0.8)
        if "traceback (most recent call last)" in lowered:
            score = max(score, 1.0)
        if _matches(lowered, _SURPRISE_PATTERNS):
            score = max(score, 0.6)
        return score

    def _error_detail(
        self, experience: Experience, lowered: str, meta: dict[str, Any]
    ) -> str:
        if experience.kind in ("error", "exception", "tool_error"):
            return f"error experience kind ({experience.kind})"
        if self._meta_error(meta):
            return "metadata error/failure flag"
        if "traceback (most recent call last)" in lowered:
            return "traceback detected"
        if _matches(lowered, _SURPRISE_PATTERNS):
            return "surprise/contradiction pattern"
        return "error signal"

    @staticmethod
    def _meta_error(meta: dict[str, Any]) -> bool:
        """Whether metadata indicates a failure."""
        for flag in _ERROR_FLAGS:
            if bool(meta.get(flag)):
                return True
        return False

    # ---- key decision ----------------------------------------------------

    def _decision_score(
        self, experience: Experience, lowered: str, meta: dict[str, Any]
    ) -> float:
        """Score a goal-advancing decision or commitment."""
        if any(bool(meta.get(flag)) for flag in _DECISION_FLAGS):
            return 1.0
        if experience.kind in _DECISION_KINDS:
            return 0.9
        if _matches(lowered, _DECISION_PATTERNS):
            return 0.85
        if _matches(lowered, _DECISION_WEAK_PATTERNS):
            return 0.45
        return 0.0

    # ---- strong association ---------------------------------------------

    def _association_score(self, experience: Experience, text: str) -> float:
        """Score overlap with existing memory through associative recall.

        Retrieval goes only through
        :class:`~encre.memdir.recall.RecallEngine` (the single retrieval path);
        the engine is an optional collaborator, so a judge built without one --
        or with a store that is empty or failing -- degrades to ``0.0``
        (signal inactive), keeping the judge offline and never raising.
        """
        engine = self._recall_engine
        if engine is None:
            return 0.0
        try:
            hits = engine.recall(text, top_k=3)
        except Exception:
            logger.debug("[judge] memory recall failed", exc_info=True)
            return 0.0
        best = 0.0
        for hit in hits or []:
            score = float(getattr(hit, "score", 0.0) or 0.0)
            if score > best:
                best = score
        # 0.5 similarity is the point at which association becomes meaningful.
        return min(1.0, best / 0.5) if best >= 0.5 else 0.0

    # ------------------------------------------------------------------
    # Type suggestion
    # ------------------------------------------------------------------

    def _suggest_type(self, experience: Experience, scores: dict[str, float]) -> str:
        """Suggest a memory type from the fired signals and experience kind.

        * a goal-advancing decision or a stated constraint about the user /
          project -> ``autobiographical``;
        * a correction or a failure lesson -> ``procedural``;
        * a repeated pattern -> ``semantic``;
        * anything else (a lived event) -> ``episodic``.
        """
        if scores.get("key_decision"):
            return "autobiographical"
        if scores.get("user_correction"):
            return "procedural"
        if scores.get("error_or_surprise"):
            return "procedural"
        if scores.get("repeated_pattern"):
            return "semantic"
        if scores.get("strong_association"):
            return "semantic"
        return "episodic"

    # ------------------------------------------------------------------
    # Bus integration
    # ------------------------------------------------------------------

    def attach(self, bus: Any) -> None:
        """Subscribe to ``bus`` and judge experiences as events arrive.

        The bus is expected to follow :class:`~encre.events.ledger.EventSourcedBus`
        semantics: ``subscribe(kind, handler)`` where ``kind`` may be ``"*"``.
        On each event the handler converts it into an :class:`Experience`,
        judges it, and -- only for candidates that clear the threshold --
        publishes a ``"memory_candidate"`` event carrying the candidate dict.

        Nothing is persisted here: this stage judges and announces.  Storage
        is a separate downstream responsibility.

        Idempotent per bus instance; never raises into the publisher (the
        bus isolates observer failures, and the handler is defensive).

        Args:
            bus: The event backbone to observe and publish on.
        """
        if bus is None or id(bus) in self._attached:
            return
        self._attached.add(id(bus))
        self._attached_bus = bus
        bus.subscribe("*", self._on_event)

    # ``observe`` is an explicit alias for callers that prefer the verb.
    observe = attach

    def _on_event(self, event: Any) -> None:
        """Bus observer: judge one event and publish significant candidates."""
        experience = self.experience_from_event(event)
        if experience is None:
            return
        candidate = self.judge(experience)
        self.record(experience)
        if candidate is None:
            return
        self.publish_candidate(self._attached_bus, candidate)

    def publish_candidate(self, bus: Any, candidate: MemoryCandidate) -> Any:
        """Publish a candidate on ``bus`` as a ``"memory_candidate"`` event.

        Args:
            bus: The event backbone (``publish(kind, session_id=..., payload=...)``).
            candidate: The candidate to announce.

        Returns:
            The bus's event object, or ``None`` when publishing is refused.
        """
        if bus is None:
            return None
        try:
            return bus.publish(
                "memory_candidate",
                session_id=candidate.session_id,
                payload=candidate.to_dict(),
                refs=list(candidate.source_refs),
            )
        except Exception:
            logger.warning("[judge] failed to publish memory_candidate", exc_info=True)
            return None

    def schedule_judgment(self, experiences: Any, bus: Any) -> asyncio.Task | None:
        """Judge ``experiences`` off the critical path and publish candidates.

        Judgment is scheduled as a background task so the caller (e.g. the
        end-of-turn phase) never waits on it.  Returns the task so callers
        that *do* want to await it (tests, shutdown) can.

        Args:
            experiences: Iterable of :class:`Experience`.
            bus: The event backbone to publish significant candidates on.

        Returns:
            The created :class:`asyncio.Task`, or ``None`` when no running
            loop is available.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(self._judge_and_publish(list(experiences), bus))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _judge_and_publish(self, experiences: list[Experience], bus: Any) -> None:
        """Background worker for :meth:`schedule_judgment`."""
        try:
            for candidate in self.judge_batch(experiences):
                self.publish_candidate(bus, candidate)
        except Exception:
            logger.warning("[judge] background judgment failed", exc_info=True)

    # ------------------------------------------------------------------
    # Event -> Experience conversion
    # ------------------------------------------------------------------

    @staticmethod
    def experience_from_event(event: Any) -> Experience | None:
        """Convert a ledger event into an :class:`Experience`.

        Returns ``None`` for events that carry no judgeable content (phase
        noise, empty payloads, our own ``memory_candidate`` echoes).
        """
        kind = str(getattr(event, "kind", "") or "")
        if not kind or kind == "memory_candidate":
            return None
        session_id = str(getattr(event, "session_id", "") or "")
        refs = [str(r) for r in (getattr(event, "refs", None) or [])]
        event_id = str(getattr(event, "event_id", "") or "")
        if event_id:
            refs.append(event_id)
        payload = getattr(event, "payload", None) or {}
        ts = float(getattr(event, "ts", 0.0) or time.time())

        if not isinstance(payload, dict):
            return None

        # Resolve the experience kind from the event kind and payload.
        exp_kind = kind
        if kind.startswith("phase:"):
            phase = kind[len("phase:"):]
            if phase in ("turn_end", "turn_started", "turn_start"):
                exp_kind = "turn_end"
            else:
                return None

        content = _extract_content(payload)
        if not content:
            return None

        metadata: dict[str, Any] = {
            key: payload[key]
            for key in (
                "is_error", "error", "failed", "exception", "traceback",
                "tool_name", "success", "source", "correction", "decision",
                "emphasis", "goal_met", "milestone", "repeated",
            )
            if key in payload
        }
        if exp_kind in ("error", "exception", "tool_error"):
            metadata.setdefault("is_error", True)
        if "correction" in kind:
            metadata.setdefault("correction", True)
        if "decision" in kind:
            metadata.setdefault("decision", True)

        return Experience(
            kind=exp_kind,
            content=content,
            session_id=session_id,
            refs=refs,
            metadata=metadata,
            ts=ts,
        )


def _extract_content(payload: dict[str, Any]) -> str:
    """Pull the most meaningful text out of an event payload."""
    for key in ("content", "text", "result", "message", "summary", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    nested = payload.get("experience")
    if isinstance(nested, dict):
        value = nested.get("content")
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    """Return ``True`` when ``text`` matches any regex in ``patterns``."""
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False
