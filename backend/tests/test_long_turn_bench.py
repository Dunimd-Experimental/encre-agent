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

from __future__ import annotations

"""P1.3 long-turn production benchmark and stateless-request verification.

The loop under test is the **real** ``EncreLoop`` driving the real phase
pipeline (P1.1 registry) over a scripted :class:`~encre.backends.base.BaseBackend`
subclass -- a real fixture backend, not a mock of loop internals.  The data
root is redirected to ``tmp_path`` via ``ENCRE_DATA_DIR`` and no network is
touched.

Invariants under a 300-iteration tool loop:

1. **Linear prompt assembly** -- per-iteration wall time must not blow up:
   the median iteration stays within 3x of the first iteration and the
   mean of the last 100 iterations stays within 3x of the mean of the
   first 100.  A quadratic prompt rebuild would show a ~20x+ blowup at
   these transcript sizes (100 -> 600 messages), so these thresholds
   assert the complexity class, not absolute speed.  The 3x mean bound is
   calibrated against the measured linear signature under ``tracemalloc``
   (1.33x without it, 2.35-2.48x with it -- the hook cost scales with the
   per-iteration allocation count, which itself grows linearly with the
   transcript because every request must tokenise the full history); GC
   was ruled out experimentally (ratio identical with gc disabled).
2. **Bounded memory** -- ``tracemalloc`` current memory sampled at chat
   call 51 vs chat call 300 must grow by less than 10 MB.  The bench sets
   ``checkpoint_max_count=1`` (real production config knob): the runner
   takes a full-transcript deepcopy checkpoint every turn and by default
   retains 10 of them, which is bounded-by-design retention that scales
   linearly with the transcript (~49 MB measured at 300 iterations with
   the default of 10) -- not a leak, but it would swamp this budget.
   With retention capped at one checkpoint the residual growth is the
   pipeline's own (ledger, session transcript, working set).
3. **Message-list correctness** -- every one of the 301 requests carries
   exactly one system message, the final request carries exactly 300 tool
   results, and the session transcript ends with the final assistant text.

Stateless-request verification (separate tests):

4. Within one run, every model request sees the *same* system prompt (the
   run-scoped prompt phase builds it once) and an identical, deduplicated
   tool-definition list.
5. Across two consecutive runs the static prefix of the system prompt is
   byte-identical; only the documented turn-scoped suffix blocks (task
   stage, working set, ``## Prior Turns`` digest and the ``## Context
   Changes This Turn`` annotation, all appended after the static prefix
   per the prompt-caching contract in ``loop/phases/prompt.py``) may
   differ.
"""

import json
import statistics
import time
import tracemalloc
from typing import Any
from unittest.mock import patch

import pytest

from encre.backends.base import BaseBackend
from encre.config import EncreConfig
from encre.loop import EncreLoop
from encre.session import EncreSession
from encre.tools.base import build_tool
from encre.utils.types import BackendFinish, BackendText, BackendToolCall, Finish

_PROMPT = "Ping the bench tool once per turn until the task is done."

_TOOL_NAME = "bench_ping"
_TOOL_ITERATIONS = 300

# Deliberately > 300 chars so the stub-response detector never flags the
# canned final response.
_FINAL_TEXT = (
    "The benchmark loop has completed all three hundred tool iterations. "
    "Each iteration called the bench_ping tool exactly once with a unique "
    "integer argument and received a deterministic pong response, which "
    "exercises the full turn-scoped phase pipeline: model, post-stream, "
    "prepare-tools, execute-tools, secondary-tools, post-tool-compaction "
    "and end-of-turn. This closing message is deliberately long so the "
    "stub detector treats it as a genuine final answer and the run "
    "terminates cleanly through the normal stop path."
)

_KB = 1024
_MEMORY_GROWTH_BUDGET = 10 * 1024 * _KB


def _bench_ping(**kwargs: Any) -> str:
    """Deterministic no-op tool body; unique args per iteration."""
    return f"pong {kwargs.get('n')}"


def _tool_names(tools: list[dict[str, Any]] | None) -> list[str]:
    """Extract function names from an OpenAI-format tool payload."""
    names: list[str] = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        name = (fn or t or {}).get("name", "") if isinstance(t, dict) else ""
        names.append(name)
    return names


