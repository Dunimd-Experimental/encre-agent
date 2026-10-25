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

"""System-level acceptance: growth (Task 10.3) -- deterministic quantitative evidence
for memory and evolution.

Core invariant, never violated anywhere in the project: **context is not memory**.
Memory is durable cognition retained only after the agent decides at runtime that
something is meaningful to it, and the :class:`SignificanceJudge` gate is the only
door through which writes may enter.

**What this module proves, stated honestly**

This environment **cannot call a real LLM**, so this module never claims an
improvement in real task success rate. It supplies only **deterministic,
reproducible, offline-runnable** quantitative evidence that:

1. **Memory growth is bounded**: after ``intake`` + ``consolidate``, repeating the
   same kind of experience N times (10/50/200) always yields a long-term memory
   count of exactly 1 logical entry (the repeated pattern merges into one), with no
   linear growth in N, contrasted against naive accumulation (N entries).
2. **Retrieval coverage rises with accumulation but never explodes**: associative
   spread in a real :class:`RecallEngine` lifts the hit count for the same query as
   links accumulate, strictly capped by ``limit`` / ``max_hops``.
3. **The significance gate works (signal-to-noise)**: within one mixed batch,
   one-off noise produces **no** candidates at all, while user corrections, errors,
   decisions, and repeated patterns do; the real ratio is reported.
4. **Forgetting is bounded and auditable**: after ``apply_decay`` the archive
   fraction rises monotonically with real time; archived entries are
   unsearchable by default, **still auditable**, and **never physically deleted**
   from disk.
5. **Evolution gating is bounded**: under a real :class:`SkillVerifier`, bad
   artifacts **never enter** ``active()``, the active set size is capped by a real
   count, and long-term behaviour stays unpolluted.

**What this module does not prove**

The evidence above only shows that "memory/evolution mechanisms keep **costs
bounded, coverage rising, and signal-to-noise controlled** under deterministic
workloads"; it is **not** equivalent to "real LLM task success improves", nor does
it prove "the cost curve does not degrade" under real inference load. Every
accepted capability uses real components: a real
:class:`~encre.memdir.layers.MemoryLayerStore`, real file I/O (encrypted writes to
disk), a real :class:`~encre.memdir.recall.RecallEngine`, a real
:class:`~encre.memdir.judgment.SignificanceJudge`, a real
:class:`~encre.improve.verification.SkillVerifier`; no mocks are passed off as
stand-ins.
"""

import time

import pytest

from encre.improve.verification import SkillStatus, SkillVerifier
from encre.memdir.judgment import Experience, MemoryCandidate, SignificanceJudge
from encre.memdir.layers import (
    ARCHIVE_DECAY_THRESHOLD,
    DECAY_HALF_LIFE_SECONDS,
    DecayState,
    LinkRelation,
    MemoryEntry,
    MemoryLayerStore,
)
from encre.memdir.recall import RecallEngine
from encre.secure_io import read_jsonl
from encre.skills.registry import EncreSkillRegistry
from encre.tools.base import build_tool
from encre.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Test isolation: the data directory always points at tmp_path and never
# pollutes the real user data directory.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Point ENCRE_DATA_DIR at a temporary directory so tests are isolated and order-independent."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "encre-data"))
    return tmp_path


def _candidate(
    content: str,
    *,
    significance: float = 0.7,
    suggested_type: str = "fact",
    session_id: str = "sess-growth",
) -> MemoryCandidate:
    """Build an already-approved candidate in the exact shape the judge publishes."""
    return MemoryCandidate(
        content=content,
        significance=significance,
        reasons=["repeated_pattern (w=0.75, s=1.00)"],
        suggested_type=suggested_type,
        session_id=session_id,
    )


def _revision_lines(store: MemoryLayerStore, layer: str = "fact") -> int:
    """Return the non-blank line count of a layer's JSONL file on disk (append-only revision log)."""
    path = store.layers_dir / f"{layer}.jsonl"
    if not path.is_file():
        return 0
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


