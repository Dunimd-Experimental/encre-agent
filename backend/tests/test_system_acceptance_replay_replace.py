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

from __future__ import annotations

"""系统级验收：可回放（Task 10.1）与可替换（Task 10.2）。

本模块不是新功能测试，而是证明 Encre 0.5.0 基座的两条性质成立：

* **可回放**：真实 :class:`~encre.events.ledger.EventSourcedBus` 落盘的事件，在
  新实例中 ``replay`` 后逐条一致、幂等、且**无重复副作用**（回放绝不重新执行
  工具、绝不重复写入记忆），并能在"进程崩溃"后从盘上恢复。
* **可替换**：替换 backend / 记忆后端 / 工具集后，能力规划与调用链仍自洽。

硬性约束（与 spec 对齐）：被验收的能力一律使用真实组件——真实 bus、真实文件
I/O、真实 :class:`~encre.memdir.layers.MemoryLayerStore`、真实
:class:`~encre.tools.registry.ToolRegistry`、真实 backend 实例；不使用 mock 冒充。
"""

import pytest

from encre.backends.adaptive import (
    apply_to_messages,
    apply_to_tools,
    compute_plan,
    tool_intent_fallback,
)
from encre.backends.local import LocalBackend
from encre.backends.ollama import OllamaBackend
from encre.backends.registry import resolve_model_info
from encre.events.ledger import EventSourcedBus, LedgerEvent
from encre.memdir.judgment import MemoryCandidate
from encre.memdir.layers import LinkRelation, MemoryEntry, MemoryLayerStore
from encre.memdir.recall import RecallEngine
from encre.secure_io import read_json, read_jsonl
from encre.tools.builtin.file_edit import file_edit_tool
from encre.tools.builtin.file_read import file_read_tool
from encre.tools.builtin.file_write import file_write_tool
from encre.tools.discovery import ToolDiscovery
from encre.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# 测试隔离：数据目录一律指向 tmp_path，绝不污染真实用户数据目录。
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """把 ENCRE_DATA_DIR 指向临时目录，保证测试互不干扰、无顺序依赖。"""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "encre-data"))
    return tmp_path


def _snapshot(event: LedgerEvent) -> tuple:
    """抽取事件中用于"逐条一致"比对的可观察字段。"""
    return (
        event.seq,
        event.kind,
        event.session_id,
        event.payload,
        list(event.refs),
        event.event_id,
    )


# ===========================================================================
# SubTask 10.1 —— 可回放验收
# ===========================================================================


