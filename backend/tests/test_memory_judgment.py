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

"""Tests for the significance judge, the single gated entry point to memory.

The governing invariant (project principle) is that context is not memory:
memory is written only after the judge decides that an experience is
meaningful, and anything below the significance threshold is dropped with no
event and no write.  Each signal is checked in isolation -- user correction,
repetition, error or surprise, key decision, and strong association with
existing memory -- together with explainable reasons, JSON round-trips, and
configurable thresholds and weights.

The second half drives the judge through the real :class:`EventSourcedBus`
backbone and the real end-of-turn dispatch hook, proving that candidates are
published as ``memory_candidate`` events, that dispatch never blocks the
critical turn path, and that a missing bus is a silent no-op.  The judge, the
dataclasses and the bus are all production implementations.
"""

import asyncio
import json
import time
from types import SimpleNamespace

from encre.events.ledger import EventSourcedBus
from encre.loop.phases.end_of_turn import PhaseEndOfTurnMixin
from encre.loop.turn_ctx import TurnContext
from encre.memdir.judgment import Experience, MemoryCandidate, SignificanceJudge
from encre.utils.types import BackendFinish, BackendText

# The user correction from the spec scenario ("our project always uses port
# 8080").  It must be recognised as a high-significance correction.
_USER_CORRECTION = "我们项目的端口固定用 8080，不要再用 3000 了"

# A one-off, task-irrelevant intermediate output: a directory listing.
_ONE_OFF_LISTING = "\n".join([
    "src/main.py",
    "src/utils.py",
    "src/config.json",
    "README.md",
    "package.json",
    "tests/test_a.py",
])


# ----------------------------------------------------------------------
# Scenario: user correction -> high-significance candidate
# ----------------------------------------------------------------------


def test_user_correction_produces_candidate():
    """An explicit user correction must clear the threshold and be explained."""
    judge = SignificanceJudge()
    candidate = judge.judge(Experience(
        kind="user_message",
        content=_USER_CORRECTION,
        session_id="sess-correction",
        refs=["msg-42"],
    ))

    assert candidate is not None
    assert candidate.significance >= judge.threshold
    assert any("user_correction" in reason for reason in candidate.reasons)
    assert candidate.metadata["signals"]["user_correction"] > 0.0
    assert candidate.source_refs == ["msg-42"]
    assert candidate.session_id == "sess-correction"
    assert candidate.suggested_type in {
        "fact", "preference", "decision", "lesson", "entity", "episode",
    }


def test_user_correction_from_feedback_learner():
    """A correction sourced from the feedback learner is high-significance."""
    judge = SignificanceJudge()
    candidate = judge.judge(Experience(
        kind="tool_result",
        content="tool 'bash' failed because the port was wrong",
        metadata={"source": "feedback_learner", "tool_name": "bash"},
    ))
    assert candidate is not None
    assert any("user_correction" in reason for reason in candidate.reasons)


# ----------------------------------------------------------------------
# Scenario: one-off, irrelevant intermediate output -> no memory
# ----------------------------------------------------------------------


def test_one_off_listing_is_not_memorized():
    """A one-off file listing must not become memory."""
    judge = SignificanceJudge()
    candidate = judge.judge(Experience(
        kind="tool_result",
        content=_ONE_OFF_LISTING,
        metadata={"tool_name": "file_list", "is_error": False},
    ))
    assert candidate is None


def test_empty_and_tiny_content_is_not_memorized():
    """Blank or trivially short content is never a candidate."""
    judge = SignificanceJudge()
    assert judge.judge(Experience(kind="tool_result", content="")) is None
    assert judge.judge(Experience(kind="tool_result", content="ok")) is None


def test_batch_filters_out_insignificant_experiences():
    """judge_batch drops the None results and keeps only real candidates."""
    judge = SignificanceJudge()
    candidates = judge.judge_batch([
        Experience(kind="tool_result", content=_ONE_OFF_LISTING),
        Experience(kind="user_message", content=_USER_CORRECTION),
        Experience(kind="tool_result", content="a\nb\nc\nd"),
    ])
    assert len(candidates) == 1
    assert "user_correction" in " ".join(candidates[0].reasons)


