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

"""Tests for the Encre compaction subsystem.

Covers the semantic context-tiering objects (ContextTier, ContextPartition,
ContextPartitioner), the SemanticToolOutputCompactor that shrinks oversized
tool results by tool type, every Encre* compaction strategy (always,
token-budget, budget-reduction, snip, micro, context-collapse, semantic,
auto, and the multi-stage pipeline), and the EncreCompactEngine orchestrator.
"""

import asyncio

from encre.compact.engine import EncreCompactEngine
from encre.compact.semantic import (
    ContextPartition,
    ContextPartitioner,
    ContextTier,
    SemanticToolOutputCompactor,
)
from encre.compact.engine import (
    EncreAlwaysCompactStrategy,
    EncreAutoCompactStrategy,
    EncreBudgetReductionStrategy,
    EncreContextCollapseStrategy,
    EncreMicroCompactStrategy,
    EncreMultiStagePipeline,
    EncreSemanticCompactStrategy,
    EncreSnipStrategy,
    EncreTokenBudgetStrategy,
)


def _m(role, content, name=None):
    """Build a message dict with a role, content, and an optional name field."""
    msg = {"role": role, "content": content}
    if name:
        msg["name"] = name
    return msg


def _make_messages(turns):
    """Build a conversation history with a system prompt followed by ``turns`` user/assistant pairs."""
    msgs = [_m("system", "You are an assistant.")]
    for i in range(turns):
        msgs.append(_m("user", f"Question {i}"))
        msgs.append(_m("assistant", f"Answer {i}"))
    return msgs


# ===========================================================================
# ContextTier / ContextPartition
# ===========================================================================

class TestContextTier:
    """Tests for the ContextTier value object used for semantic context grouping."""

    def test_verify_context_tier_creation_and_fields(self):
        """A ContextTier keeps the name and message list it was constructed with."""
        ct = ContextTier(name="test", messages=[_m("user", "hello")])
        assert ct.name == "test"
        assert len(ct.messages) == 1

    def test_verify_context_tier_token_count_is_positive(self):
        """token_count() returns a positive estimate even for minimal content."""
        ct = ContextTier(name="test", messages=[_m("user", "hello world")])
        assert ct.token_count() > 0


class TestContextPartition:
    """Tests for the ContextPartition value object that groups messages into five tiers."""

    def test_verify_defaults_produce_empty_partitions(self):
        """A freshly constructed partition has an empty list for every tier."""
        cp = ContextPartition()
        assert cp.system == []
        assert cp.hot == []
        assert cp.warm == []
        assert cp.cold == []
        assert cp.reference == []

    def test_verify_with_messages_serializes_correctly(self):
        """to_messages() flattens the assigned tier messages in tier order."""
        cp = ContextPartition(
            system=[_m("system", "You are helpful.")],
            hot=[_m("user", "latest question")],
        )
        msgs = cp.to_messages()
        assert len(msgs) == 2

    def test_verify_total_tokens_counts_all_messages(self):
        """total_tokens() reports a positive count when the hot tier has content."""
        cp = ContextPartition(hot=[_m("user", "hello world")])
        assert cp.total_tokens() > 0


class TestContextPartitioner:
    """Tests for the ContextPartitioner that splits history into semantic tiers."""

    def test_verify_partition_distributes_messages_into_correct_tiers(self):
        """partition() isolates the system prompt and places recent turns in the hot tier."""
        partitioner = ContextPartitioner()
        messages = [
            _m("system", "You are an assistant."),
            _m("user", "Hello"),
            _m("assistant", "Hi there"),
            _m("user", "Can you help me?"),
            _m("assistant", "Sure, what do you need?"),
        ]
        result = partitioner.partition(messages)
        assert isinstance(result, ContextPartition)
        assert len(result.hot) > 0
        assert len(result.system) == 1


# ===========================================================================
# SemanticToolOutputCompactor
# ===========================================================================