class TestReplayRoundTrip:
    """落盘 -> 回放一致，且回放幂等、无副作用、跨 session 隔离。"""

    def test_persisted_events_replay_identically(self, tmp_path):
        """真实落盘后，新 bus 实例回放的序列与写入序列逐条一致。"""
        path = tmp_path / "events" / "round_trip.jsonl"
        sid = "sess-round-trip"
        bus = EventSourcedBus("events", ledger_path=path)

        published = [
            # lifecycle 事件（phase 语义）
            bus.publish("phase:session_start", session_id=sid, payload={"model": "gpt-4.1"}),
            bus.publish("turn_started", session_id=sid, payload={"turn": 1}),
            # 自定义 kind + 嵌套 payload
            bus.publish(
                "state_changed",
                session_id=sid,
                payload={"nested": {"a": [1, 2, 3]}, "flag": True, "none": None, "ratio": 0.25},
            ),
            # 工具调用事件（带 refs）
            bus.publish(
                "tool_call",
                session_id=sid,
                payload={"name": "file_read", "arguments": {"file_path": "x.py"}},
                refs=["call_1", "call_2"],
            ),
            # 记忆候选事件（真实 memory_candidate payload 形状）
            bus.publish(
                "memory_candidate",
                session_id=sid,
                payload=MemoryCandidate(
                    content="用户偏好使用中文注释", significance=0.9, session_id=sid,
                ).to_dict(),
            ),
            # 空 session 的全局事件（不应被 session 回放带回）
            bus.publish("phase:pre_model_request", session_id="", payload={"global": True}),
        ]
        assert all(ev is not None for ev in published)
        bus.flush()

        fresh = EventSourcedBus("events", ledger_path=path)
        replayed = fresh.replay(sid)

        expected = [ev for ev in published if ev.session_id == sid]
        assert len(replayed) == len(expected) == 5
        assert [_snapshot(ev) for ev in replayed] == [_snapshot(ev) for ev in expected]
        # 回放顺序按 seq 升序。
        assert [ev.seq for ev in replayed] == sorted(ev.seq for ev in replayed)

    def test_replay_is_idempotent_and_creates_no_events(self, tmp_path):
        """连续两次回放结果完全相同；回放不产生新事件、不改动盘上内容。"""
        path = tmp_path / "events" / "idempotent.jsonl"
        sid = "sess-idem"
        bus = EventSourcedBus("events", ledger_path=path)
        for i in range(4):
            bus.publish("turn_started", session_id=sid, payload={"i": i})
        bus.flush()
        lines_before = len(read_jsonl(path))

        fresh = EventSourcedBus("events", ledger_path=path)
        first = fresh.replay(sid)
        second = fresh.replay(sid)
        assert first == second
        # 回放前后：盘上行数不变、新 bus 内存事件数为 0（未 publish）。
        assert len(read_jsonl(path)) == lines_before
        assert fresh.count == 0

    def test_replay_does_not_duplicate_real_side_effects(self, tmp_path):
        """关键：回放事件流不得重新触发真实副作用。

        真实副作用计数点：真实 :class:`MemoryLayerStore` 通过 ``attach(bus)``
        订阅 ``memory_candidate``，每次订阅回调都会执行真实的 ``intake``
        （写盘、可能合并/晋升）。若回放错误地 fan-out 观察者，重复 intake 会把
        已在短时缓冲区的条目合并并晋升为长期记忆——这可在真实 store 状态上观测。
        """
        ledger = tmp_path / "events" / "side_effect.jsonl"
        mem_root = tmp_path / "memory"
        sid = "sess-side"

        # 第一段：真实 store 订阅真实 bus，publish 触发真实 intake。
        store = MemoryLayerStore(root=mem_root)
        bus = EventSourcedBus("events", ledger_path=ledger)
        store.attach(bus)
        contents = [
            "Deploy pipeline uses Docker containers for releases",
            "The weather in Shanghai is rainy today",
            "Python decorators wrap functions",
        ]
        for text in contents:
            bus.publish(
                "memory_candidate",
                session_id=sid,
                payload=MemoryCandidate(content=text, significance=0.8, session_id=sid).to_dict(),
            )
        bus.flush()

        buffered_ids = {entry.id for entry in store.buffer()}
        assert len(buffered_ids) == 3          # 真实 intake 发生了 3 次
        assert store.long_term() == []         # 尚未晋升
        buffer_records_before = len(read_json(store._buffer_path, default=[]))

        # 第二段：模拟重启——新 bus + 新 store（同 root，从盘 reload），attach 后回放。
        fresh_bus = EventSourcedBus("events", ledger_path=ledger)
        fresh_store = MemoryLayerStore(root=mem_root)
        fresh_store.attach(fresh_bus)
        assert len(fresh_store.buffer()) == 3  # 已从盘恢复短时缓冲区

        replayed = fresh_bus.replay(sid)
        assert len(replayed) == 3

        # 回放若 fan-out，3 条候选会被再次 intake -> 合并 + 晋升；真实状态必须不变。
        assert {entry.id for entry in fresh_store.buffer()} == buffered_ids
        assert fresh_store.long_term() == []
        assert len(read_json(fresh_store._buffer_path, default=[])) == buffer_records_before
        # 回放本身没有 publish 新事件。
        assert fresh_bus.count == 0

    def test_project_matches_manual_fold(self, tmp_path):
        """read-model 投影与按事件流手工推导的结果一致（按其真实语义）。"""
        path = tmp_path / "events" / "project.jsonl"
        sid = "sess-proj"
        bus = EventSourcedBus("events", ledger_path=path)
        bus.publish("turn_started", session_id=sid, payload={"turn": 1})
        bus.publish("phase:turn_start", session_id=sid, payload={"turn": 1})
        bus.publish("turn_start", session_id=sid, payload={"turn": 2})
        bus.publish("state_changed", session_id=sid, payload={"k": "v"})
        bus.publish("tool_call", session_id=sid, refs=["c1"])
        bus.publish("tool_call_start", session_id=sid, refs=["c2", ""])
        bus.publish("tool_calls", session_id=sid, refs=["c3"])
        bus.publish("memory_candidate", session_id=sid, payload={"content": "x"})
        # 另一 session 的事件不得计入。
        bus.publish("turn_started", session_id="other", payload={"turn": 99})
        bus.flush()

        fresh = EventSourcedBus("events", ledger_path=path)
        project = fresh.project(sid)
        events = fresh.replay(sid)

        # 手工按实现语义折叠，作为独立期望。
        turn_kinds = {"turn_started", "turn_start", "phase:turn_start"}
        tool_kinds = {"tool_call", "tool_call_start", "tool_calls"}
        kinds: dict[str, int] = {}
        tool_calls: list[str] = []
        turns = 0
        for ev in events:
            kinds[ev.kind] = kinds.get(ev.kind, 0) + 1
            if ev.kind in turn_kinds:
                turns += 1
            if ev.kind in tool_kinds:
                tool_calls.extend(r for r in ev.refs if r)
        expected = {
            "session_id": sid,
            "events": len(events),
            "last_seq": max(ev.seq for ev in events),
            "kinds": kinds,
            "turns": turns,
            "tool_calls": tool_calls,
        }
        assert project == expected
        # 具体数值断言（显式、可读）。
        assert project["events"] == 8
        assert project["last_seq"] == 8
        assert project["turns"] == 3
        assert project["tool_calls"] == ["c1", "c2", "c3"]

    def test_replay_is_session_scoped(self, tmp_path):
        """同一 ledger 中不同 session_id 的事件互不串味。"""
        path = tmp_path / "events" / "sessions.jsonl"
        bus = EventSourcedBus("events", ledger_path=path)
        bus.publish("turn_started", session_id="A", payload={"n": 1})
        bus.publish("turn_started", session_id="B", payload={"n": 2})
        bus.publish("phase:session_start", session_id="", payload={"n": 0})
        bus.publish("turn_started", session_id="A", payload={"n": 3})
        bus.flush()

        fresh = EventSourcedBus("events", ledger_path=path)
        assert [ev.session_id for ev in fresh.replay("A")] == ["A", "A"]
        assert [ev.payload["n"] for ev in fresh.replay("A")] == [1, 3]
        assert [ev.session_id for ev in fresh.replay("B")] == ["B"]
        # 空 session_id 只匹配全局事件，不是"全部事件"。
        assert [ev.session_id for ev in fresh.replay("")] == [""]


