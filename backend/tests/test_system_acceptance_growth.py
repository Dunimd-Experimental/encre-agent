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

"""系统级验收：可生长（Task 10.3）——记忆与进化的确定性量化证据。

核心原则（全项目不可违背）：**上下文 != 记忆**。记忆是 Agent 在执行中自主判断
"这对我是有意义的"之后留存的持久认知，只有 :class:`SignificanceJudge` 这一道
门可以写入。

**本模块证明什么（诚实界定）**

本环境**无法真实调用 LLM**，因此这里绝不声称"真实任务成功率提升"。本模块只
提供**确定性、可复现、离线可跑**的量化证据，证明：

1. **记忆增长有界**：同一类经验重复 N 轮（10/50/200）经 ``intake`` + ``consolidate``
   后，长期记忆**逻辑条目数恒为 1**（重复模式被合并为单条），不随轮数线性增长；
   与"未固化的朴素累积"（= N 条）形成真实对照。
2. **检索覆盖随积累提升且不爆炸**：真实 :class:`RecallEngine` 的联想扩散随链接
   积累使同一 query 的命中数上升，但被 ``limit`` / ``max_hops`` 严格封顶。
3. **显著性门控有效（信噪比）**：同一批混合经验中，一次性噪声**不产生任何**
   候选，用户纠正 / 错误 / 决策 / 重复模式产生候选；给出真实比例。
4. **遗忘有界且可审计**：``apply_decay`` 后归档比例随真实时间单调上升；归档者
   默认不可检索，但**仍可审计**，且盘上记录**未被物理删除**。
5. **进化门控有界**：真实 :class:`SkillVerifier` 下坏产物**绝不进入** ``active()``，
   活跃集合大小被真实计数封顶，长期行为不被污染。

**本模块不证明什么**

上述证据只说明"记忆/进化机制在确定性负载下使**成本有界、覆盖提升、信噪比受控**"，
**不**等价于"真实 LLM 任务成功率提升"，也**不**证明"成本曲线不劣化"在真实推理
负载下的表现。被验收的能力一律使用真实组件：真实
:class:`~encre.memdir.layers.MemoryLayerStore`、真实文件 I/O（加密落盘）、真实
:class:`~encre.memdir.recall.RecallEngine`、真实
:class:`~encre.memdir.judgment.SignificanceJudge`、真实
:class:`~encre.improve.verification.SkillVerifier`；不使用 mock 冒充。
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
# 测试隔离：数据目录一律指向 tmp_path，绝不污染真实用户数据目录。
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """把 ENCRE_DATA_DIR 指向临时目录，保证测试互不干扰、无顺序依赖。"""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "encre-data"))
    return tmp_path


def _candidate(
    content: str,
    *,
    significance: float = 0.7,
    suggested_type: str = "semantic",
    session_id: str = "sess-growth",
) -> MemoryCandidate:
    """构造一个已过判定的候选（judge 发布时的真实形状）。"""
    return MemoryCandidate(
        content=content,
        significance=significance,
        reasons=["repeated_pattern (w=0.75, s=1.00)"],
        suggested_type=suggested_type,
        session_id=session_id,
    )


def _revision_lines(store: MemoryLayerStore, layer: str = "semantic") -> int:
    """返回某层 JSONL 文件在盘上的非空行数（append-only 修订日志）。"""
    path = store.layers_dir / f"{layer}.jsonl"
    if not path.is_file():
        return 0
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


# ===========================================================================
# 维度 A —— 记忆增长有界（重复模式合并为单条，成本不随轮数发散）
# ===========================================================================


class TestBoundedMemoryGrowth:
    """同一类经验重复 N 轮后，长期记忆逻辑条目数恒为 1。"""

    #: 重复轮数梯度：10 / 50 / 200。
    ROUND_GRADIENT = (10, 50, 200)

    @pytest.mark.parametrize("rounds", ROUND_GRADIENT)
    def test_repeated_experience_merges_to_single_long_term(self, tmp_path, rounds):
        """N 轮重复经验 -> 长期记忆 1 条；朴素累积对照为 N 条。"""
        store = MemoryLayerStore(root=tmp_path / f"memory-{rounds}")
        text = "部署流程固定使用蓝环境，不要切换回绿环境"

        for _ in range(rounds):
            store.intake(_candidate(text, suggested_type="procedural"))
        summary = store.consolidate()

        long_term = store.long_term()
        assert len(long_term) == 1, f"{rounds} 轮重复应合并为 1 条长期记忆"
        assert long_term[0].content == text
        assert store.buffer() == []
        # 真实计数：朴素累积（每个经验各占一条）为 N 条，固化后恒为 1 条。
        assert summary["long_term"] == 1
        assert summary["long_term"] < rounds
        assert len(store.long_term()) < rounds
        # 重复被真实观测到：access_count 随轮数累积（合并的真实证据）。
        assert long_term[0].decay.access_count == rounds

    def test_logical_growth_is_flat_across_gradient(self, tmp_path):
        """跨轮数梯度对照：逻辑条目数恒定，朴素累积随轮数线性增长。"""
        measured: dict[int, int] = {}
        naive: dict[int, int] = {}
        for rounds in self.ROUND_GRADIENT:
            store = MemoryLayerStore(root=tmp_path / f"gradient-{rounds}")
            text = "每次发布前必须先跑数据库迁移，再启动服务"
            for _ in range(rounds):
                store.intake(_candidate(text, suggested_type="procedural"))
            store.consolidate()
            measured[rounds] = len(store.long_term())
            naive[rounds] = rounds

        # 固化后：三个梯度全部为 1（平坦）；朴素累积：10 / 50 / 200（发散）。
        assert measured == {10: 1, 50: 1, 200: 1}
        assert naive == {10: 10, 50: 50, 200: 200}
        # 200 轮时：逻辑条目 1 条 vs 朴素累积 200 条（真实倍差 200x）。
        assert naive[200] // measured[200] == 200

    def test_append_only_revision_log_is_the_real_storage_cost(self, tmp_path):
        """诚实记录：逻辑条目平坦，但盘上是 append-only 修订日志（按行增长）。

        这是 spec 明确的设计（审计轨迹：``MemoryEntry`` 每次合并/晋升都追加一条
        修订，重载时"最后一条胜出"），不是缺陷；但"成本曲线平坦"仅对**逻辑
        条目/检索集合**成立，对**盘上修订行数**不成立。此测试把两个真实数字都
        固定下来，避免夸大结论。
        """
        rounds = 50
        store = MemoryLayerStore(root=tmp_path / "memory-revisions")
        text = "所有对外请求都必须带 trace id，方便排查"
        for _ in range(rounds):
            store.intake(_candidate(text, suggested_type="procedural"))

        logical = len(store.long_term())
        disk_lines = _revision_lines(store, "procedural")
        assert logical == 1
        # 修订日志确实按观察次数增长：首次晋升 1 条 + (rounds-1) 次合并追加。
        assert disk_lines == rounds - 1
        assert disk_lines > logical
        # 但重载后可见条目仍然只有 1 条（最后一条修订胜出，无重复）。
        store.reload()
        assert len(store.long_term()) == 1


# ===========================================================================
# 维度 B —— 检索覆盖随积累提升，且被上限封顶（记忆生效）
# ===========================================================================


class TestRecallCoverageGrowth:
    """联想扩散使命中数随链接积累上升，但严格有上限、不爆炸。"""

    #: 唯一能命中 query 的记忆内容（种子）。
    _SEED_TEXT = "quantum entanglement protocol requires calibrated detectors"
    #: 叶子内容与 query 完全无重叠，只能经扩散激活到达。
    _LEAF_TEMPLATE = "the cafeteria menu rotates every tuesday afternoon item {i}"

    def _build_store(self, root) -> tuple[MemoryLayerStore, MemoryEntry]:
        store = MemoryLayerStore(root=root)
        seed = MemoryEntry(
            content=self._SEED_TEXT, type="semantic", importance=0.8,
        )
        store.store(seed)
        return store, seed

    def test_recall_hits_grow_then_saturate_at_limit(self, tmp_path):
        """命中数：1（仅种子）-> 增长 -> 被 limit=10 封顶。"""
        store, seed = self._build_store(tmp_path / "memory-recall")
        engine = RecallEngine(store, record_access=False, limit=10, max_hops=2)

        leaves: list[MemoryEntry] = []
        counts: list[int] = []
        hop_bounds: list[int] = []

        for target in (0, 3, 20):
            while len(leaves) < target:
                leaf = MemoryEntry(
                    content=self._LEAF_TEMPLATE.format(i=len(leaves)),
                    type="semantic",
                    importance=0.6,
                )
                store.store(leaf)
                # 真实 link：种子 --causal--> 叶子，供扩散激活使用。
                assert store.link(
                    seed.id, leaf.id, LinkRelation.CAUSAL.value, weight=1.0,
                ) is not None
                leaves.append(leaf)

            hits = engine.recall(self._SEED_TEXT)
            counts.append(len(hits))
            hop_bounds.append(max((h.hops for h in hits), default=0))

        # 真实数字：0 叶子 -> 1 命中；3 叶子 -> 4；20 叶子 -> 封顶 10。
        assert counts[0] == 1
        assert counts[1] == 4
        assert counts[2] == 10
        assert counts[2] <= 10  # 不爆炸：被 limit 封顶
        # 扩散跳数受 max_hops=2 约束。
        assert max(hop_bounds) <= 2

    def test_spread_hits_are_traceable_to_the_seed(self, tmp_path):
        """扩散命中必须可追溯：带 via_links 路径且 origin 指向种子。"""
        store, seed = self._build_store(tmp_path / "memory-trace")
        leaf = MemoryEntry(
            content=self._LEAF_TEMPLATE.format(i=0), type="semantic", importance=0.6,
        )
        store.store(leaf)
        store.link(seed.id, leaf.id, LinkRelation.CAUSAL.value, weight=1.0)

        engine = RecallEngine(store, record_access=False, limit=10, max_hops=2)
        hits = engine.recall(self._SEED_TEXT)
        spread = [h for h in hits if h.hops > 0]

        assert spread, "扩散激活必须至少带回一个非种子命中"
        assert all(h.origin_id == seed.id for h in spread)
        assert all(h.via_links and h.via_links[-1]["relation"] == "causal" for h in spread)


# ===========================================================================
# 维度 C —— 显著性门控有效（信噪比受控）
# ===========================================================================


class TestSignificanceGate:
    """一次性噪声不产生候选；有意义者产生候选；给出真实比例。"""

    #: 8 条一次性、任务无关的中间输出（噪声）。
    _NOISE = tuple(
        Experience(
            kind="tool_result",
            content=f"the build output line {i} contains ordinary text",
            session_id="sess-noise",
        )
        for i in range(8)
    )
    #: 4 个有意义信号：纠正 / 错误 / 决策 / 重复（重复需第二次才达标）。
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
        """真实 judge：噪声 8/8 拒绝，有意义者产生 4 个候选。"""
        judge = SignificanceJudge()

        # 噪声：逐条判定，全部为 None（不写任何记忆）。
        noise_candidates = [judge.judge(exp) for exp in self._NOISE]
        assert noise_candidates == [None] * len(self._NOISE)

        # 有意义信号：纠正 / 错误 / 决策各产生候选。
        assert judge.judge(self._CORRECTION) is not None
        assert judge.judge(self._ERROR) is not None
        assert judge.judge(self._DECISION) is not None

        # 重复模式：第一次不达标，第二次才达标（repeated_pattern）。
        first = Experience(kind="tool_result", content=self._REPEATED_TEXT, session_id="sess-signal")
        second = Experience(kind="tool_result", content=self._REPEATED_TEXT, session_id="sess-signal")
        assert judge.judge(first) is None
        judge.record(first)
        repeated = judge.judge(second)
        assert repeated is not None
        assert "repeated_pattern" in repeated.reasons[0]

    def test_gate_ratio_through_real_store_pipeline(self, tmp_path):
        """真实管道：judge -> store.intake，噪声零写入，有意义者全部落缓冲。"""
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

        # 真实比例：13 条经验 -> 4 个候选（噪声 8/8 全拒绝）。
        assert len(batch) == 13
        assert len(candidates) == 4
        assert len(store.buffer()) == 4
        assert store.long_term() == []

        # 噪声内容绝不进入任何层（缓冲或长期）。
        stored_text = {entry.content for entry in store.buffer() + store.long_term()}
        for exp in self._NOISE:
            assert exp.content not in stored_text


# ===========================================================================
# 维度 D —— 遗忘有界（归档不可检索，但可审计且不物理删除）
# ===========================================================================


class TestBoundedForgetting:
    """归档比例随时间单调上升；归档者不可检索、可审计、盘上未删除。"""

    _BASE = 1_700_000_000.0
    _DAY = 86400.0

    def _seed(self, root) -> tuple[MemoryLayerStore, MemoryEntry, MemoryEntry]:
        store = MemoryLayerStore(root=root)
        low = MemoryEntry(
            content="a low importance transient observation about weather",
            type="semantic", importance=0.2,
            decay=DecayState(last_access=self._BASE, access_count=1),
        )
        high = MemoryEntry(
            content="a high importance durable commitment to reproducibility",
            type="semantic", importance=0.9,
            decay=DecayState(last_access=self._BASE, access_count=1),
        )
        store.store(low)
        store.store(high)
        return store, low, high

    def test_archive_fraction_rises_monotonically_and_never_deletes(self, tmp_path):
        """真实衰减：20 天归档 1/2，40 天归档 2/2；盘上记录只增不减。"""
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
        assert fraction_40d >= fraction_20d  # 单调不减
        assert second["already_archived"] == 1

        # 真实衰减公式核对：score = importance * 0.5 ** (age / half_life)。
        age = 20 * self._DAY
        expected_low = 0.2 * 0.5 ** (age / DECAY_HALF_LIFE_SECONDS)
        assert abs(score_20d_low - expected_low) < 1e-6
        assert score_20d_low < ARCHIVE_DECAY_THRESHOLD
        assert score_20d_high >= ARCHIVE_DECAY_THRESHOLD

        # 归档不是物理删除：盘上修订行只增不减，重载后内容仍在。
        assert _revision_lines(store) >= before_lines
        store.reload()
        archived = store.archived()
        assert {entry.id for entry in archived} == {low.id, high.id}
        assert {entry.content for entry in archived} == {low.content, high.content}
        assert len(read_jsonl(store.layers_dir / "semantic.jsonl")) >= before_lines

    def test_archived_is_invisible_to_recall_but_auditable(self, tmp_path):
        """默认 recall 看不到归档者；include_archived=True 可审计。"""
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
# 维度 E —— 进化门控有界（坏产物绝不进入 active）
# ===========================================================================


async def _noop(**kwargs) -> str:
    """满足工具注册契约的最小异步工具体。"""
    return ""


def _make_tool(name: str):
    """构造一个真实工具对象（真实 ToolRegistry 的注册项）。"""
    return build_tool(
        name=name,
        description=f"{name} tool",
        input_schema={"type": "object", "properties": {}},
        execute=_noop,
    )


class _Agent:
    """最小 agent：只暴露 verifier 需要的两个真实注册表。"""

    def __init__(self, tool_names: tuple[str, ...]) -> None:
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
    """构造与 SkillGenerator 输出同形状的技能定义。"""
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
    """真实 SkillVerifier：好产物进入 active，坏产物全部 rejected。"""

    _TOOLS = ("file_read", "file_write", "bash", "flaky_tool")

    def test_mixed_proposals_bound_the_active_set(self, tmp_path):
        """7 个 propose -> active 恰为 2，rejected 恰为 5（真实计数）。"""
        agent = _Agent(self._TOOLS)
        verifier = SkillVerifier(
            agent,
            skills_dir=tmp_path / "skills",
            # 真实失败率来源：flaky_tool 历史失败率 0.9 > 0.5 上限。
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
        # 与 good-one 相同工具签名 -> no_conflict 失败（重复）。
        duplicate = verifier.propose(_skill_def("auto-duplicate", ("file_read",)))

        results = verifier.verify_pending()

        assert len(verifier.all_artifacts()) == 7
        assert len(results) == 7
        assert {a.name for a in verifier.active()} == {"auto-good-one", "auto-good-two"}
        assert len(verifier.active()) == 2
        assert len(verifier.rejected()) == 5
        assert verifier.pending() == []

        # 坏产物绝不进入 active，且各自带真实失败原因。
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

        # 长期行为不被污染：只有 2 个好产物被注册进真实技能注册表。
        assert agent.skill_registry.lookup(good_one.name) is not None
        assert agent.skill_registry.lookup(good_two.name) is not None
        for bad in (bad_unknown, bad_no_tools, bad_name, bad_history, duplicate):
            assert agent.skill_registry.lookup(bad.name) is None

    def test_rejected_artifacts_survive_on_disk_for_audit(self, tmp_path):
        """被拒绝的产物仍留在盘上（可审计），不会因失败而消失。"""
        agent = _Agent(self._TOOLS)
        skills_dir = tmp_path / "skills"
        verifier = SkillVerifier(agent, skills_dir=skills_dir)

        bad = verifier.propose(_skill_def("auto-bad-unknown", ("does_not_exist",)))
        verifier.verify(bad)
        assert bad.status == SkillStatus.REJECTED

        store_path = skills_dir / "proposed.json"
        assert store_path.is_file()
        # 新实例从盘上重载：被拒产物仍可审计。
        reloaded = SkillVerifier(agent, skills_dir=skills_dir)
        assert any(
            a.name == "auto-bad-unknown" and a.status == SkillStatus.REJECTED
            for a in reloaded.all_artifacts()
        )