# ----------------------------------------------------------------------
# Signal: repeated pattern
# ----------------------------------------------------------------------


def test_repeated_pattern_raises_significance():
    """A pattern seen repeatedly becomes more significant with each sighting."""
    judge = SignificanceJudge()
    experience = Experience(
        kind="tool_result",
        content="the staging service listens on port 8080 by default",
        metadata={"tool_name": "deploy"},
    )

    # A single occurrence carries no repetition signal at all.
    first = judge.judge(experience)
    assert first is None

    judge.record(experience)
    second = judge.judge(experience)
    assert second is not None
    assert second.metadata["signals"]["repeated_pattern"] > 0.0
    assert any("repeated_pattern" in reason for reason in second.reasons)

    judge.record(experience)
    third = judge.judge(experience)
    assert third is not None
    # Significance must strictly rise as the pattern repeats.
    assert third.significance > second.significance


def test_repetition_is_isolated_per_session():
    """The same text in another session does not count as a repeat."""
    judge = SignificanceJudge()
    a = Experience(kind="tool_result", content="pattern text about caching layer")
    b = Experience(
        kind="tool_result", content="pattern text about caching layer",
        session_id="other",
    )
    judge.record(a)
    # Same content, different session: still a first occurrence over there.
    candidate = judge.judge(b)
    assert candidate is None


# ----------------------------------------------------------------------
# Signal: error / surprise
# ----------------------------------------------------------------------


def test_error_signal_produces_candidate():
    """A tool failure is significant on its own."""
    judge = SignificanceJudge()
    candidate = judge.judge(Experience(
        kind="error",
        content="Traceback (most recent call last): ValueError: bad port",
        metadata={"tool_name": "bash"},
    ))
    assert candidate is not None
    assert any("error_or_surprise" in reason for reason in candidate.reasons)


def test_tool_error_flag_produces_candidate():
    """A failed tool result is significant even without an 'error' kind."""
    judge = SignificanceJudge()
    candidate = judge.judge(Experience(
        kind="tool_result",
        content="Command failed with exit code 1 while starting the server",
        metadata={"tool_name": "bash", "is_error": True},
    ))
    assert candidate is not None
    assert candidate.metadata["signals"]["error_or_surprise"] > 0.0


def test_surprise_alone_is_below_threshold():
    """A generic surprise marker alone does not force a memory write."""
    judge = SignificanceJudge()
    candidate = judge.judge(Experience(
        kind="tool_result",
        content="The output was unexpected and contradicted the documentation",
    ))
    assert candidate is None


# ----------------------------------------------------------------------
# Signal: key decision
# ----------------------------------------------------------------------


def test_key_decision_produces_candidate():
    """A deliberate, goal-advancing decision is significant."""
    judge = SignificanceJudge()
    candidate = judge.judge(Experience(
        kind="user_message",
        content="We decided to use Redis for the session cache",
    ))
    assert candidate is not None
    assert any("key_decision" in reason for reason in candidate.reasons)


def test_weak_decision_hint_alone_is_below_threshold():
    """A generic "let's" phrasing is not, by itself, a key decision."""
    judge = SignificanceJudge()
    candidate = judge.judge(Experience(
        kind="assistant_message",
        content="Let's look at the plan again before we continue",
    ))
    assert candidate is None


# ----------------------------------------------------------------------
# Signal: strong association with existing memory
# ----------------------------------------------------------------------


class _FakeRecallHit:
    """Minimal stand-in for a recall hit, carrying a score and an entry."""

    def __init__(self, score: float) -> None:
        """Create a fake hit with a fixed score and a canned existing entry."""
        self.score = score
        self.entry = SimpleNamespace(id="existing", type="fact", content="existing")