class TestCrashRecovery:
    """模拟进程崩溃（不 close/flush 即丢弃 bus）后的恢复语义。"""

    def test_write_through_recovers_everything(self, tmp_path):
        """flush_batch=1（写透）：崩溃后已落盘事件全部可恢复。"""
        path = tmp_path / "events" / "crash_wt.jsonl"
        bus = EventSourcedBus("events", ledger_path=path, flush_batch=1)
        for i in range(5):
            bus.publish("turn_started", session_id="s", payload={"i": i})
        # 模拟崩溃：不调用 close()/flush()，直接丢弃实例。
        del bus

        fresh = EventSourcedBus("events", ledger_path=path)
        replayed = fresh.replay("s")
        assert [ev.seq for ev in replayed] == [1, 2, 3, 4, 5]
        assert [ev.payload["i"] for ev in replayed] == [0, 1, 2, 3, 4]

    def test_batched_unflushed_events_are_lost_on_crash(self, tmp_path):
        """批量模式：未 flush 的尾部事件在崩溃后丢失（真实语义）。"""
        path = tmp_path / "events" / "crash_batch.jsonl"
        bus = EventSourcedBus("events", ledger_path=path, flush_batch=3)
        for i in range(7):
            bus.publish("turn_started", session_id="s", payload={"i": i})
        # 批量=3：每累计满 3 条落盘一次 -> 已写 6 条，第 7 条仍在 _pending。
        assert len(read_jsonl(path)) == 6
        del bus  # 崩溃，未 flush/close

        fresh = EventSourcedBus("events", ledger_path=path)
        replayed = fresh.replay("s")
        assert [ev.seq for ev in replayed] == [1, 2, 3, 4, 5, 6]
        # 结论：批量模式下未 flush 的事件会丢失；显式 flush/close 才保证落盘。
        assert all(ev.seq != 7 for ev in replayed)

    def test_explicit_flush_recovers_batched_events(self, tmp_path):
        """对照：批量模式下显式 flush 后，尾部事件同样可恢复。"""
        path = tmp_path / "events" / "crash_flush.jsonl"
        bus = EventSourcedBus("events", ledger_path=path, flush_batch=3)
        for i in range(7):
            bus.publish("turn_started", session_id="s", payload={"i": i})
        bus.flush()
        assert len(read_jsonl(path)) == 7
        fresh = EventSourcedBus("events", ledger_path=path)
        assert [ev.seq for ev in fresh.replay("s")] == [1, 2, 3, 4, 5, 6, 7]