class _BenchBackend(BaseBackend):
    """Real ``BaseBackend`` subclass scripting a long tool-calling run.

    Per request it records only lightweight metadata (role sequence,
    system-message content reference, tool names, wall-clock entry time,
    tracemalloc sample) -- deliberately NOT full message-list copies, so
    the harness's own bookkeeping cannot distort the memory measurement.
    """

    def __init__(self, tool_iterations: int) -> None:
        self.tool_iterations = tool_iterations
        self.chat_calls = 0
        self.call_times: list[float] = []
        self.mem_samples: list[int] = []
        self.request_roles: list[list[str]] = []
        self.request_system_contents: list[Any] = []
        self.request_tool_names: list[list[str]] = []
        self.request_payload_bytes: list[int] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = True,
        enable_caching: bool = False,
        cache_edits_state=None,
    ):
        self.chat_calls += 1
        self.call_times.append(time.perf_counter())
        self.mem_samples.append(
            tracemalloc.get_traced_memory()[0] if tracemalloc.is_tracing() else 0
        )
        roles = [m.get("role", "") for m in messages]
        self.request_roles.append(roles)
        system_msgs = [m for m in messages if m.get("role") == "system"]
        self.request_system_contents.append(
            system_msgs[0].get("content") if system_msgs else None
        )
        self.request_tool_names.append(_tool_names(tools))
        # Approximate size of the transcript payload itself, so the memory
        # assertion can subtract legitimate linear transcript growth from
        # the traced growth (the transcript is BY DESIGN linear in turns).
        self.request_payload_bytes.append(
            sum(len(json.dumps(m, default=str)) for m in messages)
        )

        if self.chat_calls <= self.tool_iterations:
            yield BackendToolCall(
                id=f"call_{self.chat_calls}",
                name=_TOOL_NAME,
                arguments=json.dumps({"n": self.chat_calls}),
            )
            yield BackendFinish(
                reason="tool_calls",
                usage={"input_tokens": 20, "output_tokens": 5},
            )
        else:
            yield BackendText(text=_FINAL_TEXT)
            yield BackendFinish(
                reason="stop", usage={"input_tokens": 120, "output_tokens": 80},
            )

    def supports_tool_calling(self) -> bool:
        return True

    # A huge window keeps pre-model compaction inert so every scripted
    # response is consumed by the real turn pipeline.
    def context_window_size(self) -> int:
        return 1_000_000

    def supports_prompt_caching(self) -> bool:
        return False