# ===========================================================================
# Dimension A -- memory growth is bounded (repeated patterns merge into a
# single entry; cost does not diverge with the number of rounds)
# ===========================================================================


class TestBoundedMemoryGrowth:
    """After repeating one kind of experience N times, the long-term logical entry count is always 1."""

    #: Repetition round gradient: 10 / 50 / 200.
    ROUND_GRADIENT = (10, 50, 200)

    @pytest.mark.parametrize("rounds", ROUND_GRADIENT)
    def test_repeated_experience_merges_to_single_long_term(self, tmp_path, rounds):
        """N rounds of repeated experience -> 1 long-term entry; the naive accumulation control is N."""
        store = MemoryLayerStore(root=tmp_path / f"memory-{rounds}")
        text = "部署流程固定使用蓝环境，不要切换回绿环境"

        for _ in range(rounds):
            store.intake(_candidate(text, suggested_type="lesson"))
        summary = store.consolidate()

        long_term = store.long_term()
        assert len(long_term) == 1, f"{rounds} 轮重复应合并为 1 条长期记忆"
        assert long_term[0].content == text
        assert store.buffer() == []
        # Real counts: naive accumulation (one entry per experience) is N; after consolidation it is 1.
        assert summary["long_term"] == 1
        assert summary["long_term"] < rounds
        assert len(store.long_term()) < rounds
        # Repetition is really observed: access_count accumulates across rounds (evidence of the merge).
        assert long_term[0].decay.access_count == rounds

    def test_logical_growth_is_flat_across_gradient(self, tmp_path):
        """Cross-gradient comparison: logical entry count stays flat while naive accumulation grows linearly."""
        measured: dict[int, int] = {}
        naive: dict[int, int] = {}
        for rounds in self.ROUND_GRADIENT:
            store = MemoryLayerStore(root=tmp_path / f"gradient-{rounds}")
            text = "每次发布前必须先跑数据库迁移，再启动服务"
            for _ in range(rounds):
                store.intake(_candidate(text, suggested_type="lesson"))
            store.consolidate()
            measured[rounds] = len(store.long_term())
            naive[rounds] = rounds

        # After consolidation: all three gradients are 1 (flat); naive accumulation is 10 / 50 / 200 (divergent).
        assert measured == {10: 1, 50: 1, 200: 1}
        assert naive == {10: 10, 50: 50, 200: 200}
        # At 200 rounds: 1 logical entry vs 200 naively accumulated (a real 200x multiple).
        assert naive[200] // measured[200] == 200

    def test_append_only_revision_log_is_the_real_storage_cost(self, tmp_path):
        """Honest accounting: logical entries are flat, but on disk it is an append-only revision log (grows by line).

        This is an explicit part of the spec design (audit trail: every
        ``MemoryEntry`` merge/promotion appends one revision, and on reload "the
        last entry wins"); it is not a defect. However, "the cost curve is flat"
        holds only for **logical entries / the retrieval set**, not for the
        **number of revision lines on disk**. This test pins down both real
        numbers so the conclusion is not overstated.
        """
        rounds = 50
        store = MemoryLayerStore(root=tmp_path / "memory-revisions")
        text = "所有对外请求都必须带 trace id，方便排查"
        for _ in range(rounds):
            store.intake(_candidate(text, suggested_type="lesson"))

        logical = len(store.long_term())
        disk_lines = _revision_lines(store, "lesson")
        assert logical == 1
        # The revision log really grows with observation count: 1 entry for the first promotion + (rounds-1) merge appends.
        assert disk_lines == rounds - 1
        assert disk_lines > logical
        # But after reload only 1 visible entry remains (the last revision wins, with no duplicates).
        store.reload()
        assert len(store.long_term()) == 1