class _FakeRecallEngine:
    """Recall-engine stub exposing only the ``recall`` surface the judge uses."""

    def __init__(self, score: float) -> None:
        """Create the stub with a fixed score every query will return."""
        self.score = score
        self.queries: list[str] = []

    def recall(self, query: str, *, top_k: int | None = None):  # noqa: ANN001
        """Record the query and return one fake hit at the stubbed score."""
        self.queries.append(query)
        return [_FakeRecallHit(self.score)]


def test_strong_association_with_existing_memory():
    """High overlap with existing memory counts as a significance signal."""
    engine = _FakeRecallEngine(score=0.8)
    judge = SignificanceJudge(recall_engine=engine)
    candidate = judge.judge(Experience(
        kind="assistant_message",
        content="The authentication service issues JWT tokens per request",
    ))
    assert engine.queries  # the judge really consulted the recall engine
    assert candidate is not None
    assert any("strong_association" in reason for reason in candidate.reasons)


def test_no_association_below_similarity_floor():
    """Weak similarity to existing memory contributes nothing to the score."""
    judge = SignificanceJudge(recall_engine=_FakeRecallEngine(score=0.2))
    candidate = judge.judge(Experience(
        kind="assistant_message",
        content="The authentication service issues JWT tokens per request",
    ))
    assert candidate is None


def test_memory_system_failure_falls_back_to_rules():
    """A failing recall engine must not break judgment (offline fallback)."""

    class _BrokenRecall:
        """Recall engine that raises instead of answering, simulating failure."""

        def recall(self, query: str, *, top_k: int | None = None):  # noqa: ANN001
            """Always raise to simulate an unavailable recall index."""
            raise RuntimeError("index unavailable")

    judge = SignificanceJudge(recall_engine=_BrokenRecall())
    # A correction still produces a candidate despite the broken dependency.
    assert judge.judge(Experience(kind="user_message", content=_USER_CORRECTION))
    # A neutral experience still yields None.
    assert judge.judge(Experience(kind="tool_result", content=_ONE_OFF_LISTING)) is None


# ----------------------------------------------------------------------
# Configurability
# ----------------------------------------------------------------------


def test_threshold_is_configurable():
    """Raising the threshold turns a passing experience into None."""
    experience = Experience(kind="user_message", content=_USER_CORRECTION)
    assert SignificanceJudge(threshold=0.5).judge(experience) is not None
    assert SignificanceJudge(threshold=0.99).judge(experience) is None


def test_weights_are_configurable():
    """Overriding a single weight changes the aggregate significance."""
    experience = Experience(kind="user_message", content=_USER_CORRECTION)
    default = SignificanceJudge().judge(experience)
    boosted = SignificanceJudge(weights={"user_correction": 1.0}).judge(experience)
    assert default is not None and boosted is not None
    assert boosted.significance > default.significance
    assert SignificanceJudge().weights["user_correction"] == 0.85


def test_set_threshold_at_runtime():
    """The threshold can be updated after the judge is constructed."""
    judge = SignificanceJudge(threshold=0.99)
    experience = Experience(kind="user_message", content=_USER_CORRECTION)
    assert judge.judge(experience) is None
    judge.set_threshold(0.5)
    assert judge.judge(experience) is not None


# ----------------------------------------------------------------------
# Explainability and serialisation
# ----------------------------------------------------------------------


def test_reasons_are_explainable():
    """Every reason names the signal, its weight and its score."""
    judge = SignificanceJudge()
    candidate = judge.judge(Experience(
        kind="user_message",
        content=_USER_CORRECTION,
        metadata={"is_error": True},
    ))
    assert candidate is not None
    assert candidate.reasons
    for reason in candidate.reasons:
        assert "w=" in reason and "s=" in reason
    signals = candidate.metadata["signals"]
    assert signals["user_correction"] > 0.0
    # No signal may be reported as fired without a positive score behind it.
    for reason in candidate.reasons:
        name = reason.split(" ")[0]
        assert signals.get(name, 0.0) > 0.0


