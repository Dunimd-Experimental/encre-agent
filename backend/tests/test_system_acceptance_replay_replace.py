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

"""System-level acceptance: replayable (Task 10.1) and replaceable (Task 10.2).

This module is not a test of new features; it demonstrates that two properties of
the Encre 0.5.0 base layer hold:

* **Replayable**: events persisted to disk by a real
  :class:`~encre.events.ledger.EventSourcedBus` match entry by entry after
  ``replay`` in a fresh instance, are idempotent, and have **no duplicate side
  effects** (replay never re-executes a tool and never re-writes memory), and
  survive a simulated process crash by being recovered from disk.
* **Replaceable**: after swapping the backend, the memory backend, or the tool
  set, capability planning and the whole call chain remain self-consistent.

Hard constraint (aligned with the spec): every accepted capability uses real
components -- a real bus, real file I/O, a real
:class:`~encre.memdir.layers.MemoryLayerStore`, a real
:class:`~encre.tools.registry.ToolRegistry`, real backend instances; no mocks are
passed off as stand-ins.
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
# Test isolation: the data directory always points at tmp_path and never
# pollutes the real user data directory.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Point ENCRE_DATA_DIR at a temporary directory so tests are isolated and order-independent."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "encre-data"))
    return tmp_path


def _snapshot(event: LedgerEvent) -> tuple:
    """Extract the observable fields of an event used for entry-by-entry comparison."""
    return (
        event.seq,
        event.kind,
        event.session_id,
        event.payload,
        list(event.refs),
        event.event_id,
    )


# ===========================================================================
# SubTask 10.1 -- replayable acceptance
# ===========================================================================


class TestReplayRoundTrip:
    """Persist -> replay matches, and replay is idempotent, side-effect-free, and session-isolated."""

    def test_persisted_events_replay_identically(self, tmp_path):
        """After real persistence, the sequence replayed by a fresh bus instance matches the written sequence entry by entry."""
        path = tmp_path / "events" / "round_trip.jsonl"
        sid = "sess-round-trip"
        bus = EventSourcedBus("events", ledger_path=path)

        published = [
            # lifecycle events (phase semantics)
            bus.publish("phase:session_start", session_id=sid, payload={"model": "gpt-4.1"}),
            bus.publish("turn_started", session_id=sid, payload={"turn": 1}),
            # custom kind + nested payload
            bus.publish(
                "state_changed",
                session_id=sid,
                payload={"nested": {"a": [1, 2, 3]}, "flag": True, "none": None, "ratio": 0.25},
            ),
            # tool call event (with refs)
            bus.publish(
                "tool_call",
                session_id=sid,
                payload={"name": "file_read", "arguments": {"file_path": "x.py"}},
                refs=["call_1", "call_2"],
            ),
            # memory candidate event (real memory_candidate payload shape)
            bus.publish(
                "memory_candidate",
                session_id=sid,
                payload=MemoryCandidate(
                    content="用户偏好使用中文注释", significance=0.9, session_id=sid,
                ).to_dict(),
            ),
            # global event with an empty session (must not come back in a session replay)
            bus.publish("phase:pre_model_request", session_id="", payload={"global": True}),
        ]
        assert all(ev is not None for ev in published)
        bus.flush()

        fresh = EventSourcedBus("events", ledger_path=path)
        replayed = fresh.replay(sid)

        expected = [ev for ev in published if ev.session_id == sid]
        assert len(replayed) == len(expected) == 5
        assert [_snapshot(ev) for ev in replayed] == [_snapshot(ev) for ev in expected]
        # Replay order follows ascending seq.
        assert [ev.seq for ev in replayed] == sorted(ev.seq for ev in replayed)

    def test_replay_is_idempotent_and_creates_no_events(self, tmp_path):
        """Two consecutive replays return exactly the same result; replay publishes nothing and does not change disk content."""
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
        # Around replay: the number of lines on disk is unchanged and the fresh bus has 0 in-memory events (nothing published).
        assert len(read_jsonl(path)) == lines_before
        assert fresh.count == 0

    def test_replay_does_not_duplicate_real_side_effects(self, tmp_path):
        """Critical: the replayed event stream must not re-trigger real side effects.

        Real side-effect counting point: a real :class:`MemoryLayerStore`
        subscribes to ``memory_candidate`` via ``attach(bus)``, and every
        subscription callback performs a real ``intake`` (write to disk, possible
        merge/promotion). If replay wrongly fanned out to observers, the repeated
        intake would merge buffer-resident entries and promote them to long-term
        memory -- which is observable on real store state.
        """
        ledger = tmp_path / "events" / "side_effect.jsonl"
        mem_root = tmp_path / "memory"
        sid = "sess-side"

        # Phase 1: a real store subscribes to a real bus; publish triggers real intake.
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
        assert len(buffered_ids) == 3          # 3 real intake events were written
        assert store.long_term() == []         # nothing promoted to long-term yet
        buffer_records_before = len(read_json(store._buffer_path, default=[]))

        # Phase 2: simulate a restart -- new bus + new store (same root, reloaded from disk),
        # then attach and replay.
        fresh_bus = EventSourcedBus("events", ledger_path=ledger)
        fresh_store = MemoryLayerStore(root=mem_root)
        fresh_store.attach(fresh_bus)
        assert len(fresh_store.buffer()) == 3  # short-term buffer restored from disk

        replayed = fresh_bus.replay(sid)
        assert len(replayed) == 3

        # If replay fanned out, the 3 candidates would be intaken again -> merge + promote;
        # the real state must stay unchanged.
        assert {entry.id for entry in fresh_store.buffer()} == buffered_ids
        assert fresh_store.long_term() == []
        assert len(read_json(fresh_store._buffer_path, default=[])) == buffer_records_before
        # Replay itself did not publish any new event.
        assert fresh_bus.count == 0

    def test_project_matches_manual_fold(self, tmp_path):
        """The read-model projection matches the result derived manually from the event stream (per its real semantics)."""
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
        # Events from another session must not be counted.
        bus.publish("turn_started", session_id="other", payload={"turn": 99})
        bus.flush()

        fresh = EventSourcedBus("events", ledger_path=path)
        project = fresh.project(sid)
        events = fresh.replay(sid)

        # Fold manually per the implementation semantics, as an independent expectation.
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
        # Concrete numeric assertions (explicit and readable).
        assert project["events"] == 8
        assert project["last_seq"] == 8
        assert project["turns"] == 3
        assert project["tool_calls"] == ["c1", "c2", "c3"]

    def test_replay_is_session_scoped(self, tmp_path):
        """Events from different session ids in one ledger never bleed into each other."""
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
        # An empty session id matches global events only, not "all events".
        assert [ev.session_id for ev in fresh.replay("")] == [""]


class TestCrashRecovery:
    """Recovery semantics after a simulated process crash (the bus is dropped without close/flush)."""

    def test_write_through_recovers_everything(self, tmp_path):
        """flush_batch=1 (write-through): every persisted event is recoverable after a crash."""
        path = tmp_path / "events" / "crash_wt.jsonl"
        bus = EventSourcedBus("events", ledger_path=path, flush_batch=1)
        for i in range(5):
            bus.publish("turn_started", session_id="s", payload={"i": i})
        # Simulate a crash: no close()/flush() call, just drop the instance.
        del bus

        fresh = EventSourcedBus("events", ledger_path=path)
        replayed = fresh.replay("s")
        assert [ev.seq for ev in replayed] == [1, 2, 3, 4, 5]
        assert [ev.payload["i"] for ev in replayed] == [0, 1, 2, 3, 4]

    def test_batched_unflushed_events_are_lost_on_crash(self, tmp_path):
        """Batched mode: unflushed trailing events are lost after a crash (real semantics)."""
        path = tmp_path / "events" / "crash_batch.jsonl"
        bus = EventSourcedBus("events", ledger_path=path, flush_batch=3)
        for i in range(7):
            bus.publish("turn_started", session_id="s", payload={"i": i})
        # Batch = 3: a flush happens every 3 accumulated events -> 6 written, the 7th still in _pending.
        assert len(read_jsonl(path)) == 6
        del bus  # crash, not flushed/closed

        fresh = EventSourcedBus("events", ledger_path=path)
        replayed = fresh.replay("s")
        assert [ev.seq for ev in replayed] == [1, 2, 3, 4, 5, 6]
        # Conclusion: unflushed events are lost in batched mode; only an explicit flush/close guarantees persistence.
        assert all(ev.seq != 7 for ev in replayed)

    def test_explicit_flush_recovers_batched_events(self, tmp_path):
        """Control case: with an explicit flush in batched mode, trailing events are also recoverable."""
        path = tmp_path / "events" / "crash_flush.jsonl"
        bus = EventSourcedBus("events", ledger_path=path, flush_batch=3)
        for i in range(7):
            bus.publish("turn_started", session_id="s", payload={"i": i})
        bus.flush()
        assert len(read_jsonl(path)) == 7
        fresh = EventSourcedBus("events", ledger_path=path)
        assert [ev.seq for ev in fresh.replay("s")] == [1, 2, 3, 4, 5, 6, 7]


# ===========================================================================
# SubTask 10.2 -- replaceable acceptance
# ===========================================================================


def _real_model_infos() -> list:
    """Return real model metadata resolved from the registry (including local models without tool/image support)."""
    return [
        resolve_model_info("gpt-4.1"),                                  # full capability
        resolve_model_info("claude-sonnet-4.6"),                        # full capability + thinking
        resolve_model_info("deepseek-v4-pro"),                          # no image support
        resolve_model_info("gemini-2.5-pro"),                           # no prompt caching support
        resolve_model_info("qwen2.5:7b", provider="ollama"),            # no tool/image support
        resolve_model_info("hf-model", provider="local"),               # no tool/image support
    ]


class TestBackendReplacement:
    """After replacing the backend, capability planning and degraded output stay self-consistent."""

    def test_compute_plan_matches_real_model_info(self):
        """The four planning dimensions match the capability bits of the real ModelInfo, with all downgrade reasons present."""
        infos = _real_model_infos()
        assert len(infos) >= 3
        # Assertions must cover the real "no tool support" and "no image support" entries.
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
        """A real backend's capability methods are ANDed with ModelInfo (only ever stricter)."""
        capable = resolve_model_info("gpt-4.1")
        assert capable.supports_tools and capable.supports_images and capable.supports_prompt_caching

        # Real LocalBackend: with no model loaded, supports_tool_calling() conservatively returns False.
        local = LocalBackend()
        assert local.supports_tool_calling() is False
        plan = compute_plan(capable, backend=local)
        assert plan.use_tools is False
        assert "backend does not support tool calling" in plan.reasons["tools"]
        # Unrestricted dimensions stay enabled (the backend cannot relax, and does not over-restrict).
        assert plan.use_vision is True
        assert plan.use_caching is True

        # Real OllamaBackend: OpenAI protocol, so supports_tool_calling() is True.
        ollama = OllamaBackend(model="llama3.2")
        assert ollama.supports_tool_calling() is True
        plan_ok = compute_plan(capable, backend=ollama)
        assert plan_ok.use_tools is True

        # A backend cannot "relax" a capability the model itself lacks back on.
        weak = resolve_model_info("qwen2.5:7b", provider="ollama")
        assert weak.supports_tools is False
        plan_weak = compute_plan(weak, backend=ollama)
        assert plan_weak.use_tools is False

    def test_degraded_outputs_self_consistent_after_backend_swap(self):
        """After replacing the backend, apply_to_tools / apply_to_messages / fallback output stays valid."""
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

        # Degraded plan (a stricter real backend + a local model without tool/image support).
        weak = resolve_model_info("hf-model", provider="local")
        degraded = compute_plan(weak, backend=LocalBackend())
        assert apply_to_tools(tools_payload, degraded) is None
        stripped = apply_to_messages(messages, degraded)
        assert stripped is not messages
        assert all(part.get("type") != "image_url" for part in stripped[0]["content"])
        fallback = tool_intent_fallback(tools_payload)
        assert "file_read" in fallback and "bash" in fallback

        # Full-capability plan: tools pass through unchanged, messages pass through unchanged.
        capable = resolve_model_info("gpt-4.1")
        ok = compute_plan(capable, backend=OllamaBackend(model="llama3.2"))
        assert apply_to_tools(tools_payload, ok) is tools_payload
        assert apply_to_messages(messages, ok) is messages