# ===========================================================================
# Dimension B -- retrieval coverage rises with accumulation and is capped by
# an upper bound (memory takes effect)
# ===========================================================================


class TestRecallCoverageGrowth:
    """Associative spread lifts the hit count as links accumulate, strictly bounded and never explosive."""

    #: The only memory content that matches the query (the seed).
    _SEED_TEXT = "quantum entanglement protocol requires calibrated detectors"
    #: Leaf content shares no overlap with the query and is reachable only via spread activation.
    _LEAF_TEMPLATE = "the cafeteria menu rotates every tuesday afternoon item {i}"

    def _build_store(self, root) -> tuple[MemoryLayerStore, MemoryEntry]:
        """Create a store seeded with one canonical memory entry and return both."""
        store = MemoryLayerStore(root=root)
        seed = MemoryEntry(
            content=self._SEED_TEXT, type="fact", importance=0.8,
        )
        store.store(seed)
        return store, seed

    def test_recall_hits_grow_then_saturate_at_limit(self, tmp_path):
        """Hit count: 1 (seed only) -> growth -> capped at limit=10."""
        store, seed = self._build_store(tmp_path / "memory-recall")
        engine = RecallEngine(store, record_access=False, limit=10, max_hops=2)

        leaves: list[MemoryEntry] = []
        counts: list[int] = []
        hop_bounds: list[int] = []

        for target in (0, 3, 20):
            while len(leaves) < target:
                leaf = MemoryEntry(
                    content=self._LEAF_TEMPLATE.format(i=len(leaves)),
                    type="fact",
                    importance=0.6,
                )
                store.store(leaf)
                # Real link: seed --causal--> leaf, used by spread activation.
                assert store.link(
                    seed.id, leaf.id, LinkRelation.CAUSES.value, weight=1.0,
                ) is not None
                leaves.append(leaf)

            hits = engine.recall(self._SEED_TEXT)
            counts.append(len(hits))
            hop_bounds.append(max((h.hops for h in hits), default=0))

        # Real numbers: 0 leaves -> 1 hit; 3 leaves -> 4; 20 leaves -> capped at 10.
        assert counts[0] == 1
        assert counts[1] == 4
        assert counts[2] == 10
        assert counts[2] <= 10  # No explosion: capped by limit
        # Spread hop depth is constrained by max_hops=2.
        assert max(hop_bounds) <= 2

    def test_spread_hits_are_traceable_to_the_seed(self, tmp_path):
        """Spread hits must be traceable: they carry a via_links path and an origin pointing at the seed."""
        store, seed = self._build_store(tmp_path / "memory-trace")
        leaf = MemoryEntry(
            content=self._LEAF_TEMPLATE.format(i=0), type="fact", importance=0.6,
        )
        store.store(leaf)
        store.link(seed.id, leaf.id, LinkRelation.CAUSES.value, weight=1.0)

        engine = RecallEngine(store, record_access=False, limit=10, max_hops=2)
        hits = engine.recall(self._SEED_TEXT)
        spread = [h for h in hits if h.hops > 0]

        assert spread, "扩散激活必须至少带回一个非种子命中"
        assert all(h.origin_id == seed.id for h in spread)
        assert all(h.via_links and h.via_links[-1]["relation"] == "causes" for h in spread)


# ===========================================================================
# Dimension C -- the significance gate is effective (signal-to-noise controlled)
# ===========================================================================