def test_candidate_to_from_dict_round_trip():
    """MemoryCandidate survives a JSON round trip unchanged."""
    judge = SignificanceJudge()
    candidate = judge.judge(Experience(
        kind="user_message",
        content=_USER_CORRECTION,
        session_id="sess-rt",
        refs=["msg-1", "evt-2"],
    ))
    assert candidate is not None

    restored = MemoryCandidate.from_dict(json.loads(json.dumps(candidate.to_dict())))
    assert restored == candidate
    assert restored.source_refs == ["msg-1", "evt-2"]
    assert restored.reasons == candidate.reasons


def test_experience_to_from_dict_round_trip():
    """Experience survives a JSON round trip unchanged."""
    experience = Experience(
        kind="tool_result",
        content="deploy uses port 8080",
        session_id="sess-exp",
        refs=["call-1"],
        metadata={"tool_name": "bash", "is_error": False},
        ts=123.5,
    )
    restored = Experience.from_dict(json.loads(json.dumps(experience.to_dict())))
    assert restored == experience


# ----------------------------------------------------------------------
# Bus integration: candidates are published as `memory_candidate` events
# ----------------------------------------------------------------------


async def test_candidate_is_published_on_the_backbone():
    """A significant event observed on the bus yields a memory_candidate event."""
    bus = EventSourcedBus("judgment-bus")
    judge = SignificanceJudge()
    judge.attach(bus)

    received: list = []
    bus.subscribe("memory_candidate", lambda ev: received.append(ev))

    bus.publish(
        "user_message",
        session_id="sess-bus",
        payload={"content": _USER_CORRECTION},
        refs=["msg-7"],
    )

    assert len(received) == 1
    event = received[0]
    assert event.kind == "memory_candidate"
    assert event.session_id == "sess-bus"
    assert event.payload["content"] == _USER_CORRECTION
    assert any("user_correction" in r for r in event.payload["reasons"])
    assert "msg-7" in event.refs


async def test_insignificant_event_publishes_nothing():
    """A one-off event observed on the bus produces no candidate event."""
    bus = EventSourcedBus("judgment-bus-quiet")
    judge = SignificanceJudge()
    judge.attach(bus)

    received: list = []
    bus.subscribe("memory_candidate", lambda ev: received.append(ev))

    bus.publish(
        "tool_result",
        session_id="sess-bus",
        payload={"content": _ONE_OFF_LISTING, "tool_name": "file_list"},
    )

    assert received == []
    assert bus.count == 1  # only the original event, no candidate


def test_attach_is_idempotent_per_bus():
    """Attaching the same judge twice does not double-judge an event."""
    bus = EventSourcedBus("judgment-idempotent")
    judge = SignificanceJudge()
    judge.attach(bus)
    judge.attach(bus)

    received: list = []
    bus.subscribe("memory_candidate", lambda ev: received.append(ev))
    bus.publish("user_message", session_id="s", payload={"content": _USER_CORRECTION})

    assert len(received) == 1


# ----------------------------------------------------------------------
# Non-blocking dispatch and the end-of-turn hook
# ----------------------------------------------------------------------


class _ScriptedScorer:
    """The memory gate's scoring backend: keep the correction, drop the noise.

    The production gate is fail-closed -- without a backend it drops every
    candidate -- so the fake loop must expose one for the dispatch path to be
    exercised at all.  Answers exactly what the scorer contract demands (one
    decimal, nothing else), scoring the durable correction as worth keeping and
    everything else as worthless, which is what the three hook tests assert.
    """

    async def chat(self, messages, tools=None, tool_choice="auto", temperature=0.0,
                   max_tokens=4096, stream=True, enable_caching=False, cache_edits_state=None):
        """Yield 1.0 for the port correction candidate, 0.0 for anything else."""
        payload = " ".join(str(m.get("content", "")) for m in messages)
        answer = "1.0" if "8080" in payload else "0.0"
        yield BackendText(text=answer)
        yield BackendFinish(reason="stop", usage={"input_tokens": 8, "output_tokens": 1})