class TestMemoryBackendReplacement:
    """After replacing the memory retrieval backend, recall output structure stays valid, traceable, and behaviour-consistent."""

    @staticmethod
    def _seed_store(root) -> tuple[MemoryLayerStore, str, str]:
        """Build a store with a real association edge: e1 matches the query, e2 is reachable only via spread activation."""
        store = MemoryLayerStore(root=root)
        e1 = MemoryEntry(
            content="Deploy pipeline uses Docker containers for releases", type="fact",
        )
        e2 = MemoryEntry(
            content="The release checklist requires a manual approval step", type="fact",
        )
        store.store(e1)
        store.store(e2)
        # Real link: e1 --causal--> e2, used by spread activation.
        assert store.link(e1.id, e2.id, LinkRelation.CAUSES.value, weight=1.0) is not None
        return store, e1.id, e2.id

    def test_local_and_native_backends_both_valid(self, tmp_path):
        """Both backends produce valid, traceable hits; native falls back to local when no index is available."""
        store, e1_id, e2_id = self._seed_store(tmp_path / "memory")
        query = "Deploy pipeline uses Docker containers for releases"

        engine_local = RecallEngine(store, backend="local", record_access=False)
        hits_local = engine_local.recall(query)
        assert engine_local.backend == "local"
        assert hits_local, "本地检索必须至少返回一个命中"

        engine_native = RecallEngine(store, backend="native", record_access=False)
        # With no embedding_fn/index supplied, native is unavailable and silently falls back to local
        # (so both environments pass).
        assert engine_native.backend in ("local", "native")
        hits_native = engine_native.recall(query)

        for hits in (hits_local, hits_native):
            for hit in hits:
                assert isinstance(hit.entry_id, str) and hit.entry_id
                assert isinstance(hit.score, float) and hit.score >= 0.0
                assert isinstance(hit.hops, int) and hit.hops >= 0
                assert isinstance(hit.via_links, list)
                assert isinstance(hit.origin_id, str) and hit.origin_id
                # Traceable: a spread hit must carry a link path and a source seed.
                if hit.hops > 0:
                    assert hit.via_links
                    assert hit.origin_id != hit.entry_id
                    assert all("relation" in edge for edge in hit.via_links)

        # With the same store and same query, falling back to local yields results identical to an explicit local run.
        assert engine_native.backend == "local"
        assert [h.entry_id for h in hits_native] == [h.entry_id for h in hits_local]
        # Spread activation did bring back one non-seed memory (e2 via e1's causal edge).
        spread = [h for h in hits_local if h.hops > 0]
        assert any(h.entry_id == e2_id and h.origin_id == e1_id for h in spread)