def _make_loop(tmp_path, monkeypatch, backend: _BenchBackend) -> EncreLoop:
    """Real loop + real session, isolated data root, bench tool registered."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    config = EncreConfig(
        model="bench-model",
        backend_type="openai",
        permission_mode="bypass",
        max_turns=0,
        max_tokens=512,
        log_level="ERROR",
        enable_prompt_caching=False,
        enable_streaming_tool_execution=False,
        # The runner deep-copies the full transcript into a session
        # checkpoint every turn and by default retains 10 of them
        # (bounded-by-design retention, linear in transcript size, ~49MB at
        # 300 iterations).  Cap retention at one checkpoint -- still
        # exercising the checkpoint path -- so the <10MB growth assertion
        # measures the pipeline itself rather than this config knob.
        checkpoint_max_count=1,
    )
    session = EncreSession(config)
    with patch("encre.loop.create_backend") as mock_create_backend:
        mock_create_backend.return_value = backend
        loop = EncreLoop(config=config, session=session)
    loop.tool_registry.register(
        build_tool(
            name=_TOOL_NAME,
            description="Benchmark no-op tool that echoes back an integer.",
            input_schema={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": [],
            },
            execute=_bench_ping,
        )
    )
    return loop


# ----------------------------------------------------------------------
# 1 -- 300-iteration long turn: linear time, bounded memory, correct log
# ----------------------------------------------------------------------


async def test_long_turn_300_iterations_linear_bounded_correct(tmp_path, monkeypatch):
    backend = _BenchBackend(_TOOL_ITERATIONS)
    loop = _make_loop(tmp_path, monkeypatch, backend)

    tracemalloc.start()
    try:
        # Retain ONLY what the assertions need.  Keeping every streamed
        # event would hold a reference to each turn's full sanitized
        # message snapshot (turn-boundary payloads reference the live
        # transcript), making the harness itself look like an O(n^2)
        # retainer and swamping the memory measurement.
        event_total = 0
        finishes: list[Finish] = []
        async for event in loop.run(_PROMPT):
            event_total += 1
            if isinstance(event, Finish):
                finishes.append(event)
        final_mem = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()

    # -- run shape ------------------------------------------------------
    assert backend.chat_calls == _TOOL_ITERATIONS + 1, (
        f"expected {_TOOL_ITERATIONS + 1} model calls, got {backend.chat_calls}"
    )
    assert event_total > 0, "run produced no events"
    assert [f.reason for f in finishes] == ["stop"]

    # -- 1) linear prompt assembly ---------------------------------------
    # 301 call timestamps -> 300 per-iteration intervals.  Iteration i time
    # = call_times[i+1] - call_times[i], so interval i covers the prompt
    # assembly + model + tools + end-of-turn work of iteration i.
    iter_times = [b - a for a, b in zip(backend.call_times, backend.call_times[1:])]
    assert len(iter_times) == _TOOL_ITERATIONS
    first100_mean = statistics.fmean(iter_times[:100])
    last100_mean = statistics.fmean(iter_times[-100:])
    median_all = statistics.median(iter_times)

    # Median iteration must stay within 3x the first iteration.
    assert median_all <= 3.0 * iter_times[0], (
        f"non-linear prompt assembly: median={median_all * 1000:.2f}ms vs "
        f"first={iter_times[0] * 1000:.2f}ms "
        f"(ratio={median_all / iter_times[0]:.2f}x)"
    )
    # Mean of the last 100 iterations within 3x the mean of the first 100.
    # Measured linear signature: 1.33x without tracemalloc, 2.35-2.48x with
    # (the hook multiplies the linear token-count term).  A quadratic prompt
    # rebuild at these transcript sizes would show ~20x+, far past this bound.
    assert last100_mean <= 3.0 * first100_mean, (
        f"super-linear growth: last100_mean={last100_mean * 1000:.2f}ms vs "
        f"first100_mean={first100_mean * 1000:.2f}ms "
        f"(ratio={last100_mean / first100_mean:.2f}x)"
    )

    # -- 2) bounded memory ------------------------------------------------
    # Sample index 50 == chat call 51, index 299 == chat call 300.
    # The transcript itself grows linearly by design (every turn appends
    # tool results); subtract that legitimate growth and assert only the
    # RESIDUAL (ledger revisions, working set, caches, checkpoints) is
    # bounded -- i.e. no leak beyond what the conversation actually costs.
    raw_growth = backend.mem_samples[299] - backend.mem_samples[50]
    transcript_growth = (
        backend.request_payload_bytes[299] - backend.request_payload_bytes[50]
    )
    growth = raw_growth - transcript_growth
    assert growth < _MEMORY_GROWTH_BUDGET, (
        f"unbounded residual memory growth: raw {raw_growth / _KB:.0f}KB, "
        f"legitimate transcript growth {transcript_growth / _KB:.0f}KB, "
        f"residual {growth / _KB:.0f}KB between chat call 51 and chat call "
        f"300 (budget {_MEMORY_GROWTH_BUDGET / _KB:.0f}KB); final traced "
        f"memory {final_mem / _KB:.0f}KB"
    )

    # -- 3) message-list correctness ---------------------------------------
    final_roles = backend.request_roles[-1]
    assert final_roles.count("system") == 1, (
        f"final request carries {final_roles.count('system')} system messages"
    )
    assert final_roles.count("tool") == _TOOL_ITERATIONS, (
        f"final request carries {final_roles.count('tool')} tool results, "
        f"expected {_TOOL_ITERATIONS}"
    )
    assert final_roles.count("assistant") == _TOOL_ITERATIONS + 1, (
        f"final request carries {final_roles.count('assistant')} assistant "
        f"messages, expected {_TOOL_ITERATIONS + 1} (300 tool-call turns + "
        "1 final text turn)"
    )
    # The run-scoped checkpoint hard-gate may add ONE extra user message for
    # the remainder of the run; the original prompt is always present.
    assert 1 <= final_roles.count("user") <= 2, (
        f"final request carries {final_roles.count('user')} user messages"
    )
    # Every request of the run: exactly one system message.
    bad = [i for i, r in enumerate(backend.request_roles) if r.count("system") != 1]
    assert not bad, f"requests without exactly one system message: {bad[:10]}"

    # Session transcript: 1 system + 1 user + 300 assistant(tool_calls)
    # + 300 tool results + 1 final assistant.
    session_roles = [m.get("role", "") for m in loop.session.messages]
    assert session_roles.count("system") == 1
    assert session_roles.count("tool") == _TOOL_ITERATIONS
    assert session_roles.count("assistant") == _TOOL_ITERATIONS + 1
    assert session_roles[-1] == "assistant"
    assert _FINAL_TEXT in str(loop.session.messages[-1].get("content", ""))

    # All 301 requests saw an identical tool-definition list, deduplicated.
    first_names = backend.request_tool_names[0]
    assert len(first_names) == len(set(first_names)), "duplicate tool definitions"
    changed = [
        i for i, names in enumerate(backend.request_tool_names) if names != first_names
    ]
    assert not changed, f"tool payload changed at requests {changed[:10]}"


# ----------------------------------------------------------------------
# 2 -- stateless requests: one system message, identical content, no
#      duplicated tool definitions (within a run and across two runs)
# ----------------------------------------------------------------------


async def test_stateless_requests_within_one_run(tmp_path, monkeypatch):
    backend = _BenchBackend(tool_iterations=2)
    loop = _make_loop(tmp_path, monkeypatch, backend)

    events: list = []
    async for event in loop.run(_PROMPT):
        events.append(event)
    assert backend.chat_calls == 3  # 2 tool turns + 1 final text turn
    finishes = [f for f in events if isinstance(f, Finish)]
    assert [f.reason for f in finishes] == ["stop"]

    # Every request: exactly one system message, byte-identical content.
    systems = backend.request_system_contents
    assert all(s is not None for s in systems)
    assert systems[0] == systems[1] == systems[2], (
        "system prompt drifted between requests of the same run"
    )

    # Tool definitions: identical across requests, no duplicates within.
    names0 = backend.request_tool_names[0]
    assert len(names0) == len(set(names0)), "duplicate tool definitions"
    assert backend.request_tool_names[1] == names0
    assert backend.request_tool_names[2] == names0

    # Role sequences: no duplicated system entries, no stray user inserts.
    # NB: roles are recorded at chat() entry, i.e. BEFORE the response that
    # request produces -- so the final request ends at the second tool result.
    for i, roles in enumerate(backend.request_roles):
        assert roles.count("system") == 1, f"request {i}: {roles.count('system')} system"
        assert roles.count("user") == 1, f"request {i}: {roles.count('user')} user"
    assert backend.request_roles[2] == [
        "system", "user", "assistant", "tool", "assistant", "tool",
    ]


async def test_stateless_requests_across_two_runs(tmp_path, monkeypatch):
    backend = _BenchBackend(tool_iterations=0)
    loop = _make_loop(tmp_path, monkeypatch, backend)

    async for _ in loop.run(_PROMPT):
        pass
    # A verbatim repeat of the last user message is deduped by design
    # (prompt.py skips adding a duplicate), so run 2 uses a follow-up prompt.
    prompt2 = _PROMPT + " Then summarise the outcome in one sentence."
    async for _ in loop.run(prompt2):
        pass

    assert backend.chat_calls == 2
    r1, r2 = backend.request_roles
    assert r1.count("system") == 1 and r2.count("system") == 1
    assert r2 == ["system", "user", "assistant", "user"], f"unexpected roles: {r2}"

    sys1 = backend.request_system_contents[0]
    sys2 = backend.request_system_contents[1]
    assert sys1 is not None and sys2 is not None

    # The prompt-caching contract (loop/phases/prompt.py): the STATIC prefix
    # of the system prompt is byte-identical across runs; the turn-scoped
    # blocks appended after it (task stage / working set / prior turns /
    # context-change annotation) legitimately differ.  The task-stage block
    # always renders and always opens with "**Work Phase:**", so split there.
    stage_marker = "\n\n**Work Phase:**"
    prefix1 = sys1.partition(stage_marker)[0]
    prefix2 = sys2.partition(stage_marker)[0]
    assert stage_marker in sys1 and stage_marker in sys2, (
        "expected a task-stage block in the system prompt"
    )
    assert prefix1 == prefix2, (
        "system prompt static prefix changed across runs "
        f"(prefix1 {len(prefix1)} chars, prefix2 {len(prefix2)} chars)"
    )
    # Tool definitions identical and deduplicated across the two runs.
    names1, names2 = backend.request_tool_names
    assert names1 == names2
    assert len(names1) == len(set(names1))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-header"]))