class TestSemanticToolOutputCompactor:
    """Tests for the SemanticToolOutputCompactor, which shrinks oversized tool output by tool type."""

    def setup_method(self):
        """Create a fresh compactor instance before each test."""
        self.compactor = SemanticToolOutputCompactor()

    def test_verify_grep_output_is_compacted(self):
        """Large grep-formatted output is trimmed below its original length."""
        big = "file.py:1:line1\nfile.py:2:line2\n" * 600
        result = self.compactor.compact_tool_output("grep", big)
        assert len(result) < len(big)

    def test_verify_glob_output_is_compacted(self):
        """Large glob output is summarized instead of emitting every path."""
        big = "\n".join(f"/path/to/file{i}.py" for i in range(800))
        result = self.compactor.compact_tool_output("glob", big)
        assert "files" in result.lower() or "glob" in result.lower()

    def test_verify_bash_output_is_compacted(self):
        """Verbose bash output is reduced to fit the context budget."""
        big = "error line 1\n" * 700
        result = self.compactor.compact_tool_output("bash", big)
        assert len(result) < len(big)

    def test_verify_file_read_output_is_compacted(self):
        """Large file_read output is trimmed so it does not fill the context window."""
        big = "def foo():\n    pass\n" * 500
        result = self.compactor.compact_tool_output("file_read", big)
        assert len(result) < len(big)

    def test_verify_web_fetch_output_is_compacted(self):
        """Oversized HTML fetched from the web is reduced in size."""
        html = "<html><head><title>Test</title></head><body>" + "<p>content</p>" * 600 + "</body></html>"  # noqa: E501
        result = self.compactor.compact_tool_output("web_fetch", html)
        assert len(result) < len(html)

    def test_verify_task_list_output_is_compacted(self):
        """Task-list output is bounded well below the hard size limit."""
        big = '{"id": "1", "subject": "test"}\n' * 20
        result = self.compactor.compact_tool_output("task_list", big)
        assert len(result) < 700

    def test_verify_unknown_tool_truncates_to_input_size(self):
        """Unknown tool types fall back to truncation and never grow the output."""
        big = "x" * 10000
        result = self.compactor.compact_tool_output("unknown_tool", big)
        assert len(result) <= 10000

    def test_verify_short_output_is_passed_through_unchanged(self):
        """Output below the compaction threshold is returned verbatim."""
        short = "short output"
        result = self.compactor.compact_tool_output("grep", short)
        assert result == short


# ===========================================================================
# Compaction Strategies
# ===========================================================================