class TestSignificanceGate:
    """One-off noise yields no candidate; meaningful signals do; the real ratio is reported."""

    #: 8 one-off, task-irrelevant intermediate outputs (noise).
    _NOISE = tuple(
        Experience(
            kind="tool_result",
            content=f"the build output line {i} contains ordinary text",
            session_id="sess-noise",
        )
        for i in range(8)
    )
    #: 4 meaningful signals: correction / error / decision / repeat (a repeat only qualifies on its second occurrence).
    _CORRECTION = Experience(
        kind="user_message", content="不要用 3000 端口，改成 8080", session_id="sess-signal",
    )
    _ERROR = Experience(
        kind="error",
        content="connection refused while contacting the staging database",
        session_id="sess-signal",
    )
    _DECISION = Experience(
        kind="decision",
        content="we decided to ship the unified memory layers",
        session_id="sess-signal",
    )
    _REPEATED_TEXT = "the deploy script runs database migrations before serving traffic"

    def test_noise_yields_no_candidate_while_signal_does(self):
        """Real judge: 8/8 noise rejected, and the meaningful signals produce 4 candidates."""
        judge = SignificanceJudge()

        # Noise: judged one by one, all None (no memory written at all).
        noise_candidates = [judge.judge(exp) for exp in self._NOISE]
        assert noise_candidates == [None] * len(self._NOISE)

        # Meaningful signals: correction / error / decision each produce a candidate.
        assert judge.judge(self._CORRECTION) is not None
        assert judge.judge(self._ERROR) is not None
        assert judge.judge(self._DECISION) is not None

        # Repeated pattern: the first occurrence does not qualify, the second does (repeated_pattern).
        first = Experience(kind="tool_result", content=self._REPEATED_TEXT, session_id="sess-signal")
        second = Experience(kind="tool_result", content=self._REPEATED_TEXT, session_id="sess-signal")
        assert judge.judge(first) is None
        judge.record(first)
        repeated = judge.judge(second)
        assert repeated is not None
        assert "repeated_pattern" in repeated.reasons[0]

    def test_gate_ratio_through_real_store_pipeline(self, tmp_path):
        """Real pipeline: judge -> store.intake, zero writes for noise, all meaningful signals land in the buffer."""
        judge = SignificanceJudge()
        store = MemoryLayerStore(root=tmp_path / "memory-gate")

        batch = list(self._NOISE) + [self._CORRECTION, self._ERROR, self._DECISION]
        batch += [
            Experience(kind="tool_result", content=self._REPEATED_TEXT, session_id="sess-signal"),
            Experience(kind="tool_result", content=self._REPEATED_TEXT, session_id="sess-signal"),
        ]
        candidates = judge.judge_batch(batch)
        for candidate in candidates:
            store.intake(candidate)

        # Real ratio: 13 experiences -> 4 candidates (all 8/8 noise rejected).
        assert len(batch) == 13
        assert len(candidates) == 4
        assert len(store.buffer()) == 4
        assert store.long_term() == []

        # Noise content never enters any layer (buffer or long-term).
        stored_text = {entry.content for entry in store.buffer() + store.long_term()}
        for exp in self._NOISE:
            assert exp.content not in stored_text


# ===========================================================================
# Dimension D -- forgetting is bounded (archived is unsearchable but
# auditable, and never physically deleted)
# ===========================================================================