# ===========================================================================
# SubTask 10.2 —— 可替换验收
# ===========================================================================


def _real_model_infos() -> list:
    """取真实注册表解析出的模型元数据（含不支持工具/图像的本地类模型）。"""
    return [
        resolve_model_info("gpt-4.1"),                                  # 全能力
        resolve_model_info("claude-sonnet-4.6"),                        # 全能力 + thinking
        resolve_model_info("deepseek-v4-pro"),                          # 不支持图像
        resolve_model_info("gemini-2.5-pro"),                           # 不支持 prompt caching
        resolve_model_info("qwen2.5:7b", provider="ollama"),            # 不支持工具/图像
        resolve_model_info("hf-model", provider="local"),               # 不支持工具/图像
    ]


class TestBackendReplacement:
    """替换 backend 后能力规划与降级输出仍自洽。"""

    def test_compute_plan_matches_real_model_info(self):
        """四维结果与真实 ModelInfo 的能力位一致，降级原因齐备。"""
        infos = _real_model_infos()
        assert len(infos) >= 3
        # 断言覆盖到真实存在的"不支持工具"与"不支持图像"条目。
        assert any(not info.supports_tools for info in infos)
        assert any(not info.supports_images for info in infos)

        for info in infos:
            plan = compute_plan(info)
            assert plan.use_tools is bool(info.supports_tools)
            assert plan.use_vision is bool(info.supports_images)
            assert plan.use_caching is bool(info.supports_prompt_caching)
            assert plan.use_streaming is bool(info.supports_streaming)
            for dim, enabled in (
                ("tools", plan.use_tools),
                ("vision", plan.use_vision),
                ("caching", plan.use_caching),
                ("streaming", plan.use_streaming),
            ):
                if enabled:
                    assert dim not in plan.reasons
                else:
                    assert dim in plan.reasons

    def test_real_backend_capability_is_stricter_and(self):
        """真实 backend 的能力方法与 ModelInfo 取"与"（只能更严格）。"""
        capable = resolve_model_info("gpt-4.1")
        assert capable.supports_tools and capable.supports_images and capable.supports_prompt_caching

        # 真实 LocalBackend：未加载模型时 supports_tool_calling() 保守返回 False。
        local = LocalBackend()
        assert local.supports_tool_calling() is False
        plan = compute_plan(capable, backend=local)
        assert plan.use_tools is False
        assert "backend does not support tool calling" in plan.reasons["tools"]
        # 未受限的维度保持开启（backend 不能放宽，也不误伤）。
        assert plan.use_vision is True
        assert plan.use_caching is True

        # 真实 OllamaBackend：OpenAI 协议，supports_tool_calling() 为 True。
        ollama = OllamaBackend(model="llama3.2")
        assert ollama.supports_tool_calling() is True
        plan_ok = compute_plan(capable, backend=ollama)
        assert plan_ok.use_tools is True

        # backend 无法把模型本身缺失的能力"放宽"回来。
        weak = resolve_model_info("qwen2.5:7b", provider="ollama")
        assert weak.supports_tools is False
        plan_weak = compute_plan(weak, backend=ollama)
        assert plan_weak.use_tools is False

    def test_degraded_outputs_self_consistent_after_backend_swap(self):
        """替换 backend 后 apply_to_tools / apply_to_messages / fallback 输出合法。"""
        tools_payload = [
            {
                "type": "function",
                "function": {"name": "file_read", "description": "Read a file from disk."},
            },
            {"type": "function", "function": {"name": "bash", "description": "Run a shell command."}},
        ]
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                ],
            }
        ]

        # 降级方案（真实 backend 更严格 + 本地模型不支持工具/图像）。
        weak = resolve_model_info("hf-model", provider="local")
        degraded = compute_plan(weak, backend=LocalBackend())
        assert apply_to_tools(tools_payload, degraded) is None
        stripped = apply_to_messages(messages, degraded)
        assert stripped is not messages
        assert all(part.get("type") != "image_url" for part in stripped[0]["content"])
        fallback = tool_intent_fallback(tools_payload)
        assert "file_read" in fallback and "bash" in fallback

        # 全能力方案：tools 原样透传、消息原样透传。
        capable = resolve_model_info("gpt-4.1")
        ok = compute_plan(capable, backend=OllamaBackend(model="llama3.2"))
        assert apply_to_tools(tools_payload, ok) is tools_payload
        assert apply_to_messages(messages, ok) is messages