class TestToolsetReplacement:
    """After replacing the tool set, the discovery output and degradation semantics change accordingly and stay self-consistent."""

    @staticmethod
    def _discovery(*tools) -> ToolDiscovery:
        """Build a ToolDiscovery over a fresh registry holding exactly the given tools."""
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        return ToolDiscovery(registry)

    def test_active_tools_payload_changes_with_toolset(self):
        """The active tools payload and the fallback listing track the registered tool set."""
        one = self._discovery(file_read_tool)
        three = self._discovery(file_read_tool, file_write_tool, file_edit_tool)

        payload_one = one.get_active_tools_payload("sess")
        payload_three = three.get_active_tools_payload("sess")

        names_one = {item["function"]["name"] for item in payload_one}
        names_three = {item["function"]["name"] for item in payload_three}
        assert names_one == {"file_read"}
        assert names_three == {"file_read", "file_write", "file_edit"}
        assert payload_one != payload_three

        # The tool listing rendered by the fallback matches the discovery output.
        fallback_one = tool_intent_fallback(payload_one)
        fallback_three = tool_intent_fallback(payload_three)
        assert "file_read" in fallback_one and "file_write" not in fallback_one
        assert all(name in fallback_three for name in names_three)

    def test_degradation_semantics_unaffected_by_toolset(self):
        """Replacing the tool set does not affect degradation semantics: when degraded, apply_to_tools still returns None."""
        three = self._discovery(file_read_tool, file_write_tool, file_edit_tool)
        payload = three.get_active_tools_payload("sess")

        degraded = compute_plan(resolve_model_info("hf-model", provider="local"))
        assert apply_to_tools(payload, degraded) is None

        ok = compute_plan(resolve_model_info("gpt-4.1"))
        assert apply_to_tools(payload, ok) is payload