class TestBoundedForgetting:
    """Archive fraction rises monotonically with time; archived is unsearchable, auditable, and still on disk."""

    _BASE = 1_700_000_000.0
    _DAY = 86400.0

    def _seed(self, root) -> tuple[MemoryLayerStore, MemoryEntry, MemoryEntry]:
        """Create a store holding one low-importance and one high-importance entry, both aged identically."""
        store = MemoryLayerStore(root=root)
        low = MemoryEntry(
            content="a low importance transient observation about weather",
            type="fact", importance=0.2,
            decay=DecayState(last_access=self._BASE, access_count=1),
        )
        high = MemoryEntry(
            content="a high importance durable commitment to reproducibility",
            type="fact", importance=0.9,
            decay=DecayState(last_access=self._BASE, access_count=1),
        )
        store.store(low)
        store.store(high)
        return store, low, high

    def test_archive_fraction_rises_monotonically_and_never_deletes(self, tmp_path):
        """Real decay: 20 days archives 1/2, 40 days archives 2/2; disk records only grow, never shrink."""
        store, low, high = self._seed(tmp_path / "memory-decay")
        before_lines = _revision_lines(store)

        first = store.apply_decay(now=self._BASE + 20 * self._DAY)
        score_20d_low = store.get(low.id).decay.decay_score
        score_20d_high = store.get(high.id).decay.decay_score
        fraction_20d = len(store.archived()) / len(store.long_term())
        second = store.apply_decay(now=self._BASE + 40 * self._DAY)
        fraction_40d = len(store.archived()) / len(store.long_term())

        assert first["scanned"] == 2
        assert first["archived"] == [low.id]
        assert fraction_20d == 0.5
        assert fraction_40d == 1.0
        assert fraction_40d >= fraction_20d  # Monotonically non-decreasing
        assert second["already_archived"] == 1

        # Real decay formula check: score = importance * 0.5 ** (age / half_life).
        age = 20 * self._DAY
        expected_low = 0.2 * 0.5 ** (age / DECAY_HALF_LIFE_SECONDS)
        assert abs(score_20d_low - expected_low) < 1e-6
        assert score_20d_low < ARCHIVE_DECAY_THRESHOLD
        assert score_20d_high >= ARCHIVE_DECAY_THRESHOLD

        # Archiving is not physical deletion: revision lines on disk only grow, and content survives reload.
        assert _revision_lines(store) >= before_lines
        store.reload()
        archived = store.archived()
        assert {entry.id for entry in archived} == {low.id, high.id}
        assert {entry.content for entry in archived} == {low.content, high.content}
        assert len(read_jsonl(store.layers_dir / "semantic.jsonl")) >= before_lines

    def test_archived_is_invisible_to_recall_but_auditable(self, tmp_path):
        """Default recall cannot see archived entries; include_archived=True makes them auditable."""
        store, low, _high = self._seed(tmp_path / "memory-audit")
        store.apply_decay(now=self._BASE + 40 * self._DAY)
        assert low.decay.archived is True

        engine = RecallEngine(store, record_access=False)
        visible = engine.recall(low.content)
        assert all(not hit.entry.decay.archived for hit in visible)

        audit = engine.recall(low.content, include_archived=True)
        assert any(hit.entry.id == low.id for hit in audit)
        assert {entry.id for entry in engine.archived()} == {low.id, _high.id}


# ===========================================================================
# Dimension E -- evolution gating is bounded (bad artifacts never enter
# active)
# ===========================================================================


async def _noop(**kwargs) -> str:
    """Minimal async tool body that satisfies the tool registration contract."""
    return ""


def _make_tool(name: str):
    """Build a real tool object, i.e. an actual ToolRegistry registration item."""
    return build_tool(
        name=name,
        description=f"{name} tool",
        input_schema={"type": "object", "properties": {}},
        execute=_noop,
    )


class _Agent:
    """Minimal agent that exposes only the two real registries the verifier needs."""

    def __init__(self, tool_names: tuple[str, ...]) -> None:
        """Register each named tool in a real ToolRegistry and create an empty skill registry."""
        self.tool_registry = ToolRegistry()
        for name in tool_names:
            self.tool_registry.register(_make_tool(name))
        self.skill_registry = EncreSkillRegistry()


def _skill_def(
    name: str,
    tools: tuple[str, ...],
    *,
    description: str = "Automatically generated skill from repeated tool use.",
    body: str | None = None,
) -> dict:
    """Build a skill definition with the same shape as SkillGenerator output."""
    if body is None:
        tool_lines = "\n".join(f"- `{t}` (used 3 times)" for t in tools)
        body = (
            f"# {name}\n\n{description}\n\n## When to Use\n\nsource files\n\n"
            f"## Tools\n\n{tool_lines}\n\n## Steps\n\n- Do the thing\n"
        )
    return {
        "name": name,
        "description": description,
        "aliases": [name],
        "source": "auto_generated",
        "when_to_use": "source files, logs, configuration",
        "body": body,
        "generated_at": time.time(),
        "tool_names": list(tools),
        "reason": "Repeated tool-use pattern detected",
    }