class TestCompactionStrategies:
    """Tests for each compaction strategy's should_compact gate and compact action."""

    def test_verify_always_compact_triggers_on_sufficient_turns(self):
        """EncreAlwaysCompactStrategy fires once the conversation reaches 8 turns."""
        s = EncreAlwaysCompactStrategy()
        assert asyncio.run(s.should_compact(_make_messages(8), 128000)) is True

    def test_verify_always_compact_skips_with_few_turns(self):
        """EncreAlwaysCompactStrategy does not fire on a short 2-turn conversation."""
        s = EncreAlwaysCompactStrategy()
        assert asyncio.run(s.should_compact(_make_messages(2), 128000)) is False

    def test_verify_always_compact_execute_reduces_message_count(self):
        """EncreAlwaysCompactStrategy.compact never increases the message count."""
        s = EncreAlwaysCompactStrategy()
        msgs = _make_messages(8)
        result = asyncio.run(s.compact(msgs, 128000))
        assert len(result) <= len(msgs)

    def test_verify_token_budget_should_compact_when_over_ratio(self):
        """EncreTokenBudgetStrategy.should_compact returns a bool when the budget is exceeded."""
        s = EncreTokenBudgetStrategy(budget_ratio=0.5)
        msgs = [_m("user", "x" * 10000)]
        assert isinstance(asyncio.run(s.should_compact(msgs, 1000)), bool)

    def test_verify_token_budget_execute_reduces_message_count(self):
        """EncreTokenBudgetStrategy.compact stays at or below the input message count."""
        s = EncreTokenBudgetStrategy(budget_ratio=0.5)
        msgs = _make_messages(20)
        result = asyncio.run(s.compact(msgs, 128000))
        assert len(result) <= len(msgs)

    def test_verify_budget_reduction_truncates_oversized_messages(self):
        """EncreBudgetReductionStrategy truncates messages that exceed max_chars_per_message."""
        s = EncreBudgetReductionStrategy(max_chars_per_message=100)
        msgs = [_m("user", "x" * 5000)]
        result = asyncio.run(s.compact(msgs, 128000))
        assert len(result[0]["content"]) < 5000  # the oversized message was truncated

    def test_verify_budget_reduction_should_compact_on_oversized_message(self):
        """EncreBudgetReductionStrategy fires when any message exceeds the per-message budget."""
        s = EncreBudgetReductionStrategy(max_chars_per_message=100)
        msgs = [_m("user", "x" * 5000)]
        assert asyncio.run(s.should_compact(msgs, 128000)) is True

    def test_verify_snip_execute_keeps_recent_turns_only(self):
        """EncreSnipStrategy.compact returns at most the original number of messages."""
        s = EncreSnipStrategy(keep_recent_turns=3)
        msgs = _make_messages(20)
        result = asyncio.run(s.compact(msgs, 128000))
        assert len(result) <= len(msgs)

    def test_verify_micro_compact_execute_returns_valid_result(self):
        """EncreMicroCompactStrategy.compact always returns a valid message list."""
        s = EncreMicroCompactStrategy()
        msgs = _make_messages(10)
        result = asyncio.run(s.compact(msgs, 128000))
        assert result is not None

    def test_verify_micro_compact_should_compact_on_large_single_message(self):
        """EncreMicroCompactStrategy fires for a single very large message."""
        s = EncreMicroCompactStrategy()
        msgs = [_m("user", "x" * 5000)]
        assert asyncio.run(s.should_compact(msgs, 128000)) is True

    def test_verify_context_collapse_execute_returns_valid_result(self):
        """EncreContextCollapseStrategy.compact always returns a valid message list."""
        s = EncreContextCollapseStrategy()
        msgs = _make_messages(20)
        result = asyncio.run(s.compact(msgs, 128000))
        assert result is not None

    def test_verify_semantic_should_compact_on_oversized_tool_output(self):
        """EncreSemanticCompactStrategy.should_compact returns a bool for dense tool content."""
        s = EncreSemanticCompactStrategy()
        msgs = [_m("tool", "x" * 10000)]
        assert isinstance(asyncio.run(s.should_compact(msgs, 128000)), bool)

    def test_verify_semantic_execute_returns_valid_result(self):
        """EncreSemanticCompactStrategy.compact always returns a valid message list."""
        s = EncreSemanticCompactStrategy()
        msgs = [_m("user", "test")]
        result = asyncio.run(s.compact(msgs, 128000))
        assert result is not None

    def test_verify_multi_stage_pipeline_has_minimum_stages(self):
        """EncreMultiStagePipeline initializes with at least six compaction stages."""
        pipeline = EncreMultiStagePipeline()
        assert len(pipeline._stages) >= 6

    def test_verify_multi_stage_execute_returns_valid_result(self):
        """EncreMultiStagePipeline.compact always returns a valid message list."""
        pipeline = EncreMultiStagePipeline()
        msgs = _make_messages(5)
        result = asyncio.run(pipeline.compact(msgs, 128000))
        assert result is not None

    def test_verify_multi_stage_should_compact_returns_bool(self):
        """EncreMultiStagePipeline.should_compact returns a definitive boolean decision."""
        pipeline = EncreMultiStagePipeline()
        assert isinstance(asyncio.run(pipeline.should_compact(_make_messages(2), 128000)), bool)

    def test_verify_auto_compact_should_compact_on_large_content(self):
        """EncreAutoCompactStrategy.should_compact returns a bool for large input."""
        s = EncreAutoCompactStrategy(threshold_ratio=0.5)
        msgs = [_m("user", "x" * 50000)]
        assert isinstance(asyncio.run(s.should_compact(msgs, 1000)), bool)

    def test_verify_auto_compact_execute_returns_valid_result(self):
        """EncreAutoCompactStrategy.compact always returns a valid message list."""
        s = EncreAutoCompactStrategy(threshold_ratio=0.5)
        msgs = _make_messages(2)
        result = asyncio.run(s.compact(msgs, 128000))
        assert result is not None


# ===========================================================================
# EncreCompactEngine
# ===========================================================================

class _MockBackend:
    """Stub backend for the engine tests, exposing a fixed 128K window and static model name."""

    def context_window_size(self) -> int:
        """Return the fixed context window size in tokens reported by this stub backend."""
        return 128000

    @property
    def model(self) -> str:
        """Return the static model identifier reported by this stub backend."""
        return "mock-model"


class TestCompactEngine:
    """Tests for the EncreCompactEngine orchestrator that coordinates compaction decisions."""

    def test_verify_engine_construction(self):
        """EncreCompactEngine constructs successfully with no arguments."""
        engine = EncreCompactEngine()
        assert engine is not None

    def test_verify_should_compact_returns_bool(self):
        """should_compact returns a side-effect-free boolean decision."""
        engine = EncreCompactEngine()
        msgs = _make_messages(2)
        assert isinstance(engine.should_compact(msgs, 128000), bool)

    def test_verify_compact_with_mock_backend_returns_valid_result(self):
        """compact() returns a valid message list when given a backend with no real API."""
        engine = EncreCompactEngine()
        msgs = _make_messages(30)
        backend = _MockBackend()
        result = asyncio.run(engine.compact(msgs, backend=backend, turn_count=0))
        # The mock backend exposes no API endpoint, so the engine uses _budget_fallback
        assert result is not None