class TestCombinedReplacement:
    """After all three are replaced at once, the full call chain still runs without error and produces valid output structure."""

    def test_combined_swap_is_self_consistent(self, tmp_path):
        """Swap backend + model metadata + tool set + memory backend together and verify the whole chain."""
        # 1) Replace the backend (real LocalBackend) + model metadata (real local-class model).
        model_info = resolve_model_info("qwen2.5:7b", provider="ollama")
        backend = LocalBackend()
        plan = compute_plan(model_info, backend=backend)

        # 2) Replace the tool set (real ToolRegistry + real builtin tools).
        registry = ToolRegistry()
        for tool in (file_read_tool, file_write_tool, file_edit_tool):
            registry.register(tool)
        discovery = ToolDiscovery(registry)
        payload = discovery.get_active_tools_payload("sess-combined")
        assert {item["function"]["name"] for item in payload} == {
            "file_read", "file_write", "file_edit",
        }

        # 3) Replace the memory backend (real store + a native request).
        store = MemoryLayerStore(root=tmp_path / "memory")
        entry = MemoryEntry(content="Combined replacement smoke entry", type="fact")
        store.store(entry)
        engine = RecallEngine(store, backend="native", record_access=False)

        # Full call chain: compute_plan -> apply_to_* -> fallback -> recall.
        assert apply_to_tools(payload, plan) is None  # local model downgrades
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