class _FakeLoop(PhaseEndOfTurnMixin):
    """Minimal host exposing only the attributes the judgment hook touches."""

    def __init__(self, bus: EventSourcedBus) -> None:
        """Attach the given bus plus a canned session id and no memory system."""
        self.event_bus = bus
        # ``turn_count`` is part of the real session surface: every experience
        # carries the turn it was produced in so rollback can archive exactly
        # the memories a discarded turn wrote.
        self.session = SimpleNamespace(id="sess-loop", turn_count=1)
        self.memory_system = None
        self.backend = _ScriptedScorer()


def _turn_context(prompt: str = "", prepared: list | None = None) -> TurnContext:
    """Build a TurnContext carrying a prompt and optional prepared tool results."""
    ctx = TurnContext(prompt, None, "", "", None)
    ctx.turn_start = time.time()
    ctx.prepared = prepared or []
    return ctx


async def test_schedule_judgment_returns_an_independent_task():
    """Judgment is scheduled as a task, not awaited on the critical path."""
    bus = EventSourcedBus("judgment-sched")
    judge = SignificanceJudge()
    received: list = []
    bus.subscribe("memory_candidate", lambda ev: received.append(ev))

    task = judge.schedule_judgment(
        [Experience(kind="user_message", content=_USER_CORRECTION, session_id="s")],
        bus,
    )

    assert isinstance(task, asyncio.Task)
    assert not task.done()  # created but not yet run, so the caller did not block
    assert received == []

    await task
    assert len(received) == 1


async def test_end_of_turn_hook_dispatches_without_blocking():
    """The end-of-turn hook returns immediately and judges in the background."""
    bus = EventSourcedBus("judgment-hook")
    loop = _FakeLoop(bus)
    received: list = []
    bus.subscribe("memory_candidate", lambda ev: received.append(ev))

    ctx = _turn_context(prompt=_USER_CORRECTION)
    loop._dispatch_significance_judgment(ctx)

    # Dispatch returned before the background task ran, so the turn tail did
    # not wait for judgment to finish.
    assert received == []
    judge = loop._significance_judge
    assert judge._tasks  # an independent task was scheduled

    await asyncio.gather(*list(judge._tasks))
    assert len(received) == 1
    assert received[0].payload["session_id"] == "sess-loop"


async def test_end_of_turn_hook_drops_insignificant_turn():
    """A turn holding only one-off tool output publishes nothing at all."""
    bus = EventSourcedBus("judgment-hook-quiet")
    loop = _FakeLoop(bus)
    received: list = []
    bus.subscribe("memory_candidate", lambda ev: received.append(ev))

    ctx = _turn_context(prepared=[
        {"name": "file_list", "result": _ONE_OFF_LISTING, "is_error": False},
    ])
    loop._dispatch_significance_judgment(ctx)

    judge = loop._significance_judge
    await asyncio.gather(*list(judge._tasks))
    assert received == []
    assert bus.count == 0  # no candidate, no event, no write


async def test_neutral_prompt_is_not_promoted_by_re_observation():
    """A neutral run prompt seen on later turns does not count as a repeat."""
    bus = EventSourcedBus("judgment-prompt-dedupe")
    loop = _FakeLoop(bus)
    received: list = []
    bus.subscribe("memory_candidate", lambda ev: received.append(ev))

    neutral = "please summarise the repository layout for me"
    for _ in range(3):  # three turns of the same run re-see the same prompt
        loop._dispatch_significance_judgment(_turn_context(prompt=neutral))
        await asyncio.gather(*list(loop._significance_judge._tasks))

    assert received == []
    assert bus.count == 0


def test_end_of_turn_hook_without_bus_is_a_noop():
    """With no event backbone wired the hook does nothing and never writes."""
    loop = _FakeLoop(bus=None)  # type: ignore[arg-type]
    ctx = _turn_context(prompt=_USER_CORRECTION)
    loop._dispatch_significance_judgment(ctx)  # must not raise
    assert not hasattr(loop, "_significance_judge")