class TestMemoryBackendReplacement:
    """替换记忆检索后端后，recall 输出结构合法、可追溯，行为一致。"""

    @staticmethod
    def _seed_store(root) -> tuple[MemoryLayerStore, str, str]:
        """构造带真实关联边的 store：e1 命中查询，e2 只能经扩散激活到达。"""
        store = MemoryLayerStore(root=root)
        e1 = MemoryEntry(
            content="Deploy pipeline uses Docker containers for releases", type="semantic",
        )
        e2 = MemoryEntry(
            content="The release checklist requires a manual approval step", type="semantic",
        )
        store.store(e1)
        store.store(e2)
        # 真实 link：e1 --causal--> e2，供扩散激活使用。
        assert store.link(e1.id, e2.id, LinkRelation.CAUSAL.value, weight=1.0) is not None
        return store, e1.id, e2.id

    def test_local_and_native_backends_both_valid(self, tmp_path):
        store, e1_id, e2_id = self._seed_store(tmp_path / "memory")
        query = "Deploy pipeline uses Docker containers for releases"

        engine_local = RecallEngine(store, backend="local", record_access=False)
        hits_local = engine_local.recall(query)
        assert engine_local.backend == "local"
        assert hits_local, "本地检索必须至少返回一个命中"

        engine_native = RecallEngine(store, backend="native", record_access=False)
        # 未提供 embedding_fn/index 时，native 不可用并静默回退本地（两种环境都能过）。
        assert engine_native.backend in ("local", "native")
        hits_native = engine_native.recall(query)

        for hits in (hits_local, hits_native):
            for hit in hits:
                assert isinstance(hit.entry_id, str) and hit.entry_id
                assert isinstance(hit.score, float) and hit.score >= 0.0
                assert isinstance(hit.hops, int) and hit.hops >= 0
                assert isinstance(hit.via_links, list)
                assert isinstance(hit.origin_id, str) and hit.origin_id
                # 可追溯：扩散命中必须带链接路径与来源种子。
                if hit.hops > 0:
                    assert hit.via_links
                    assert hit.origin_id != hit.entry_id
                    assert all("relation" in edge for edge in hit.via_links)

        # 同一 store、同一 query 下，回退本地时结果与显式 local 完全一致。
        assert engine_native.backend == "local"
        assert [h.entry_id for h in hits_native] == [h.entry_id for h in hits_local]
        # 扩散激活确实把一个非种子记忆带了回来（e2 经 e1 的因果边）。
        spread = [h for h in hits_local if h.hops > 0]
        assert any(h.entry_id == e2_id and h.origin_id == e1_id for h in spread)