class TestEvolutionGateBounds:
    """Real SkillVerifier: good artifacts enter active, all bad artifacts are rejected."""

    _TOOLS = ("file_read", "file_write", "bash", "flaky_tool")

    def test_mixed_proposals_bound_the_active_set(self, tmp_path):
        """7 proposals -> exactly 2 active and exactly 5 rejected (real counts)."""
        agent = _Agent(self._TOOLS)
        verifier = SkillVerifier(
            agent,
            skills_dir=tmp_path / "skills",
            # Real failure-rate source: flaky_tool has a historical failure rate of 0.9, above the 0.5 ceiling.
            failure_stats=lambda: {"flaky_tool": 0.9},
        )

        good_one = verifier.propose(_skill_def("auto-good-one", ("file_read",)))
        good_two = verifier.propose(_skill_def("auto-good-two", ("file_write",)))
        bad_unknown = verifier.propose(_skill_def("auto-bad-unknown", ("does_not_exist",)))
        bad_no_tools = verifier.propose(
            _skill_def(
                "auto-bad-no-tools", (),
                body="# auto-bad-no-tools\n\nno tools listed here\n",
            )
        )
        bad_name = verifier.propose(_skill_def("Bad Name!", ("file_read",)))
        bad_history = verifier.propose(_skill_def("auto-bad-history", ("flaky_tool",)))
        # Same tool signature as good-one -> no_conflict failure (duplicate).
        duplicate = verifier.propose(_skill_def("auto-duplicate", ("file_read",)))

        results = verifier.verify_pending()

        assert len(verifier.all_artifacts()) == 7
        assert len(results) == 7
        assert {a.name for a in verifier.active()} == {"auto-good-one", "auto-good-two"}
        assert len(verifier.active()) == 2
        assert len(verifier.rejected()) == 5
        assert verifier.pending() == []

        # Bad artifacts never enter active, and each carries its real failure reason.
        for bad, expected_check in (
            (bad_unknown, "tools_exist"),
            (bad_no_tools, "schema"),
            (bad_name, "schema"),
            (bad_history, "history"),
            (duplicate, "no_conflict"),
        ):
            assert bad.status == SkillStatus.REJECTED
            assert bad.name not in {a.name for a in verifier.active()}
            assert bad.verification is not None and bad.verification.passed is False
            failed = {c.name for c in bad.verification.checks if not c.passed}
            assert expected_check in failed

        # Long-term behaviour is not polluted: only the 2 good artifacts are registered in the real skill registry.
        assert agent.skill_registry.lookup(good_one.name) is not None
        assert agent.skill_registry.lookup(good_two.name) is not None
        for bad in (bad_unknown, bad_no_tools, bad_name, bad_history, duplicate):
            assert agent.skill_registry.lookup(bad.name) is None

    def test_rejected_artifacts_survive_on_disk_for_audit(self, tmp_path):
        """Rejected artifacts remain on disk (auditable) and are not lost because verification failed."""
        agent = _Agent(self._TOOLS)
        skills_dir = tmp_path / "skills"
        verifier = SkillVerifier(agent, skills_dir=skills_dir)

        bad = verifier.propose(_skill_def("auto-bad-unknown", ("does_not_exist",)))
        verifier.verify(bad)
        assert bad.status == SkillStatus.REJECTED

        store_path = skills_dir / "proposed.json"
        assert store_path.is_file()
        # A fresh instance reloads from disk: the rejected artifact is still auditable.
        reloaded = SkillVerifier(agent, skills_dir=skills_dir)
        assert any(
            a.name == "auto-bad-unknown" and a.status == SkillStatus.REJECTED
            for a in reloaded.all_artifacts()
        )