class TestToolsetReplacement:
    """替换工具集后，discovery 输出与降级语义随之变化且自洽。"""

    @staticmethod
    def _discovery(*tools) -> ToolDiscovery:
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        return ToolDiscovery(registry)

    def test_active_tools_payload_changes_with_toolset(self):
        one = self._discovery(file_read_tool)
        three = self._discovery(file_read_tool, file_write_tool, file_edit_tool)

        payload_one = one.get_active_tools_payload("sess")
        payload_three = three.get_active_tools_payload("sess")

        names_one = {item["function"]["name"] for item in payload_one}
        names_three = {item["function"]["name"] for item in payload_three}
        assert names_one == {"file_read"}
        assert names_three == {"file_read", "file_write", "file_edit"}
        assert payload_one != payload_three

        # fallback 渲染的工具清单与 discovery 输出一致。
        fallback_one = tool_intent_fallback(payload_one)
        fallback_three = tool_intent_fallback(payload_three)
        assert "file_read" in fallback_one and "file_write" not in fallback_one
        assert all(name in fallback_three for name in names_three)

    def test_degradation_semantics_unaffected_by_toolset(self):
        """工具集被替换不影响降级语义：降级时 apply_to_tools 仍返回 None。"""
        three = self._discovery(file_read_tool, file_write_tool, file_edit_tool)
        payload = three.get_active_tools_payload("sess")

        degraded = compute_plan(resolve_model_info("hf-model", provider="local"))
        assert apply_to_tools(payload, degraded) is None

        ok = compute_plan(resolve_model_info("gpt-4.1"))
        assert apply_to_tools(payload, ok) is payload


class TestCombinedReplacement:
    """三者同时替换后，完整调用链仍无异常、输出结构合法。"""

    def test_combined_swap_is_self_consistent(self, tmp_path):
        # 1) 替换 backend（真实 LocalBackend）+ 模型元数据（真实本地类模型）。
        model_info = resolve_model_info("qwen2.5:7b", provider="ollama")
        backend = LocalBackend()
        plan = compute_plan(model_info, backend=backend)

        # 2) 替换工具集（真实 ToolRegistry + 真实 builtin 工具）。
        registry = ToolRegistry()
        for tool in (file_read_tool, file_write_tool, file_edit_tool):
            registry.register(tool)
        discovery = ToolDiscovery(registry)
        payload = discovery.get_active_tools_payload("sess-combined")
        assert {item["function"]["name"] for item in payload} == {
            "file_read", "file_write", "file_edit",
        }

        # 3) 替换记忆后端（真实 store + native 请求）。
        store = MemoryLayerStore(root=tmp_path / "memory")
        entry = MemoryEntry(content="Combined replacement smoke entry", type="semantic")
        store.store(entry)
        engine = RecallEngine(store, backend="native", record_access=False)

        # 完整调用链：compute_plan -> apply_to_* -> fallback -> recall。
        assert apply_to_tools(payload, plan) is None  # 本地模型降级
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
                ],
            }
        ]
        out_messages = apply_to_messages(messages, plan)
        assert out_messages[0]["content"][0]["type"] == "text"
        assert all(part.get("type") != "image_url" for part in out_messages[0]["content"])
        fallback = tool_intent_fallback(payload)
        assert "file_read" in fallback

        hits = engine.recall("Combined replacement smoke entry")
        assert engine.backend in ("local", "native")
        assert hits and all(h.entry_id for h in hits)
